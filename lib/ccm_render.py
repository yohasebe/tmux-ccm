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
def _resolve_ambiguous_width() -> int:
    raw = os.environ.get("CCM_AMBIGUOUS_WIDTH", "1")
    try:
        v = int(raw)
    except ValueError:
        return 1
    return 2 if v == 2 else 1


_AMBIGUOUS_WIDTH = _resolve_ambiguous_width()


def _char_width(c: str) -> int:
    cat = unicodedata.category(c)
    if cat in ("Mn", "Me", "Cf"):
        return 0
    eaw = unicodedata.east_asian_width(c)
    if eaw in ("W", "F"):
        return 2
    if eaw == "A":
        return _AMBIGUOUS_WIDTH
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
from ccm_core import (
    JSONL_HOOK_GAP_TOLERANCE,
    STATE_ICONS,
)


# ─── ANSI colour codes ───
# Used by the print_* helpers below and re-exported by ccm_core
# for any caller that needs to stay terminal-output-aware.
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


def signal_age_suffix(project_dir, state):
    """Returns a parenthesised stale-signal age (e.g. " (8m)") when
    the hook signal for this project is old enough to be worth
    surfacing in the UI, or "" otherwise.

    Only returns a non-empty string for state in {BUSY, PERMIT} —
    those are the states where a stale hook signal can mask a real
    state change (IDLE) that the release rules cannot confidently
    make. SHELL / IDLE / DOWN either have no associated hook
    signal or the signal is freshness-irrelevant.

    Best-effort: never raises; returns "" on any error reading the
    signal file."""
    if state not in ("BUSY", "PERMIT"):
        return ""
    if not project_dir:
        return ""
    try:
        sig = ccm_core.read_hook_signal(project_dir)
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


def format_elapsed(ts):
    if not ts or ts == 0:
        return ""
    elapsed = int(time.time()) - ts
    if elapsed < 0:
        return ""
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m"
    if elapsed < 86400:
        return f"{elapsed // 3600}h"
    return f"{elapsed // 86400}d"


def format_dir(directory, prefix_len, cols):
    d = directory.replace(os.path.expanduser("~"), "~")
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


# ─── CLI commands ───

def print_status():
    """Print status of all ccm projects (for `ccm status` CLI command)."""
    projects = ccm_core.build_project_list(fast=False)

    if not projects:
        print("No active projects.")
        return

    if ccm_core.hooks_configured():
        print(f"{C_DIM}Hooks: ON{C_RESET}")
    else:
        print(f"{C_DIM}Hooks: OFF (run 'ccm setup-hooks' for improved detection){C_RESET}")
    for warning in (
        ccm_core.hooks_log_warning(),
        ccm_core.disable_all_hooks_warning(),
        ccm_core.managed_hooks_only_warning(),
    ):
        if warning:
            print(f"\033[33m⚠ {warning}\033[0m")
    for cluster_msg in ccm_core.shell_cluster_warnings(projects):
        print(f"\033[33m⚠ {cluster_msg}\033[0m")
    print()

    print(f"{C_BOLD}{'STATUS':<12} {'PROJECT':<20} {'BRANCH':<16} {'PORTS':<12} {'DIRECTORY'}{C_RESET}")
    print(f"{'------':<12} {'-------':<20} {'------':<16} {'-----':<12} {'---------'}")

    for p in projects:
        color = C_STATE.get(p.state, C_DIM)
        icon = STATE_ICONS.get(p.state, "?")
        # State-modifier suffixes (stale-age / bg) attach to the
        # STATUS column right after the state name. Mutually
        # exclusive — stale only fires for BUSY/PERMIT, bg only
        # for IDLE.
        suffix = signal_age_suffix(p.dir, p.state)
        if p.bg_active:
            suffix += " (bg)"
        status = f"{color}{icon} {p.state}{suffix}{C_RESET}"
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
        branch = p.branch or "-"
        ports = p.ports or "-"
        d = p.dir.replace(os.path.expanduser("~"), "~") if p.dir else ""
        # ANSI codes inflate len() past visible width; reserve the
        # extra characters in the format spec so columns still line
        # up. CJK / emoji project names need `pad_to_width` (terminal
        # columns) instead of the f-string `<N` spec (codepoint
        # count); otherwise a name like `日本語` would be padded by 17
        # spaces under `<20` and overflow the column by 6 columns.
        status_w = 22 + len(suffix)
        name_pad_w = max(0, 20 - pane_marker_visible_w - display_width(p.name))
        name_field = f"{p.name}{pane_marker}{' ' * name_pad_w}"
        print(f"{status:<{status_w}} {name_field} "
              f"{pad_to_width(branch, 16)} {pad_to_width(ports, 12)} {d}")


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
        d = p.dir.replace(os.path.expanduser("~"), "~") if p.dir else ""
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
                d = f" {wdir.replace(os.path.expanduser('~'), '~')}"
            elif not project:
                pane_path = ccm_core.tmux_cmd("display-message", "-t", win_target, "-p", "#{pane_current_path}")
                if pane_path:
                    d = f" {pane_path.replace(os.path.expanduser('~'), '~')}"

            icon_str = f"{color}{icon}{C_RESET} " if icon else ""
            print(f"{w_pre}{icon_str}{name}{extra}{C_DIM}{d}{C_RESET}")


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
