"""Claude Code session log (JSONL) reader.

Claude Code writes one JSONL file per session under
`~/.claude/projects/<slug>/<sessionId>.jsonl`. Records are appended
at conversation turn boundaries (user prompt, assistant message,
tool_use, tool_result). The file mtime is therefore a reliable
"session activity" signal — when fresh, Claude is alive and
exchanging records, regardless of whether hooks are firing.

This module provides:
  - `read_session_info(pid)` — read `~/.claude/sessions/<pid>.json`
    runtime mapping, with a pid-reuse staleness check.
  - `read_jsonl_tail_info(project_dir, pid)` — return
    `(real_activity_age, last_assistant_stop_reason)` from the
    project's newest JSONL.
  - `read_jsonl_age(project_dir, pid)` — wrapper for callers that
    only need the age.

Limitation: pure thinking / token streaming phases do NOT update
the file (records are written at message completion, not during
generation). A stale mtime does not imply IDLE — only fresh mtime
is actionable. Detection uses this as a positive BUSY signal only.

Slug rule (verified empirically against ~/.claude/projects/):
  `/Users/alice/code/myproject` → `-Users-alice-code-myproject`
Claude Code uses the *literal* cwd at session start (no realpath
resolution), so we must NOT resolve symlinks here.
"""

import json
import os
import re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Optional, Tuple

import ccm_core  # late-bound for find_process_age (pid-reuse staleness check)
from ccm_constants import JSONL_USER_PENDING, TERMINAL_STOP_REASONS


# ─── Constants ───

JSONL_ACTIVITY_TYPES = frozenset({"user", "assistant"})

# Records ignored entirely (housekeeping that doesn't reflect
# conversation activity). Keep narrow — anything not here counts as
# activity if the type is in JSONL_ACTIVITY_TYPES.
JSONL_NON_ACTIVITY_TYPES = frozenset({
    "system/away_summary",
    "system/turn_duration",
    "system/stop_hook_summary",
    "attachment/task_reminder",
    "permission-mode",
    "file-history-snapshot",
    "last-prompt",
})

# Tail size (bytes) read from each JSONL when looking for the most
# recent real activity record. Needs to accommodate a single large
# tool_result record (Read of 2000 lines, long shell output, ...)
# plus several trailing system records — any tool-result record alone
# can easily exceed 8 KB. 32 KB covers that comfortably while
# remaining trivially cheap per detection cycle.
JSONL_TAIL_BYTES = 32768

# Safety cap on how many lines from the tail we will JSON-parse.
JSONL_TAIL_MAX_LINES = 200

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CLAUDE_SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
JSONL_CACHE_TTL = int(os.environ.get("CCM_JSONL_CACHE_TTL", "60"))

# Drift tolerance (seconds) for the session_info staleness check.
# Compares Claude Code's recorded startedAt against the live
# process's etime-derived start time. Anything beyond this means
# the .json file is from a recycled pid's prior session.
# 10 s comfortably covers normal clock drift, NTP corrections, and
# the few-second gap between fork and Claude writing session_info.
_SESSION_INFO_AGE_DRIFT_SEC = int(
    os.environ.get("CCM_SESSION_INFO_AGE_DRIFT_SEC", "10")
)

# Synthesized stop_reason value: emitted when the latest real-activity
# record is a `user` entry that landed AFTER a terminal assistant
# record. This is the signature of "user submitted a new prompt;
# `JSONL_USER_PENDING` and `TERMINAL_STOP_REASONS` live in
# `ccm_constants` — referenced from `ccm_rules` at module-load
# time and importing them from here would close a `ccm_rules →
# ccm_jsonl → ccm_core → ccm_commands → ccm_detection → ccm_rules`
# cycle.

# In-process cache: project_dir → (newest_jsonl_path, expiry_unixtime).
# Path is re-discovered on cache expiry or when the cached file is gone.
_jsonl_path_cache: dict = {}

# Cache for _parse_jsonl_tail. Key: jsonl path. Value:
# ((mtime_int, size_int), (real_activity_ts_or_None, last_stop_reason_or_None)).
# The cache hits on every detection cycle as long as the JSONL file hasn't
# been written, so the cost of tail-reading + JSON parsing is paid only
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
_jsonl_activity_cache: "OrderedDict[str, Tuple[Tuple[int, int], Tuple[Optional[int], Optional[str]]]]" = OrderedDict()


# ─── Session info file ───

def read_session_info(claude_pid, ps_lines=None):
    """Read the Claude Code runtime session file for a pid.

    Claude Code writes `~/.claude/sessions/{pid}.json` at session start
    with fields: pid, sessionId, cwd, startedAt, kind, entrypoint.
    This is the authoritative source for mapping a running Claude
    process to its session id and recorded cwd — no slug guessing,
    no symlink / worktree edge cases.

    PID-reuse defence: when `ps_lines` is provided, the file's
    `startedAt` (unix ms when Claude Code recorded its own start) is
    cross-checked against the live process's etime-derived start.
    If they disagree by more than `_SESSION_INFO_AGE_DRIFT_SEC`
    seconds the file is considered stale (a previous Claude session
    whose pid was recycled to a new claude process before the file
    was overwritten) and we return None — readers fall through to
    legacy detection rather than reading the wrong session's events.
    Without `ps_lines` the cross-check is skipped (caller had no
    `ps` snapshot to verify against, so we accept the file as-is).

    Returns a dict on success, or None if the file is missing,
    malformed, or fails the staleness check. Callers gracefully
    fall back to slug-based discovery when this returns None
    (older Claude Code versions, sandboxed execution, etc.).
    """
    if not claude_pid:
        return None
    path = os.path.join(CLAUDE_SESSIONS_DIR, f"{claude_pid}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    if ps_lines is not None:
        # Cross-check `startedAt` (unix ms Claude recorded at session
        # start) against the live process's etime-derived start time.
        # Disagreement past the drift tolerance means the json file
        # is from a previous session whose pid got recycled.
        started_at_ms = data.get("startedAt")
        if isinstance(started_at_ms, (int, float)):
            etime_seconds = ccm_core.find_process_age(claude_pid, ps_lines)
            if etime_seconds >= 0:
                live_started_unix = int(time.time()) - etime_seconds
                file_started_unix = int(started_at_ms) // 1000
                if abs(live_started_unix - file_started_unix) > _SESSION_INFO_AGE_DRIFT_SEC:
                    return None
    return data


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def read_session_versions():
    """Build a {sessionId: version} map by scanning all
    `~/.claude/sessions/*.json` files. Used by `ccm doctor` to show
    the Claude Code version each running session is on (catches the
    "ran `claude update` mid-session" case where one window is on
    a newer version than another). Bounded by the number of running
    Claude sessions (typically <10), so cost is negligible.

    ANSI escape sequences are stripped from `version` and `sessionId`
    before storing. The values come from JSON written under the user's
    home dir (same trust boundary as Claude Code itself), but a
    malformed / tampered file should not be able to inject colour
    codes or cursor moves into the doctor output.

    Skips malformed / unreadable files silently — the doctor row
    just shows no version next to that session, which the operator
    can treat as a separate signal."""
    import glob
    out: dict = {}
    for path in glob.glob(os.path.join(CLAUDE_SESSIONS_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        ver = data.get("version")
        if isinstance(sid, str) and isinstance(ver, str):
            out[_ANSI_ESCAPE_RE.sub("", sid)] = _ANSI_ESCAPE_RE.sub("", ver)
    return out


# ─── JSONL path resolution ───

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


# ─── JSONL tail parser ───

def _parse_jsonl_tail(
    path: str, mtime: int, size: int
) -> Tuple[Optional[int], Optional[str]]:
    """Tail-read a JSONL file and return:
      - unix timestamp of the most recent real-conversation-activity
        record, or None if no such record was found in the tail window
      - active stop-state at the JSONL tail:
          * `stop_reason` of the most recent assistant record (any of
            `tool_use` / `end_turn` / `max_tokens` / `stop_sequence`,
            etc.), OR
          * the synthetic value `JSONL_USER_PENDING` when the latest
            real-activity record is a `user` entry whose timestamp is
            newer than the latest assistant record AND that assistant
            had a terminal stop_reason. This indicates a fresh user
            prompt is in flight (extended-thinking case where claude
            has not written any new assistant record yet).
        None when the tail contained neither.

    Only records whose `type` is in `JSONL_ACTIVITY_TYPES`
    (whitelist) are considered for both fields; everything else
    is skipped as system / housekeeping metadata.

    Cached by (path, mtime, size): the second call with an unchanged
    mtime AND size returns the cached tuple without re-reading the
    file. A new write changes the size (JSONL is append-only during
    a session), so cache invalidation is reliable even within the
    same wall-clock second.
    """
    key = (mtime, size)
    cached = _jsonl_activity_cache.get(path)
    if cached is not None and cached[0] == key:
        _jsonl_activity_cache.move_to_end(path)
        return cached[1]

    real_ts: Optional[int] = None
    latest_user_ts: Optional[int] = None
    latest_assistant_ts: Optional[int] = None
    last_stop_reason: Optional[str] = None

    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            actual_size = f.tell()
            f.seek(max(0, actual_size - JSONL_TAIL_BYTES))
            tail_bytes = f.read()
    except OSError:
        _cache_jsonl_activity(path, key, (None, None))
        return (None, None)

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
        if not rec_type or rec_type not in JSONL_ACTIVITY_TYPES:
            continue
        # Parse timestamp — defence-in-depth in case Claude Code adds
        # a whitelisted type that omits the field.
        rec_ts: Optional[int] = None
        ts_str = rec.get("timestamp")
        if ts_str and isinstance(ts_str, str):
            try:
                iso = ts_str.replace("Z", "+00:00")
                rec_ts = int(datetime.fromisoformat(iso).timestamp())
            except (ValueError, TypeError):
                pass
        if rec_ts is None:
            continue
        if real_ts is None:
            real_ts = rec_ts
        if rec_type == "user" and latest_user_ts is None:
            latest_user_ts = rec_ts
        elif rec_type == "assistant" and latest_assistant_ts is None:
            latest_assistant_ts = rec_ts
            msg = rec.get("message") or {}
            sr = msg.get("stop_reason") if isinstance(msg, dict) else None
            if isinstance(sr, str) and sr:
                last_stop_reason = sr
        # Stop scanning once we have everything we need.
        if (real_ts is not None and latest_user_ts is not None
                and latest_assistant_ts is not None):
            break

    # Promote to JSONL_USER_PENDING when a user record is newer than
    # the latest terminal assistant — i.e. the user just submitted a
    # new prompt and claude has not written any response yet.
    if (latest_user_ts is not None and latest_assistant_ts is not None
            and latest_user_ts > latest_assistant_ts
            and last_stop_reason in TERMINAL_STOP_REASONS):
        last_stop_reason = JSONL_USER_PENDING

    result = (real_ts, last_stop_reason)
    _cache_jsonl_activity(path, key, result)
    return result


def _cache_jsonl_activity(path: str, key: Tuple[int, int],
                          value: Tuple[Optional[int], Optional[str]]) -> None:
    """Insert into _jsonl_activity_cache with LRU eviction."""
    _jsonl_activity_cache[path] = (key, value)
    _jsonl_activity_cache.move_to_end(path)
    while len(_jsonl_activity_cache) > JSONL_ACTIVITY_CACHE_MAX:
        _jsonl_activity_cache.popitem(last=False)


# ─── Public read API ───

def read_jsonl_tail_info(project_dir: str, claude_pid=None) -> Tuple[int, Optional[str]]:
    """Return `(age_seconds, last_assistant_stop_reason)` for the project's
    newest JSONL file.

      - age_seconds: seconds since the most recent real-activity record,
        or -1 if no JSONL exists or no real activity is present in the
        tail.
      - last_assistant_stop_reason: `stop_reason` string from the most
        recent `assistant` record in the tail (e.g. `"tool_use"`,
        `"end_turn"`, `"max_tokens"`), or None if none was found.

    System / housekeeping records (anything outside the
    `JSONL_ACTIVITY_TYPES` whitelist) are filtered out of both
    fields so they do NOT register as fresh activity and do NOT
    clobber the last-assistant stop_reason signal.

    `stop_reason` is the upstream signal that distinguishes "tool
    pending mid-turn" (`tool_use`) from "response complete"
    (`end_turn` / `max_tokens` / `stop_sequence`). The event-log
    detection path uses it to hold BUSY across tool-turn
    boundaries.

    When claude_pid is provided, the exact session file is resolved
    via `~/.claude/sessions/{pid}.json` (authoritative, no slug guess).
    """
    if not project_dir:
        return -1, None
    newest = _find_newest_jsonl(project_dir, claude_pid=claude_pid)
    if newest is None:
        return -1, None
    try:
        st = os.stat(newest)
    except OSError:
        return -1, None
    real_ts, stop_reason = _parse_jsonl_tail(newest, int(st.st_mtime), st.st_size)
    if real_ts is None:
        return -1, stop_reason
    return int(time.time() - real_ts), stop_reason


def read_jsonl_age(project_dir: str, claude_pid=None) -> int:
    """Thin wrapper around `read_jsonl_tail_info` that returns only the age.

    Kept for callers that do not need the stop_reason and for backward
    compatibility with the pytest suite that mocks this function
    directly. The combined accessor is preferred in new detection code
    because it shares the underlying tail-parse cache entry.
    """
    age, _ = read_jsonl_tail_info(project_dir, claude_pid=claude_pid)
    return age
