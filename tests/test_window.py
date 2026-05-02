"""Tests for ccm_window — auto-focus on attach + window-state
mutation helpers.

`reset_window_after_attach` interacts with the cluster-SHELL
canary (`@ccm_shell_history` reset on attach), so its end-to-end
tests stay in `test_canaries.py` next to the canary itself. This
file owns the auto-focus pane-selection logic.
"""

from unittest.mock import patch

import pytest

import ccm_core
import ccm_window

from conftest import make_ps_lines


class TestAutoFocusAttentionPane:
    def _stub_tmux(self, monkeypatch, panes_raw, proj_dir="/tmp/proj"):
        """Stub tmux_cmd: return show-option for @ccm_dir, the
        provided list-panes output, and record select-pane calls."""
        select_calls = []

        def fake(*args):
            if args[:2] == ("show-option", "-wqv") and args[-1] == "@ccm_dir":
                return proj_dir
            if args[0] == "list-panes":
                return panes_raw
            if args[0] == "select-pane":
                select_calls.append(args)
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        return select_calls

    def test_no_op_when_proj_dir_missing(self, monkeypatch):
        """Non-ccm window (no @ccm_dir tag): no list-panes call, no
        select-pane call. Same guard as the rest of
        reset_window_after_attach — auto-focus is a ccm feature, it
        must not touch arbitrary user windows."""
        select_calls = self._stub_tmux(monkeypatch, "", proj_dir="")
        ccm_window.auto_focus_attention_pane("0:5")
        assert select_calls == []

    def test_no_op_on_single_pane_window(self, monkeypatch):
        """Single-pane windows have no panes to choose between."""
        panes_raw = "100\t%0\tclaude\t1\t48"
        select_calls = self._stub_tmux(monkeypatch, panes_raw)
        ccm_window.auto_focus_attention_pane("0:5")
        assert select_calls == []

    def test_focuses_permit_pane_when_active_is_not_permit(
        self, monkeypatch
    ):
        """Active pane is a shell, inactive pane has a permission
        modal up. Auto-focus must switch to the permit pane."""
        # Make the inactive pane (%1) be the one that capture-pane
        # reports as showing a permit footer. The active pane (%0)
        # has cmd=zsh so detect_pane_state short-circuits to SHELL
        # without consulting the capture-pane mock.
        ps = make_ps_lines(
            (100, 1, 100, "zsh"),  # active pane: shell
            (200, 1, 200, "zsh"), (300, 200, 300, "claude"),  # inactive: claude
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot",
                            lambda: "\n".join(ps))
        # list-panes output: pane_pid \t pane_id \t current_command
        # \t pane_active \t pane_height
        panes_raw = (
            "100\t%0\tzsh\t1\t48\n"
            "200\t%1\tclaude\t0\t48"
        )

        # tmux_cmd dispatch: show-option for @ccm_dir, list-panes for
        # the panes string, capture-pane returns permit footer for ANY
        # pane (only the claude pane will reach that branch of
        # detect_pane_state because the shell pane short-circuits to
        # SHELL via current_command='zsh').
        select_calls = []

        def fake(*args):
            if args[:2] == ("show-option", "-wqv") and args[-1] == "@ccm_dir":
                return "/tmp/proj"
            if args[0] == "list-panes":
                return panes_raw
            if args[0] == "select-pane":
                select_calls.append(args)
                return ""
            if args[0] == "capture-pane":
                return "Esc to cancel · Tab to amend"
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        ccm_window.auto_focus_attention_pane("0:5")

        assert any("%1" in c for c in select_calls), (
            f"select-pane was not called with %1: {select_calls}"
        )

    def test_no_op_when_active_pane_is_already_permit(
        self, monkeypatch
    ):
        """If the active pane is itself in PERMIT, do not steal
        focus to another permit pane (would jitter focus needlessly
        on multi-permit windows)."""
        ps = make_ps_lines(
            (100, 1, 100, "zsh"), (200, 100, 100, "claude"),  # active: claude
            (101, 1, 101, "zsh"), (201, 101, 101, "claude"),  # inactive: claude
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot",
                            lambda: "\n".join(ps))
        panes_raw = (
            "100\t%0\tclaude\t1\t48\n"
            "101\t%1\tclaude\t0\t48"
        )
        select_calls = []

        def fake(*args):
            if args[:2] == ("show-option", "-wqv") and args[-1] == "@ccm_dir":
                return "/tmp/proj"
            if args[0] == "list-panes":
                return panes_raw
            if args[0] == "select-pane":
                select_calls.append(args)
                return ""
            if args[0] == "capture-pane":
                return "Esc to cancel · Tab to amend"  # both panes PERMIT
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        ccm_window.auto_focus_attention_pane("0:5")
        assert select_calls == [], (
            f"Should not steal focus when active is already PERMIT, "
            f"but got: {select_calls}"
        )

    def test_sliver_permit_pane_not_focused(self, monkeypatch):
        """A sliver pane below the height threshold cannot reliably
        report PERMIT (capture-pane is empty), so even if its process
        tree looks like a claude waiting, auto-focus must not switch
        to it. Mirrors the sliver exclusion in detect_window_raw."""
        ps = make_ps_lines(
            (100, 1, 100, "zsh"),  # active shell
            (200, 1, 200, "zsh"), (300, 200, 300, "claude"),  # sliver claude
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot",
                            lambda: "\n".join(ps))
        panes_raw = (
            "100\t%0\tzsh\t1\t47\n"
            "200\t%1\tclaude\t0\t1"  # 1-row sliver
        )
        select_calls = []

        def fake(*args):
            if args[:2] == ("show-option", "-wqv") and args[-1] == "@ccm_dir":
                return "/tmp/proj"
            if args[0] == "list-panes":
                return panes_raw
            if args[0] == "select-pane":
                select_calls.append(args)
                return ""
            if args[0] == "capture-pane":
                return "Esc to cancel · Tab to amend"
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        ccm_window.auto_focus_attention_pane("0:5")
        assert select_calls == [], (
            f"Sliver pane should be excluded from auto-focus: {select_calls}"
        )
