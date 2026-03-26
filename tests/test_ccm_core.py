"""Tests for ccm_core.py — state detection, helpers, and batch tmux commands."""

import os
import sys
import time
from unittest.mock import patch, MagicMock, call

import pytest

# Add lib to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import ccm_core


# ─── Fixtures ───

@pytest.fixture(autouse=True)
def reset_state():
    """Reset any module-level state between tests."""
    yield


def make_ps_lines(*entries):
    """Build ps output lines. Each entry: (pid, ppid, pgid, comm)."""
    lines = ["  PID  PPID  PGID COMM"]
    for pid, ppid, pgid, comm in entries:
        lines.append(f"  {pid}   {ppid}   {pgid} {comm}")
    return lines


# ─── find_claude_pid ───

class TestFindClaudePid:
    def test_finds_claude_child(self):
        ps = make_ps_lines((200, 100, 100, "claude"))
        assert ccm_core.find_claude_pid(100, ps) == "200"

    def test_returns_none_when_no_claude(self):
        ps = make_ps_lines((200, 100, 100, "bash"))
        assert ccm_core.find_claude_pid(100, ps) is None

    def test_ignores_claude_with_different_parent(self):
        ps = make_ps_lines((200, 999, 999, "claude"))
        assert ccm_core.find_claude_pid(100, ps) is None


# ─── has_children ───

class TestHasChildren:
    def test_true_when_child_exists(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 200, "node"))
        assert ccm_core.has_children("200", ps, "99999") is True

    def test_false_when_no_children(self):
        ps = make_ps_lines((200, 100, 100, "claude"))
        assert ccm_core.has_children("200", ps, "99999") is False

    def test_excludes_caffeinate(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 200, "caffeinate"))
        assert ccm_core.has_children("200", ps, "99999") is False

    def test_excludes_own_pgid(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 12345, "node"))
        assert ccm_core.has_children("200", ps, "12345") is False

    def test_true_with_non_caffeinate_alongside_caffeinate(self):
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "caffeinate"),
            (400, 200, 200, "node"),
        )
        assert ccm_core.has_children("200", ps, "99999") is True


# ─── detect_pane_state ───

class TestDetectPaneState:
    @patch("ccm_core.tmux_cmd")
    def test_shell_when_no_claude(self, mock_tmux):
        ps = make_ps_lines((100, 1, 100, "bash"))
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "SHELL"

    @patch("ccm_core.tmux_cmd")
    def test_idle_when_no_children(self, mock_tmux):
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_no_prompt(self, mock_tmux):
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Processing files...\nRunning tests..."
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_permit_with_children_and_permit_text(self, mock_tmux):
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Do you want to allow this?\n  Yes    No"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_idle_with_children_and_input_prompt(self, mock_tmux):
        """Background workers (MCP servers) + visible prompt = IDLE."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Some output\n❯ "
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_and_accept_edits_prompt(self, mock_tmux):
        """Accept-edits prompt (❯❯) should NOT be treated as idle."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running tests...\n❯❯ accept edits on"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"


# ─── detect_window_raw ───

class TestDetectWindowRaw:
    def test_down_when_no_panes(self):
        assert ccm_core.detect_window_raw("0:1", [], [], "99999") == "DOWN"

    @patch("ccm_core.tmux_cmd")
    def test_permit_takes_priority(self, mock_tmux):
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node"),
            (101, 1, 101, "bash"), (201, 101, 101, "claude"),
        )
        # First pane: PERMIT, second pane: IDLE
        mock_tmux.return_value = "Do you want to allow?\n  Yes  No"
        panes = [("0:1", "100", "%0"), ("0:1", "101", "%1")]
        assert ccm_core.detect_window_raw("0:1", panes, ps, "99999") == "PERMIT"


# ─── detect_window_state with hooks ───

class TestDetectWindowStateHooks:
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_idle_plus_hook_busy_returns_busy(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=BUSY → BUSY (text generation)."""
        mock_hook.return_value = (int(time.time()), "BUSY")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_idle_plus_hook_done_with_prompt_returns_done(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=DONE + prompt visible → DONE."""
        mock_hook.return_value = (int(time.time()), "DONE")
        mock_tmux.return_value = "Result\n❯ "
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "DONE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_idle_plus_hook_done_no_prompt_returns_busy(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=DONE + no prompt → BUSY (safety net)."""
        mock_hook.return_value = (int(time.time()), "DONE")
        mock_tmux.return_value = "Thinking..."
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_shell_ignores_hooks(self, mock_hook, mock_tmux):
        """raw=SHELL → SHELL regardless of hook signals."""
        mock_hook.return_value = (int(time.time()), "BUSY")
        ps = make_ps_lines((100, 1, 100, "bash"))  # No claude
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "SHELL"


# ─── Formatting helpers ───

class TestFormatElapsed:
    def test_seconds(self):
        ts = int(time.time()) - 30
        assert ccm_core.format_elapsed(ts) == "30s"

    def test_minutes(self):
        ts = int(time.time()) - 180
        assert ccm_core.format_elapsed(ts) == "3m"

    def test_hours(self):
        ts = int(time.time()) - 7200
        assert ccm_core.format_elapsed(ts) == "2h"

    def test_days(self):
        ts = int(time.time()) - 172800
        assert ccm_core.format_elapsed(ts) == "2d"

    def test_zero_returns_empty(self):
        assert ccm_core.format_elapsed(0) == ""

    def test_none_returns_empty(self):
        assert ccm_core.format_elapsed(None) == ""


class TestFormatDir:
    def test_fits_full(self):
        assert ccm_core.format_dir("/short", 10, 80) == "/short"

    def test_truncates_to_parent_base(self):
        long_dir = "/very/long/path/to/project"
        result = ccm_core.format_dir(long_dir, 60, 80)
        assert "…/" in result or result == "project"

    def test_returns_empty_when_too_narrow(self):
        assert ccm_core.format_dir("/some/path", 75, 80) == ""


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
