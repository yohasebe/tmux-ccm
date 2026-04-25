"""Tests for inject_status.py — pure helpers that do not need a tmux server."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import inject_status
import ccm_core


def make_project(win_target, win_idx, name, state):
    return ccm_core.Project(
        win_target=win_target,
        win_idx=win_idx,
        name=name,
        directory="/tmp/" + name,
        state=state,
    )


# ─── build_detail_entries: active highlighting ───

class TestBuildDetailEntriesActive:
    """Regression: indices alone collide across sessions, so the
    active-window comparison must use the full session:index target.
    """

    def test_only_target_match_is_bold(self):
        """Two windows share index '2' but live in different sessions —
        only the one whose full target matches current_win_target should
        be highlighted as active."""
        projects = [
            make_project("0:2", "2", "ccm-dev", "SHELL"),
            make_project("1:2", "2", "whisper-stream", "BUSY"),
        ]
        entries = inject_status.build_detail_entries(
            projects, with_extras=True, current_win_target="1:2"
        )
        assert "bold" in entries[1] and "whisper-stream" in entries[1]
        # ccm-dev must NOT be bold even though its index is also "2"
        assert "bold" not in entries[0]
        assert "ccm-dev" in entries[0]

    def test_no_match_no_bold(self):
        """If the current window is not a ccm project, nothing is bold."""
        projects = [
            make_project("0:2", "2", "ccm-dev", "SHELL"),
            make_project("0:5", "5", "speechdock", "IDLE"),
        ]
        entries = inject_status.build_detail_entries(
            projects, current_win_target="0:99"
        )
        assert all("bold" not in e for e in entries)

    def test_mode1_uses_same_compare(self):
        """Mode 1 (with_extras=False) shares the bold rule."""
        projects = [
            make_project("0:2", "2", "ccm-dev", "SHELL"),
            make_project("1:2", "2", "whisper-stream", "BUSY"),
        ]
        entries = inject_status.build_detail_entries(
            projects, with_extras=False, current_win_target="0:2"
        )
        assert "bold" in entries[0] and "ccm-dev" in entries[0]
        assert "bold" not in entries[1]


# ─── priority_color / priority_icon: CONT joins the BUSY group ───

class TestPriorityCont:
    """The mode-0 status-right indicator collapses every project's
    state into a single icon + color. CONT means "Claude paused mid-
    tool, do not interrupt", which is semantically identical to BUSY
    for the purpose of "should the user know not to send right now",
    so it is grouped with BUSY in both helpers."""

    def test_color_cont_only_groups_with_busy(self):
        projects = [make_project("0:5", "5", "blog", "CONT")]
        assert inject_status.priority_color(projects) == \
            inject_status.TMUX_COLORS["BUSY"]

    def test_icon_cont_only_listed_in_busy_group(self):
        projects = [make_project("0:5", "5", "blog", "CONT")]
        # Listed under "BUSY" so the user reads "5: BUSY ◉" — the
        # collapsed display should not have to teach the new state.
        assert inject_status.priority_icon(projects) == "5: BUSY ◉"

    def test_color_permit_still_wins_over_cont(self):
        projects = [
            make_project("0:5", "5", "blog", "CONT"),
            make_project("0:6", "6", "site", "PERMIT"),
        ]
        assert inject_status.priority_color(projects) == \
            inject_status.TMUX_COLORS["PERMIT"]

    def test_icon_cont_and_busy_listed_together(self):
        projects = [
            make_project("0:5", "5", "blog", "CONT"),
            make_project("0:6", "6", "site", "BUSY"),
        ]
        # Both window indices appear in the BUSY indicator — the user
        # does not need to know one is CONT vs BUSY at this resolution.
        assert inject_status.priority_icon(projects) == "5,6: BUSY ◉"

    def test_color_falls_to_idle_when_no_busy_no_permit(self):
        projects = [
            make_project("0:5", "5", "blog", "IDLE"),
            make_project("0:6", "6", "site", "SHELL"),
        ]
        assert inject_status.priority_color(projects) == \
            inject_status.TMUX_COLORS["IDLE"]

    def test_cont_in_tmux_colors_table(self):
        """CONT must have its own color so mode 1/2's per-project
        rendering does not fall back to SHELL gray."""
        assert "CONT" in inject_status.TMUX_COLORS
