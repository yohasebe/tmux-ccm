"""Runtime health canaries.

Each function returns a one-line warning string when its check
trips, or "" when everything is fine. `ccm status`, `ccm doctor`,
and the dashboard footer aggregate these so users learn about
silent upstream Claude Code regressions before they manifest as
"why is my project stuck?".

Three independent canary classes:

  - **hooks.log size**: anthropics/claude-code#16047 — a bloated
    `~/.claude/hooks.log` silently disables every hook write.
  - **Settings flags**: `disableAllHooks: true` and
    `allowManagedHooksOnly: true` in `~/.claude/settings.json`
    silently disable user-scope hooks (every ccm hook is user-scope).
  - **Cluster-SHELL transitions**: rapid SHELL → BUSY → SHELL loops
    likely mean the macOS silent-exit regression
    (anthropics/claude-code#48069) is recycling Claude.

Cross-module discipline: this module imports `ccm_core` for
late-bound access to `tmux_cmd` (so test mocks via
`monkeypatch.setattr(ccm_core, "tmux_cmd", ...)` reach the
callsites here). Public callers import directly from
`ccm_canaries` — `ccm_core` is purely the helper provider.
"""

import json
import os
import time

import ccm_core  # late-bound for tmux_cmd


# ─── Cluster-SHELL configuration ───
# How wide a window we look at and how many transitions inside it
# count as "clustering". Defaults: 3 transitions in 10 minutes —
# permissive enough not to fire on a single user-driven `ccm stop`,
# tight enough to catch a runaway silent-exit loop within minutes.
SHELL_CLUSTER_WINDOW = int(os.environ.get("CCM_SHELL_CLUSTER_WINDOW", "600"))
SHELL_CLUSTER_COUNT = int(os.environ.get("CCM_SHELL_CLUSTER_COUNT", "3"))
# Issue reference surfaced in the cluster warning. Kept as constants
# so doc rendering can link to them and tests can assert against them
# without hard-coding the upstream URL in two places.
SHELL_CLUSTER_ISSUE = "anthropics/claude-code#48069"
SHELL_CLUSTER_ISSUE_NOTE = "macOS silent-exit"


# ─── hooks.log canary ───
# An unrotated `~/.claude/hooks.log` can grow to many GB and silently
# disable all hook firing (every hook write fails). Claude Code does
# not rotate or cap this file. We warn so the user can
# `: > ~/.claude/hooks.log` and recover hook delivery.

CLAUDE_HOOKS_LOG = os.path.expanduser("~/.claude/hooks.log")
HOOKS_LOG_WARN_BYTES = int(
    os.environ.get("CCM_HOOKS_LOG_WARN_BYTES", str(100 * 1024 * 1024))  # 100 MB
)


def hooks_log_size() -> int:
    """Return the byte size of `~/.claude/hooks.log`, or -1 if absent."""
    try:
        return os.path.getsize(CLAUDE_HOOKS_LOG)
    except OSError:
        return -1


def hooks_log_warning() -> str:
    """Return a one-line warning string when hooks.log is bloated past
    the threshold, or "" if everything is fine. The message tells the
    user the exact remediation command — this is a self-service fix.
    """
    size = hooks_log_size()
    if size < HOOKS_LOG_WARN_BYTES:
        return ""
    mb = size / (1024 * 1024)
    return (
        f"Claude hooks.log is {mb:.0f} MB — hooks may be silently failing. "
        f"Run `: > ~/.claude/hooks.log` to restore hook delivery (#16047)."
    )


# ─── Silent-exception burst canary ───
# `log_caught_exception` records every silent catch in errors.log
# and is wrapped around hot paths (inject_status, dashboard refresh,
# build_project_list, autosave). A bug that fires every poll cycle
# (e.g. the autosave NameError that ran 38 hours undetected before
# we caught it) writes ~30 entries per minute. Without a canary,
# operators only notice when they happen to run `ccm errors`.
#
# The canary scans errors.log itself (no separate counter state to
# get out of sync) and fires when the burst rate suggests a
# poll-cycle failure rather than a one-off blip. Threshold and
# window are env-overridable for tuning if real-world usage shows
# the defaults are too tight or too loose.

ERRORS_BURST_COUNT = int(os.environ.get("CCM_ERRORS_BURST_THRESHOLD", "20"))
ERRORS_BURST_WINDOW = int(os.environ.get("CCM_ERRORS_BURST_WINDOW", "300"))


def errors_log_burst_warning() -> str:
    """Return a one-line warning string when errors.log has at
    least `ERRORS_BURST_COUNT` entries within the last
    `ERRORS_BURST_WINDOW` seconds, or "" otherwise.

    The poll-cycle failure mode (a hot path raising on every refresh)
    accumulates ~30 records/min; a burst of 20 in 5 min is well
    above one-off noise but well below the runaway pattern, so
    crossing this threshold reliably indicates "something is
    looping". Active log only — rotated `errors.log.1` is ignored
    because by the time rotation has happened, the burst is over."""
    log_path = ccm_core.CCM_ERRORS_LOG
    try:
        if not os.path.exists(log_path):
            return ""
    except OSError:
        return ""
    cutoff = time.time() - ERRORS_BURST_WINDOW
    count = 0
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = rec.get("ts")
                if isinstance(ts, (int, float)) and ts >= cutoff:
                    count += 1
    except OSError:
        return ""
    if count < ERRORS_BURST_COUNT:
        return ""
    mins = max(1, ERRORS_BURST_WINDOW // 60)
    return (
        f"{count} silent-fail records in last {mins} min — "
        f"a hot path is looping. Inspect with `ccm errors`."
    )


# ─── Settings flags canary ───
# Detect configuration in ~/.claude/settings.json that silently
# disables ccm's fast-path signals. Without these checks, state
# detection degrades with no obvious error and the user sees the
# symptom (sluggish state changes) without the cause.

CLAUDE_SETTINGS_FILE = os.path.expanduser("~/.claude/settings.json")


def _read_claude_settings():
    try:
        with open(CLAUDE_SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def disable_all_hooks_warning() -> str:
    """Return a warning string if `disableAllHooks: true` is set in
    ~/.claude/settings.json, otherwise "".

    Per Claude Code's docs, this setting disables ALL hooks AND any
    custom statusLine — ccm's entire fast-path signal goes dark with
    no error. Same class of silent failure as the hooks.log bloat
    canary.

    Scope: only the user-level file `~/.claude/settings.json` is
    checked. Project-scope settings (`<project>/.claude/settings.json`)
    and enterprise managed settings (e.g.
    `/Library/Application Support/ClaudeCode/managed-settings.json`
    on macOS) are NOT inspected. The setting is also valid in those
    locations, so a managed-policy or per-project disable will not
    surface a warning here. Adding cross-scope checks would require
    walking Claude Code's full settings precedence chain, which is
    out of scope for this canary.
    """
    data = _read_claude_settings()
    if not data:
        return ""
    if data.get("disableAllHooks") is True:
        return (
            "Claude Code `disableAllHooks: true` is set in "
            "~/.claude/settings.json — this disables ALL hooks AND any "
            "custom `statusLine` command. ccm state detection falls "
            "back to JSONL polling and process tree only, and any "
            "embedded statusLine you configured will stop rendering. "
            "Remove the setting to restore real-time hook signals."
        )
    return ""


def managed_hooks_only_warning() -> str:
    """Return a warning string if `allowManagedHooksOnly: true` is set
    in ~/.claude/settings.json, otherwise "".

    Per Claude Code's docs, when this is set in *managed* settings,
    every user-scope hook (which is exactly where ccm installs all
    14 of its hooks) is silently blocked with no error.
    The result looks identical to a broken Claude Code install from
    ccm's perspective: no hooks fire, ever.

    Scope (important caveat): only the user-level file
    `~/.claude/settings.json` is checked. The setting is most
    commonly placed in an enterprise-managed settings file (e.g.
    `/Library/Application Support/ClaudeCode/managed-settings.json`
    on macOS), which is the actual deployment scenario this flag
    targets. ccm does NOT walk Claude Code's settings precedence
    chain — that path varies by OS and is not stably documented.

    This canary therefore catches:
      - a user who set the flag in their own file by mistake or test
      - a managed file symlinked to the user-scope path
    But it does NOT catch the typical enterprise deployment where
    the flag lives in a separate managed file. Users in managed
    enterprise environments should not expect a warning here even
    when ccm hooks are silently disabled.
    """
    data = _read_claude_settings()
    if not data:
        return ""
    if data.get("allowManagedHooksOnly") is True:
        return (
            "Claude Code `allowManagedHooksOnly: true` is set — all "
            "user-scope hooks (including every ccm hook) are blocked. "
            "Remove the setting or move ccm hooks to managed scope to "
            "restore real-time signals."
        )
    return ""


# ─── Cluster-SHELL-transition detection ───
# When Claude Code dies repeatedly (most commonly the macOS silent-
# exit regression, anthropics/claude-code#48069), ccm observes a
# rapid SHELL → (user or ccm re-attach) → BUSY → IDLE → SHELL loop.
# Each SHELL transition is recorded in a per-window tmux option
# `@ccm_shell_history`; a warning fires if the count in the last
# `SHELL_CLUSTER_WINDOW` exceeds `SHELL_CLUSTER_COUNT`.

_SHELL_HISTORY_OPT = "@ccm_shell_history"


def _parse_shell_history_raw(raw: str) -> list:
    """Parse a raw `@ccm_shell_history` value (comma-separated unix
    timestamps) into a list of ints, newest first, with entries
    older than `SHELL_CLUSTER_WINDOW` filtered out. Shared by the
    per-window read and the batch read.
    """
    if not raw:
        return []
    horizon = int(time.time()) - SHELL_CLUSTER_WINDOW
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ts = int(item)
        except ValueError:
            continue
        if ts >= horizon:
            out.append(ts)
    return out


def _read_shell_history(win_target: str) -> list:
    """Read the SHELL transition timestamp history for a single window.

    Used by the rare write path (`_push_shell_transition`). The hot
    read path (`shell_cluster_warnings`) goes through
    `_read_all_shell_histories` to amortise the subprocess cost
    across windows.
    """
    raw = ccm_core.tmux_cmd(
        "show-option", "-wqv", "-t", win_target, _SHELL_HISTORY_OPT
    )
    return _parse_shell_history_raw(raw)


def _read_all_shell_histories() -> dict:
    """Return `{win_target: history_list}` for every window in one
    `tmux list-windows -a` subprocess instead of one show-option
    per window. Keeps `shell_cluster_warnings` O(1) subprocess
    regardless of project count.
    """
    raw = ccm_core.tmux_cmd(
        "list-windows", "-a",
        "-F", "#{session_name}:#{window_index}\t#{" + _SHELL_HISTORY_OPT + "}",
    )
    histories = {}
    if not raw:
        return histories
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        win_target, _, history_raw = line.partition("\t")
        histories[win_target] = _parse_shell_history_raw(history_raw)
    return histories


def _push_shell_transition(win_target: str) -> None:
    """Record a new SHELL transition for a window.

    Prepends the current timestamp to `@ccm_shell_history`, trims
    entries older than `SHELL_CLUSTER_WINDOW` via
    `_read_shell_history`, and caps the stored size at
    2 × `SHELL_CLUSTER_COUNT` entries (or a floor of 6) to prevent
    unbounded growth of the tmux option. Every push writes back a
    capped, trimmed history so even pre-existing oversized values
    converge on the cap.
    """
    existing = _read_shell_history(win_target)
    now = int(time.time())
    max_entries = max(SHELL_CLUSTER_COUNT * 2, 6)

    # Deduplicate same-second pushes so two code paths hitting
    # apply_actions for the same scan cycle don't double-count one
    # transition. The capped history is still written back so
    # pre-existing oversized values are normalised.
    if existing and existing[0] == now:
        updated = existing
    else:
        updated = [now] + existing

    updated = updated[:max_entries]
    ccm_core.tmux_cmd(
        "set-option", "-wt", win_target, _SHELL_HISTORY_OPT,
        ",".join(str(t) for t in updated),
    )


def _format_cluster_warning(history_len: int, project_name: str) -> str:
    label = f"{project_name}: " if project_name else ""
    return (
        f"{label}Claude Code exited {history_len}+ times in "
        f"{SHELL_CLUSTER_WINDOW // 60} min — likely "
        f"{SHELL_CLUSTER_ISSUE} ({SHELL_CLUSTER_ISSUE_NOTE}). "
        f"The conversation auto-restores via `claude --continue`."
    )


def shell_cluster_warning(win_target: str, project_name: str = "") -> str:
    """Single-window read path. One subprocess per call. Most callers
    should prefer `shell_cluster_warnings` which batches across all
    project windows in one subprocess.
    """
    history = _read_shell_history(win_target)
    if len(history) < SHELL_CLUSTER_COUNT:
        return ""
    return _format_cluster_warning(len(history), project_name)


def shell_cluster_warnings(projects) -> list:
    """Return a list of one-line warning strings for every project
    whose SHELL transition history meets the cluster threshold.
    Reads all per-window histories in a single batched subprocess.
    """
    histories = _read_all_shell_histories()
    out = []
    for p in projects:
        history = histories.get(p.win_target, [])
        if len(history) < SHELL_CLUSTER_COUNT:
            continue
        out.append(_format_cluster_warning(len(history), p.name))
    return out


# ─── Hook-silence canary (opt-in, observe-first) ───
#
# Detects the upstream failure mode where a session's hook event log
# stops updating while the conversation is demonstrably still active —
# the #16047-class regression where hooks silently stop firing
# mid-session (the 2026-07-04 jwriter incident: hooks silent through a
# whole real turn). When that happens ccm's precise event-log path goes
# blind and detection falls back to the coarser raw+JSONL heuristics,
# where false BUSY/IDLE become possible. Surfacing this tells the
# operator "detection is degraded for project X, and it's upstream, not
# ccm".
#
# Signature: fresh JSONL real-activity (the session is working right
# now) whose timestamp is well AHEAD of the newest hook event (the hook
# log never recorded that activity). Requiring the event log to EXIST
# and to lag by a wide margin keeps this clear of the benign look-alikes:
#   - long tool run  → JSONL freezes at tool-start too (tool_result is
#                       written on completion), so jsonl_age exceeds the
#                       freshness gate and this abstains
#   - slash command  → already filtered out of JSONL activity upstream
#                       in _parse_jsonl_tail, so it never looks fresh
#   - startup        → no event log yet (no hook has fired), so the
#                       "event log exists" requirement excludes it
#   - idle session   → no fresh JSONL activity, freshness gate excludes
#
# OBSERVE-FIRST: default OFF. During calibration the operator opts in
# with `@ccm-hook-silence on` and watches the dashboard footer; a
# mis-tuned threshold can then only mislead someone who explicitly
# asked to see it, never a default user. Promote to default-on in a
# later release once real-session dogfooding confirms zero false fires.

HOOK_SILENCE_FRESH = int(os.environ.get("CCM_HOOK_SILENCE_FRESH", "90"))
HOOK_SILENCE_GAP = int(os.environ.get("CCM_HOOK_SILENCE_GAP", "120"))
_HOOK_SILENCE_OPT = "@ccm-hook-silence"

# ─── Firing log (evidence for the default-on promotion) ───
# Warnings alone leave no trace: the promotion criteria ("zero false
# fires across N days of dogfood", "caught at least one real
# silence") cannot be evaluated from memory. Every firing appends one
# JSON line so the calibration record survives dashboard restarts and
# reboots. The log lives under the persistent data dir (NOT
# $TMPDIR — macOS purges that within days, which would erase exactly
# the multi-week evidence this exists to collect) and is deliberately
# separate from errors.log so firings can't trip the
# `errors_log_burst_warning` canary or drown real silent-catch
# records.
#
# Rate limit: the warning surfaces re-poll every ~2 s while an
# episode is live; a per-project marker file caps the log at one
# record per `HOOK_SILENCE_LOG_INTERVAL` so a 30-minute episode
# reads as a handful of lines (episode duration stays inferable),
# not a thousand.
HOOK_SILENCE_LOG_INTERVAL = int(
    os.environ.get("CCM_HOOK_SILENCE_LOG_INTERVAL", "600")
)
HOOK_SILENCE_LOG_MAX_BYTES = int(
    os.environ.get("CCM_HOOK_SILENCE_LOG_MAX_BYTES", str(1 * 1024 * 1024))
)


def hook_silence_log_path() -> str:
    """Resolve the firing-log path at call time so tests (and users)
    can redirect it via CCM_HOOK_SILENCE_LOG without reload tricks."""
    return os.environ.get(
        "CCM_HOOK_SILENCE_LOG",
        os.path.join(ccm_core.CCM_DATA_DIR, "state", "hook-silence.log"),
    )


def _log_hook_silence_firing(project_name, project_dir, state,
                             jsonl_age, lag_sec, now) -> None:
    """Append one JSON evidence record for a canary firing,
    rate-limited per project.

    Best-effort throughout: evidence logging must never break the
    warning surface it observes, so every failure path is swallowed.
    Rotation mirrors `log_caught_exception` (rename to `.1` at the
    size cap) though at one record per interval the cap is ~decades
    away — it exists so a pathological loop still cannot eat disk.
    """
    try:
        log_path = hook_silence_log_path()
        # Rate-limit markers live NEXT TO the log (`<log>.markers/`),
        # not under $TMPDIR: one path knob isolates both in tests,
        # and marker mtimes survive reboots so an episode spanning a
        # dashboard restart is not double-logged.
        marker_dir = log_path + ".markers"
        os.makedirs(marker_dir, exist_ok=True)
        marker = os.path.join(
            marker_dir, ccm_core.md5_hash(project_dir or project_name))
        try:
            if now - os.path.getmtime(marker) < HOOK_SILENCE_LOG_INTERVAL:
                return
        except OSError:
            pass  # no marker yet → first firing for this project

        try:
            if os.path.getsize(log_path) >= HOOK_SILENCE_LOG_MAX_BYTES:
                os.replace(log_path, log_path + ".1")
        except OSError:
            pass
        record = {
            "ts": int(now),
            "project": project_name,
            "state": state,
            "jsonl_age": int(jsonl_age),
            "gap": int(lag_sec),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(int(now)))
    except Exception:
        pass


def hook_silence_log_count() -> int:
    """Number of firing records in the active log (rotated `.1` not
    counted). `ccm doctor` shows this so the promotion review has its
    evidence count one command away. Returns 0 when the log is
    absent or unreadable."""
    try:
        with open(hook_silence_log_path(), encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def hook_silence_enabled() -> bool:
    """Whether the opt-in hook-silence canary is turned on. Reads the
    global `@ccm-hook-silence` tmux option; anything in the truthy set
    enables it, everything else (including absent) leaves it off."""
    val = (ccm_core.tmux_cmd("show-option", "-gqv", _HOOK_SILENCE_OPT)
           or "").strip().lower()
    return val in ("on", "always", "1", "true", "yes")


def hook_silence_suspect(state, jsonl_age, jsonl_ts, event_ts, now,
                         fresh=None, gap=None) -> bool:
    """Pure predicate: does this project's hook event log look silent?

    True iff a live session shows fresh JSONL real-activity whose
    timestamp leads the newest hook event by at least `gap` seconds —
    i.e. real work the hook log never recorded.

    Arguments (all plain values — no I/O, fully testable):
        state:      committed ccm state string. Only BUSY/IDLE/PERMIT
                    qualify; SHELL/DOWN have no live session to track.
        jsonl_age:  seconds since newest JSONL real-activity, or -1/None
                    when the JSONL has no activity record.
        jsonl_ts:   unix ts of that activity (0/None if none).
        event_ts:   unix ts of the newest hook event (0/None when the
                    event log is absent or empty — the exclusion that
                    keeps startup and hook-less sessions quiet).
        now:        current unix time.
        fresh/gap:  thresholds; default to the module constants.
    """
    fresh = HOOK_SILENCE_FRESH if fresh is None else fresh
    gap = HOOK_SILENCE_GAP if gap is None else gap
    if state not in ("BUSY", "IDLE", "PERMIT"):
        return False
    if jsonl_age is None or jsonl_age < 0 or jsonl_age > fresh:
        return False          # no fresh activity for hooks to have missed
    if not jsonl_ts or not event_ts:
        return False          # need both anchors; absent event log excluded
    return (jsonl_ts - event_ts) >= gap


def _read_all_session_ids() -> dict:
    """Return `{win_target: session_id}` for every window in one
    `tmux list-windows -a` subprocess. Empty session_id values are
    dropped so callers can treat "present in map" as "resolved"."""
    raw = ccm_core.tmux_cmd(
        "list-windows", "-a",
        "-F", "#{session_name}:#{window_index}\t#{@ccm_session_id}",
    )
    out = {}
    if not raw:
        return out
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        win_target, _, sid = line.partition("\t")
        sid = sid.strip()
        if sid:
            out[win_target] = sid
    return out


def _format_hook_silence_warning(project_name: str, lag_sec: int) -> str:
    label = f"{project_name}: " if project_name else ""
    lag_min = max(1, lag_sec // 60)
    return (
        f"{label}hooks appear silent — event log frozen ~{lag_min}m behind "
        f"live activity; detection on JSONL fallback (upstream #16047-class)"
    )


def hook_silence_warnings(projects, enabled=None, now=None) -> list:
    """Return one-line warning strings for every live project whose
    hook event log looks silent. Opt-in: returns [] unless
    `@ccm-hook-silence` is on (override with `enabled=` in tests).

    Read-only and off the detection hot path — called only from the
    status / doctor / dashboard-footer surfaces. The per-project JSONL
    and event-log reads hit the same mtime+size caches the detection
    cycle just populated, so the extra cost is a cache lookup, not a
    re-read.
    """
    if enabled is None:
        enabled = hook_silence_enabled()
    if not enabled:
        return []
    # Absent hooks → absent event logs are expected, not a regression.
    if not ccm_core.hooks_configured():
        return []

    # Function-local imports: ccm_jsonl / ccm_signals both import
    # ccm_core, which top-level-imports this module — importing them at
    # module scope would close that cycle. Deferring to call time keeps
    # the import graph acyclic (and costs nothing after first load).
    import ccm_jsonl
    import ccm_signals

    if now is None:
        now = int(time.time())
    sid_map = _read_all_session_ids()
    out = []
    for p in projects:
        sid = sid_map.get(p.win_target)
        if not sid:
            continue
        jsonl_age, _stop = ccm_jsonl.read_jsonl_tail_info(p.dir)
        jsonl_ts = (now - jsonl_age) if (jsonl_age is not None
                                         and jsonl_age >= 0) else 0
        events = ccm_signals.read_events_tail(p.dir, session_id=sid)
        event_ts = 0
        if events:
            last = events[-1]
            if isinstance(last, dict):
                event_ts = last.get("ts", 0) or 0
        if hook_silence_suspect(p.state, jsonl_age, jsonl_ts, event_ts, now):
            lag = jsonl_ts - event_ts
            out.append(_format_hook_silence_warning(p.name, lag))
            _log_hook_silence_firing(
                p.name, p.dir, p.state, jsonl_age, lag, now)
    return out
