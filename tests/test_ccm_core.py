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


# ─── has_grandchildren ───

class TestHasGrandchildren:
    def test_false_when_only_direct_children(self):
        """MCP servers / language servers as direct children → no grandchildren."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "node"),               # MCP server
            (400, 200, 200, "sourcekit-lsp"),      # language server
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is False

    def test_true_when_bash_tool_running(self):
        """claude → bash → command (e.g. xcodebuild) — tool execution."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "bash"),
            (400, 300, 300, "xcodebuild"),
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is True

    def test_false_when_no_children(self):
        ps = make_ps_lines((200, 100, 100, "claude"))
        assert ccm_core.has_grandchildren("200", ps, "99999") is False

    def test_excludes_caffeinate_at_grandchild_level(self):
        """A bash child whose only grandchild is caffeinate is not a tool run."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "bash"),
            (400, 300, 300, "caffeinate"),
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is False

    def test_excludes_caffeinate_at_child_level(self):
        """caffeinate as a direct child is excluded from the children set,
        so its (hypothetical) own children do not count as claude grandchildren."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "caffeinate"),
            (400, 300, 300, "node"),
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is False

    def test_mixed_mcp_and_tool(self):
        """MCP server as direct child + bash → cmd as another branch → True."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "node"),               # MCP server (direct only)
            (400, 200, 200, "bash"),               # Bash tool
            (500, 400, 400, "xcodebuild"),         # tool subprocess
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is True


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
    def test_busy_with_children_permit_text_ignored(self, mock_tmux):
        """Generic 'Do you want to allow this?' text without the v2.1+ footer
        markers does NOT trigger PERMIT — only 'Tab to amend' / 'ctrl+e to explain'
        do. Children + ordinary text still resolves to BUSY."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Do you want to allow this?\n  Yes    No"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

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

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_and_new_accept_edits_prompt(self, mock_tmux):
        """Accept-edits prompt (⏵⏵) with leading spaces should NOT be treated as idle."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running tests...\n  ⏵⏵ accept edits on (shift+tab to cycle)"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_footer_tab_to_amend(self, mock_tmux):
        """Permission dialog footer 'Tab to amend' → PERMIT (hook-independent).

        Fallback for when Claude Code stops firing PermissionRequest hooks
        mid-session (anthropics/claude-code#16047).
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "Do you want to proceed?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "\n"
            "Esc to cancel · Tab to amend · ctrl+e to explain"
        )
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_footer_indented(self, mock_tmux):
        """Footer with leading whitespace still matches."""
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = "  Esc to cancel · Tab to amend · ctrl+e to explain"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_detected_even_with_children(self, mock_tmux):
        """PERMIT footer during parallel tool execution overrides BUSY."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running...\nEsc to cancel · Tab to amend · ctrl+e to explain"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_slash_menu_footer(self, mock_tmux):
        """'Enter to confirm · Esc to cancel' (slash menu) is NOT permission."""
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = "Choose a model\n❯ Opus\n  Sonnet\nEnter to confirm · Esc to cancel"
        # No "Esc to cancel · Tab to amend" prefix → falls through to IDLE
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_response_body_mentioning_footer(self, mock_tmux):
        """Claude response body containing 'ctrl+e to explain' in prose
        must not false-trigger PERMIT. Pattern is anchored to the start
        of a line with the 'Esc to cancel · ' prefix; in-body mentions
        always have leading text before the phrase.
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "⏺ In permit dialogs you can use ctrl+e to explain the\n"
            "  command, or Tab to amend it before approving.\n"
            "❯ "
        )
        # Input prompt visible, no footer → IDLE
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_inline_mention(self, mock_tmux):
        """Even a line that ENDS with 'ctrl+e to explain' but has other
        text first (e.g. a quoted example) should not match."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "  The footer says: Esc to cancel · Tab to amend · ctrl+e to explain"
        # Has indentation but the "The footer says:" prefix breaks the anchor
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_busy_when_grandchild_overrides_visible_prompt(self, mock_tmux):
        """Tool running (claude → bash → xcodebuild) + visible `❯ ` prompt
        from the v2.1+ background-tool UI → BUSY (grandchild signal wins)."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"),
            (200, 100, 100, "claude"),
            (300, 200, 200, "bash"),               # Bash tool
            (400, 300, 300, "xcodebuild"),         # subprocess
        )
        mock_tmux.return_value = (
            "✳ Doodling… (1m 57s · ↓ 646 tokens)\n"
            "─────\n"
            "❯ \n"
            "─────\n"
            "  ⏵⏵ accept edits on (shift+tab to cycle)"
        )
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_idle_when_only_mcp_children_and_prompt_visible(self, mock_tmux):
        """No grandchildren (only MCP/LSP direct children) + visible prompt → IDLE.
        Regression guard: the new grandchild path must not break the
        established 'background workers + ❯ = IDLE' rule."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"),
            (200, 100, 100, "claude"),
            (300, 200, 200, "node"),               # MCP server
            (400, 200, 200, "sourcekit-lsp"),      # LSP
        )
        mock_tmux.return_value = "Some output\n❯ "
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"


# ─── detect_window_raw ───

class TestDetectWindowRaw:
    def test_down_when_no_panes(self):
        assert ccm_core.detect_window_raw("0:1", [], [], "99999") == "DOWN"

    @patch("ccm_core.tmux_cmd")
    def test_busy_takes_priority_over_idle(self, mock_tmux):
        """PERMIT is hook-only; pane with children = BUSY, takes priority over IDLE."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node"),
            (101, 1, 101, "bash"), (201, 101, 101, "claude"),
        )
        mock_tmux.return_value = "Processing..."
        panes = [("0:1", "100", "%0"), ("0:1", "101", "%1")]
        assert ccm_core.detect_window_raw("0:1", panes, ps, "99999") == "BUSY"


# ─── detect_window_state with hooks ───

class TestDetectWindowStateHooks:
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_idle_plus_hook_busy_returns_busy(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=BUSY → BUSY (text generation)."""
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_hook_permit_with_raw_busy(self, mock_hook, mock_tmux):
        """raw=BUSY + hook=PERMIT → PERMIT.

        During permission dialog, process tree reports BUSY (background
        MCP servers etc.) and input prompt is not visible. PERMIT overrides.
        """
        hook_ts = int(time.time())
        mock_hook.return_value = (hook_ts, "PERMIT", "")
        mock_tmux.return_value = ""  # capture-pane: no input prompt visible
        # claude (200) has child process (300) → raw=BUSY
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"),
                           (300, 200, 100, "node"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", "", 0, panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_raw_permit_overrides_stale_busy_hook(self, mock_hook, mock_tmux):
        """capture-pane detects PERMIT footer + stale BUSY hook → PERMIT.

        End-to-end scenario for anthropics/claude-code#16047: Claude Code
        stopped firing PermissionRequest mid-session, so the hook signal
        is stuck on stale BUSY. The capture-pane fallback must win.
        """
        hook_ts = int(time.time()) - 600  # 10 min stale
        mock_hook.return_value = (hook_ts, "BUSY", "")
        mock_tmux.return_value = (
            "❯ 1. Yes\n  2. No\n"
            "Esc to cancel · Tab to amend · ctrl+e to explain"
        )
        # claude with no child (permission dialog pre-tool-spawn)
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", "", 0, panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_raw_permit_without_any_hook(self, mock_hook, mock_tmux):
        """capture-pane PERMIT + no hook signal at all → PERMIT."""
        mock_hook.return_value = None
        mock_tmux.return_value = (
            "  Esc to cancel · Tab to amend · ctrl+e to explain"
        )
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_permit_persists_when_raw_idle(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=PERMIT + prev=PERMIT → still PERMIT.

        After user responds to permission dialog, there's a brief IDLE gap
        before the tool subprocess starts. The fallback must NOT convert
        this to DONE — keep PERMIT until a hook signal (BUSY/DONE) arrives.
        """
        hook_ts = int(time.time()) - 3
        mock_hook.return_value = (hook_ts, "PERMIT", "")
        mock_tmux.return_value = ""
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "PERMIT", "", 0, panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_hook_busy_stays_busy_even_with_permit_text(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=BUSY + generic permission text (no footer marker) → BUSY.

        The v2.1+ capture-pane PERMIT fallback only triggers on 'Tab to amend'
        or 'ctrl+e to explain' — plain 'Do you want to proceed?' text still
        defers to the hook signal.
        """
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        mock_tmux.return_value = "Do you want to proceed?\n  1. Yes\n  2. No"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", "", 0, panes, ps, "99999"
        )
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_idle_plus_hook_done_with_prompt_returns_done(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=DONE + prompt visible → DONE."""
        mock_hook.return_value = (int(time.time()), "DONE", "")
        mock_tmux.return_value = "Result\n❯ "
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "DONE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_idle_plus_hook_done_trusted(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=DONE → DONE (trust hook, no capture-pane verification)."""
        mock_hook.return_value = (int(time.time()), "DONE", "")
        mock_tmux.return_value = ""  # capture-pane not called
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "DONE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_shell_ignores_hooks(self, mock_hook, mock_tmux):
        """raw=SHELL → SHELL regardless of hook signals."""
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        ps = make_ps_lines((100, 1, 100, "bash"))  # No claude
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "SHELL"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_accept_edits_without_children_returns_idle(self, mock_hook, mock_tmux):
        """Safety net: ⏵⏵ visible, no children → IDLE (waiting for user action)."""
        mock_hook.return_value = None  # No hook signal (expired)
        mock_tmux.return_value = "Some output\n❯ \n  ⏵⏵ accept edits on (shift+tab to cycle)"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_no_prompt_no_hook_returns_idle_not_busy(self, mock_hook, mock_tmux):
        """Safety net removed: no prompt, no hook → IDLE (trust process tree)."""
        mock_hook.return_value = None  # No hook signal
        mock_tmux.return_value = "Some tool output without prompt"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_session_end_hook_ignored_when_idle(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=SHELL → IDLE (process tree authoritative; stale SHELL signal ignored)."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_session_end_hook_with_shell_raw(self, mock_hook, mock_tmux):
        """raw=SHELL + hook=SHELL → SHELL (consistent)."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"))  # no claude process
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", "", 0, panes, ps, "99999"
        )
        assert state == "SHELL"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_session_end_hook_ignored_when_busy(self, mock_hook, mock_tmux):
        """raw=BUSY + hook=SHELL should not happen in practice, but raw=BUSY takes priority."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 100, "node"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", "", 0, panes, ps, "99999"
        )
        # raw=BUSY (children running), SHELL hook is ignored since condition is raw in ("SHELL", "IDLE")
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_no_hook_no_capture_permit(self, mock_hook, mock_tmux):
        """Without hook signal, PERMIT text on screen does NOT trigger PERMIT (hook-only detection)."""
        mock_hook.return_value = None
        mock_tmux.return_value = "Do you want to allow this?\n  Yes  No"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_permit_overrides_raw_busy(self, mock_hook, mock_tmux):
        """raw=BUSY + hook=PERMIT → PERMIT (background processes don't mask permission prompt)."""
        hook_ts = int(time.time())
        mock_hook.return_value = (hook_ts, "PERMIT", "")
        # window_activity is older (no user response yet)
        mock_tmux.return_value = str(hook_ts - 5)
        # Claude has child processes → raw would be BUSY
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"),
            (300, 200, 200, "node"),  # MCP server or other child
        )
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", "", 0, panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_permit_expires_after_max_timeout(self, mock_hook, mock_tmux):
        """Stale PERMIT signal (older than PERMIT_MAX_TIMEOUT) is ignored in hook path.

        When PERMIT expires, the hook check falls through. With prev_state=PERMIT,
        the fallback keeps PERMIT until a new hook signal arrives. But with
        prev_state=IDLE (no prior PERMIT), it would stay IDLE.
        """
        old_ts = int(time.time()) - ccm_core.PERMIT_MAX_TIMEOUT - 10
        mock_hook.return_value = (old_ts, "PERMIT", "")
        mock_tmux.return_value = str(old_ts - 5)
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        # With prev_state=IDLE: expired PERMIT doesn't resurrect
        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", "", 0, panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_busy_signal_clears_stale_permit(self, mock_hook, mock_tmux):
        """prev_state=PERMIT + hook=BUSY → BUSY (new signal clears old PERMIT)."""
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "PERMIT", "", 0, panes, ps, "99999"
        )
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_done_after_permit_within_settle_time(self, mock_hook, mock_tmux):
        """prev_state=PERMIT + hook=DONE (fresh) → BUSY (settle time).

        After user grants permission, the tool runs and Stop fires DONE.
        Within DONE_SETTLE_TIME, keep showing BUSY to reflect the tool
        execution that happens between PERMIT and DONE.
        """
        mock_hook.return_value = (int(time.time()), "DONE", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "PERMIT", "", 0, panes, ps, "99999"
        )
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_done_after_permit_past_settle_time(self, mock_hook, mock_tmux):
        """prev_state=PERMIT + hook=DONE (old) → DONE (settle time passed)."""
        mock_hook.return_value = (int(time.time()) - 5, "DONE", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state, _, _ = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "PERMIT", "", 0, panes, ps, "99999"
        )
        assert state == "DONE"


# ─── Declarative rule evaluation (pure) ───


def make_ctx(**overrides):
    """Build a DetectionContext with sensible defaults for rule testing."""
    now = int(time.time())
    defaults = dict(
        raw="IDLE",
        hook_state="",
        hook_ts=0,
        hook_age=-1,
        prev_state="IDLE",
        done_flag="",
        done_age=-1,
        last_done_ts=0,
        last_busy_age=-1,
        now=now,
    )
    defaults.update(overrides)
    return ccm_core.DetectionContext(**defaults)


class TestEvaluateRules:
    """Pure unit tests: each case asserts (matched_rule_name, resolved_state).

    No tmux, ps, or filesystem mocking — the Context is built directly.
    """

    # --- process-level ---

    def test_raw_down(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="DOWN"))
        assert (rule.name, state) == ("process_down", "DOWN")

    def test_raw_shell(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="SHELL"))
        assert (rule.name, state) == ("process_shell", "SHELL")

    def test_shell_beats_hook_busy(self):
        """Process tree authoritative: SHELL beats any hook signal."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="SHELL", hook_state="BUSY", hook_age=0)
        )
        assert (rule.name, state) == ("process_shell", "SHELL")

    # --- fresh BUSY hook fast path ---

    def test_hook_fresh_busy_over_raw_idle(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY", hook_age=1)
        )
        assert (rule.name, state) == ("hook_fresh_busy", "BUSY")

    def test_hook_fresh_busy_over_raw_busy_is_noop(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="BUSY", hook_state="BUSY", hook_age=0)
        )
        assert (rule.name, state) == ("hook_fresh_busy", "BUSY")

    def test_hook_stale_busy_slow_path(self):
        """Age >= 2 → slow path rule (hook_busy_idle)."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY", hook_age=5)
        )
        assert (rule.name, state) == ("hook_busy_idle", "BUSY")

    def test_hook_busy_trusted_regardless_of_age(self):
        """Long-running tool / text generation: BUSY hook age >5 min still wins.

        Regression guard: previously HOOK_TIMEOUT=300 capped this rule,
        causing fallback_busy_to_done to fire false DONE on long tasks.
        """
        for age in (60, 299, 400, 900, 3600, 86400):
            rule, state = ccm_core.evaluate_rules(
                make_ctx(raw="IDLE", hook_state="BUSY", hook_age=age,
                         prev_state="BUSY")
            )
            assert (rule.name, state) == ("hook_busy_idle", "BUSY"), (
                f"age={age} should still match hook_busy_idle"
            )

    # --- PERMIT ---

    def test_hook_permit_blocking_raw_busy(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="BUSY", hook_state="PERMIT", hook_age=3)
        )
        assert (rule.name, state) == ("hook_permit_blocking", "PERMIT")

    def test_hook_permit_idle_falls_through_to_fallback(self):
        """raw=IDLE means user moved past dialog; don't force PERMIT via rule 4."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="PERMIT", hook_age=3, prev_state="PERMIT")
        )
        assert rule.name == "fallback_permit_hold"
        assert state == "PERMIT"

    def test_hook_permit_expired_no_hold(self):
        """Expired PERMIT + prev=IDLE → default rule, state=IDLE."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="PERMIT",
                hook_age=ccm_core.PERMIT_MAX_TIMEOUT + 10,
                prev_state="IDLE",
            )
        )
        assert rule.name == "default"
        assert state == "IDLE"

    # --- DONE variants ---

    def test_hook_post_permit_tool(self):
        """prev=PERMIT + DONE within settle → BUSY + write_busy_file."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE", hook_state="DONE", hook_age=1, prev_state="PERMIT"
            )
        )
        assert (rule.name, state) == ("hook_post_permit_tool", "BUSY")
        assert rule.action == ccm_core.Action.WRITE_BUSY_FILE

    def test_hook_multiturn_boundary(self):
        """prev=BUSY + DONE with recent .busy file → BUSY (not genuine)."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="DONE",
                hook_age=2,
                prev_state="BUSY",
                last_busy_age=1,
            )
        )
        assert (rule.name, state) == ("hook_multiturn_boundary", "BUSY")

    def test_hook_multiturn_no_busy_file_passes_through(self):
        """prev=BUSY + DONE but no .busy file → genuine DONE."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="DONE",
                hook_age=2,
                prev_state="BUSY",
                last_busy_age=-1,
            )
        )
        assert (rule.name, state) == ("hook_done_genuine", "DONE")

    def test_hook_done_genuine(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="DONE", hook_age=2, prev_state="IDLE")
        )
        assert (rule.name, state) == ("hook_done_genuine", "DONE")
        assert rule.action == ccm_core.Action.SET_DONE_HOOK

    def test_hook_done_expired_falls_through(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="DONE",
                hook_age=ccm_core.DONE_TIMEOUT + 10,
                prev_state="IDLE",
            )
        )
        assert rule.name == "default"
        assert state == "IDLE"

    # --- fallback (no hooks) ---

    def test_fallback_busy_to_done(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="BUSY")
        )
        assert (rule.name, state) == ("fallback_busy_to_done", "DONE")
        assert rule.action == ccm_core.Action.SET_DONE_NOW

    def test_fallback_permit_hold(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE", prev_state="PERMIT",
                hook_state="PERMIT", hook_age=5,
            )
        )
        assert (rule.name, state) == ("fallback_permit_hold", "PERMIT")
        assert rule.action == ccm_core.Action.HOLD_NO_WRITE

    def test_fallback_permit_hold_requires_hook_signal(self):
        """Without an active PERMIT hook, do not hold PERMIT indefinitely.

        Regression guard: if Claude Code crashes during a permission
        dialog, the PERMIT hook eventually ages out but prev_state stays
        PERMIT in tmux. The old unconditional fallback_permit_hold would
        keep PERMIT forever. Now we require the hook signal to still be
        present and within PERMIT_MAX_TIMEOUT.
        """
        # No hook signal at all → fall through to default (IDLE)
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="PERMIT")
        )
        assert rule.name == "default"
        assert state == "IDLE"

        # PERMIT hook present but expired → fall through
        rule2, state2 = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                prev_state="PERMIT",
                hook_state="PERMIT",
                hook_age=ccm_core.PERMIT_MAX_TIMEOUT + 10,
            )
        )
        assert rule2.name == "default"
        assert state2 == "IDLE"

    def test_fallback_done_active(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", done_flag="1000", done_age=5)
        )
        assert (rule.name, state) == ("fallback_done_active", "DONE")

    def test_fallback_done_expired_clears(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                done_flag="1000",
                done_age=ccm_core.DONE_TIMEOUT + 5,
            )
        )
        assert (rule.name, state) == ("fallback_done_expired", "IDLE")
        assert rule.action == ccm_core.Action.CLEAR_DONE

    # --- raw_not_idle_clear ---

    def test_raw_busy_clears_done(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="BUSY"))
        assert (rule.name, state) == ("raw_not_idle_clear", "BUSY")
        assert rule.action == ccm_core.Action.CLEAR_DONE

    def test_raw_permit_clears_done(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="PERMIT"))
        assert (rule.name, state) == ("raw_not_idle_clear", "PERMIT")

    # --- default ---

    def test_default_pure_idle(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="IDLE"))
        assert rule.name == "default"
        assert state == "IDLE"


class TestRuleMatching:
    """Directly exercise Rule.matches() edge cases."""

    def test_hook_in_requires_signal_present(self):
        rule = ccm_core.Rule(name="t", hook_in=("BUSY",), hook_age_lt=10)
        # hook_state="" should NOT match even if hook_age_lt is satisfied by -1
        assert not rule.matches(make_ctx(hook_state="", hook_age=-1))

    def test_hook_age_lt_rejects_missing_signal(self):
        rule = ccm_core.Rule(name="t", hook_age_lt=10)
        assert not rule.matches(make_ctx(hook_age=-1))

    def test_busy_age_lt_rejects_missing_busy_file(self):
        rule = ccm_core.Rule(name="t", busy_age_lt=5)
        assert not rule.matches(make_ctx(last_busy_age=-1))

    def test_done_valid_true_requires_flag(self):
        rule = ccm_core.Rule(name="t", done_valid=True)
        assert not rule.matches(make_ctx(done_flag="", done_age=-1))
        assert rule.matches(make_ctx(done_flag="100", done_age=5))

    def test_done_valid_false_requires_flag_present(self):
        """done_valid=False means flag exists but expired — not 'no flag'."""
        rule = ccm_core.Rule(name="t", done_valid=False)
        assert not rule.matches(make_ctx(done_flag="", done_age=-1))
        assert rule.matches(
            make_ctx(done_flag="100", done_age=ccm_core.DONE_TIMEOUT + 1)
        )

    def test_wildcard_matches_all(self):
        rule = ccm_core.Rule(name="wild")
        assert rule.matches(make_ctx())
        assert rule.matches(make_ctx(raw="BUSY", hook_state="DONE"))


class TestApplyActions:
    """Direct tests for the side-effect layer.

    Uses a tmp dir for the hook signal path and mocks `_set_win_state`
    so we can assert both tmux writes and filesystem writes per action.
    """

    @pytest.fixture
    def project_dir(self, tmp_path, monkeypatch):
        # Redirect hook signal files to an isolated tmp dir
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        proj = tmp_path / "proj"
        proj.mkdir()
        return str(proj)

    def _run(self, rule, ctx, project_dir="", win_target="0:1"):
        with patch.object(ccm_core, "_set_win_state") as set_win:
            result = ccm_core.apply_actions(
                win_target, project_dir, ctx, rule, rule.result
            )
        return result, set_win

    def test_default_writes_state_keeps_done(self):
        rule = ccm_core.Rule(name="t", result="BUSY", action=ccm_core.Action.DEFAULT)
        ctx = make_ctx(done_flag="100", last_done_ts=100)
        (state, df, ldt), set_win = self._run(rule, ctx)
        assert state == "BUSY" and df == "100" and ldt == 100
        set_win.assert_called_once_with("0:1", "BUSY")

    def test_clear_done_unsets_flag(self):
        rule = ccm_core.Rule(
            name="t", result="SHELL", action=ccm_core.Action.CLEAR_DONE
        )
        ctx = make_ctx(done_flag="100", last_done_ts=100)
        (state, df, ldt), set_win = self._run(rule, ctx)
        assert state == "SHELL" and df == ""
        set_win.assert_called_once_with("0:1", "SHELL", unset_done=True)

    def test_hold_no_write_skips_tmux(self):
        rule = ccm_core.Rule(
            name="t", result="PERMIT", action=ccm_core.Action.HOLD_NO_WRITE
        )
        ctx = make_ctx(done_flag="99", last_done_ts=99)
        (state, df, ldt), set_win = self._run(rule, ctx)
        assert state == "PERMIT" and df == "99" and ldt == 99
        set_win.assert_not_called()

    def test_set_done_now_uses_ctx_now(self):
        rule = ccm_core.Rule(
            name="t", result="DONE", action=ccm_core.Action.SET_DONE_NOW
        )
        ctx = make_ctx(now=12345)
        (state, df, ldt), set_win = self._run(rule, ctx)
        assert state == "DONE" and df == "12345" and ldt == 12345
        set_win.assert_called_once_with("0:1", "DONE", done=12345, last_done=12345)

    def test_set_done_hook_uses_hook_ts(self):
        rule = ccm_core.Rule(
            name="t", result="DONE", action=ccm_core.Action.SET_DONE_HOOK
        )
        ctx = make_ctx(hook_ts=98765, hook_state="DONE", hook_age=1)
        (state, df, ldt), set_win = self._run(rule, ctx)
        assert state == "DONE" and df == "98765" and ldt == 98765
        set_win.assert_called_once_with("0:1", "DONE", done=98765, last_done=98765)

    def test_write_busy_file_creates_file(self, project_dir):
        rule = ccm_core.Rule(
            name="t", result="BUSY", action=ccm_core.Action.WRITE_BUSY_FILE
        )
        ctx = make_ctx(now=55555)
        (state, _, _), _ = self._run(rule, ctx, project_dir=project_dir)
        assert state == "BUSY"
        busy_file = ccm_core._hook_signal_path(project_dir) + ".busy"
        assert os.path.exists(busy_file)
        with open(busy_file) as f:
            assert f.read().strip() == "55555"

    def test_write_busy_file_no_project_dir_is_safe(self):
        """Action.WRITE_BUSY_FILE with empty project_dir should not crash."""
        rule = ccm_core.Rule(
            name="t", result="BUSY", action=ccm_core.Action.WRITE_BUSY_FILE
        )
        ctx = make_ctx(now=1)
        (state, _, _), set_win = self._run(rule, ctx, project_dir="")
        assert state == "BUSY"
        set_win.assert_called_once_with("0:1", "BUSY")


class TestFastPath:
    """evaluate_fast uses the same DETECTION_RULES as the slow path,
    so the statusline and dashboard can never disagree on state logic.
    """

    @pytest.fixture
    def project_dir(self, tmp_path, monkeypatch):
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        proj = tmp_path / "proj"
        proj.mkdir()
        return str(proj)

    def _write_hook(self, project_dir, state, age=0):
        path = ccm_core._hook_signal_path(project_dir)
        ts = int(time.time()) - age
        with open(path, "w") as f:
            f.write(f"{ts} {state}")

    # --- basic prev_state → state propagation ---

    def test_prev_idle_no_hook(self, project_dir):
        assert ccm_core.evaluate_fast("IDLE", "", project_dir) == "IDLE"

    def test_prev_busy_no_hook_stays_busy(self, project_dir):
        """Without ps info, prev=BUSY stays BUSY via rule raw_not_idle_clear."""
        assert ccm_core.evaluate_fast("BUSY", "", project_dir) == "BUSY"

    def test_prev_permit_no_hook_stays_permit(self, project_dir):
        assert ccm_core.evaluate_fast("PERMIT", "", project_dir) == "PERMIT"

    def test_prev_shell_stays_shell(self, project_dir):
        assert ccm_core.evaluate_fast("SHELL", "", project_dir) == "SHELL"

    def test_prev_done_with_valid_flag(self, project_dir):
        """prev=DONE + fresh done_flag → DONE via fallback_done_active."""
        now = int(time.time())
        assert ccm_core.evaluate_fast("DONE", str(now - 5), project_dir) == "DONE"

    def test_prev_done_with_expired_flag(self, project_dir):
        """prev=DONE + stale done_flag → IDLE via fallback_done_expired."""
        now = int(time.time())
        expired = str(now - ccm_core.DONE_TIMEOUT - 10)
        assert ccm_core.evaluate_fast("DONE", expired, project_dir) == "IDLE"

    # --- hook overrides ---

    def test_hook_busy_overrides_idle(self, project_dir):
        self._write_hook(project_dir, "BUSY", age=1)
        assert ccm_core.evaluate_fast("IDLE", "", project_dir) == "BUSY"

    def test_hook_permit_overrides_busy(self, project_dir):
        self._write_hook(project_dir, "PERMIT", age=2)
        assert ccm_core.evaluate_fast("BUSY", "", project_dir) == "PERMIT"

    def test_hook_busy_trusted_regardless_of_age(self, project_dir):
        """Regression guard: no HOOK_TIMEOUT cap in fast path either."""
        self._write_hook(project_dir, "BUSY", age=900)
        assert ccm_core.evaluate_fast("IDLE", "", project_dir) == "BUSY"

    def test_hook_done_fresh_from_idle(self, project_dir):
        self._write_hook(project_dir, "DONE", age=1)
        assert ccm_core.evaluate_fast("IDLE", "", project_dir) == "DONE"

    def test_hook_permit_expired(self, project_dir):
        """Stale PERMIT hook + prev=IDLE → IDLE (not stuck PERMIT)."""
        self._write_hook(
            project_dir, "PERMIT",
            age=ccm_core.PERMIT_MAX_TIMEOUT + 10,
        )
        assert ccm_core.evaluate_fast("IDLE", "", project_dir) == "IDLE"

    # --- no project dir ---

    def test_no_project_dir(self):
        """evaluate_fast with empty project_dir skips hook read gracefully."""
        assert ccm_core.evaluate_fast("BUSY", "", "") == "BUSY"


class TestLifecycleSequences:
    """End-to-end state transition sequences, evaluated as pure rule chains.

    Each test walks a realistic Claude Code lifecycle (user prompt → tool
    execution → permission → completion) and asserts that the rule table
    produces the right state at every step. No tmux/ps/file mocking —
    Context is constructed directly so we focus on detection logic.
    """

    def _eval(self, **ctx_kwargs):
        rule, state = ccm_core.evaluate_rules(make_ctx(**ctx_kwargs))
        return rule.name, state

    def test_simple_turn(self):
        """IDLE → BUSY(fresh) → BUSY(slow) → DONE(genuine) → IDLE(held)."""
        # Initial idle
        assert self._eval(raw="IDLE", prev_state="IDLE") == ("default", "IDLE")
        # UserPromptSubmit fires BUSY hook (< 2s)
        assert self._eval(
            raw="IDLE", prev_state="IDLE", hook_state="BUSY", hook_age=0
        ) == ("hook_fresh_busy", "BUSY")
        # Text generation continues, pipeline still sees IDLE
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="BUSY", hook_age=5
        ) == ("hook_busy_idle", "BUSY")
        # Stop fires DONE, no recent .busy → genuine completion
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="DONE", hook_age=1,
            last_busy_age=-1,
        ) == ("hook_done_genuine", "DONE")
        # Next cycle: DONE flag still valid
        assert self._eval(
            raw="IDLE", prev_state="DONE", done_flag="999999999", done_age=2,
        ) == ("fallback_done_active", "DONE")
        # After DONE_TIMEOUT the flag expires
        assert self._eval(
            raw="IDLE", prev_state="DONE",
            done_flag="1", done_age=ccm_core.DONE_TIMEOUT + 1,
        ) == ("fallback_done_expired", "IDLE")

    def test_multi_turn_false_done_suppressed(self):
        """BUSY → (Stop fires DONE between tools) → BUSY must be kept.

        Real scenario: Claude uses Bash, Stop fires, PreToolUse fires
        again within 1-2s for another Bash call. The .busy file was
        touched by PreToolUse, so DONE is within busy_age_lt window.
        """
        # Tool execution in progress
        assert self._eval(
            raw="BUSY", prev_state="BUSY", hook_state="BUSY", hook_age=0,
        ) == ("hook_fresh_busy", "BUSY")
        # Stop fires, but .busy was just touched → multi-turn boundary
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="DONE", hook_age=1,
            last_busy_age=1,
        ) == ("hook_multiturn_boundary", "BUSY")
        # Next PreToolUse immediately fires BUSY again
        assert self._eval(
            raw="BUSY", prev_state="BUSY", hook_state="BUSY", hook_age=0,
            last_busy_age=0,
        ) == ("hook_fresh_busy", "BUSY")

    def test_permit_lifecycle(self):
        """BUSY → PERMIT → (user approves) → BUSY(post-permit) → DONE."""
        # Tool wants permission: PreToolUse fired BUSY, then
        # PermissionRequest fired PERMIT. raw=BUSY (background MCP).
        assert self._eval(
            raw="BUSY", prev_state="BUSY", hook_state="PERMIT", hook_age=0,
        ) == ("hook_permit_blocking", "PERMIT")
        # User sees dialog for a while
        assert self._eval(
            raw="BUSY", prev_state="PERMIT", hook_state="PERMIT", hook_age=5,
        ) == ("hook_permit_blocking", "PERMIT")
        # User approves; brief IDLE gap before tool actually runs.
        # hook=PERMIT still in file (not cleared yet), raw=IDLE now.
        # Rule 4 declines (raw=IDLE); rule 10 holds PERMIT.
        assert self._eval(
            raw="IDLE", prev_state="PERMIT", hook_state="PERMIT", hook_age=6,
        ) == ("fallback_permit_hold", "PERMIT")
        # Tool runs → Stop fires DONE within SETTLE → post-permit BUSY.
        assert self._eval(
            raw="IDLE", prev_state="PERMIT", hook_state="DONE", hook_age=1,
        ) == ("hook_post_permit_tool", "BUSY")
        # Next cycle: prev=BUSY now. Even if hook=DONE still fresh,
        # multi-turn rule keeps BUSY because WRITE_BUSY_FILE refreshed
        # last_busy_age to 0 in the previous step.
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="DONE", hook_age=2,
            last_busy_age=1,
        ) == ("hook_multiturn_boundary", "BUSY")
        # Tool finishes cleanly; no more BUSY refresh, genuine DONE.
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="DONE", hook_age=2,
            last_busy_age=ccm_core.DONE_SETTLE_TIME + 5,
        ) == ("hook_done_genuine", "DONE")

    def test_fallback_no_hooks(self):
        """Hook signals absent entirely (old config or disabled)."""
        # Text generation: raw=BUSY from process tree
        assert self._eval(raw="BUSY", prev_state="IDLE") == (
            "raw_not_idle_clear", "BUSY",
        )
        # Prompt returns: raw=IDLE, prev=BUSY → DONE via fallback
        assert self._eval(raw="IDLE", prev_state="BUSY") == (
            "fallback_busy_to_done", "DONE",
        )

    def test_long_running_tool_stays_busy(self):
        """Long bash / text generation: BUSY hook goes stale but state must stay BUSY.

        Real incident (jwriter, 2026-04-10): a multi-minute tool chain
        produced no PreToolUse refresh for >5 min. With the old HOOK_TIMEOUT
        cap, the hook path failed and fallback_busy_to_done fired false DONE,
        then fallback_done_expired → IDLE. Now rule hook_busy_idle has no
        age limit so BUSY is maintained as long as the hook says BUSY.
        """
        # Tool starts: fresh BUSY
        assert self._eval(
            raw="IDLE", hook_state="BUSY", hook_age=0, prev_state="BUSY",
        ) == ("hook_fresh_busy", "BUSY")
        # Still going at 1 min
        assert self._eval(
            raw="IDLE", hook_state="BUSY", hook_age=60, prev_state="BUSY",
        ) == ("hook_busy_idle", "BUSY")
        # Past old HOOK_TIMEOUT boundary (was the regression)
        assert self._eval(
            raw="IDLE", hook_state="BUSY", hook_age=301, prev_state="BUSY",
        ) == ("hook_busy_idle", "BUSY")
        # 15 minutes in
        assert self._eval(
            raw="IDLE", hook_state="BUSY", hook_age=900, prev_state="BUSY",
        ) == ("hook_busy_idle", "BUSY")
        # Finally Stop fires → DONE
        assert self._eval(
            raw="IDLE", hook_state="DONE", hook_age=0, prev_state="BUSY",
            last_busy_age=905,
        ) == ("hook_done_genuine", "DONE")

    def test_shell_override_anywhere(self):
        """SHELL from process tree wins over any hook state, any prev."""
        for prev in ("IDLE", "BUSY", "PERMIT", "DONE"):
            for hook in ("", "BUSY", "DONE", "PERMIT"):
                name, state = self._eval(
                    raw="SHELL", prev_state=prev, hook_state=hook, hook_age=0,
                )
                assert state == "SHELL", f"prev={prev} hook={hook}"


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

class TestSanitizeSnapshotName:
    def test_basic(self):
        assert ccm_core._sanitize_snapshot_name("my-snapshot") == "my-snapshot"

    def test_path_traversal(self):
        assert ccm_core._sanitize_snapshot_name("../../etc/passwd") == "passwd"

    def test_slash(self):
        assert ccm_core._sanitize_snapshot_name("foo/bar") == "bar"

    def test_dots_only(self):
        with pytest.raises(SystemExit):
            ccm_core._sanitize_snapshot_name("..")

    def test_empty(self):
        with pytest.raises(SystemExit):
            ccm_core._sanitize_snapshot_name("")


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

class TestSnapshotSave:
    @patch("ccm_core.tmux_cmd")
    def test_creates_json(self, mock_tmux, tmp_path):
        mock_tmux.return_value = "1\twin1\tproj1\t/home/user/dir1\n2\twin2\tproj2\t/home/user/dir2"
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        ccm_core.cmd_snapshot_save("test-snap", quiet=True)
        import json
        fp = tmp_path / "test-snap.json"
        assert fp.exists()
        data = json.loads(fp.read_text())
        assert data["name"] == "test-snap"
        assert data["version"] == 1
        assert len(data["projects"]) == 2

    @patch("ccm_core.tmux_cmd")
    def test_skips_empty_project(self, mock_tmux, tmp_path):
        mock_tmux.return_value = "1\twin1\t\t/dir1\n2\twin2\tproj2\t/dir2"
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        ccm_core.cmd_snapshot_save("test2", quiet=True)
        import json
        data = json.loads((tmp_path / "test2.json").read_text())
        assert len(data["projects"]) == 1


# ─── cmd_snapshot_load ───

class TestSnapshotLoad:
    def _write_snapshot(self, tmp_path, name, projects):
        import json
        snap = {"version": 1, "name": name, "created": "2025-01-01T00:00:00+0000",
                "projects": projects}
        fp = tmp_path / f"{name}.json"
        fp.write_text(json.dumps(snap))

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.hooks_configured", return_value=True)
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.get_session", return_value="main")
    def test_load_creates_windows(self, mock_session, mock_tmux, mock_batch, mock_hooks, mock_auto, tmp_path):
        """Loading a snapshot creates windows for each project."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        # Create a real temp directory for the project
        proj_dir = tmp_path / "myproject"
        proj_dir.mkdir()

        self._write_snapshot(tmp_path, "test-snap", [
            {"name": "proj1", "dir": str(proj_dir)},
        ])

        # find_window returns None (project doesn't exist yet), new-window returns "1"
        def tmux_side_effect(*args, **kwargs):
            if args[0] == "list-windows" and "-a" in args:
                return ""  # no existing projects (for autosave)
            if args[0] == "list-windows":
                return ""  # no existing ccm windows
            if args[0] == "new-window":
                return "1"
            if args[0] == "display-message":
                return "proj1"
            return ""
        mock_tmux.side_effect = tmux_side_effect

        ccm_core.cmd_snapshot_load("test-snap")

        # Verify new-window was called
        new_window_calls = [c for c in mock_tmux.call_args_list if c[0][0] == "new-window"]
        assert len(new_window_calls) == 1

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    @patch("ccm_core.get_session", return_value="main")
    @patch("ccm_core.tmux_cmd", return_value="")
    def test_load_skips_missing_dir(self, mock_tmux, mock_session, tmp_path, capsys):
        """Projects with missing directories are skipped with a warning."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        self._write_snapshot(tmp_path, "test-skip", [
            {"name": "gone", "dir": "/nonexistent/path/xyz"},
        ])

        ccm_core.cmd_snapshot_load("test-skip")

        captured = capsys.readouterr()
        assert "Directory not found" in captured.err

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    @patch("ccm_core.find_window", return_value="1")
    @patch("ccm_core.get_session", return_value="main")
    @patch("ccm_core.tmux_cmd", return_value="")
    def test_load_skips_existing_project(self, mock_tmux, mock_session, mock_find, tmp_path, capsys):
        """Projects that already exist are skipped with a warning."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        proj_dir = tmp_path / "existing"
        proj_dir.mkdir()

        self._write_snapshot(tmp_path, "test-dup", [
            {"name": "existing-proj", "dir": str(proj_dir)},
        ])

        ccm_core.cmd_snapshot_load("test-dup")

        captured = capsys.readouterr()
        assert "already exists" in captured.err

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    def test_load_nonexistent_snapshot(self, tmp_path):
        """Loading a non-existent snapshot exits with error."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        with pytest.raises(SystemExit):
            ccm_core.cmd_snapshot_load("nonexistent")

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    @patch("ccm_core.get_session", return_value="main")
    @patch("ccm_core.tmux_cmd", return_value="")
    def test_load_skips_null_entries(self, mock_tmux, mock_session, tmp_path):
        """Null/empty project entries in snapshot are silently skipped."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        self._write_snapshot(tmp_path, "test-null", [
            {"name": "", "dir": "/some/path"},
            {"name": "null", "dir": "/some/path"},
            {"name": "valid", "dir": "null"},
        ])

        # Should not raise — all entries skipped
        ccm_core.cmd_snapshot_load("test-null")

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir


# ─── cmd_add ───

class TestCmdAdd:
    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.hooks_configured", return_value=True)
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.get_session", return_value="main")
    def test_add_creates_window(self, mock_session, mock_tmux, mock_batch, mock_hooks, mock_auto, tmp_path):
        """cmd_add creates a new tmux window with correct metadata tags."""
        proj_dir = tmp_path / "my-project"
        proj_dir.mkdir()

        def tmux_side_effect(*args, **kwargs):
            if args[0] == "list-windows":
                return ""  # no existing windows
            if args[0] == "new-window":
                return "3"
            if args[0] == "display-message":
                return "my-project"
            return ""
        mock_tmux.side_effect = tmux_side_effect

        ccm_core.cmd_add(str(proj_dir), "my-project")

        # Verify tmux_batch was called to set metadata
        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        tag_names = [a[3] for a in batch_args if len(a) > 3]
        assert "@ccm_project" in tag_names
        assert "@ccm_dir" in tag_names

    def test_add_missing_dir_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_add("/nonexistent/directory/xyz")

    def test_add_empty_dir_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_add("")

    @patch("ccm_core.find_window", return_value="1")
    @patch("ccm_core.get_session", return_value="main")
    def test_add_duplicate_name_exits(self, mock_session, mock_find, tmp_path):
        proj_dir = tmp_path / "dup"
        proj_dir.mkdir()
        with pytest.raises(SystemExit):
            ccm_core.cmd_add(str(proj_dir), "existing")

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.hooks_configured", return_value=True)
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.get_session", return_value="main")
    def test_add_defaults_name_to_basename(self, mock_session, mock_tmux, mock_batch, mock_hooks, mock_auto, tmp_path):
        proj_dir = tmp_path / "cool-project"
        proj_dir.mkdir()

        def tmux_side_effect(*args, **kwargs):
            if args[0] == "list-windows":
                return ""
            if args[0] == "new-window":
                return "1"
            if args[0] == "display-message":
                return "cool-project"
            return ""
        mock_tmux.side_effect = tmux_side_effect

        ccm_core.cmd_add(str(proj_dir))

        # Check @ccm_project was set to basename
        batch_args = mock_batch.call_args[0]
        project_tag = [a for a in batch_args if len(a) > 3 and a[3] == "@ccm_project"]
        assert project_tag[0][4] == "cool-project"

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.hooks_configured", return_value=True)
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.get_session", return_value="main")
    def test_add_loading_skips_autosave(self, mock_session, mock_tmux, mock_batch, mock_hooks, mock_auto, tmp_path):
        proj_dir = tmp_path / "loading-test"
        proj_dir.mkdir()

        def tmux_side_effect(*args, **kwargs):
            if args[0] == "list-windows":
                return ""
            if args[0] == "new-window":
                return "1"
            if args[0] == "display-message":
                return "loading-test"
            return ""
        mock_tmux.side_effect = tmux_side_effect

        ccm_core.cmd_add(str(proj_dir), "loading-test", start_claude=False, _loading=True)

        mock_auto.assert_not_called()


# ─── cmd_unregister ───

class TestCmdUnregister:
    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_removes_tags(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_core.cmd_unregister("my-proj")

        # Should call tmux_batch to remove all tags
        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        # Every command should be a set-option -u (unset)
        for cmd in batch_args:
            assert "-u" in cmd

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_restores_original_name(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_core.cmd_unregister("my-proj")

        # Should call rename-window with original name
        rename_calls = [c for c in mock_tmux.call_args_list
                        if c[0][0] == "rename-window"]
        assert len(rename_calls) == 1
        assert rename_calls[0][0][-1] == "orig-name"

    def test_unregister_empty_name_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_unregister("")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_core.cmd_unregister("nonexistent")

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_triggers_autosave(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_core.cmd_unregister("proj")
        mock_auto.assert_called_once()


# ─── cmd_rename ───

class TestCmdRename:
    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_updates_tag_and_window(self, mock_session, mock_find, mock_batch, mock_auto):
        # find_window returns index for old name, None for new name (not duplicate)
        mock_find.side_effect = lambda sess, name: "1" if name == "old" else None

        ccm_core.cmd_rename("old", "new")

        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        # Should set @ccm_project to "new" and rename window
        set_cmd = [a for a in batch_args if "set-option" in a[0] and "@ccm_project" in a]
        assert set_cmd[0][-1] == "new"
        rename_cmd = [a for a in batch_args if "rename-window" in a[0]]
        assert rename_cmd[0][-1] == "new"

    def test_rename_empty_old_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_rename("", "new")

    def test_rename_empty_new_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_rename("old", "")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_core.cmd_rename("nonexistent", "new")

    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_duplicate_exits(self, mock_session, mock_find):
        # Both old and new names exist
        mock_find.return_value = "1"
        with pytest.raises(SystemExit):
            ccm_core.cmd_rename("old", "taken")

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_triggers_autosave(self, mock_session, mock_find, mock_batch, mock_auto):
        mock_find.side_effect = lambda sess, name: "1" if name == "old" else None
        ccm_core.cmd_rename("old", "new")
        mock_auto.assert_called_once()


# ─── raise_on_die / CCMError ───

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
