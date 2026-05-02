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
