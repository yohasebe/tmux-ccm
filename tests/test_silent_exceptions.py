"""Regression tests for the "silent exception class" of bug.

The autosave NameError that ran for 38 hours, the notify dedup
keying mismatch, and the multi-line ❯ false-BUSY were all silent
because their failure paths were caught by broad `except Exception:`
clauses or by `log_caught_exception`. Tests that mock the called
function pass even when the real function would NameError.

This module specifically exercises the HAPPY path of every
silent-exception site: that is, the test fails if the function
raises any NameError / AttributeError / ImportError class bug,
even though production-time the same bug would silently log.

Sites covered here:
  - `ccm_commands._autosave_trigger`
  - `ccm_snapshot.cmd_snapshot_load` post-load autosave
  - `inject_status.inject_status` main entry (smoke)
  - `dashboard.Dashboard._refresh_loop` one-iteration body (smoke)

Already covered by direct tests elsewhere (see test_runtime.py,
TestCmdStopAll in test_commands.py): `_force_autosave`,
`periodic_autosave`, `cmd_stop --all`."""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

import ccm_commands
import ccm_core
import ccm_snapshot


class TestAutosaveTrigger:
    """`_autosave_trigger` is called from cmd_add / cmd_remove /
    cmd_unregister / cmd_rename. A NameError-class bug here would
    leak `Autosave failed: ...` warnings on every lifecycle action
    while users assumed the snapshot was up to date."""

    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_calls_snapshot_save(self, mock_save):
        ccm_commands._autosave_trigger()
        mock_save.assert_called_once_with("_autosave", quiet=True)

    @patch("ccm_core.ccm_warn")
    @patch("ccm_snapshot.cmd_snapshot_save", side_effect=OSError("disk full"))
    def test_swallows_exception_and_warns(self, mock_save, mock_warn):
        # Best-effort: must not propagate, but must surface the failure.
        ccm_commands._autosave_trigger()
        mock_warn.assert_called_once()
        assert "Autosave failed" in mock_warn.call_args.args[0]


class TestSnapshotLoadAutosave:
    """`cmd_snapshot_load` writes `_autosave` after restoring all
    projects so `ccm start <name>` followed by a fresh ccm session
    has the right starting point. The post-load save is wrapped in
    a broad `except Exception` and the existing snapshot tests mock
    `_autosave_trigger` away — so a NameError in the save call
    site (or its callees) would be silent."""

    @patch("ccm_core.project_exists", return_value=False)
    @patch("ccm_core.get_session", return_value="main")
    def test_post_load_autosave_invokes_save(
        self, mock_session, mock_exists, tmp_path
    ):
        # Minimal valid snapshot.
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        snap_data = {
            "version": 1,
            "name": "test-load",
            "created": "2026-05-04T00:00:00+0000",
            "projects": [{"name": "proj", "dir": str(proj_dir),
                          "auto_start_claude": False}],
        }
        (tmp_path / "test-load.json").write_text(json.dumps(snap_data))

        # Stub cmd_add so we don't recurse into tmux. The post-load
        # autosave is what we want to observe.
        with patch("ccm_commands.cmd_add"), \
             patch.object(ccm_snapshot, "cmd_snapshot_save") as mock_save:
            old_dir = ccm_core.CCM_SNAPSHOT_DIR
            ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
            try:
                ccm_snapshot.cmd_snapshot_load("test-load")
            finally:
                ccm_core.CCM_SNAPSHOT_DIR = old_dir

        # The post-load autosave call site (line 166 of ccm_snapshot.py)
        # must invoke cmd_snapshot_save("_autosave", quiet=True). A
        # NameError-class bug there would silently warn ("Failed to
        # save autosave snapshot after load") with the broad except.
        autosave_calls = [
            c for c in mock_save.call_args_list
            if c.args[:1] == ("_autosave",)
        ]
        assert len(autosave_calls) == 1
        assert autosave_calls[0].kwargs.get("quiet") is True


class TestInjectStatusSmoke:
    """`inject_status()` runs every status-interval (1-15s by tmux
    convention). It has no direct test coverage and a top-level
    `except Exception: log_caught_exception` swallows any
    NameError-class bug. This smoke test invokes the function with
    every external stubbed and asserts no exception escapes."""

    def test_inject_status_runs_without_nameerror(self, tmp_path, monkeypatch):
        import inject_status

        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        # acquire_lockfile expects the tmp dir to exist.
        os.makedirs(str(tmp_path), exist_ok=True)

        # Stub every external call sites reaches.
        with patch("inject_status.tmux_cmd", return_value=""), \
             patch("inject_status.build_project_list", return_value=[]), \
             patch("inject_status.update_window_names"), \
             patch("inject_status.periodic_autosave"), \
             patch("inject_status.auto_exit_idle"), \
             patch("inject_status.detect_external_status_change"), \
             patch("inject_status.sanitize_orig_status"):
            # Should not raise. Lockfile may be acquired or not depending
            # on prior test state — both branches must complete cleanly.
            inject_status.inject_status()

    def test_force_fast_runs_even_when_lock_is_held(self, monkeypatch):
        """The focus-refresh path (`force_fast=True`, fired on a window
        switch) must NOT skip when another inject-status holds the
        lock. It is read-only on `@ccm_prev_state` (cached render), so
        it cannot cause the state flicker the lock guards against — and
        skipping it was the intermittent "status bar didn't switch on
        dashboard select" bug (2026-06-30): a switch landing during a
        periodic full-detection tick dropped the focus refresh, leaving
        the highlight stalled until the next tick. Pin that it runs the
        impl AND issues the redraw even with the lock held."""
        import inject_status

        impl = patch("inject_status._inject_status_impl").start()
        # Lock held → acquire_lockfile returns None.
        patch("inject_status.acquire_lockfile", return_value=None).start()
        refreshed = []
        patch("inject_status.tmux_cmd",
              side_effect=lambda *a: refreshed.append(a) or "").start()
        try:
            inject_status.inject_status(force_fast=True)
        finally:
            patch.stopall()

        impl.assert_called_once()
        assert impl.call_args.kwargs.get("force_fast") is True
        assert ("refresh-client", "-S") in refreshed, (
            "force_fast must force a redraw even when the lock is held"
        )

    def test_periodic_path_still_skips_when_lock_is_held(self, monkeypatch):
        """The full-detection (periodic) path must still skip when the
        lock is held — two detections racing both write
        @ccm_prev_state and flicker the state. Only force_fast is
        exempt from the skip."""
        import inject_status

        impl = patch("inject_status._inject_status_impl").start()
        patch("inject_status.acquire_lockfile", return_value=None).start()
        try:
            inject_status.inject_status(force_fast=False)
        finally:
            patch.stopall()

        impl.assert_not_called()


class TestDashboardRefreshLoopSmoke:
    """`Dashboard._refresh_loop` runs in a thread on every dashboard
    open. It has its own `except Exception: log_caught_exception`
    around `build_project_list`, so any NameError there would silent-
    fail. This test exercises one loop iteration with stubs."""

    def test_refresh_loop_one_iteration(self, monkeypatch):
        import dashboard
        # Speed up the loop's sleep waits.
        monkeypatch.setattr("dashboard.REFRESH_INTERVAL", 0.0)
        monkeypatch.setattr("dashboard.time.sleep", lambda *_: None)

        # The loop's own `except Exception: log_caught_exception(...)`
        # would swallow exactly the NameError/AttributeError class of
        # bug this smoke test exists to catch — making the test pass
        # while its mission fails. Record every silent catch and
        # assert none happened. (This is not hypothetical: this test's
        # own scaffold once lacked `bg_visible` after the bg-section
        # feature added that attribute, and the AttributeError was
        # silently logged — to the user's REAL errors.log — on every
        # pytest run while the test kept passing. 2026-06-07.)
        caught = []
        monkeypatch.setattr(
            "dashboard.log_caught_exception",
            lambda scope: caught.append(scope),
        )

        # Construct without curses (avoid full render path). Every
        # attribute `_refresh_loop` touches must be initialised here;
        # when adding an attribute to Dashboard.__init__ that the
        # refresh loop reads, add it here too or this test will flag
        # the omission via the `caught` assertion below.
        d = dashboard.Dashboard.__new__(dashboard.Dashboard)
        import threading
        d.lock = threading.Lock()
        d.projects = []
        d.initial_load = True
        d.data_dirty = False
        d.running = True
        d.preview_enabled = False
        d.mode = "dashboard"
        d._last_preview_target = ""
        d.bg_visible = False
        d.bg_sessions = []

        # Exit after one body cycle: the data-source stub flips
        # `running` off so the next outer-loop check terminates.
        def stop_loop(*_a, **_kw):
            d.running = False
            return []

        monkeypatch.setattr("dashboard.build_project_list", stop_loop)

        # The loop must complete without swallowing any exception.
        d._refresh_loop()
        assert caught == [], (
            f"_refresh_loop silently caught exception(s) in scope(s) "
            f"{caught} — the smoke test scaffold is missing an "
            f"attribute the loop now reads, or the loop body has a "
            f"real NameError-class bug."
        )
