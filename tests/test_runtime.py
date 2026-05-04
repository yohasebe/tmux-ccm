"""Tests for ccm_runtime — autosave + idle auto-exit + window-name
update helpers. The silent-NameError class of bug (caught by
`log_caught_exception` in production) makes these paths easy to
break without anyone noticing, so the regression coverage here
focuses on `actually called the underlying function` rather than
just `did not raise`."""

import os
from unittest.mock import patch, MagicMock

import pytest

import ccm_runtime
import ccm_core
import ccm_snapshot


class TestForceAutosave:
    """`_force_autosave` is called from `auto_exit_idle` after
    closing an idle window, so it must persist the snapshot or the
    user loses the project on the next `ccm start _autosave`."""

    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_calls_snapshot_save(self, mock_save):
        ccm_runtime._force_autosave()
        mock_save.assert_called_once_with("_autosave", quiet=True)

    @patch("ccm_core.log_caught_exception")
    @patch("ccm_snapshot.cmd_snapshot_save", side_effect=OSError("disk full"))
    def test_swallows_exception_and_logs(self, mock_save, mock_log):
        # Best-effort path: must not propagate, but must record.
        ccm_runtime._force_autosave()
        mock_log.assert_called_once_with("_force_autosave")


class TestPeriodicAutosave:
    """`periodic_autosave` runs every poll cycle (every ~2 s via
    `inject_status`). It must (a) be cheap to call repeatedly, (b)
    actually persist when 120 s have elapsed, (c) leave a stale
    snapshot alone when no projects exist."""

    @patch("ccm_core.tmux_cmd", return_value="")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_no_projects_skips_save(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_save.assert_not_called()

    @patch("ccm_core.tmux_cmd", return_value="proj1\nproj2\n")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_with_projects_and_no_marker_saves(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        # Fresh marker file (last_save=0) → 120 s threshold passed → save.
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_save.assert_called_once_with("_autosave", quiet=True)
        marker = os.path.join(tmp_path, "autosave-time")
        assert os.path.exists(marker)

    @patch("ccm_core.tmux_cmd", return_value="proj1\n")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_recent_marker_skips_save(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        import time
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        marker = os.path.join(tmp_path, "autosave-time")
        with open(marker, "w") as f:
            f.write(str(int(time.time())))  # just-saved marker
        ccm_runtime.periodic_autosave()
        mock_save.assert_not_called()

    @patch("ccm_core.log_caught_exception")
    @patch("ccm_core.tmux_cmd", return_value="proj1\n")
    @patch("ccm_snapshot.cmd_snapshot_save", side_effect=OSError("disk full"))
    def test_save_failure_logs_silently(
        self, mock_save, mock_tmux, mock_log, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_log.assert_called_once_with("periodic_autosave")
