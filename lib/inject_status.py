#!/usr/bin/env python3
"""ccm inject-status — status bar updater (called periodically by tmux)."""

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
    tmux_cmd, build_project_list, update_window_names,
    auto_exit_idle, periodic_autosave,
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


def acquire_pidfile():
    """Prevent concurrent inject-status execution."""
    pidfile = os.path.join(CCM_TMP_DIR, "inject.pid")
    os.makedirs(CCM_TMP_DIR, exist_ok=True)

    if os.path.exists(pidfile):
        try:
            old_pid = int(open(pidfile).read().strip())
            os.kill(old_pid, 0)  # Check if running
            # Check age
            age = time.time() - os.path.getmtime(pidfile)
            if age < 30:
                return None  # Still running, skip
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass  # Stale, continue

    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    return pidfile


def detect_external_status_change():
    """Detect if status-right was changed externally (by theme plugins)."""
    current_sr = tmux_cmd("show-option", "-gv", "status-right")
    if "inject-status" not in current_sr and "inject_status" not in current_sr:
        tmux_cmd("set", "-g", "@ccm-orig-status-right", current_sr)
        orig_len = tmux_cmd("show-option", "-gv", "status-right-length")
        tmux_cmd("set", "-g", "@ccm-orig-sr-length", orig_len)
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
    pidfile = acquire_pidfile()
    if pidfile is None:
        return  # Another instance running

    try:
        _inject_status_impl()
    finally:
        try:
            os.unlink(pidfile)
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

    # Always update window name icons
    update_window_names(projects)

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
        # Mode 1: ccm-style window list in status-right
        _cleanup_extra_lines()
        all_projects = scan_active_windows(projects, include_all=True)

        # Hide standard window list
        _touch_mode2_marker()
        tmux_cmd("set", "-g", "window-status-format", "")
        tmux_cmd("set", "-g", "window-status-current-format", "")

        entries = build_detail_entries(all_projects)

        if not entries:
            new_status = f"#[fg=#666666]≡#[default] {original}{refresh}"
        else:
            detail = ""
            for i, entry in enumerate(entries):
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
        tmux_cmd("set", "-g", "status-right", main_status)

        _touch_mode2_marker()
        tmux_cmd("set", "-g", "window-status-format", "")
        tmux_cmd("set", "-g", "window-status-current-format", "")

        all_projects = scan_active_windows(projects, include_all=True)
        entries = build_detail_entries(all_projects, with_extras=True)

        if not entries:
            tmux_cmd("set", "-g", "status", "2")
            fmt = "#[align=right]#[fg=#666666,bg=#3a3a3a] ≡ ccm: no projects  "
            tmux_cmd("set", "-g", "status-format[1]", fmt)
        else:
            term_width = 120
            try:
                term_width = int(tmux_cmd("display-message", "-p", "#{client_width}") or "120")
            except ValueError:
                pass

            # Estimate entry width from first entry
            avg_width = 20
            if entries:
                sample = re.sub(r'#\[[^\]]*\]', '', entries[0])
                avg_width = len(sample) + 3
                if avg_width < 10:
                    avg_width = 10

            entries_per_line = max(1, (term_width - 5) // avg_width)
            num_lines = max(1, (len(entries) + entries_per_line - 1) // entries_per_line)

            tmux_cmd("set", "-g", "status", str(num_lines + 1))

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
                tmux_cmd("set", "-g", f"status-format[{line_idx + 1}]", fmt)

            # Clear extra lines
            for extra in range(num_lines + 1, 6):
                tmux_cmd("set", "-g", "-u", f"status-format[{extra}]")

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
    tmux_cmd("set", "-g", "status", "on")
    for n in range(1, 6):
        tmux_cmd("set", "-g", "-u", f"status-format[{n}]")


def _cleanup_mode02():
    marker = os.path.join(CCM_TMP_DIR, "mode2-active")
    if os.path.exists(marker):
        try:
            os.unlink(marker)
        except OSError:
            pass
        tmux_cmd("set", "-g", "-u", "window-status-format")
        tmux_cmd("set", "-g", "-u", "window-status-current-format")
        _cleanup_extra_lines()
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
        with open(cache_file, "w") as f:
            f.write(content)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        inject_status()
    except Exception:
        pass  # Never crash — tmux will retry
