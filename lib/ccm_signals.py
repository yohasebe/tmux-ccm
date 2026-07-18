"""ccm hook signal + event log readers.

Bash hooks under `hooks/` write four kinds of artefact for ccm's
detection pipeline to consume:

  - **Signal file** — `$HOOK_DIR/<sessionId>` — single-line
    "<unix_ts> <STATE> [detail]" written by the BUSY / PERMIT
    hooks; deleted by Stop / idle_prompt. `read_hook_signal` parses
    it.
  - **Event log** — `$HOOK_DIR/<sessionId>.events.jsonl` —
    append-only JSONL `{"ts": int, "type": str}` per hook
    invocation. `read_events_tail` returns the most recent N
    records as the input to `derive_state_from_events`.
  - **Pending sentinel** — `$HOOK_DIR/<sessionId>.pending` —
    write-only sentinel for the multi-turn Stop grace-period; ccm
    only deletes it as part of project cleanup.
  - **Notify marker** — `$CCM_NOTIFY_MARKER_DIR/<md5(cwd)>` —
    "<unix_ts> <STATE>" written by `_ccm_instant_notify` when a
    hook-driven desktop notification fires. Read by inject_status
    polling so the Python side does not duplicate a notification
    the bash hook already sent.

`session_id` resolution: hook artefacts are keyed on Claude Code's
session UUID (stable for the session's lifetime, distinct across
sessions). `_session_id_from_tmux` reads the cached
`@ccm_session_id` tmux window option populated by the slow
detection path; the live pid → sessionId chain
(`pane → claude pid → ~/.claude/sessions/<pid>.json`) lives in
`ccm_jsonl::read_session_info` and is invoked from
`ccm_detection::build_detection_context`.

`cleanup_project_runtime_files` is the project-wide unlinker
called from `cmd_unregister` / `cmd_remove`. It targets the
session's hook artefacts via the cached `@ccm_session_id` plus
the cwd-keyed caches (notify marker, git, port) in one place, so
project deletion leaves no runtime trace behind.

Cross-module discipline: `ccm_core` is imported for late-bound
access to `tmux_cmd` / `md5_hash` / runtime directory constants,
keeping test mocks routed via `ccm_core` reachable.
"""

import json
import os
from collections import OrderedDict
from typing import Optional, Tuple

import ccm_core  # late-bound for tmux_cmd / md5_hash / CCM_HOOK_DIR / etc.


# ─── Project-dir resolution ───

def _resolve_project_dir(project_dir):
    """Expand and resolve a project directory path."""
    expanded = os.path.expanduser(project_dir)
    try:
        expanded = os.path.realpath(expanded)
    except OSError:
        pass
    return expanded


# ─── Hook signal ───
# Signal file format: "<unix_timestamp> <STATE> [extra_fields...]"
# - Fields are space-separated; first two are required
# - STATE: one of BUSY, PERMIT (Stop hook deletes the file instead of writing)
# - Extra fields are reserved for future use and ignored by current code
# Written by: hooks/on-prompt-submit.sh, hooks/on-pre-tool-use.sh,
#             hooks/on-notification.sh (PERMIT only)
# Deleted by: hooks/on-stop.sh, hooks/on-notification.sh (idle_prompt)

VALID_HOOK_STATES = {"BUSY", "PERMIT", "SHELL"}

_CCM_SESSION_ID_OPTION = "@ccm_session_id"


def _session_id_from_tmux(project_dir: str) -> Optional[str]:
    """Look up the cached session_id for a project window via the
    `@ccm_session_id` tmux option. Returns None when no window
    matches (project removed) or no slow-path scan has populated
    the cache yet."""
    if not project_dir:
        return None
    expanded = _resolve_project_dir(project_dir)
    raw = ccm_core.tmux_cmd(
        "list-windows", "-a", "-F",
        "#{@ccm_dir}\t#{" + _CCM_SESSION_ID_OPTION + "}",
    )
    if not raw:
        return None
    for line in raw.split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        d, sid = parts[0], parts[1]
        if not d:
            continue
        if _resolve_project_dir(d) == expanded:
            return sid or None
    return None


def _hook_signal_path(project_dir, session_id: Optional[str] = None):
    """Get the hook signal file path for a project window.

    `session_id` semantics:
      - non-empty string → use directly (caller resolved it).
      - ``""``           → caller authoritatively reports "no session";
                            return None without any tmux lookup.
      - ``None``         → caller did not resolve; fall back to the
                            cached `@ccm_session_id` tmux option.
    Returns None whenever no session_id is known — callers must treat
    that as "no signal available" rather than reading a default path.
    """
    if session_id is None:
        session_id = _session_id_from_tmux(project_dir)
    if not session_id:
        return None
    return os.path.join(ccm_core.CCM_HOOK_DIR, session_id)


def read_hook_signal(project_dir, session_id: Optional[str] = None):
    """Read hook signal file. Returns (timestamp, state, detail) or None.
    Detail is optional extra info (e.g., tool name for PERMIT).

    Caller may pass `session_id` directly (slow path, after resolving
    via pid chain). Otherwise resolved via the `@ccm_session_id`
    tmux option set by the most recent slow path. Returns None when
    no session_id is yet known for this project — fresh sessions
    before the first hook fire fall through to legacy detection.
    """
    hook_file = _hook_signal_path(project_dir, session_id=session_id)
    if not hook_file:
        return None
    try:
        with open(hook_file, encoding="utf-8") as f:
            content = f.read().strip()
        parts = content.split(None, 2)  # split into at most 3 parts
        if len(parts) >= 2 and parts[1] in VALID_HOOK_STATES:
            detail = parts[2] if len(parts) >= 3 else ""
            return int(parts[0]), parts[1], detail
    except (OSError, ValueError):
        pass
    return None


# ─── Notify marker ───
# Per-project marker `$CCM_NOTIFY_MARKER_DIR/<md5(cwd)>` written by
# `hooks/lib.sh::_ccm_instant_notify` when the bash hook fires a
# desktop notification. inject_status polling reads this to suppress
# a duplicate Python-side notification within the dedup window.
# Per-project scoping (vs a global marker) is required so project A's
# completion does not silently suppress project B's notification.

def read_project_notify_marker(project_dir):
    """Read the per-project instant-notify marker. Returns
    `(timestamp, state)` or None if missing/unparseable."""
    if not project_dir:
        return None
    expanded = _resolve_project_dir(project_dir)
    marker_path = os.path.join(
        ccm_core.CCM_NOTIFY_MARKER_DIR, ccm_core.md5_hash(expanded)
    )
    try:
        with open(marker_path, encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return None
    parts = content.split(None, 1)
    if len(parts) < 2:
        return None
    try:
        return (int(parts[0]), parts[1])
    except (ValueError, TypeError):
        return None


# ─── Event log reader ───
# The per-session event log is written by
# `hooks/lib.sh::ccm_append_event` as append-only JSONL at
# `$HOOK_DIR/<sessionId>.events.jsonl`. Each record is
# `{"ts": unix_seconds, "type": <normalized_type>}` plus an optional
# `"mode"` (the payload's `permission_mode`, sanitized bash-side),
# one per hook invocation. State is derived as a pure function of
# the event tail by `derive_state_from_events`, which keys on
# "type" only — extra fields are opaque to detection; this reader
# is the input side.
#
# The reader uses the same tail-read + bounded-cache pattern as
# `_parse_jsonl_tail` so per-cycle overhead is ~0 on cache hit and a
# single 8 KB seek + JSON parse on miss. The cache key is
# (mtime_int, size) which is guaranteed to invalidate on any append
# because the file is monotonically growing.

EVENTS_TAIL_BYTES = 8192       # ~200 events at typical line length
EVENTS_TAIL_MAX_LINES = 200    # parse cap per cycle
EVENTS_CACHE_MAX = 128
_events_cache: "OrderedDict[str, Tuple[Tuple[int, int], Tuple[dict, ...]]]" = OrderedDict()


def _events_log_path(project_dir: str,
                     session_id: Optional[str] = None) -> Optional[str]:
    """Return the absolute path of a project's event log file.

    Keyed on Claude Code's session_id (UUID per session, stable for
    the session's lifetime). `session_id` semantics match
    `_hook_signal_path`: non-empty → use, ``""`` → "no session"
    (skip tmux lookup), ``None`` → fall back to cached tmux option.
    Returns None whenever no session_id is known.
    """
    if session_id is None:
        session_id = _session_id_from_tmux(project_dir)
    if not session_id:
        return None
    return os.path.join(ccm_core.CCM_HOOK_DIR, session_id + ".events.jsonl")


def _cache_events(path: str, key: Tuple[int, int],
                  value: Tuple[dict, ...]) -> None:
    _events_cache[path] = (key, value)
    _events_cache.move_to_end(path)
    while len(_events_cache) > EVENTS_CACHE_MAX:
        _events_cache.popitem(last=False)


def read_events_tail(project_dir: str, limit: int = 20,
                     session_id: Optional[str] = None) -> Tuple[dict, ...]:
    """Return the last `limit` events from a project's event log.

    Each event is a dict `{"ts": int, "type": str}`. Malformed lines
    are silently skipped. Returns an empty tuple when no log exists
    for the project (hook not installed, no event yet written, or
    session_id not yet resolved).

    Caller may pass `session_id` directly (slow path) or rely on the
    `@ccm_session_id` tmux option set by the most recent slow path.

    Why a tuple rather than a list: the result is cached and must be
    immutable against accidental caller mutation.

    Tail strategy: read the last EVENTS_TAIL_BYTES (8 KB) of the file
    and parse forward, then slice the most recent `limit`. An
    append-only log grows monotonically so the tail always contains
    the most recent events even if the file exceeds the tail window.
    """
    if not project_dir:
        return ()
    path = _events_log_path(project_dir, session_id=session_id)
    if not path:
        return ()
    try:
        st = os.stat(path)
    except OSError:
        return ()
    size = st.st_size
    mtime = int(st.st_mtime)
    key = (mtime, size)
    cached = _events_cache.get(path)
    if cached is not None and cached[0] == key:
        _events_cache.move_to_end(path)
        events = cached[1]
        return events[-limit:] if limit and len(events) > limit else events

    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            actual_size = f.tell()
            f.seek(max(0, actual_size - EVENTS_TAIL_BYTES))
            tail_bytes = f.read()
    except OSError:
        _cache_events(path, key, ())
        return ()

    tail = tail_bytes.decode("utf-8", errors="ignore")
    lines = tail.split("\n")
    # Drop the leading partial line if we read mid-file.
    if size > EVENTS_TAIL_BYTES and len(lines) > 1:
        lines = lines[1:]

    # Walk newest-first so the EVENTS_TAIL_MAX_LINES parse cap keeps
    # the most recent records rather than dropping them. Final list
    # is reversed back to chronological order for callers.
    events_newest_first: list = []
    for line in reversed(lines):
        if len(events_newest_first) >= EVENTS_TAIL_MAX_LINES:
            break
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        t = rec.get("type")
        ts = rec.get("ts")
        if not isinstance(t, str) or not t:
            continue
        if not isinstance(ts, (int, float)):
            continue
        out_rec = {"ts": int(ts), "type": t}
        # Optional permission-mode annotation. Preserved for display
        # readers (`read_latest_permission_mode`); detection ignores
        # any key other than ts/type.
        mode = rec.get("mode")
        if isinstance(mode, str) and mode:
            out_rec["mode"] = mode
        events_newest_first.append(out_rec)

    events_newest_first.reverse()
    result = tuple(events_newest_first)
    _cache_events(path, key, result)
    return result[-limit:] if limit and len(result) > limit else result


def read_latest_permission_mode(project_dir: str,
                                session_id: Optional[str] = None) -> str:
    """Return the most recent `permission_mode` recorded in a
    project's event log, or "" when none is known.

    Display-layer helper for the dashboard / `ccm status` mode badge.
    The value is whatever the newest mode-bearing hook event carried —
    a mid-session mode change (shift+tab) stays stale until the next
    hook fires, which is acceptable for a secondary indicator (the
    state model itself never reads this). Goes through
    `read_events_tail`, so on a detection-warmed cache this costs a
    dict lookup, not file I/O.
    """
    for rec in reversed(read_events_tail(project_dir,
                                         session_id=session_id)):
        mode = rec.get("mode")
        if isinstance(mode, str) and mode:
            return mode
    return ""


# ─── Project-wide runtime cleanup ───

def cleanup_project_runtime_files(project_dir):
    """Remove all runtime files for a project window.

    Called from `cmd_unregister` and `cmd_remove`. Targets two
    distinct keying schemes:

    1. Hook artefacts keyed on Claude Code session_id
       (`$HOOK_DIR/<sessionId>`, `.events.jsonl`, `.busy`, `.pending`).
       Discovered via the cached `@ccm_session_id` tmux option for
       this project's window. Stale past-session files persist until
       macOS `$TMPDIR` auto-cleanup; this is acceptable since the
       resolver only matches the live session.

    2. Caches keyed on `md5(project_dir)` (git-cache, port-cache,
       notify-marker). These were already cwd-keyed on the ccm
       side and stay that way — they identify the project window,
       not the session.

    Each removal is independent and guarded against OSError so a
    missing file (normal case for inactive projects) is a silent
    no-op; one failure does not block the rest.
    """
    if not project_dir:
        return
    expanded = _resolve_project_dir(project_dir)
    cwd_key = ccm_core.md5_hash(expanded)

    session_ids = set()
    cached_sid = _session_id_from_tmux(project_dir)
    if cached_sid:
        session_ids.add(cached_sid)

    for sid in session_ids:
        for suffix in ("", ".busy", ".pending", ".events.jsonl"):
            path = os.path.join(ccm_core.CCM_HOOK_DIR, sid + suffix)
            try:
                os.unlink(path)
            except OSError:
                pass

    # Caches keyed on cwd: notification marker, git branch, ports.
    for directory in (
        ccm_core.CCM_NOTIFY_MARKER_DIR,
        ccm_core.CCM_GIT_CACHE_DIR,
        ccm_core.CCM_PORT_CACHE_DIR,
    ):
        try:
            os.unlink(os.path.join(directory, cwd_key))
        except OSError:
            pass
