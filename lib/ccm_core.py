#!/usr/bin/env python3
"""ccm core — shared constants, helpers, state detection, and project list building."""

import contextlib
import glob as _glob_mod
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

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

# Display-layer "recently completed" marker timeout. Canonical env
# var is CCM_COMPLETED_AT_TIMEOUT; CCM_DONE_TIMEOUT is accepted as a
# backwards-compatibility alias for v0.1.0 users whose shell config
# still sets the old name.
COMPLETED_AT_TIMEOUT = int(os.environ.get(
    "CCM_COMPLETED_AT_TIMEOUT",
    os.environ.get("CCM_DONE_TIMEOUT", "30"),
))
# Hook signal age (seconds) below which a BUSY signal is treated as "fresh"
# and trusted unconditionally — bypasses the slower pipeline when multiple
# projects contend for evaluation time.
HOOK_FRESH_THRESHOLD = 2
# JSONL session-log freshness threshold. If the project's newest .jsonl
# file was touched within this many seconds, treat it as positive
# evidence of activity (Claude wrote a record at a turn boundary).
# Used as a hook-independent BUSY signal when the visible pane suggests
# IDLE but Claude has just exchanged a record.
JSONL_FRESH_THRESHOLD = int(os.environ.get("CCM_JSONL_FRESH_THRESHOLD", "5"))
# Short JSONL window used to HOLD an already-BUSY state through brief
# gaps where neither hook nor JSONL is fresh. Catches the couple of
# seconds after `jsonl_fresh_activity` (5s) expires but the session
# hasn't settled yet — e.g. Stop hook latency or a brief streaming
# pause. Short by design: longer thinking phases with an active hook
# are covered by `hook_busy_idle` (600s window + gap discriminator),
# so this rule only needs to bridge the sub-minute gap between
# "JSONL fresh" and "hook fresh" signals.
#
# Was 120s in the DONE-era design where it suppressed the BUSY→DONE
# transition during long thinking — with DONE removed and Stop hooks
# deleting the signal file, 120s produced a painful ~2-minute BUSY
# lingering after visual completion (empirically measured).
# Tuned to 15s: Stop-to-IDLE transition completes within ~15s in the
# common case, and hook_busy_idle still covers long tool runs.
JSONL_ACTIVE_THRESHOLD = int(os.environ.get("CCM_JSONL_ACTIVE_THRESHOLD", "15"))
# Window for trusting a BUSY hook signal without JSONL corroboration.
# A BUSY hook older than this AND a JSONL that has been silent for
# the same duration is almost certainly left over from a turn that
# completed without a Stop hook firing (anthropics/claude-code#25655
# class). Past this window, the rule table stops trusting the hook
# and falls through to the raw=IDLE fallback path so the state can
# eventually drop out of BUSY. Default 10 minutes — long enough to
# cover real thinking phases that legitimately lack tool activity,
# short enough that a missed Stop does not strand the project in
# BUSY indefinitely.
BUSY_HOOK_JSONL_WINDOW = int(os.environ.get("CCM_BUSY_HOOK_JSONL_WINDOW", "600"))
# JSONL real-activity filter (Claude Code v2.1.108+ recap interaction).
# Records whose top-level `type` is in this set are treated as system
# metadata, not real conversation activity. read_jsonl_age() walks the
# tail of the JSONL file backwards and returns the timestamp of the
# most recent record NOT in this set, so events like the v2.1.108
# `system/away_summary` (recap), `system/turn_duration`,
# `system/stop_hook_summary`, and `attachment/task_reminder` do not
# falsely register as fresh activity. Without this filter, recap
# generation makes ccm hold BUSY for up to BUSY_HOOK_JSONL_WINDOW
# seconds because both the JSONL mtime and the simultaneous BUSY hook
# signal corroborate "session is busy".
JSONL_NON_ACTIVITY_TYPES = frozenset({"system", "attachment", "summary"})
# Tail size (bytes) read from each JSONL when looking for the most
# recent real activity record. Needs to accommodate a single large
# tool_result record (Read of 2000 lines, long shell output, ...)
# plus several trailing system records — any tool-result record alone
# can easily exceed 8 KB, which at the previous cap could leave the
# tail without a decodable real-activity line and force the mtime
# fallback. 32 KB covers that comfortably while remaining trivially
# cheap per detection cycle.
JSONL_TAIL_BYTES = 32768
# Safety cap on how many lines from the tail we will JSON-parse.
JSONL_TAIL_MAX_LINES = 200
# Hook-vs-real-activity gap discriminator (Phase 2 of the recap fix).
# A BUSY hook signal fired more than this many seconds AFTER the last
# real conversation activity is treated as a phantom hook (no
# surrounding real work) — this is the v2.1.108 recap pattern, where
# `away_summary` generation fires a BUSY-class hook with no
# corresponding Stop. The `hook_busy_idle` rule uses this to release
# stale BUSY without breaking long-thinking detection: in real
# long-thinking, both hook_age and real_activity_age grow together
# (the gap stays ~0), but in recap the hook is brand new while
# real_activity is minutes old (gap >> threshold).
JSONL_HOOK_GAP_TOLERANCE = int(os.environ.get("CCM_JSONL_HOOK_GAP_TOLERANCE", "60"))
# Cluster-SHELL-transition detection: surface a warning when a project
# drops back to SHELL too many times in a short window. The canonical
# trigger is anthropics/claude-code#48069 (macOS silent exits in
# v2.1.107+), where Claude Code dies every 1-5 minutes and ccm
# observes SHELL → (user re-attaches) → BUSY → IDLE → SHELL loops.
# Defaults: 3 transitions in 10 minutes.
SHELL_CLUSTER_WINDOW = int(os.environ.get("CCM_SHELL_CLUSTER_WINDOW", "600"))
SHELL_CLUSTER_COUNT = int(os.environ.get("CCM_SHELL_CLUSTER_COUNT", "3"))
# Upstream issue tag surfaced in the cluster-SHELL warning. Centralised
# so future re-classification (different issue, root-cause PR, etc.)
# only needs one edit. Update both halves when the upstream story
# changes.
SHELL_CLUSTER_ISSUE = "anthropics/claude-code#48069"
SHELL_CLUSTER_ISSUE_NOTE = "macOS silent-exit"
PERMIT_MAX_TIMEOUT = int(os.environ.get("CCM_PERMIT_MAX_TIMEOUT", "600"))  # 10 min safety net
IDLE_EXIT_TIMEOUT = int(os.environ.get("CCM_IDLE_EXIT_TIMEOUT", "600"))  # 10 minutes default
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
# Permission dialog footer markers (v2.1.101+). "Tab to amend" and
# "ctrl+e to explain" are unique to the permission prompt — other
# menus (slash commands, /hooks) only show "Esc to cancel". Used as a
# hook-independent fallback when Claude Code stops firing hooks
# mid-session (see anthropics/claude-code#16047, #13193).
#
# Anchored to the start of the line (after optional whitespace and the
# "Esc to cancel · " prefix) so that the same words appearing in the
# body of a Claude response — e.g. "use ctrl+e to explain" inside an
# answer — do not falsely trigger PERMIT.
PATTERN_PERMIT_FOOTER = re.compile(
    r"^\s*Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
)

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
    "on-permission-denied.sh",
    "on-session-end.sh",
]

STATE_PRIORITY = {"PERMIT": 0, "BUSY": 1, "IDLE": 2, "SHELL": 3, "DOWN": 4}
STATE_ICONS = {
    "PERMIT": "⚠", "BUSY": "◉", "IDLE": "●", "SHELL": "■", "DOWN": "○",
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
# - STATE: one of BUSY, PERMIT (Stop hook deletes the file instead of writing)
# - Extra fields are reserved for future use and ignored by current code
# Written by: hooks/on-prompt-submit.sh, hooks/on-pre-tool-use.sh,
#             hooks/on-notification.sh (PERMIT only)
# Deleted by: hooks/on-stop.sh, hooks/on-notification.sh (idle_prompt)

VALID_HOOK_STATES = {"BUSY", "PERMIT", "SHELL"}


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


# ─── Claude Code session log (JSONL) ───
# Independent activity heartbeat that survives hook outages.
# Claude Code writes one JSONL file per session under
# `~/.claude/projects/<slug>/<sessionId>.jsonl`. Records are appended
# at conversation turn boundaries (user prompt, assistant message,
# tool_use, tool_result). The file mtime is therefore a reliable
# "session activity" signal — when fresh, Claude is alive and
# exchanging records, regardless of whether hooks are firing.
#
# Limitation: pure thinking / token streaming phases do NOT update
# the file (records are written at message completion, not during
# generation). So a stale mtime does not imply IDLE — only fresh
# mtime is actionable. We use this as a positive BUSY signal only.
#
# Slug rule (verified empirically against ~/.claude/projects/):
#   `/Users/.../speechdock` → `-Users-...-speechdock`
# Claude Code uses the *literal* cwd at session start (no realpath
# resolution), so we must NOT resolve symlinks here.

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CLAUDE_SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
JSONL_CACHE_TTL = int(os.environ.get("CCM_JSONL_CACHE_TTL", "60"))

# In-process cache: project_dir → (newest_jsonl_path, expiry_unixtime).
# Path is re-discovered on cache expiry or when the cached file is gone.
_jsonl_path_cache: dict = {}


def read_session_info(claude_pid):
    """Read the Claude Code runtime session file for a pid.

    Claude Code writes `~/.claude/sessions/{pid}.json` at session start
    with fields: pid, sessionId, cwd, startedAt, kind, entrypoint.
    This is the authoritative source for mapping a running Claude
    process to its session id and recorded cwd — no slug guessing,
    no symlink / worktree edge cases.

    Returns a dict on success, or None if the file is missing or
    malformed. Callers should gracefully fall back to slug-based
    discovery when this returns None (older Claude Code versions,
    sandboxed execution, etc.).
    """
    if not claude_pid:
        return None
    path = os.path.join(CLAUDE_SESSIONS_DIR, f"{claude_pid}.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _project_slug(project_dir: str) -> str:
    """Convert a project directory to its Claude Code JSONL slug.

    Uses the literal path with `/` → `-`. Tilde is expanded but
    symlinks are NOT resolved (Claude Code records the cwd as-given).
    """
    expanded = os.path.expanduser(project_dir)
    return expanded.replace("/", "-")


def _jsonl_from_session_info(claude_pid):
    """Resolve the exact JSONL path via ~/.claude/sessions/{pid}.json.

    Returns the path to the session's JSONL file, or None if the
    runtime session file is missing or does not point at an existing
    JSONL. Skips non-interactive sessions (e.g. `claude -p` headless
    runs) — they are not user-facing and ccm should ignore them.
    """
    info = read_session_info(claude_pid)
    if not info:
        return None
    if info.get("kind") != "interactive":
        return None
    session_id = info.get("sessionId")
    cwd = info.get("cwd")
    if not session_id or not cwd:
        return None
    slug = cwd.replace("/", "-")
    path = os.path.join(CLAUDE_PROJECTS_DIR, slug, f"{session_id}.jsonl")
    return path if os.path.exists(path) else None


def _find_newest_jsonl(project_dir: str, claude_pid=None):
    """Return the path to the newest *.jsonl file for this project,
    or None if there is none.

    Prefers the authoritative mapping via `~/.claude/sessions/{pid}.json`
    when claude_pid is provided and the runtime session file exists.
    Falls back to slug-based directory scanning for older Claude Code
    versions or when the pid mapping cannot be resolved.

    Result is cached for JSONL_CACHE_TTL seconds; the file's mtime is
    read live each call.
    """
    now = time.time()

    # Fast path: runtime session file gives us the exact JSONL.
    if claude_pid:
        exact = _jsonl_from_session_info(claude_pid)
        if exact:
            _jsonl_path_cache[project_dir] = (exact, now + JSONL_CACHE_TTL)
            return exact

    cached = _jsonl_path_cache.get(project_dir)
    if cached and now < cached[1]:
        path = cached[0]
        if path is None:
            return None
        if os.path.exists(path):
            return path
        # cached path vanished — fall through to re-scan

    slug = _project_slug(project_dir)
    session_dir = os.path.join(CLAUDE_PROJECTS_DIR, slug)
    try:
        entries = os.listdir(session_dir)
    except OSError:
        _jsonl_path_cache[project_dir] = (None, now + JSONL_CACHE_TTL)
        return None

    newest = None
    newest_mtime = -1.0
    for entry in entries:
        if not entry.endswith(".jsonl"):
            continue
        full = os.path.join(session_dir, entry)
        try:
            mt = os.path.getmtime(full)
        except OSError:
            continue
        if mt > newest_mtime:
            newest_mtime = mt
            newest = full

    _jsonl_path_cache[project_dir] = (newest, now + JSONL_CACHE_TTL)
    return newest


# Cache for _read_jsonl_real_activity_ts. Key: jsonl path. Value:
# ((mtime_int, size_int), real_activity_ts_or_None). The cache hits
# on every detection cycle as long as the JSONL file hasn't been
# written, so the cost of tail-reading + JSON parsing is paid only
# when the file actually changes.
#
# Why (mtime, size) and not just mtime: int(mtime) has 1-second
# precision, so two writes within the same wall-clock second would
# share the same int(mtime) and collide. JSONL files are append-only
# during a session, so the size is monotonic and any new write
# changes it — adding size to the key catches sub-second writes that
# bare mtime would miss.
#
# OrderedDict + bounded eviction: a new JSONL file is created on every
# `claude --continue` or `/compact`, so the cache would otherwise grow
# without bound in long-running tmux sessions. On each lookup we move
# the entry to the end (MRU); on insertion, we pop the oldest entry
# if the cache exceeds JSONL_ACTIVITY_CACHE_MAX.
JSONL_ACTIVITY_CACHE_MAX = 128
_jsonl_activity_cache: "OrderedDict[str, Tuple[Tuple[int, int], Optional[int]]]" = OrderedDict()


def _read_jsonl_real_activity_ts(
    path: str, mtime: int, size: int
) -> Optional[int]:
    """Tail-read a JSONL file and return the unix timestamp of the most
    recent real-conversation-activity record, or None if no such record
    was found in the tail window. System metadata records (those whose
    `type` is in JSONL_NON_ACTIVITY_TYPES) are skipped.

    Cached by (path, mtime, size): the second call with an unchanged
    mtime AND size returns the cached result without re-reading the
    file. A new write changes the size (JSONL is append-only during
    a session), so cache invalidation is reliable even within the
    same wall-clock second.

    On a "real activity record found but timestamp unparseable" edge
    case (malformed Claude Code output, hypothetical schema drift),
    falls back to the file mtime itself so the rule engine still has
    a usable signal — better to err on the side of "fresh" than lose
    detection entirely.
    """
    key = (mtime, size)
    cached = _jsonl_activity_cache.get(path)
    if cached is not None and cached[0] == key:
        _jsonl_activity_cache.move_to_end(path)
        return cached[1]

    real_ts: Optional[int] = None
    found_real_activity = False

    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            actual_size = f.tell()
            f.seek(max(0, actual_size - JSONL_TAIL_BYTES))
            tail_bytes = f.read()
    except OSError:
        _cache_jsonl_activity(path, key, None)
        return None

    tail = tail_bytes.decode("utf-8", errors="ignore")
    lines = tail.split("\n")
    # When we read mid-file, the first chunk line is potentially partial.
    # Drop it unless we read the entire file in one shot.
    if size > JSONL_TAIL_BYTES and len(lines) > 1:
        lines = lines[1:]

    parsed = 0
    for line in reversed(lines):
        if parsed >= JSONL_TAIL_MAX_LINES:
            break
        line = line.strip()
        if not line:
            continue
        parsed += 1
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        rec_type = rec.get("type")
        if not rec_type or rec_type in JSONL_NON_ACTIVITY_TYPES:
            continue
        # Found a real-activity record.
        found_real_activity = True
        ts_str = rec.get("timestamp")
        if not ts_str or not isinstance(ts_str, str):
            continue
        try:
            iso = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            real_ts = int(dt.timestamp())
            break
        except (ValueError, TypeError):
            continue

    if real_ts is None and found_real_activity:
        # Real activity records exist but none had a parseable timestamp.
        # Fall back to file mtime as a safe approximation so detection
        # rules still see a "fresh" signal.
        real_ts = mtime

    _cache_jsonl_activity(path, key, real_ts)
    return real_ts


def _cache_jsonl_activity(path: str, key: Tuple[int, int],
                          real_ts: Optional[int]) -> None:
    """Insert into _jsonl_activity_cache with LRU eviction."""
    _jsonl_activity_cache[path] = (key, real_ts)
    _jsonl_activity_cache.move_to_end(path)
    while len(_jsonl_activity_cache) > JSONL_ACTIVITY_CACHE_MAX:
        _jsonl_activity_cache.popitem(last=False)


def read_jsonl_age(project_dir: str, claude_pid=None) -> int:
    """Return seconds since the project's most recent real conversation
    activity (`user`/`assistant`/etc.) in the newest JSONL file, or -1
    if no JSONL exists or no real activity is present in the tail.

    NOTE: this filters out system metadata records (Claude Code v2.1.108+
    `system/away_summary` recap, `system/turn_duration`,
    `system/stop_hook_summary`, `attachment/task_reminder`, ...) so
    that the recap event does NOT register as fresh activity. Without
    the filter, recap holds BUSY for up to BUSY_HOOK_JSONL_WINDOW
    seconds because both the file mtime and the simultaneous BUSY
    hook signal corroborate "session is busy".

    When claude_pid is provided, the exact session file is resolved
    via `~/.claude/sessions/{pid}.json` (authoritative, no slug guess).
    """
    if not project_dir:
        return -1
    newest = _find_newest_jsonl(project_dir, claude_pid=claude_pid)
    if newest is None:
        return -1
    try:
        st = os.stat(newest)
    except OSError:
        return -1
    real_ts = _read_jsonl_real_activity_ts(newest, int(st.st_mtime), st.st_size)
    if real_ts is None:
        return -1
    return int(time.time() - real_ts)


# ─── Claude Code hooks.log canary ───
# Per anthropics/claude-code#16047, an unrotated `~/.claude/hooks.log`
# can grow to many GB and silently disable all hook firing (every
# hook write fails). Claude Code does not rotate or cap this file.
# We surface a warning in `ccm status` and the dashboard footer when
# the size crosses a threshold so the user can `: > ~/.claude/hooks.log`
# and recover hook delivery.

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


# ─── Claude Code global settings canary ───
# Detect configuration flags in ~/.claude/settings.json that silently
# disable ccm's fast-path signals. If the user sets these, state
# detection degrades without any obvious error — we surface a warning
# so the cause is discoverable.

CLAUDE_SETTINGS_FILE = os.path.expanduser("~/.claude/settings.json")


def _read_claude_settings():
    try:
        with open(CLAUDE_SETTINGS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def disable_all_hooks_warning() -> str:
    """Return a warning string if `disableAllHooks: true` is set in
    ~/.claude/settings.json, otherwise "".

    Per Claude Code v2.1.104 docs, this setting disables ALL hooks AND
    any custom statusLine — ccm's entire fast-path signal goes dark
    with no error. Same class of silent failure as the hooks.log
    bloat canary.

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

    Per Claude Code v2.1.107 docs, when this is set in *managed*
    settings, every user-scope hook (which is exactly where ccm
    installs all 14 of its hooks) is silently blocked with no error.
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
# When Claude Code dies repeatedly (most commonly the v2.1.107+ macOS
# silent-exit regression, anthropics/claude-code#48069), ccm observes a
# rapid SHELL → (user or ccm re-attach) → BUSY → IDLE → SHELL loop.
# We record each SHELL transition timestamp in a per-window tmux
# option `@ccm_shell_history` and surface a warning if the count in
# the last SHELL_CLUSTER_WINDOW exceeds SHELL_CLUSTER_COUNT.

_SHELL_HISTORY_OPT = "@ccm_shell_history"


def _read_shell_history(win_target: str) -> list:
    """Read the SHELL transition timestamp history for a window.

    Returns a list of unix timestamps, newest first. Entries older
    than SHELL_CLUSTER_WINDOW seconds are filtered out on read.
    """
    raw = tmux_cmd("show-option", "-wqv", "-t", win_target, _SHELL_HISTORY_OPT)
    if not raw:
        return []
    now = int(time.time())
    horizon = now - SHELL_CLUSTER_WINDOW
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


def _push_shell_transition(win_target: str) -> None:
    """Record a new SHELL transition for a window.

    Prepends the current timestamp to `@ccm_shell_history`, trims
    entries older than SHELL_CLUSTER_WINDOW via `_read_shell_history`,
    and caps the stored size at 2 × SHELL_CLUSTER_COUNT entries (or
    a floor of 6) to prevent unbounded growth of the tmux option.
    Every push writes back a capped, trimmed history so that even
    pre-existing oversized values converge on the cap.
    """
    existing = _read_shell_history(win_target)
    now = int(time.time())
    max_entries = max(SHELL_CLUSTER_COUNT * 2, 6)

    # Deduplicate same-second pushes so two code paths hitting
    # apply_actions for the same scan cycle don't double-count one
    # transition. We still write back the capped history though,
    # so pre-existing oversized values are normalised.
    if existing and existing[0] == now:
        updated = existing
    else:
        updated = [now] + existing

    updated = updated[:max_entries]
    tmux_cmd(
        "set-option", "-wt", win_target, _SHELL_HISTORY_OPT,
        ",".join(str(t) for t in updated),
    )


def shell_cluster_warning(win_target: str, project_name: str = "") -> str:
    """Return a one-line warning if this window has hit the SHELL
    cluster threshold, otherwise "".

    The caller typically iterates over projects and collects the
    non-empty warnings for display in the dashboard footer and
    `ccm status` output.
    """
    history = _read_shell_history(win_target)
    if len(history) < SHELL_CLUSTER_COUNT:
        return ""
    label = f"{project_name}: " if project_name else ""
    return (
        f"{label}Claude Code exited {len(history)}+ times in "
        f"{SHELL_CLUSTER_WINDOW // 60} min — likely "
        f"{SHELL_CLUSTER_ISSUE} ({SHELL_CLUSTER_ISSUE_NOTE}). "
        f"The conversation auto-restores via `claude --continue`."
    )


def shell_cluster_warnings(projects) -> list:
    """Return a list of warning strings, one per project that has
    crossed the SHELL cluster threshold. Empty list if all quiet.
    """
    out = []
    for p in projects:
        msg = shell_cluster_warning(p.win_target, p.name)
        if msg:
            out.append(msg)
    return out


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


def has_grandchildren(pid, ps_lines, own_pgid):
    """True if any descendant two levels below `pid` exists.

    Claude Code's Bash tool runs commands as `claude → bash → cmd`,
    so a grandchild process is unambiguous evidence that a foreground
    tool is executing. MCP servers and language servers are direct
    children of claude only — they do not spawn nested workers in
    normal operation, so this check does not false-trigger on them.

    This is the hook-independent fallback for the case where Claude
    Code's UI shows the empty `❯ ` prompt above a still-running tool
    (the v2.1+ "ctrl+b ctrl+b to background" affordance), which would
    otherwise let the input-prompt heuristic resolve to IDLE.
    """
    children = set()
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == str(pid):
            if parts[2] == str(own_pgid):
                continue
            if parts[3] in IGNORED_CHILDREN:
                continue
            children.add(parts[0])
    if not children:
        return False
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] in children:
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

    has_child = has_children(claude_pid, ps_lines, own_pgid)

    # Capture once and share between the permit-footer check and the
    # input-prompt check below. We check permit before has_children
    # because permission dialogs can appear with no Claude child
    # process (the tool subprocess has not been spawned yet — Claude
    # is waiting for user approval).
    bottom = capture_pane_bottom(pane_target)
    for line in bottom:
        if PATTERN_PERMIT_FOOTER.match(line):
            return "PERMIT"

    if has_child:
        # Input prompt visible → Claude is waiting for user input.
        # Even if grandchildren exist (e.g. a dev server started by
        # a previous Bash tool that is still running), Claude itself
        # is idle. The v2.1+ case where `❯ ` appears above a STILL-
        # ACTIVE tool is handled at the window level by hook_busy_idle
        # and jsonl_fresh_activity rules, not here.
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


def _set_win_state(win_target, state):
    """Write @ccm_prev_state to the window."""
    tmux_cmd("set-option", "-wt", win_target, "@ccm_prev_state", state)


# ─── Declarative state detection ───
# detect_window_state is decomposed into:
#   1. build_detection_context — gather raw, hook, jsonl inputs
#   2. evaluate_rules          — pure priority-ordered rule match
#   3. apply_actions           — execute tmux / busy-file side effects
#
# To add or change a detection rule, edit DETECTION_RULES below.
# The rule table is the single source of truth for state transitions.


@dataclass(frozen=True)
class DetectionContext:
    """Immutable snapshot of all inputs to state detection.

    All fields are derived before any rule is evaluated, so rule matching
    is a pure function of this context.
    """
    raw: str              # detect_window_raw result: DOWN/SHELL/BUSY/IDLE
    hook_state: str       # hook signal state: BUSY/PERMIT/SHELL/""
    hook_ts: int          # hook signal timestamp (0 if no signal)
    hook_age: int         # now - hook_ts (-1 if no signal)
    prev_state: str       # previous detected state
    jsonl_age: int        # now - newest JSONL mtime (-1 if missing)
    now: int              # current unix timestamp


class Action(Enum):
    """Side effect to execute when a rule matches.

    DEFAULT         — set @ccm_prev_state to resolved state
    HOLD_NO_WRITE   — do not touch tmux state (preserve prior state)
    """
    DEFAULT = "default"
    HOLD_NO_WRITE = "hold_no_write"


# Sentinel: result=USE_RAW means "use ctx.raw as the resolved state".
# A unique object ensures it cannot collide with a real state name.
class _UseRawSentinel:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<USE_RAW>"


USE_RAW = _UseRawSentinel()


@dataclass(frozen=True)
class Rule:
    """Declarative detection rule.

    Condition fields: None = wildcard (not checked).
    - raw_in         — ctx.raw must be in this tuple
    - hook_in        — ctx.hook_state must be in this tuple (implies signal present)
    - prev_in        — ctx.prev_state must be in this tuple
    - hook_age_lt    — ctx.hook_age must satisfy 0 <= age < value
    """
    name: str
    # str for concrete state (e.g. "BUSY"), or the USE_RAW sentinel
    result: object = USE_RAW
    action: Action = Action.DEFAULT
    raw_in: Optional[Tuple[str, ...]] = None
    hook_in: Optional[Tuple[str, ...]] = None
    prev_in: Optional[Tuple[str, ...]] = None
    hook_age_lt: Optional[int] = None
    jsonl_age_lt: Optional[int] = None
    # True: ctx.jsonl_age must be < 0 (no JSONL file at all)
    # False: ctx.jsonl_age must be >= 0 (JSONL file exists)
    # None: not checked
    jsonl_missing: Optional[bool] = None
    # Recap discriminator (Phase 2 of the v2.1.108 recap fix). When
    # set, the rule matches only if the BUSY hook signal was fired
    # within `value` seconds AFTER the last real conversation activity
    # (`ctx.jsonl_age - ctx.hook_age < value`). Both hook_age and
    # jsonl_age must be valid (>= 0); otherwise the rule does NOT
    # match.
    hook_after_real_activity_lt: Optional[int] = None

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
        if self.jsonl_age_lt is not None:
            if ctx.jsonl_age < 0 or ctx.jsonl_age >= self.jsonl_age_lt:
                return False
        if self.jsonl_missing is True:
            if ctx.jsonl_age >= 0:
                return False
        elif self.jsonl_missing is False:
            if ctx.jsonl_age < 0:
                return False
        if self.hook_after_real_activity_lt is not None:
            if ctx.hook_age < 0 or ctx.jsonl_age < 0:
                return False
            if (ctx.jsonl_age - ctx.hook_age) >= self.hook_after_real_activity_lt:
                return False
        return True


# Priority-ordered rule table. First match wins.
#
# Priority rationale (4-state model: PERMIT/BUSY/IDLE/SHELL):
#   1-2  process tree authoritative for SHELL/DOWN
#   3    fresh BUSY hook beats stale pipeline (multi-project race)
#   4    PERMIT blocks BUSY when dialog actually visible
#   5-6  BUSY hook overrides idle pipeline (text generation)
#   7-8  JSONL freshness signals
#   9    BUSY → IDLE fallback (direct, no DONE intermediate)
#   10   PERMIT hold (brief IDLE gap after user approves)
#   11   raw BUSY/PERMIT passthrough
#   12   default: trust raw state
DETECTION_RULES: Tuple[Rule, ...] = (
    Rule(name="process_down", raw_in=("DOWN",), result="DOWN"),
    Rule(name="process_shell", raw_in=("SHELL",), result="SHELL"),
    Rule(
        # Fast path: very fresh BUSY hook is trusted over any raw state.
        # The recap discriminator (hook_after_real_activity_lt) rejects
        # phantom hooks from v2.1.108+ recap events.
        name="hook_fresh_busy",
        hook_in=("BUSY",),
        hook_age_lt=HOOK_FRESH_THRESHOLD,
        hook_after_real_activity_lt=JSONL_HOOK_GAP_TOLERANCE,
        result="BUSY",
    ),
    Rule(
        # PERMIT dialog visible (raw != IDLE means input prompt is not
        # showing, so the permission UI is still active).
        name="hook_permit_blocking",
        hook_in=("PERMIT",),
        hook_age_lt=PERMIT_MAX_TIMEOUT,
        raw_in=("BUSY", "PERMIT"),
        result="PERMIT",
    ),
    Rule(
        # Slow path: trust a BUSY hook signal while raw=IDLE, as long as
        # the session's JSONL has been written within BUSY_HOOK_JSONL_WINDOW
        # AND the BUSY hook fired within JSONL_HOOK_GAP_TOLERANCE of the
        # last real conversation activity. Stale BUSY hooks (no JSONL
        # corroboration) fall through to fallback_busy_to_idle.
        name="hook_busy_idle",
        hook_in=("BUSY",),
        raw_in=("IDLE",),
        jsonl_age_lt=BUSY_HOOK_JSONL_WINDOW,
        jsonl_missing=False,
        hook_after_real_activity_lt=JSONL_HOOK_GAP_TOLERANCE,
        result="BUSY",
    ),
    Rule(
        # Same BUSY-hook-trust path for the edge case where no JSONL
        # exists for this project at all. Trust the BUSY hook
        # unconditionally without JSONL corroboration.
        name="hook_busy_idle_no_jsonl",
        hook_in=("BUSY",),
        raw_in=("IDLE",),
        jsonl_missing=True,
        result="BUSY",
    ),
    Rule(
        # JSONL session log shows fresh activity (within 5s). Independent
        # of hooks — Claude Code writes records at conversation turn
        # boundaries. Also serves as the natural multi-turn bridge: when
        # Stop deletes the signal file, JSONL freshness keeps BUSY for
        # a few seconds until the next PreToolUse fires.
        name="jsonl_fresh_activity",
        raw_in=("IDLE",),
        jsonl_age_lt=JSONL_FRESH_THRESHOLD,
        result="BUSY",
    ),
    Rule(
        # Longer JSONL window used to HOLD an already-BUSY state
        # through long thinking phases when hooks have gone silent.
        # Prevents premature BUSY → IDLE during silent thinking.
        name="jsonl_holds_busy",
        raw_in=("IDLE",),
        prev_in=("BUSY",),
        jsonl_age_lt=JSONL_ACTIVE_THRESHOLD,
        result="BUSY",
    ),
    Rule(
        # Fallback: BUSY → IDLE direct transition (no DONE intermediate).
        # Fires when all BUSY evidence has aged out.
        name="fallback_busy_to_idle",
        raw_in=("IDLE",),
        prev_in=("BUSY",),
        result="IDLE",
    ),
    Rule(
        # Fallback: keep PERMIT until a hook signal (BUSY) arrives.
        # After user responds, there's a brief IDLE gap before the tool
        # starts; don't let the fallback turn that into IDLE.
        # HOLD_NO_WRITE: do not touch tmux state — preserve prior PERMIT.
        name="fallback_permit_hold",
        raw_in=("IDLE",),
        prev_in=("PERMIT",),
        hook_in=("PERMIT",),
        hook_age_lt=PERMIT_MAX_TIMEOUT,
        result="PERMIT",
        action=Action.HOLD_NO_WRITE,
    ),
    Rule(
        # raw BUSY/PERMIT passthrough: trust raw state.
        name="raw_not_idle",
        raw_in=("BUSY", "PERMIT"),
        result=USE_RAW,
    ),
    Rule(
        # Default: trust raw state. Always matches (terminal rule).
        name="default",
        result=USE_RAW,
    ),
)


def evaluate_rules(ctx: DetectionContext,
                   rules: Tuple[Rule, ...] = DETECTION_RULES) -> Tuple[Rule, str]:
    """Pure: return (matched_rule, resolved_state) for the first matching rule.

    No I/O, no tmux calls — this function is the testable core of detection.
    """
    for rule in rules:
        if rule.matches(ctx):
            state = ctx.raw if rule.result is USE_RAW else rule.result
            return rule, state
    # The terminal "default" rule guarantees a match. If we get here the
    # rule table is broken.
    raise RuntimeError("DETECTION_RULES has no terminal default rule")


# prev_state → synthetic raw mapping for the fast path.
# The fast path (statusline) skips the ps/capture-pane pipeline, so it
# has no real `raw` value. It derives one from prev_state under the
# assumption that Claude is still in the same lifecycle phase as the
# last authoritative slow-path evaluation.
_FAST_PREV_TO_RAW = {
    "DOWN": "DOWN",
    "SHELL": "SHELL",
    "BUSY": "BUSY",
    "PERMIT": "PERMIT",
    "IDLE": "IDLE",
    "": "IDLE",
}


def build_fast_context(prev_state, project_dir,
                       now=None) -> DetectionContext:
    """Build a DetectionContext for the read-only statusline path.

    Does not call ps/capture-pane/tmux queries for process tree info.
    Derives `raw` from prev_state, reads hook signal only.
    """
    if now is None:
        now = int(time.time())

    raw = _FAST_PREV_TO_RAW.get(prev_state, "IDLE")

    hook_state = ""
    hook_ts = 0
    hook_age = -1
    if project_dir:
        sig = read_hook_signal(project_dir)
        if sig is not None:
            hook_ts, hook_state, _detail = sig
            if hook_state == "SHELL":
                hook_state = ""
                hook_ts = 0
            else:
                hook_age = now - hook_ts

    jsonl_age = read_jsonl_age(project_dir) if project_dir else -1

    return DetectionContext(
        raw=raw,
        hook_state=hook_state,
        hook_ts=hook_ts,
        hook_age=hook_age,
        prev_state=prev_state,
        jsonl_age=jsonl_age,
        now=now,
    )


def evaluate_fast(prev_state, project_dir, now=None) -> str:
    """Read-only state evaluation for statusline-speed contexts.

    Runs the same DETECTION_RULES table as the slow path, so there is
    one source of truth for state transitions. Does not write to tmux
    — the slow-path run next cycle is authoritative for persisting state.
    """
    ctx = build_fast_context(prev_state, project_dir, now)
    _rule, state = evaluate_rules(ctx)
    return state


def build_detection_context(win_target, project_dir, prev_state,
                            panes_cache, ps_lines, own_pgid
                            ) -> DetectionContext:
    """Gather all inputs needed for rule evaluation.

    Read-only side effects only (tmux query, ps snapshot, file reads).
    The returned context is an immutable snapshot.
    """
    now = int(time.time())
    raw = detect_window_raw(win_target, panes_cache, ps_lines, own_pgid)

    # Find the window's primary claude_pid (first pane that hosts one).
    # Used to resolve the exact JSONL path via the runtime session file
    # at ~/.claude/sessions/{pid}.json.
    claude_pid = None
    for wt, pane_pid, _pane_id in panes_cache:
        if wt != win_target:
            continue
        cp = find_claude_pid(pane_pid, ps_lines)
        if cp:
            claude_pid = cp
            break

    hook_state = ""
    hook_ts = 0
    hook_age = -1
    if project_dir:
        sig = read_hook_signal(project_dir)
        if sig is not None:
            hook_ts, hook_state, _detail = sig
            # SHELL hook signal is ignored: process tree is authoritative
            # for SHELL; trusting a stale SHELL signal while raw=IDLE
            # causes false SHELL after Claude restarts.
            if hook_state == "SHELL":
                hook_state = ""
                hook_ts = 0
            else:
                hook_age = now - hook_ts

    jsonl_age = read_jsonl_age(project_dir, claude_pid=claude_pid) if project_dir else -1

    return DetectionContext(
        raw=raw,
        hook_state=hook_state,
        hook_ts=hook_ts,
        hook_age=hook_age,
        prev_state=prev_state,
        jsonl_age=jsonl_age,
        now=now,
    )


def apply_actions(win_target, project_dir, ctx: DetectionContext, rule: Rule,
                  state: str) -> str:
    """Execute the side effects declared by a matched rule.

    Returns the resolved state string.
    """
    action = rule.action

    # Record SHELL transitions for cluster-crash detection (#48069).
    # A transition into SHELL from a known *active* state (BUSY /
    # IDLE / PERMIT) is a real session exit and gets pushed.
    if state == "SHELL" and ctx.prev_state in (
        "BUSY", "IDLE", "PERMIT"
    ):
        _push_shell_transition(win_target)

    if action == Action.HOLD_NO_WRITE:
        return state

    # Action.DEFAULT — set @ccm_prev_state
    _set_win_state(win_target, state)

    # Set @ccm_completed_at when transitioning from BUSY/PERMIT to IDLE.
    # This is a display-layer marker — the ✔ icon shows for
    # COMPLETED_AT_TIMEOUT seconds after the transition.
    if state == "IDLE" and ctx.prev_state in ("BUSY", "PERMIT"):
        tmux_cmd("set-option", "-wt", win_target, "@ccm_completed_at", str(ctx.now))

    return state


def detect_window_state(win_target, project_dir, prev_state,
                        panes_cache, ps_lines, own_pgid):
    """Full detection pipeline. Returns the resolved state string.

    Thin orchestration layer:
      1. build_detection_context — gather inputs
      2. evaluate_rules          — pure rule-table match
      3. apply_actions           — execute tmux/file side effects

    All state transitions are declared in DETECTION_RULES above. To add
    or change a case, edit the rule table rather than this function.
    """
    ctx = build_detection_context(
        win_target, project_dir, prev_state,
        panes_cache, ps_lines, own_pgid,
    )
    rule, state = evaluate_rules(ctx)
    return apply_actions(win_target, project_dir, ctx, rule, state)


# ─── Project data ───

class Project:
    __slots__ = (
        "win_target", "win_idx", "name", "dir", "state",
        "branch", "ports", "completed_at", "sort_key",
    )

    def __init__(self, win_target, win_idx, name, directory, state,
                 branch="", ports="", completed_at=0):
        self.win_target = win_target
        self.win_idx = win_idx
        self.name = name
        self.dir = directory
        self.state = state
        self.branch = branch
        self.ports = ports
        self.completed_at = completed_at
        self.sort_key = (STATE_PRIORITY.get(state, 4), -(completed_at or 0))


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
    """Build project list from tmux. If fast, skip git/port refresh.
    Set CCM_MOCK_STATE=1 env var or @ccm-mock-state tmux option to force
    fast mode (uses @ccm_prev_state only — for screenshots).
    """
    if (os.environ.get("CCM_MOCK_STATE") == "1"
            or tmux_cmd("show-option", "-gqv", "@ccm-mock-state") == "1"):
        fast = True
    raw = tmux_cmd(
        "list-windows", "-a", "-F",
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_dir}\t"
        "#{@ccm_prev_state}\t#{@ccm_completed_at}\t#{window_activity}"
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
        if len(parts) < 6:
            continue
        win_target, project, proj_dir = parts[0], parts[1], parts[2]
        prev_state, completed_at_str, win_activity_str = (
            parts[3], parts[4], parts[5]
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

        completed_at = 0
        if completed_at_str and completed_at_str != "0":
            try:
                completed_at = int(completed_at_str)
            except ValueError:
                pass

        win_activity = 0
        if win_activity_str:
            try:
                win_activity = int(win_activity_str)
            except ValueError:
                pass

        if fast:
            # Unified with slow path via DETECTION_RULES. Read-only.
            state = evaluate_fast(prev_state, proj_dir)
        else:
            state = detect_window_state(
                win_target, proj_dir, prev_state,
                panes_cache, ps_lines, own_pgid
            )

        sort_ts = max(completed_at, win_activity) if win_activity else completed_at

        branch = read_cache_file(CCM_GIT_CACHE_DIR, proj_dir) if proj_dir else ""
        ports = read_cache_file(CCM_PORT_CACHE_DIR, proj_dir) if proj_dir else ""

        projects.append(Project(
            win_target=win_target, win_idx=win_idx, name=project,
            directory=proj_dir, state=state, branch=branch, ports=ports,
            completed_at=sort_ts,
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
    Controlled by @ccm-notify tmux option: off, permit, completed, permit,completed, all.
    detail: optional context (e.g., "Bash: rm -rf ..." for PERMIT).
    """
    setting = tmux_cmd("show-option", "-gqv", "@ccm-notify") or "permit,completed"
    if setting == "off":
        return

    state_lower = state.lower()
    # Backwards compatibility: "done" in setting also matches "completed"
    if setting != "all":
        if state_lower not in setting and not (state_lower == "completed" and "done" in setting):
            return

    sound_setting = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound") or "off"
    sound_name = (tmux_cmd("show-option", "-gqv", "@ccm-notify-sound-name") or "Glass") if sound_setting == "on" else ""

    permit_body = f"Permission required: {detail}" if detail else \
                  "Action required — respond to the permission prompt"
    messages = {
        "PERMIT": (f"ccm ⚠ {project}",
                   permit_body,
                   sound_name),
        "COMPLETED": (f"ccm ✔ {project}",
                      "Claude has finished responding — review the output when ready",
                      sound_name),
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
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_prev_state}\t#{@ccm_completed_at}\t#{window_activity}"
    )
    if not activity_raw:
        return

    for line in activity_raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 5:
            parts.append("")
        win_target, project, prev_state, completed_at_str, win_activity_str = parts[:5]

        if not project or prev_state != "IDLE":
            continue
        if win_target == current_target:
            continue

        # Parse timestamps
        completed_at = 0
        if completed_at_str and completed_at_str != "0":
            try:
                completed_at = int(completed_at_str)
            except ValueError:
                pass

        win_activity = 0
        if win_activity_str and win_activity_str != "0":
            try:
                win_activity = int(win_activity_str)
            except ValueError:
                pass

        idle_since = max(completed_at, win_activity)

        if idle_since == 0:
            tmux_cmd("set-option", "-wt", win_target, "@ccm_completed_at", str(now))
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
            _set_win_state(win_target, "SHELL")
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
    warning = hooks_log_warning()
    if warning:
        print(f"\033[33m⚠ {warning}\033[0m")
    disable_warning = disable_all_hooks_warning()
    if disable_warning:
        print(f"\033[33m⚠ {disable_warning}\033[0m")
    managed_warning = managed_hooks_only_warning()
    if managed_warning:
        print(f"\033[33m⚠ {managed_warning}\033[0m")
    for cluster_msg in shell_cluster_warnings(projects):
        print(f"\033[33m⚠ {cluster_msg}\033[0m")
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
    active = [p for p in projects if p.state in ("BUSY", "PERMIT")]
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


class CCMError(Exception):
    """Raised by ccm_die when raise_on_die() context is active.

    Allows TUI callers (e.g. dashboard) to receive errors as exceptions
    instead of stderr output + sys.exit, which would corrupt curses display.
    """
    pass


_die_mode = threading.local()


@contextlib.contextmanager
def raise_on_die():
    """Context manager: make ccm_die raise CCMError instead of exit.

    Thread-local, so CLI callers on other threads are unaffected.
    """
    prev = getattr(_die_mode, "raise_errors", False)
    _die_mode.raise_errors = True
    try:
        yield
    finally:
        _die_mode.raise_errors = prev


def ccm_die(msg):
    """Print error message and exit.

    If called inside a raise_on_die() context on the same thread, raises
    CCMError(msg) instead.
    """
    if getattr(_die_mode, "raise_errors", False):
        raise CCMError(msg)
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


def reset_window_after_attach(win_target):
    """Run the post-attach reset bundle for a project window.

    Called whenever the user attaches to a project (CLI `cmd_attach`,
    dashboard `_do_attach`, dashboard tree-mode attach). All side
    effects are keyed off `@ccm_dir`; on a non-ccm window this is a
    no-op:

    1. Unset `@ccm_completed_at` so the ✔ marker disappears.
    2. Clear `@ccm_prev_state` so the next scan recomputes state
       from scratch (no stale carry-over from before the attach).
    3. Unset `@ccm_shell_history` so the cluster-SHELL canary
       (#48069) is acknowledged. The warning will reappear only if
       NEW transitions cluster after the attach.

    Symmetric across all attach paths — do not duplicate these
    set-option calls inline elsewhere.
    """
    proj_dir = tmux_cmd("show-option", "-wqv", "-t", win_target, "@ccm_dir")
    if not proj_dir:
        return
    tmux_cmd("set-option", "-wt", win_target, "-u", "@ccm_completed_at")
    tmux_cmd("set-option", "-wq", "-t", win_target, "@ccm_prev_state", "")
    tmux_cmd("set-option", "-wt", win_target, "-u", "@ccm_shell_history")


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

    # Remove all ccm tags. Legacy tags (`@ccm_done`, `@ccm_last_done`)
    # from the pre-4-state model are included so v0.1.0 installs that
    # have lingering tmux options get a clean unregister after upgrade.
    tags = ["automatic-rename", "@ccm_project", "@ccm_dir", "@ccm_orig_name",
            "@ccm_prev_state", "@ccm_completed_at",
            "@ccm_state_icon", "@ccm_state_color",
            "@ccm_shell_history",
            "@ccm_done", "@ccm_last_done"]
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

    reset_window_after_attach(win_target)
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



def cmd_send(args):
    """Send a prompt to a project's Claude Code session.

    Usage:
      ccm send <name|#idx> <message>       Send literal message + Enter
      ccm send <name> --file <path>        Read message from file
      ccm send <name> --stdin              Read message from stdin
      ccm send <name> --no-enter <msg>     Send without submitting
      ccm send <name> --force <msg>        Send to a BUSY project (queued)
      ccm send <name> --start <msg>        Auto-launch Claude if SHELL
      ccm send -y <name> <msg>             Skip confirmation prompt
      ccm send <name> -- "--literal"       `--` ends flag parsing

    State policy:
      IDLE         → send immediately
      BUSY         → refuse without --force; queue into buffer with --force
      PERMIT       → ALWAYS refuse (hard guard — typing into a permission
                     dialog could accidentally approve or deny a tool call)
      SHELL        → refuse without --start; launch Claude + 2s wait with --start

    Multi-line messages (`\\n` in content) are converted to M-Enter between
    lines + a final Enter, matching Claude Code's "newline without submit"
    convention.
    """
    target = None
    positional_parts = []
    message_file = None
    use_stdin = False
    no_enter = False
    force = False
    auto_start = False
    skip_confirm = False

    stop_flags = False
    i = 0
    while i < len(args):
        arg = args[i]
        if not stop_flags and arg == "--":
            stop_flags = True
            i += 1
            continue
        if not stop_flags and arg.startswith("-") and arg != "-":
            if arg == "--file":
                i += 1
                if i >= len(args):
                    ccm_die("--file requires a path argument")
                message_file = args[i]
            elif arg == "--stdin":
                use_stdin = True
            elif arg == "--no-enter":
                no_enter = True
            elif arg == "--force":
                force = True
            elif arg == "--start":
                auto_start = True
            elif arg in ("-y", "--yes"):
                skip_confirm = True
            else:
                ccm_die(
                    f"Unknown flag: {arg}\n"
                    "Usage: ccm send <name> <message> "
                    "[--file path] [--stdin] [--force] [--start] "
                    "[--no-enter] [-y]"
                )
        else:
            if arg == "-":  # conventional stdin alias
                use_stdin = True
            elif target is None:
                target = arg
            else:
                positional_parts.append(arg)
        i += 1

    if not target:
        ccm_die(
            "Usage: ccm send <name> <message> "
            "[--file path] [--stdin] [--force] [--start] "
            "[--no-enter] [-y]"
        )

    # Resolve message source (exactly one of the three)
    positional_message = " ".join(positional_parts) if positional_parts else None
    source_count = sum(x is not None and x is not False for x in
                       (positional_message, message_file, use_stdin or None))
    if source_count == 0:
        ccm_die("No message provided (positional, --file, or --stdin)")
    if source_count > 1:
        ccm_die("Provide exactly one of: positional message, --file, or --stdin")

    if message_file:
        try:
            with open(message_file) as f:
                message = f.read()
        except OSError as e:
            ccm_die(f"Failed to read message file: {e}")
    elif use_stdin:
        message = sys.stdin.read()
        # Once we have consumed stdin, the interactive confirmation
        # prompt can no longer read from it (EOFError). Force-skip
        # confirmation so a TTY user running `ccm send blog --stdin`
        # and typing a body terminated by Ctrl-D is not silently
        # cancelled.
        skip_confirm = True
    else:
        message = positional_message

    if not message.strip() and not no_enter:
        ccm_die("Empty message (use --no-enter to send only Enter suppression)")

    # Resolve target window
    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    if target.startswith("#"):
        idx = target[1:]
    elif target.isdigit():
        idx = target
    else:
        idx = find_window(session, target)
        if idx is None:
            ccm_die(f"Project not found: {target}")

    win_target = f"{session}:{idx}"

    # Look up project state from the current ccm scan
    projects = build_project_list(fast=False)
    matched = next((p for p in projects if p.win_target == win_target), None)
    if matched is None:
        ccm_die(f"Window is not a registered ccm project: {win_target}")

    project_name = matched.name
    state = matched.state

    # State-based gating
    if state == "PERMIT":
        ccm_die(
            f"{project_name} is in PERMIT state (permission dialog active). "
            "Refusing to send — typing into a permission dialog could "
            "accidentally approve or deny a tool call. Respond in the "
            "target pane first, then retry."
        )

    if state == "SHELL":
        if not auto_start:
            ccm_die(
                f"{project_name} is in SHELL state (Claude not running). "
                "Use --start to auto-launch Claude before sending."
            )
        ccm_info(f"Starting Claude in {project_name}...")
        tmux_cmd("send-keys", "-t", win_target, "-X", "cancel")
        tmux_cmd("send-keys", "-t", win_target, CLAUDE_CMD, "Enter")
        # Crude wait for Claude to initialize. Longer would block ccm
        # pipelines; shorter risks sending before the input prompt is
        # ready. 2 seconds is a reasonable compromise on modern hardware.
        time.sleep(2)

    if state == "BUSY" and not force:
        ccm_die(
            f"{project_name} is BUSY. The message would queue in the "
            "input buffer and mix with Claude's current turn. Use --force "
            "if that is what you want."
        )

    # Confirmation prompt (skip when piping or --yes)
    interactive = (
        not skip_confirm
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if interactive:
        preview = message.strip().replace("\n", " ")[:80]
        if len(message.strip()) > 80:
            preview += "..."
        tag = " (force)" if state == "BUSY" else ""
        print(f"Send to {project_name} ({state}{tag}): {preview}")
        try:
            ans = input("Proceed? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ccm_info("Cancelled")
            return
        if ans not in ("y", "yes"):
            ccm_info("Cancelled")
            return

    # Defensively exit any tmux mode on the target pane. Without this,
    # a pane stuck in copy-mode would interpret the message characters
    # as copy-mode bindings (same class of bug as the dashboard attach
    # fix in d1ca09b).
    tmux_cmd("send-keys", "-t", win_target, "-X", "cancel")

    # Literal send, converting `\n` into M-Enter (Claude Code's
    # "newline without submit" key) so the body is delivered as a
    # single multi-line prompt rather than multiple submitted turns.
    lines = message.split("\n")
    for line_i, line in enumerate(lines):
        if line:
            tmux_cmd("send-keys", "-t", win_target, "-l", line)
        if line_i < len(lines) - 1:
            tmux_cmd("send-keys", "-t", win_target, "M-Enter")

    # Final submit (unless --no-enter)
    if not no_enter:
        tmux_cmd("send-keys", "-t", win_target, "Enter")

    ccm_info(f"Sent to {project_name}")


def cmd_reset_window():
    """CLI handler for `ccm reset-window` — runs the post-attach reset
    on the current window. Internal plumbing used by the bash wrapper
    for attach paths that cannot call `reset_window_after_attach`
    directly; not user-facing."""
    session_name = tmux_cmd("display-message", "-p", "#{session_name}")
    win_idx = tmux_cmd("display-message", "-p", "#{window_index}")
    if session_name and win_idx:
        reset_window_after_attach(f"{session_name}:{win_idx}")


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
    elif cmd == "send":
        cmd_send(args)
    elif cmd == "snapshot-save":
        cmd_snapshot_save(args[0] if args else "")
    elif cmd == "snapshot-load":
        cmd_snapshot_load(args[0] if args else "")
    elif cmd == "snapshot-list":
        cmd_snapshot_list()
    elif cmd == "snapshot-delete":
        cmd_snapshot_delete(args[0] if args else "")
    elif cmd == "reset-window":
        cmd_reset_window()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
