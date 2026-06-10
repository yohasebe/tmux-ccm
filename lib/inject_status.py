#!/usr/bin/env python3
"""ccm inject-status — status bar updater (called periodically by tmux)."""

import fcntl
import os
import re
import sys
import time

# Add lib dir to path for ccm_core import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ccm_constants import (
    CCM_ROOT,
    CCM_SNAPSHOT_DIR,
    CCM_TMP_DIR,
    COMPLETED_AT_TIMEOUT,
    STATE_ICONS,
    STATE_PRIORITY,
)
from ccm_core import (
    build_project_list,
    tmux_batch,
    tmux_cmd,
)
from ccm_notify import notify
from ccm_render import signal_age_suffix
from ccm_runtime import (
    auto_exit_idle,
    periodic_autosave,
    update_window_names,
)
from ccm_signals import (
    read_hook_signal,
    read_project_notify_marker,
)

# tmux status bar color map
TMUX_COLORS = {
    "PERMIT": "yellow",
    "BUSY": "#e8967d",
    "IDLE": "#5f87af",
    "SHELL": "#8a8a8a",
    "DOWN": "#8a8a8a",
}


def strip_tmux_formats(s):
    """Strip tmux format codes (#[...] and #{...} including nested braces) for visible width estimation."""
    # First strip #[...] (style codes, no nesting)
    s = re.sub(r'#\[[^\]]*\]', '', s)
    # Then strip #{...} with nested braces (conditionals like #{?#{pane_in_mode},M,})
    result = []
    i = 0
    while i < len(s):
        if s[i:i+2] == '#{':
            # Find matching closing brace, accounting for nesting
            depth = 1
            j = i + 2
            while j < len(s) and depth > 0:
                if s[j] == '{':
                    depth += 1
                elif s[j] == '}':
                    depth -= 1
                j += 1
            i = j  # skip past the closing brace
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


def acquire_lockfile():
    """Prevent concurrent inject-status execution using flock."""
    lockfile = os.path.join(CCM_TMP_DIR, "inject.lock")
    os.makedirs(CCM_TMP_DIR, exist_ok=True)

    fd = open(lockfile, "w", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The pid note is diagnostic only (lets a human inspect who
        # holds the lock); a write failure (disk full, …) must not
        # leak the fd — without the close the flock would stay held
        # for the process lifetime and every later poll cycle would
        # silently skip its status update.
        fd.write(str(os.getpid()))
        fd.flush()
        return fd  # Keep fd open to hold the lock
    except (IOError, OSError):
        fd.close()
        return None  # Another instance holds the lock (or write failed)


def detect_external_status_change():
    """Detect if status-right was changed externally (by theme plugins)."""
    current_sr = tmux_cmd("show-option", "-gv", "status-right")
    if "inject-status" not in current_sr and "inject_status" not in current_sr:
        orig_len = tmux_cmd("show-option", "-gv", "status-right-length")
        tmux_batch(
            ("set", "-g", "@ccm-orig-status-right", current_sr),
            ("set", "-g", "@ccm-orig-sr-length", orig_len),
        )
        # Clear cache to force re-injection
        cache_file = os.path.join(CCM_TMP_DIR, "status-cache")
        try:
            os.unlink(cache_file)
        except OSError:
            pass


def sanitize_orig_status():
    """Remove any inject-status fragments from saved original status."""
    orig = tmux_cmd("show-option", "-gqv", "@ccm-orig-status-right")
    if orig and ("inject-status" in orig or "inject_status" in orig):
        try:
            cleaned = re.sub(r'#\([^)]*inject.status[^)]*\)', '', orig)
            cleaned = re.sub(r'#\[fg=[^]]*bg=#3a3a3a[^]]*\][^#]*#\[default\]', '', cleaned)
            tmux_cmd("set", "-g", "@ccm-orig-status-right", cleaned)
        except Exception:
            pass


def priority_color(projects):
    """Determine highest priority color from project states."""
    has_permit = has_busy = False
    for p in projects:
        if p.state == "PERMIT":
            has_permit = True
        elif p.state == "BUSY":
            has_busy = True

    if has_permit:
        return TMUX_COLORS["PERMIT"]
    elif has_busy:
        return TMUX_COLORS["BUSY"]
    return TMUX_COLORS["IDLE"]


def priority_icon(projects):
    """Determine highest priority icon with window indices."""
    permit_wins = []
    busy_wins = []
    for p in projects:
        if p.state == "PERMIT":
            permit_wins.append(p.win_idx)
        elif p.state == "BUSY":
            busy_wins.append(p.win_idx)

    if permit_wins:
        return f"{','.join(permit_wins)}: PERMIT ⚠"
    elif busy_wins:
        return f"{','.join(busy_wins)}: BUSY ◉"
    return "≡"


def build_detail_entries(projects, with_extras=False, current_win_target=""):
    """Build status bar entries for mode 1/2.
    current_win_target: full `session:window_index` of the active
    window. Compared against `Project.win_target`, not just the
    index, so that two windows sharing an index across sessions are
    not both highlighted as active.
    """
    entries = []
    for p in projects:
        color = TMUX_COLORS.get(p.state, TMUX_COLORS["SHELL"])
        icon = STATE_ICONS.get(p.state, "○")
        is_current = (p.win_target == current_win_target)

        # Stale-signal suffix for BUSY / PERMIT (e.g. "(2m)"). Same
        # threshold as the dashboard / `ccm status` affordance — gives
        # the user a visible hint that a stuck-looking state is past
        # the auto-release window without forcing them to open the
        # popup. Stripped of leading space; we add explicit dim
        # markup before it.
        stale = signal_age_suffix(p.dir, p.state).strip()
        # Background-activity affordance: state=IDLE but raw=BUSY
        # (leftover dev server etc.). Mutually exclusive with `stale`
        # (which only fires for BUSY/PERMIT) so we can render either
        # cleanly.
        bg = "(bg)" if p.bg_active else ""
        # Stale-age and bg fire on different states (BUSY/PERMIT vs
        # IDLE) so they're mutually exclusive. Both render AFTER
        # the state icon as state-modifiers ("BUSY but stale", "IDLE
        # but with leftover bg processes").
        post = stale or bg
        # Multi-pane marker `[N]` is structural (about the window's
        # pane layout, not the project's state). Render it BEFORE
        # the icon so the visual unit `:icon` stays clean and the
        # marker sits next to the project name where the user
        # parses identity. Brackets dim, number cyan so the count
        # draws the eye. ASCII-only to avoid font/terminal width
        # ambiguity that would offset later columns.
        pane_n = str(p.pane_count) if p.pane_count > 1 else ""

        def _render_pane_marker(name_color):
            """`[N]` with brackets dim and N cyan, returning to
            the caller's name colour after."""
            if not pane_n:
                return ""
            return (
                f" #[fg=#666666][#[fg=cyan]{pane_n}#[fg=#666666]]"
                f"#[fg={name_color}]"
            )

        if with_extras:
            # Mode 2: idx:name (branch) [:port][ [N]]:icon[(stale|bg)]
            if is_current:
                entry = f"#[fg=#ffffff,bold]{p.win_idx}:#[fg=#ffffff,bold]{p.name}"
            else:
                entry = f"#[fg=#666666]{p.win_idx}:#[fg=#9E9E9E]{p.name}"
            if p.branch:
                if is_current:
                    entry += f" #[fg=#888888](#[fg=cyan,bold]{p.branch}#[fg=#888888])#[fg=#ffffff,bold]"
                else:
                    entry += f" #[fg=#666666](#[fg=cyan]{p.branch}#[fg=#666666])#[fg=#9E9E9E]"
            if p.ports:
                entry += f"#[fg=#666666][:{p.ports}]#[fg=#9E9E9E]"
            entry += _render_pane_marker(
                "#ffffff,bold" if is_current else "#9E9E9E"
            )
            entry += f":#[fg={color}]{icon}#[fg=#9E9E9E]"
            if post:
                entry += f"#[fg=#666666]{post}#[fg=#9E9E9E]"
            if is_current:
                entry += "#[nobold]"
        else:
            # Mode 1: name[ [N]]:icon[(stale|bg)]
            if is_current:
                entry = f"#[fg=#ffffff,bold]{p.name}"
                entry += _render_pane_marker("#ffffff,bold")
                entry += f":#[fg={color},bold]{icon}#[nobold]#[fg=#9E9E9E]"
            else:
                entry = f"{p.name}"
                entry += _render_pane_marker("#9E9E9E")
                entry += f":#[fg={color}]{icon}#[fg=#9E9E9E]"
            if post:
                entry += f"#[fg=#666666]{post}#[fg=#9E9E9E]"

        entries.append(entry)
    return entries


def scan_active_windows(projects, include_all=False):
    """Filter projects for status bar display."""
    if include_all:
        return projects
    return [p for p in projects if p.state in ("BUSY", "PERMIT")]


def inject_status():
    """Main inject-status logic."""
    lock_fd = acquire_lockfile()
    if lock_fd is None:
        return  # Another instance running

    try:
        _inject_status_impl()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except OSError:
            pass


def _inject_status_impl():
    # Detect external status-right changes
    detect_external_status_change()
    sanitize_orig_status()

    # If dashboard is running, skip full detection to avoid race conditions.
    # Both inject-status and dashboard write @ccm_prev_state via _set_win_state;
    # running both causes state flickering between different detection results.
    dash_pidfile = os.path.join(CCM_TMP_DIR, "dashboard.pid")
    dashboard_running = False
    if os.path.exists(dash_pidfile):
        try:
            dash_pid = int(open(dash_pidfile, encoding="utf-8").read().strip())
            os.kill(dash_pid, 0)
            dashboard_running = True
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass

    # Get mode
    mode = tmux_cmd("show-option", "-gqv", "@ccm-status-line") or "2"

    # Build project list — use fast mode when dashboard handles full detection
    projects = build_project_list(fast=dashboard_running)

    # Check for instant PERMIT flag set by hook (bypass polling delay)
    permit_pending = tmux_cmd("show-option", "-gqv", "@ccm-permit-pending")
    if permit_pending:
        tmux_cmd("set", "-g", "-u", "@ccm-permit-pending")  # Clear flag
        # Force PERMIT state on the matching project if not already detected
        parts = permit_pending.split(":", 1)
        if len(parts) == 2:
            pending_idx, pending_name = parts
            for p in projects:
                if p.win_idx == pending_idx and p.state != "PERMIT":
                    p.state = "PERMIT"
                    break

    # Always update window name icons
    update_window_names(projects)

    # Desktop notifications on state transitions
    # Read previous states BEFORE build_project_list overwrites them
    # (build_project_list already ran above, so we use a cache file approach)
    notify_cache = os.path.join(CCM_TMP_DIR, "notify-cache")
    prev_states = {}
    try:
        if os.path.exists(notify_cache):
            with open(notify_cache, encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split("\t", 1)
                    if len(parts) == 2:
                        prev_states[parts[0]] = parts[1]
    except OSError:
        pass

    # Per-project instant-notify marker is read inside the loop below
    # via `read_project_notify_marker`. The marker is per-project so
    # one project's recent notification cannot suppress another's,
    # which matters when several concurrent Claude sessions are
    # running.
    now = int(time.time())

    # Write current states and check for transitions.
    # Polling notifications are only a SAFETY NET for the hook-
    # triggered instant notification path: they fire when the
    # project's own hook signal corroborates the state. This
    # prevents late / spurious notifications that would otherwise
    # fire whenever a fallback detection path derives a state
    # transition long after the actual event.
    #
    # COMPLETED: the Stop hook DELETES the signal file, so there is
    # no hook signal to corroborate a BUSY→IDLE transition after the
    # fact. We therefore rely exclusively on the instant path
    # (`_ccm_instant_notify` called from `on-stop.sh` /
    # `on-notification.sh idle_prompt`) to deliver completion
    # notifications. If Stop / idle_prompt never fires (hooks.log
    # bloat, upstream silent-exit, ...), the fallback detection will
    # still transition the project to IDLE, but deliberately WITHOUT
    # a notification — we prefer silence to a late, misleading ping.
    try:
        tmp = notify_cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for p in projects:
                f.write(f"{p.win_target}\t{p.state}\n")
                prev = prev_states.get(p.win_target, "")
                # Notify on PERMIT transitions
                if p.state != prev and p.state == "PERMIT":
                    # Per-project dedup: if the hook already instant-
                    # notified PERMIT for THIS project, skip.
                    marker = read_project_notify_marker(p.dir) if p.dir else None
                    if marker is not None:
                        marker_ts, marker_state = marker
                        if marker_state == p.state and (now - marker_ts) < COMPLETED_AT_TIMEOUT:
                            continue
                    hook = read_hook_signal(p.dir) if p.dir else None
                    if not hook:
                        continue
                    hook_ts, hook_st, hook_detail = hook
                    if hook_st != p.state:
                        continue
                    if (now - hook_ts) >= COMPLETED_AT_TIMEOUT:
                        continue
                    detail = hook_detail if p.state == "PERMIT" else ""
                    notify(p.state, p.name, detail)
        os.replace(tmp, notify_cache)
    except OSError:
        pass

    # Periodic autosave
    periodic_autosave()

    # Auto-exit idle sessions
    auto_exit_idle(projects)

    # Status bar rendering (continues even when dashboard is running,
    # using fast-mode project list to stay in sync without race conditions)
    cache_file = os.path.join(CCM_TMP_DIR, "status-cache")
    prev_status = ""
    try:
        if os.path.exists(cache_file):
            with open(cache_file, encoding="utf-8") as f:
                prev_status = f.read()
    except OSError:
        pass

    original = tmux_cmd("show-option", "-gqv", "@ccm-orig-status-right")
    ccm_bin = os.path.join(CCM_ROOT, "ccm")
    refresh = f"#({ccm_bin} inject-status 2>/dev/null)"

    # Current window target (session:index) for highlighting the active
    # project in mode 1/2. Indices alone are not unique across sessions,
    # so we must compare the full target.
    current_win_target = tmux_cmd(
        "display-message", "-p", "#{session_name}:#{window_index}"
    )

    if mode == "1":
        # Mode 1: ccm-style window list in status-right (fit as many as possible)
        _cleanup_extra_lines()
        all_projects = scan_active_windows(projects, include_all=True)

        # Hide standard window list
        _touch_mode2_marker()
        tmux_batch(
            ("set", "-g", "window-status-format", ""),
            ("set", "-g", "window-status-current-format", ""),
        )

        entries = build_detail_entries(all_projects, current_win_target=current_win_target)

        if not entries:
            new_status = f"#[fg=#666666]≡#[default] {original}{refresh}"
        else:
            # Calculate available width for ccm entries
            term_width = 120
            try:
                term_width = int(tmux_cmd("display-message", "-p", "#{client_width}") or "120")
            except ValueError:
                pass
            orig_visible = len(strip_tmux_formats(original))
            avail = term_width - orig_visible - 10  # margin for separators + refresh

            # Select entries by priority (highest first) until width is exhausted,
            # then reverse so high-priority items appear on the right (visible in right-aligned status-right)
            selected = []
            for entry in entries:
                stripped = re.sub(r'#\[[^\]]*\]', '', entry)
                entry_width = len(stripped) + 3  # separator + spaces
                if selected and (avail - entry_width) < 0:
                    break
                selected.append(entry)
                avail -= entry_width

            # Reverse: low priority on left (clipped first), high priority on right (always visible)
            selected.reverse()

            detail = ""
            for i, entry in enumerate(selected):
                if i > 0:
                    detail += " #[fg=#666666]│#[fg=#9E9E9E]"
                detail += f" {entry}"

            new_status = f"#[fg=#9E9E9E,bg=#3a3a3a]{detail} #[fg=#666666]│#[default]{original}{refresh}"

        if new_status != prev_status:
            _write_cache(cache_file, new_status)
            tmux_cmd("set", "-g", "status-right", new_status)
            _extend_status_right_length(original, factor=2, minimum=120)

    elif mode == "2":
        # Mode 2: dedicated status line(s)
        main_status = f"{original}{refresh}"

        _touch_mode2_marker()
        tmux_batch(
            ("set", "-g", "status-right", main_status),
            ("set", "-g", "window-status-format", ""),
            ("set", "-g", "window-status-current-format", ""),
        )

        all_projects = scan_active_windows(projects, include_all=True)
        entries = build_detail_entries(all_projects, with_extras=True, current_win_target=current_win_target)

        # Visual palette for the dedicated mode-2 line(s). `BG`
        # (slightly darker than the main bar) keeps the ccm rows
        # visually settled below. `GUTTER_BG` is a single near-
        # black row inserted between the main bar and the entry
        # rows so the boundary reads as two distinct regions
        # rather than a continuation. Each colour can be overridden
        # via tmux options (`@ccm-status-bg`, `@ccm-status-gutter-bg`,
        # `@ccm-status-fg`, `@ccm-status-fg-dim`) so light-theme or
        # accessibility-tuned setups can adjust without forking.
        # Separator `·` with 2-space padding either side.
        BG = _opt_color("@ccm-status-bg", "#262626")
        GUTTER_BG = _opt_color("@ccm-status-gutter-bg", "#1a1a1a")
        FG_DEFAULT = _opt_color("@ccm-status-fg", "#9E9E9E")
        FG_DIM = _opt_color("@ccm-status-fg-dim", "#5a5a5a")
        SEP = f"  #[fg={FG_DIM}]·#[fg={FG_DEFAULT}]  "
        SEP_VISIBLE_W = 5  # "  ·  "
        GUTTER_FMT = f"#[fill={GUTTER_BG}]#[bg={GUTTER_BG}] "

        if not entries:
            fmt = f"#[fill={BG}]#[fg={FG_DIM},bg={BG}] ≡ ccm: no projects  "
            cmds = [
                ("set", "-g", "status", "3"),
                ("set", "-g", "status-format[1]", GUTTER_FMT),
                ("set", "-g", "status-format[2]", fmt),
            ]
            cmds.extend(_clear_mode2_slots_above(2))
            tmux_batch(*cmds)
        else:
            term_width = 120
            try:
                term_width = int(tmux_cmd("display-message", "-p", "#{client_width}") or "120")
            except ValueError:
                pass

            # Estimate entry width from ALL entries (strip tmux color codes)
            total_visible_width = 0
            for e in entries:
                stripped = re.sub(r'#\[[^\]]*\]', '', e)
                total_visible_width += len(stripped) + 1  # +1 for leading space
            total_visible_width += (len(entries) - 1) * SEP_VISIBLE_W if len(entries) > 1 else 0
            entries_per_line = max(1, len(entries) * term_width // max(total_visible_width, 1))
            num_lines = max(1, (len(entries) + entries_per_line - 1) // entries_per_line)
            # Cap num_lines at what `_clear_mode2_slots_above` can
            # actually clean up. Without this, an extreme degenerate
            # case (very narrow terminal × many projects) could write
            # to slots beyond `_MODE2_MAX_SLOTS`, and the next render
            # with a smaller layout would leave the high slots stale.
            # Slot 1 is the gutter, so `num_lines + 1 == _MODE2_MAX_SLOTS`
            # is the highest entry slot we may write.
            max_entry_lines = _MODE2_MAX_SLOTS - 1
            if num_lines > max_entry_lines:
                num_lines = max_entry_lines
                # Pack remaining entries into the last visible line so
                # nothing silently disappears — better to render the
                # last row crowded than to drop projects from view.
                entries_per_line = max(1, (len(entries) + num_lines - 1) // num_lines)

            # Layout: main bar + 1 gutter + N entry lines.
            cmds = [
                ("set", "-g", "status", str(num_lines + 2)),
                ("set", "-g", "status-format[1]", GUTTER_FMT),
            ]

            entry_idx = 0
            for line_idx in range(num_lines):
                line_str = ""
                count = 0
                while entry_idx < len(entries) and count < entries_per_line:
                    if count > 0:
                        line_str += SEP
                    else:
                        line_str += " "
                    line_str += entries[entry_idx]
                    entry_idx += 1
                    count += 1
                fmt = f"#[fill={BG}]#[fg={FG_DEFAULT},bg={BG}]{line_str}  "
                # +2 because slot 0 is main bar, slot 1 is gutter,
                # slot 2 is first entry line.
                cmds.append(("set", "-g", f"status-format[{line_idx + 2}]", fmt))

            # Clear any leftover slots from a previous render with
            # more lines (e.g. user closed projects or the terminal
            # widened so entries now fit on fewer rows).
            cmds.extend(_clear_mode2_slots_above(num_lines + 1))
            tmux_batch(*cmds)

    else:
        # Mode 0: icon in status-right
        _cleanup_mode02()
        active = scan_active_windows(projects)

        if not active:
            new_status = f"{original}#[range=user|ccm]#[fg=#666666,bg=#3a3a3a] ≡ #[norange]{refresh}"
        else:
            icon_color = priority_color(active)
            icon_char = priority_icon(active)
            new_status = f"{original}#[range=user|ccm]#[fg={icon_color},bg=#3a3a3a,bold] {icon_char} #[norange]{refresh}"

        _extend_status_right_length(original, factor=1, minimum=0, extra=40)

        if new_status != prev_status:
            _write_cache(cache_file, new_status)
            tmux_cmd("set", "-g", "status-right", new_status)


# Upper bound on mode-2 status-format slots ccm will manage.
# tmux supports indexed status-format entries indefinitely, but in
# practice mode-2 needs at most a few rows (gutter + entry lines).
# 16 covers the most extreme realistic case (very narrow terminal +
# many projects) with plenty of headroom; cheap to over-clear since
# unsetting an unset option is a no-op.
_MODE2_MAX_SLOTS = 16


def _clear_mode2_slots_above(highest_used: int):
    """Return tmux `-u` commands that clear every status-format
    slot above `highest_used` (1-indexed), up to `_MODE2_MAX_SLOTS`.
    Used after a mode-2 render to wipe rows left over from a
    previous frame that needed more lines.
    """
    return [
        ("set", "-g", "-u", f"status-format[{n}]")
        for n in range(highest_used + 1, _MODE2_MAX_SLOTS + 1)
    ]


_TMUX_COLOR_NAMES = frozenset({
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "default", "terminal", "brightblack", "brightred", "brightgreen",
    "brightyellow", "brightblue", "brightmagenta", "brightcyan", "brightwhite",
})

_TMUX_COLOR_RE = re.compile(
    r"^(?:"
    r"#[0-9a-fA-F]{3}"        # #RGB
    r"|#[0-9a-fA-F]{6}"       # #RRGGBB
    r"|colour\d{1,3}"         # colourN (palette index, tmux spelling)
    r"|color\d{1,3}"          # colorN (alias accepted by tmux)
    r")$"
)


def _is_valid_color(value: str) -> bool:
    """Return True if `value` is something tmux's `bg=` / `fg=` will
    accept. Accepts hex (`#RGB` / `#RRGGBB`), `colour123` palette
    indices, and the named colours tmux understands. Rejects everything
    else so a typo'd `@ccm-status-bg` does not silently produce a
    blank status bar.
    """
    if not value:
        return False
    if value.lower() in _TMUX_COLOR_NAMES:
        return True
    return bool(_TMUX_COLOR_RE.match(value))


def _opt_color(name: str, default: str) -> str:
    """Return tmux global option `name` if set to a valid colour,
    else `default`. Used for the user-overridable mode-2 colour
    palette. Validates the value against tmux's accepted colour
    syntax — invalid input falls back to the default rather than
    being passed through to a malformed `#[bg=garbage]` directive.
    """
    val = tmux_cmd("show-option", "-gqv", name)
    if val:
        candidate = val.strip()
        if _is_valid_color(candidate):
            return candidate
    return default


def _cleanup_extra_lines():
    cmds = [("set", "-g", "status", "on")]
    cmds.extend(_clear_mode2_slots_above(0))
    tmux_batch(*cmds)


def _cleanup_mode02():
    marker = os.path.join(CCM_TMP_DIR, "mode2-active")
    if os.path.exists(marker):
        try:
            os.unlink(marker)
        except OSError:
            pass
        cmds = [
            ("set", "-g", "-u", "window-status-format"),
            ("set", "-g", "-u", "window-status-current-format"),
            ("set", "-g", "status", "on"),
        ]
        cmds.extend(_clear_mode2_slots_above(0))
        tmux_batch(*cmds)
    orig_len = tmux_cmd("show-option", "-gqv", "@ccm-orig-sr-length")
    if orig_len:
        tmux_cmd("set", "-g", "status-right-length", orig_len)


def _touch_mode2_marker():
    marker = os.path.join(CCM_TMP_DIR, "mode2-active")
    try:
        open(marker, "a", encoding="utf-8").close()
    except OSError:
        pass


def _extend_status_right_length(original, factor=1, minimum=0, extra=0):
    orig_len = tmux_cmd("show-option", "-gqv", "@ccm-orig-sr-length") or "40"
    try:
        orig_len = int(orig_len)
    except ValueError:
        orig_len = 40
    new_len = orig_len * factor + extra
    if new_len < minimum:
        new_len = minimum
    tmux_cmd("set", "-g", "status-right-length", str(new_len))


def _write_cache(cache_file, content):
    try:
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, cache_file)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        inject_status()
    except Exception:
        # Never crash — tmux will retry. But log so the next
        # detection-cycle regression is debuggable without users
        # having to enable CCM_DEBUG_TRACE in advance.
        from ccm_core import log_caught_exception
        log_caught_exception("inject_status")
