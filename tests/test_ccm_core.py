"""Tests for ccm_core.py — state detection, helpers, and batch tmux commands."""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

import pytest

# Add lib to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import ccm_core
import ccm_activity
import ccm_commands
import ccm_detection
import ccm_pane_state
import ccm_render
import ccm_rules
import ccm_canaries
import ccm_jsonl
import ccm_notify
import ccm_signals


# ─── Fixtures ───

@pytest.fixture(autouse=True)
def reset_state():
    """Reset any module-level state between tests."""
    yield


def _iso_ts(unix_ts):
    """Format a unix timestamp as the ISO 8601 string Claude Code writes
    into JSONL records (UTC, milliseconds, trailing Z)."""
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def write_jsonl(path, records):
    """Write a list of dict records as one JSON-per-line to `path`.
    `records` may include `system/away_summary`, `user`, `assistant`,
    etc. — whatever the test needs to simulate."""
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def real_activity_record(unix_ts, role="assistant"):
    """Build a minimal user/assistant JSONL record at the given ts."""
    return {"type": role, "timestamp": _iso_ts(unix_ts), "message": {"content": "x"}}


def system_record(unix_ts, subtype="away_summary"):
    """Build a minimal system metadata record (e.g. recap)."""
    return {"type": "system", "subtype": subtype, "timestamp": _iso_ts(unix_ts)}


def make_ps_lines(*entries):
    """Build ps output lines. Each entry: (pid, ppid, pgid, comm)."""
    lines = ["  PID  PPID  PGID COMM"]
    for pid, ppid, pgid, comm in entries:
        lines.append(f"  {pid}   {ppid}   {pgid} {comm}")
    return lines


# ─── find_claude_pid ───

class TestParseEtime:
    """Unit tests for the ps `etime` parser that feeds claude_pid_age
    into the detection context. Malformed input must return -1 so
    detection rules keyed on `claude_pid_age_lt` cleanly skip rather
    than false-match."""

    def test_seconds_only(self):
        assert ccm_core._parse_etime("45") == 45

    def test_minutes_seconds(self):
        assert ccm_core._parse_etime("01:30") == 90
        assert ccm_core._parse_etime("2:15") == 135

    def test_hours_minutes_seconds(self):
        assert ccm_core._parse_etime("01:02:03") == 3723

    def test_days_prefix(self):
        # "1-02:03:04" → 1 day + 2h + 3m + 4s
        assert ccm_core._parse_etime("1-02:03:04") == 86400 + 2*3600 + 3*60 + 4

    def test_empty(self):
        assert ccm_core._parse_etime("") == -1

    def test_malformed(self):
        assert ccm_core._parse_etime("not-a-time") == -1
        assert ccm_core._parse_etime("abc:def") == -1



class TestFindProcessAge:
    """`find_process_age` reads the etime column from `ps_snapshot`
    output. Pin the expected column position: etime is `parts[4]` in
    the `pid ppid pgid comm etime` format."""

    def test_returns_age_for_matching_pid(self):
        ps_lines = [
            "  PID  PPID  PGID COMM              ELAPSED",
            "  100     1     1 claude             01:30",
            "  200   100   100 node               00:45",
        ]
        assert ccm_core.find_process_age(100, ps_lines) == 90
        assert ccm_core.find_process_age(200, ps_lines) == 45

    def test_returns_minus_one_for_missing_pid(self):
        ps_lines = ["  100     1     1 claude             01:30"]
        assert ccm_core.find_process_age(999, ps_lines) == -1

    def test_returns_minus_one_when_etime_missing(self):
        # Old ps_snapshot format (pre-etime); parts[4] doesn't exist.
        ps_lines = ["  100     1     1 claude"]
        assert ccm_core.find_process_age(100, ps_lines) == -1

    def test_returns_minus_one_when_etime_unparseable(self):
        ps_lines = ["  100     1     1 claude   garbage"]
        assert ccm_core.find_process_age(100, ps_lines) == -1



class TestHooksConfigured:
    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_false_when_no_settings(self, mock_open):
        assert ccm_core.hooks_configured() is False


# ─── tmux_batch ───


class TestTmuxBatch:
    @patch("subprocess.run")
    def test_single_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        ccm_core.tmux_batch(("set-option", "-wt", "0:1", "@key", "val"))
        args = mock_run.call_args[0][0]
        assert args == ["tmux", "set-option", "-wt", "0:1", "@key", "val"]

    @patch("subprocess.run")
    def test_multiple_commands(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        ccm_core.tmux_batch(
            ("set-option", "-wt", "0:1", "@key1", "val1"),
            ("set-option", "-wt", "0:1", "@key2", "val2"),
        )
        args = mock_run.call_args[0][0]
        assert ";" in args
        assert args.count(";") == 1

    @patch("subprocess.run")
    def test_empty_batch_no_call(self, mock_run):
        ccm_core.tmux_batch()
        mock_run.assert_not_called()



class TestNonUtf8Output:
    """`ps` and `capture-pane` can emit byte sequences that are not
    valid UTF-8: macOS truncates the `comm` column at a fixed byte
    width, slicing multi-byte characters mid-codepoint, and
    capture-pane returns whatever bytes the pane currently shows.
    Both `ps_snapshot` and `tmux_cmd` must survive — a UnicodeDecodeError
    here would propagate up and silently kill the entire detection
    cycle, leaving every project's `@ccm_prev_state` frozen."""

    @patch("subprocess.run")
    def test_ps_snapshot_survives_truncated_multibyte(self, mock_run):
        # `⌘` is U+2318 → bytes \xe2\x8c\x98. macOS ps truncated the
        # comm column mid-codepoint, leaving an orphan \xe2\x8c.
        bad_bytes = b"100 1 100 /Applications/\xe2\x8c    01:17:15\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=bad_bytes)
        out = ccm_core.ps_snapshot()
        # Should not raise; replacement chars are fine because comm
        # column is only used for prefix matching, not display.
        assert "100 1 100" in out

    @patch("subprocess.run")
    def test_tmux_cmd_survives_invalid_utf8(self, mock_run):
        bad_bytes = b"line1\n\xe2\x8c truncated\nline3"
        mock_run.return_value = MagicMock(returncode=0, stdout=bad_bytes)
        out = ccm_core.tmux_cmd("capture-pane", "-p")
        assert "line1" in out
        assert "line3" in out



class TestLogCaughtException:
    """`log_caught_exception` records silent-catch sites so the next
    detection-cycle regression is debuggable without enabling
    CCM_DEBUG_TRACE in advance."""

    def test_writes_one_record_inside_except(self, tmp_path):
        log_path = tmp_path / "errors.log"
        with patch.object(ccm_core, "CCM_ERRORS_LOG", str(log_path)):
            try:
                raise ValueError("synthetic boom")
            except ValueError:
                ccm_core.log_caught_exception("test_scope")
        text = log_path.read_text()
        assert text.count("\n") == 1
        record = json.loads(text)
        assert record["scope"] == "test_scope"
        assert record["type"] == "ValueError"
        assert record["msg"] == "synthetic boom"
        assert "traceback" in record

    def test_outside_except_block_writes_nothing(self, tmp_path):
        log_path = tmp_path / "errors.log"
        with patch.object(ccm_core, "CCM_ERRORS_LOG", str(log_path)):
            ccm_core.log_caught_exception("never_called")
        assert not log_path.exists()

    def test_size_cap_rotates_to_dot_one(self, tmp_path):
        log_path = tmp_path / "errors.log"
        prev_path = tmp_path / "errors.log.1"
        # Pre-fill the log past the cap to simulate a long-running
        # process that has already accumulated errors.
        log_path.write_text("x" * (ccm_core.ERRORS_LOG_MAX_BYTES + 1))
        with patch.object(ccm_core, "CCM_ERRORS_LOG", str(log_path)), \
                patch.object(ccm_core, "CCM_ERRORS_LOG_PREV", str(prev_path)):
            try:
                raise RuntimeError("after-cap")
            except RuntimeError:
                ccm_core.log_caught_exception("rotated")
        # Old log moved aside, fresh log holds exactly the new record.
        assert prev_path.exists()
        new_text = log_path.read_text()
        assert new_text.count("\n") == 1
        record = json.loads(new_text)
        assert record["scope"] == "rotated"

    def test_subsequent_writes_after_rotation_go_to_active(self, tmp_path):
        log_path = tmp_path / "errors.log"
        prev_path = tmp_path / "errors.log.1"
        log_path.write_text("x" * (ccm_core.ERRORS_LOG_MAX_BYTES + 1))
        with patch.object(ccm_core, "CCM_ERRORS_LOG", str(log_path)), \
                patch.object(ccm_core, "CCM_ERRORS_LOG_PREV", str(prev_path)):
            for i in range(3):
                try:
                    raise RuntimeError(f"e{i}")
                except RuntimeError:
                    ccm_core.log_caught_exception(f"call_{i}")
        # Active log accumulates after the rotation; .1 still has the
        # pre-rotation epoch (unchanged).
        assert log_path.read_text().count("\n") == 3
        assert prev_path.stat().st_size == ccm_core.ERRORS_LOG_MAX_BYTES + 1



class TestBuildProjectListIsolation:
    """A bug in detection for one project must not freeze every
    other project's state. The per-project barrier carries forward
    `@ccm_prev_state` on detect-call failure (worst case: that
    project is stale for a tick) instead of letting the loop die
    and freezing all projects."""

    @patch.object(ccm_rules, "evaluate_fast")
    @patch.object(ccm_core, "tmux_cmd")
    def test_one_failing_project_does_not_break_others(
        self, mock_tmux, mock_evaluate
    ):
        # Two projects: first raises, second resolves to IDLE.
        mock_tmux.return_value = (
            "0:1\tproj-a\t/p/a\tBUSY\t0\t1234567890\t\n"
            "0:2\tproj-b\t/p/b\tIDLE\t0\t1234567890\t"
        )

        def evaluate_side_effect(prev_state, proj_dir, now=None, **kwargs):
            if proj_dir == "/p/a":
                raise RuntimeError("synthetic detection failure")
            return "IDLE"

        mock_evaluate.side_effect = evaluate_side_effect

        with patch.object(ccm_core, "log_caught_exception") as mock_log:
            projects = ccm_core.build_project_list(fast=True)

        assert len(projects) == 2
        names = {p.name: p.state for p in projects}
        # proj-a carries forward prev_state=BUSY (stale, but not lost)
        assert names["proj-a"] == "BUSY"
        # proj-b unaffected by proj-a's failure
        assert names["proj-b"] == "IDLE"
        # The silent failure was logged for diagnosis
        scopes = [c.args[0] for c in mock_log.call_args_list]
        assert any("proj-a" in s for s in scopes)
        assert not any("proj-b" in s for s in scopes)



class TestBuildProjectListSubprocessCount:
    """Regression guard against the N+1 tmux subprocess class of
    perf bug. `build_project_list` should issue a bounded number of
    tmux subprocess calls regardless of how many projects exist —
    the per-project session_id resolution must come from the bulk
    `list-windows` query, not a per-project lookup."""

    def _windows_raw(self, n):
        """Build a `list-windows` output for `n` projects, each with
        an `@ccm_session_id` field populated. The 8-column format
        matches the production query."""
        rows = []
        for i in range(n):
            rows.append(
                f"0:{i+1}\tproj-{i}\t/p/{i}\tIDLE\t0\t1234567890\t\t"
                f"sid-{i}-uuid-uuid-uuid-uuid-uuid"
            )
        return "\n".join(rows)

    def test_fast_path_subprocess_count_bounded(self, monkeypatch):
        """Fast path: list-windows for project metadata only. No
        per-project tmux lookup once cached_session_id is threaded
        through. Allowed: bounded constants for environment / mock
        checks and the one bulk list-windows."""
        n_projects = 10
        windows_out = self._windows_raw(n_projects)
        calls = []
        def fake_tmux(*args, **kwargs):
            calls.append(args[0] if args else "")
            if args and args[0] == "list-windows":
                return windows_out
            return ""
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake_tmux)
        # evaluate_fast doesn't need to touch the real path; stub.
        monkeypatch.setattr(ccm_rules, "evaluate_fast",
                            lambda *a, **k: "IDLE")

        ccm_core.build_project_list(fast=True)

        # The N+1 bug would have produced 10 list-windows calls
        # (one per project via _session_id_from_tmux). The fix
        # makes it exactly 1.
        list_windows_calls = [c for c in calls if c == "list-windows"]
        assert len(list_windows_calls) == 1, (
            f"fast path issued {len(list_windows_calls)} list-windows "
            f"calls for {n_projects} projects — N+1 regression"
        )

    def test_slow_path_show_option_count_bounded(self, monkeypatch):
        """Slow path: should NOT call show-option @ccm_session_id
        per project. The cached value from the bulk list-windows is
        passed through `cached_session_id`, so the per-project
        show-option in `build_detection_context` is bypassed."""
        n_projects = 10
        windows_out = self._windows_raw(n_projects)
        calls = []
        def fake_tmux(*args, **kwargs):
            calls.append(args[:3] if len(args) >= 3 else args)
            if args and args[0] == "list-windows":
                return windows_out
            return ""
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake_tmux)
        # Stub the heavy detection so we only exercise the dispatch
        # plumbing (we're measuring tmux calls, not detection logic).
        monkeypatch.setattr(ccm_detection, "detect_window_state",
                            lambda *a, **k: "IDLE")
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")

        ccm_core.build_project_list(fast=False)

        # show-option @ccm_session_id should be called 0 times
        # (cached_session_id from list-windows is passed through).
        # Other show-options (e.g. CCM_MOCK_STATE) are allowed but
        # bounded; we count only @ccm_session_id specifically.
        sid_show_options = [
            c for c in calls
            if len(c) >= 3 and c[0] == "show-option"
            and "@ccm_session_id" in str(c)
        ]
        assert len(sid_show_options) == 0, (
            f"slow path issued {len(sid_show_options)} show-option "
            f"@ccm_session_id calls for {n_projects} projects — "
            "cached_session_id wasn't threaded through"
        )


# ─── validate_name ───


class TestValidateName:
    def test_basic(self):
        assert ccm_core.validate_name("my-project") == "my-project"

    def test_whitespace_to_hyphens(self):
        assert ccm_core.validate_name("my project  name") == "my-project-name"

    def test_strip_dangerous_chars(self):
        assert ccm_core.validate_name("test;rm -rf") == "testrm-rf"

    def test_strip_quotes(self):
        assert ccm_core.validate_name("it's a \"test\"") == "its-a-test"

    def test_strip_leading_trailing_hyphens(self):
        assert ccm_core.validate_name("--foo--") == "foo"

    def test_empty_returns_empty(self):
        assert ccm_core.validate_name("") == ""

    def test_all_dangerous_returns_empty(self):
        assert ccm_core.validate_name("$();&") == ""

    def test_tabs_and_newlines(self):
        assert ccm_core.validate_name("a\tb\nc") == "a-b-c"


# ─── find_window / project_exists ───


class TestFindWindow:
    @patch("ccm_core.tmux_cmd")
    def test_found(self, mock_tmux):
        mock_tmux.return_value = "1\tmy-proj\n2\tother"
        assert ccm_core.find_window("main", "my-proj") == "1"

    @patch("ccm_core.tmux_cmd")
    def test_not_found(self, mock_tmux):
        mock_tmux.return_value = "1\tother"
        assert ccm_core.find_window("main", "missing") is None

    @patch("ccm_core.tmux_cmd")
    def test_empty_output(self, mock_tmux):
        mock_tmux.return_value = ""
        assert ccm_core.find_window("main", "any") is None



class TestProjectExists:
    @patch("ccm_core.find_window", return_value="1")
    def test_exists(self, _):
        assert ccm_core.project_exists("main", "proj") is True

    @patch("ccm_core.find_window", return_value=None)
    def test_not_exists(self, _):
        assert ccm_core.project_exists("main", "proj") is False


# ─── list_windows_raw ───


class TestListWindowsRaw:
    @patch("ccm_core.tmux_cmd")
    def test_returns_tagged_only(self, mock_tmux):
        mock_tmux.return_value = "1\twin1\tproj1\t/dir1\n2\twin2\t\t/dir2\n3\twin3\tproj3\t/dir3"
        result = ccm_core.list_windows_raw("main")
        assert len(result) == 2
        assert result[0] == ("1", "win1", "proj1", "/dir1")
        assert result[1] == ("3", "win3", "proj3", "/dir3")

    @patch("ccm_core.tmux_cmd")
    def test_empty(self, mock_tmux):
        mock_tmux.return_value = ""
        assert ccm_core.list_windows_raw("main") == []


# ─── snapshot save/list (with mocked tmux) ───


class TestRaiseOnDie:
    def test_ccm_die_exits_by_default(self):
        with pytest.raises(SystemExit):
            ccm_core.ccm_die("boom")

    def test_ccm_die_raises_inside_context(self):
        with ccm_core.raise_on_die():
            with pytest.raises(ccm_core.CCMError, match="boom"):
                ccm_core.ccm_die("boom")

    def test_context_restores_previous_mode(self):
        with ccm_core.raise_on_die():
            pass
        # After exit, default behavior (exit) must be restored
        with pytest.raises(SystemExit):
            ccm_core.ccm_die("after")

    def test_nested_context(self):
        with ccm_core.raise_on_die():
            with ccm_core.raise_on_die():
                with pytest.raises(ccm_core.CCMError):
                    ccm_core.ccm_die("inner")
            # Outer context still active
            with pytest.raises(ccm_core.CCMError):
                ccm_core.ccm_die("outer")

    def test_other_thread_unaffected(self):
        """raise_on_die() is thread-local: other threads keep exit behavior."""
        import threading
        result = {}

        def worker():
            try:
                ccm_core.ccm_die("from worker")
            except SystemExit:
                result["exited"] = True
            except ccm_core.CCMError:
                result["raised"] = True

        with ccm_core.raise_on_die():
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        assert result.get("exited") is True
        assert "raised" not in result


