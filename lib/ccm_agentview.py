"""Read-only access to Claude Code's per-user agent-view daemon.

Claude Code 2.1.139 introduced an "agent view" (`claude agents`,
`claude --bg <prompt>`, `claude attach <short>`) that runs sessions
as workers under a per-user supervisor daemon. The daemon writes
its roster to `~/.claude/daemon/roster.json` and each session's
job state to `~/.claude/jobs/<short>/state.json`.

ccm reads these files to surface the background-session list in the
dashboard. This module is strictly read-only: it never writes to
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
      "state": "working|idle|done|failed|needs_input|...",
      "tempo": "active|idle",
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

import json
import os
import re
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
    "needs_input": "NEEDS",
    "idle": "IDLE",
    "done": "DONE",
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
    "UNKNOWN": 5,
}


@dataclass
class BgSession:
    """One row in the agent-view roster, joined with its job state.json."""
    short: str               # 8-char short ID (roster key)
    pid: int                 # worker process pid (0 if missing)
    cwd: str                 # absolute cwd
    name: str                # human-readable label
    state: str               # normalized: WORKING / NEEDS / IDLE / DONE / FAILED / UNKNOWN
    raw_state: str           # lowercase string from state.json (debug aid)
    tempo: str               # "active" / "idle" / ""
    cli_version: str         # e.g. "2.1.139"
    session_id: str          # full UUID
    created_at: Optional[float]   # unix seconds (None if unparseable)
    updated_at: Optional[float]   # unix seconds (None if unparseable)
    source: str              # dispatch.source: "slash" / "bg" / "attach" / ""


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


def read_daemon_status() -> dict:
    """Return parsed `~/.claude/daemon.status.json`, or `{}`."""
    return _safe_load_json(DAEMON_STATUS_PATH)


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
