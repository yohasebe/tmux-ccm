"""State detection orchestrator.

This module is the **thin orchestration layer** of ccm's state
machine. It glues three submodules together once per detection
cycle:

  1. `ccm_pane_state` — process tree + capture-pane → raw state
     (`detect_window_raw`, `find_claude_pid`, …)
  2. `ccm_activity`   — event-log + JSONL → activity → state
     (the **primary** detection path; returns None when it has no
     authoritative answer)
  3. `ccm_rules`      — declarative rule table + evaluator
     (the **legacy** safety net for cases where activity defers)

`detect_window_state` runs both paths and prefers the event-log
result; `apply_actions` writes the resolved state to tmux options
(`@ccm_prev_state`, `@ccm_completed_at`, `@ccm_bg_active`) and
records SHELL transitions for cluster-crash detection.
`build_detection_context` is the I/O-heavy input gatherer
(`tmux_cmd`, `ps_snapshot`, file reads).

This module also owns the optional debug trace (`CCM_DEBUG_TRACE`
env var) which writes one JSONL record per detection cycle.

## `@ccm_prev_state` write sites (2, intentionally distributed)

1. `apply_actions` → `_set_win_state` (this file)
   Detection-pipeline write. Runs every slow-path scan
   (inject_status poll, dashboard refresh) and records the
   resolved state so the next scan can key transition-based rules
   off `ctx.prev_state`.

2. `ccm_write_signal` (`hooks/lib.sh`)
   Hot-path write from Claude Code hook scripts. Bypasses the
   Python detector so the statusline reflects BUSY / PERMIT /
   SHELL with zero polling latency. Routing through Python would
   add 30–80 ms of interpreter startup per hook event, which
   defeats the purpose of instant status updates.

When changing state-transition semantics, audit both sites.
"""

import json
import os
import time
from typing import Optional

import ccm_canaries
import ccm_core  # late-bound for tmux_cmd / find_process_age
import ccm_jsonl
import ccm_signals
from ccm_activity import derive_state_from_events
from ccm_pane_state import detect_window_raw, find_claude_pid, read_work_clock
from ccm_rules import (
    Action,
    DetectionContext,
    Rule,
    evaluate_rules,
)


def _set_win_state(win_target, state):
    """Write @ccm_prev_state to the window."""
    ccm_core.tmux_cmd("set-option", "-wt", win_target, "@ccm_prev_state", state)


def build_detection_context(win_target, project_dir, prev_state,
                            panes_cache, ps_lines, own_pgid,
                            prev_bg_active: bool = False,
                            cached_session_id: Optional[str] = None,
                            cached_work_clock=None,
                            ) -> DetectionContext:
    """Gather all inputs needed for rule evaluation.

    Read-only side effects only (tmux query, ps snapshot, file reads).
    The returned context is an immutable snapshot.
    """
    now = int(time.time())

    prev_work_clock = read_work_clock(win_target, cached=cached_work_clock)

    observed_clocks = []
    raw = detect_window_raw(win_target, panes_cache, ps_lines, own_pgid,
                            stored_clock=prev_work_clock,
                            clock_out=observed_clocks)
    work_clock = observed_clocks[0] if observed_clocks else None

    # Find the window's primary claude_pid (first pane that hosts one).
    # Used to resolve the exact JSONL path via the runtime session file
    # at ~/.claude/sessions/{pid}.json AND to measure how long Claude
    # has been running (input to the startup-transient rule).
    claude_pid = None
    claude_pid_age = -1
    for entry in panes_cache:
        wt = entry[0]
        pane_pid = entry[1]
        if wt != win_target:
            continue
        # Skip CCM_IGNORE'd panes: their session must never become the
        # window's tracked session (that is the whole point of ignore).
        if ccm_core._pane_is_ignored(entry):
            continue
        cp = find_claude_pid(pane_pid, ps_lines)
        if cp:
            claude_pid = cp
            claude_pid_age = ccm_core.find_process_age(cp, ps_lines)
            break

    # Session_id resolution: claude_pid → ~/.claude/sessions/<pid>.json
    # → sessionId. This is the primary key for hook signal / events
    # files. Cache on the @ccm_session_id tmux window option so the
    # fast path (statusline, evaluate_fast) can read it without
    # repeating the pid chain. Re-write only when the value changes
    # to avoid pointless tmux churn on every scan.
    session_id = None
    if claude_pid is not None:
        # Pass `ps_lines` so `read_session_info` can verify the
        # session_info file's `startedAt` against the live process's
        # etime — defends against pid recycling where a prior
        # claude session's json file lingers under the same pid.
        info = ccm_jsonl.read_session_info(claude_pid, ps_lines=ps_lines)
        if info:
            session_id = info.get("sessionId") or info.get("session_id")
    if win_target:
        # `cached_session_id` is the value already read from the
        # bulk `list-windows` query in `build_project_list`, so we
        # avoid an extra `show-option` subprocess per project per
        # cycle. When it's None (e.g. a direct caller without the
        # cached value, like a one-off `cmd_debug_trace` invocation)
        # we still pay one show-option as a defensive fallback.
        prev_sid = cached_session_id if cached_session_id is not None else (
            ccm_core.tmux_cmd(
                "show-option", "-w", "-t", win_target, "-qv", "@ccm_session_id"
            )
        )
        if session_id and session_id != prev_sid:
            ccm_core.tmux_cmd(
                "set-option", "-w", "-t", win_target,
                "@ccm_session_id", session_id,
            )
        elif not session_id and prev_sid:
            # claude exited / no session yet — clear cached value so
            # the fast path doesn't read a stale session_id's signal
            ccm_core.tmux_cmd(
                "set-option", "-w", "-t", win_target, "-u", "@ccm_session_id"
            )

    hook_state = ""
    hook_ts = 0
    hook_age = -1
    if project_dir:
        # `session_id or ""`: when no session_id was resolved (SHELL
        # window — no claude pid — or session_info unreadable), the
        # `@ccm_session_id` cache was already cleared above, so "" is
        # the authoritative "no session" form. Passing None here would
        # make `read_hook_signal` fall back to a per-project
        # `list-windows -a` tmux subprocess on EVERY detection cycle.
        sig = ccm_signals.read_hook_signal(
            project_dir, session_id=session_id or "")
        if sig is not None:
            hook_ts, hook_state, _detail = sig
            # SHELL hook signal is ignored: process tree is authoritative
            # for SHELL; trusting a stale SHELL signal while raw=IDLE
            # causes false SHELL after Claude restarts.
            if hook_state == "SHELL":
                hook_state = ""
                hook_ts = 0
            else:
                hook_age = now - hook_ts

    if project_dir:
        jsonl_age, jsonl_last_stop_reason = ccm_jsonl.read_jsonl_tail_info(
            project_dir, claude_pid=claude_pid
        )
    else:
        jsonl_age, jsonl_last_stop_reason = -1, None

    return DetectionContext(
        raw=raw,
        hook_state=hook_state,
        hook_ts=hook_ts,
        hook_age=hook_age,
        prev_state=prev_state,
        jsonl_age=jsonl_age,
        jsonl_last_stop_reason=jsonl_last_stop_reason,
        claude_pid_age=claude_pid_age,
        now=now,
        prev_bg_active=prev_bg_active,
        session_id=session_id,
        work_clock=work_clock,
        prev_work_clock=prev_work_clock,
    )


def apply_actions(win_target, project_dir, ctx: DetectionContext, rule: Rule,
                  state: str, event_log_state=None) -> str:
    """Execute the side effects declared by a matched rule.

    Returns the resolved state string.

    `event_log_state` is the result of `derive_state_from_events`
    when the event-log path is enabled (the default); it is
    forwarded to `_trace_scan` so the debug trace records both
    states on every scan regardless of which one is committed.
    """
    action = rule.action

    # Optional trace log — useful for debugging false-BUSY / false-IDLE
    # reports without requiring a second ccm process. Activated by
    # setting CCM_DEBUG_TRACE to a file path in the environment of the
    # process that runs detection (inject_status, dashboard, etc.).
    # Each scan cycle appends one JSON-per-line record with every
    # DetectionContext input plus the matched rule + resolved state.
    # Writing happens here (after the rule is known, before side
    # effects) so a malformed trace never corrupts real state.
    _trace_scan(win_target, ctx, rule, state, event_log_state=event_log_state)

    # Record SHELL transitions for cluster-crash detection (#48069).
    # A transition into SHELL from a known *active* state (BUSY /
    # IDLE / PERMIT) is a real session exit and gets pushed.
    if state == "SHELL" and ctx.prev_state in (
        "BUSY", "IDLE", "PERMIT"
    ):
        ccm_canaries._push_shell_transition(win_target)

    if action == Action.HOLD_NO_WRITE:
        return state

    # Work-clock persistence. The tick check in ccm_pane_state
    # compares the window's current clock against the value stored
    # here last pass — and that history cannot live in-process,
    # because tmux spawns the periodic path (ccm inject-status, the
    # one auto-exit runs on) fresh every status-interval. Write on
    # CHANGE only: a static frame must never refresh the timestamp,
    # because its age is the verdict. Same shape as the
    # @ccm_session_id cache above, including clearing when the clock
    # disappears so a later identical footer starts fresh.
    prev_clock_str = ctx.prev_work_clock[0] if ctx.prev_work_clock else None
    if ctx.work_clock != prev_clock_str:
        if ctx.work_clock is None:
            ccm_core.tmux_cmd("set-option", "-wut", win_target, "@ccm_work_clock")
            ccm_core.tmux_cmd("set-option", "-wut", win_target, "@ccm_work_clock_ts")
        else:
            ccm_core.tmux_cmd("set-option", "-wt", win_target,
                              "@ccm_work_clock", ctx.work_clock)
            ccm_core.tmux_cmd("set-option", "-wt", win_target,
                              "@ccm_work_clock_ts", str(ctx.now))

    # Action.DEFAULT — set @ccm_prev_state.
    # Skip the write when the value is already what we'd set. On a
    # 13-window dashboard refreshing twice a second, a tmux subprocess
    # per window dominates the slow path — and the steady state is
    # mostly "everyone IDLE", so the overwhelming majority of writes
    # are no-ops. The hot path now pays one tmux subprocess only when
    # a state actually transitions.
    if state != ctx.prev_state:
        _set_win_state(win_target, state)

    # Set @ccm_completed_at when transitioning from BUSY/PERMIT to IDLE.
    # This is a display-layer marker — the `* elapsed` indicator
    # shows for COMPLETED_AT_TIMEOUT seconds after the transition.
    if state == "IDLE" and ctx.prev_state in ("BUSY", "PERMIT"):
        ccm_core.tmux_cmd("set-option", "-wt", win_target, "@ccm_completed_at", str(ctx.now))
    elif state != "IDLE" and ctx.prev_state == "IDLE":
        # Clear `@ccm_completed_at` when the project leaves IDLE.
        # The display layer already suppresses the marker for
        # non-IDLE states, but we also clear the stored value so
        # that an unusual sequence — e.g. IDLE → SHELL → IDLE
        # without going through BUSY (claude crash + restart) —
        # cannot resurrect a stale "* 5s" from the previous session
        # when the project returns to IDLE. Fires at most once per
        # transition, so the perf cost is one tmux subprocess on
        # IDLE→non-IDLE edges only.
        ccm_core.tmux_cmd("set-option", "-wut", win_target, "@ccm_completed_at")

    # Background-activity affordance. State captures "whose turn
    # it is" (BUSY = claude is responding, IDLE = user has the
    # ball). But process-tree detection sets raw=BUSY whenever a
    # tool grandchild exists — including leftover dev servers /
    # background scripts claude started but no longer owns.
    # Surface the discrepancy as a `(bg)` UI suffix so the user
    # knows "claude is at rest, but something it spawned is still
    # alive". Written when committed state is IDLE but raw says
    # BUSY; cleared whenever they agree, so a stale flag cannot
    # survive a state change.
    if state == "IDLE" and ctx.raw == "BUSY":
        # Only write when transitioning into bg-active mode.
        if not ctx.prev_bg_active:
            ccm_core.tmux_cmd("set-option", "-wt", win_target, "@ccm_bg_active", "1")
    elif ctx.prev_bg_active:
        # Clear when no longer applicable, but only when actually
        # set. Most windows never enter bg-active mode; unconditional
        # unset would add one tmux subprocess per window per refresh
        # cycle for no benefit.
        ccm_core.tmux_cmd("set-option", "-wut", win_target, "@ccm_bg_active")

    return state


def _event_log_enabled():
    """Return True if the event-log detection path should run.

    Reads CCM_USE_EVENT_LOG. Unset (the default) and any truthy
    value enable the event-log path; falsy values (``""`` / ``"0"``
    / ``"off"`` / ``"no"`` / ``"false"``, case-insensitive) act as
    a diagnostic kill-switch that forces legacy-only operation.
    """
    raw_env = os.environ.get("CCM_USE_EVENT_LOG")
    if raw_env is None:
        return True
    return raw_env.strip().lower() not in ("", "0", "off", "no", "false")


def resolve_state_from_context(ctx: DetectionContext, project_dir: str):
    """Single decision point that picks the resolved state for a
    built `DetectionContext`. Returns
    `(state, matched_rule, event_log_state)`:

      - `state` — the state to commit (BUSY / IDLE / PERMIT / SHELL /
        DOWN). The event-log primary path takes priority when it
        returns a non-None answer; otherwise the legacy rule-table
        result is used.
      - `matched_rule` — the rule from `DETECTION_RULES` that fired
        (always returned; needed for `apply_actions` even when the
        event-log path overrode the state, because the rule's
        `action` and `phase` drive side effects and tracing).
      - `event_log_state` — the raw output of
        `derive_state_from_events` (or None when the kill-switch
        `CCM_USE_EVENT_LOG=off` is set, or when the primary path
        deferred). Forwarded to the trace writer so a diff between
        the two paths is visible in observation logs.

    This function is the "two-path" merge in one place. The
    orchestrator just calls it.
    """
    rule, legacy_state = evaluate_rules(ctx)

    event_log_state = None
    # raw=IGNORED short-circuits the event-log path: the verdict is
    # about visibility (every claude pane is deliberately unseen), not
    # activity, and the log belongs to sessions ccm chose not to
    # watch — their hooks early-exit, so anything in it is stale and
    # must not resurrect a claim. It would also read pid_present as
    # False and answer SHELL, overriding the verdict.
    if _event_log_enabled() and ctx.raw != "IGNORED":
        # `ctx.session_id or ""` — same N+1 guard as the hook-signal
        # read in build_detection_context: a SHELL window has no
        # session_id, and None would trigger a per-cycle tmux
        # `list-windows -a` fallback inside `_events_log_path`.
        events = ccm_signals.read_events_tail(
            project_dir, session_id=ctx.session_id or "")
        # pid_present: the legacy raw detection already resolved
        # SHELL when no claude process is present, so raw not in
        # ("SHELL", "DOWN") is a reliable proxy without a second ps
        # scan. IGNORED's visible panes host no claude either.
        pid_present = ctx.raw not in ("SHELL", "DOWN", "IGNORED")
        event_log_state = derive_state_from_events(
            events=events,
            jsonl_stop_reason=ctx.jsonl_last_stop_reason,
            pid_present=pid_present,
            claude_pid_age=ctx.claude_pid_age,
            raw=ctx.raw,
            jsonl_age=ctx.jsonl_age,
            now=ctx.now,
        )

    state = event_log_state if event_log_state is not None else legacy_state
    return state, rule, event_log_state


def detect_window_state(win_target, project_dir, prev_state,
                        panes_cache, ps_lines, own_pgid,
                        prev_bg_active: bool = False,
                        cached_session_id: Optional[str] = None,
                        cached_work_clock=None):
    """Full detection pipeline. Returns the resolved state string.

    Three-line orchestrator:
      1. `build_detection_context` — gather inputs (I/O heavy)
      2. `resolve_state_from_context` — event-log primary, legacy
         rules as fallback (pure)
      3. `apply_actions` — execute tmux / file side effects

    `CCM_USE_EVENT_LOG=off` opts out (legacy only) — the
    diagnostic kill-switch lives inside `resolve_state_from_context`.
    """
    ctx = build_detection_context(
        win_target, project_dir, prev_state,
        panes_cache, ps_lines, own_pgid,
        prev_bg_active=prev_bg_active,
        cached_session_id=cached_session_id,
        cached_work_clock=cached_work_clock,
    )
    state, rule, event_log_state = resolve_state_from_context(ctx, project_dir)
    return apply_actions(win_target, project_dir, ctx, rule, state,
                         event_log_state=event_log_state)


# ─── Debug trace (CCM_DEBUG_TRACE env var) ───
# When CCM_DEBUG_TRACE is set to a file path, every detection cycle
# emits a JSON line capturing the full DetectionContext, matched
# rule, and resolved state. This gives us one-shot visibility into
# why a given project is in a given state without needing to spin up
# `ccm debug trace` in a second pane — useful when the report is
# "it happened once last week" rather than "it's happening right
# now".
#
# Writing is best-effort (swallows I/O errors) so a broken trace
# file never blocks real detection. The writer uses line-buffered
# append so multi-project scans interleave correctly.
#
# Safety cap: at TRACE_MAX_BYTES the writer silently stops appending
# to prevent a forgotten CCM_DEBUG_TRACE from exhausting disk. One
# sentinel line is written at the threshold so a user returning to
# the log finds the reason writes stopped. The cap is resolved from
# CCM_TRACE_MAX_BYTES (default 100 MB) at module load; tuning it
# requires restarting the tmux server that launches inject-status.
TRACE_MAX_BYTES = int(
    os.environ.get("CCM_TRACE_MAX_BYTES", str(100 * 1024 * 1024))
)


def _trace_scan(win_target, ctx, rule, state, event_log_state=None):
    """Append one JSON record per detection cycle when CCM_DEBUG_TRACE
    is set. Records include every DetectionContext field plus the
    rule name and resolved state. Failure to open/write the trace
    file is silently ignored — this path must never break detection.

    When the event-log path is active (CCM_USE_EVENT_LOG), the
    derived state is included as `event_log_state` alongside the
    legacy rule result. A `diff=true` field flags rows where the
    two disagree, so `jq -c 'select(.diff)'` isolates the interesting
    scans during observation.

    `CCM_TRACE_ONLY_DIFF=1` (any truthy value) restricts writes to
    rows where the legacy and event-log derivations disagree. Lets
    long-running traces sit on a busy multi-project tmux without
    hitting the size cap — the file only grows when a diff actually
    shows up. Has no effect when the event-log path is disabled
    (nothing to diff against, so the file would stay empty).

    Above `TRACE_MAX_BYTES` the writer emits a single sentinel line
    (so the reason for the gap is visible in the log) and stops
    appending. The stat + writes stay best-effort; a missing file
    or permission error is swallowed.
    """
    path = os.environ.get("CCM_DEBUG_TRACE")
    if not path:
        return
    only_diff = os.environ.get(
        "CCM_TRACE_ONLY_DIFF", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if only_diff and (event_log_state is None or event_log_state == state):
        # No disagreement to record. Skip before any stat/write I/O
        # so the hot path stays cheap on quiet scans.
        return
    try:
        size = -1
        try:
            size = os.path.getsize(path)
        except OSError:
            # File does not exist yet — treat as size 0 and proceed.
            size = 0
        if size >= TRACE_MAX_BYTES:
            # Write one sentinel line for the user then stop. Once the
            # sentinel itself is past the cap we no longer need to
            # write anything — the next append would just grow the
            # file past the cap again. Use a size delta check (any
            # bytes past the cap) so the sentinel is emitted exactly
            # once per process.
            if size < TRACE_MAX_BYTES + 200:
                try:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "t": ctx.now,
                            "event": "trace_cap_reached",
                            "cap_bytes": TRACE_MAX_BYTES,
                            "note": "writes suspended; unset CCM_DEBUG_TRACE or set CCM_TRACE_MAX_BYTES higher",
                        }) + "\n")
                except OSError:
                    pass
            return
        record = {
            "t": ctx.now,
            "target": win_target,
            "raw": ctx.raw,
            "prev": ctx.prev_state,
            "hook_state": ctx.hook_state,
            "hook_age": ctx.hook_age,
            "jsonl_age": ctx.jsonl_age,
            "jsonl_stop": ctx.jsonl_last_stop_reason,
            "claude_pid_age": ctx.claude_pid_age,
            "rule": rule.name,
            "phase": rule.phase,
            "state": state,
            "action": rule.action.value,
        }
        if event_log_state is not None:
            record["event_log_state"] = event_log_state
            if event_log_state != state:
                record["diff"] = True
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
