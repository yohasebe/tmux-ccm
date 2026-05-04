"""Tests for ccm_commands.

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
import ccm_send
import ccm_signals
import ccm_snapshot

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

class TestCmdErrors:
    """`ccm errors [--clear]` reads the silent-exception log."""

    def test_no_log_prints_friendly_message(self, tmp_path, capsys):
        log_path = tmp_path / "errors.log"
        prev_path = tmp_path / "errors.log.1"
        with patch.object(ccm_core, "CCM_ERRORS_LOG", str(log_path)), \
                patch.object(ccm_core, "CCM_ERRORS_LOG_PREV", str(prev_path)):
            ccm_commands.cmd_errors([])
        out = capsys.readouterr().out
        assert "No silent-caught errors logged." in out

    def test_prints_records_in_chronological_order(self, tmp_path, capsys):
        log_path = tmp_path / "errors.log"
        prev_path = tmp_path / "errors.log.1"
        prev_path.write_text(json.dumps({
            "ts": 1000, "scope": "older_scope",
            "type": "ValueError", "msg": "older",
            "traceback": "  File 'x', line 1\n    foo\n",
        }) + "\n")
        log_path.write_text(json.dumps({
            "ts": 2000, "scope": "newer_scope",
            "type": "RuntimeError", "msg": "newer",
            "traceback": "",
        }) + "\n")
        with patch.object(ccm_core, "CCM_ERRORS_LOG", str(log_path)), \
                patch.object(ccm_core, "CCM_ERRORS_LOG_PREV", str(prev_path)):
            ccm_commands.cmd_errors([])
        out = capsys.readouterr().out
        # Older first
        assert out.find("older_scope") < out.find("newer_scope")
        assert "ValueError: older" in out
        assert "RuntimeError: newer" in out
        # Indented traceback line
        assert "    File 'x', line 1" in out

    def test_clear_removes_both_files(self, tmp_path, capsys):
        log_path = tmp_path / "errors.log"
        prev_path = tmp_path / "errors.log.1"
        log_path.write_text("garbage")
        prev_path.write_text("garbage")
        with patch.object(ccm_core, "CCM_ERRORS_LOG", str(log_path)), \
                patch.object(ccm_core, "CCM_ERRORS_LOG_PREV", str(prev_path)):
            ccm_commands.cmd_errors(["--clear"])
        assert not log_path.exists()
        assert not prev_path.exists()
        assert "cleared" in capsys.readouterr().out.lower()

    def test_malformed_lines_skipped(self, tmp_path, capsys):
        log_path = tmp_path / "errors.log"
        prev_path = tmp_path / "errors.log.1"
        log_path.write_text(
            "not-json\n"
            + json.dumps({"ts": 1, "scope": "ok", "type": "X", "msg": "y"}) + "\n"
        )
        with patch.object(ccm_core, "CCM_ERRORS_LOG", str(log_path)), \
                patch.object(ccm_core, "CCM_ERRORS_LOG_PREV", str(prev_path)):
            ccm_commands.cmd_errors([])
        out = capsys.readouterr().out
        assert "X: y" in out
        # No traceback raised, no failure to print the well-formed record.


class TestCmdDoctor:
    """`ccm doctor` aggregates dependency / setup / canary / project
    diagnostics into a single output. Each section is independent;
    these tests verify the output structure and that each canary
    branch (warn vs ok) renders correctly. The full-system
    integration is exercised by manual runs — these tests focus on
    not crashing and on rendering each section's text."""

    def _stub_world(self, monkeypatch, tmp_path,
                    hooks=True, hooks_log_warning="",
                    disable_warning="", managed_warning="",
                    cluster_warnings=(), projects=(),
                    errors_log_lines=0):
        """Stub every external dependency cmd_doctor reads, returning
        controlled values so each test exercises a specific branch
        without standing up real tmux / claude state."""
        monkeypatch.setattr(ccm_core, "hooks_configured",
                            lambda: hooks)
        monkeypatch.setattr(ccm_canaries, "hooks_log_warning",
                            lambda: hooks_log_warning)
        monkeypatch.setattr(ccm_canaries, "hooks_log_size",
                            lambda: 1024 * 1024 if not hooks_log_warning else -1)
        monkeypatch.setattr(ccm_canaries, "disable_all_hooks_warning",
                            lambda: disable_warning)
        monkeypatch.setattr(ccm_canaries, "managed_hooks_only_warning",
                            lambda: managed_warning)
        monkeypatch.setattr(ccm_canaries, "shell_cluster_warnings",
                            lambda p: list(cluster_warnings))
        monkeypatch.setattr(ccm_core, "build_project_list",
                            lambda fast=False: list(projects))
        # tmux_cmd is called for show-option @ccm_session_id per
        # project; return empty for all of them.
        monkeypatch.setattr(ccm_core, "tmux_cmd", lambda *a, **kw: "")
        # Errors log
        log_path = tmp_path / "errors.log"
        if errors_log_lines:
            log_path.write_text("\n".join(
                json.dumps({"ts": i, "scope": "x", "type": "E", "msg": "e"})
                for i in range(errors_log_lines)
            ) + "\n")
        monkeypatch.setattr(ccm_core, "CCM_ERRORS_LOG", str(log_path))
        # subprocess.run for `which`/`--version` calls — return
        # something plausible for each.
        def fake_run(args, **kw):
            cmd = args[0] if args else ""
            if cmd == "which":
                tool = args[1]
                stdout = f"/usr/bin/{tool}" if tool != "missing" else ""
                return MagicMock(returncode=0 if stdout else 1, stdout=stdout)
            if cmd.endswith("claude"):
                return MagicMock(returncode=0, stdout="2.1.126 (Claude Code)\n")
            if cmd == "tmux":
                return MagicMock(returncode=0, stdout="tmux 3.5\n")
            return MagicMock(returncode=0, stdout="")
        monkeypatch.setattr("subprocess.run", fake_run)

    def test_clean_environment_renders_all_sections(self, tmp_path,
                                                    monkeypatch, capsys):
        self._stub_world(monkeypatch, tmp_path)
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        for section in ("Environment", "Setup", "Runtime canaries",
                        "Active projects", "Silent-exception log",
                        "Configuration"):
            assert section in out, f"missing section: {section}"

    def test_warns_when_hooks_not_installed(self, tmp_path,
                                            monkeypatch, capsys):
        self._stub_world(monkeypatch, tmp_path, hooks=False)
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "Hooks not installed" in out
        assert "ccm setup-hooks" in out  # actionable guidance

    def test_warns_on_hooks_log_bloat(self, tmp_path, monkeypatch, capsys):
        self._stub_world(
            monkeypatch, tmp_path,
            hooks_log_warning="hooks.log is 250 MB — see anthropics/...",
        )
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "250 MB" in out

    def test_warns_on_disable_all_hooks(self, tmp_path, monkeypatch, capsys):
        self._stub_world(
            monkeypatch, tmp_path,
            disable_warning="disableAllHooks=true; ccm hooks won't fire",
        )
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "disableAllHooks=true" in out

    def test_lists_projects_with_states(self, tmp_path, monkeypatch, capsys):
        proj_a = ccm_core.Project(
            win_target="0:1", win_idx="1", name="alpha",
            directory="/p/a", state="BUSY",
        )
        proj_b = ccm_core.Project(
            win_target="0:2", win_idx="2", name="beta",
            directory="/p/b", state="IDLE",
        )
        self._stub_world(monkeypatch, tmp_path,
                         projects=(proj_a, proj_b))
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "alpha" in out and "BUSY" in out
        assert "beta" in out and "IDLE" in out
        assert "(2)" in out  # active projects (2)

    def test_reports_empty_errors_log(self, tmp_path, monkeypatch, capsys):
        self._stub_world(monkeypatch, tmp_path)
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "errors.log" in out
        assert "empty" in out

    def test_reports_errors_log_record_count(self, tmp_path,
                                             monkeypatch, capsys):
        self._stub_world(monkeypatch, tmp_path, errors_log_lines=7)
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "7 record(s)" in out
        assert "ccm errors" in out  # actionable guidance

    def test_lists_cluster_shell_warnings_when_present(
            self, tmp_path, monkeypatch, capsys):
        self._stub_world(
            monkeypatch, tmp_path,
            cluster_warnings=("alpha cluster: 4 SHELL transitions in 8 min",),
        )
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "4 SHELL transitions" in out


# ─── cmd_add ───

class TestCmdAdd:
    @patch("ccm_commands._autosave_trigger")
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

        ccm_commands.cmd_add(str(proj_dir), "my-project")

        # Verify tmux_batch was called to set metadata
        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        tag_names = [a[3] for a in batch_args if len(a) > 3]
        assert "@ccm_project" in tag_names
        assert "@ccm_dir" in tag_names

    def test_add_missing_dir_exits(self):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_add("/nonexistent/directory/xyz")

    def test_add_empty_dir_exits(self):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_add("")

    @patch("ccm_core.find_window", return_value="1")
    @patch("ccm_core.get_session", return_value="main")
    def test_add_duplicate_name_exits(self, mock_session, mock_find, tmp_path):
        proj_dir = tmp_path / "dup"
        proj_dir.mkdir()
        with pytest.raises(SystemExit):
            ccm_commands.cmd_add(str(proj_dir), "existing")

    @patch("ccm_commands._autosave_trigger")
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

        ccm_commands.cmd_add(str(proj_dir))

        # Check @ccm_project was set to basename
        batch_args = mock_batch.call_args[0]
        project_tag = [a for a in batch_args if len(a) > 3 and a[3] == "@ccm_project"]
        assert project_tag[0][4] == "cool-project"

    @patch("ccm_commands._autosave_trigger")
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

        ccm_commands.cmd_add(str(proj_dir), "loading-test", start_claude=False, _loading=True)

        mock_auto.assert_not_called()


# ─── cmd_unregister ───

class TestCmdUnregister:
    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_removes_tags(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_commands.cmd_unregister("my-proj")

        # Should call tmux_batch to remove all tags
        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        # Every command should be a set-option -u (unset)
        for cmd in batch_args:
            assert "-u" in cmd

    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_restores_original_name(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_commands.cmd_unregister("my-proj")

        # Should call rename-window with original name
        rename_calls = [c for c in mock_tmux.call_args_list
                        if c[0][0] == "rename-window"]
        assert len(rename_calls) == 1
        assert rename_calls[0][0][-1] == "orig-name"

    def test_unregister_empty_name_exits(self):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_unregister("")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_unregister("nonexistent")

    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_triggers_autosave(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_commands.cmd_unregister("proj")
        mock_auto.assert_called_once()

    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_signals.cleanup_project_runtime_files")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="/x/proj-dir")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_cleans_runtime_files(
        self, mock_session, mock_find, mock_tmux, mock_batch,
        mock_cleanup, mock_auto,
    ):
        """Unregister must sweep the project's hook signal / notify
        marker / caches — otherwise re-registering the same dir later
        inherits stale state."""
        ccm_commands.cmd_unregister("proj")
        mock_cleanup.assert_called_once_with("/x/proj-dir")


# ─── cmd_remove ───

class TestCmdRemove:
    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_signals.cleanup_project_runtime_files")
    @patch("ccm_core.tmux_cmd", return_value="/x/proj-dir")
    @patch("ccm_core.find_window", return_value="3")
    @patch("ccm_core.get_session", return_value="main")
    def test_remove_kills_window_and_cleans(
        self, mock_session, mock_find, mock_tmux, mock_cleanup, mock_auto,
    ):
        ccm_commands.cmd_remove("proj")
        # kill-window called on the resolved win_target
        kill_calls = [c for c in mock_tmux.call_args_list
                      if c[0][0] == "kill-window"]
        assert len(kill_calls) == 1
        assert kill_calls[0][0][-1] == "main:3"
        # cleanup runs AFTER resolving @ccm_dir but before returning
        mock_cleanup.assert_called_once_with("/x/proj-dir")
        mock_auto.assert_called_once()

    def test_remove_empty_name_exits(self):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_remove("")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_remove_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_remove("nonexistent")


# ─── cmd_rename ───

class TestCmdRename:
    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_updates_tag_and_window(self, mock_session, mock_find, mock_batch, mock_auto):
        # find_window returns index for old name, None for new name (not duplicate)
        mock_find.side_effect = lambda sess, name: "1" if name == "old" else None

        ccm_commands.cmd_rename("old", "new")

        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        # Should set @ccm_project to "new" and rename window
        set_cmd = [a for a in batch_args if "set-option" in a[0] and "@ccm_project" in a]
        assert set_cmd[0][-1] == "new"
        rename_cmd = [a for a in batch_args if "rename-window" in a[0]]
        assert rename_cmd[0][-1] == "new"

    def test_rename_empty_old_exits(self):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_rename("", "new")

    def test_rename_empty_new_exits(self):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_rename("old", "")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_rename("nonexistent", "new")

    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_duplicate_exits(self, mock_session, mock_find):
        # Both old and new names exist
        mock_find.return_value = "1"
        with pytest.raises(SystemExit):
            ccm_commands.cmd_rename("old", "taken")

    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_triggers_autosave(self, mock_session, mock_find, mock_batch, mock_auto):
        mock_find.side_effect = lambda sess, name: "1" if name == "old" else None
        ccm_commands.cmd_rename("old", "new")
        mock_auto.assert_called_once()


class TestCmdStopAll:
    """`ccm stop --all` must persist `_autosave` before killing
    windows. The snapshot save is the only on-shutdown guarantee
    that a `ccm start _autosave` later restores the workspace."""

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_snapshot.cmd_snapshot_save")
    @patch("ccm_core.list_windows_raw", return_value=[])
    @patch("ccm_core.get_session", return_value="main")
    def test_no_windows_skips_save(self, mock_sess, mock_list, mock_save, mock_tmux):
        ccm_commands.cmd_stop("--all")
        mock_save.assert_not_called()

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.init_dirs")
    @patch("ccm_snapshot.cmd_snapshot_save")
    @patch(
        "ccm_core.list_windows_raw",
        return_value=[("1", "session-id", "proj1", "/dir1")],
    )
    @patch("ccm_core.get_session", return_value="main")
    def test_with_windows_saves_then_kills(
        self, mock_sess, mock_list, mock_save, mock_init, mock_tmux
    ):
        ccm_commands.cmd_stop("--all")
        mock_save.assert_called_once_with("_autosave", quiet=True)
        # Kill-window invoked for the project window.
        kill_calls = [c for c in mock_tmux.call_args_list if c.args[:1] == ("kill-window",)]
        assert len(kill_calls) == 1


class TestDispatcherPassthrough:
    """`send` / `capture` / `errors` accept flags intermixed with
    positionals. The dispatcher must pass raw argv through to the
    handler instead of letting argparse intercept the flags."""

    @patch("ccm_send.cmd_send")
    def test_send_with_file_flag_passes_through(self, mock_send):
        ccm_core.dispatch(["send", "blog", "--file", "/tmp/m.txt"])
        mock_send.assert_called_once_with(["blog", "--file", "/tmp/m.txt"])

    @patch("ccm_send.cmd_send")
    def test_send_with_yes_flag_passes_through(self, mock_send):
        ccm_core.dispatch(["send", "blog", "msg", "-y"])
        mock_send.assert_called_once_with(["blog", "msg", "-y"])

    @patch("ccm_send.cmd_send")
    def test_send_with_force_flag_passes_through(self, mock_send):
        ccm_core.dispatch(["send", "blog", "--force", "queued msg"])
        mock_send.assert_called_once_with(["blog", "--force", "queued msg"])

    @patch("ccm_send.cmd_send")
    def test_send_preserves_double_dash_separator(self, mock_send):
        # `--` lets users send messages that start with `-`
        ccm_core.dispatch(["send", "blog", "--", "--literal-flag-as-message"])
        mock_send.assert_called_once_with(
            ["blog", "--", "--literal-flag-as-message"]
        )

    @patch("ccm_commands.cmd_capture")
    def test_capture_with_leading_copy_flag(self, mock_capture):
        # `ccm capture --copy blog` — flag BEFORE positional. The
        # original bug: argparse rejected `--copy` because it was
        # not a defined subparser flag.
        ccm_core.dispatch(["capture", "--copy", "blog"])
        mock_capture.assert_called_once_with(["--copy", "blog"])

    @patch("ccm_commands.cmd_capture")
    def test_capture_with_trailing_copy_flag(self, mock_capture):
        ccm_core.dispatch(["capture", "blog", "--copy"])
        mock_capture.assert_called_once_with(["blog", "--copy"])

    @patch("ccm_commands.cmd_errors")
    def test_errors_with_clear_flag(self, mock_errors):
        ccm_core.dispatch(["errors", "--clear"])
        mock_errors.assert_called_once_with(["--clear"])

    @patch("ccm_commands.cmd_errors")
    def test_errors_with_no_args(self, mock_errors):
        ccm_core.dispatch(["errors"])
        mock_errors.assert_called_once_with([])

    def test_unknown_command_still_rejected(self):
        # Strict argparse validation preserved for non-passthrough
        # commands (catches typos like `ccm sttatus`).
        with pytest.raises(SystemExit):
            ccm_core.dispatch(["nonexistent-cmd"])

    @patch("ccm_commands.cmd_attach")
    def test_strict_command_with_unknown_flag_still_rejected(self, mock_attach):
        # `attach` is not a passthrough — argparse should still
        # reject unknown flags, not silently pass them through.
        with pytest.raises(SystemExit):
            ccm_core.dispatch(["attach", "blog", "--bogus-flag"])
        mock_attach.assert_not_called()

    def test_send_help_prints_usage(self, capsys):
        # Passthrough handler must intercept -h/--help itself since
        # the dispatcher bypasses argparse.
        ccm_core.dispatch(["send", "--help"])
        out = capsys.readouterr().out
        assert "Usage: ccm send" in out

    def test_capture_help_prints_usage(self, capsys):
        ccm_core.dispatch(["capture", "-h"])
        out = capsys.readouterr().out
        assert "Usage: ccm capture" in out

    def test_errors_help_prints_usage(self, capsys):
        ccm_core.dispatch(["errors", "--help"])
        out = capsys.readouterr().out
        assert "Usage: ccm errors" in out

    def test_passthrough_set_derived_from_marker(self):
        # The set must be derived from `_passthrough_argparse_config`
        # identity, not a hand-maintained list. Adding a new
        # passthrough subcommand should require no separate edit.
        names = {n for n, c, _ in ccm_core._SUBCOMMANDS
                 if c is ccm_core._passthrough_argparse_config}
        assert names == set(ccm_core._PASSTHROUGH_COMMANDS)
        assert {"send", "capture", "errors"} <= names


