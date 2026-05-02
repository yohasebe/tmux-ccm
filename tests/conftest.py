"""Shared test infrastructure: lib path setup, autouse fixtures,
and helpers used across the per-module test files."""

import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest

# Make lib/ importable so each test file can `import ccm_core` etc.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))


@pytest.fixture(autouse=True)
def reset_state():
    """Reset any module-level state between tests."""
    yield


# ─── JSONL helpers ───
# Used by tests that simulate Claude Code's per-session JSONL log.

def iso_ts(unix_ts):
    """Format a unix timestamp as the ISO 8601 string Claude Code
    writes into JSONL records (UTC, milliseconds, trailing Z)."""
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def write_jsonl(path, records):
    """Write a list of dict records as one JSON-per-line to `path`."""
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def real_activity_record(unix_ts, role="assistant"):
    """Build a minimal user/assistant JSONL record at the given ts."""
    return {"type": role, "timestamp": iso_ts(unix_ts),
            "message": {"content": "x"}}


def system_record(unix_ts, subtype="away_summary"):
    """Build a minimal system metadata record (e.g. recap)."""
    return {"type": "system", "subtype": subtype, "timestamp": iso_ts(unix_ts)}


# ─── ps snapshot helpers ───

def make_ps_lines(*entries):
    """Build ps output lines. Each entry: (pid, ppid, pgid, comm)."""
    lines = ["  PID  PPID  PGID COMM"]
    for pid, ppid, pgid, comm in entries:
        lines.append(f"  {pid}   {ppid}   {pgid} {comm}")
    return lines


# ─── DetectionContext factory ───

# Set of states `evaluate_rules` / `derive_state_from_events` are
# allowed to commit. Any rule resolution outside this set means a
# bug in DETECTION_RULES (the dashboard renderer assumes it sees only
# these). Used by Pipeline / Derive invariant tests.
VALID_RESOLVED_STATES = frozenset({"SHELL", "DOWN", "BUSY", "IDLE", "PERMIT"})


def make_ctx(**overrides):
    """Build a DetectionContext with sensible defaults for rule
    testing. Optional fields (`jsonl_last_stop_reason`,
    `claude_pid_age`) are NOT in the defaults so tests can opt in
    via overrides; tests that omit them inherit dataclass defaults.

    Imported lazily because conftest is loaded before any test file
    has had a chance to `import ccm_rules` — taking the chain at
    conftest load time (`ccm_rules → ccm_core → ccm_commands →
    ccm_detection → ccm_rules`) hits the partial-module wall."""
    import ccm_rules  # deferred past the circular-load window
    now = int(time.time())
    defaults = dict(
        raw="IDLE",
        hook_state="",
        hook_ts=0,
        hook_age=-1,
        prev_state="IDLE",
        jsonl_age=-1,
        now=now,
    )
    defaults.update(overrides)
    return ccm_rules.DetectionContext(**defaults)
