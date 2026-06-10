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


@pytest.fixture(autouse=True)
def isolate_errors_log(tmp_path, monkeypatch):
    """Redirect the silent-exception log away from the user's real
    `$TMPDIR/ccm-$UID/errors.log` for every test.

    `CCM_ERRORS_LOG` is computed at import time from `CCM_TMP_DIR`,
    so tests that monkeypatch `CCM_TMP_DIR` do NOT redirect the
    error log — any test that exercises a silent-catch path (or has
    a bug in its scaffolding) would append garbage entries to the
    real log that `ccm errors` then shows the user as if they were
    production failures. This actually happened: a stale
    `Dashboard.__new__` scaffold in test_silent_exceptions.py was
    missing the `bg_visible` attribute, and the resulting
    AttributeError was logged to the real errors.log on every test
    run, masquerading as a production dashboard bug (2026-06-07).
    """
    import ccm_core
    monkeypatch.setattr(ccm_core, "CCM_ERRORS_LOG",
                        str(tmp_path / "errors.log"))
    monkeypatch.setattr(ccm_core, "CCM_ERRORS_LOG_PREV",
                        str(tmp_path / "errors.log.1"))
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
