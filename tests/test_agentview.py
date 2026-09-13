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
    """Write a roster whose workers are claimed unless a test says
    otherwise.

    A worker with neither a job document nor a dispatch record is one
    the daemon keeps warm for the next task, and ccm does not list
    those. Tests about anything else — pid coercion, malformed keys —
    want ordinary sessions, so a dispatch is filled in for them; a
    test about the unclaimed case passes its own worker dict with
    `dispatch` left out.
    """
    filled = {}
    for short, w in workers.items():
        if isinstance(w, dict) and "dispatch" not in w:
            w = dict(w, dispatch={"short": short})
        filled[short] = w
    roster = {"proto": 1, "supervisorPid": 12345, "workers": filled}
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

    def test_blocked_carries_the_ask(self, fake_claude_home):
        """A blocked job records what it waits for in `needs`; the
        session carries it verbatim (whitespace collapsed) so the
        listing can say what would unblock it. Absent → ""."""
        _write_roster(fake_claude_home, {"abcd1234": {"pid": 1, "cwd": "/x"}})
        _write_job_state(fake_claude_home, "abcd1234",
                         {"state": "blocked", "needs": "go-ahead for\n plan A or B"})
        s = ccm_agentview.list_bg_sessions()[0]
        assert (s.state, s.needs) == ("NEEDS", "go-ahead for plan A or B")
        _write_job_state(fake_claude_home, "abcd1234", {"state": "working"})
        assert ccm_agentview.list_bg_sessions()[0].needs == ""

    def test_state_normalization_known_values(self, fake_claude_home):
        cases = {
            "working": "WORKING",
            "needs_input": "NEEDS",
            "blocked": "NEEDS",
            "idle": "IDLE",
            "done": "DONE",
            "stopped": "STOPPED",
            "failed": "FAILED",
        }
        # Use distinct 8-char hex shorts per case so the
        # is_valid_short filter doesn't drop them.
        case_shorts = {
            "working": "abcd0001",
            "needs_input": "abcd0002",
            "blocked": "abcd0006",
            "idle": "abcd0003",
            "done": "abcd0004",
            "stopped": "abcd0007",
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

    def test_unclaimed_worker_is_not_a_session(self, fake_claude_home):
        """The daemon keeps a worker warm so the next dispatch starts
        fast. It is not work anyone is waiting on, and listing it puts
        a nameless row in front of the reader with nothing to attach
        to."""
        roster = {"proto": 1, "workers": {
            "aaaaaaaa": {"pid": 1, "cwd": "/x", "dispatch": {"short": "aaaaaaaa"}},
            "bbbbbbbb": {"pid": 2, "cwd": "/y"},
        }}
        (fake_claude_home / ".claude" / "daemon" / "roster.json").write_text(
            json.dumps(roster))
        shorts = [s.short for s in ccm_agentview.list_bg_sessions()]
        assert shorts == ["aaaaaaaa"]

    def test_dispatched_job_shows_before_its_state_file_lands(
            self, fake_claude_home):
        """A dispatch that has registered but whose state file has not
        been written yet is a real session starting, not a warm
        worker. Hiding it would trade one invisible thing for
        another."""
        roster = {"proto": 1, "workers": {
            "aaaaaaaa": {"pid": 1, "cwd": "/x", "dispatch": {"short": "aaaaaaaa"}},
        }}
        (fake_claude_home / ".claude" / "daemon" / "roster.json").write_text(
            json.dumps(roster))
        assert len(ccm_agentview.list_bg_sessions()) == 1

    def test_corrupt_state_file_still_counts_as_a_job(self, fake_claude_home):
        """Existence, not parseability: a job whose state file cannot
        be read is still a job, and dropping it would hide real work
        because of a bad byte."""
        roster = {"proto": 1, "workers": {"abcd1234": {"pid": 1, "cwd": "/x"}}}
        (fake_claude_home / ".claude" / "daemon" / "roster.json").write_text(
            json.dumps(roster))
        jd = fake_claude_home / ".claude" / "jobs" / "abcd1234"
        jd.mkdir(parents=True)
        (jd / "state.json").write_text("garbage{{{")
        assert len(ccm_agentview.list_bg_sessions()) == 1

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


# ─── `claude --continue` hand-off check ───

import ccm_core
import ccm_jsonl


def _rec(**fields):
    return json.dumps(fields)


def _write_transcript(projects_dir, project_dir, session_id, lines, mtime):
    """Write one transcript under the project's slug directory and
    pin its mtime so the newest-first order is under test control."""
    slug_dir = projects_dir / ccm_jsonl._project_slug(str(project_dir))
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


# Synthetic ids in the CLI's uuid shape (hex only); the short id is
# the first eight characters, as the daemon derives it.
BG_ID = "0b900000-0000-4000-8000-000000000001"
OLD_ID = "01d00000-0000-4000-8000-000000000002"
NEW_ID = "0e300000-0000-4000-8000-000000000003"


@pytest.fixture
def cli_says_live(monkeypatch):
    """Stub the session registry's live set. Default: BG_ID is live;
    a test about the roster outliving the daemon overrides it."""
    state = {"ids": None}

    def fake():
        return state["ids"]

    monkeypatch.setattr(ccm_agentview, "live_noninteractive_session_ids", fake)

    def set_live(ids):
        state["ids"] = None if ids is None else set(ids)
    set_live({BG_ID})
    return set_live


@pytest.fixture
def handoff_env(fake_claude_home, cli_says_live, tmp_path, monkeypatch):
    """A project directory, a fake transcripts root, the CLI's live
    list stubbed (BG_ID live). Returns (project_dir, projects_dir)."""
    project_dir = tmp_path / "work" / "proj"
    project_dir.mkdir(parents=True)
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(ccm_agentview, "_claim_cache", {})
    monkeypatch.setattr(ccm_agentview, "_handoff_cache", {})
    return project_dir, projects_dir




def _conversation(cwd):
    return [
        _rec(type="user", cwd=cwd, message={"role": "user", "content": "hi"}),
        _rec(type="assistant", cwd=cwd,
             message={"role": "assistant", "stop_reason": "end_turn"}),
    ]


def _handoff(from_id, to_id):
    return [
        _rec(type="continued-in", sessionId=from_id, continuedInSessionId=to_id),
        _rec(type="last-prompt", sessionId=from_id),
        _rec(type="cost-state", sessionId=from_id),
    ]


class TestContinuedInTarget:
    def test_handoff_as_last_conversation_record(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("\n".join(_conversation("/w") + _handoff(OLD_ID, BG_ID)) + "\n")
        assert ccm_agentview.continued_in_target(str(p)) == BG_ID

    def test_handoff_talked_past_is_not_reported(self, tmp_path):
        """A conversation record after the hand-off means the CLI
        walks past the hand-off too; the transcript is resumable."""
        p = tmp_path / "t.jsonl"
        p.write_text("\n".join(
            _conversation("/w") + _handoff(OLD_ID, BG_ID) + _conversation("/w")
        ) + "\n")
        assert ccm_agentview.continued_in_target(str(p)) is None

    def test_no_handoff(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("\n".join(_conversation("/w")) + "\n")
        assert ccm_agentview.continued_in_target(str(p)) is None

    def test_missing_file(self, tmp_path):
        assert ccm_agentview.continued_in_target(str(tmp_path / "no.jsonl")) is None

    def test_malformed_handoff_line_is_skipped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("\n".join(_conversation("/w")) + "\n"
                     + '{"type":"continued-in", broken\n')
        assert ccm_agentview.continued_in_target(str(p)) is None

    def test_marker_only_in_prose_is_not_a_record(self, tmp_path):
        """The marker inside a message body is not a hand-off: the
        parsed record's own type decides."""
        p = tmp_path / "t.jsonl"
        line = _rec(type="user", cwd="/w",
                    message={"role": "user",
                             "content": 'what is "type":"continued-in"?'})
        p.write_text(line + "\n")
        assert ccm_agentview.continued_in_target(str(p)) is None

    def test_handoff_beyond_tail_window_is_not_seen(self, tmp_path):
        """Deliberate bound: a hand-off followed by more than the tail
        window of housekeeping is missed, never misreported."""
        p = tmp_path / "t.jsonl"
        filler = _rec(type="cost-state", pad="x" * 4096)
        lines = _conversation("/w") + _handoff(OLD_ID, BG_ID) + [filler] * 32
        p.write_text("\n".join(lines) + "\n")
        assert ccm_agentview.continued_in_target(str(p)) is None


def _bg(short, session_id, cwd, state="DONE"):
    return ccm_agentview.BgSession(
        short=short, pid=1, cwd=cwd, name="n", state=state,
        raw_state=state.lower(), tempo="", cli_version="",
        session_id=session_id, created_at=None, updated_at=None,
        source="slash")


class TestContinueBlocker:
    def test_newest_transcript_handed_to_live_bg_session(self, handoff_env):
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        got = ccm_agentview.continue_blocker(cwd, [bg])
        assert got is bg

    def test_bg_transcript_itself_is_not_the_newest(self, handoff_env):
        """The worker's own transcript lives in the same directory
        and is usually the newest file there; the CLI does not offer
        it to `--continue`, so it must not hide the hand-off."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        _write_transcript(projects_dir, project_dir, BG_ID,
                          _conversation(cwd), 2000)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg

    def test_newer_interactive_session_clears_it(self, handoff_env):
        """Once a newer interactive transcript exists, `--continue`
        resumes that one; the older hand-off no longer blocks."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        _write_transcript(projects_dir, project_dir, NEW_ID,
                          _conversation(cwd), 3000)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_handoff_to_session_no_longer_in_roster(self, handoff_env):
        """Stopped or removed worker: the CLI moves on, so does this."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        other = _bg("deadbeef", "deadbeef-0000-0000-0000-000000000000", cwd)
        assert ccm_agentview.continue_blocker(cwd, [other]) is None

    def test_bg_session_for_another_directory_is_ignored(self, handoff_env):
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, str(project_dir.parent / "elsewhere"))
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_done_worker_still_blocks(self, handoff_env):
        """The trap this check exists for: the roster keeps a
        finished worker, and the CLI counts it as live."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd, state="DONE")
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg

    def test_roster_worker_the_cli_no_longer_counts_as_live(
            self, handoff_env, cli_says_live):
        """The roster outlives the daemon: after a crash or reboot
        every worker is still listed, with a dead pid. The CLI's own
        live list is what `--continue` consults, and it is asked."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        cli_says_live(set())
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_empty_registry_reports_nothing(self, handoff_env, cli_says_live):
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        cli_says_live(set())
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_unknown_registry_reports_nothing(self, handoff_env, cli_says_live):
        """The CLI treats an unreadable registry as no set at all and
        walks past the hand-off; so must the notice."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        cli_says_live(None)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    @pytest.mark.parametrize("value", ["", "~/.claude", ".claude", "relative/.claude"])
    def test_config_home_forms_the_cli_reads_differently_are_not_default(
            self, monkeypatch, value):
        """The CLI takes the variable as-is: empty is not unset, `~`
        is not expanded, a relative path is the launcher's cwd
        business. None of these can be called the default home."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
        assert ccm_agentview.claude_config_home_is_default() is False

    def test_config_home_unset_or_same_realpath_is_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert ccm_agentview.claude_config_home_is_default() is True
        link = tmp_path / "link"
        os.symlink(ccm_agentview.DEFAULT_CLAUDE_HOME, link)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link))
        assert ccm_agentview.claude_config_home_is_default() is True
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
        assert ccm_agentview.claude_config_home_is_default() is False

    def test_moved_config_home_withholds_the_notice(self, handoff_env, monkeypatch, tmp_path):
        """`CLAUDE_CONFIG_DIR` elsewhere: the CLI reads another home;
        facts under ~/.claude say nothing about that launch."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-home"))
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", ccm_agentview.DEFAULT_CLAUDE_HOME)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg

    def test_cli_is_asked_only_when_roster_and_handoff_agree(
            self, handoff_env, monkeypatch):
        """The subprocess is the expensive fact; it runs only once a
        listed worker and a matching hand-off make it necessary."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        asked = []
        monkeypatch.setattr(ccm_agentview, "live_noninteractive_session_ids",
                            lambda: asked.append(1) or {BG_ID})
        bg = _bg("0b900000", BG_ID, cwd)
        # no transcript at all
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None
        # transcript without a hand-off
        _write_transcript(projects_dir, project_dir, NEW_ID, _conversation(cwd), 100)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None
        assert asked == []
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg
        assert asked == [1]

    def test_one_pass_asks_the_cli_once(self, handoff_env, monkeypatch):
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        asked = []
        monkeypatch.setattr(ccm_agentview, "live_noninteractive_session_ids",
                            lambda: asked.append(1) or {BG_ID})
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        projects = [ccm_core.Project(f"0:{i}", str(i), f"p{i}", cwd, "SHELL")
                    for i in range(3)]
        assert len(ccm_agentview.continue_blockers(projects, [bg])) == 3
        assert asked == [1]

    def test_no_roster_no_scan(self, handoff_env, monkeypatch):
        called = []
        monkeypatch.setattr(ccm_jsonl, "scan_newest_jsonl",
                            lambda *a, **kw: called.append(a) or None)
        assert ccm_agentview.continue_blocker("/somewhere", []) is None
        assert called == []

    def test_reads_roster_when_not_given(self, handoff_env):
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        home = projects_dir.parent.parent
        _write_roster(home, {"0b900000": {"pid": 1, "sessionId": BG_ID, "cwd": cwd}})
        _write_job_state(home, "0b900000", {"state": "done", "name": "x"})
        got = ccm_agentview.continue_blocker(cwd)
        assert got is not None and got.short == "0b900000"

    def test_newer_silent_transcript_is_judged_instead(self, handoff_env):
        """Detection prefers a transcript that names the directory
        over a newer one that names none; the CLI does not. A newer
        silent transcript with no hand-off means nothing blocks."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        silent = [_rec(type="user", message={"role": "user", "content": "x"})]
        _write_transcript(projects_dir, project_dir, NEW_ID, silent, 2000)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_positive_answer_yields_to_a_new_transcript(self, handoff_env):
        """Nothing about the answer is cached: a new interactive
        transcript clears it on the very next call."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg
        _write_transcript(projects_dir, project_dir, NEW_ID,
                          _conversation(cwd), 3000)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_positive_answer_yields_when_another_transcript_is_appended(
            self, handoff_env):
        """An older transcript resumed and written to becomes the
        newest; the directory's own mtime does not move for that."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        old = _write_transcript(projects_dir, project_dir, NEW_ID,
                                _conversation(cwd), 100)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 200)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg
        with open(old, "a") as f:
            f.write("\n".join(_conversation(cwd)) + "\n")
        os.utime(old, (300, 300))
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_positive_answer_yields_when_transcript_grows(self, handoff_env):
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        path = _write_transcript(projects_dir, project_dir, OLD_ID,
                                 _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg
        with open(path, "a") as f:
            f.write("\n".join(_conversation(cwd)) + "\n")
        os.utime(path, (1001, 1001))
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_positive_answer_yields_when_roster_exclusion_changes(
            self, handoff_env):
        """A worker's own transcript, excluded while the worker was in
        the roster, is a candidate again once it leaves — and being
        newest, it is the one judged."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        other_id = "0a1d0000-0000-4000-8000-000000000004"
        _write_transcript(projects_dir, project_dir, other_id,
                          _conversation(cwd), 2000)
        bg = _bg("0b900000", BG_ID, cwd)
        other = _bg("0a1d0000", other_id, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg, other]) is bg
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_positive_answer_yields_when_transcript_is_removed(
            self, handoff_env):
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        path = _write_transcript(projects_dir, project_dir, OLD_ID,
                                 _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg
        os.remove(path)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_unreadable_newest_is_a_miss(self, handoff_env, monkeypatch):
        """Cannot read the newest transcript → nothing is claimed."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        real_open = open

        def failing_open(path, *a, **kw):
            if str(path).endswith(f"{OLD_ID}.jsonl") and "b" in (a[0] if a else kw.get("mode", "")):
                raise PermissionError(path)
            return real_open(path, *a, **kw)

        monkeypatch.setattr("builtins.open", failing_open)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is None

    def test_stat_failure_does_not_reuse_a_parse_result(self, handoff_env, monkeypatch):
        """A parse result is keyed on a real (mtime, size); a file
        that cannot be stat'd gets no answer, not the last one."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        path = _write_transcript(projects_dir, project_dir, OLD_ID,
                                 _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        assert ccm_agentview.continued_in_target(str(path)) == BG_ID
        real_stat = os.stat

        def failing_stat(p, *a, **kw):
            if str(p) == str(path):
                raise PermissionError(p)
            return real_stat(p, *a, **kw)

        monkeypatch.setattr(os, "stat", failing_stat)
        assert ccm_agentview.continued_in_target(str(path)) is None

    def test_repeat_calls_reuse_parse_results_only(self, handoff_env, monkeypatch):
        """Unchanged files are not re-read; the newest-file decision
        and the roster check still run every call."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg
        reads = []
        monkeypatch.setattr(ccm_jsonl, "_jsonl_cwd_claim",
                            lambda *a, **kw: reads.append(a) or pytest.fail("re-read"))
        monkeypatch.setattr(ccm_agentview, "_read_continued_in_target",
                            lambda *a, **kw: pytest.fail("re-read"))
        scans = []
        real_scan = ccm_jsonl.scan_newest_jsonl
        monkeypatch.setattr(ccm_jsonl, "scan_newest_jsonl",
                            lambda *a, **kw: scans.append(a) or real_scan(*a, **kw))
        assert ccm_agentview.continue_blocker(cwd, [bg]) is bg
        assert len(scans) == 1 and reads == []


class TestContinueBlockerSlugCollision:
    """`a-b` and `a_b` share one transcript directory. What a file
    says about its own directory may be cached; whether that is
    *this* project's directory must be asked every time, or one
    project inherits the other's answer."""

    def _setup(self, tmp_path, monkeypatch, fake_claude_home):
        monkeypatch.setattr(ccm_agentview, "live_noninteractive_session_ids",
                            lambda: {BG_ID, "0b000000-0000-4000-8000-000000000005"})
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(projects_dir))
        a = tmp_path / "a-b"
        b = tmp_path / "a_b"
        a.mkdir()
        b.mkdir()
        assert ccm_jsonl._project_slug(str(a)) == ccm_jsonl._project_slug(str(b))
        _write_transcript(projects_dir, a, OLD_ID,
                          _conversation(str(a)) + _handoff(OLD_ID, BG_ID), 200)
        _write_transcript(projects_dir, b, NEW_ID, _conversation(str(b)), 100)
        bg_a = _bg("0b900000", BG_ID, str(a))
        bg_b = _bg("0b000000", "0b000000-0000-4000-8000-000000000005", str(b))
        return a, b, [bg_a, bg_b], bg_a

    def test_a_then_b(self, tmp_path, monkeypatch, fake_claude_home):
        a, b, sessions, bg_a = self._setup(tmp_path, monkeypatch, fake_claude_home)
        assert ccm_agentview.continue_blocker(str(a), sessions) is bg_a
        assert ccm_agentview.continue_blocker(str(b), sessions) is None
        assert ccm_agentview.continue_blocker(str(a), sessions) is bg_a

    def test_b_then_a(self, tmp_path, monkeypatch, fake_claude_home):
        a, b, sessions, bg_a = self._setup(tmp_path, monkeypatch, fake_claude_home)
        assert ccm_agentview.continue_blocker(str(b), sessions) is None
        assert ccm_agentview.continue_blocker(str(a), sessions) is bg_a
        assert ccm_agentview.continue_blocker(str(b), sessions) is None


class TestLiveNoninteractiveSessionIds:
    """The registry `<home>/sessions/<pid>.json` is what
    `claude --continue` consults. Mirrors the CLI's reading where
    the CLI is definite, and returns None (unknown) wherever the CLI
    would not commit to a set — or where ccm cannot verify what the
    CLI verifies."""

    TOKEN = "Thu Sep 10 00:37:10 2026"

    @pytest.fixture
    def registry(self, tmp_path, monkeypatch):
        d = tmp_path / "sessions"
        d.mkdir()
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(d))
        monkeypatch.setattr(ccm_agentview, "own_pid_domain", lambda: "darwin")
        alive = set()
        tokens = {}

        def fake_kill(pid, sig):
            assert sig == 0
            if pid not in alive:
                raise ProcessLookupError(pid)

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(ccm_agentview, "process_start_token",
                            lambda pid: tokens.get(pid))

        def write(pid_, **fields):
            fields.setdefault("pid", pid_)
            fields.setdefault("pidDomain", "darwin")
            fields.setdefault("procStart", self.TOKEN)
            (d / f"{pid_}.json").write_text(json.dumps(fields))

        def live(pid_):
            alive.add(pid_)
            tokens[pid_] = self.TOKEN
        return d, write, live, alive, tokens

    def test_live_bg_record_counts(self, registry):
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        assert ccm_agentview.live_noninteractive_session_ids() == {BG_ID}

    def test_interactive_records_are_not_counted(self, registry):
        d, write, live, alive, tokens = registry
        write(100, kind="interactive", sessionId=NEW_ID)
        live(100)
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    @pytest.mark.parametrize("kind", ["bg", "daemon", "daemon-worker"])
    def test_known_noninteractive_kinds_count(self, registry, kind):
        d, write, live, alive, tokens = registry
        write(100, kind=kind, sessionId=BG_ID)
        live(100)
        assert ccm_agentview.live_noninteractive_session_ids() == {BG_ID}

    def test_unknown_kind_makes_the_set_unknown(self, registry):
        """The CLI normalises a kind it does not recognise to none,
        and a live record with no kind makes it drop the whole set.
        Dead or alive, such a record ends the answer here."""
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        write(105, kind="future-kind", sessionId=OLD_ID)
        live(105)
        assert ccm_agentview.live_noninteractive_session_ids() is None
        (d / "105.json").unlink()
        write(106, kind=7, sessionId=OLD_ID)
        assert ccm_agentview.live_noninteractive_session_ids() is None
        for bad in ([], {}, ["bg"], None):
            write(106, kind=bad, sessionId=OLD_ID)
            assert ccm_agentview.live_noninteractive_session_ids() is None, bad

    def test_odd_numeric_names_and_huge_pids_are_not_records(self, registry):
        """The CLI enumerates ASCII `\d+.json` only; a superscript
        digit is not a record, and a number no pid can hold is not a
        process. Neither may raise."""
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        (d / "\u00b2.json").write_text("{not json")
        assert ccm_agentview.live_noninteractive_session_ids() == {BG_ID}
        huge = str(1 << 100)
        (d / f"{huge}.json").write_text(json.dumps(
            {"kind": "bg", "sessionId": OLD_ID, "pidDomain": "darwin",
             "procStart": self.TOKEN}))
        assert ccm_agentview.live_noninteractive_session_ids() == {BG_ID}

    def test_process_is_live_never_raises(self, monkeypatch):
        def kill(pid, sig):
            raise OverflowError("too large")
        monkeypatch.setattr(os, "kill", kill)
        assert ccm_agentview._process_is_live(1 << 100) is False
        assert ccm_agentview._process_is_live(0) is False
        assert ccm_agentview._process_is_live("100") is False

    def test_pid_comes_from_the_filename(self, registry):
        """The CLI checks the process the file is named after. A body
        naming another pid is a record ccm cannot vouch for."""
        d, write, live, alive, tokens = registry
        write(999, kind="bg", sessionId=BG_ID, pid=100)
        live(100)
        assert ccm_agentview.live_noninteractive_session_ids() is None

    def test_non_canonical_filename_is_ignored(self, registry):
        d, write, live, alive, tokens = registry
        (d / "0100.json").write_text(json.dumps(
            {"kind": "bg", "sessionId": BG_ID, "pid": 100,
             "pidDomain": "darwin", "procStart": self.TOKEN}))
        live(100)
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_proc_start_ft_takes_precedence(self, registry):
        """`procStartFt ?? procStart` is what the CLI compares."""
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID, procStartFt="133000000000000000")
        live(100)
        assert ccm_agentview.live_noninteractive_session_ids() == set()
        write(100, kind="bg", sessionId=BG_ID, procStartFt=self.TOKEN,
              procStart="something else")
        assert ccm_agentview.live_noninteractive_session_ids() == {BG_ID}

    def test_dead_process_is_not_live(self, registry):
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        tokens[100] = self.TOKEN
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_permission_error_is_not_live(self, registry, monkeypatch):
        """The CLI's `kill(pid, 0)` treats every error as not live,
        EPERM included."""
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        tokens[100] = self.TOKEN

        def kill(pid, sig):
            raise PermissionError(pid)
        monkeypatch.setattr(os, "kill", kill)
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_pid_one_or_less_is_not_live(self, registry):
        d, write, live, alive, tokens = registry
        write(1, kind="bg", sessionId=BG_ID)
        live(1)
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_start_token_mismatch_is_pid_reuse(self, registry):
        """Same pid, different start time: another process now owns
        the pid. The CLI compares `procStart` with `ps -o lstart=`."""
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID, procStart="Wed Sep  9 13:23:47 2026")
        live(100)  # token is TOKEN, one second and more away
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_unreadable_start_token_is_not_live(self, registry):
        """The CLI accepts a record whose token it cannot read; ccm
        does not — the miss side."""
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        alive.add(100)  # no token
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_record_without_proc_start_is_not_live(self, registry):
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID, procStart=None)
        live(100)
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_other_pid_domain_is_not_counted(self, registry):
        """The CLI keeps records from other domains unverified; ccm
        cannot verify them and leaves them out."""
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID, pidDomain="linux:abc")
        live(100)
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_unknown_own_domain_drops_domain_records(self, registry, monkeypatch):
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        monkeypatch.setattr(ccm_agentview, "own_pid_domain", lambda: None)
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_unreadable_record_makes_the_set_unknown(self, registry):
        """One broken record beside a good one: the CLI does not
        commit to a set, and `--continue` walks past the hand-off.
        Returning the good record alone would notify where the CLI
        resumes."""
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        (d / "101.json").write_text("{not json")
        assert ccm_agentview.live_noninteractive_session_ids() is None

    def test_non_dict_record_makes_the_set_unknown(self, registry):
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        (d / "102.json").write_text(json.dumps(["list"]))
        assert ccm_agentview.live_noninteractive_session_ids() is None

    def test_live_record_without_kind_or_session_id_makes_the_set_unknown(self, registry):
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        write(103, kind="bg")  # no sessionId
        assert ccm_agentview.live_noninteractive_session_ids() is None
        (d / "103.json").unlink()
        write(104, sessionId=OLD_ID)  # no kind
        assert ccm_agentview.live_noninteractive_session_ids() is None

    def test_non_numeric_files_are_ignored(self, registry):
        d, write, live, alive, tokens = registry
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        (d / "abc.json").write_text("{not json")
        (d / "100.deadbeef.key").write_text("k")
        assert ccm_agentview.live_noninteractive_session_ids() == {BG_ID}

    def test_missing_registry_dir_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path / "none"))
        assert ccm_agentview.live_noninteractive_session_ids() == set()

    def test_unlistable_registry_dir_is_unknown(self, tmp_path, monkeypatch):
        f = tmp_path / "file-not-dir"
        f.write_text("x")
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(f))
        assert ccm_agentview.live_noninteractive_session_ids() is None

    def test_saved_job_without_registry_record_is_not_live(
            self, fake_claude_home, registry, tmp_path, monkeypatch):
        """A job the daemon saved as blocked/working but with no
        registry record (no process) must not block: `--continue`
        reads the registry, not the job store. Real reader."""
        d, write, live, alive, tokens = registry
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(projects_dir))
        project_dir = tmp_path / "work" / "proj"
        project_dir.mkdir(parents=True)
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        home = projects_dir.parent.parent
        _write_roster(home, {"0b900000": {"pid": 0, "sessionId": BG_ID, "cwd": cwd}})
        _write_job_state(home, "0b900000", {"state": "blocked", "name": "x"})
        assert ccm_agentview.continue_blocker(cwd) is None
        # with a live, verified registry record for it, it does block
        write(100, kind="bg", sessionId=BG_ID)
        live(100)
        got = ccm_agentview.continue_blocker(cwd)
        assert got is not None and got.short == "0b900000"


class TestProcessStartToken:
    def _run(self, monkeypatch, **result):
        class R:
            returncode = result.get("rc", 0)
            stdout = result.get("out", b"")

        def fake_run(argv, **kw):
            assert argv == ["ps", "-o", "lstart=", "-p", "100"]
            assert kw["env"]["LC_ALL"] == "C" and kw["env"]["TZ"] == "UTC"
            exc = result.get("raise")
            if exc:
                raise exc
            return R()
        monkeypatch.setattr(ccm_agentview.subprocess, "run", fake_run)
        return ccm_agentview.process_start_token(100)

    def test_token_is_stripped(self, monkeypatch):
        assert self._run(monkeypatch, out=b"Thu Sep 10 00:37:10 2026\n") == "Thu Sep 10 00:37:10 2026"

    @pytest.mark.parametrize("failure", [
        dict(rc=1), dict(out=b""), dict(out=b"   \n"),
        dict(raise_=FileNotFoundError("ps")),
        dict(raise_=ccm_agentview.subprocess.TimeoutExpired("ps", 5)),
    ])
    def test_failures_read_as_none(self, monkeypatch, failure):
        kw = dict(failure)
        if "raise_" in kw:
            kw["raise"] = kw.pop("raise_")
        assert self._run(monkeypatch, **kw) is None


class TestContinueBlockerWarnings:
    def test_one_line_per_blocked_project(self, handoff_env):
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        projects = [
            ccm_core.Project("0:1", "1", "proj", cwd, "SHELL"),
            ccm_core.Project("0:2", "2", "other", "/nowhere", "IDLE"),
        ]
        msgs = ccm_agentview.continue_blocker_warnings(projects, [bg])
        assert len(msgs) == 1
        assert msgs[0].startswith("proj: ")
        assert "0b900000" in msgs[0]
        assert "claude attach 0b900000" in msgs[0]
        assert "claude stop 0b900000" in msgs[0]
        assert "(done)" in msgs[0]

    def test_only_shell_projects_are_reported(self, handoff_env):
        """A window already running Claude has no launch coming —
        it may be the background session itself, attached — so the
        notice would contradict what is on screen."""
        project_dir, projects_dir = handoff_env
        cwd = str(project_dir)
        _write_transcript(projects_dir, project_dir, OLD_ID,
                          _conversation(cwd) + _handoff(OLD_ID, BG_ID), 1000)
        bg = _bg("0b900000", BG_ID, cwd)
        for state in ("BUSY", "IDLE", "PERMIT", "IGNORED"):
            projects = [ccm_core.Project("0:1", "1", "proj", cwd, state)]
            assert ccm_agentview.continue_blocker_warnings(projects, [bg]) == [], state
        projects = [ccm_core.Project("0:1", "1", "proj", cwd, "SHELL")]
        assert len(ccm_agentview.continue_blocker_warnings(projects, [bg])) == 1

    def test_warnings_never_raise(self, monkeypatch):
        """status / doctor have no exception barrier of their own; a
        failing reader must not take the report down with it."""
        monkeypatch.setattr(ccm_agentview, "continue_blockers",
                            lambda *a, **kw: (_ for _ in ()).throw(TypeError("x")))
        logged = []
        monkeypatch.setattr("ccm_core.log_caught_exception", lambda scope: logged.append(scope))
        projects = [ccm_core.Project("0:1", "1", "proj", "/p", "SHELL")]
        assert ccm_agentview.continue_blocker_warnings(projects, []) == []
        assert logged == ["continue_blocker_warnings"]

    def test_empty_roster_is_empty(self):
        projects = [ccm_core.Project("0:1", "1", "proj", "/p", "SHELL")]
        assert ccm_agentview.continue_blocker_warnings(projects, []) == []

    def test_unknown_state_reads_as_live(self):
        bg = _bg("0b900000", BG_ID, "/p", state="UNKNOWN")
        assert "(live)" in ccm_agentview.format_continue_blocker("proj", bg)
