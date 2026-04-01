#!/usr/bin/env python3
"""ccm core — shared constants, helpers, state detection, and project list building."""

import glob as _glob_mod
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
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

DONE_TIMEOUT = int(os.environ.get("CCM_DONE_TIMEOUT", "30"))
HOOK_TIMEOUT = int(os.environ.get("CCM_HOOK_TIMEOUT", "300"))
IDLE_EXIT_TIMEOUT = int(os.environ.get("CCM_IDLE_EXIT_TIMEOUT", "300"))  # 5 minutes default
CACHE_TTL = int(os.environ.get("CCM_CACHE_TTL", "30"))  # git/port cache seconds

# ─── Claude Code UI patterns (update when Claude Code UI changes) ───
# These are the ONLY place where Claude Code's terminal output is matched.
# If detection breaks after a Claude Code update, check these first.
# See: https://github.com/anthropics/claude-code

# Input prompt characters (single character followed by space = idle prompt)
_PROMPT_CHARS = "❯"
# Accept-edits prompt characters (doubled = accept-edits mode)
_ACCEPT_CHARS = "❯⏵"
PATTERN_INPUT_PROMPT = re.compile(rf"^[{_PROMPT_CHARS}]\s")
PATTERN_ACCEPT_EDITS = re.compile(rf"^\s*[{_ACCEPT_CHARS}]{{2}}")

# Claude Code process name in `ps` output
CLAUDE_PROCESS_NAME = "claude"
# Processes that are always children of Claude Code and should be ignored
# when checking for meaningful child processes (tool execution).
IGNORED_CHILDREN = {"caffeinate"}

CLAUDE_CMD = "claude --continue 2>/dev/null || claude"

# Hook script filenames (single source of truth for hooks_configured checks)
HOOK_SCRIPTS = [
    "on-prompt-submit.sh",
    "on-stop.sh",
    "on-pre-tool-use.sh",
    "on-notification.sh",
    "on-permission-request.sh",
    "on-session-end.sh",
]

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
# Signal file format: "<unix_timestamp> <STATE> [extra_fields...]"
# - Fields are space-separated; first two are required
# - STATE: one of BUSY, DONE, PERMIT
# - Extra fields are reserved for future use and ignored by current code
# Written by: hooks/on-prompt-submit.sh, hooks/on-pre-tool-use.sh,
#             hooks/on-stop.sh, hooks/on-notification.sh

VALID_HOOK_STATES = {"BUSY", "DONE", "PERMIT", "SHELL"}


def _resolve_project_dir(project_dir):
    """Expand and resolve a project directory path."""
    expanded = os.path.expanduser(project_dir)
    try:
        expanded = os.path.realpath(expanded)
    except OSError:
        pass
    return expanded


def _hook_signal_path(project_dir):
    """Get the hook signal file path for a project directory."""
    expanded = _resolve_project_dir(project_dir)
    return os.path.join(CCM_HOOK_DIR, md5_hash(expanded))


def read_hook_signal(project_dir):
    """Read hook signal file. Returns (timestamp, state, detail) or None.
    Detail is optional extra info (e.g., tool name for PERMIT).
    """
    hook_file = _hook_signal_path(project_dir)
    try:
        with open(hook_file) as f:
            content = f.read().strip()
        parts = content.split(None, 2)  # split into at most 3 parts
        if len(parts) >= 2 and parts[1] in VALID_HOOK_STATES:
            detail = parts[2] if len(parts) >= 3 else ""
            return int(parts[0]), parts[1], detail
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
            if parts[3] in IGNORED_CHILDREN:
                continue
            return True
    return False


def capture_pane_bottom(pane_target, lines=8):
    """Capture bottom non-empty lines of a pane.
    Handles alternate screen mode (CLAUDE_CODE_NO_FLICKER=1) by trying
    normal capture first, then falling back to alternate screen capture.
    """
    raw = tmux_cmd("capture-pane", "-t", pane_target, "-p", "-S", "-10")
    if not raw or not raw.strip():
        # Try alternate screen (used when CLAUDE_CODE_NO_FLICKER=1)
        raw = tmux_cmd("capture-pane", "-a", "-t", pane_target, "-p", "-S", "-10")
    if not raw:
        return []
    non_empty = [l for l in raw.split("\n") if l.strip()]
    return non_empty[-lines:]


def detect_pane_state(pane_pid, pane_target, ps_lines, own_pgid):
    claude_pid = find_claude_pid(pane_pid, ps_lines)
    if not claude_pid:
        return "SHELL"

    if has_children(claude_pid, ps_lines, own_pgid):
        # PERMIT is detected by Notification hook (not capture-pane text matching)
        # Check if input prompt visible → background workers, not tool execution
        bottom = capture_pane_bottom(pane_target)
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

    if raw == "DOWN":
        _set_win_state(win_target, raw, unset_done=True)
        return raw, "", last_done_ts

    if raw == "SHELL":
        _set_win_state(win_target, raw, unset_done=True)
        return raw, "", last_done_ts

    # Hook-based enhancement (single read_hook_signal call per cycle)
    if project_dir:
        hook = read_hook_signal(project_dir)

        # Note: SHELL hook signal (from SessionEnd) is NOT used here.
        # Process tree is authoritative for SHELL detection (raw == "SHELL" above).
        # Trusting SHELL signal when raw=IDLE causes false SHELL after Claude restarts
        # (stale signal persists until next UserPromptSubmit overwrites it).

        if hook and hook[1] != "SHELL":
            hook_ts, hook_state, _hook_detail = hook
            hook_age = now - hook_ts

            if raw == "IDLE":
                # PERMIT signal from PermissionRequest/Notification hook.
                # No timeout — permission prompts can persist indefinitely.
                # Only cleared when user responds (window_activity > hook_ts)
                # or a newer BUSY/DONE signal overwrites it.
                if hook_state == "PERMIT":
                    # Check if user has responded since PERMIT was set
                    # (window_activity > hook timestamp = user pressed a key)
                    win_act = tmux_cmd("display-message", "-t", win_target, "-p", "#{window_activity}")
                    try:
                        if win_act and int(win_act) > hook_ts:
                            _set_win_state(win_target, "BUSY")
                            return "BUSY", done_flag, last_done_ts
                    except ValueError:
                        pass
                    _set_win_state(win_target, "PERMIT")
                    return "PERMIT", done_flag, last_done_ts

                if hook_state == "BUSY" and hook_age < HOOK_TIMEOUT:
                    # PERMIT is now detected by Notification hook (writes PERMIT signal)
                    # No capture-pane fallback needed — avoids false positives from
                    # Claude Code UI text (e.g., "pre-approve" in tips)
                    _set_win_state(win_target, "BUSY")
                    return "BUSY", done_flag, last_done_ts

                if hook_state == "DONE" and hook_age < DONE_TIMEOUT:
                    # Trust the DONE hook signal — no capture-pane verification needed.
                    # The Notification(idle_prompt) also writes DONE, providing redundancy.
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
    # PERMIT is detected exclusively by Notification(permission_prompt) hook.
    # No capture-pane text matching — avoids false positives from UI text.

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
    expanded = os.path.expanduser(directory)
    try:
        expanded = os.path.realpath(expanded)
    except OSError:
        pass
    key = md5_hash(expanded)
    path = os.path.join(cache_dir, key)
    try:
        if os.path.exists(path):
            age = time.time() - os.path.getmtime(path)
            if age < CACHE_TTL:
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
            resolved = os.path.realpath(os.path.expanduser(proj_dir))
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
                    hook_ts, hook_state, _hook_detail = hook
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
        return all(script in content for script in HOOK_SCRIPTS)
    except OSError:
        return False


def save_tmux_conf_setting(setting):
    """Persist a tmux setting to ~/.tmux.conf (before ccm/TPM load lines).
    setting: e.g., 'set -g @ccm-auto-restore on'
    """
    conf = os.path.expanduser("~/.tmux.conf")
    parts = setting.split()
    if len(parts) < 3:
        return
    key = parts[2]  # e.g., "@ccm-auto-restore"

    try:
        lines = []
        if os.path.exists(conf):
            with open(conf) as f:
                lines = f.readlines()

        # Remove existing lines with this key
        lines = [l for l in lines if key not in l]

        # Find earliest ccm/TPM load line
        insert_at = len(lines)
        for i, l in enumerate(lines):
            if "source-file" in l and "ccm" in l:
                insert_at = min(insert_at, i)
            if l.strip().startswith("run") and "tpm" in l.lower():
                insert_at = min(insert_at, i)

        lines.insert(insert_at, setting + "\n")

        with open(conf, "w") as f:
            f.writelines(lines)
    except OSError:
        pass


# ─── Desktop notifications ───

def notify(state, project, detail=""):
    """Send desktop notification for state changes.
    Controlled by @ccm-notify tmux option: off, permit, done, permit,done, all.
    detail: optional context (e.g., "Bash: rm -rf ..." for PERMIT).
    """
    setting = tmux_cmd("show-option", "-gqv", "@ccm-notify") or "permit,done"
    if setting == "off":
        return

    state_lower = state.lower()
    if setting != "all" and state_lower not in setting:
        return

    sound_setting = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound") or "off"

    permit_body = f"Permission required: {detail}" if detail else \
                  "Action required — respond to the permission prompt"
    messages = {
        "PERMIT": (f"ccm ⚠ {project}",
                   permit_body,
                   "Glass" if sound_setting == "on" else ""),
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
        # Escape double quotes and backslashes for AppleScript string literals
        esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
        esc_body = body.replace("\\", "\\\\").replace('"', '\\"')
        sound_opt = ""
        if sound:
            esc_sound = sound.replace("\\", "\\\\").replace('"', '\\"')
            sound_opt = f' sound name "{esc_sound}"'
        cmd = f'display notification "{esc_body}" with title "{esc_title}"{sound_opt}'
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
            # Force autosave after auto-exit to preserve project in snapshot
            _force_autosave()


# ─── Autosave ───

def _force_autosave():
    """Force an immediate autosave."""
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        pass


def periodic_autosave():
    """Save autosave snapshot if 2 minutes have passed."""
    marker = os.path.join(CCM_TMP_DIR, "autosave-time")
    now = int(time.time())
    last_save = 0
    try:
        if os.path.exists(marker):
            with open(marker) as f:
                last_save = int(f.read().strip())
    except (OSError, ValueError):
        pass

    if now - last_save < 120:
        return

    # Check if any ccm projects exist
    check = tmux_cmd("list-windows", "-a", "-F", "#{@ccm_project}")
    has_projects = any(line.strip() for line in check.split("\n") if line.strip())
    if not has_projects:
        return

    try:
        cmd_snapshot_save("_autosave", quiet=True)
        with open(marker, "w") as f:
            f.write(str(now))
    except Exception:
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


def print_ports():
    """Print listening ports per project (for `ccm ports` CLI command)."""
    projects = build_project_list(fast=True)
    if not projects:
        print("No active projects.")
        return

    print(f"{_C_BOLD}{'PROJECT':<20} {'PORTS':<16} {'DIRECTORY'}{_C_RESET}")
    print(f"{'-------':<20} {'-----':<16} {'---------'}")

    for p in projects:
        ports = p.ports or "-"
        d = p.dir.replace(os.path.expanduser("~"), "~") if p.dir else ""
        print(f"{p.name:<20} {ports:<16} {d}")


def print_tree():
    """Print hierarchical tree of all sessions/windows/panes (for `ccm tree`)."""
    sessions_raw = tmux_cmd("list-sessions", "-F", "#{session_name}")
    if not sessions_raw:
        print("No tmux sessions.")
        return

    sessions = sorted(sessions_raw.split("\n"))
    current_session = get_session()

    # Build project state lookup
    projects = build_project_list(fast=True)
    project_map = {p.win_target: p for p in projects}

    for si, sess in enumerate(sessions):
        is_last_s = si == len(sessions) - 1
        s_pre = "└── " if is_last_s else "├── "
        s_cont = "    " if is_last_s else "│   "
        marker = " ◀" if sess == current_session else ""
        print(f"{s_pre}{_C_BOLD}{sess}{_C_RESET}{marker}")

        windows_raw = tmux_cmd("list-windows", "-t", sess, "-F",
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
                color = _C_STATE.get(proj.state, _C_DIM)
                icon = STATE_ICONS.get(proj.state, "?")
                name = proj.name
                extra = ""
                if proj.branch:
                    extra += f" ({proj.branch})"
                if proj.ports:
                    extra += f" [:{proj.ports}]"
            else:
                color = _C_DIM
                icon = ""
                name = win_name
                extra = ""

            d = ""
            if wdir:
                d = f" {wdir.replace(os.path.expanduser('~'), '~')}"
            elif not project:
                pane_path = tmux_cmd("display-message", "-t", win_target, "-p", "#{pane_current_path}")
                if pane_path:
                    d = f" {pane_path.replace(os.path.expanduser('~'), '~')}"

            icon_str = f"{color}{icon}{_C_RESET} " if icon else ""
            print(f"{w_pre}{icon_str}{name}{extra}{_C_DIM}{d}{_C_RESET}")


def print_statusline():
    """Print one-line status for tmux status bar (for `ccm statusline`)."""
    projects = build_project_list(fast=True)
    active = [p for p in projects if p.state in ("BUSY", "PERMIT", "DONE")]
    if not active:
        return

    parts = []
    for p in active:
        icon = STATE_ICONS.get(p.state, "?")
        parts.append(f"{p.name}:{icon}")

    print(f"| {' '.join(parts)} |")


# ─── CLI helpers ───

_C_RED = "\033[0;31m"
_C_GREEN = "\033[0;32m"
_C_YELLOW = "\033[1;33m"


def ccm_die(msg):
    """Print error message and exit."""
    print(f"{_C_RED}Error: {msg}{_C_RESET}", file=sys.stderr)
    sys.exit(1)


def ccm_warn(msg):
    """Print warning message."""
    print(f"{_C_YELLOW}Warning: {msg}{_C_RESET}", file=sys.stderr)


def ccm_info(msg):
    """Print info message."""
    print(f"{_C_GREEN}{msg}{_C_RESET}")


def validate_name(name):
    """Sanitize project name. Returns cleaned name or empty string."""
    if not name:
        return ""
    # Strip whitespace
    name = name.strip()
    # Replace whitespace with hyphens
    name = re.sub(r'\s+', '-', name)
    # Remove shell-dangerous characters
    name = re.sub(r"['\"`$\\;&|<>()]", '', name)
    # Collapse and strip hyphens
    name = re.sub(r'-+', '-', name).strip('-')
    return name


def find_window(session, name):
    """Find window index by project name. Returns index string or None."""
    raw = tmux_cmd("list-windows", "-t", session, "-F",
                   "#{window_index}\t#{@ccm_project}")
    if not raw:
        return None
    for line in raw.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    return None


def project_exists(session, name):
    """Check if project name already exists in session."""
    return find_window(session, name) is not None


def list_windows_raw(session):
    """List ccm-managed windows. Returns list of (idx, name, project, dir)."""
    raw = tmux_cmd("list-windows", "-t", session, "-F",
                   "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}")
    if not raw:
        return []
    result = []
    for line in raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        if parts[2]:  # has @ccm_project tag
            result.append(tuple(parts[:4]))
    return result


def clipboard_copy(text):
    """Copy text to system clipboard. Returns True on success."""
    for cmd in [["pbcopy"], ["clip.exe"], ["xclip", "-selection", "clipboard"], ["xsel", "-b"]]:
        try:
            subprocess.run(cmd, input=text, text=True, timeout=5,
                           capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False


def auto_start_claude(win_target):
    """Auto-start Claude Code if @ccm-auto-start is on."""
    setting = tmux_cmd("show-option", "-gqv", "@ccm-auto-start") or "on"
    if setting != "on":
        return
    tmux_cmd("send-keys", "-t", win_target, CLAUDE_CMD, "Enter")


def clear_done(win_target):
    """Clear DONE hook signal for a window."""
    proj_dir = tmux_cmd("show-option", "-wqv", "-t", win_target, "@ccm_dir")
    if not proj_dir:
        return
    resolved = _resolve_project_dir(proj_dir)
    hook_file = os.path.join(CCM_HOOK_DIR, md5_hash(resolved))
    try:
        if os.path.exists(hook_file):
            with open(hook_file) as f:
                if "DONE" in f.read():
                    os.unlink(hook_file)
    except OSError:
        pass
    tmux_cmd("set-option", "-wq", "-t", win_target, "@ccm_prev_state", "")


def init_dirs():
    """Create runtime directories."""
    for d in [CCM_SNAPSHOT_DIR, CCM_TMP_DIR, CCM_HOOK_DIR,
              CCM_GIT_CACHE_DIR, CCM_PORT_CACHE_DIR,
              os.path.join(os.path.expanduser("~/.local/share/ccm"), "state")]:
        os.makedirs(d, exist_ok=True)


def fzf_select(items, prompt="Select: "):
    """Run fzf for interactive selection. Returns selected item or None."""
    try:
        r = subprocess.run(["fzf", "--prompt", prompt, "--height=10"],
                           input="\n".join(items), capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    except FileNotFoundError:
        ccm_die("fzf not found (install with: brew install fzf)")


# ─── Snapshot commands ───


def _sanitize_snapshot_name(name):
    """Sanitize snapshot name to prevent path traversal."""
    # Strip path components — only keep the basename
    name = os.path.basename(name)
    # Remove any remaining dots that could cause issues (e.g., ".." left over)
    name = name.strip(".")
    if not name:
        ccm_die("Invalid snapshot name")
    return name


def cmd_snapshot_save(name="", quiet=False):
    """Save current projects as a snapshot."""
    if not name:
        try:
            name = input("Snapshot name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not name:
        ccm_die("Snapshot name is required")
    name = _sanitize_snapshot_name(name)

    init_dirs()

    # Scan ALL sessions for ccm-tagged windows
    raw = tmux_cmd("list-windows", "-a", "-F",
                   "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}")
    if not raw:
        if not quiet:
            ccm_die("No active projects to save")
        return

    projects_list = []
    for line in raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        project, proj_dir = parts[2], parts[3]
        if not project or not proj_dir:
            continue
        # Replace $HOME with ~ for portability
        proj_dir = proj_dir.replace(os.path.expanduser("~"), "~")
        projects_list.append({
            "name": project,
            "dir": proj_dir,
            "auto_start_claude": True,
        })

    if not projects_list:
        if not quiet:
            ccm_die("No active projects to save")
        return

    snapshot = {
        "version": 1,
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "projects": projects_list,
    }

    file_path = os.path.join(CCM_SNAPSHOT_DIR, f"{name}.json")
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, file_path)

    if not quiet:
        ccm_info(f"Snapshot saved: {name} ({file_path})")


def cmd_snapshot_load(name=""):
    """Load and restore a snapshot."""
    init_dirs()
    if not name:
        files = sorted(_glob_mod.glob(os.path.join(CCM_SNAPSHOT_DIR, "*.json")))
        if not files:
            ccm_die("No snapshots found")
        items = [os.path.splitext(os.path.basename(f))[0] for f in files]
        name = fzf_select(items, "Select snapshot: ")
        if not name:
            return

    name = _sanitize_snapshot_name(name)
    file_path = os.path.join(CCM_SNAPSHOT_DIR, f"{name}.json")
    if not os.path.exists(file_path):
        ccm_die(f"Snapshot not found: {name}")

    with open(file_path) as f:
        data = json.load(f)

    snap_projects = data.get("projects", [])
    print(f"Loading snapshot: {name} ({len(snap_projects)} projects)")

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    for proj in snap_projects:
        proj_name = proj.get("name", "")
        proj_dir = proj.get("dir", "")
        if not proj_name or proj_name == "null":
            continue
        if not proj_dir or proj_dir == "null":
            continue

        proj_dir = os.path.expanduser(proj_dir)
        try:
            proj_dir = os.path.realpath(proj_dir)
        except OSError:
            pass

        if project_exists(session, proj_name):
            ccm_warn(f"Project window already exists, skipping: {proj_name}")
            continue
        if not os.path.isdir(proj_dir):
            ccm_warn(f"Directory not found, skipping: {proj_name} ({proj_dir})")
            continue

        # Don't auto-start Claude on restore — saves resources
        cmd_add(proj_dir, proj_name, start_claude=False, _loading=True)

    # Save autosave after all projects loaded
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        ccm_warn("Failed to save autosave snapshot after load")

    ccm_info(f"Snapshot loaded: {name}")


def cmd_snapshot_list():
    """List available snapshots."""
    init_dirs()
    files = sorted(_glob_mod.glob(os.path.join(CCM_SNAPSHOT_DIR, "*.json")))
    if not files:
        print("No snapshots.")
        return

    print(f"{_C_BOLD}{'NAME':<20} {'CREATED':<24} {'PROJECTS'}{_C_RESET}")
    print(f"{'----':<20} {'-------':<24} {'--------'}")

    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            name = data.get("name", os.path.splitext(os.path.basename(fp))[0])
            created = data.get("created", "-")
            count = len(data.get("projects", []))
            print(f"{name:<20} {created:<24} {count}")
        except (json.JSONDecodeError, OSError):
            pass


def cmd_snapshot_delete(name=""):
    """Delete a snapshot."""
    init_dirs()
    if not name:
        files = sorted(_glob_mod.glob(os.path.join(CCM_SNAPSHOT_DIR, "*.json")))
        if not files:
            ccm_die("No snapshots found")
        items = [os.path.splitext(os.path.basename(f))[0] for f in files]
        name = fzf_select(items, "Delete snapshot: ")
        if not name:
            return

    name = _sanitize_snapshot_name(name)
    file_path = os.path.join(CCM_SNAPSHOT_DIR, f"{name}.json")
    if not os.path.exists(file_path):
        ccm_die(f"Snapshot not found: {name}")

    os.unlink(file_path)
    ccm_info(f"Snapshot deleted: {name}")


# ─── Session commands ───


def _autosave_trigger():
    """Trigger autosave in background (non-blocking)."""
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        pass


def cmd_add(directory, name="", start_claude=True, _loading=False):
    """Add a new ccm project window."""
    if not directory:
        ccm_die("Directory is required")

    directory = os.path.expanduser(directory)
    try:
        directory = os.path.realpath(directory)
    except OSError:
        pass

    if not os.path.isdir(directory):
        ccm_die(f"Directory does not exist: {directory}")

    if not name:
        name = os.path.basename(directory)
    name = validate_name(name)
    if not name:
        ccm_die("Invalid project name")

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    if project_exists(session, name):
        ccm_die(f"Project window already exists: {name}")

    # Check for duplicate directory
    for _idx, _wn, _proj, existing_dir in list_windows_raw(session):
        try:
            real_existing = os.path.realpath(os.path.expanduser(existing_dir))
        except OSError:
            real_existing = existing_dir
        if directory == real_existing:
            ccm_die(f"Directory already registered as project '{_proj}': {existing_dir}")

    # Create new window
    win_idx = tmux_cmd("new-window", "-P", "-F", "#{window_index}",
                       "-t", f"{session}:", "-n", name, "-c", directory)
    if not win_idx:
        ccm_die("Failed to create window")

    win_target = f"{session}:{win_idx}"

    # Tag the window with ccm metadata
    orig_name = tmux_cmd("display-message", "-t", win_target, "-p", "#{window_name}") or name
    tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_orig_name", orig_name),
        ("set-option", "-wt", win_target, "@ccm_project", name),
        ("set-option", "-wt", win_target, "@ccm_dir", directory),
        ("set-option", "-wt", win_target, "automatic-rename", "off"),
    )

    if start_claude:
        tmux_cmd("send-keys", "-t", win_target, CLAUDE_CMD, "Enter")

    ccm_info(f"Added project: {name} ({directory})")

    if not hooks_configured():
        ccm_warn("Hooks not installed. Run 'ccm setup-hooks' for accurate state detection.")

    if not _loading:
        _autosave_trigger()


def cmd_open(directory, name=""):
    """Start Claude in the current pane (for split-pane use)."""
    if not directory:
        ccm_die("Directory is required")

    directory = os.path.expanduser(directory)
    try:
        directory = os.path.realpath(directory)
    except OSError:
        pass

    if not os.path.isdir(directory):
        ccm_die(f"Directory does not exist: {directory}")

    if not name:
        name = os.path.basename(directory)

    # shlex.quote for safety
    tmux_cmd("send-keys",
             f"cd {shlex.quote(directory)} && (claude --continue 2>/dev/null || claude)",
             "Enter")


def cmd_register(source_target, new_name=""):
    """Register an existing tmux window as a ccm project."""
    if not source_target:
        ccm_die("Usage: ccm register <window_name|window_index> [name]")

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    # Find window by index or name
    if source_target.isdigit():
        win_idx = source_target
        win_name = tmux_cmd("display-message", "-t", f"{session}:{win_idx}",
                            "-p", "#{window_name}")
        if not win_name:
            ccm_die(f"Window not found at index: {source_target}")
    else:
        raw = tmux_cmd("list-windows", "-t", session, "-F",
                       "#{window_index}\t#{window_name}")
        win_idx = None
        win_name = source_target
        if raw:
            for line in raw.split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1] == source_target:
                    win_idx = parts[0]
                    break
        if win_idx is None:
            ccm_die(f"Window not found: {source_target}")

    win_target = f"{session}:{win_idx}"

    # Check if already tagged
    existing = tmux_cmd("show-option", "-wt", win_target, "-qv", "@ccm_project")
    if existing:
        ccm_die(f"Already a ccm project: {existing}")

    name = new_name or win_name
    name = validate_name(name)
    if not name:
        ccm_die("Invalid project name")

    if project_exists(session, name):
        ccm_die(f"Project name already in use: {name}")

    # Get directory from pane
    pane_dir = tmux_cmd("display-message", "-t", win_target, "-p", "#{pane_current_path}")

    tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_orig_name", win_name),
        ("set-option", "-wt", win_target, "@ccm_project", name),
        ("set-option", "-wt", win_target, "@ccm_dir", pane_dir or ""),
        ("set-option", "-wt", win_target, "automatic-rename", "off"),
        ("rename-window", "-t", win_target, name),
    )

    ccm_info(f"Registered: {win_name} → {name}")
    _autosave_trigger()


def cmd_unregister(name):
    """Unregister window from ccm (keep window alive)."""
    if not name:
        ccm_die("Project name is required")

    session = get_session()
    idx = find_window(session, name)
    if idx is None:
        ccm_die(f"Project window not found: {name}")

    win_target = f"{session}:{idx}"

    # Restore original name
    orig_name = tmux_cmd("show-option", "-wt", win_target, "-qv", "@ccm_orig_name")
    if orig_name:
        tmux_cmd("rename-window", "-t", win_target, orig_name)

    # Remove all ccm tags
    tags = ["automatic-rename", "@ccm_project", "@ccm_dir", "@ccm_orig_name",
            "@ccm_prev_state", "@ccm_done", "@ccm_last_done",
            "@ccm_state_icon", "@ccm_state_color"]
    cmds = [("set-option", "-wt", win_target, "-u", tag) for tag in tags]
    tmux_batch(*cmds)

    ccm_info(f"Unregistered: {name} (window kept)")
    _autosave_trigger()


def cmd_rename(old_name, new_name):
    """Rename a ccm project."""
    if not old_name:
        ccm_die("Usage: ccm rename <current_name> <new_name>")
    if not new_name:
        ccm_die("New name is required")

    new_name = validate_name(new_name)
    if not new_name:
        ccm_die("Invalid project name")

    session = get_session()
    idx = find_window(session, old_name)
    if idx is None:
        ccm_die(f"Project not found: {old_name}")

    if project_exists(session, new_name):
        ccm_die(f"Project name already in use: {new_name}")

    win_target = f"{session}:{idx}"
    tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_project", new_name),
        ("rename-window", "-t", win_target, new_name),
    )

    ccm_info(f"Renamed: {old_name} → {new_name}")
    _autosave_trigger()


def cmd_remove(name):
    """Remove a ccm project window (kill window)."""
    if not name:
        ccm_die("Project name is required")

    session = get_session()
    idx = find_window(session, name)
    if idx is None:
        ccm_die(f"Project window not found: {name}")

    tmux_cmd("kill-window", "-t", f"{session}:{idx}")
    ccm_info(f"Removed project: {name}")
    _autosave_trigger()


def cmd_list():
    """List all ccm-managed project windows."""
    session = get_session()
    if not session:
        print("No active projects.")
        return

    windows = list_windows_raw(session)
    if not windows:
        print("No active projects.")
        return

    print(f"{_C_BOLD}{'PROJECT':<20} {'DIRECTORY'}{_C_RESET}")
    print(f"{'-------':<20} {'---------'}")

    for _idx, _wn, project, proj_dir in windows:
        print(f"{project:<20} {proj_dir}")


def cmd_attach(target):
    """Switch to a ccm project window."""
    if not target:
        ccm_die("Project name or number is required")

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    idx = None
    if target.isdigit():
        # By window index
        windows = list_windows_raw(session)
        for w_idx, _, _, _ in windows:
            if w_idx == target:
                idx = w_idx
                break
        if idx is None:
            ccm_die(f"No ccm project at window index: {target}")
    else:
        idx = find_window(session, target)
        if idx is None:
            # Try by window name
            raw = tmux_cmd("list-windows", "-t", session, "-F",
                           "#{window_index}\t#{window_name}")
            if raw:
                for line in raw.split("\n"):
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[1] == target:
                        idx = parts[0]
                        break
            if idx is None:
                ccm_die(f"Project not found: {target}")

    # Check if already on this window
    current_idx = tmux_cmd("display-message", "-t", session, "-p", "#{window_index}")
    if current_idx == idx:
        ccm_info("Already in this window")
        return

    win_target = f"{session}:{idx}"

    # Auto-start Claude if SHELL state
    pane_pid = tmux_cmd("list-panes", "-t", win_target, "-F", "#{pane_pid}")
    if pane_pid:
        pane_pid = pane_pid.split("\n")[0]
        # Check if claude is running as child
        try:
            ps_out = subprocess.run(["ps", "-eo", "ppid,comm"],
                                    capture_output=True, text=True, timeout=5)
            has_claude = False
            for line in ps_out.stdout.strip().split("\n"):
                fields = line.split()
                if len(fields) >= 2 and fields[0] == pane_pid and fields[1] == "claude":
                    has_claude = True
                    break
        except (subprocess.TimeoutExpired, OSError):
            has_claude = True  # Assume running on error

        if not has_claude:
            auto_start_claude(win_target)

    clear_done(win_target)
    tmux_cmd("select-window", "-t", f"{session}:{idx}")


def cmd_capture(args):
    """Capture visible content of a project window."""
    copy_mode = False
    target = ""
    for arg in args:
        if arg in ("--copy", "-c"):
            copy_mode = True
        else:
            target = arg

    if not target:
        ccm_die("Usage: ccm capture [--copy] <name|#id>")

    session = get_session()

    # Resolve target to window index
    if target.startswith("#"):
        num = target[1:]
    elif target.isdigit():
        num = target
    else:
        num = None

    if num is not None:
        windows = list_windows_raw(session)
        idx = None
        proj_name = None
        for w_idx, _, proj, _ in windows:
            if w_idx == num:
                idx = w_idx
                proj_name = proj
                break
        if idx is None:
            ccm_die(f"No ccm project at window index: {num}")
    else:
        proj_name = target
        idx = find_window(session, target)
        if idx is None:
            ccm_die(f"Project not found: {target}")

    output = tmux_cmd("capture-pane", "-t", f"{session}:{idx}", "-p", "-S", "-50")

    if copy_mode:
        if clipboard_copy(output):
            ccm_info(f"Captured {proj_name} → clipboard")
        else:
            ccm_warn("No clipboard tool available (install pbcopy, xclip, or xsel)")
    else:
        print(f"=== ccm capture: {proj_name} ===")
        print(output)
        print("=== end ===")


def cmd_stop(target):
    """Stop project window(s)."""
    if target == "--all":
        session = get_session()
        windows = list_windows_raw(session)
        if not windows:
            print("No active projects.")
            return

        # Auto-save before stopping
        init_dirs()
        try:
            cmd_snapshot_save("_autosave", quiet=True)
            ccm_info("Auto-saved snapshot: _autosave")
        except Exception:
            pass

        for w_idx, _, project, _ in windows:
            tmux_cmd("kill-window", "-t", f"{session}:{w_idx}")
            ccm_info(f"Stopped: {project}")
    elif target:
        cmd_remove(target)
    else:
        ccm_die("Usage: ccm stop [--all|<name>]")


def cmd_pane_title(action="toggle"):
    """Control pane title display."""
    if not action:
        action = "toggle"

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    if action == "on":
        tmux_batch(
            ("set-option", "-t", session, "pane-border-status", "top"),
            ("set-option", "-t", session, "pane-border-format", "#{pane_title}"),
            ("set-option", "-g", "@ccm-pane-title", "on"),
        )
        tmux_cmd("display-message", "ccm: pane title ON")
    elif action == "off":
        tmux_batch(
            ("set-option", "-t", session, "-u", "pane-border-status"),
            ("set-option", "-t", session, "-u", "pane-border-format"),
            ("set-option", "-g", "@ccm-pane-title", "off"),
        )
        tmux_cmd("display-message", "ccm: pane title OFF")
    elif action == "toggle":
        current = tmux_cmd("show-option", "-t", session, "-qv", "pane-border-status")
        cmd_pane_title("off" if current == "top" else "on")
    elif action == "status":
        current = tmux_cmd("show-option", "-t", session, "-qv", "pane-border-status")
        print(f"pane-title: {'on' if current == 'top' else 'off'}")
    else:
        ccm_die("Usage: ccm pane-title [on|off|toggle|status]")


def cmd_clear_done():
    """Clear DONE flag for the current window."""
    session_name = tmux_cmd("display-message", "-p", "#{session_name}")
    win_idx = tmux_cmd("display-message", "-p", "#{window_index}")
    if session_name and win_idx:
        clear_done(f"{session_name}:{win_idx}")


# ─── CLI entry point ───

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]

    if cmd == "status":
        print_status()
    elif cmd == "ports":
        print_ports()
    elif cmd == "tree":
        print_tree()
    elif cmd == "statusline":
        print_statusline()
    elif cmd == "add":
        cmd_add(args[0] if args else "", args[1] if len(args) > 1 else "")
    elif cmd == "open":
        cmd_open(args[0] if args else "", args[1] if len(args) > 1 else "")
    elif cmd == "register":
        cmd_register(args[0] if args else "", args[1] if len(args) > 1 else "")
    elif cmd == "unregister":
        cmd_unregister(args[0] if args else "")
    elif cmd == "rename":
        cmd_rename(args[0] if args else "", args[1] if len(args) > 1 else "")
    elif cmd == "remove":
        cmd_remove(args[0] if args else "")
    elif cmd == "attach":
        cmd_attach(args[0] if args else "")
    elif cmd == "list":
        cmd_list()
    elif cmd == "capture":
        cmd_capture(args)
    elif cmd == "stop":
        cmd_stop(args[0] if args else "")
    elif cmd == "pane-title":
        cmd_pane_title(args[0] if args else "")
    elif cmd == "snapshot-save":
        cmd_snapshot_save(args[0] if args else "")
    elif cmd == "snapshot-load":
        cmd_snapshot_load(args[0] if args else "")
    elif cmd == "snapshot-list":
        cmd_snapshot_list()
    elif cmd == "snapshot-delete":
        cmd_snapshot_delete(args[0] if args else "")
    elif cmd == "clear-done":
        cmd_clear_done()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
