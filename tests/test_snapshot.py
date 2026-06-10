"""Tests for ccm_snapshot — project-state persistence (save / load /
list / delete) plus the snapshot-name sanitizer."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

import pytest

import ccm_core
import ccm_commands
import ccm_snapshot


class TestSanitizeSnapshotName:
    def test_basic(self):
        assert ccm_snapshot._sanitize_snapshot_name("my-snapshot") == "my-snapshot"

    def test_path_traversal(self):
        assert ccm_snapshot._sanitize_snapshot_name("../../etc/passwd") == "passwd"

    def test_slash(self):
        assert ccm_snapshot._sanitize_snapshot_name("foo/bar") == "bar"

    def test_dots_only(self):
        with pytest.raises(SystemExit):
            ccm_snapshot._sanitize_snapshot_name("..")

    def test_empty(self):
        with pytest.raises(SystemExit):
            ccm_snapshot._sanitize_snapshot_name("")


class TestSnapshotSave:
    @patch("ccm_core.tmux_cmd")
    def test_creates_json(self, mock_tmux, tmp_path):
        mock_tmux.return_value = "1\twin1\tproj1\t/home/user/dir1\n2\twin2\tproj2\t/home/user/dir2"
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        ccm_snapshot.cmd_snapshot_save("test-snap", quiet=True)
        fp = tmp_path / "test-snap.json"
        assert fp.exists()
        data = json.loads(fp.read_text())
        assert data["name"] == "test-snap"
        assert data["version"] == 1
        assert len(data["projects"]) == 2

    @patch("ccm_core.tmux_cmd")
    def test_skips_empty_project(self, mock_tmux, tmp_path):
        mock_tmux.return_value = "1\twin1\t\t/dir1\n2\twin2\tproj2\t/dir2"
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        ccm_snapshot.cmd_snapshot_save("test2", quiet=True)
        data = json.loads((tmp_path / "test2.json").read_text())
        assert len(data["projects"]) == 1

    @patch("ccm_core.tmux_cmd")
    def test_round_trip_preserves_project_fields(self, mock_tmux, tmp_path):
        """Save a snapshot from a synthetic project list and verify
        every field needed by the load path round-trips through the
        on-disk JSON. Catches drift in either the save serialization
        or the load schema expectations.

        Format mirrors `tmux list-windows -a -F` with the four fields
        the saver requests:
          window_index <TAB> window_name <TAB> @ccm_project <TAB> @ccm_dir
        """
        mock_tmux.return_value = (
            "1\twin-α\tα-プロジェクト\t/tmp/with spaces/proj-a\n"
            "2\twin-β\tproj-β\t/tmp/proj b\n"
            "3\twin-γ\tregular\t/tmp/regular"
        )
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        ccm_snapshot.cmd_snapshot_save("rt-snap", quiet=True)

        on_disk = json.loads((tmp_path / "rt-snap.json").read_text())

        # Schema invariants
        assert on_disk["version"] == 1
        assert on_disk["name"] == "rt-snap"
        assert "created" in on_disk
        assert isinstance(on_disk["projects"], list)
        assert len(on_disk["projects"]) == 3

        # Field round-trip — names with non-ASCII, dirs with spaces
        names = [p["name"] for p in on_disk["projects"]]
        dirs = [p["dir"] for p in on_disk["projects"]]
        assert "α-プロジェクト" in names
        assert "proj-β" in names
        assert "regular" in names
        assert "/tmp/with spaces/proj-a" in dirs
        assert "/tmp/proj b" in dirs

        # Every project entry must carry the keys load expects
        for p in on_disk["projects"]:
            assert "name" in p and p["name"]
            assert "dir" in p and p["dir"]


class TestSnapshotLoad:
    def _write_snapshot(self, tmp_path, name, projects):
        snap = {"version": 1, "name": name, "created": "2025-01-01T00:00:00+0000",
                "projects": projects}
        fp = tmp_path / f"{name}.json"
        fp.write_text(json.dumps(snap))

    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_core.hooks_configured", return_value=True)
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.get_session", return_value="main")
    def test_save_load_round_trip_via_disk(
        self, mock_session, mock_tmux, mock_batch, mock_hooks, mock_auto, tmp_path
    ):
        """End-to-end round trip: save a snapshot from a synthetic
        project list, then load the same file back and assert the load
        path creates the expected windows. Catches schema drift between
        save and load that the JSON-only round trip
        (`test_round_trip_preserves_project_fields`) cannot see — e.g.
        if `cmd_snapshot_save` started writing `path` while
        `cmd_snapshot_load` still reads `dir`."""
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        proj_dir_a = tmp_path / "alpha"
        proj_dir_b = tmp_path / "beta"
        proj_dir_a.mkdir()
        proj_dir_b.mkdir()

        save_listing = (
            f"1\twin1\talpha\t{proj_dir_a}\n"
            f"2\twin2\tbeta\t{proj_dir_b}"
        )

        def tmux_side_effect(*args, **kwargs):
            if args[0] == "list-windows" and "-a" in args:
                # Save phase asks for ccm windows; subsequent calls during
                # load also hit this. Return the same listing for save,
                # empty for load (so windows are seen as "missing").
                return save_listing if not load_phase["active"] else ""
            if args[0] == "list-windows":
                return ""
            if args[0] == "new-window":
                load_phase["new_windows"].append(kwargs.get("input", "") or " ".join(args))
                return "9"
            if args[0] == "display-message":
                return ""
            return ""

        load_phase = {"active": False, "new_windows": []}
        mock_tmux.side_effect = tmux_side_effect

        # SAVE
        ccm_snapshot.cmd_snapshot_save("rt-disk", quiet=True)
        snap_path = tmp_path / "rt-disk.json"
        assert snap_path.exists()

        # LOAD the file we just wrote
        load_phase["active"] = True
        ccm_snapshot.cmd_snapshot_load("rt-disk")

        # Two `new-window` calls expected (one per saved project)
        new_window_calls = [c for c in mock_tmux.call_args_list if c[0][0] == "new-window"]
        assert len(new_window_calls) == 2, (
            f"Expected 2 new-window calls during load, got {len(new_window_calls)}"
        )

    @patch("ccm_commands._autosave_trigger")
    @patch("ccm_core.hooks_configured", return_value=True)
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.get_session", return_value="main")
    def test_load_creates_windows(self, mock_session, mock_tmux, mock_batch, mock_hooks, mock_auto, tmp_path):
        """Loading a snapshot creates windows for each project."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        proj_dir = tmp_path / "myproject"
        proj_dir.mkdir()

        self._write_snapshot(tmp_path, "test-snap", [
            {"name": "proj1", "dir": str(proj_dir)},
        ])

        # find_window returns None (project doesn't exist yet), new-window returns "1"
        def tmux_side_effect(*args, **kwargs):
            if args[0] == "list-windows" and "-a" in args:
                return ""  # no existing projects (for autosave)
            if args[0] == "list-windows":
                return ""  # no existing ccm windows
            if args[0] == "new-window":
                return "1"
            if args[0] == "display-message":
                return "proj1"
            return ""
        mock_tmux.side_effect = tmux_side_effect

        ccm_snapshot.cmd_snapshot_load("test-snap")

        new_window_calls = [c for c in mock_tmux.call_args_list if c[0][0] == "new-window"]
        assert len(new_window_calls) == 1

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    @patch("ccm_core.get_session", return_value="main")
    @patch("ccm_core.tmux_cmd", return_value="")
    def test_load_skips_missing_dir(self, mock_tmux, mock_session, tmp_path, capsys):
        """Projects with missing directories are skipped with a warning."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        self._write_snapshot(tmp_path, "test-skip", [
            {"name": "gone", "dir": "/nonexistent/path/xyz"},
        ])

        ccm_snapshot.cmd_snapshot_load("test-skip")

        captured = capsys.readouterr()
        assert "Directory not found" in captured.err

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    @patch("ccm_core.find_window", return_value="1")
    @patch("ccm_core.get_session", return_value="main")
    @patch("ccm_core.tmux_cmd", return_value="")
    def test_load_skips_existing_project(self, mock_tmux, mock_session, mock_find, tmp_path, capsys):
        """Projects that already exist are skipped with a warning."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        proj_dir = tmp_path / "existing"
        proj_dir.mkdir()

        self._write_snapshot(tmp_path, "test-dup", [
            {"name": "existing-proj", "dir": str(proj_dir)},
        ])

        ccm_snapshot.cmd_snapshot_load("test-dup")

        captured = capsys.readouterr()
        assert "already exists" in captured.err

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    def test_load_nonexistent_snapshot(self, tmp_path):
        """Loading a non-existent snapshot exits with error."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        with pytest.raises(SystemExit):
            ccm_snapshot.cmd_snapshot_load("nonexistent")

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    @patch("ccm_core.get_session", return_value="main")
    @patch("ccm_core.tmux_cmd", return_value="")
    def test_load_skips_null_entries(self, mock_tmux, mock_session, tmp_path):
        """Null/empty project entries in snapshot are silently skipped."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        self._write_snapshot(tmp_path, "test-null", [
            {"name": "", "dir": "/some/path"},
            {"name": "null", "dir": "/some/path"},
            {"name": "valid", "dir": "null"},
        ])

        # Should not raise — all entries skipped
        ccm_snapshot.cmd_snapshot_load("test-null")

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    def test_load_top_level_non_dict_dies_cleanly(self, tmp_path):
        """A hand-edited / truncated snapshot can parse to valid JSON
        that is a top-level array or scalar (json.load does not raise
        on those). The load must die with a readable message, not an
        AttributeError traceback from `data.get(...)`. Regression for
        the adversarial-review finding 2026-06-11: the first cut of the
        malformed-JSON guard checked `projects` but not `data` itself,
        so `[1,2,3]` still crashed."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        (tmp_path / "badlist.json").write_text("[1, 2, 3]")
        try:
            with pytest.raises(SystemExit):
                ccm_snapshot.cmd_snapshot_load("badlist")
        finally:
            ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    def test_load_non_list_projects_dies_cleanly(self, tmp_path):
        """`projects` present but not a list (e.g. an object) must die
        readably rather than crash when len() / iteration is attempted."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        (tmp_path / "badproj.json").write_text(
            json.dumps({"version": 1, "name": "badproj", "projects": {"a": 1}})
        )
        try:
            with pytest.raises(SystemExit):
                ccm_snapshot.cmd_snapshot_load("badproj")
        finally:
            ccm_core.CCM_SNAPSHOT_DIR = orig_dir
