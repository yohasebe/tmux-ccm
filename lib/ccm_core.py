#!/usr/bin/env python3
"""ccm core — shared constants, helpers, state detection, and project list building."""

import hashlib
import os
import re
import subprocess
import time

# ─── Constants ───

CCM_ROOT = os.environ.get(
    "CCM_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CCM_TMP_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"ccm-{os.getuid()}")
CCM_HOOK_DIR = os.path.join(CCM_TMP_DIR, "hooks")
CCM_SNAPSHOT_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "ccm",
    "snapshots",
)
CCM_GIT_CACHE_DIR = os.path.join(CCM_TMP_DIR, "git-cache")
CCM_PORT_CACHE_DIR = os.path.join(CCM_TMP_DIR, "port-cache")

DONE_TIMEOUT = 30
HOOK_TIMEOUT = 300
IDLE_EXIT_TIMEOUT = 300  # 5 minutes default

PATTERN_PERMIT = re.compile(
    r"(Do you want to|Allow .+ to|yes.*no|y/n|Would you like|Esc to cancel)",
    re.IGNORECASE,
)
PATTERN_INPUT_PROMPT = re.compile(r"^❯\s")
# Accept-edits prompt: ❯❯ or ⏵⏵ (Claude Code may use either, with optional leading spaces)
PATTERN_ACCEPT_EDITS = re.compile(r"^\s*[❯⏵]{2}")

CLAUDE_PROCESS_NAME = "claude"
CLAUDE_CMD = "claude --continue 2>/dev/null || claude"

STATE_PRIORITY = {"PERMIT": 0, "DONE": 1, "BUSY": 2, "IDLE": 3, "SHELL": 4, "DOWN": 5}
STATE_ICONS = {
    "PERMIT": "⚠", "BUSY": "◉", "DONE": "✔", "IDLE": "●", "SHELL": "■", "DOWN": "○",
}


# ─── Subprocess helpers ───

def tmux_cmd(*args, timeout=5):
    """Run tmux command, return stdout."""
    try:
        r = subprocess.run(
            ["tmux"] + list(args), capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def tmux_batch(*commands):
    """Run multiple tmux commands in a single subprocess call.
    Each command is a tuple of args. Commands are joined with ';' separator.
    """
    if not commands:
        return
    args = ["tmux"]
    for i, cmd in enumerate(commands):
        if i > 0:
            args.append(";")
        args.extend(cmd)
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def ps_snapshot():
    """Single ps call for scan cycle."""
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,ppid,pgid,comm"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def md5_hash(s):
    return hashlib.md5(s.encode()).hexdigest()


# ─── Session detection ───

def get_session():
    popup_file = os.path.join(CCM_TMP_DIR, "popup-session")
    try:
        if os.path.exists(popup_file):
            age = time.time() - os.path.getmtime(popup_file)
            if age < 60:
                with open(popup_file) as f:
                    return f.read().strip()
    except OSError:
        pass
    session = tmux_cmd("display-message", "-p", "#{session_name}")
    if session:
        return session
    out = tmux_cmd("list-clients", "-F", "#{session_name}")
    return out.split("\n")[0] if out else ""


def touch_popup_session():
    path = os.path.join(CCM_TMP_DIR, "popup-session")
    try:
        os.utime(path, None)
    except OSError:
        pass


# ─── Hook signal ───

def read_hook_signal(project_dir):
    """Read hook signal file. Returns (timestamp, state) or None."""
    expanded = os.path.expanduser(project_dir.replace("~", os.path.expanduser("~")))
    try:
        expanded = os.path.realpath(expanded)
    except OSError:
        pass
    cache_key = md5_hash(expanded)
    hook_file = os.path.join(CCM_HOOK_DIR, cache_key)
    try:
        with open(hook_file) as f:
            content = f.read().strip()
        parts = content.split(" ", 1)
        if len(parts) == 2:
            return int(parts[0]), parts[1]
    except (OSError, ValueError):
        pass
    return None


# ─── State detection ───

def find_claude_pid(parent_pid, ps_lines):
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == str(parent_pid) and parts[3] == CLAUDE_PROCESS_NAME:
            return parts[0]
    return None


def has_children(pid, ps_lines, own_pgid):
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == str(pid):
            if parts[2] == str(own_pgid):
                continue
            if parts[3] == "caffeinate":
                continue
            return True
    return False


def capture_pane_bottom(pane_target, lines=8):
    """Capture bottom non-empty lines of a pane."""
    raw = tmux_cmd("capture-pane", "-t", pane_target, "-p", "-S", "-10")
    if not raw:
        return []
    non_empty = [l for l in raw.split("\n") if l.strip()]
    return non_empty[-lines:]


def detect_pane_state(pane_pid, pane_target, ps_lines, own_pgid):
    claude_pid = find_claude_pid(pane_pid, ps_lines)
    if not claude_pid:
        return "SHELL"

    if has_children(claude_pid, ps_lines, own_pgid):
        bottom = capture_pane_bottom(pane_target)
        for line in bottom:
            if PATTERN_PERMIT.search(line):
                return "PERMIT"
        for line in bottom:
            if PATTERN_INPUT_PROMPT.match(line) and not PATTERN_ACCEPT_EDITS.match(line):
                return "IDLE"
        return "BUSY"

    return "IDLE"


def detect_window_raw(win_target, panes_cache, ps_lines, own_pgid):
    panes = [
        (pid, pane_id)
        for wt, pid, pane_id in panes_cache
        if wt == win_target
    ]
    if not panes:
        return "DOWN"

    best = "SHELL"
    for pid, pane_id in panes:
        state = detect_pane_state(pid, pane_id, ps_lines, own_pgid)
        if state == "PERMIT":
            return "PERMIT"
        elif state == "BUSY":
            best = "BUSY"
        elif state == "IDLE" and best != "BUSY":
            best = "IDLE"
    return best


def _set_win_state(win_target, state, done=None, last_done=None, unset_done=False):
    """Batch-write window state options in a single tmux call."""
    cmds = [("set-option", "-wt", win_target, "@ccm_prev_state", state)]
    if unset_done:
        cmds.append(("set-option", "-wt", win_target, "-u", "@ccm_done"))
    elif done is not None:
        cmds.append(("set-option", "-wt", win_target, "@ccm_done", str(done)))
    if last_done is not None:
        cmds.append(("set-option", "-wt", win_target, "@ccm_last_done", str(last_done)))
    tmux_batch(*cmds)


def detect_window_state(win_target, project_dir, prev_state, done_flag, last_done_ts,
                        panes_cache, ps_lines, own_pgid):
    """Full detection pipeline. Returns (state, new_done_flag, new_last_done)."""
    now = int(time.time())
    raw = detect_window_raw(win_target, panes_cache, ps_lines, own_pgid)

    if raw in ("SHELL", "DOWN"):
        _set_win_state(win_target, raw, unset_done=True)
        return raw, "", last_done_ts

    # Hook-based enhancement
    if project_dir:
        hook = read_hook_signal(project_dir)
        if hook:
            hook_ts, hook_state = hook
            hook_age = now - hook_ts

            if raw == "IDLE":
                # PERMIT signal from Notification hook (most reliable)
                if hook_state == "PERMIT" and hook_age < HOOK_TIMEOUT:
                    _set_win_state(win_target, "PERMIT")
                    return "PERMIT", done_flag, last_done_ts

                if hook_state == "BUSY" and hook_age < HOOK_TIMEOUT:
                    # PERMIT is now detected by Notification hook (writes PERMIT signal)
                    # No capture-pane fallback needed — avoids false positives from
                    # Claude Code UI text (e.g., "pre-approve" in tips)
                    _set_win_state(win_target, "BUSY")
                    return "BUSY", done_flag, last_done_ts

                if hook_state == "DONE" and hook_age < DONE_TIMEOUT:
                    bottom = capture_pane_bottom(win_target)
                    prompt_visible = any(PATTERN_INPUT_PROMPT.match(l) for l in bottom)
                    if not prompt_visible:
                        _set_win_state(win_target, "BUSY")
                        return "BUSY", done_flag, last_done_ts
                    _set_win_state(win_target, "DONE", done=hook_ts, last_done=hook_ts)
                    return "DONE", str(hook_ts), hook_ts

    # Fallback: transition-based DONE tracking
    if raw == "IDLE":
        if prev_state in ("BUSY", "PERMIT"):
            _set_win_state(win_target, "DONE", done=now, last_done=now)
            return "DONE", str(now), now

        if done_flag:
            try:
                done_age = now - int(done_flag)
                if 0 <= done_age < DONE_TIMEOUT:
                    _set_win_state(win_target, "DONE")
                    return "DONE", done_flag, last_done_ts
            except ValueError:
                pass
            _set_win_state(win_target, raw, unset_done=True)

    elif raw != "IDLE":
        _set_win_state(win_target, raw, unset_done=True)

    # PERMIT check: always scan for permission prompts when raw=IDLE
    # (PERMIT requires user action and must not be missed)
    # Note: the old "safety net" (no prompt → BUSY) has been removed because
    # it caused frequent false BUSY from ambiguous screen content. We now trust
    # the process tree (raw state) + hook signals as the primary detection.
    if raw == "IDLE":
        bottom = capture_pane_bottom(win_target)
        permit_visible = any(PATTERN_PERMIT.search(l) for l in bottom)
        if permit_visible:
            _set_win_state(win_target, "PERMIT")
            return "PERMIT", done_flag, last_done_ts

    _set_win_state(win_target, raw)
    return raw, done_flag, last_done_ts


# ─── Project data ───

class Project:
    __slots__ = (
        "win_target", "win_idx", "name", "dir", "state",
        "branch", "ports", "last_done_ts", "sort_key", "tagged",
    )

    def __init__(self, win_target, win_idx, name, directory, state,
                 branch="", ports="", last_done_ts=0, tagged=True):
        self.win_target = win_target
        self.win_idx = win_idx
        self.name = name
        self.dir = directory
        self.state = state
        self.branch = branch
        self.ports = ports
        self.last_done_ts = last_done_ts
        self.tagged = tagged
        self.sort_key = (STATE_PRIORITY.get(state, 5), -(last_done_ts or 0))


def read_cache_file(cache_dir, directory):
    expanded = os.path.expanduser(directory.replace("~", os.path.expanduser("~")))
    try:
        expanded = os.path.realpath(expanded)
    except OSError:
        pass
    key = md5_hash(expanded)
    path = os.path.join(cache_dir, key)
    try:
        if os.path.exists(path):
            age = time.time() - os.path.getmtime(path)
            if age < 30:
                with open(path) as f:
                    return f.read().strip()
    except OSError:
        pass
    return ""


def build_project_list(fast=False):
    """Build project list from tmux. If fast, skip git/port refresh."""
    raw = tmux_cmd(
        "list-windows", "-a", "-F",
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_dir}\t"
        "#{@ccm_prev_state}\t#{@ccm_done}\t#{@ccm_last_done}\t#{window_activity}"
    )
    if not raw:
        return []

    ps_lines = ps_snapshot().strip().split("\n") if not fast else []
    panes_cache = []
    if not fast:
        panes_raw = tmux_cmd("list-panes", "-a", "-F",
                             "#{session_name}:#{window_index}\t#{pane_pid}\t#{pane_id}")
        for line in panes_raw.split("\n"):
            parts = line.split("\t")
            if len(parts) == 3:
                panes_cache.append((parts[0], parts[1], parts[2]))

    own_pgid = str(os.getpgrp())
    seen_dirs = set()
    projects = []

    for line in raw.split("\n"):
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        win_target, project, proj_dir = parts[0], parts[1], parts[2]
        prev_state, done_flag, last_done_str, win_activity_str = (
            parts[3], parts[4], parts[5], parts[6]
        )

        if not project:
            continue

        try:
            resolved = os.path.realpath(
                os.path.expanduser(proj_dir.replace("~", os.path.expanduser("~")))
            )
        except OSError:
            resolved = proj_dir
        if resolved in seen_dirs:
            continue
        seen_dirs.add(resolved)

        win_idx = win_target.split(":")[-1]

        last_done_ts = 0
        if last_done_str and last_done_str != "0":
            try:
                last_done_ts = int(last_done_str)
            except ValueError:
                pass

        win_activity = 0
        if win_activity_str:
            try:
                win_activity = int(win_activity_str)
            except ValueError:
                pass

        if fast:
            state = prev_state if prev_state else "IDLE"
            if state == "IDLE" and done_flag:
                try:
                    done_age = int(time.time()) - int(done_flag)
                    if 0 <= done_age < DONE_TIMEOUT:
                        state = "DONE"
                except ValueError:
                    pass
            if proj_dir:
                hook = read_hook_signal(proj_dir)
                if hook:
                    hook_ts, hook_state = hook
                    hook_age = int(time.time()) - hook_ts
                    if hook_state == "BUSY" and hook_age < HOOK_TIMEOUT and state != "PERMIT":
                        state = "BUSY"
                    elif hook_state == "DONE" and hook_age < DONE_TIMEOUT and state == "IDLE":
                        state = "DONE"
        else:
            state, done_flag, last_done_ts_new = detect_window_state(
                win_target, proj_dir, prev_state, done_flag, last_done_ts,
                panes_cache, ps_lines, own_pgid
            )
            if last_done_ts_new:
                last_done_ts = last_done_ts_new if isinstance(last_done_ts_new, int) else last_done_ts

        sort_ts = max(last_done_ts, win_activity) if win_activity else last_done_ts

        branch = read_cache_file(CCM_GIT_CACHE_DIR, proj_dir) if proj_dir else ""
        ports = read_cache_file(CCM_PORT_CACHE_DIR, proj_dir) if proj_dir else ""

        projects.append(Project(
            win_target=win_target, win_idx=win_idx, name=project,
            directory=proj_dir, state=state, branch=branch, ports=ports,
            last_done_ts=sort_ts, tagged=True,
        ))

    projects.sort(key=lambda p: p.sort_key)
    return projects


# ─── Formatting helpers ───

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
    if len(d) <= avail:
        return d
    base = os.path.basename(d)
    parent = os.path.basename(os.path.dirname(d))
    short = f"…/{parent}/{base}"
    if len(short) <= avail:
        return short
    if len(base) <= avail:
        return base
    return ""


def hooks_configured():
    settings_file = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings_file) as f:
            content = f.read()
        return ("on-prompt-submit.sh" in content
                and "on-stop.sh" in content
                and "on-pre-tool-use.sh" in content
                and "on-notification.sh" in content)
    except OSError:
        return False


# ─── Desktop notifications ───

def notify(state, project):
    """Send desktop notification for state changes.
    Controlled by @ccm-notify tmux option: off, permit, done, permit,done, all.
    """
    setting = tmux_cmd("show-option", "-gqv", "@ccm-notify") or "off"
    if setting == "off":
        return

    state_lower = state.lower()
    if setting != "all" and state_lower not in setting:
        return

    sound_setting = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound") or "on"

    messages = {
        "PERMIT": (f"ccm ⚠ {project}",
                   "Action required — switch to this project and respond to the permission prompt",
                   "Basso" if sound_setting == "on" else ""),
        "DONE":   (f"ccm ✔ {project}",
                   "Claude has finished responding — review the output when ready",
                   ""),
        "BUSY":   (f"ccm ◉ {project}",
                   "Claude is now processing your request",
                   ""),
        "IDLE":   (f"ccm {project}",
                   "Waiting for your input",
                   ""),
    }

    if state not in messages:
        return

    title, body, sound = messages[state]
    try:
        sound_opt = f' sound name "{sound}"' if sound else ""
        cmd = f'display notification "{body}" with title "{title}"{sound_opt}'
        subprocess.Popen(["osascript", "-e", cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        try:
            subprocess.Popen(["notify-send", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass


# ─── Window name update ───

def update_window_names(projects):
    """Update tmux window names with state icons."""
    all_windows = tmux_cmd(
        "list-windows", "-a", "-F",
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{window_name}"
    )
    if not all_windows:
        return

    project_states = {p.win_target: p.state for p in projects}

    for line in all_windows.split("\n"):
        parts = line.split("\t")
        if len(parts) < 3 or not parts[1]:
            continue
        win_target, project, current_name = parts[0], parts[1], parts[2]
        state = project_states.get(win_target, "IDLE")
        icon = STATE_ICONS.get(state, "●")
        new_name = f"{icon} {project}"
        if current_name != new_name:
            tmux_cmd("rename-window", "-t", win_target, new_name)


# ─── Auto-exit idle sessions ───

def auto_exit_idle(projects):
    """Exit idle Claude Code sessions to free resources."""
    idle_timeout_str = tmux_cmd("show-option", "-gqv", "@ccm-idle-timeout")
    if idle_timeout_str:
        try:
            idle_timeout = int(idle_timeout_str) * 60
        except ValueError:
            idle_timeout = IDLE_EXIT_TIMEOUT
    else:
        idle_timeout = IDLE_EXIT_TIMEOUT

    if idle_timeout <= 0:
        return

    now = int(time.time())

    # Get current window to exclude
    current_session = tmux_cmd("display-message", "-p", "#{session_name}")
    current_win = tmux_cmd("display-message", "-p", "#{window_index}")
    current_target = f"{current_session}:{current_win}"

    # Get window activity for all windows
    activity_raw = tmux_cmd(
        "list-windows", "-a", "-F",
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_prev_state}\t#{@ccm_last_done}\t#{window_activity}"
    )
    if not activity_raw:
        return

    for line in activity_raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 5:
            parts.append("")
        win_target, project, prev_state, last_done_str, win_activity_str = parts[:5]

        if not project or prev_state != "IDLE":
            continue
        if win_target == current_target:
            continue

        # Parse timestamps
        last_done = 0
        if last_done_str and last_done_str != "0":
            try:
                last_done = int(last_done_str)
            except ValueError:
                pass

        win_activity = 0
        if win_activity_str and win_activity_str != "0":
            try:
                win_activity = int(win_activity_str)
            except ValueError:
                pass

        idle_since = max(last_done, win_activity)

        if idle_since == 0:
            tmux_cmd("set-option", "-wt", win_target, "@ccm_last_done", str(now))
            continue

        idle_duration = now - idle_since
        if idle_duration >= idle_timeout:
            # Cancel any partial input, then cleanly exit Claude Code
            tmux_cmd("send-keys", "-t", win_target, "Escape")
            time.sleep(0.1)
            tmux_cmd("send-keys", "-t", win_target, "/exit", "Enter")
            time.sleep(0.5)
            # Clear the pane so auto-restart shows a clean screen
            tmux_cmd("send-keys", "-t", win_target, "clear", "Enter")
            _set_win_state(win_target, "SHELL", unset_done=True)


# ─── Autosave ───

def periodic_autosave():
    """Save autosave snapshot if 5 minutes have passed."""
    marker = os.path.join(CCM_TMP_DIR, "autosave-time")
    now = int(time.time())
    last_save = 0
    try:
        if os.path.exists(marker):
            with open(marker) as f:
                last_save = int(f.read().strip())
    except (OSError, ValueError):
        pass

    if now - last_save < 300:
        return

    # Check if any ccm projects exist
    check = tmux_cmd("list-windows", "-a", "-F", "#{@ccm_project}")
    has_projects = any(line.strip() for line in check.split("\n") if line.strip())
    if not has_projects:
        return

    ccm_bin = os.path.join(CCM_ROOT, "ccm")
    try:
        subprocess.run([ccm_bin, "snapshot", "save", "_autosave"],
                       capture_output=True, timeout=10)
        with open(marker, "w") as f:
            f.write(str(now))
    except (subprocess.TimeoutExpired, OSError):
        pass


# ─── CLI output helpers ───

# ANSI color codes for terminal output
_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_STATE = {
    "PERMIT": "\033[1;33m",      # bold yellow
    "BUSY": "\033[38;5;209m",    # salmon
    "DONE": "\033[0;32m",        # green
    "IDLE": "\033[0;34m",        # blue
    "SHELL": "\033[38;5;245m",   # gray
    "DOWN": "\033[2m",           # dim
}


def print_status():
    """Print status of all ccm projects (for `ccm status` CLI command)."""
    projects = build_project_list(fast=False)

    if not projects:
        print("No active projects.")
        return

    # Hooks status
    if hooks_configured():
        print(f"{_C_DIM}Hooks: ON{_C_RESET}")
    else:
        print(f"{_C_DIM}Hooks: OFF (run 'ccm setup-hooks' for improved detection){_C_RESET}")
    print()

    # Header
    print(f"{_C_BOLD}{'STATUS':<12} {'PROJECT':<20} {'BRANCH':<16} {'PORTS':<12} {'DIRECTORY'}{_C_RESET}")
    print(f"{'------':<12} {'-------':<20} {'------':<16} {'-----':<12} {'---------'}")

    for p in projects:
        color = _C_STATE.get(p.state, _C_DIM)
        icon = STATE_ICONS.get(p.state, "?")
        status = f"{color}{icon} {p.state}{_C_RESET}"
        branch = p.branch or "-"
        ports = p.ports or "-"
        d = p.dir.replace(os.path.expanduser("~"), "~") if p.dir else ""
        # Status field with ANSI codes is wider than visible, compensate
        print(f"{status:<22} {p.name:<20} {branch:<16} {ports:<12} {d}")


# ─── CLI entry point ───

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        print_status()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
