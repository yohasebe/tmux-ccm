"""Terminal-output formatters for ccm CLI commands.

`print_status` / `print_ports` / `print_tree` / `print_statusline`
plus the small helpers (`signal_age_suffix`, `format_elapsed`,
`format_dir`) and ANSI colour constants that the dashboard and
status bar also consume.

I/O lives in `ccm_core`; this module is one-way downstream of it
(import direction: ccm_render → ccm_core).
"""
from __future__ import annotations

import os
import time
import unicodedata

import ccm_core
import ccm_canaries
import ccm_signals
import ccm_spool


# ─── Display width (terminal column count) ───
# `len(s)` returns codepoint count, which mismatches the actual
# column width of a string in a monospace terminal:
#   - CJK ideographs and most emoji ("W"/"F" East Asian Width) take 2.
#   - Combining marks ("Mn"/"Me") and zero-width formatters ("Cf",
#     e.g. ZWJ/ZWNJ) take 0.
#   - Ambiguous ("A") characters render as 1 in non-CJK terminals and
#     as 2 in CJK terminals; the historical Unicode TR11 ambiguity
#     means we cannot pick a default that is correct for everyone.
# Column alignment (dashboard list, status bar, directory truncation)
# needs the terminal width. Edge cases like grapheme clusters and
# regional-indicator flag sequences are not perfectly Unicode-spec
# correct here, but match what every common monospace terminal
# actually renders. The alternative — pulling in the `wcwidth`
# package — adds a runtime dependency for a tmux plugin, which
# we deliberately avoid.

# `CCM_AMBIGUOUS_WIDTH` lets users on CJK locale terminals (where
# Ambiguous chars render as 2 columns) opt into the wider
# treatment. Default 1 matches non-CJK terminals (the majority).
# Scope note: this only affects EAW='A' chars. Some symbols that
# also widen on CJK terminals (e.g. ⚠/◉ which are EAW='N') are
# outside this knob — closing that gap would require the external
# `wcwidth` package, which the plugin deliberately avoids. Read at
# module load; restart inject-status / dashboard to pick up a
# changed value.
#
# Setting it is also a statement that the user knows what their
# terminal does, which is worth more than the value alone: layouts
# that would otherwise reserve room for the case they cannot rule
# out can stop reserving it. So "1" and "unset" mean different
# things here even though they count the same — hence the flag.
#
# `@ccm-ambiguous-width` is the primary source and the environment
# variable the fallback, because ccm is started by parents with
# different environments and they must agree. The status bar is
# rendered both from tmux's `#()` and from the hooks, which Claude
# Code spawns; `tmux set-environment` reaches only the first, and
# the hook-driven path is the one that is never rate-limited. A
# value that reaches one but not the other makes the bar alternate
# between two layouts. A tmux option answers the same to whoever
# asks. The variable stays for running ccm outside tmux, where
# there is no option to read.
def _declared_width(raw):
    """The width `raw` states, or None when it states nothing."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v in (1, 2) else None


def _resolve_ambiguous_width() -> tuple[int, bool]:
    declared = None
    try:
        declared = _declared_width(
            ccm_core.tmux_cmd("show-option", "-gqv", "@ccm-ambiguous-width"))
    except Exception:
        declared = None
    if declared is None:
        declared = _declared_width(os.environ.get("CCM_AMBIGUOUS_WIDTH"))
    if declared is None:
        return 1, False
    return declared, True


# Resolved once per process, on first use rather than at import: the
# lookup costs a tmux round trip, and the rate-limited status tick
# exits before importing anything at all.
_AMBIGUOUS_STATE = None


def _ambiguous_state() -> tuple[int, bool]:
    global _AMBIGUOUS_STATE
    if _AMBIGUOUS_STATE is None:
        _AMBIGUOUS_STATE = _resolve_ambiguous_width()
    return _AMBIGUOUS_STATE


def ambiguous_width_declared() -> bool:
    """True when the user has said what their terminal draws.

    Layouts that would otherwise reserve room for the wider case can
    stop reserving it — see `inject_status._ambiguous_width_allowance`.
    """
    return _ambiguous_state()[1]


def _char_width(c: str) -> int:
    cat = unicodedata.category(c)
    if cat in ("Mn", "Me", "Cf"):
        return 0
    eaw = unicodedata.east_asian_width(c)
    if eaw in ("W", "F"):
        return 2
    if eaw == "A":
        return _ambiguous_state()[0]
    return 1


def display_width(s: str) -> int:
    """Return the terminal column count of `s`."""
    if not s:
        return 0
    return sum(_char_width(c) for c in s)


def truncate_to_width(s: str, max_width: int) -> str:
    """Truncate `s` so its `display_width` does not exceed `max_width`.
    Wide chars are kept whole — never sliced mid-character — so a
    string starting with a CJK char being squeezed into 1 column
    returns the empty string rather than half a glyph."""
    if max_width <= 0:
        return ""
    width = 0
    out = []
    for c in s:
        cw = _char_width(c)
        if cw == 0:
            out.append(c)
            continue
        if width + cw > max_width:
            break
        out.append(c)
        width += cw
    return "".join(out)


def pad_to_width(s: str, width: int) -> str:
    """Right-pad `s` with spaces so its `display_width` equals `width`.
    Use this in CLI table formatters instead of the f-string `<N`
    spec, which pads by codepoint count and silently misaligns
    columns the moment a CJK or emoji character appears in the
    field. Wider-than-`width` strings are returned unchanged."""
    pad = width - display_width(s)
    return s + " " * pad if pad > 0 else s
from ccm_constants import (
    JSONL_HOOK_GAP_TOLERANCE,
    PERMISSION_MODE_WARN,
    STATE_ICONS,
    permission_mode_label,
)


# ─── ANSI colour codes ───
# Used by the print_* helpers below.
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"        # for [N] pane-count digit
C_GREEN_BOLD = "\033[1;32m"  # for "*" completed marker
C_STATE = {
    "PERMIT": "\033[1;33m",      # bold yellow
    "BUSY": "\033[38;5;209m",    # salmon
    "IDLE": "\033[0;34m",        # blue
    "SHELL": "\033[38;5;245m",   # gray
    "DOWN": "\033[2m",           # dim
}


# ─── Stale-signal age suffix ───
# Threshold above which a hook signal counts as "stale" enough to
# surface in the UI. Bound to `JSONL_HOOK_GAP_TOLERANCE` directly
# so the dashboard's "stale" affordance automatically tracks the
# threshold the detection rules use to decide whether to release a
# stuck state. Visually flagging staleness BEFORE the release
# rules can release would be confusing — the user would see the
# hint, do nothing, and the rule would silently un-stick anyway.
SIGNAL_STALE_DISPLAY_THRESHOLD = JSONL_HOOK_GAP_TOLERANCE  # seconds


def signal_age_suffix(project_dir, state, session_id=None):
    """Returns a parenthesised stale-signal age (e.g. " (8m)") when
    the hook signal for this project is old enough to be worth
    surfacing in the UI, or "" otherwise.

    Only returns a non-empty string for state in {BUSY, PERMIT} —
    those are the states where a stale hook signal can mask a real
    state change (IDLE) that the release rules cannot confidently
    make. SHELL / IDLE / DOWN either have no associated hook
    signal or the signal is freshness-irrelevant.

    `session_id` semantics match `ccm_signals.read_hook_signal`:
    callers that already hold the bulk-fetched `@ccm_session_id`
    (Project.cached_session_id) MUST pass it (`or ""`) — otherwise
    every call pays a `tmux list-windows -a` subprocess, which in
    the dashboard's 2-second annotation loop and the inject-status
    poll is the classic N+1 this module's callers are designed to
    avoid. `""` is the authoritative "no session" form and skips
    the tmux fallback entirely.

    Best-effort: never raises; returns "" on any error reading the
    signal file."""
    if state not in ("BUSY", "PERMIT"):
        return ""
    if not project_dir:
        return ""
    try:
        sig = ccm_signals.read_hook_signal(project_dir, session_id=session_id)
    except Exception:
        return ""
    if sig is None:
        return ""
    ts = sig[0]
    age = int(time.time()) - ts
    if age < SIGNAL_STALE_DISPLAY_THRESHOLD:
        return ""
    if age < 60:
        return f" ({age}s)"
    if age < 3600:
        return f" ({age // 60}m)"
    if age < 86400:
        return f" ({age // 3600}h)"
    return f" ({age // 86400}d)"


# Minimum elapsed time before the `* elapsed` completion marker
# becomes visible in dashboard / statusline renderers. Hides the
# marker during the first few seconds after a BUSY→IDLE transition.
#
# Why this exists (added, ccm 0.3.x post-agent-view):
#   Multi-turn auto-loop commands — `/goal` is the canonical case,
#   `/loop` and `/plan`-driven follow-ups have similar shape —
#   execute as `BUSY → end_turn → (1-3 s IDLE gap) → auto-fired
#   UserPromptSubmit → BUSY → …`. State detection during the gap
#   is correct (Claude IS briefly idle waiting for the auto-prompt),
#   but the dashboard would otherwise render `* 1s` / `* 2s` markers
#   for each gap — falsely implying the work just completed when
#   the loop is still mid-flight. Verified empirically on
# with `ccm debug trace` against a 3-turn `/goal`
#   condition: two ~2 s IDLE windows between turns, both surfaced
#   the marker. See memory `project_goal_flicker_2026_05_13`.
#
#   The notification path (`on-stop.sh` grace sentinel) already
#   absorbed these gaps via `CCM_COMPLETION_GRACE_SEC=3`. This
#   constant brings the visual marker into the same window —
#   conceptually one knob, applied at two layers.
#
# When this code can be removed (audit guide):
#   - Upstream Claude Code exposes a "goal/loop active" signal
#     (JSONL field or hook payload) that lets us suppress the
#     marker only during real auto-loops. Then a blanket 3 s rule
#     is too coarse and this should go.
#   - `/goal` and similar auto-multi-turn commands disappear
#     upstream (unlikely). Verify by running the empirical test
#     below before deleting.
#   - User reports the 3 s delay on normal completions is
#     disruptive. In that case the tradeoff was wrong; remove
#     this and restore immediate marker visibility.
#
# How to verify it's still load-bearing:
#   `ccm debug trace <project> 0.3`, dispatch a `/goal` condition
#   that requires 3+ turns (e.g. `/goal create files /tmp/x{1,2,3}.txt
#   one per turn`). If short (<3 s) BUSY→IDLE→BUSY oscillations
#   still appear in the trace, this suppression is still needed.
MIN_ELAPSED_DISPLAY_SEC = 3


def format_elapsed(ts):
    """Format a unix timestamp as a short "time since" string.

    Returns "" for the first `MIN_ELAPSED_DISPLAY_SEC` seconds
    (see the constant's comment above for the auto-loop flicker
    rationale this guard exists to mitigate). After that, returns
    a **fixed 3-character** right-padded string (`" 5s"`, `"10s"`,
    `" 1m"`, …) so the completion marker `* elapsed` has constant
    visual width — required by the dashboard's right-anchored
    elapsed slot, which would otherwise wobble at 1↔2 digit
    boundaries as the counter ticks.
    """
    if not ts or ts == 0:
        return ""
    elapsed = int(time.time()) - ts
    if elapsed < MIN_ELAPSED_DISPLAY_SEC:
        return ""
    if elapsed < 60:
        return f"{elapsed:>2d}s"
    if elapsed < 3600:
        return f"{elapsed // 60:>2d}m"
    if elapsed < 86400:
        return f"{elapsed // 3600:>2d}h"
    return f"{elapsed // 86400:>2d}d"


def format_dir(directory, prefix_len, cols):
    d = ccm_core.shorten_home(directory)
    avail = cols - prefix_len - 4
    if avail < 10:
        return ""
    if display_width(d) <= avail:
        return d
    base = os.path.basename(d)
    parent = os.path.basename(os.path.dirname(d))
    short = f"…/{parent}/{base}"
    if display_width(short) <= avail:
        return short
    if display_width(base) <= avail:
        return base
    return ""


def external_agent_label(project):
    """Compact label for a project's external-agent CLI panes
    ("" when none). Names are the pane foreground commands
    de-duplicated in first-seen order; a name hosted by several
    panes gets a `×N` count suffix. Shared by `ccm status`, the
    dashboard, and the status bar so the badge text is identical
    on every surface."""
    names = getattr(project, "external_agents", ()) or ()
    if not names:
        return ""
    parts = []
    for name in dict.fromkeys(names):
        count = names.count(name)
        parts.append(name if count == 1 else f"{name}×{count}")
    return ",".join(parts)


# ─── CLI commands ───

def print_status():
    """Print status of all ccm projects (for `ccm status` CLI command)."""
    projects = ccm_core.build_project_list(fast=False)
    spool_counts = ccm_spool.pending_counts()

    if not projects:
        print("No active projects.")
        _print_spool_summary(spool_counts)
        return

    if ccm_core.hooks_configured():
        print(f"{C_DIM}Hooks: ON{C_RESET}")
    else:
        print(f"{C_DIM}Hooks: OFF (run 'ccm setup-hooks' for improved detection){C_RESET}")
    for warning in (
        ccm_canaries.hooks_log_warning(),
        ccm_canaries.disable_all_hooks_warning(),
        ccm_canaries.managed_hooks_only_warning(),
    ):
        if warning:
            print(f"\033[33m⚠ {warning}\033[0m")
    for cluster_msg in ccm_canaries.shell_cluster_warnings(projects):
        print(f"\033[33m⚠ {cluster_msg}\033[0m")
    for silence_msg in ccm_canaries.hook_silence_warnings(projects):
        print(f"\033[33m⚠ {silence_msg}\033[0m")
    print()

    # A SHELL row hosting an external agent says so once, with the
    # `⚙name` badge beside the project name. It used to say it twice —
    # a `(name)` note in this column repeated the badge, and widening
    # the column to fit it shifted every other row's layout.
    status_w = 12

    print(f"{C_BOLD}{'STATUS':<{status_w}} {'PROJECT':<20} {'MODE':<8} {'BRANCH':<16} {'PORTS':<12} {'DIRECTORY'}{C_RESET}")
    print(f"{'------':<{status_w}} {'-------':<20} {'----':<8} {'------':<16} {'-----':<12} {'---------'}")

    for p in projects:
        color = C_STATE.get(p.state, C_DIM)
        icon = STATE_ICONS.get(p.state, "?")
        # State-modifier suffixes (stale-age / bg) attach to the
        # STATUS column right after the state name. Mutually
        # exclusive — stale only fires for BUSY/PERMIT, bg only
        # for IDLE.
        suffix = signal_age_suffix(
            p.dir, p.state, session_id=p.cached_session_id or "")
        if p.bg_active:
            suffix += " (bg)"
        # Pad the STATUS column by VISIBLE width (pad_to_width), not by
        # len(): the per-state colour codes differ in length (256-colour
        # `\033[38;5;209m` is 11 chars vs `\033[1;33m` at 7), so the old
        # `22 + len(suffix)` format spec put the PROJECT column at a
        # different offset for BUSY/SHELL vs PERMIT/IDLE/DOWN rows.
        status_text = f"{icon} {p.state}{suffix}"
        if display_width(status_text) > status_w:
            # pad_to_width passes overlong content through unchanged,
            # so a stale-signal row (`⚠ PERMIT (12m)` = 14 cols) would
            # shove every later column rightward. Drop the age suffix
            # instead of truncating it mid-number (`(1` reads as a
            # wrong age); the icon/colour still flags the state, and
            # only PERMIT can overflow (BUSY + suffix fits exactly).
            status_text = f"{icon} {p.state}"
        status_field = (
            f"{color}{pad_to_width(status_text, status_w)}"
            f"{C_RESET}"
        )
        # Pane-count marker `[N]` belongs to the PROJECT column.
        # Brackets dim, digit cyan to draw the eye to the count.
        if p.pane_count > 1:
            n = str(p.pane_count)
            pane_marker = (
                f" {C_DIM}[{C_RESET}{C_CYAN}{n}{C_RESET}"
                f"{C_DIM}]{C_RESET}"
            )
            pane_marker_visible_w = 1 + 2 + len(n)  # " [N]"
        else:
            pane_marker = ""
            pane_marker_visible_w = 0
        # Ignore marker `⊘`: a CCM_IGNORE'd sidekick pane is present
        # but untracked. Dim so it reads as a quiet aside, not a
        # state. `⊘` (U+2298) is one terminal column.
        if getattr(p, "ignored_panes", 0):
            # An ignored Claude sidekick waiting on a permission
            # dialog turns its ⊘ PERMIT-yellow — the counterpart of
            # the ⚙ badge's attention colour, since a hidden claude
            # wears ⊘ rather than ⚙.
            ignore_colour = (
                C_STATE["PERMIT"]
                if "claude" in getattr(p, "attention_agents", ())
                else C_DIM)
            ignore_marker = f" {ignore_colour}⊘{C_RESET}"
            pane_marker_visible_w += 2  # " ⊘"
        else:
            ignore_marker = ""
        pane_marker += ignore_marker
        # External-agent presence badge `⚙<name>`: a pane running an
        # external agent CLI exists in this window. Dim like `⊘` —
        # presence only, not a state. When a sidekick has a live
        # attention marker (it is blocked on a decision), the badge
        # takes PERMIT's bold yellow: same colour vocabulary, same
        # meaning — a human is needed — while staying out of the
        # 4-state model itself.
        ext_label = external_agent_label(p)
        if ext_label:
            badge = f"⚙{ext_label}"
            colour = (C_STATE["PERMIT"]
                      if getattr(p, "attention_agents", ()) else C_DIM)
            pane_marker += f" {colour}{badge}{C_RESET}"
            pane_marker_visible_w += 1 + display_width(badge)
        # Spool marker `✉N`: N messages are queued for this project
        # (store-and-forward). Dim like `⊘` — a queue length, not a
        # state. `✉` (U+2709) is ambiguous-width, same as `⚙`.
        spool_n = spool_counts.get(p.name, 0)
        if spool_n:
            spool_marker = f"✉{spool_n}"
            pane_marker += f" {C_DIM}{spool_marker}{C_RESET}"
            pane_marker_visible_w += 1 + display_width(spool_marker)
        branch = p.branch or "-"
        ports = p.ports or "-"
        d = ccm_core.shorten_home(p.dir) if p.dir else ""
        # Permission-mode badge. A secondary indicator (dim), except
        # bypassPermissions which means every guardrail is off and
        # gets the same bold yellow as PERMIT. "-" when unknown
        # (SHELL/DOWN, hooks not installed, or no mode-bearing hook
        # event yet).
        mode_label = permission_mode_label(p.permission_mode) or "-"
        mode_color = ("\033[1;33m" if p.permission_mode in PERMISSION_MODE_WARN
                      else C_DIM)
        mode_field = f"{mode_color}{pad_to_width(mode_label, 8)}{C_RESET}"
        # ANSI codes inflate len() past visible width; reserve the
        # extra characters in the format spec so columns still line
        # up. CJK / emoji project names need `pad_to_width` (terminal
        # columns) instead of the f-string `<N` spec (codepoint
        # count); otherwise a name like `日本語` would be padded by 17
        # spaces under `<20` and overflow the column by 6 columns.
        # A name wider than the column is truncated (wide chars kept
        # whole) instead of being clamped to zero padding, which
        # previously let an overlong name shove every later column
        # rightward and break the table.
        name_avail = max(0, 20 - pane_marker_visible_w)
        name_text = truncate_to_width(p.name, name_avail)
        name_pad_w = max(0, name_avail - display_width(name_text))
        name_field = f"{name_text}{pane_marker}{' ' * name_pad_w}"
        print(f"{status_field} {name_field} {mode_field} "
              f"{pad_to_width(branch, 16)} {pad_to_width(ports, 12)} {d}")

    _print_spool_summary(spool_counts)


def _print_spool_summary(spool_counts):
    """A queued-but-undelivered message is the store-and-forward
    equivalent of an unread refusal — keep the totals visible where
    an agent polling `ccm status` will see them."""
    total_queued = sum(spool_counts.values())
    if total_queued:
        breakdown = ", ".join(f"{n}:{c}" for n, c in spool_counts.items())
        print(f"\n{C_DIM}spool: {total_queued} queued ({breakdown}) — "
              f"`ccm spool list`{C_RESET}")


def print_ports():
    """Print listening ports per project (for `ccm ports` CLI command)."""
    projects = ccm_core.build_project_list(fast=True)
    if not projects:
        print("No active projects.")
        return

    print(f"{C_BOLD}{'PROJECT':<20} {'PORTS':<16} {'DIRECTORY'}{C_RESET}")
    print(f"{'-------':<20} {'-----':<16} {'---------'}")

    for p in projects:
        ports = p.ports or "-"
        d = ccm_core.shorten_home(p.dir) if p.dir else ""
        print(f"{pad_to_width(p.name, 20)} {pad_to_width(ports, 16)} {d}")


def print_tree():
    """Print hierarchical tree of all sessions/windows/panes (for `ccm tree`)."""
    sessions_raw = ccm_core.tmux_cmd("list-sessions", "-F", "#{session_name}")
    if not sessions_raw:
        print("No tmux sessions.")
        return

    sessions = sorted(sessions_raw.split("\n"))
    current_session = ccm_core.get_session()

    projects = ccm_core.build_project_list(fast=True)
    project_map = {p.win_target: p for p in projects}

    for si, sess in enumerate(sessions):
        is_last_s = si == len(sessions) - 1
        s_pre = "└── " if is_last_s else "├── "
        s_cont = "    " if is_last_s else "│   "
        marker = " ◀" if sess == current_session else ""
        print(f"{s_pre}{C_BOLD}{sess}{C_RESET}{marker}")

        windows_raw = ccm_core.tmux_cmd("list-windows", "-t", sess, "-F",
                               "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}")
        if not windows_raw:
            continue
        windows = windows_raw.split("\n")

        for wi, wline in enumerate(windows):
            parts = wline.split("\t")
            while len(parts) < 4:
                parts.append("")
            win_idx, win_name, project, wdir = parts[:4]
            win_target = f"{sess}:{win_idx}"
            is_last_w = wi == len(windows) - 1
            w_pre = f"{s_cont}└── " if is_last_w else f"{s_cont}├── "

            proj = project_map.get(win_target)
            if proj:
                color = C_STATE.get(proj.state, C_DIM)
                icon = STATE_ICONS.get(proj.state, "?")
                name = proj.name
                extra = ""
                if proj.branch:
                    extra += f" ({proj.branch})"
                if proj.ports:
                    extra += f" [:{proj.ports}]"
            else:
                color = C_DIM
                icon = ""
                name = win_name
                extra = ""

            d = ""
            if wdir:
                d = f" {ccm_core.shorten_home(wdir)}"
            elif not project:
                pane_path = ccm_core.tmux_cmd("display-message", "-t", win_target, "-p", "#{pane_current_path}")
                if pane_path:
                    d = f" {ccm_core.shorten_home(pane_path)}"

            icon_str = f"{color}{icon}{C_RESET} " if icon else ""
            print(f"{w_pre}{icon_str}{name}{extra}{C_DIM}{d}{C_RESET}")


# Colour table for the bg-session state column. Mirrors the
# intent of `C_STATE` for ccm projects: NEEDS demands attention
# (yellow), WORKING is in-progress (cyan), IDLE is at-rest (blue),
# DONE / FAILED are terminal (dim / red).
_BG_STATE_COLOR = {
    "NEEDS": "\033[1;33m",       # bold yellow
    "WORKING": "\033[36m",       # cyan
    "IDLE": "\033[0;34m",        # blue
    "DONE": "\033[2m",           # dim
    "FAILED": "\033[31m",        # red
    "UNKNOWN": "\033[2m",        # dim
}


def print_bg_sessions():
    """Print external Claude Code agent-view sessions (the per-user
    daemon's roster, joined with each session's job state.json).

    Read-only: ccm does not dispatch or stop these sessions; it
    surfaces them so the user has one place to see both ccm-managed
    project windows and out-of-tmux background agent-view sessions.
    Lifecycle stays with Claude Code's own CLI (`claude agents`,
    `claude attach`, `claude stop`).

    For `ccm bg list` CLI.
    """
    import ccm_agentview  # local import keeps cold-start cheap

    sessions = ccm_agentview.list_bg_sessions()
    if not sessions:
        if ccm_agentview.daemon_running():
            print("No active background sessions.")
        else:
            print("No background sessions (Claude Code agent-view "
                  "daemon is not running).")
            print(f"{C_DIM}Start one with `claude --bg <prompt>` "
                  f"or `claude agents`.{C_RESET}")
        return

    print(f"{C_BOLD}{'SHORT':<10} {'STATE':<11} {'NAME':<38} "
          f"{'AGE':<7} {'DIRECTORY'}{C_RESET}")
    print(f"{'-----':<10} {'-----':<11} {'----':<38} "
          f"{'---':<7} {'---------'}")

    for s in sessions:
        from ccm_agentview import STATE_ICONS as BG_ICONS
        icon = BG_ICONS.get(s.state, "?")
        color = _BG_STATE_COLOR.get(s.state, C_DIM)
        state_field = f"{color}{icon} {s.state:<7}{C_RESET}"
        # 11 visible cols = "✽ WORKING  " (icon is 1 visible col,
        # the state word is up to 7, plus separator + padding).
        # ANSI codes don't count toward width.
        state_w = 11 + (len(state_field) - display_width(f"{icon} {s.state:<7}"))

        name = s.name or "(unnamed)"
        if display_width(name) > 38:
            name = truncate_to_width(name, 37) + "…"
        name_padded = pad_to_width(name, 38)

        age_str = ""
        if s.created_at:
            age = int(time.time() - s.created_at)
            if age < 60:
                age_str = f"{age}s"
            elif age < 3600:
                age_str = f"{age // 60}m"
            elif age < 86400:
                age_str = f"{age // 3600}h"
            else:
                age_str = f"{age // 86400}d"

        d = ccm_core.shorten_home(s.cwd) if s.cwd else ""

        short_field = pad_to_width(s.short, 10)
        age_field = pad_to_width(age_str, 7)

        print(f"{short_field} {state_field:<{state_w}} {name_padded} "
              f"{age_field} {C_DIM}{d}{C_RESET}")


def print_statusline():
    """Print one-line status for tmux status bar (for `ccm statusline`)."""
    projects = ccm_core.build_project_list(fast=True)
    active = [p for p in projects if p.state in ("BUSY", "PERMIT")]
    if not active:
        return

    parts = []
    for p in active:
        icon = STATE_ICONS.get(p.state, "?")
        parts.append(f"{p.name}:{icon}")

    print(f"| {' '.join(parts)} |")
