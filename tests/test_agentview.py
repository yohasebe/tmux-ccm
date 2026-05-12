"""Tests for ccm_agentview — read-only access to Claude Code's
per-user agent-view daemon (roster.json + jobs/<short>/state.json)."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import ccm_agentview


@pytest.fixture
def fake_claude_home(tmp_path, monkeypatch):
    """Redirect ccm_agentview's daemon-path constants to a tmp tree.

    All four module-level paths are patched so a misbehaving test
    can't accidentally read the user's real ~/.claude/.
    """
    daemon_dir = tmp_path / ".claude" / "daemon"
    jobs_dir = tmp_path / ".claude" / "jobs"
    daemon_dir.mkdir(parents=True)
    jobs_dir.mkdir(parents=True)
    monkeypatch.setattr(ccm_agentview, "DAEMON_DIR", str(daemon_dir))
    monkeypatch.setattr(
        ccm_agentview, "DAEMON_ROSTER_PATH", str(daemon_dir / "roster.json"))
    monkeypatch.setattr(
        ccm_agentview, "DAEMON_STATUS_PATH",
        str(tmp_path / ".claude" / "daemon.status.json"))
    monkeypatch.setattr(ccm_agentview, "JOBS_DIR", str(jobs_dir))
    return tmp_path


def _write_roster(home, workers):
    roster = {"proto": 1, "supervisorPid": 12345, "workers": workers}
    (home / ".claude" / "daemon" / "roster.json").write_text(json.dumps(roster))


def _write_job_state(home, short, payload):
    job_dir = home / ".claude" / "jobs" / short
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "state.json").write_text(json.dumps(payload))


# ─── read_roster / read_job_state ───

class TestReaders:
    def test_missing_roster_returns_empty_dict(self, fake_claude_home):
        assert ccm_agentview.read_roster() == {}

    def test_malformed_roster_returns_empty_dict(self, fake_claude_home):
        path = fake_claude_home / ".claude" / "daemon" / "roster.json"
        path.write_text("{not valid json")
        assert ccm_agentview.read_roster() == {}

    def test_roster_top_level_list_treated_as_empty(self, fake_claude_home):
        # A daemon schema change to a list at the top level should
        # not crash ccm. _safe_load_json filters non-dict roots.
        path = fake_claude_home / ".claude" / "daemon" / "roster.json"
        path.write_text("[]")
        assert ccm_agentview.read_roster() == {}

    def test_read_job_state_rejects_path_traversal(self, fake_claude_home):
        # Even if the daemon ever wrote a slash into a worker key,
        # the path-traversal guard short-circuits before open().
        assert ccm_agentview.read_job_state("../../etc/passwd") == {}
        assert ccm_agentview.read_job_state("/abs") == {}
        assert ccm_agentview.read_job_state("") == {}
        assert ccm_agentview.read_job_state(".hidden") == {}

    def test_read_job_state_missing_returns_empty_dict(self, fake_claude_home):
        assert ccm_agentview.read_job_state("nonexist") == {}


# ─── list_bg_sessions ───

class TestListBgSessions:
    def test_empty_when_no_daemon(self, fake_claude_home):
        assert ccm_agentview.list_bg_sessions() == []

    def test_empty_roster_yields_empty_list(self, fake_claude_home):
        _write_roster(fake_claude_home, {})
        assert ccm_agentview.list_bg_sessions() == []

    def test_single_working_session(self, fake_claude_home):
        _write_roster(fake_claude_home, {
            "8f7bfb5b": {
                "pid": 11974,
                "sessionId": "8f7bfb5b-37e6-485e-a58f-fa8772009c3d",
                "cwd": "/home/u/proj",
                "startedAt": 1778583950174,
                "cliVersion": "2.1.139",
                "dispatch": {
                    "source": "slash",
                    "seed": {"name": "Continue agent view work"},
                },
            }
        })
        _write_job_state(fake_claude_home, "8f7bfb5b", {
            "state": "working",
            "tempo": "active",
            "name": "Continue agent view work",
            "sessionId": "8f7bfb5b-37e6-485e-a58f-fa8772009c3d",
            "cwd": "/home/u/proj",
            "createdAt": "2026-05-12T01:51:34.559Z",
            "updatedAt": "2026-05-12T11:09:42.252Z",
        })
        sessions = ccm_agentview.list_bg_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s.short == "8f7bfb5b"
        assert s.pid == 11974
        assert s.state == "WORKING"
        assert s.raw_state == "working"
        assert s.tempo == "active"
        assert s.name == "Continue agent view work"
        assert s.cli_version == "2.1.139"
        assert s.source == "slash"
        assert s.created_at is not None
        assert s.updated_at is not None and s.updated_at > s.created_at

    def test_state_normalization_unknown_value(self, fake_claude_home):
        _write_roster(fake_claude_home, {"abcd1234": {"pid": 1, "cwd": "/x"}})
        _write_job_state(fake_claude_home, "abcd1234",
                         {"state": "future_state_we_haven_t_seen"})
        s = ccm_agentview.list_bg_sessions()[0]
        assert s.state == "UNKNOWN"
        assert s.raw_state == "future_state_we_haven_t_seen"

    def test_state_normalization_known_values(self, fake_claude_home):
        cases = {
            "working": "WORKING",
            "needs_input": "NEEDS",
            "idle": "IDLE",
            "done": "DONE",
            "failed": "FAILED",
        }
        # Use distinct 8-char hex shorts per case so the
        # is_valid_short filter doesn't drop them.
        case_shorts = {
            "working": "abcd0001",
            "needs_input": "abcd0002",
            "idle": "abcd0003",
            "done": "abcd0004",
            "failed": "abcd0005",
        }
        for raw, expected in cases.items():
            short = case_shorts[raw]
            _write_roster(fake_claude_home, {short: {"pid": 1, "cwd": "/x"}})
            _write_job_state(fake_claude_home, short, {"state": raw})
            s = ccm_agentview.list_bg_sessions()[0]
            assert s.state == expected, f"raw={raw}"

    def test_name_falls_back_to_dispatch_seed(self, fake_claude_home):
        # state.json missing `name` — should fall back to roster's
        # dispatch.seed.name. This is the case for fresh `--bg`
        # dispatches before the first turn has assigned a label.
        _write_roster(fake_claude_home, {
            "abcd1234": {
                "pid": 100, "cwd": "/x",
                "dispatch": {"seed": {"name": "seed-name"}},
            }
        })
        _write_job_state(fake_claude_home, "abcd1234", {"state": "working"})
        s = ccm_agentview.list_bg_sessions()[0]
        assert s.name == "seed-name"

    def test_name_state_json_overrides_seed(self, fake_claude_home):
        # Once the job has its own `name`, that wins over the seed
        # (seed is the original dispatch label; state.json's name is
        # the auto-generated post-first-turn label).
        _write_roster(fake_claude_home, {
            "abcd1234": {
                "pid": 100, "cwd": "/x",
                "dispatch": {"seed": {"name": "original"}},
            }
        })
        _write_job_state(fake_claude_home, "abcd1234",
                         {"state": "working", "name": "auto-generated"})
        s = ccm_agentview.list_bg_sessions()[0]
        assert s.name == "auto-generated"

    def test_missing_state_file_uses_roster_only(self, fake_claude_home):
        # No state.json yet (very fresh worker); list_bg_sessions
        # still returns the roster entry with UNKNOWN state.
        _write_roster(fake_claude_home, {
            "abcd1234": {
                "pid": 100, "cwd": "/x", "startedAt": 1778583950000,
                "cliVersion": "2.1.139",
                "dispatch": {"seed": {"name": "fresh"}},
            }
        })
        sessions = ccm_agentview.list_bg_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s.state == "UNKNOWN"
        assert s.name == "fresh"
        # startedAt (epoch ms) feeds created_at when state.json
        # is unavailable.
        assert s.created_at == pytest.approx(1778583950.174, abs=1.0)

    def test_malformed_state_file_doesnt_crash(self, fake_claude_home):
        _write_roster(fake_claude_home, {"abcd1234": {"pid": 1, "cwd": "/x"}})
        (fake_claude_home / ".claude" / "jobs" / "abcd1234").mkdir(parents=True)
        (fake_claude_home / ".claude" / "jobs" / "abcd1234" / "state.json").write_text(
            "garbage{{{"
        )
        sessions = ccm_agentview.list_bg_sessions()
        assert len(sessions) == 1
        assert sessions[0].state == "UNKNOWN"

    def test_priority_sort_needs_before_working(self, fake_claude_home):
        # NEEDS goes first (operator action required), then WORKING,
        # then IDLE/DONE. Same-priority sessions sorted by updated_at
        # descending so the most-recently-active is on top.
        _write_roster(fake_claude_home, {
            "aaaaaaaa": {"pid": 1, "cwd": "/a"},
            "bbbbbbbb": {"pid": 2, "cwd": "/b"},
            "cccccccc": {"pid": 3, "cwd": "/c"},
        })
        _write_job_state(fake_claude_home, "aaaaaaaa", {
            "state": "done", "updatedAt": "2026-05-12T01:00:00Z"})
        _write_job_state(fake_claude_home, "bbbbbbbb", {
            "state": "needs_input", "updatedAt": "2026-05-12T02:00:00Z"})
        _write_job_state(fake_claude_home, "cccccccc", {
            "state": "working", "updatedAt": "2026-05-12T03:00:00Z"})
        sessions = ccm_agentview.list_bg_sessions()
        assert [s.short for s in sessions] == ["bbbbbbbb", "cccccccc", "aaaaaaaa"]

    def test_pid_int_coercion(self, fake_claude_home):
        # Should an upstream version ever stringify pid, ccm coerces
        # safely. Garbage values fall to 0 rather than crashing.
        _write_roster(fake_claude_home, {
            "aaaaaaaa": {"pid": "1234", "cwd": "/x"},
            "bbbbbbbb": {"pid": "abcd1234", "cwd": "/x"},
            "cccccccc": {"pid": None, "cwd": "/x"},
        })
        sessions = {s.short: s for s in ccm_agentview.list_bg_sessions()}
        assert sessions["aaaaaaaa"].pid == 1234
        assert sessions["bbbbbbbb"].pid == 0
        assert sessions["cccccccc"].pid == 0

    def test_non_dict_worker_entry_skipped(self, fake_claude_home):
        # Defensive: a partial daemon write could leave a worker
        # value that isn't a dict. ccm should skip it, not crash.
        _write_roster(fake_claude_home, {
            "aaaaaaaa": "not a dict",
            "bbbbbbbb": {"pid": 100, "cwd": "/x"},
        })
        sessions = ccm_agentview.list_bg_sessions()
        assert len(sessions) == 1
        assert sessions[0].short == "bbbbbbbb"


class TestIsValidShort:
    """`is_valid_short` gates the value that ultimately becomes the
    short ID embedded in a `claude attach <short>` shell command.
    Anything outside Claude's documented form (lower-case hex,
    4–16 chars) must reject."""

    def test_accepts_normal_8char_hex(self):
        assert ccm_agentview.is_valid_short("8f7bfb5b") is True

    def test_accepts_short_min(self):
        assert ccm_agentview.is_valid_short("abcd") is True

    def test_accepts_long_max(self):
        assert ccm_agentview.is_valid_short("0" * 16) is True

    def test_rejects_uppercase(self):
        assert ccm_agentview.is_valid_short("ABCD1234") is False

    def test_rejects_metachars(self):
        # Most important: shell metachars must never pass through.
        for bad in (
            "abc;rm",
            "abc rm",
            "abc&&rm",
            "abc|rm",
            "abc`x`",
            "abc$x",
            "..",
            "../abc",
            "abc/def",
        ):
            assert ccm_agentview.is_valid_short(bad) is False, bad

    def test_rejects_too_short(self):
        assert ccm_agentview.is_valid_short("abc") is False

    def test_rejects_too_long(self):
        assert ccm_agentview.is_valid_short("0" * 17) is False

    def test_rejects_non_string(self):
        for bad in (None, 12345678, b"abcd1234", [], {}):
            assert ccm_agentview.is_valid_short(bad) is False

    def test_rejects_empty(self):
        assert ccm_agentview.is_valid_short("") is False


class TestRosterShortFiltering:
    """list_bg_sessions must drop workers whose roster key fails
    is_valid_short — they would otherwise pollute the dashboard with
    rows that the attach handler cannot dispatch."""

    def test_malformed_short_keys_dropped(self, fake_claude_home):
        _write_roster(fake_claude_home, {
            "8f7bfb5b": {"pid": 1, "cwd": "/x"},          # valid
            "BAD;rm": {"pid": 2, "cwd": "/x"},            # metachars
            "X" * 4: {"pid": 3, "cwd": "/x"},             # uppercase
            "../etc": {"pid": 4, "cwd": "/x"},            # traversal
        })
        sessions = ccm_agentview.list_bg_sessions()
        shorts = {s.short for s in sessions}
        assert shorts == {"8f7bfb5b"}


class TestDaemonRunning:
    def test_no_files_means_not_running(self, fake_claude_home):
        assert ccm_agentview.daemon_running() is False

    def test_status_file_alone_counts_as_running(self, fake_claude_home):
        (fake_claude_home / ".claude" / "daemon.status.json").write_text("{}")
        assert ccm_agentview.daemon_running() is True

    def test_roster_alone_counts_as_running(self, fake_claude_home):
        _write_roster(fake_claude_home, {})
        assert ccm_agentview.daemon_running() is True
