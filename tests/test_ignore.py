"""Tests for the CCM_IGNORE feature: hiding a pane/session from ccm.

Covers the predicate (`_pane_is_ignored`), the count helper, the raw
pane-aggregation skip, and the `ccm ignore` / `ccm unignore` command
markers. The bash side (hook early-exit + pane stamp) is covered in
tests/test_event_log.bats.
"""

import os
from unittest.mock import patch

import pytest

import ccm_core
import ccm_commands
import ccm_pane_state


class TestPaneIsIgnored:
    """`_pane_is_ignored` is the single source of truth for the ignore
    test — every detection consumer reads pane field index 6 through
    it. Backward-compatible with pre-feature 6-tuples (no field 6)."""

    def test_six_tuple_is_not_ignored(self):
        pc = ("0:1", "100", "%0", "claude", "1", "40")  # legacy shape
        assert ccm_core._pane_is_ignored(pc) is False

    def test_marker_one_is_ignored(self):
        pc = ("0:1", "100", "%0", "claude", "1", "40", "1")
        assert ccm_core._pane_is_ignored(pc) is True

    def test_empty_marker_not_ignored(self):
        pc = ("0:1", "100", "%0", "claude", "1", "40", "")
        assert ccm_core._pane_is_ignored(pc) is False

    def test_zero_marker_not_ignored(self):
        # tmux can echo "0" for an option explicitly set to 0.
        pc = ("0:1", "100", "%0", "claude", "1", "40", "0")
        assert ccm_core._pane_is_ignored(pc) is False


class TestCountIgnoredPanes:
    def test_counts_only_matching_window_ignored(self):
        cache = [
            ("0:1", "100", "%0", "claude", "1", "40", "1"),   # ignored, win 1
            ("0:1", "200", "%1", "zsh", "0", "40", ""),       # not, win 1
            ("0:2", "300", "%2", "claude", "1", "40", "1"),   # ignored, win 2
        ]
        assert ccm_core._count_ignored_panes(cache, "0:1") == 1
        assert ccm_core._count_ignored_panes(cache, "0:2") == 1
        assert ccm_core._count_ignored_panes(cache, "0:3") == 0

    def test_total_pane_count_includes_ignored(self):
        # `[N]` is physical truth — an ignored sidekick is still a pane.
        cache = [
            ("0:1", "100", "%0", "claude", "1", "40", ""),
            ("0:1", "200", "%1", "claude", "0", "40", "1"),
        ]
        assert ccm_core._count_panes(cache, "0:1") == 2
        assert ccm_core._count_ignored_panes(cache, "0:1") == 1


class TestRawAggregationSkip:
    """detect_window_raw must drop ignored panes so a hidden sidekick's
    state never contributes to the window."""

    def _run(self, cache, pane_states):
        """Patch detect_pane_state to return a fixed state per pid so
        aggregation is deterministic without capture-pane."""
        def _fake(pid, pane_id, ps_lines, own_pgid, current_command=None):
            return pane_states[pid]
        with patch.object(ccm_pane_state, "detect_pane_state", _fake):
            return ccm_pane_state.detect_window_raw(
                "0:1", cache, [], "999")

    def test_ignored_busy_pane_excluded(self):
        # Main claude IDLE + ignored sidekick BUSY → window must be
        # IDLE, not BUSY (the sidekick is invisible).
        cache = [
            ("0:1", "100", "%0", "claude", "1", "40", ""),   # main, IDLE
            ("0:1", "200", "%1", "claude", "0", "40", "1"),  # ignored, BUSY
        ]
        assert self._run(cache, {"100": "IDLE", "200": "BUSY"}) == "IDLE"

    def test_ignored_permit_pane_excluded(self):
        # Even PERMIT (highest priority) from an ignored pane is hidden.
        cache = [
            ("0:1", "100", "%0", "claude", "1", "40", ""),   # main, IDLE
            ("0:1", "200", "%1", "claude", "0", "40", "1"),  # ignored, PERMIT
        ]
        assert self._run(cache, {"100": "IDLE", "200": "PERMIT"}) == "IDLE"

    def test_all_panes_ignored_yields_down(self):
        cache = [
            ("0:1", "100", "%0", "claude", "1", "40", "1"),
        ]
        assert self._run(cache, {"100": "BUSY"}) == "DOWN"


class TestPrimaryPidSkip:
    """The window's tracked session must never resolve to an ignored
    pane's claude."""

    def test_ignored_first_pane_not_picked_as_primary(self, monkeypatch):
        import ccm_detection
        cache = [
            ("0:1", "200", "%0", "claude", "1", "40", "1"),  # ignored, first
            ("0:1", "300", "%1", "claude", "0", "40", ""),   # tracked
        ]
        seen = []

        def _fake_find(pane_pid, ps_lines):
            seen.append(pane_pid)
            return {"200": 201, "300": 301}.get(pane_pid)

        monkeypatch.setattr(ccm_detection, "find_claude_pid", _fake_find)
        monkeypatch.setattr(ccm_detection, "detect_window_raw",
                            lambda *a, **k: "IDLE")
        monkeypatch.setattr(ccm_core, "find_process_age", lambda *a: 100)
        try:
            ccm_detection.build_detection_context(
                "0:1", "/tmp/x", "IDLE", cache, [], "999")
        except Exception:
            pass  # downstream resolution may bail; we only check `seen`
        # Non-vacuous: the loop DID run (probed the tracked pane) but
        # skipped the ignored one before probing it.
        assert "300" in seen, "the primary-pid loop must have run"
        assert "200" not in seen, (
            "ignored pane's pid must not be probed for the primary claude")


class TestIgnoreCommands:
    """`ccm ignore` / `ccm unignore` set/clear both markers: the tmux
    pane option (detection skip) and the session marker file (hook
    suppression)."""

    def _setup(self, tmp_path, monkeypatch):
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *a, **k: calls.append(a) or "")
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr(ccm_core, "ccm_info", lambda *a, **k: None)
        # Resolve a session id for the pane so the marker path is used.
        monkeypatch.setattr(ccm_commands, "_pane_session_id",
                            lambda pane, ps: "sid-xyz")
        monkeypatch.setenv("TMUX_PANE", "%7")
        return hook_dir, calls

    def test_ignore_sets_pane_option_and_marker(self, tmp_path, monkeypatch):
        hook_dir, calls = self._setup(tmp_path, monkeypatch)
        ccm_commands.cmd_ignore("")  # current pane
        # pane option set
        assert any(c[:3] == ("set-option", "-p", "-t") and "@ccm_ignore" in c
                   for c in calls), calls
        # pane title set
        assert any(c[0] == "select-pane" and "-T" in c for c in calls)
        # session marker written
        assert (hook_dir / "sid-xyz.ignore").exists()

    def test_unignore_clears_pane_option_and_marker(self, tmp_path, monkeypatch):
        hook_dir, calls = self._setup(tmp_path, monkeypatch)
        (hook_dir / "sid-xyz.ignore").write_text("")
        ccm_commands.cmd_unignore("")
        assert any(c[:3] == ("set-option", "-p", "-t") and "-u" in c
                   and "@ccm_ignore" in c for c in calls), calls
        assert not (hook_dir / "sid-xyz.ignore").exists()

    def test_ignore_no_pane_context_dies(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.delenv("TMUX_PANE", raising=False)

        def _die(msg):
            raise SystemExit(msg)
        monkeypatch.setattr(ccm_core, "ccm_die", _die)
        with pytest.raises(SystemExit):
            ccm_commands.cmd_ignore("")
