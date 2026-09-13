"""Read-only access to Claude Code's per-user agent-view daemon.

Claude Code 2.1.139 introduced an "agent view" (`claude agents`,
`claude --bg <prompt>`, `claude attach <short>`) that runs sessions
as workers under a per-user supervisor daemon. The daemon writes
its roster to `~/.claude/daemon/roster.json` and each session's
job state to `~/.claude/jobs/<short>/state.json`.

ccm reads these files to surface the background-session list in the
dashboard, and to tell when a project's `claude --continue` will not
resume its conversation because the newest transcript was handed to
a session that is still live (see the hand-off check at the end of
this module, which reads the CLI's session registry for that). This module is strictly read-only: it never writes to
`~/.claude` and never sends signals to the daemon. The display in
ccm's dashboard is a passive observer; dispatch / lifecycle stays
the responsibility of Claude Code's own CLI (`claude attach`,
`claude stop`, `claude agents`).

Schema (observed on Claude Code 2.1.139):

  roster.json
    {
      "proto": 1,
      "supervisorPid": int,
      "updatedAt": <epoch ms>,
      "workers": {
        "<short8>": {
          "pid": int, "sessionId": "<uuid>", "cwd": "...",
          "startedAt": <epoch ms>, "cliVersion": "2.1.139",
          "dispatch": { "seed": { "name": "...", "intent": "..." },
                        "source": "slash|bg|attach", ... },
          ...
        }, ...
      }
    }

  jobs/<short>/state.json
    {
      "state": "working|blocked|done|failed|stopped|...",
      "tempo": "active|idle|blocked",
      "needs": "the exact ask, when blocked",
      "name": "auto-generated label",
      "cwd": "...", "sessionId": "<uuid>",
      "createdAt": "<ISO-8601>", "updatedAt": "<ISO-8601>", ...
    }

Workers are removed from `roster.json` after ~1 hour of idle
(`settled (done)` in `~/.claude/daemon.log`), but the per-session
`state.json` persists on disk. `list_bg_sessions()` iterates the
roster so only currently-active sessions surface — matching the
behavior of `claude agents` itself.
"""

import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional


# Claude Code generates the worker short ID as the first 8 chars of
# the session UUID, so the on-disk form is always lower-case hex.
# We accept 4–16 chars to be forward-compatible with a possible
# upstream length change, but require the character class strictly
# so a malformed roster key (a hypothetical upstream bug, or a
# manual tampering attempt) can never reach the `claude attach
# <short>` command we send via tmux send-keys — that path would
# interpret shell metachars in the receiving pane.
_SHORT_RE = re.compile(r"^[0-9a-f]{4,16}$")


def is_valid_short(short) -> bool:
    """True when `short` is safe to embed in a shell-bound command.
    Used by both the reader (drop malformed roster entries) and the
    dashboard (defence-in-depth before the tmux send-keys hop)."""
    return isinstance(short, str) and bool(_SHORT_RE.match(short))


# ─── Paths ───
# Anchored at $HOME (not $CLAUDE_HOME) because the daemon hardcodes
# this layout. If Claude Code ever introduces a configurable root,
# this is the single point to update.

DAEMON_DIR = os.path.expanduser("~/.claude/daemon")
DAEMON_ROSTER_PATH = os.path.join(DAEMON_DIR, "roster.json")
DAEMON_STATUS_PATH = os.path.expanduser("~/.claude/daemon.status.json")
JOBS_DIR = os.path.expanduser("~/.claude/jobs")


# ─── State normalization ───
# Map state.json `state` values to short labels used in display.
# Unknown / unset values fall through to "UNKNOWN" so the column
# stays a fixed-width word.

STATE_LABEL_MAP = {
    "working": "WORKING",
    # `blocked` is the CLI's "someone other than the agent owns the
    # next step": a reply or approval it is waiting for, or a login,
    # billing or rate-limit condition the person has to clear. All
    # of those want the person's attention, so they read as NEEDS;
    # the `needs` field carries the exact ask.
    "blocked": "NEEDS",
    "needs_input": "NEEDS",
    "idle": "IDLE",
    "done": "DONE",
    # A stopped session was ended, not finished: it is neither a
    # success nor a failure, and reads as neither.
    "stopped": "STOPPED",
    "failed": "FAILED",
}

# Display icons mirror the upstream agent-view TUI's iconography
# (✽ for working / ✻ for needs input / etc.) so users moving between
# `claude agents` and ccm dashboard see consistent symbols.
STATE_ICONS = {
    "WORKING": "✽",
    "NEEDS": "✻",
    "IDLE": "●",
    "DONE": "✓",
    "FAILED": "✕",
    "STOPPED": "■",
    "UNKNOWN": "?",
}

# Priority for sorting / aggregation: actionable states first, then
# in-progress, then quiescent. Used by the dashboard renderer to put
# NEEDS sessions at the top of the list.
STATE_PRIORITY = {
    "NEEDS": 0,
    "WORKING": 1,
    "IDLE": 2,
    "DONE": 3,
    "FAILED": 4,
    "STOPPED": 5,
    "UNKNOWN": 6,
}


@dataclass
class BgSession:
    """One row in the agent-view roster, joined with its job state.json."""
    short: str               # 8-char short ID (roster key)
    pid: int                 # worker process pid (0 if missing)
    cwd: str                 # absolute cwd
    name: str                # human-readable label
    state: str               # normalized: WORKING / NEEDS / IDLE / DONE / FAILED / STOPPED / UNKNOWN
    raw_state: str           # lowercase string from state.json (debug aid)
    tempo: str               # "active" / "idle" / ""
    cli_version: str         # e.g. "2.1.139"
    session_id: str          # full UUID
    created_at: Optional[float]   # unix seconds (None if unparseable)
    updated_at: Optional[float]   # unix seconds (None if unparseable)
    source: str              # dispatch.source: "slash" / "bg" / "attach" / ""
    needs: str = ""          # the exact ask when blocked (state.json `needs`), else ""


def _safe_load_json(path):
    """Parse a JSON file. Returns `{}` on any read / parse failure.

    Used for files the daemon owns and rewrites atomically — we may
    momentarily see a partial write between the daemon's `rename`,
    and any structural difference between releases should not crash
    ccm's display."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_roster() -> dict:
    """Return parsed `~/.claude/daemon/roster.json`, or `{}`."""
    return _safe_load_json(DAEMON_ROSTER_PATH)


def job_record_exists(short: str) -> bool:
    """True when the daemon has written a job document for `short`.

    Existence, not parseability: a corrupt state file still means a
    job is there, and hiding it because it cannot be read is the
    failure this check exists to avoid, not the one it should cause.
    """
    return os.path.exists(os.path.join(JOBS_DIR, short, "state.json"))


def read_job_state(short: str) -> dict:
    """Return parsed `~/.claude/jobs/<short>/state.json`, or `{}`.

    `short` is validated to be a bare basename (no path components,
    no leading dots) so a malformed roster key can never escape the
    jobs directory.
    """
    if not short or not isinstance(short, str):
        return {}
    if "/" in short or "\\" in short or short.startswith("."):
        return {}
    return _safe_load_json(os.path.join(JOBS_DIR, short, "state.json"))


def _parse_iso_ts(s):
    """Parse an ISO-8601 timestamp ('2026-05-12T01:51:34.559Z') into
    unix seconds. Returns None on failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        from datetime import datetime
        # Python's fromisoformat doesn't accept 'Z' in <3.11; the
        # explicit replacement is portable and a no-op on newer ones.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _normalize_state(raw) -> str:
    if not raw:
        return "UNKNOWN"
    return STATE_LABEL_MAP.get(str(raw).lower(), "UNKNOWN")


def _seed_name(info: dict) -> str:
    """Reach into roster's nested dispatch.seed.name without throwing
    on partial / malformed shapes."""
    dispatch = info.get("dispatch") if isinstance(info, dict) else None
    if not isinstance(dispatch, dict):
        return ""
    seed = dispatch.get("seed")
    if not isinstance(seed, dict):
        return ""
    return str(seed.get("name") or "")


def _dispatch_source(info: dict) -> str:
    dispatch = info.get("dispatch") if isinstance(info, dict) else None
    if not isinstance(dispatch, dict):
        return ""
    return str(dispatch.get("source") or "")


def list_bg_sessions() -> List[BgSession]:
    """Return all currently-active background sessions.

    Iterates roster.json `workers` (the daemon's live view); enriches
    each entry with its `jobs/<short>/state.json`. Sessions removed
    from the roster after `settled (done)` do NOT surface — matching
    the upstream `claude agents` TUI's own filter.

    Returns `[]` when no daemon is running (no roster file).
    Read-only and side-effect-free; safe to call at any cadence.
    """
    roster = read_roster()
    workers = roster.get("workers", {})
    if not isinstance(workers, dict):
        return []

    out = []
    for short, info in workers.items():
        if not isinstance(info, dict):
            continue
        # Drop entries whose short key doesn't match Claude's
        # documented form. Tampered / malformed shorts must not
        # propagate to the `claude attach <short>` shell command.
        if not is_valid_short(short):
            continue
        state_doc = read_job_state(short)
        # A worker is only a session once a task has claimed it. The
        # daemon also keeps unclaimed workers warm so the next
        # dispatch starts fast, and those are not work anyone is
        # waiting on — listing them puts a nameless row in front of
        # the reader and invites `claude attach` on nothing.
        #
        # Claimed is read as "has a job document OR a dispatch
        # record", which is deliberately the weaker of the two tests:
        # a job whose dispatch has landed but whose state file has
        # not yet been written is still shown. An unclaimed worker
        # carrying a dispatch would slip through, and this is written
        # without one to look at.
        if not job_record_exists(short) and not info.get("dispatch"):
            continue
        raw_state = state_doc.get("state", "")
        # Prefer the job's `name` (auto-generated by Claude after the
        # first turn); fall back to the dispatch seed name (set at
        # dispatch time, may be empty for `--bg` calls that didn't
        # supply one).
        name = (
            str(state_doc.get("name") or "").strip()
            or _seed_name(info)
        )
        created_at = _parse_iso_ts(state_doc.get("createdAt"))
        if created_at is None:
            ms = info.get("startedAt")
            if isinstance(ms, (int, float)) and ms > 0:
                created_at = float(ms) / 1000.0
        updated_at = _parse_iso_ts(state_doc.get("updatedAt"))
        try:
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        out.append(BgSession(
            short=str(short),
            pid=pid,
            cwd=str(info.get("cwd") or state_doc.get("cwd") or ""),
            name=name,
            state=_normalize_state(raw_state),
            raw_state=str(raw_state) if raw_state else "",
            tempo=str(state_doc.get("tempo") or ""),
            needs=" ".join(str(state_doc.get("needs") or "").split()),
            cli_version=str(info.get("cliVersion") or ""),
            session_id=str(info.get("sessionId") or state_doc.get("sessionId") or ""),
            created_at=created_at,
            updated_at=updated_at,
            source=_dispatch_source(info),
        ))

    out.sort(key=lambda w: (STATE_PRIORITY.get(w.state, 9),
                            -(w.updated_at or 0)))
    return out


def daemon_running() -> bool:
    """Heuristic: does the per-user agent-view daemon appear active?

    A roster file with at least one worker is the strongest signal.
    Empty roster + `daemon.status.json` present is also a yes (the
    daemon is up but idle); both files missing means no.
    """
    if os.path.exists(DAEMON_ROSTER_PATH):
        return True
    if os.path.exists(DAEMON_STATUS_PATH):
        return True
    return False


# ─── `claude --continue` handoff check ───
#
# A session sent to the background with `/bg` leaves a hand-off
# record at the end of its transcript:
#
#   {"type":"continued-in","sessionId":<old>,"continuedInSessionId":<bg>}
#
# `claude --continue` walks the directory's transcripts newest first,
# and when the newest one hands off to a session the daemon still
# lists as live, it stops there: nothing is resumed, a notice names
# the background session, and a fresh session starts. Older
# transcripts are not considered. The background worker counts as
# live for as long as it sits in the roster — including after its
# task is `done` — so the launch command ccm types on attach can
# quietly produce an empty session for a project whose conversation
# is intact on disk.
#
# ccm cannot ask `claude --continue` what it would do (there is no
# dry run), so this reads the same two facts the CLI reads: the
# newest transcript's hand-off, and the roster. The check is
# narrower than the CLI's on purpose — it names a session only when
# the hand-off is the last conversation record — so a mismatch costs
# a missed notice, never a false one.

#: How much of the transcript tail to read for the hand-off record.
#: The records after a hand-off are housekeeping (cost, last prompt)
#: and small. A hand-off further back than this is not judged — the
#: miss side, by design.
CONTINUED_IN_TAIL_BYTES = 64 * 1024

# Line-level pre-filters. The CLI writes compact JSON, but the record's
# parsed `type` decides — the literal only chooses which lines to parse.
_CONTINUED_IN_RE = re.compile(r'"type"\s*:\s*"continued-in"')
_CONVERSATION_RE = re.compile(r'"type"\s*:\s*"(?:user|assistant)"')

#: Per-file parse results, keyed by path and valid for one
#: (mtime_ns, size). Only statements a file makes about itself are
#: kept — the directory it records, the session its tail hands off
#: to — never anything relative to a project or to the roster.
#: Which transcript is newest, whose directory it names, and whether
#: its hand-off target is in the roster are decided from live stats
#: and the live roster on every call. A positive answer that
#: outlived the state it described was the failure this design
#: replaces; the cost that remains is one stat per transcript per
#: call, for SHELL projects that have a worker in the roster.
#:
#: Known limits of the (mtime_ns, size) key: a rewrite that keeps
#: both is not seen, so either parse result — the recorded cwd or
#: the hand-off target, positive or None — can go stale until the
#: file's signature moves; a transient tail-read failure is
#: remembered as "no hand-off" the same way. The recorded cwd is
#: also a canonical path, so a re-pointed symlink stales it without
#: the file changing. None of these is a shape the CLI has been seen
#: to produce; they are named so the key's reach is not overstated.
_claim_cache = {}      # path → (mtime_ns, size, recorded cwd or None)
_handoff_cache = {}    # path → (mtime_ns, size, target or None)

#: Registry root: the CLI's config home. `CLAUDE_CONFIG_DIR` moves it;
#: ccm's transcript readers are anchored at `~/.claude`, so when the
#: two differ the notice has no matching pair of facts to read and is
#: withheld (see `continue_blocker`).
DEFAULT_CLAUDE_HOME = os.path.expanduser("~/.claude")


def claude_config_home_is_default() -> bool:
    """True when the CLI's config home is `~/.claude`: the variable
    is unset, or is an absolute path whose real location is that
    directory. The CLI takes the variable's string as-is — no `~`
    expansion, an empty string is not "unset", a relative path is
    resolved against the launching process's cwd, which ccm does not
    know — so every one of those forms reads as "not default" here,
    and the notice is withheld. Read from ccm's own environment; the
    launching shell's is not visible from here."""
    if "CLAUDE_CONFIG_DIR" not in os.environ:
        return True
    override = os.environ["CLAUDE_CONFIG_DIR"]
    if not override or not os.path.isabs(override):
        return False
    try:
        return os.path.realpath(override) == os.path.realpath(DEFAULT_CLAUDE_HOME)
    except OSError:
        return False


#: The session kinds the CLI's registry reader recognises. Any other
#: value normalises to "no kind" there, which makes the CLI treat the
#: whole live set as unknown.
REGISTRY_KINDS = frozenset({"interactive", "bg", "daemon", "daemon-worker"})
_ASCII_DIGITS = re.compile(r"[0-9]+")


def own_pid_domain() -> Optional[str]:
    """The pid domain the CLI records for processes on this host, or
    None when ccm cannot derive it. macOS records the platform name;
    elsewhere the CLI derives it from machine and pid-namespace ids
    that ccm does not reproduce."""
    return "darwin" if sys.platform == "darwin" else None


def process_start_token(pid) -> Optional[str]:
    """The process start time in the form the CLI records as
    `procStart` (`LC_ALL=C TZ=UTC ps -o lstart= -p <pid>`), or None
    when it cannot be read."""
    try:
        r = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, timeout=5,
            env={"LC_ALL": "C", "TZ": "UTC", "PATH": "/bin:/usr/bin:/usr/sbin"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    token = r.stdout.decode("utf-8", errors="replace").strip()
    return token or None


def _process_is_live(pid) -> bool:
    """The CLI's own existence test: pid > 1 and `kill(pid, 0)` raises
    nothing. Any error — a missing process, or one ccm may not signal
    — counts as not live, as it does for the CLI."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError, ValueError):
        # OverflowError: a number too large for a pid — no process.
        return False
    return True


def live_noninteractive_session_ids():
    """Session ids of the non-interactive Claude sessions that are
    live right now — the set a hand-off target is checked against —
    or None when that set cannot be known.

    Read from the CLI's own session registry, `<config home>/sessions/
    <pid>.json`, which is what `claude --continue` consults
    (`listAllLiveSessions`): a record per running process. Not from
    the daemon's roster — that outlives the daemon and lists dead
    workers — and not from `claude agents --json`, which folds saved
    jobs into its output and, being the CLI, may sweep the registry
    as a side effect.

    The CLI does not answer with a partial set: a record it cannot
    read, or a live record without a session id or with a kind it
    does not recognise, makes it treat the whole set as unknown, and
    `--continue` then does not stop at a hand-off. So does this: any
    such record → None, and the caller reports nothing. The process
    is the one the file is named after (a canonical `<pid>.json`;
    the CLI ignores other spellings, and a body that names a
    different pid is a record ccm cannot vouch for → None). A record
    counts as live only on the CLI's own terms — the process exists
    (`kill(pid, 0)`, pid > 1, no error of any kind) and its start
    time still matches the record (`procStartFt` when present, else
    `procStart`, as the CLI chooses) — and, more strictly than the
    CLI, only when that start time can be read and compared: an
    unreadable token, a record without one, or a record from another
    pid domain is not counted (a missed notice, never a false one).
    Read-only.
    """
    import ccm_jsonl
    try:
        entries = os.listdir(ccm_jsonl.CLAUDE_SESSIONS_DIR)
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    live = set()
    domain = own_pid_domain()
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        stem = entry[:-len(".json")]
        # The CLI enumerates /^\d+\.json$/ — ASCII digits only
        # (`str.isdigit` would also accept superscripts and the like)
        # — and adopts only the canonical spelling of the number.
        if not _ASCII_DIGITS.fullmatch(stem) or stem != str(int(stem)):
            continue
        pid = int(stem)
        path = os.path.join(ccm_jsonl.CLAUDE_SESSIONS_DIR, entry)
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(info, dict):
            return None
        kind = info.get("kind")
        if not isinstance(kind, str) or kind not in REGISTRY_KINDS:
            return None
        if kind == "interactive":
            continue
        sid = info.get("sessionId")
        if not isinstance(sid, str) or not sid:
            return None
        body_pid = info.get("pid")
        if body_pid is not None and body_pid != pid:
            return None
        record_domain = info.get("pidDomain")
        if record_domain is not None and (domain is None or record_domain != domain):
            continue
        if not _process_is_live(pid):
            continue
        recorded = info.get("procStartFt")
        if recorded is None:
            recorded = info.get("procStart")
        if not isinstance(recorded, str) or not recorded:
            continue
        token = process_start_token(pid)
        if token is None or token != recorded:
            continue
        live.add(sid)
    return live


def continued_in_target(jsonl_path) -> Optional[str]:
    """Session id the transcript hands off to, or None.

    Reads the tail and walks it backwards. A hand-off record wins
    only when no conversation record (user / assistant) follows it —
    a hand-off that has since been talked past does not block the
    CLI, and is not reported here. Unreadable → None (miss side).
    """
    try:
        st = os.stat(jsonl_path)
    except OSError:
        return None
    hit = _handoff_cache.get(jsonl_path)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    target = _read_continued_in_target(jsonl_path, st.st_size)
    _handoff_cache[jsonl_path] = (st.st_mtime_ns, st.st_size, target)
    return target


def _read_continued_in_target(jsonl_path, size) -> Optional[str]:
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - CONTINUED_IN_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    if not _CONTINUED_IN_RE.search(tail):
        return None
    for line in reversed(tail.split("\n")):
        if _CONTINUED_IN_RE.search(line):
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if isinstance(rec, dict) and rec.get("type") == "continued-in":
                target = rec.get("continuedInSessionId")
                return target if isinstance(target, str) and target else None
        # A conversation record after the hand-off (or a message that
        # merely quotes the marker) ends the walk: the CLI stops here too.
        if _CONVERSATION_RE.search(line):
            return None
    return None


class _LiveIds:
    """Reads the registry once, on first need, and remembers the
    answer (a set, or None for unknown) for the rest of one
    `continue_blockers` pass."""
    __slots__ = ("_ids", "_asked")

    def __init__(self, ids=None):
        self._ids, self._asked = ids, ids is not None

    def get(self):
        if not self._asked:
            self._ids, self._asked = live_noninteractive_session_ids(), True
        return self._ids


def continue_blocker(project_dir: str, bg_sessions=None,
                     live_ids=None) -> Optional[BgSession]:
    """The live background session that will make `claude --continue`
    in `project_dir` start a fresh session, or None.

    Three facts, from three places, in the order that keeps the
    expensive one last:

      1. the roster (`bg_sessions`, read here when omitted) — a
         worker for this directory must be listed. Necessary, not
         sufficient: the roster outlives the daemon, so a listed
         worker may be long dead. Used only to decide whether the
         rest is worth doing, and to name the session in the notice;
      2. the newest transcript's tail — it must hand off to that
         worker's session;
      3. the CLI's own session registry (`live_ids`, a `_LiveIds`,
         read only when 1 and 2 hold) — the hand-off target must be
         a live non-interactive session there. This is the check
         `claude --continue` makes; when the registry cannot be
         read as a whole (None), nothing is reported.

    Withheld entirely when `CLAUDE_CONFIG_DIR` moves the CLI's home:
    the transcripts and registry ccm reads live under `~/.claude`,
    and facts from one home say nothing about a launch in another.

    Only per-file parse results are cached (see `_claim_cache`). The
    reads are not atomic with each other, so an answer describes the
    disk, roster and registry as they were during this call.
    """
    import ccm_jsonl  # lazy: ccm_jsonl pulls ccm_core, not needed by the readers above

    if not project_dir or not claude_config_home_is_default():
        return None
    sessions = list_bg_sessions() if bg_sessions is None else bg_sessions
    listed = {s.session_id: s for s in sessions if s.session_id}
    if not listed:
        return None

    want = ccm_jsonl._canonical(os.path.expanduser(project_dir))
    if not any(ccm_jsonl._canonical(s.cwd) == want for s in listed.values() if s.cwd):
        return None

    # The workers' own transcripts sit in the same directory and can
    # be the newest file there; the CLI does not offer them to
    # `--continue`, so they are not the newest for this question
    # either. Plain mtime order otherwise: a newer transcript that
    # names no directory is still the one the CLI walks first, and it
    # must be the one judged here.
    newest = ccm_jsonl.scan_newest_jsonl(
        project_dir, exclude_stems=frozenset(listed), prefer_claim=False,
        claim_cache=_claim_cache)
    if newest is None:
        return None
    target = continued_in_target(newest)
    if not target or target not in listed:
        return None
    live = (live_ids or _LiveIds()).get()
    if live is None or target not in live:
        return None
    return listed[target]


def continue_blockers(projects, bg_sessions=None) -> dict:
    """Map `win_target` → blocking BgSession for every project whose
    `claude --continue` would start fresh. Reads the roster once.

    Only SHELL projects are considered: the notice is about the
    launch command ccm types on attach, and that is typed into a
    shell pane only. A window already running Claude — often the
    very background session, attached in the foreground — has no
    launch coming, and a notice there would say "starts fresh" over
    a conversation that is on screen."""
    shell_projects = [p for p in projects if p.state == "SHELL"]
    if not shell_projects:
        return {}
    sessions = list_bg_sessions() if bg_sessions is None else bg_sessions
    if not sessions:
        return {}
    live_ids = _LiveIds()
    out = {}
    for p in shell_projects:
        s = continue_blocker(p.dir, sessions, live_ids)
        if s is not None:
            out[p.win_target] = s
    return out


def format_continue_blocker(project_name: str, s: BgSession) -> str:
    """One-line notice for a project whose launch command will not
    resume its conversation. Names the exits the CLI provides."""
    state = s.state.lower() if s.state and s.state != "UNKNOWN" else "live"
    return (
        f"{project_name}: last conversation moved to background session "
        f"{s.short} ({state}) — `claude --continue` starts fresh. "
        f"`claude attach {s.short}` opens it, `claude stop {s.short}` releases it"
    )


def continue_blocker_warnings(projects, bg_sessions=None) -> list:  # noqa: E302
    """Warning lines for `ccm status` / dashboard / `ccm doctor`.
    Never raises: a notice is not worth interrupting the report it
    decorates, so any failure reads as "nothing to report" and is
    logged for `ccm errors`."""
    try:
        blockers = continue_blockers(projects, bg_sessions)
    except Exception:
        import ccm_core
        ccm_core.log_caught_exception("continue_blocker_warnings")
        return []
    return [format_continue_blocker(p.name, blockers[p.win_target])
            for p in projects if p.win_target in blockers]
