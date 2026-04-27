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
        hook. IDLE / SHELL / DOWN must never see the suffix even if
        a stale signal happens to be lying around."""
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_core, "read_hook_signal",
                            lambda d: (ts - 600, "BUSY", ""))
        for state in ("IDLE", "SHELL", "DOWN"):
            entries = inject_status.build_detail_entries(
                [make_project("0:2", "2", "ccm-dev", state)],
                with_extras=False, current_win_target="0:2",
            )
            assert "(10m)" not in entries[0], f"unexpected suffix for {state}"


class TestBgActiveSuffixInStatusBar:
    """The `(bg)` affordance for IDLE projects with leftover
    background activity (state=IDLE committed but raw=BUSY because
    of grandchild processes). Indicates "user has the ball but
    something claude spawned is still running"."""

    def _make(self, state, bg):
        p = make_project("0:2", "2", "ccm-dev", state)
        p.bg_active = bg
        return p

    def test_idle_with_bg_shows_bg_suffix(self):
        entries = inject_status.build_detail_entries(
            [self._make("IDLE", True)],
            with_extras=False, current_win_target="0:2",
        )
        assert "(bg)" in entries[0]

    def test_idle_without_bg_no_suffix(self):
        entries = inject_status.build_detail_entries(
            [self._make("IDLE", False)],
            with_extras=False, current_win_target="0:2",
        )
        assert "(bg)" not in entries[0]

    def test_mode2_idle_with_bg_shows_bg_suffix(self):
        entries = inject_status.build_detail_entries(
            [self._make("IDLE", True)],
            with_extras=True, current_win_target="0:2",
        )
        assert "(bg)" in entries[0]


class TestPaneCountSuffixInStatusBar:
    """The `[N]` marker for windows with more than one pane.
    Surfaces the multi-pane case (Agent Teams, casual splits,
    leftover orphan panes) so the user can spot windows where the
    aggregated state may belong to a non-active pane. Brackets
    are rendered dim, the digit cyan, so the eye lands on the
    count.
    """

    def _make(self, pane_count, state="IDLE"):
        p = make_project("0:2", "2", "ccm-dev", state)
        p.pane_count = pane_count
        return p

    def test_single_pane_no_marker(self):
        entries = inject_status.build_detail_entries(
            [self._make(1)],
            with_extras=False, current_win_target="0:2",
        )
        # No bracketed digit between name and the icon-colon.
        assert "[1]" not in entries[0]
        # And (sanity) no leftover legacy marker.
        assert "⊞" not in entries[0]

    def test_two_panes_show_marker(self):
        entries = inject_status.build_detail_entries(
            [self._make(2)],
            with_extras=False, current_win_target="0:2",
        )
        # The digit appears between dim brackets.
        assert "[" in entries[0] and "]" in entries[0]
        assert ">2<" in entries[0].replace("#[fg=cyan]", ">").replace("#[fg=#666666]", "<")

    def test_three_panes_show_marker(self):
        entries = inject_status.build_detail_entries(
            [self._make(3)],
            with_extras=False, current_win_target="0:2",
        )
        assert "3" in entries[0]
        # The digit was emitted with a cyan colour code so the
        # user's eye is drawn to the count.
        assert "#[fg=cyan]3" in entries[0]

    def test_mode2_two_panes_show_marker(self):
        entries = inject_status.build_detail_entries(
            [self._make(2)],
            with_extras=True, current_win_target="0:2",
        )
        assert "#[fg=cyan]2" in entries[0]

    def test_pane_count_combines_with_stale_signal(self, monkeypatch):
        """A stuck-PERMIT split-pane window should render both the
        stale-age suffix and the multi-pane marker. Layout: [N]
        is BEFORE the state icon (structural marker next to the
        project name), stale-age is AFTER the icon (state-
        modifier). So in left-to-right text order: [N] first,
        then the icon, then (10m)."""
        import time
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_core, "read_hook_signal",
                            lambda d: (ts - 600, "PERMIT", ""))
        p = self._make(2, state="PERMIT")
        entries = inject_status.build_detail_entries(
            [p],
            with_extras=False, current_win_target="0:2",
        )
        # Both markers present; order: pane marker first (pre-
        # icon), stale after (post-icon).
        assert "(10m)" in entries[0]
        assert "#[fg=cyan]2" in entries[0]
        assert entries[0].index("#[fg=cyan]2") < entries[0].index("(10m)")


