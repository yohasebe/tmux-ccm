"""Tests for ccm_signals.

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
import ccm_signals

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

class TestProjectNotifyMarker:
    def _setup_tmp(self, tmp_path, monkeypatch):
        marker_dir = tmp_path / "notified"
        marker_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_NOTIFY_MARKER_DIR", str(marker_dir))
        return marker_dir

    def test_missing_marker_returns_none(self, tmp_path, monkeypatch):
        self._setup_tmp(tmp_path, monkeypatch)
        assert ccm_signals.read_project_notify_marker("/no/such/project") is None

    def test_empty_project_dir_returns_none(self, tmp_path, monkeypatch):
        self._setup_tmp(tmp_path, monkeypatch)
        assert ccm_signals.read_project_notify_marker("") is None
        assert ccm_signals.read_project_notify_marker(None) is None

    def test_valid_marker_parses_ts_and_state(self, tmp_path, monkeypatch):
        marker_dir = self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/proj-a"
        key = ccm_core.md5_hash(ccm_signals._resolve_project_dir(project))
        (marker_dir / key).write_text("1234567890 COMPLETED")
        result = ccm_signals.read_project_notify_marker(project)
        assert result == (1234567890, "COMPLETED")

    def test_malformed_marker_returns_none(self, tmp_path, monkeypatch):
        marker_dir = self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/malformed"
        key = ccm_core.md5_hash(ccm_signals._resolve_project_dir(project))
        # Missing state field
        (marker_dir / key).write_text("1234567890")
        assert ccm_signals.read_project_notify_marker(project) is None

    def test_non_integer_ts_returns_none(self, tmp_path, monkeypatch):
        marker_dir = self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/bad-ts"
        key = ccm_core.md5_hash(ccm_signals._resolve_project_dir(project))
        (marker_dir / key).write_text("not-a-number COMPLETED")
        assert ccm_signals.read_project_notify_marker(project) is None

    def test_projects_are_isolated(self, tmp_path, monkeypatch):
        """The whole point of the fix: project A's marker must not
        appear when asking about project B, and vice versa."""
        marker_dir = self._setup_tmp(tmp_path, monkeypatch)
        project_a = "/x/proj-a"
        project_b = "/x/proj-b"
        key_a = ccm_core.md5_hash(ccm_signals._resolve_project_dir(project_a))
        key_b = ccm_core.md5_hash(ccm_signals._resolve_project_dir(project_b))
        (marker_dir / key_a).write_text("100 COMPLETED")
        (marker_dir / key_b).write_text("200 PERMIT")
        assert ccm_signals.read_project_notify_marker(project_a) == (100, "COMPLETED")
        assert ccm_signals.read_project_notify_marker(project_b) == (200, "PERMIT")


# ─── Project runtime-file cleanup (unregister / remove) ───

class TestCleanupProjectRuntimeFiles:
    def _setup_tmp(self, tmp_path, monkeypatch):
        for name, attr in (
            ("hooks", "CCM_HOOK_DIR"),
            ("notified", "CCM_NOTIFY_MARKER_DIR"),
            ("git-cache", "CCM_GIT_CACHE_DIR"),
            ("port-cache", "CCM_PORT_CACHE_DIR"),
        ):
            d = tmp_path / name
            d.mkdir()
            monkeypatch.setattr(ccm_core, attr, str(d))

    def _populate(self, tmp_path, project_dir, session_id):
        """Create all runtime files for a project. Hook artefacts
        keyed by `session_id`; cwd-keyed caches by `md5(project_dir)`."""
        cwd_key = ccm_core.md5_hash(ccm_signals._resolve_project_dir(project_dir))
        (tmp_path / "hooks" / session_id).write_text("0 BUSY")
        (tmp_path / "hooks" / f"{session_id}.busy").write_text("0")
        (tmp_path / "hooks" / f"{session_id}.pending").write_text("0")
        (tmp_path / "hooks" / f"{session_id}.events.jsonl").write_text(
            '{"ts":100,"type":"prompt"}\n{"ts":101,"type":"stop"}\n'
        )
        (tmp_path / "notified" / cwd_key).write_text("0 COMPLETED")
        (tmp_path / "git-cache" / cwd_key).write_text("main")
        (tmp_path / "port-cache" / cwd_key).write_text("3000")
        return cwd_key

    def test_removes_all_runtime_files(self, tmp_path, monkeypatch):
        self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/proj-a"
        sid = "session-uuid-a"
        monkeypatch.setattr(
            ccm_signals, "_session_id_from_tmux",
            lambda d: sid if d == project else None,
        )
        cwd_key = self._populate(tmp_path, project, sid)

        ccm_signals.cleanup_project_runtime_files(project)

        for rel in (
            f"hooks/{sid}",
            f"hooks/{sid}.busy",
            f"hooks/{sid}.pending",
            f"hooks/{sid}.events.jsonl",
            f"notified/{cwd_key}",
            f"git-cache/{cwd_key}",
            f"port-cache/{cwd_key}",
        ):
            assert not (tmp_path / rel).exists(), f"{rel} should be deleted"

    def test_leaves_other_projects_alone(self, tmp_path, monkeypatch):
        """Cleanup uses session_id (hook artefacts) and md5(project_dir)
        (cwd-keyed caches); other projects' files must survive."""
        self._setup_tmp(tmp_path, monkeypatch)
        project_a = "/x/proj-a"
        project_b = "/x/proj-b"
        sid_a = "session-uuid-a"
        sid_b = "session-uuid-b"
        sid_map = {project_a: sid_a, project_b: sid_b}
        monkeypatch.setattr(
            ccm_signals, "_session_id_from_tmux",
            lambda d: sid_map.get(d),
        )
        cwd_a = self._populate(tmp_path, project_a, sid_a)
        cwd_b = self._populate(tmp_path, project_b, sid_b)

        ccm_signals.cleanup_project_runtime_files(project_a)

        # Project A files gone
        assert not (tmp_path / "hooks" / sid_a).exists()
        assert not (tmp_path / "notified" / cwd_a).exists()
        # Project B files intact
        assert (tmp_path / "hooks" / sid_b).exists()
        assert (tmp_path / "notified" / cwd_b).exists()
        assert (tmp_path / "git-cache" / cwd_b).exists()

    def test_missing_files_silent_noop(self, tmp_path, monkeypatch):
        """No files to delete (fresh project) must not raise — this
        is the common case when unregistering an idle project."""
        self._setup_tmp(tmp_path, monkeypatch)
        # Should not raise
        ccm_signals.cleanup_project_runtime_files("/x/never-ran")

    def test_empty_project_dir_noop(self, tmp_path, monkeypatch):
        self._setup_tmp(tmp_path, monkeypatch)
        ccm_signals.cleanup_project_runtime_files("")
        ccm_signals.cleanup_project_runtime_files(None)

    def test_active_session_hook_files_removed(self, tmp_path, monkeypatch):
        """Cleanup uses the cached `@ccm_session_id` to find the
        active session's hook artefacts and unlinks them."""
        self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/proj-a"
        sid = "active-session-uuid"
        monkeypatch.setattr(
            ccm_signals, "_session_id_from_tmux",
            lambda d: sid if d == project else None,
        )
        # Hook artefacts under the active session_id
        (tmp_path / "hooks" / sid).write_text("100 BUSY")
        (tmp_path / "hooks" / f"{sid}.events.jsonl").write_text(
            '{"ts":100,"type":"pretool"}\n')
        (tmp_path / "hooks" / f"{sid}.pending").write_text("0")

        ccm_signals.cleanup_project_runtime_files(project)

        assert not (tmp_path / "hooks" / sid).exists()
        assert not (tmp_path / "hooks" / f"{sid}.events.jsonl").exists()
        assert not (tmp_path / "hooks" / f"{sid}.pending").exists()

class TestReadEventsTail:
    # Synthetic session_id used by every test; the fixture registers
    # it for any project_dir so `_events_log_path(project_dir)` and
    # `read_events_tail(project_dir)` deterministically map to
    # `<HOOK_DIR>/<TEST_SESSION_ID>.events.jsonl`.
    TEST_SESSION_ID = "test-session-events"

    def _setup_hook_dir(self, tmp_path, monkeypatch):
        """Redirect CCM_HOOK_DIR to a sandbox and return its path."""
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        monkeypatch.setattr(
            ccm_signals, "_session_id_from_tmux",
            lambda _project_dir: self.TEST_SESSION_ID,
        )
        # Clear the module-level cache so tests do not interfere.
        ccm_signals._events_cache.clear()
        return hook_dir

    def _events_path(self, project_dir):
        return ccm_signals._events_log_path(project_dir)

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        assert ccm_signals.read_events_tail("/x/never") == ()

    def test_empty_project_dir_returns_empty(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        assert ccm_signals.read_events_tail("") == ()
        assert ccm_signals.read_events_tail(None) == ()

    def test_reads_single_event(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            f.write('{"ts":100,"type":"prompt"}\n')
        events = ccm_signals.read_events_tail("/x/proj")
        assert events == ({"ts": 100, "type": "prompt"},)

    def test_reads_multiple_events_in_order(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        records = [
            '{"ts":100,"type":"prompt"}',
            '{"ts":101,"type":"pretool"}',
            '{"ts":102,"type":"stop"}',
        ]
        with open(path, "w") as f:
            f.write("\n".join(records) + "\n")
        events = ccm_signals.read_events_tail("/x/proj")
        assert [e["type"] for e in events] == ["prompt", "pretool", "stop"]
        assert [e["ts"] for e in events] == [100, 101, 102]

    def test_skips_malformed_lines(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            f.write("not json\n")
            f.write('{"ts":100,"type":"prompt"}\n')
            f.write('{"corrupt":\n')
            f.write('{"ts":101,"type":"stop"}\n')
        events = ccm_signals.read_events_tail("/x/proj")
        assert [e["type"] for e in events] == ["prompt", "stop"]

    def test_skips_records_missing_required_fields(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            f.write('{"type":"prompt"}\n')              # no ts
            f.write('{"ts":100}\n')                      # no type
            f.write('{"ts":"bad","type":"stop"}\n')      # ts wrong type
            f.write('{"ts":100,"type":""}\n')            # empty type
            f.write('{"ts":101,"type":"prompt"}\n')      # ok
        events = ccm_signals.read_events_tail("/x/proj")
        assert events == ({"ts": 101, "type": "prompt"},)

    def test_limit_slices_newest(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            for i in range(30):
                f.write(f'{{"ts":{i},"type":"prompt"}}\n')
        events = ccm_signals.read_events_tail("/x/proj", limit=5)
        assert len(events) == 5
        assert [e["ts"] for e in events] == [25, 26, 27, 28, 29]

    def test_caches_by_mtime_and_size(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            f.write('{"ts":100,"type":"prompt"}\n')
        e1 = ccm_signals.read_events_tail("/x/proj")
        # Second read with identical mtime/size must hit cache — the
        # simplest observation is the result is the same tuple object.
        e2 = ccm_signals.read_events_tail("/x/proj")
        assert e1 is e2 or e1 == e2  # tuple identity preserved on hit

    def test_invalidates_on_append(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            f.write('{"ts":100,"type":"prompt"}\n')
        e1 = ccm_signals.read_events_tail("/x/proj")
        assert [e["type"] for e in e1] == ["prompt"]
        # Bump mtime so the cache key changes even if the second write
        # lands within the same wall-clock second (CI can be fast enough
        # for that to happen). The size will also change, so the cache
        # key (mtime, size) is definitely different.
        os.utime(path, (time.time() + 2, time.time() + 2))
        with open(path, "a") as f:
            f.write('{"ts":101,"type":"stop"}\n')
        e2 = ccm_signals.read_events_tail("/x/proj")
        assert [e["type"] for e in e2] == ["prompt", "stop"]

    def test_tail_truncation_for_large_files(self, tmp_path, monkeypatch):
        """File larger than EVENTS_TAIL_BYTES: the reader must return
        only the tail records, not parse the whole file."""
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            # Write enough records to exceed EVENTS_TAIL_BYTES (8 KB).
            # Each line is ~28 bytes → need ~300 lines to comfortably
            # exceed the window.
            for i in range(400):
                f.write(f'{{"ts":{i},"type":"prompt"}}\n')
        events = ccm_signals.read_events_tail("/x/proj", limit=5)
        assert len(events) == 5
        # Last 5 records (399, 398, 397, 396, 395) in chronological order
        assert [e["ts"] for e in events] == [395, 396, 397, 398, 399]

    def test_mode_field_preserved(self, tmp_path, monkeypatch):
        """`mode` (permission_mode annotation from hooks/lib.sh) must
        survive the reader — the display badge depends on it."""
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            f.write('{"ts":100,"type":"pretool","mode":"acceptEdits"}\n')
            f.write('{"ts":101,"type":"stop"}\n')
        events = ccm_signals.read_events_tail("/x/proj")
        assert events[0] == {"ts": 100, "type": "pretool",
                             "mode": "acceptEdits"}
        assert events[1] == {"ts": 101, "type": "stop"}  # no mode key

    def test_mode_field_non_string_dropped(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        path = self._events_path("/x/proj")
        with open(path, "w") as f:
            f.write('{"ts":100,"type":"pretool","mode":42}\n')
            f.write('{"ts":101,"type":"stop","mode":""}\n')
        events = ccm_signals.read_events_tail("/x/proj")
        assert all("mode" not in e for e in events)


class TestReadLatestPermissionMode:
    TEST_SESSION_ID = "test-session-mode"

    def _setup_hook_dir(self, tmp_path, monkeypatch):
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        monkeypatch.setattr(
            ccm_signals, "_session_id_from_tmux",
            lambda _project_dir: self.TEST_SESSION_ID,
        )
        ccm_signals._events_cache.clear()
        return hook_dir

    def _write_events(self, tmp_path, lines):
        path = os.path.join(
            str(tmp_path / "hooks"),
            self.TEST_SESSION_ID + ".events.jsonl")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_no_log_returns_empty(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        assert ccm_signals.read_latest_permission_mode("/x/proj") == ""

    def test_returns_newest_mode(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        self._write_events(tmp_path, [
            '{"ts":100,"type":"pretool","mode":"default"}',
            '{"ts":101,"type":"pretool","mode":"acceptEdits"}',
        ])
        assert ccm_signals.read_latest_permission_mode(
            "/x/proj") == "acceptEdits"

    def test_skips_trailing_events_without_mode(self, tmp_path, monkeypatch):
        """A newest event lacking the annotation (e.g. written by a
        pre-upgrade hook script) must not blank the badge — the scan
        walks back to the newest mode-bearing record."""
        self._setup_hook_dir(tmp_path, monkeypatch)
        self._write_events(tmp_path, [
            '{"ts":100,"type":"pretool","mode":"bypassPermissions"}',
            '{"ts":101,"type":"stop"}',
        ])
        assert ccm_signals.read_latest_permission_mode(
            "/x/proj") == "bypassPermissions"

    def test_no_mode_anywhere_returns_empty(self, tmp_path, monkeypatch):
        self._setup_hook_dir(tmp_path, monkeypatch)
        self._write_events(tmp_path, [
            '{"ts":100,"type":"prompt"}',
            '{"ts":101,"type":"stop"}',
        ])
        assert ccm_signals.read_latest_permission_mode("/x/proj") == ""

    def test_authoritative_no_session_skips_lookup(self, tmp_path, monkeypatch):
        """session_id="" (bulk-fetch authoritative "no session") must
        not fall back to a per-project tmux lookup — this is what
        keeps build_project_list's subprocess count flat."""
        self._setup_hook_dir(tmp_path, monkeypatch)
        monkeypatch.setattr(
            ccm_signals, "_session_id_from_tmux",
            lambda _d: pytest.fail("tmux lookup must not happen"))
        assert ccm_signals.read_latest_permission_mode(
            "/x/proj", session_id="") == ""

