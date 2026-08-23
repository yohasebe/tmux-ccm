"""Declarative state-detection rule table + evaluator.

This module owns the **legacy** detection path: the
priority-ordered `DETECTION_RULES` table and its supporting types
(`DetectionContext`, `Rule`, `Action`, `USE_RAW`). It runs as the
safety net when `ccm_activity.derive_state_from_events` returns
None (empty event log, malformed records, unknown event types,
or `session_end` with a live pid).

The slow path orchestrator (`ccm_detection.detect_window_state`)
runs both paths and prefers the event-log result; this table is
the deterministic fallback.

The fast path (`evaluate_fast`) — used by the statusline
inject-status helper — runs the same `DETECTION_RULES` against a
synthetic context built from `prev_state` plus a hook-signal read,
without ps/capture-pane I/O. One source of truth for legacy state
transitions across both pipelines.

Each rule carries a `phase` annotation (`shell` / `startup` /
`midturn` / `between_tools` / `idle` / `permit`, or `None` for
real catch-all passthroughs). This is metadata only — the
evaluator still scans rules in priority order — but it makes
"why did this fire?" investigations one step clearer in
`ccm debug trace` output, and forces new rule authors to think
about scope.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

# `ccm_jsonl` and `ccm_signals` are deferred-imported inside the
# functions that use them so module load order does not form a
# `ccm_rules → ccm_jsonl → ccm_core → ccm_commands → ccm_detection
# → ccm_rules.Action` cycle when ccm_rules is the entry point. The
# fast-path functions are only called after the full module graph
# has finished loading, so the deferred import is free in steady
# state and only matters for fresh standalone imports.
from ccm_constants import (
    BUSY_STALE_RELEASE_SEC,
    HOOK_FRESH_THRESHOLD,
    JSONL_HOOK_GAP_TOLERANCE,
    JSONL_USER_PENDING,
    STARTUP_GRACE_SEC,
)


# ─── Phase taxonomy ───
# Each rule is annotated with the session-lifecycle "phase" in
# which it is designed to fire. A drift-guard test asserts every
# rule's phase is in PHASES or explicitly None.
PHASES = (
    "shell",          # no Claude process (DOWN / SHELL)
    "startup",        # claude pid young, MCP loading, no hooks yet
    "midturn",        # user prompt in flight; Claude generating / tool-calling
    "between_tools",  # Stop fired mid-response, next tool pending
    "idle",           # prompt visible, waiting for user input
    "permit",         # permission dialog open
)


@dataclass(frozen=True)
class DetectionContext:
    """Immutable snapshot of all inputs to state detection.

    All fields are derived before any rule is evaluated, so rule
    matching is a pure function of this context.
    """
    raw: str              # detect_window_raw result: DOWN/SHELL/BUSY/IDLE
    hook_state: str       # hook signal state: BUSY/PERMIT/SHELL/""
    hook_ts: int          # hook signal timestamp (0 if no signal)
    hook_age: int         # now - hook_ts (-1 if no signal)
    prev_state: str       # previous detected state
    jsonl_age: int        # now - newest JSONL real-activity ts (-1 if missing)
    now: int              # current unix timestamp
    # Seconds since the `claude` process in this window started, or
    # -1 if no claude pid was found (SHELL state) or the ps snapshot
    # had no etime column. Used by `startup_transient_raw_busy` to
    # distinguish MCP-loading startup from steady-state operation.
    claude_pid_age: int = -1
    # stop_reason of the most recent `assistant` record in the JSONL
    # tail. "tool_use" is the authoritative signal that Claude has
    # paused mid-response to await a tool result; "end_turn" /
    # "max_tokens" / "stop_sequence" mean the response truly ended.
    # None when no assistant record was in the tail window.
    jsonl_last_stop_reason: Optional[str] = None
    # Whether `@ccm_bg_active` is currently set on the window.
    # `apply_actions` consults this to skip the per-window
    # `set-option -wut @ccm_bg_active` subprocess when the option
    # is already unset (the steady-state case for nearly every window).
    prev_bg_active: bool = False
    # Claude Code session_id (UUID) for the live claude in this
    # window's pane, derived from the runtime session_info file
    # `~/.claude/sessions/<pid>.json`. None when no live claude is
    # running, or when session_info has not been written yet.
    session_id: Optional[str] = None
    # The work clock this window's panes show now (first
    # clock-showing pane's, e.g. "(7s · ↓ 380 tokens)" or
    # "Retrying in 3s"), or None when no pane shows one. Detection
    # only observes it; `apply_actions` persists it so the next
    # detection process can judge whether it ticks.
    work_clock: Optional[str] = None
    # The window's persisted `(clock_string, unix_ts)` from the
    # previous detection pass, or None when nothing is stored.
    # Compared against `work_clock` by the tick check.
    prev_work_clock: Optional[Tuple[str, int]] = None


class Action(Enum):
    """Side effect to execute when a rule matches.

    DEFAULT         — set @ccm_prev_state to resolved state
    HOLD_NO_WRITE   — do not touch tmux state (preserve prior state)
    """
    DEFAULT = "default"
    HOLD_NO_WRITE = "hold_no_write"


class _UseRawSentinel:
    """Sentinel: result=USE_RAW means "use ctx.raw as the resolved
    state". A unique object ensures it cannot collide with a real
    state name."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "<USE_RAW>"


USE_RAW = _UseRawSentinel()


@dataclass(frozen=True)
class Rule:
    """Declarative detection rule.

    Condition fields: None = wildcard (not checked).
    """
    name: str
    # str for concrete state (e.g. "BUSY"), or the USE_RAW sentinel
    result: object = USE_RAW
    action: Action = Action.DEFAULT
    raw_in: Optional[Tuple[str, ...]] = None
    hook_in: Optional[Tuple[str, ...]] = None
    prev_in: Optional[Tuple[str, ...]] = None
    hook_age_lt: Optional[int] = None
    # Match only while the `claude` process started less than this
    # many seconds ago. Requires `0 <= ctx.claude_pid_age < value` —
    # an unknown age (-1) does NOT match.
    claude_pid_age_lt: Optional[int] = None
    jsonl_age_lt: Optional[int] = None
    # True:  ctx.hook_state must be "" (no hook signal present)
    # False: ctx.hook_state must be non-empty
    # None:  not checked
    hook_missing: Optional[bool] = None
    # True: ctx.jsonl_age must be < 0 (no JSONL file at all)
    # False: ctx.jsonl_age must be >= 0
    # None: not checked
    jsonl_missing: Optional[bool] = None
    # Authoritative BUSY-hold discriminator: stop_reason of the most
    # recent assistant record in the JSONL tail must be in this tuple.
    # `"tool_use"` means Claude paused mid-response to await a tool
    # result; other values indicate the response truly ended.
    jsonl_last_stop_reason_in: Optional[Tuple[str, ...]] = None
    # Recap-phantom discriminator. When set, the rule matches only if
    # the BUSY hook signal was fired within `value` seconds AFTER the
    # last real conversation activity
    # (`ctx.jsonl_age - ctx.hook_age < value`). Both ages must be
    # valid (>= 0). Rejects phantom hooks fired by Claude Code's
    # recap housekeeping records.
    hook_after_real_activity_lt: Optional[int] = None
    # Session-lifecycle phase this rule belongs to. Must be one of
    # `PHASES` above, or None for genuine catch-all passthroughs.
    # Metadata only — not consulted by `matches()`.
    phase: Optional[str] = None

    def matches(self, ctx: "DetectionContext") -> bool:
        if self.raw_in is not None and ctx.raw not in self.raw_in:
            return False
        if self.hook_in is not None:
            if ctx.hook_state == "" or ctx.hook_state not in self.hook_in:
                return False
        if self.prev_in is not None and ctx.prev_state not in self.prev_in:
            return False
        if self.hook_age_lt is not None:
            if ctx.hook_age < 0 or ctx.hook_age >= self.hook_age_lt:
                return False
        if self.claude_pid_age_lt is not None:
            if ctx.claude_pid_age < 0 or ctx.claude_pid_age >= self.claude_pid_age_lt:
                return False
        if self.hook_missing is True:
            if ctx.hook_state != "":
                return False
        elif self.hook_missing is False:
            if ctx.hook_state == "":
                return False
        if self.jsonl_age_lt is not None:
            if ctx.jsonl_age < 0 or ctx.jsonl_age >= self.jsonl_age_lt:
                return False
        if self.jsonl_missing is True:
            if ctx.jsonl_age >= 0:
                return False
        elif self.jsonl_missing is False:
            if ctx.jsonl_age < 0:
                return False
        if self.jsonl_last_stop_reason_in is not None:
            if ctx.jsonl_last_stop_reason is None:
                return False
            if ctx.jsonl_last_stop_reason not in self.jsonl_last_stop_reason_in:
                return False
        if self.hook_after_real_activity_lt is not None:
            if ctx.hook_age < 0 or ctx.jsonl_age < 0:
                return False
            if (ctx.jsonl_age - ctx.hook_age) >= self.hook_after_real_activity_lt:
                return False
        return True


# Priority-ordered rule table. First match wins.
#
# Priority rationale:
#   1-2  process tree authoritative for SHELL/DOWN
#   3    fresh BUSY hook beats stale pipeline (rare race
#        where event-log is empty but hook just fired)
#   4    startup transient: demote raw=BUSY during MCP loading
#        (pid-age based, monotonic discriminator)
#   5-6  raw BUSY/PERMIT passthrough (process-tree fallback for
#        hook-less detection)
#   7    default: trust raw state
DETECTION_RULES: Tuple[Rule, ...] = (
    Rule(name="process_down", raw_in=("DOWN",), result="DOWN", phase="shell"),
    Rule(name="process_shell", raw_in=("SHELL",), result="SHELL", phase="shell"),
    Rule(
        # Fast path: very fresh BUSY hook is trusted over any raw
        # state. The recap discriminator
        # (`hook_after_real_activity_lt`) rejects phantom hooks from
        # upstream's housekeeping records (e.g. `away_summary`).
        name="hook_fresh_busy",
        hook_in=("BUSY",),
        hook_age_lt=HOOK_FRESH_THRESHOLD,
        hook_after_real_activity_lt=JSONL_HOOK_GAP_TOLERANCE,
        result="BUSY",
        phase="midturn",
    ),
    Rule(
        # Startup transient: `detect_pane_state` reports raw=BUSY
        # when `claude` has children (MCP servers, LSP) but the `❯`
        # prompt has not yet been rendered. During Claude's 5–30 s
        # startup this signature is indistinguishable from a
        # streaming response. Authoritative discriminator: the
        # `claude` pid's own age from the kernel — if it started
        # less than `STARTUP_GRACE_SEC` ago and no hook signal has
        # been written yet, we are still in MCP-loading startup.
        # HOLD_NO_WRITE preserves prev_state.
        name="startup_transient_raw_busy",
        raw_in=("BUSY",),
        hook_missing=True,
        claude_pid_age_lt=STARTUP_GRACE_SEC,
        result="IDLE",
        action=Action.HOLD_NO_WRITE,
        phase="startup",
    ),
    Rule(
        # raw=BUSY passthrough. Reached when no specific BUSY-
        # promoting / IDLE-demoting rule above matched — typically
        # the no-hooks process-tree fallback where claude has
        # children but the `❯` prompt is not visible.
        name="raw_busy_passthrough",
        raw_in=("BUSY",),
        result="BUSY",
        phase="midturn",
    ),
    Rule(
        # JSONL says the user just submitted a new prompt after a
        # terminal assistant turn (`user_pending` synthesized
        # stop_reason). Claude is processing it — typically in the
        # extended-thinking phase before any new assistant record
        # lands. Without this rule, accept-edits mode (where `❯` is
        # visible at column 0 → raw=IDLE) would falsely classify the
        # window as IDLE for the entire thinking phase. Hooks that
        # would normally cover this (PreToolUse, etc.) may not have
        # fired yet — claude is still thinking, not yet calling tools.
        # Bounded by the same window the event-log path gives itself
        # before it stops claiming a turn is running. It used to be
        # ten times longer, so a turn the event log had already given
        # up on was re-asserted here — an Esc that landed before the
        # answer began writes no terminal record for either path to
        # see, and the pane sat BUSY for ten minutes with nothing
        # running. Sharing the shorter window is safe now that a
        # childless working turn reads raw=BUSY on its own: thinking,
        # generation, and retry backoffs all show a ticking work
        # clock, and a ticking clock never reaches this rule.
        name="jsonl_user_prompt_pending",
        raw_in=("IDLE",),
        jsonl_last_stop_reason_in=(JSONL_USER_PENDING,),
        jsonl_age_lt=BUSY_STALE_RELEASE_SEC,
        result="BUSY",
        phase="midturn",
    ),
    Rule(
        # raw=PERMIT passthrough. capture-pane footer detected a
        # permission modal but no PermissionRequest hook fired (or
        # fired but was already cleared by the time we read).
        name="raw_permit_passthrough",
        raw_in=("PERMIT",),
        result="PERMIT",
        phase="permit",
    ),
    Rule(
        # Default: trust raw state. Always matches (terminal rule).
        # No phase — fires in any unmatched case (typically raw=IDLE
        # from prev=IDLE/SHELL/"" with no promoting evidence).
        name="default",
        result=USE_RAW,
    ),
)


def evaluate_rules(ctx: DetectionContext,
                   rules: Tuple[Rule, ...] = DETECTION_RULES) -> Tuple[Rule, str]:
    """Pure: return (matched_rule, resolved_state) for the first
    matching rule. No I/O, no tmux calls — this function is the
    testable core of detection."""
    for rule in rules:
        if rule.matches(ctx):
            state = ctx.raw if rule.result is USE_RAW else rule.result
            return rule, state
    raise RuntimeError("DETECTION_RULES has no terminal default rule")


# ─── Fast-path context builder ───
# prev_state → synthetic raw mapping for the fast path. The fast
# path (statusline) skips the ps/capture-pane pipeline, so it has
# no real `raw` value. It derives one from prev_state under the
# assumption that Claude is still in the same lifecycle phase as
# the last authoritative slow-path evaluation.
_FAST_PREV_TO_RAW = {
    "DOWN": "DOWN",
    "SHELL": "SHELL",
    "BUSY": "BUSY",
    "PERMIT": "PERMIT",
    "IDLE": "IDLE",
    "": "IDLE",
}


def build_fast_context(prev_state, project_dir,
                       now=None,
                       session_id: Optional[str] = None) -> DetectionContext:
    """Build a DetectionContext for the read-only statusline path.

    Does not call ps/capture-pane/tmux queries for process tree info.
    Derives `raw` from prev_state and reads the hook signal only.

    `session_id` may be passed in (cached value from the same tmux
    query that built the project list) to avoid an O(N) tmux
    subprocess per fast-path refresh.
    """
    import ccm_jsonl   # deferred — see top-of-file note
    import ccm_signals
    if now is None:
        now = int(time.time())

    raw = _FAST_PREV_TO_RAW.get(prev_state, "IDLE")

    hook_state = ""
    hook_ts = 0
    hook_age = -1
    if project_dir:
        sig = ccm_signals.read_hook_signal(project_dir, session_id=session_id)
        if sig is not None:
            hook_ts, hook_state, _detail = sig
            if hook_state == "SHELL":
                hook_state = ""
                hook_ts = 0
            else:
                hook_age = now - hook_ts

    if project_dir:
        jsonl_age, jsonl_last_stop_reason = ccm_jsonl.read_jsonl_tail_info(project_dir)
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
        now=now,
    )


def evaluate_fast(prev_state, project_dir, now=None,
                  session_id: Optional[str] = None) -> str:
    """Read-only state evaluation for statusline-speed contexts.

    Runs the same DETECTION_RULES as the slow path so there is one
    source of truth for state transitions. Does not write to tmux —
    the slow-path run next cycle is authoritative for persisting state.
    """
    ctx = build_fast_context(prev_state, project_dir, now,
                             session_id=session_id)
    _rule, state = evaluate_rules(ctx)
    return state
