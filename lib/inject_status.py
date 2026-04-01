#!/usr/bin/env python3
"""ccm inject-status — status bar updater (called periodically by tmux)."""

import fcntl
import os
import re
import signal
import subprocess
import sys
import time

# Add lib dir to path for ccm_core import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ccm_core import (
    CCM_ROOT, CCM_TMP_DIR, CCM_SNAPSHOT_DIR,
    STATE_ICONS, STATE_PRIORITY,
    tmux_cmd, tmux_batch, build_project_list, update_window_names,
    auto_exit_idle, periodic_autosave, notify,
)

# tmux status bar color map
TMUX_COLORS = {
    "PERMIT": "yellow",
    "BUSY": "#e8967d",
    "DONE": "green",
    "IDLE": "#5f87af",
    "SHELL": "#8a8a8a",
    "DOWN": "#8a8a8a",
}


def acquire_lockfile():
    """Prevent concurrent inject-status execution using flock."""
    lockfile = os.path.join(CCM_TMP_DIR, "inject.lock")
    os.makedirs(CCM_TMP_DIR, exist_ok=True)

    fd = open(lockfile, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        fd.close()
        return None  # Another instance holds the lock
    fd.write(str(os.getpid()))
    fd.flush()
    return fd  # Keep fd open to hold the lock


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
    has_permit = has_busy = has_done = False
    for p in projects:
        if p.state == "PERMIT":
            has_permit = True
        elif p.state == "BUSY":
            has_busy = True
        elif p.state == "DONE":
            has_done = True

    if has_permit:
        return TMUX_COLORS["PERMIT"]
    elif has_busy:
        return TMUX_COLORS["BUSY"]
    elif has_done:
        return TMUX_COLORS["DONE"]
    return TMUX_COLORS["IDLE"]


def priority_icon(projects):
    """Determine highest priority icon with window indices."""
    permit_wins = []
    busy_wins = []
    done_wins = []
    for p in projects:
        if p.state == "PERMIT":
            permit_wins.append(p.win_idx)
        elif p.state == "BUSY":
            busy_wins.append(p.win_idx)
        elif p.state == "DONE":
            done_wins.append(p.win_idx)

    if permit_wins:
        return f"{','.join(permit_wins)}: PERMIT ⚠"
    elif busy_wins:
        return f"{','.join(busy_wins)}: BUSY ◉"
    elif done_wins:
        return f"{','.join(done_wins)}: DONE ✔"
    return "≡"


def build_detail_entries(projects, with_extras=False):
    """Build status bar entries for mode 1/2."""
    entries = []
    for p in projects:
        color = TMUX_COLORS.get(p.state, TMUX_COLORS["SHELL"])
        icon = STATE_ICONS.get(p.state, "○")

        if with_extras:
            entry = f"#[fg=#666666]{p.win_idx}:#[fg=#9E9E9E]{p.name}"
            if p.branch:
                entry += f" #[fg=#666666](#[fg=cyan]{p.branch}#[fg=#666666])#[fg=#9E9E9E]"
            if p.ports:
                entry += f"#[fg=#666666][:{p.ports}]#[fg=#9E9E9E]"
            entry += f":#[fg={color}]{icon}#[fg=#9E9E9E]"
        else:
            entry = f"{p.name}:#[fg={color}]{icon}#[fg=#9E9E9E]"

        entries.append(entry)
    return entries


def scan_active_windows(projects, include_all=False):
    """Filter projects for status bar display."""
    if include_all:
        return projects
    return [p for p in projects if p.state in ("BUSY", "PERMIT", "DONE")]


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

    # Get mode
    mode = tmux_cmd("show-option", "-gqv", "@ccm-status-line") or "0"

    # Build project list (full detection)
    projects = build_project_list(fast=False)

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
            with open(notify_cache) as f:
                for line in f:
                    parts = line.strip().split("\t", 1)
                    if len(parts) == 2:
                        prev_states[parts[0]] = parts[1]
    except OSError:
        pass

    # Check if hook already sent a recent PERMIT notification (avoid duplicate)
    permit_notified_file = os.path.join(CCM_TMP_DIR, "permit-notified")
    hook_permit_ts = 0
    try:
        if os.path.exists(permit_notified_file):
            with open(permit_notified_file) as f:
                hook_permit_ts = int(f.read().strip())
    except (OSError, ValueError):
        pass
    now = int(time.time())

    # Write current states and check for transitions
    try:
        tmp = notify_cache + ".tmp"
        with open(tmp, "w") as f:
            for p in projects:
                f.write(f"{p.win_target}\t{p.state}\n")
                prev = prev_states.get(p.win_target, "")
                if p.state != prev and p.state in ("PERMIT", "DONE"):
                    # Skip PERMIT notification if hook already sent one within 5 seconds
                    if p.state == "PERMIT" and (now - hook_permit_ts) < 5:
                        continue
                    notify(p.state, p.name)
        os.replace(tmp, notify_cache)
    except OSError:
        pass

    # Periodic autosave
    periodic_autosave()

    # Auto-exit idle sessions
    auto_exit_idle(projects)

    # Check if dashboard is running — skip status bar rendering if so
    dash_pidfile = os.path.join(CCM_TMP_DIR, "dashboard.pid")
    if os.path.exists(dash_pidfile):
        try:
            dash_pid = int(open(dash_pidfile).read().strip())
            os.kill(dash_pid, 0)  # Check if running
            return  # Dashboard handles its own display
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass

    # Status bar rendering
    cache_file = os.path.join(CCM_TMP_DIR, "status-cache")
    prev_status = ""
    try:
        if os.path.exists(cache_file):
            with open(cache_file) as f:
                prev_status = f.read()
    except OSError:
        pass

    original = tmux_cmd("show-option", "-gqv", "@ccm-orig-status-right")
    ccm_bin = os.path.join(CCM_ROOT, "ccm")
    refresh = f"#({ccm_bin} inject-status 2>/dev/null)"

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

        entries = build_detail_entries(all_projects)

        if not entries:
            new_status = f"#[fg=#666666]≡#[default] {original}{refresh}"
        else:
            # Calculate available width for ccm entries
            term_width = 120
            try:
                term_width = int(tmux_cmd("display-message", "-p", "#{client_width}") or "120")
            except ValueError:
                pass
            orig_visible = len(re.sub(r'#\[[^\]]*\]', '', original))
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
        entries = build_detail_entries(all_projects, with_extras=True)

        if not entries:
            fmt = "#[align=right]#[fg=#666666,bg=#3a3a3a] ≡ ccm: no projects  "
            tmux_batch(
                ("set", "-g", "status", "2"),
                ("set", "-g", "status-format[1]", fmt),
            )
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
                total_visible_width += len(stripped) + 3  # +3 for separator "│" + spaces
            # Add separators between entries
            total_visible_width += (len(entries) - 1) * 3 if len(entries) > 1 else 0
            entries_per_line = max(1, len(entries) * term_width // max(total_visible_width, 1))
            num_lines = max(1, (len(entries) + entries_per_line - 1) // entries_per_line)

            cmds = [("set", "-g", "status", str(num_lines + 1))]

            entry_idx = 0
            for line_idx in range(num_lines):
                line_str = ""
                count = 0
                while entry_idx < len(entries) and count < entries_per_line:
                    if count > 0:
                        line_str += " #[fg=#666666]│#[fg=#9E9E9E]"
                    line_str += f" {entries[entry_idx]}"
                    entry_idx += 1
                    count += 1
                fmt = f"#[align=right]#[fg=#9E9E9E,bg=#3a3a3a]{line_str}  "
                cmds.append(("set", "-g", f"status-format[{line_idx + 1}]", fmt))

            # Clear extra lines
            for extra in range(num_lines + 1, 6):
                cmds.append(("set", "-g", "-u", f"status-format[{extra}]"))
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


def _cleanup_extra_lines():
    cmds = [("set", "-g", "status", "on")]
    for n in range(1, 6):
        cmds.append(("set", "-g", "-u", f"status-format[{n}]"))
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
        for n in range(1, 6):
            cmds.append(("set", "-g", "-u", f"status-format[{n}]"))
        tmux_batch(*cmds)
    orig_len = tmux_cmd("show-option", "-gqv", "@ccm-orig-sr-length")
    if orig_len:
        tmux_cmd("set", "-g", "status-right-length", orig_len)


def _touch_mode2_marker():
    marker = os.path.join(CCM_TMP_DIR, "mode2-active")
    try:
        open(marker, "a").close()
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
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, cache_file)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        inject_status()
    except Exception:
        pass  # Never crash — tmux will retry
