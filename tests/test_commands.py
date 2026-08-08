"""Tests for ccm_commands.

Auto-split from test_ccm_core.py. Shared fixtures + helpers
(write_jsonl, make_ps_lines, real_activity_record, system_record,
iso_ts) live in conftest.py; import them here when used.
"""

import json
import os
import re
import subprocess
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
import ccm_window

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
                    errors_log_lines=0, ps_text="", panes_cache=()):
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
        # The multi-claude row reads the bulk panes cache plus a ps
        # snapshot. Both would trip conftest's live-subprocess guard,
        # and the scan swallows that failure — so without these stubs
        # it would silently no-op in every doctor test.
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: ps_text)
        monkeypatch.setattr(ccm_core, "_build_panes_cache",
                            lambda: list(panes_cache))
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

    # ── multi-claude windows row ────────────────────────────────────
    # Two panes hosting claude in window 0:1 — shell pids 100 and 200,
    # each given a claude child by `_PS_TWO_CLAUDE` so
    # `find_claude_pid` resolves both. panes_cache entries are
    # `_build_panes_cache`'s 7-tuples: (target, pid, pane_id,
    # current_command, pane_active, pane_height, ignore).
    _PS_TWO_CLAUDE = "900 100 900 claude\n901 200 901 claude"
    _PANES_TWO_CLAUDE = (
        ("0:1", "100", "%10", "claude", "1", "40", ""),
        ("0:1", "200", "%11", "claude", "0", "40", ""),
    )

    def _one_project(self):
        return ccm_core.Project(
            win_target="0:1", win_idx="1", name="alpha",
            directory="/p/a", state="BUSY",
        )

    def test_reports_windows_hosting_two_visible_claudes(
        self, tmp_path, monkeypatch, capsys
    ):
        """The informational row: state the fact, name both readings.

        It must NOT read as "hide one" — the same window shape is a
        normal Agent Teams split, where hiding a teammate costs the
        reader that teammate's PERMIT."""
        self._stub_world(
            monkeypatch, tmp_path,
            projects=(self._one_project(),),
            ps_text=self._PS_TWO_CLAUDE,
            panes_cache=self._PANES_TWO_CLAUDE,
        )
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "multi-claude windows" in out
        assert "alpha (2)" in out
        assert "Agent Teams" in out, "the teammate reading is missing"
        assert "CCM_IGNORE" in out, "the sidekick reading is missing"

    def test_no_multi_claude_row_for_a_single_claude_window(
        self, tmp_path, monkeypatch, capsys
    ):
        """The ordinary one-claude window must stay silent — this row
        exists to explain a surprise, not to annotate normality."""
        self._stub_world(
            monkeypatch, tmp_path,
            projects=(self._one_project(),),
            ps_text="900 100 900 claude",
            panes_cache=(("0:1", "100", "%10", "claude", "1", "40", ""),),
        )
        ccm_commands.cmd_doctor()
        assert "multi-claude" not in capsys.readouterr().out

    def test_ignored_sidekick_pane_is_not_counted(
        self, tmp_path, monkeypatch, capsys
    ):
        """Once the sidekick is hidden the window has ONE visible
        claude, so the row disappears. Counting ignored panes here
        would keep nagging a reader who already acted on it."""
        self._stub_world(
            monkeypatch, tmp_path,
            projects=(self._one_project(),),
            ps_text=self._PS_TWO_CLAUDE,
            panes_cache=(
                ("0:1", "100", "%10", "claude", "1", "40", ""),
                ("0:1", "200", "%11", "claude", "0", "40", "1"),
            ),
        )
        ccm_commands.cmd_doctor()
        assert "multi-claude" not in capsys.readouterr().out

    def test_multi_claude_scan_failure_is_reported_not_dropped(
        self, tmp_path, monkeypatch, capsys
    ):
        """When the scan itself fails, say so. A check that vanishes
        silently reads as a check that passed, and doctor is the
        command you run when something is already wrong."""
        self._stub_world(monkeypatch, tmp_path,
                         projects=(self._one_project(),))
        monkeypatch.setattr(ccm_core, "_build_panes_cache",
                            lambda: (_ for _ in ()).throw(OSError("tmux gone")))
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "multi-claude windows" in out
        assert "not checked" in out

    def test_empty_ps_output_does_not_pass_the_scan_guard(
        self, tmp_path, monkeypatch, capsys
    ):
        """`"".split("\\n")` is `[""]`, which is truthy — so an empty ps
        snapshot used to wave the guard through with no data.

        Asserting only "no row appears" would pass either way, since a
        scan over `[""]` resolves no claude and prints nothing anyway.
        So assert the scan is not entered at all: with no process list
        there is nothing to resolve panes against, and the guard exists
        to say so."""
        scanned = []
        monkeypatch.setattr(
            ccm_pane_state, "find_claude_pid",
            lambda pid, ps: scanned.append(pid),
        )
        self._stub_world(
            monkeypatch, tmp_path,
            projects=(self._one_project(),),
            ps_text="",
            panes_cache=self._PANES_TWO_CLAUDE,
        )
        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert scanned == [], f"scanned panes with no ps data: {scanned}"
        assert "multi-claude" not in out

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

    def test_missing_probe_binaries_render_not_found_not_crash(
            self, tmp_path, monkeypatch, capsys):
        """Regression: doctor is the dependency-check command — when
        `which` / `tmux` themselves are absent, every probe raises
        FileNotFoundError. That must degrade to 'not found' rows,
        not crash the whole report."""
        self._stub_world(monkeypatch, tmp_path)

        def raising_run(*a, **kw):
            raise FileNotFoundError("no such file or directory")
        monkeypatch.setattr("subprocess.run", raising_run)

        ccm_commands.cmd_doctor()
        out = capsys.readouterr().out
        assert "binary not found" in out          # claude
        assert "not found" in out                  # tmux
        assert "not found (recommended)" in out    # jq / fzf


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
    def test_add_create_dir_creates_missing_leaf(
        self, mock_session, mock_tmux, mock_batch, mock_hooks, mock_auto, tmp_path
    ):
        """create_dir=True + missing leaf + existing parent → mkdir
        then proceed. The created directory is tagged on @ccm_dir
        and registered as a project."""
        target = tmp_path / "fresh-project"
        assert not target.exists()

        def tmux_side_effect(*args, **kwargs):
            if args[0] == "list-windows":
                return ""
            if args[0] == "new-window":
                return "1"
            if args[0] == "display-message":
                return "fresh-project"
            return ""
        mock_tmux.side_effect = tmux_side_effect

        ccm_commands.cmd_add(str(target), "fresh-project", create_dir=True)

        assert target.is_dir(), "leaf directory should have been created"
        assert mock_batch.called

    def test_add_create_dir_refuses_recursive(self, tmp_path):
        """Parent-must-exist gate: refuse to recursively create.
        Typo'd deep paths are a far more common failure mode than
        legitimate deep-tree creation, so the safer default is
        explicit refusal that forces the caller to spell intent."""
        target = tmp_path / "gone" / "leaf"
        with pytest.raises(SystemExit):
            ccm_commands.cmd_add(str(target), create_dir=True)
        assert not target.exists()
        assert not target.parent.exists()

    def test_add_create_dir_refuses_existing_file(self, tmp_path):
        """A non-directory already at the path is a user error; the
        underlying mkdir would fail anyway, but the tailored message
        tells the user exactly why."""
        f = tmp_path / "afile"
        f.write_text("x")
        with pytest.raises(SystemExit):
            ccm_commands.cmd_add(str(f), create_dir=True)

    def test_add_create_dir_false_is_default(self, tmp_path):
        """Default behavior is unchanged: missing dir dies, no mkdir.
        Snapshot-load relies on this — a stale snapshot whose dir
        was deleted must skip with a warning, not silently re-create
        an empty directory."""
        target = tmp_path / "nope"
        with pytest.raises(SystemExit):
            ccm_commands.cmd_add(str(target))  # create_dir defaults False
        assert not target.exists()

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


# ─── cmd_reset ───

class TestCmdReset:
    """`ccm reset <name>` — runtime-state recovery hatch. Must wipe
    only ephemeral runtime artefacts; the conversation JSONL,
    Claude Code's session info, and the tmux window itself stay
    untouched. Whitelist-driven design — see `_RESET_WINDOW_OPTIONS`."""

    def test_reset_empty_name_exits(self):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_reset("")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_reset_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_reset("nonexistent")

    @patch("ccm_signals.cleanup_project_runtime_files")
    @patch("ccm_core.tmux_cmd", return_value="/x/proj-dir")
    @patch("ccm_core.find_window", return_value="3")
    @patch("ccm_core.get_session", return_value="main")
    def test_reset_calls_cleanup_with_project_dir(
        self, mock_session, mock_find, mock_tmux, mock_cleanup,
    ):
        ccm_commands.cmd_reset("proj")
        # cleanup_project_runtime_files runs with the resolved @ccm_dir
        mock_cleanup.assert_called_once_with("/x/proj-dir")

    @patch("ccm_signals.cleanup_project_runtime_files")
    @patch("ccm_core.tmux_cmd", return_value="/x/proj-dir")
    @patch("ccm_core.find_window", return_value="3")
    @patch("ccm_core.get_session", return_value="main")
    def test_reset_unsets_only_whitelisted_window_options(
        self, mock_session, mock_find, mock_tmux, mock_cleanup,
    ):
        ccm_commands.cmd_reset("proj")
        unset_options = {
            c.args[-1] for c in mock_tmux.call_args_list
            if c.args[:1] == ("set-option",) and "-u" in c.args
        }
        # Must touch the whitelisted ephemeral options...
        assert unset_options == set(ccm_commands._RESET_WINDOW_OPTIONS)
        # ...and never the source-of-truth options that identify
        # the window as a ccm project.
        assert "@ccm_project" not in unset_options
        assert "@ccm_dir" not in unset_options

    @patch("ccm_signals.cleanup_project_runtime_files")
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.find_window", return_value="3")
    @patch("ccm_core.get_session", return_value="main")
    def test_reset_does_not_kill_window_or_exit_claude(
        self, mock_session, mock_find, mock_tmux, mock_cleanup,
    ):
        mock_tmux.return_value = "/x/proj-dir"
        ccm_commands.cmd_reset("proj")
        commands = [c.args[0] for c in mock_tmux.call_args_list if c.args]
        # Recovery, not removal — must not kill or send exit signals.
        assert "kill-window" not in commands
        assert "send-keys" not in commands

    @patch("ccm_signals.cleanup_project_runtime_files")
    @patch("ccm_core.tmux_cmd", return_value="")
    @patch("ccm_core.find_window", return_value="3")
    @patch("ccm_core.get_session", return_value="main")
    def test_reset_skips_cleanup_when_no_proj_dir(
        self, mock_session, mock_find, mock_tmux, mock_cleanup,
    ):
        # `@ccm_dir` empty (e.g. registration drift) — still wipe
        # window options, but skip cwd-keyed cache cleanup.
        ccm_commands.cmd_reset("proj")
        mock_cleanup.assert_not_called()


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
    """`send` / `capture` / `errors` / `stop` accept flags that
    argparse would otherwise reject or swallow. The dispatcher must
    pass raw argv through to the handler instead of letting argparse
    intercept the flags."""

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

    @patch("ccm_commands.cmd_stop")
    def test_stop_all_reaches_handler(self, mock_stop):
        # `ccm stop --all` — argparse used to reject `--all` as an
        # unknown option (exit 2) before cmd_stop ever saw it, so the
        # documented autosave-on-stop path was unreachable from the
        # CLI. The stop subcommand now uses the same raw-argv
        # passthrough as capture/send.
        ccm_core.dispatch(["stop", "--all"])
        mock_stop.assert_called_once_with("--all")

    @patch("ccm_commands.cmd_stop")
    def test_stop_name_reaches_handler(self, mock_stop):
        # Existing `ccm stop <name>` behaviour is unchanged.
        ccm_core.dispatch(["stop", "blog"])
        mock_stop.assert_called_once_with("blog")

    @patch("ccm_commands.cmd_stop")
    def test_stop_without_args_reaches_handler(self, mock_stop):
        # No arg → cmd_stop prints usage and dies (as before).
        ccm_core.dispatch(["stop"])
        mock_stop.assert_called_once_with("")

    def test_stop_extra_args_rejected(self):
        # `ccm stop a b` is not a valid invocation; die with usage
        # instead of silently dropping the extra argument.
        with pytest.raises(SystemExit):
            ccm_core.dispatch(["stop", "a", "b"])

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




# ─── cmd_attach ───

class TestCmdAttach:
    """`ccm attach <name|number>` switches to a project window and
    auto-starts claude when the window has no claude process. The
    claude check must scan EVERY pane of the window (split-pane
    layouts put claude in pane 2+), and any ps failure (exception OR
    non-zero rc) must fall to the safe side — assume claude is
    running rather than auto-starting a duplicate into a live
    session."""

    def _stub_attach(self, monkeypatch, *, pane_pids="1001",
                     ps_stdout=b"", ps_returncode=0, ps_exc=None,
                     current_idx="1", find_window="2",
                     windows=None, window_names=""):
        """Stub the tmux/ps world cmd_attach reads. Returns the
        (auto_start, reset, tmux_cmd) mocks for assertion."""
        monkeypatch.setattr(ccm_core, "get_session", lambda: "main")
        monkeypatch.setattr(ccm_core, "find_window",
                            lambda s, n: find_window)
        monkeypatch.setattr(ccm_core, "list_windows_raw",
                            lambda s: list(windows or []))
        tmux_mock = MagicMock()

        def tmux_side_effect(*args, **kw):
            if args[0] == "display-message":
                return current_idx
            if args[0] == "list-panes":
                return pane_pids
            if args[0] == "list-windows":
                return window_names
            return ""
        tmux_mock.side_effect = tmux_side_effect
        monkeypatch.setattr(ccm_core, "tmux_cmd", tmux_mock)

        if ps_exc is not None:
            def raising_run(*a, **kw):
                raise ps_exc
            monkeypatch.setattr("subprocess.run", raising_run)
        else:
            monkeypatch.setattr(
                "subprocess.run",
                lambda *a, **kw: MagicMock(returncode=ps_returncode,
                                           stdout=ps_stdout))

        auto_start = MagicMock()
        reset = MagicMock()
        monkeypatch.setattr(ccm_window, "auto_start_claude", auto_start)
        monkeypatch.setattr(ccm_window, "reset_window_after_attach", reset)
        return auto_start, reset, tmux_mock

    def test_claude_in_second_pane_skips_autostart(self, monkeypatch):
        """Regression: pane 1 is a shell, claude runs in pane 2. The
        old code checked only the first list-panes line and wrongly
        auto-started a duplicate claude."""
        auto_start, _, tmux_mock = self._stub_attach(
            monkeypatch,
            pane_pids="1001\n1002",
            ps_stdout=b"  PPID COMM\n  1001 zsh\n  1002 claude\n",
        )
        ccm_commands.cmd_attach("proj")
        auto_start.assert_not_called()
        # Still switches to the window.
        select_calls = [c for c in tmux_mock.call_args_list
                        if c.args[:1] == ("select-window",)]
        assert len(select_calls) == 1
        assert "main:2" in select_calls[0].args

    def test_no_claude_in_any_pane_autostarts(self, monkeypatch):
        auto_start, reset, _ = self._stub_attach(
            monkeypatch,
            pane_pids="1001\n1002",
            ps_stdout=b"  PPID COMM\n  1001 zsh\n  1002 vim\n",
        )
        ccm_commands.cmd_attach("proj")
        auto_start.assert_called_once_with("main:2")
        reset.assert_called_once_with("main:2")

    def test_ps_timeout_assumes_running(self, monkeypatch):
        """Safe side: a ps exception must not trigger auto-start."""
        auto_start, _, _ = self._stub_attach(
            monkeypatch,
            ps_exc=subprocess.TimeoutExpired("ps", 5),
        )
        ccm_commands.cmd_attach("proj")
        auto_start.assert_not_called()

    def test_ps_oserror_assumes_running(self, monkeypatch):
        auto_start, _, _ = self._stub_attach(
            monkeypatch,
            ps_exc=OSError("ps blew up"),
        )
        ccm_commands.cmd_attach("proj")
        auto_start.assert_not_called()

    def test_ps_nonzero_rc_assumes_running(self, monkeypatch):
        """Regression: ps exiting non-zero with empty stdout used to
        fall through to has_claude=False and wrongly auto-start."""
        auto_start, _, _ = self._stub_attach(
            monkeypatch,
            ps_stdout=b"",
            ps_returncode=1,
        )
        ccm_commands.cmd_attach("proj")
        auto_start.assert_not_called()

    def test_already_in_window_returns_early(self, monkeypatch, capsys):
        auto_start, _, tmux_mock = self._stub_attach(
            monkeypatch, current_idx="2",
        )
        ccm_commands.cmd_attach("proj")
        out = capsys.readouterr().out
        assert "Already in this window" in out
        auto_start.assert_not_called()
        select_calls = [c for c in tmux_mock.call_args_list
                        if c.args[:1] == ("select-window",)]
        assert not select_calls

    def test_attach_by_window_index(self, monkeypatch):
        auto_start, _, tmux_mock = self._stub_attach(
            monkeypatch,
            windows=[("2", "sess", "proj", "/dir")],
            ps_stdout=b"  PPID COMM\n  1001 claude\n",
        )
        ccm_commands.cmd_attach("2")
        auto_start.assert_not_called()
        select_calls = [c for c in tmux_mock.call_args_list
                        if c.args[:1] == ("select-window",)]
        assert len(select_calls) == 1
        assert "main:2" in select_calls[0].args

    def test_attach_by_window_index_not_found_exits(self, monkeypatch):
        self._stub_attach(monkeypatch, windows=[])
        with pytest.raises(SystemExit):
            ccm_commands.cmd_attach("9")

    def test_attach_by_window_name_fallback(self, monkeypatch):
        """find_window misses; fall back to matching the tmux window
        name column."""
        auto_start, _, tmux_mock = self._stub_attach(
            monkeypatch,
            find_window=None,
            window_names="1\twin-a\n3\tmyproj",
            ps_stdout=b"  PPID COMM\n  1001 claude\n",
        )
        ccm_commands.cmd_attach("myproj")
        select_calls = [c for c in tmux_mock.call_args_list
                        if c.args[:1] == ("select-window",)]
        assert len(select_calls) == 1
        assert "main:3" in select_calls[0].args

    def test_project_not_found_exits(self, monkeypatch):
        self._stub_attach(monkeypatch, find_window=None, window_names="")
        with pytest.raises(SystemExit):
            ccm_commands.cmd_attach("ghost")

    def test_no_session_exits(self, monkeypatch):
        monkeypatch.setattr(ccm_core, "get_session", lambda: None)
        with pytest.raises(SystemExit):
            ccm_commands.cmd_attach("proj")


# ─── cmd_capture ───

class TestCmdCapture:
    """`ccm capture [--copy] <name|#id|window_index>` dumps the
    visible pane content or pipes it to the clipboard."""

    def _stub_capture(self, monkeypatch, *, find_window="2",
                      windows=None, captured="line1\nline2",
                      clipboard_ok=True):
        monkeypatch.setattr(ccm_core, "get_session", lambda: "main")
        monkeypatch.setattr(ccm_core, "find_window",
                            lambda s, n: find_window)
        monkeypatch.setattr(ccm_core, "list_windows_raw",
                            lambda s: list(windows or []))
        tmux_mock = MagicMock()
        tmux_mock.side_effect = lambda *a, **kw: (
            captured if a[0] == "capture-pane" else "")
        monkeypatch.setattr(ccm_core, "tmux_cmd", tmux_mock)
        clipboard = MagicMock(return_value=clipboard_ok)
        monkeypatch.setattr(ccm_core, "clipboard_copy", clipboard)
        return clipboard, tmux_mock

    def test_help_prints_usage(self, capsys):
        ccm_commands.cmd_capture(["--help"])
        assert "Usage: ccm capture" in capsys.readouterr().out

    def test_missing_target_exits(self):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_capture([])

    def test_capture_by_name_prints_content(self, monkeypatch, capsys):
        self._stub_capture(monkeypatch)
        ccm_commands.cmd_capture(["proj"])
        out = capsys.readouterr().out
        assert "=== ccm capture: proj ===" in out
        assert "line1" in out and "line2" in out
        assert "=== end ===" in out

    def test_capture_by_window_index(self, monkeypatch, capsys):
        self._stub_capture(
            monkeypatch,
            windows=[("1", "sess", "indexed-proj", "/dir")],
        )
        ccm_commands.cmd_capture(["1"])
        out = capsys.readouterr().out
        assert "indexed-proj" in out
        assert "line1" in out

    def test_capture_by_hash_id(self, monkeypatch, capsys):
        self._stub_capture(
            monkeypatch,
            windows=[("1", "sess", "hash-proj", "/dir")],
        )
        ccm_commands.cmd_capture(["#1"])
        assert "hash-proj" in capsys.readouterr().out

    def test_capture_unknown_index_exits(self, monkeypatch):
        self._stub_capture(monkeypatch, windows=[])
        with pytest.raises(SystemExit):
            ccm_commands.cmd_capture(["7"])

    def test_capture_unknown_name_exits(self, monkeypatch):
        self._stub_capture(monkeypatch, find_window=None)
        with pytest.raises(SystemExit):
            ccm_commands.cmd_capture(["ghost"])

    def test_copy_mode_success(self, monkeypatch, capsys):
        clipboard, _ = self._stub_capture(monkeypatch)
        ccm_commands.cmd_capture(["--copy", "proj"])
        clipboard.assert_called_once_with("line1\nline2")
        out = capsys.readouterr().out
        assert "clipboard" in out
        assert "proj" in out

    def test_copy_mode_without_clipboard_tool_warns(self, monkeypatch,
                                                    capsys):
        clipboard, _ = self._stub_capture(monkeypatch, clipboard_ok=False)
        ccm_commands.cmd_capture(["-c", "proj"])
        clipboard.assert_called_once()
        # ccm_warn writes to stderr.
        assert "No clipboard tool" in capsys.readouterr().err


class TestCmdCaptureSplitWindow:
    """`capture-pane -t <window>` delivers only the window's ACTIVE
    pane, so a split window used to return one pane and silently drop
    the rest — from inside the window that is the caller's own pane,
    and from outside it is whichever pane held focus. (The same
    window-vs-pane flaw the dashboard preview had before
    `_resolve_preview_pane`.) capture now enumerates the panes and
    labels each section, so a split window is fully visible and the
    result no longer depends on focus."""

    def _stub(self, monkeypatch, panes, bodies, ignore_map=None):
        """Stub a window whose `list-panes` yields `panes`
        (pane_id, pane_pid, active, current_command) and whose
        per-pane capture returns `bodies[pane_id]`."""
        monkeypatch.setattr(ccm_core, "get_session", lambda: "main")
        monkeypatch.setattr(ccm_core, "find_window", lambda s, n: "2")
        monkeypatch.setattr(ccm_core, "list_windows_raw", lambda s: [])
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        ignore_map = ignore_map or {}

        def fake_tmux(*a, **kw):
            if a[0] == "list-panes":
                if "#{pane_id}" == a[-1]:          # cheap count probe
                    return "\n".join(p[0] for p in panes)
                return "\n".join(                   # full enumeration
                    f"{pid}\t{ppid}\t{1 if act else 0}\t{cmd}\t"
                    f"{ignore_map.get(pid, '')}"
                    for pid, ppid, act, cmd in panes)
            if a[0] == "capture-pane":
                return bodies.get(a[2], "")
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake_tmux)

    def test_all_panes_captured_with_labels(self, monkeypatch, capsys):
        """The regression: a 2-pane window must show BOTH panes."""
        self._stub(
            monkeypatch,
            panes=[("%1", "100", True, "zsh"), ("%94", "200", False, "kimi")],
            bodies={"%1": "claude side", "%94": "kimi side"},
        )
        ccm_commands.cmd_capture(["proj"])
        out = capsys.readouterr().out
        assert "claude side" in out and "kimi side" in out, \
            "a split window must not silently drop a pane"
        assert "--- pane %1 [zsh] (active) ---" in out
        assert "--- pane %94 [kimi] ---" in out

    def test_claude_pane_labelled_by_role_not_command(self, monkeypatch,
                                                     capsys):
        """A claude pane reports a versioned launcher as its
        foreground command (e.g. `2_1_220`), which reads as noise —
        the process-tree resolution must label it `claude`."""
        monkeypatch.setattr(
            "ccm_pane_state.find_claude_pid",
            lambda pid, ps: "999" if pid == "100" else None)
        self._stub(
            monkeypatch,
            panes=[("%1", "100", True, "2_1_220"),
                   ("%94", "200", False, "kimi")],
            bodies={"%1": "a", "%94": "b"},
        )
        ccm_commands.cmd_capture(["proj"])
        out = capsys.readouterr().out
        assert "[claude]" in out and "2_1_220" not in out

    def test_ignored_pane_is_captured_and_marked(self, monkeypatch, capsys):
        """`CCM_IGNORE` means "ccm does not track or write to this
        pane", not "hide it from a read the user explicitly asked
        for" — the sidekick is often the very thing being inspected.
        It is captured, and marked so the output still says which
        pane ccm keeps its hands off."""
        self._stub(
            monkeypatch,
            panes=[("%1", "100", True, "zsh"), ("%94", "200", False, "kimi")],
            bodies={"%1": "a", "%94": "sidekick body"},
            ignore_map={"%94": "1"},
        )
        ccm_commands.cmd_capture(["proj"])
        out = capsys.readouterr().out
        assert "sidekick body" in out
        assert "(ignored)" in out

    def test_single_pane_output_unchanged(self, monkeypatch, capsys):
        """Backward compatibility: one pane → no section headers,
        and no `ps` snapshot is taken (the label walk is only needed
        once there are labels to print)."""
        def boom():
            raise AssertionError("single-pane capture must not run ps")
        self._stub(
            monkeypatch,
            panes=[("%1", "100", True, "zsh")],
            # The single-pane path captures the WINDOW target, the
            # same call the pre-fix code made.
            bodies={"main:2": "solo"},
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot", boom)
        ccm_commands.cmd_capture(["proj"])
        out = capsys.readouterr().out
        assert "solo" in out
        assert "--- pane" not in out

    def test_enumeration_failure_falls_back_to_window(self, monkeypatch,
                                                     capsys):
        """tmux hiccup → empty enumeration → old window-target
        capture rather than an empty result."""
        monkeypatch.setattr(ccm_core, "get_session", lambda: "main")
        monkeypatch.setattr(ccm_core, "find_window", lambda s, n: "2")
        monkeypatch.setattr(ccm_core, "list_windows_raw", lambda s: [])
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")

        def fake_tmux(*a, **kw):
            if a[0] == "list-panes":
                # count probe sees two panes, enumeration then fails
                return "%1\n%94" if a[-1] == "#{pane_id}" else ""
            if a[0] == "capture-pane":
                return "fallback body" if a[2] == "main:2" else ""
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake_tmux)
        ccm_commands.cmd_capture(["proj"])
        assert "fallback body" in capsys.readouterr().out


# ─── cmd_debug_trace ───

class _StopTrace(Exception):
    """Sentinel raised by the stubbed time.sleep to break out of the
    trace loop after one tick."""


class TestCmdDebugTrace:
    """`ccm debug trace <project>` is a read-only observer loop. These
    tests cover the project-name resolution (exact match preferred
    over substring, substring against name or dir basename) and the
    shape of one emitted tick line; the loop itself is broken via a
    stubbed time.sleep after the first tick."""

    def _run_one_tick(self, monkeypatch, capsys, *, rows, needle,
                      session="main"):
        """Run cmd_debug_trace for exactly one tick. Returns the
        (stdout, stderr) captured up to the loop break."""
        monkeypatch.setattr(ccm_core, "get_session", lambda: session)

        def tmux_side_effect(*args, **kw):
            if args[0] == "list-windows":
                return rows
            if args[0] == "list-panes":
                return ""
            return ""  # show-option etc.
        monkeypatch.setattr(ccm_core, "tmux_cmd", tmux_side_effect)
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr(ccm_detection, "build_detection_context",
                            lambda *a, **kw: make_ctx(raw="IDLE"))
        rule = ccm_rules.Rule(name="default", phase="idle")
        monkeypatch.setattr(
            ccm_detection, "resolve_state_from_context",
            lambda ctx, d: ("IDLE", rule, None))
        # Don't touch the real SIGINT handler, and break the loop
        # after the first tick.
        monkeypatch.setattr("signal.signal", lambda *a, **kw: None)

        def stop_sleep(_seconds):
            raise _StopTrace
        monkeypatch.setattr("time.sleep", stop_sleep)

        with pytest.raises(_StopTrace):
            ccm_commands.cmd_debug_trace(needle, interval=5.0)
        captured = capsys.readouterr()
        return captured.out, captured.err

    def test_exact_match_preferred_over_substring(self, monkeypatch,
                                                  capsys):
        # "alpha" is a substring of the first row's name but an exact
        # match of the second — the exact row must win.
        rows = ("main:1\talpha-two\t/dir/alpha-two\n"
                "main:2\talpha\t/dir/alpha")
        _, err = self._run_one_tick(monkeypatch, capsys,
                                    rows=rows, needle="alpha")
        assert "alpha (main:2)" in err

    def test_substring_match_on_name(self, monkeypatch, capsys):
        rows = ("main:1\talpha\t/dir/alpha\n"
                "main:2\talpha-two\t/dir/alpha-two")
        _, err = self._run_one_tick(monkeypatch, capsys,
                                    rows=rows, needle="two")
        assert "alpha-two (main:2)" in err

    def test_substring_match_on_dir_basename(self, monkeypatch, capsys):
        rows = "main:1\tproj-x\t/code/blog-engine"
        _, err = self._run_one_tick(monkeypatch, capsys,
                                    rows=rows, needle="blog")
        assert "proj-x (main:1)" in err

    def test_tick_line_contains_context_columns(self, monkeypatch,
                                                capsys):
        rows = "main:1\talpha\t/dir/alpha"
        out, err = self._run_one_tick(monkeypatch, capsys,
                                      rows=rows, needle="alpha")
        assert "raw=IDLE" in out
        assert "prev=-" in out
        assert "default[idle]" in out
        assert "→ IDLE" in out and "[WRITE]" in out
        # Header documents the trace target and columns.
        assert "interval=5.0s" in err

    def test_no_match_exits(self, monkeypatch):
        monkeypatch.setattr(ccm_core, "get_session", lambda: "main")
        # Neither a project match nor a resolvable tmux target: the
        # display-message probe returns "" for an unknown target.
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *a, **kw: "main:1\talpha\t/dir/alpha"
                            if a[0] == "list-windows" else "")
        with pytest.raises(SystemExit):
            ccm_commands.cmd_debug_trace("ghost")

    def test_no_session_exits(self, monkeypatch):
        monkeypatch.setattr(ccm_core, "get_session", lambda: None)
        with pytest.raises(SystemExit):
            ccm_commands.cmd_debug_trace("alpha")


class TestResolveTraceTarget:
    """`debug trace` accepts an unregistered tmux target so a probe
    session — the kind spun up for an experiment and never registered
    as a ccm project — can be observed directly instead of by grepping
    across every session's event log."""

    WINDOWS = "main:1\talpha\t/dir/alpha"

    def _tmux(self, *, windows=WINDOWS, display=""):
        def side_effect(*args, **kw):
            if args[0] == "list-windows":
                return windows
            if args[0] == "display-message":
                return display
            return ""
        return side_effect

    def test_project_match_wins_over_tmux_target(self, monkeypatch):
        # A registered project must keep its meaning even when tmux
        # would also resolve the same string, so pass 1 runs first.
        monkeypatch.setattr(
            ccm_core, "tmux_cmd",
            self._tmux(display="other:9\t/somewhere/else"))
        assert ccm_commands._resolve_trace_target("alpha") == (
            "main:1", "alpha", "/dir/alpha")

    def test_pane_id_resolves_with_pane_cwd(self, monkeypatch):
        monkeypatch.setattr(
            ccm_core, "tmux_cmd",
            self._tmux(display="probe:0\t/tmp/probe-dir"))
        win, label, d = ccm_commands._resolve_trace_target("%42")
        assert win == "probe:0"
        assert d == "/tmp/probe-dir"
        # The label must say the target is not a ccm project, so trace
        # output is never mistaken for a managed window.
        assert "unregistered" in label

    def test_window_id_resolves(self, monkeypatch):
        monkeypatch.setattr(
            ccm_core, "tmux_cmd", self._tmux(display="probe:3\t/tmp/w"))
        win, _, d = ccm_commands._resolve_trace_target("@7")
        assert (win, d) == ("probe:3", "/tmp/w")

    def test_unknown_target_returns_none(self, monkeypatch):
        # Measured tmux behaviour: `display-message -t %9999` exits 0
        # and substitutes empty fields, so the reply is the bare ":"
        # separator rather than "". Anchored on the real response —
        # an earlier mock returned "" here and let every typo resolve
        # to a phantom window in live use.
        monkeypatch.setattr(ccm_core, "tmux_cmd", self._tmux(display=":"))
        assert ccm_commands._resolve_trace_target("ghost") is None

    def test_unknown_target_with_cwd_field_returns_none(self, monkeypatch):
        # Same empty-target reply, but with the cwd field present.
        monkeypatch.setattr(ccm_core, "tmux_cmd", self._tmux(display=":\t"))
        assert ccm_commands._resolve_trace_target("ghost") is None

    def test_empty_display_reply_returns_none(self, monkeypatch):
        monkeypatch.setattr(ccm_core, "tmux_cmd", self._tmux(display=""))
        assert ccm_commands._resolve_trace_target("ghost") is None

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_target_returns_none(self, monkeypatch, blank):
        """An empty needle is a substring of every name, so the pass-1
        substring loop would return whichever project is listed first
        and trace a window the caller never named. Measured before the
        guard: `ccm debug trace ""` followed the first project."""
        monkeypatch.setattr(
            ccm_core, "tmux_cmd", self._tmux(display="probe:0\t/tmp/p"))
        assert ccm_commands._resolve_trace_target(blank) is None

    def test_target_without_cwd_field_still_resolves(self, monkeypatch):
        # A pane whose current path tmux cannot report yields an empty
        # dir rather than refusing: the event-log path is keyed on
        # session_id, so a missing cwd degrades JSONL lookup only.
        monkeypatch.setattr(
            ccm_core, "tmux_cmd", self._tmux(display="probe:0"))
        win, _, d = ccm_commands._resolve_trace_target("%1")
        assert (win, d) == ("probe:0", "")

    def test_no_registered_windows_falls_through_to_tmux(self, monkeypatch):
        monkeypatch.setattr(
            ccm_core, "tmux_cmd",
            self._tmux(windows="", display="probe:0\t/tmp/p"))
        assert ccm_commands._resolve_trace_target("%1")[0] == "probe:0"
