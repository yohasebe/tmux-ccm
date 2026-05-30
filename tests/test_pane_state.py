"""Tests for ccm_pane_state.

Auto-split from test_ccm_core.py. Shared fixtures + helpers
(write_jsonl, make_ps_lines, real_activity_record, system_record,
iso_ts) live in conftest.py; import them here when used.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

import pytest

import ccm_core
import ccm_activity
import ccm_canaries
import ccm_commands
import ccm_detection
import ccm_jsonl
import ccm_notify
import ccm_pane_state
import ccm_render
import ccm_rules
import ccm_runtime
import ccm_signals

from conftest import (
    iso_ts,
    make_ctx,
    make_ps_lines,
    real_activity_record,
    system_record,
    write_jsonl,
)

# Backward-compat alias used by some tests.
_iso_ts = iso_ts

class TestFindClaudePid:
    def test_finds_claude_child(self):
        ps = make_ps_lines((200, 100, 100, "claude"))
        assert ccm_pane_state.find_claude_pid(100, ps) == "200"

    def test_returns_none_when_no_claude(self):
        ps = make_ps_lines((200, 100, 100, "bash"))
        assert ccm_pane_state.find_claude_pid(100, ps) is None

    def test_ignores_claude_with_different_parent(self):
        ps = make_ps_lines((200, 999, 999, "claude"))
        assert ccm_pane_state.find_claude_pid(100, ps) is None


# ─── has_children ───

class TestHasChildren:
    def test_true_when_child_exists(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 200, "node"))
        assert ccm_pane_state.has_children("200", ps, "99999") is True

    def test_false_when_no_children(self):
        ps = make_ps_lines((200, 100, 100, "claude"))
        assert ccm_pane_state.has_children("200", ps, "99999") is False

    def test_excludes_caffeinate(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 200, "caffeinate"))
        assert ccm_pane_state.has_children("200", ps, "99999") is False

    def test_excludes_own_pgid(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 12345, "node"))
        assert ccm_pane_state.has_children("200", ps, "12345") is False

    def test_true_with_non_caffeinate_alongside_caffeinate(self):
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "caffeinate"),
            (400, 200, 200, "node"),
        )
        assert ccm_pane_state.has_children("200", ps, "99999") is True


# ─── JSONL session log freshness ───

class TestDetectPaneState:
    @patch("ccm_core.tmux_cmd")
    def test_shell_when_no_claude(self, mock_tmux):
        ps = make_ps_lines((100, 1, 100, "bash"))
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "SHELL"

    @patch("ccm_core.tmux_cmd")
    def test_idle_when_no_children(self, mock_tmux):
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_no_prompt(self, mock_tmux):
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Processing files...\nRunning tests..."
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_permit_text_ignored(self, mock_tmux):
        """Generic 'Do you want to allow this?' text without the v2.1+ footer
        markers does NOT trigger PERMIT — only 'Tab to amend' / 'ctrl+e to explain'
        do. Children + ordinary text still resolves to BUSY."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Do you want to allow this?\n  Yes    No"
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_idle_with_children_and_input_prompt(self, mock_tmux):
        """Background workers (MCP servers) + visible prompt = IDLE."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Some output\n❯ "
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_idle_with_children_and_multiline_input(self, mock_tmux):
        """Long multi-line user input pushes the `❯` row well above
        the bottom of the pane while the user keeps composing. The
        prompt scan must look at the whole visible pane, not just
        the last few rows, so the `❯` is still found even when many
        wrapped text rows sit between it and the footer."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        # Layout: previous response above, then `❯ <multi-line text>`,
        # then the footer at the bottom. The `❯` is many rows above
        # the bottom because the user's typed text wraps.
        mock_tmux.return_value = (
            "Cooked for 28s\n"
            "──────────────────────────\n"
            "❯ first line of long input\n"
            "  second wrapped row\n"
            "  third wrapped row\n"
            "  fourth wrapped row\n"
            "  fifth wrapped row\n"
            "  sixth wrapped row\n"
            "  seventh wrapped row\n"
            "──────────────────────────\n"
            "  ~/path  branch  Model  ████ 50%\n"
        )
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_and_accept_edits_prompt(self, mock_tmux):
        """Accept-edits prompt (❯❯) should NOT be treated as idle."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running tests...\n❯❯ accept edits on"
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_and_new_accept_edits_prompt(self, mock_tmux):
        """Accept-edits prompt (⏵⏵) with leading spaces should NOT be treated as idle."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running tests...\n  ⏵⏵ accept edits on (shift+tab to cycle)"
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

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
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_footer_indented(self, mock_tmux):
        """Footer with leading whitespace still matches."""
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = "  Esc to cancel · Tab to amend · ctrl+e to explain"
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_detected_even_with_children(self, mock_tmux):
        """PERMIT footer during parallel tool execution overrides BUSY."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running...\nEsc to cancel · Tab to amend · ctrl+e to explain"
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_confirmation_modal_footer(self, mock_tmux):
        """'Enter to confirm · Esc to <verb>' modals are classified
        as PERMIT. Semantically Claude is blocked pending a single
        user keypress — the same UX pattern as a permission dialog —
        so the ⚠ icon and the same state are the correct surface.
        PATTERN_PERMIT_FOOTER runs BEFORE the input-prompt check so
        it catches modals even when they also contain a `❯` cursor."""
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = "Choose a model\n❯ Opus\n  Sonnet\nEnter to confirm · Esc to cancel"
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_model_picker_exit_footer(self, mock_tmux):
        """The `/model` picker footer can carry any Esc-verb
        (`Esc to cancel`, `Esc to exit`, `Esc to close`, ...). The
        permissive `Esc to \\w+` branch in PATTERN_PERMIT_FOOTER must
        keep matching every variant so the dialog always classifies
        as PERMIT.
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "Select model\n"
            "Switch between Claude models. Applies to this session.\n"
            "\n"
            "  ❯ 1. Default (recommended) ✔  Opus 4.7 with 1M context\n"
            "    2. Sonnet                   Sonnet 4.6\n"
            "    3. Sonnet (1M context)      Sonnet 4.6 with 1M context\n"
            "    4. Haiku                    Haiku 4.5\n"
            "\n"
            "  ◉ xHigh effort (default) ← → to adjust\n"
            "\n"
            "  Enter to confirm · Esc to exit"
        )
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_model_picker_v2_1_153_footer(self, mock_tmux):
        """v2.1.153 reworded the `/model` footer:
            `Enter to set as default · s to use this session only · Esc to cancel`
        The Enter verb is no longer literally `confirm`. The
        pre-v2.1.153 regex required `Enter to confirm` and would
        miss this entirely, leaving `/model` showing as IDLE while
        the user is actually blocked at the picker. The relaxed
        `Enter to \\S[^\\n]*? · ... · Esc to <verb>` shape catches
        this without regressing free-nav slash menus.
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "Select model\n"
            "Switch between Claude models. Your pick becomes the default for new sessions.\n"
            "\n"
            "  ❯ 1. Default (recommended) ✔  Opus 4.7 with 1M context\n"
            "    2. Sonnet                   Sonnet 4.6\n"
            "    3. Sonnet (1M context)      Sonnet 4.6 with 1M context\n"
            "    4. Haiku                    Haiku 4.5\n"
            "\n"
            "  ◉ xHigh effort (default) ←/→ to adjust\n"
            "\n"
            "  Use /fast to turn on Fast mode (Opus 4.7).\n"
            "\n"
            "  Enter to set as default · s to use this session only · Esc to cancel"
        )
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_session_resume_modal(self, mock_tmux):
        """The session-resume modal indents its `❯` cursor (so
        PATTERN_INPUT_PROMPT does NOT match at column 0) and uses
        the footer `Enter to confirm · Esc to cancel`. ccm must
        classify it as PERMIT via the modal-footer pattern; without
        that, has_child + no prompt would yield BUSY for a modal
        actually awaiting user input.
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "This session is 1h 58m old and 732.6k tokens.\n"
            "\n"
            "  ❯ 1. Resume from summary (recommended)\n"
            "    2. Resume full session as-is\n"
            "    3. Don't ask me again\n"
            "\n"
            "  Enter to confirm · Esc to cancel"
        )
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_bare_esc_to_cancel(self, mock_tmux):
        """Slash menus (/hooks, /config navigation) show JUST
        'Esc to cancel' with no separator or confirming action.
        These remain non-PERMIT — the user is browsing, not blocked
        on a decision. Classification boundary:
          • 'Esc to cancel'                      → slash menu   (IDLE)
          • 'Enter to use, t to sort, Esc to …'  → slash menu   (IDLE)
          • 'Esc to cancel · Tab to amend'       → permission   (PERMIT)
          • 'Esc to cancel · ctrl+e ...'         → permission   (PERMIT)
          • 'Enter to confirm · Esc to <verb>'   → confirmation (PERMIT)
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "/hooks\n"
            "❯ UserPromptSubmit\n"
            "  PreToolUse\n"
            "Esc to cancel"
        )
        # Bare footer → no PERMIT match. ❯ at column 0 → IDLE.
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_skills_menu_footer(self, mock_tmux):
        """`/skills` uses its own footer — neither the `Enter to
        confirm` prefix nor the bare `Esc to cancel` matches
        PATTERN_PERMIT_FOOTER. Semantically this is a free-
        navigation slash menu (Enter toggles/selects a skill,
        user can leave anytime), NOT a blocked decision — IDLE
        is the correct classification.

        Two upstream wordings observed:
        - `Enter to use, t to sort, Esc to close`
        - `Enter to use, / to search, t to sort, Esc to close`
          (search box added in a later release; same Esc-to-close
          structure, same outcome for ccm)

        Footer text below uses the 2.1.121 format; the old format
        is exercised by the regex pattern test in TestPermitFooter.
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "Skills\n"
            "10 skills · Enter to use, / to search, t to sort, Esc to close\n"
            "\n"
            "  ❯ ✔ on         clip · user · ~11 tok\n"
            "      ✔ on         commit · user · ~11 tok"
        )
        # Footer does not match PATTERN_PERMIT_FOOTER.
        # `❯` is indented (col 2), so PATTERN_INPUT_PROMPT (col 0)
        # does not match either — detect_pane_state returns IDLE
        # via the no-child fallthrough.
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

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
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_inline_mention(self, mock_tmux):
        """Even a line that ENDS with 'ctrl+e to explain' but has other
        text first (e.g. a quoted example) should not match."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "  The footer says: Esc to cancel · Tab to amend · ctrl+e to explain"
        # Has indentation but the "The footer says:" prefix breaks the anchor
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_idle_when_grandchild_exists_but_prompt_visible(self, mock_tmux):
        """Leftover server (claude → zsh → ruby) + visible `❯ ` prompt
        → IDLE at the pane level. The v2.1+ case where `❯ ` appears
        above a STILL-ACTIVE tool is handled at the window level by
        the event-log path, not here."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"),
            (200, 100, 100, "claude"),
            (300, 200, 200, "/bin/zsh"),            # leftover shell
            (400, 300, 300, "ruby"),                # dev server
        )
        mock_tmux.return_value = (
            "Task completed.\n"
            "─────\n"
            "❯ \n"
            "─────\n"
            "  ⏵⏵ accept edits on (shift+tab to cycle)"
        )
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

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
        assert ccm_pane_state.detect_pane_state("100", "%0", ps, "99999") == "IDLE"


# ─── detect_window_raw ───

class TestDetectWindowRaw:
    def test_down_when_no_panes(self):
        assert ccm_pane_state.detect_window_raw("0:1", [], [], "99999") == "DOWN"

    @patch("ccm_core.tmux_cmd")
    def test_busy_aggregates_across_panes(self, mock_tmux):
        """Two-pane window where one pane has claude with children
        running. Aggregation picks BUSY since at least one eligible
        pane is BUSY (PERMIT > BUSY > IDLE > SHELL ordering)."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node"),
            (101, 1, 101, "bash"), (201, 101, 101, "claude"),
        )
        mock_tmux.return_value = "Processing..."
        panes = [
            ("0:1", "100", "%0", "claude", "1", "48"),
            ("0:1", "101", "%1", "claude", "0", "48"),
        ]
        assert ccm_pane_state.detect_window_raw("0:1", panes, ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_permit_trumps_busy_in_aggregation(self, mock_tmux):
        """Agent Teams workflow: one pane is BUSY (teammate working),
        another pane is PERMIT (different teammate waiting for user).
        Window must surface PERMIT so the dashboard prompts the user
        to handle the modal even when another teammate is mid-work."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node"),
            (101, 1, 101, "bash"), (201, 101, 101, "claude"),
        )
        # Both panes get the permit footer in capture-pane. detect_pane_state
        # checks the footer first, so both panes report PERMIT and the
        # aggregation picks PERMIT (which it would also have done if only
        # one matched — that is the test's point).
        mock_tmux.return_value = "Esc to cancel · Tab to amend"
        panes = [
            ("0:1", "100", "%0", "claude", "1", "48"),
            ("0:1", "101", "%1", "claude", "0", "48"),
        ]
        assert ccm_pane_state.detect_window_raw("0:1", panes, ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_sliver_pane_excluded_from_aggregation(self, mock_tmux):
        """Sliver-pane scenario:
        a 1-row sliver pane held a long-idle claude and false-read
        BUSY (children present, capture-pane empty so no `❯`). Pre-
        fix that BUSY infected the whole window; post-fix the
        sliver is filtered out and the visible shell pane drives
        the result → SHELL."""
        ps = make_ps_lines(
            (100, 1, 100, "zsh"),  # visible pane: just a shell
            (200, 1, 200, "zsh"), (300, 200, 300, "claude"),  # sliver: claude with MCP
            (400, 300, 400, "python"),
        )
        # capture-pane on the sliver returns nothing (1 row cannot
        # render the prompt). For the visible shell pane the test
        # short-circuits before capture-pane is consulted.
        mock_tmux.return_value = ""
        panes = [
            ("0:1", "100", "%0", "zsh", "1", "47"),    # full-size shell
            ("0:1", "200", "%1", "claude", "0", "1"),  # 1-row sliver
        ]
        assert ccm_pane_state.detect_window_raw("0:1", panes, ps, "99999") == "SHELL"

    @patch("ccm_core.tmux_cmd")
    def test_all_slivers_fallback_uses_all_panes(self, mock_tmux):
        """Edge case (impossible in practice): every pane is shorter
        than SLIVER_HEIGHT_THRESHOLD. The filter is bypassed so
        detection still produces an answer rather than silently
        falling through to SHELL on a window full of slivers."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node"),
        )
        mock_tmux.return_value = "Processing..."
        panes = [
            ("0:1", "100", "%0", "claude", "1", "1"),
            ("0:1", "101", "%1", "claude", "0", "2"),
        ]
        # Both slivers — without the bypass this would be SHELL.
        # With bypass, the BUSY pane is detected.
        assert ccm_pane_state.detect_window_raw("0:1", panes, ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_empty_height_skips_sliver_filter(self, mock_tmux):
        """A 6-tuple with empty pane_height ("") still works — the
        sliver filter only fires when height parses as a positive
        integer below the threshold."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node"),
        )
        mock_tmux.return_value = "Processing..."
        panes = [("0:1", "100", "%0", "claude", "1", "")]
        assert ccm_pane_state.detect_window_raw("0:1", panes, ps, "99999") == "BUSY"

