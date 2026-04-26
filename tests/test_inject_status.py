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


class TestStaleSignalSuffixInStatusBar:
    """Mode 1 / 2 status bar should append `(Nm)` when a BUSY or
    PERMIT signal is past JSONL_HOOK_GAP_TOLERANCE — same affordance
    the dashboard and `ccm status` already provide. Surfacing the
    age in the always-visible status bar lets the user catch
    phantom-subagent / silent-permission stuck states without
    opening the popup.
    """

    def test_mode1_stale_busy_appends_suffix(self, monkeypatch):
        # Hook signal 8 minutes old → "(8m)" suffix.
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_core, "read_hook_signal",
                            lambda d: (ts - 480, "BUSY", ""))
        entries = inject_status.build_detail_entries(
            [make_project("0:2", "2", "ccm-dev", "BUSY")],
            with_extras=False, current_win_target="0:2",
        )
        assert "(8m)" in entries[0]

    def test_mode2_stale_permit_appends_suffix(self, monkeypatch):
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_core, "read_hook_signal",
                            lambda d: (ts - 120, "PERMIT", ""))
        entries = inject_status.build_detail_entries(
            [make_project("0:2", "2", "ccm-dev", "PERMIT")],
            with_extras=True, current_win_target="0:2",
        )
        assert "(2m)" in entries[0]

    def test_fresh_busy_no_suffix(self, monkeypatch):
        """Below the stale threshold the suffix is suppressed —
        otherwise every active turn would clutter the status bar."""
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_core, "read_hook_signal",
                            lambda d: (ts - 5, "BUSY", ""))
        entries = inject_status.build_detail_entries(
            [make_project("0:2", "2", "ccm-dev", "BUSY")],
            with_extras=False, current_win_target="0:2",
        )
        # No "(Ns)" / "(Nm)" pattern in the entry.
        assert "(5s)" not in entries[0] and "(0m)" not in entries[0]

    def test_idle_state_never_gets_suffix(self, monkeypatch):
        """Only BUSY / PERMIT can mask a real state behind a stale
        hook. IDLE / SHELL / DOWN / CONT must never see the suffix
        even if a stale signal happens to be lying around."""
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_core, "read_hook_signal",
                            lambda d: (ts - 600, "BUSY", ""))
        for state in ("IDLE", "SHELL", "DOWN", "CONT"):
            entries = inject_status.build_detail_entries(
                [make_project("0:2", "2", "ccm-dev", state)],
                with_extras=False, current_win_target="0:2",
            )
            assert "(10m)" not in entries[0], f"unexpected suffix for {state}"


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
