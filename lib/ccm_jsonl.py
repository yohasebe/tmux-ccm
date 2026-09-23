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
  - `read_session_versions()` — scan all `~/.claude/sessions/*.json`
    and return `{sessionId: version}` for use by `ccm doctor`.
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
import glob
import os
import re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Optional, Tuple

import ccm_core  # late-bound for find_process_age (pid-reuse staleness check)
from ccm_constants import (JSONL_INTERRUPT_RE, JSONL_INTERRUPTED,
                           JSONL_USER_PENDING, TERMINAL_STOP_REASONS)


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

# Content-tag prefixes that mark a `user` record as a local slash
# command wrapper rather than a genuine prompt. A single `/model`
# (or /status, /clear, …) writes up to three user records:
#   1. <command-name>/model</command-name>…      (isMeta absent)
#   2. <local-command-stdout>…</local-command-stdout>  (isMeta absent)
#   3. <local-command-caveat>…</local-command-caveat>  (isMeta: true)
# None of them triggers an assistant turn, so none must be counted
# as real activity nor drive the `user_pending` promotion — otherwise
# running a slash command while idle falsely shows BUSY for the whole
# BUSY_HOOK_JSONL_WINDOW (~10 min). isMeta alone only catches #3;
# the content prefix is required for #1 and #2. Genuine prompts do
# not begin with these tags.
JSONL_LOCAL_COMMAND_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<command-contents>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)

# Reverse reads stop at the first sufficient activity evidence. Each changed
# file costs at most 1 MiB of I/O and 200 non-empty record parses; a single
# record is also bounded by that byte budget. Unchanged files use the LRU.
JSONL_TAIL_BYTES = 32768
JSONL_TAIL_MAX_BYTES = 1024 * 1024
JSONL_TAIL_MAX_LINES = 200

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
#: Longest slug the CLI writes as-is. A longer one is cut here and a
#: hash of the path appended; ccm does not reproduce the hash, so a
#: directory whose slug exceeds this is one ccm cannot look up.
PROJECT_SLUG_MAX = 200
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
# ((mtime_ns, size_int), (real_activity_ts_or_None, last_stop_reason_or_None)).
# The cache hits on every detection cycle as long as the JSONL file hasn't
# been written, so the cost of tail-reading + JSON parsing is paid only
# when the file actually changes.
#
# Nanosecond mtime detects same-size rewrites; size also detects appends
# even on filesystems with coarse timestamp resolution. Rewrites preserving
# both signature fields remain indistinguishable from unchanged files.
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
    from ccm_agentview import read_registry_record
    try:
        data = read_registry_record(claude_pid)
    except (OSError, ValueError):
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

    Claude Code replaces EVERY non-alphanumeric character with `-`
    (not just `/`): `/ほげ/ふが2000` becomes
    `------2000` (one dash per CJK char, digits survive) and
    `test_project` becomes `test-project`. ccm used to replace only
    `/`, which silently missed the JSONL for any project whose path
    contains non-ASCII (incident: with the JSONL
    unresolvable, a trailing `stop` event could not be confirmed
    terminal, the pause-class branch stayed conservative, and the
    dashboard held a false BUSY indefinitely — the combined-stale
    release also needs a valid jsonl_age, so nothing ever expired it).
    Rule verified against every existing slug directory in
    ~/.claude/projects (ASCII paths are unaffected: `/` and `-` both
    map to `-`). Tilde is expanded but symlinks are NOT resolved
    (Claude Code records the cwd as-given).
    """
    expanded = os.path.expanduser(project_dir)
    return re.sub(r"[^A-Za-z0-9]", "-", expanded)


#: Default for the `session_info` parameters below: the caller has not
#: consulted the session registry, so the resolver reads it itself
#: (without the pid-reuse check, which needs a `ps` snapshot the
#: resolver does not have). A caller that has consulted it passes the
#: validated record, or None when it found nothing usable — and then
#: the registry is not read again here: a record the caller rejected
#: as another process's must not come back through this door.
UNCHECKED = object()


def _jsonl_from_session_info(claude_pid, session_info=UNCHECKED):
    """Resolve the exact JSONL path via ~/.claude/sessions/{pid}.json.

    Returns the path to the session's JSONL file, or None if the
    runtime session file is missing or does not point at an existing
    JSONL. Skips non-interactive sessions (e.g. `claude -p` headless
    runs) — they are not user-facing and ccm should ignore them.

    `session_info`: see `UNCHECKED`.
    """
    info = read_session_info(claude_pid) if session_info is UNCHECKED else session_info
    if not info:
        return None
    if info.get("kind") != "interactive":
        return None
    session_id = info.get("sessionId")
    cwd = info.get("cwd")
    if not session_id or not cwd:
        return None
    return jsonl_path_for_session(cwd, session_id)


def jsonl_path_for_session(cwd: str, session_id: str) -> Optional[str]:
    """The transcript path for one session id, or None when no such
    file exists: `<slug(cwd)>/<session_id>.jsonl` first, then any
    project directory that holds `<session_id>.jsonl` — see below."""
    if not cwd or not session_id:
        return None
    # Same sanitisation as _project_slug — Claude Code dashes every
    # non-alphanumeric character, not just `/`.
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    path = os.path.join(CLAUDE_PROJECTS_DIR, slug, f"{session_id}.jsonl")
    if os.path.exists(path):
        return path
    # The directory name is not always the sanitised cwd: a host can
    # name it itself (CLAUDE_CODE_PROJECT_DIR_NAME, 2.1.234), and the
    # rule has been read wrong before — a slug computed from the wrong
    # transformation loses the transcript, and with it the terminal
    # `stop_reason` that releases a held BUSY. That failure has no
    # timeout, so it holds until someone notices.
    #
    # The session id is what actually identifies the file, so fall
    # back to finding it by that. This adds no new assumption about
    # upstream: `<session id>.jsonl` is the same filename the primary
    # lookup already relies on. It runs only when the primary misses.
    for found in glob.glob(
            os.path.join(CLAUDE_PROJECTS_DIR, "*", f"{session_id}.jsonl")):
        return found
    return None


def _canonical(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


#: How far into a transcript to read looking for the directory it
#: records. Bytes, not lines: the records that carry no `cwd` are the
#: housekeeping ones — queued task notifications among them — and a
#: single such line can run to megabytes, so a line budget bounds the
#: wrong thing. Sessions exist whose first `cwd` sits past line 100
#: behind that housekeeping, and they are the busy ones, where
#: getting the attribution right matters most.
JSONL_CWD_PROBE_BYTES = 512 * 1024

#: What a transcript says about the directory it belongs to.
CWD_MINE, CWD_FOREIGN, CWD_SILENT = "mine", "foreign", "silent"


def _jsonl_recorded_cwd(path: str) -> Optional[str]:
    """The (canonical) directory this transcript records as its own,
    or None when it names none within the probe window or cannot be
    read. A property of the file alone — what a caller compares it
    against is the caller's business, so this is what may be cached.
    """
    # Bytes are read in bounded chunks and interpreted a complete
    # line at a time, stopping at the first record that names a
    # directory: most transcripts answer within their first line,
    # and the budget is a ceiling, not an amount to read. Iterating
    # lines instead would read a whole line before its length could
    # be checked, so one multi-megabyte record at the head would be
    # read in full. A line cut off by the budget is not interpreted —
    # it may be a `cwd` record, but that is unknown, and unknown
    # reads as "names none".
    buf = b""
    read = 0
    try:
        with open(path, "rb") as f:
            while read < JSONL_CWD_PROBE_BYTES:
                chunk = f.read(min(_CWD_PROBE_CHUNK, JSONL_CWD_PROBE_BYTES - read))
                if not chunk:
                    # EOF: a final line without a newline is complete.
                    cwd = _cwd_of_record(buf)
                    return cwd
                read += len(chunk)
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line, buf = buf[:nl], buf[nl + 1:]
                    cwd = _cwd_of_record(line)
                    if cwd is not None:
                        return cwd
    except OSError:
        return None
    return None  # budget reached with the answer not yet seen


_CWD_PROBE_CHUNK = 64 * 1024


def _cwd_of_record(raw: bytes) -> Optional[str]:
    """The canonical `cwd` a transcript record names, or None. Only a
    non-empty string counts; anything else the record carries under
    that key is not a directory."""
    if b'"cwd"' not in raw:
        return None
    try:
        cwd = json.loads(raw.decode("utf-8", errors="replace")).get("cwd")
    except (ValueError, TypeError, AttributeError):
        return None
    if isinstance(cwd, str) and cwd:
        return _canonical(cwd)
    return None


def _claim_from_cwd(recorded: Optional[str], want: str) -> str:
    if recorded is None:
        return CWD_SILENT
    return CWD_MINE if recorded == want else CWD_FOREIGN


def _jsonl_cwd_claim(path: str, want: str) -> str:
    """Whether this transcript claims `want`, claims somewhere else,
    or says nothing.

    Three answers, not two: a file that positively names another
    directory is evidence against itself, and treating that the same
    as silence is what lets the wrong project's transcript through.
    """
    return _claim_from_cwd(_jsonl_recorded_cwd(path), want)


def _find_newest_jsonl(project_dir: str, claude_pid=None, session_info=UNCHECKED):
    """Return the path to the newest *.jsonl file for this project,
    or None if there is none.

    Prefers the authoritative mapping via `~/.claude/sessions/{pid}.json`
    when claude_pid is provided and the runtime session file exists.
    Falls back to slug-based directory scanning for older Claude Code
    versions or when the pid mapping cannot be resolved.

    `session_info` (see `UNCHECKED`): a validated registry record from
    the caller, or None when the caller consulted the registry and
    found nothing usable. In the latter case the exact mapping is
    skipped — and so is a cached exact path, which may have been
    resolved from the record the caller has since rejected.

    Result is cached for JSONL_CACHE_TTL seconds; the file's mtime is
    read live each call.
    """
    now = time.time()

    # Fast path: runtime session file gives us the exact JSONL.
    if claude_pid and session_info is not None:
        exact = _jsonl_from_session_info(claude_pid, session_info)
        if exact:
            _jsonl_path_cache[project_dir] = (exact, now + JSONL_CACHE_TTL, "exact")
            return exact

    cached = _jsonl_path_cache.get(project_dir)
    if cached and session_info is None and cached[2] == "exact":
        # The caller consulted the registry and found nothing usable
        # for this pid; an exact path cached from an earlier record
        # is exactly what it may have just rejected.
        cached = None
    if cached and now < cached[1]:
        path = cached[0]
        if path is None:
            return None
        if os.path.exists(path):
            return path
        # cached path vanished — fall through to re-scan

    chosen = scan_newest_jsonl(project_dir)
    _jsonl_path_cache[project_dir] = (chosen, now + JSONL_CACHE_TTL, "scan")
    return chosen


def scan_newest_jsonl(project_dir: str, exclude_stems=frozenset(),
                      prefer_claim=True, claim_cache=None):
    """Scan the project's slug directory and return the newest
    transcript that belongs to it, or None. Uncached; the mtime and
    cwd probe run on every call, so callers on a polling path go
    through `_find_newest_jsonl`.

    `exclude_stems` drops transcripts by session id (the filename
    stem) before the newest is chosen — a caller that knows some
    files in the directory are not candidates for what it is asking
    (a background worker's own transcript, say) names them here
    rather than re-implementing the scan.

    `prefer_claim` is the detection policy: a transcript that names
    this directory beats a newer one that names none. A caller
    predicting what `claude --continue` will pick wants plain mtime
    order among the non-foreign files instead — the CLI does not
    demote a silent transcript — and passes False.

    `claim_cache`, when given, is a dict the caller keeps across
    calls: path → (mtime_ns, size, recorded cwd). A file whose mtime
    and size are unchanged reuses the directory it recorded instead
    of re-reading its head, so a caller that scans often pays one
    stat per file. What is cached is the file's own statement; the
    comparison against `project_dir` is made on every call, so two
    projects that share a slug directory (`a-b` and `a_b` do) never
    see each other's answer. The scan itself is never cached — which
    file is newest is decided from live stats every time.
    """
    slug = _project_slug(project_dir)
    session_dir = os.path.join(CLAUDE_PROJECTS_DIR, slug)
    try:
        entries = os.listdir(session_dir)
    except OSError:
        return None

    # The slug is lossy: every non-alphanumeric character becomes a
    # dash, so `a/b`, `a-b` and `a_b` all name the same directory.
    # Two projects can therefore share one, and "newest here" would
    # answer with the other one's session — its activity holding this
    # project BUSY, or its terminal stop reason releasing a BUSY that
    # is still running. A transcript records the directory it belongs
    # to, so prefer a candidate that claims this one; fall back to
    # newest only when none of them says.
    mine = quiet = None
    mine_mtime = quiet_mtime = -1.0
    want = _canonical(project_dir)
    for entry in entries:
        if not entry.endswith(".jsonl"):
            continue
        if entry[:-len(".jsonl")] in exclude_stems:
            continue
        full = os.path.join(session_dir, entry)
        try:
            st = os.stat(full)
        except OSError:
            continue
        mt = st.st_mtime
        if claim_cache is None:
            claim = _jsonl_cwd_claim(full, want)
        else:
            hit = claim_cache.get(full)
            if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                recorded = hit[2]
            else:
                recorded = _jsonl_recorded_cwd(full)
                claim_cache[full] = (st.st_mtime_ns, st.st_size, recorded)
            claim = _claim_from_cwd(recorded, want)
        if claim == CWD_MINE:
            if mt > mine_mtime:
                mine_mtime, mine = mt, full
        elif claim == CWD_SILENT:
            if mt > quiet_mtime:
                quiet_mtime, quiet = mt, full

    # A file that names another directory is never the answer, even
    # when it is the only one here: "no transcript" costs a held BUSY
    # nobody released, while the wrong project's transcript can
    # release a BUSY that is still running — and auto-exit acts on
    # that one. Silence is not disqualifying; a claim to somewhere
    # else is.
    if not prefer_claim and quiet is not None and quiet_mtime > mine_mtime:
        return quiet
    return mine or quiet


# ─── JSONL tail parser ───

def _is_local_command_user_record(rec: dict) -> bool:
    """True when a `user` record is a local slash-command wrapper
    (/model, /status, /clear, …) rather than a genuine prompt.

    Two independent signals, either sufficient:
      - `isMeta: true` (the <local-command-caveat> record), or
      - the leading text content begins with a local-command tag
        (the <command-name> / <local-command-stdout> records, which
        carry no isMeta flag).
    These records produce no assistant turn, so treating them as
    real activity would falsely surface BUSY. See
    JSONL_LOCAL_COMMAND_PREFIXES.
    """
    if rec.get("isMeta") is True:
        return True
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    text: Optional[str] = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                text = blk.get("text")
                break
            if isinstance(blk, str):
                text = blk
                break
    if not isinstance(text, str):
        return False
    return text.lstrip().startswith(JSONL_LOCAL_COMMAND_PREFIXES)


def _user_record_text(rec: dict) -> Optional[str]:
    """Leading text content of a `user` record, or None."""
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                return blk.get("text")
            if isinstance(blk, str):
                return blk
    return None


def _is_interrupt_user_record(rec: dict) -> bool:
    """True when a `user` record is Claude Code's own note that the
    turn was interrupted with Esc, not a prompt the user typed.

    This is the ONLY positive evidence an Esc-interrupted turn leaves:
    no Stop hook fires, so the event log's last entry stays start-class
    and the session reads BUSY until an aging guard eventually gives
    up. Distinguishing it here lets the normal terminal-stop_reason
    release path end the turn at once.

    Read as a genuine prompt it is actively harmful — the record is
    NEWER than the interrupted assistant turn, so it would promote to
    `user_pending` ("user just submitted, no response yet") and pin
    BUSY for the whole user-pending window.

    Matched against the WHOLE stripped text, never as a substring: a
    message that merely mentions the phrase would otherwise release a
    working session to IDLE. See JSONL_INTERRUPT_RE."""
    text = _user_record_text(rec)
    return isinstance(text, str) and bool(JSONL_INTERRUPT_RE.match(text.strip()))


def _reverse_tail_lines(path):
    """Yield complete lines newest first, without rereading expanded windows.

    A line crossing the byte limit is discarded: a suffix is not a record.
    Buffers and JSON decoding are bounded by JSONL_TAIL_MAX_BYTES.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            remaining = JSONL_TAIL_MAX_BYTES
            pending = b""
            while pos and remaining:
                count = min(pos, remaining, JSONL_TAIL_BYTES)
                pos -= count
                remaining -= count
                f.seek(pos)
                parts = (f.read(count) + pending).split(b"\n")
                pending = parts[0]
                yield from reversed(parts[1:])
            if pos == 0 and pending:
                yield pending
    except OSError:
        return


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
    interrupt_seen = False

    parsed = 0
    for line in _reverse_tail_lines(path):
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
        # Local slash commands (/model, /status, /clear, …) write
        # `user` records that produce no assistant turn. Skip them so
        # they neither count as real activity nor drive the
        # `user_pending` promotion below — otherwise running a slash
        # command while idle leaves such a record as the JSONL tail
        # and the `jsonl_user_prompt_pending` rule falsely shows BUSY
        # for the whole of that rule's window.
        if rec_type == "user" and _is_local_command_user_record(rec):
            continue
        # Esc-interrupt note: evidence the turn ENDED, so it must not
        # count as activity (it would reset the aging guard's clock to
        # the moment of the interrupt — the very thing being waited
        # out) nor as a prompt (it would promote to `user_pending`).
        # Recorded as a terminal stop_reason instead, and only when
        # nothing newer has been seen, so a later real record wins.
        if rec_type == "user" and _is_interrupt_user_record(rec):
            # Scanning newest-first, so "nothing recorded yet" means
            # nothing NEWER than this interrupt exists — neither the
            # assistant turn it cut short nor a fresh prompt that
            # would have restarted the session.
            if (last_stop_reason is None and latest_assistant_ts is None
                    and latest_user_ts is None):
                interrupt_seen = True
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
        if real_ts is not None and latest_assistant_ts is not None:
            break

    # Promote to JSONL_USER_PENDING when a user record is newer than
    # the latest terminal assistant — i.e. the user just submitted a
    # new prompt and claude has not written any response yet.
    if (latest_user_ts is not None and latest_assistant_ts is not None
            and latest_user_ts > latest_assistant_ts
            and last_stop_reason in TERMINAL_STOP_REASONS):
        last_stop_reason = JSONL_USER_PENDING

    # An interrupt note newer than everything else ends the turn, and
    # OVERRIDES the stop_reason of the assistant record it cut short.
    # That record almost always says `tool_use` — non-terminal, and
    # precisely the value that pins a session to BUSY with every
    # release path closed. `interrupt_seen` is only set when nothing
    # newer was found, so this cannot mask a resumed session.
    if interrupt_seen:
        last_stop_reason = JSONL_INTERRUPTED

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

def read_jsonl_tail_info(project_dir: str, claude_pid=None,
                         session_info=UNCHECKED) -> Tuple[int, Optional[str]]:
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
    A caller that already read and validated that file passes it as
    `session_info` (see `UNCHECKED`) so it is neither read twice nor
    read without the validation.
    """
    if not project_dir:
        return -1, None
    newest = _find_newest_jsonl(project_dir, claude_pid=claude_pid,
                                session_info=session_info)
    if newest is None:
        return -1, None
    try:
        st = os.stat(newest)
    except OSError:
        return -1, None
    real_ts, stop_reason = _parse_jsonl_tail(newest, st.st_mtime_ns, st.st_size)
    if real_ts is None:
        return -1, stop_reason
    return int(time.time() - real_ts), stop_reason


def read_jsonl_tail_info_for_session(project_dir: str, session_id: str
                                     ) -> Tuple[int, Optional[str]]:
    """Like `read_jsonl_tail_info`, but for a SPECIFIC session's JSONL
    (`<slug(project_dir)>/<session_id>.jsonl`) rather than the newest
    file in the slug directory.

    Required when several sessions share a cwd — e.g. a CCM_IGNORE'd
    sidekick running in a split pane of the same window. There,
    newest-by-mtime picks whichever session wrote last (typically the
    active sidekick), so any check that pairs the JSONL with a
    SPECIFIC session's other signals (the hook-silence canary compares
    it against that session's event log) would cross-contaminate and
    misfire. Scoping the JSONL read to the same session_id keeps the
    comparison honest. Returns `(-1, None)` when the file is absent."""
    path = jsonl_path_for_session(os.path.expanduser(project_dir), session_id) \
        if project_dir and session_id else None
    if path is None:
        return -1, None
    try:
        st = os.stat(path)
    except OSError:
        return -1, None
    real_ts, stop_reason = _parse_jsonl_tail(path, st.st_mtime_ns, st.st_size)
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
