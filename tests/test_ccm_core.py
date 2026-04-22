"""Tests for ccm_core.py — state detection, helpers, and batch tmux commands."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

import pytest

# Add lib to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import ccm_core


# ─── Fixtures ───

@pytest.fixture(autouse=True)
def reset_state():
    """Reset any module-level state between tests."""
    yield


def _iso_ts(unix_ts):
    """Format a unix timestamp as the ISO 8601 string Claude Code writes
    into JSONL records (UTC, milliseconds, trailing Z)."""
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def write_jsonl(path, records):
    """Write a list of dict records as one JSON-per-line to `path`.
    `records` may include `system/away_summary`, `user`, `assistant`,
    etc. — whatever the test needs to simulate."""
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def real_activity_record(unix_ts, role="assistant"):
    """Build a minimal user/assistant JSONL record at the given ts."""
    return {"type": role, "timestamp": _iso_ts(unix_ts), "message": {"content": "x"}}


def system_record(unix_ts, subtype="away_summary"):
    """Build a minimal system metadata record (e.g. recap)."""
    return {"type": "system", "subtype": subtype, "timestamp": _iso_ts(unix_ts)}


def make_ps_lines(*entries):
    """Build ps output lines. Each entry: (pid, ppid, pgid, comm)."""
    lines = ["  PID  PPID  PGID COMM"]
    for pid, ppid, pgid, comm in entries:
        lines.append(f"  {pid}   {ppid}   {pgid} {comm}")
    return lines


# ─── find_claude_pid ───

class TestFindClaudePid:
    def test_finds_claude_child(self):
        ps = make_ps_lines((200, 100, 100, "claude"))
        assert ccm_core.find_claude_pid(100, ps) == "200"

    def test_returns_none_when_no_claude(self):
        ps = make_ps_lines((200, 100, 100, "bash"))
        assert ccm_core.find_claude_pid(100, ps) is None

    def test_ignores_claude_with_different_parent(self):
        ps = make_ps_lines((200, 999, 999, "claude"))
        assert ccm_core.find_claude_pid(100, ps) is None


# ─── has_children ───

class TestHasChildren:
    def test_true_when_child_exists(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 200, "node"))
        assert ccm_core.has_children("200", ps, "99999") is True

    def test_false_when_no_children(self):
        ps = make_ps_lines((200, 100, 100, "claude"))
        assert ccm_core.has_children("200", ps, "99999") is False

    def test_excludes_caffeinate(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 200, "caffeinate"))
        assert ccm_core.has_children("200", ps, "99999") is False

    def test_excludes_own_pgid(self):
        ps = make_ps_lines((200, 100, 100, "claude"), (300, 200, 12345, "node"))
        assert ccm_core.has_children("200", ps, "12345") is False

    def test_true_with_non_caffeinate_alongside_caffeinate(self):
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "caffeinate"),
            (400, 200, 200, "node"),
        )
        assert ccm_core.has_children("200", ps, "99999") is True


# ─── has_grandchildren ───

class TestHasGrandchildren:
    def test_false_when_only_direct_children(self):
        """MCP servers / language servers as direct children → no grandchildren."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "node"),               # MCP server
            (400, 200, 200, "sourcekit-lsp"),      # language server
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is False

    def test_true_when_bash_tool_running(self):
        """claude → bash → command (e.g. xcodebuild) — tool execution."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "bash"),
            (400, 300, 300, "xcodebuild"),
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is True

    def test_false_when_no_children(self):
        ps = make_ps_lines((200, 100, 100, "claude"))
        assert ccm_core.has_grandchildren("200", ps, "99999") is False

    def test_excludes_caffeinate_at_grandchild_level(self):
        """A bash child whose only grandchild is caffeinate is not a tool run."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "bash"),
            (400, 300, 300, "caffeinate"),
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is False

    def test_excludes_caffeinate_at_child_level(self):
        """caffeinate as a direct child is excluded from the children set,
        so its (hypothetical) own children do not count as claude grandchildren."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "caffeinate"),
            (400, 300, 300, "node"),
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is False

    def test_mixed_mcp_and_tool(self):
        """MCP server as direct child + bash → cmd as another branch → True."""
        ps = make_ps_lines(
            (200, 100, 100, "claude"),
            (300, 200, 200, "node"),               # MCP server (direct only)
            (400, 200, 200, "bash"),               # Bash tool
            (500, 400, 400, "xcodebuild"),         # tool subprocess
        )
        assert ccm_core.has_grandchildren("200", ps, "99999") is True


# ─── JSONL session log freshness ───

class TestJsonlFreshness:
    def setup_method(self):
        # Reset the in-process caches so tests do not leak across cases
        ccm_core._jsonl_path_cache.clear()
        ccm_core._jsonl_activity_cache.clear()

    def teardown_method(self):
        ccm_core._jsonl_path_cache.clear()
        ccm_core._jsonl_activity_cache.clear()

    def test_slug_simple(self):
        assert ccm_core._project_slug("/Users/yo/code/foo") == "-Users-yo-code-foo"

    def test_slug_with_tilde(self):
        # ~ should expand; trailing slash and structure preserved
        slug = ccm_core._project_slug("~/code/foo")
        home = os.path.expanduser("~")
        assert slug == (home + "/code/foo").replace("/", "-")

    def test_age_minus_one_when_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        assert ccm_core.read_jsonl_age("/nonexistent/path/foo") == -1

    def test_age_minus_one_when_no_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_core._project_slug("/x/y")
        (tmp_path / slug).mkdir()
        # empty dir
        assert ccm_core.read_jsonl_age("/x/y") == -1

    def test_age_reads_newest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_core._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        old = d / "old.jsonl"
        new = d / "new.jsonl"
        now = time.time()
        write_jsonl(old, [real_activity_record(now - 1000)])
        write_jsonl(new, [real_activity_record(now - 3)])
        os.utime(old, (now - 1000, now - 1000))
        os.utime(new, (now - 3, now - 3))
        age = ccm_core.read_jsonl_age("/x/y")
        assert 2 <= age <= 5  # newest record is ~3s old

    def test_ignores_non_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_core._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        (d / "foo.txt").write_text("ignored")
        (d / "bar.log").write_text("ignored")
        assert ccm_core.read_jsonl_age("/x/y") == -1

    def test_cache_returns_stable_path(self, tmp_path, monkeypatch):
        """Calling twice should not re-listdir if cache is hot.
        We assert by deleting the dir between calls — second call
        still returns the cached path's age (until path vanishes)."""
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_core._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        f = d / "session.jsonl"
        write_jsonl(f, [real_activity_record(time.time())])
        a1 = ccm_core.read_jsonl_age("/x/y")
        assert a1 >= 0
        # Cache hit on second call: same file, same age (within 1s)
        a2 = ccm_core.read_jsonl_age("/x/y")
        assert abs(a2 - a1) <= 1

    def test_cache_recovers_when_file_disappears(self, tmp_path, monkeypatch):
        """If the cached file is deleted, the next call must re-glob
        and either find a replacement or return -1."""
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_core._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        f = d / "session.jsonl"
        write_jsonl(f, [real_activity_record(time.time())])
        assert ccm_core.read_jsonl_age("/x/y") >= 0
        f.unlink()
        # No replacement → -1
        assert ccm_core.read_jsonl_age("/x/y") == -1


# ─── JSONL real-activity filter (recap fix Phase 1) ───
#
# These tests exercise read_jsonl_age()'s filtering of system metadata
# records (Claude Code v2.1.108+ recap, turn_duration, attachment, ...)
# so that recap and similar internal events do not register as fresh
# activity. See the recap interaction notes in CHANGELOG and CLAUDE.md.

class TestJsonlRealActivityFilter:
    def setup_method(self):
        ccm_core._jsonl_path_cache.clear()
        ccm_core._jsonl_activity_cache.clear()

    def teardown_method(self):
        ccm_core._jsonl_path_cache.clear()
        ccm_core._jsonl_activity_cache.clear()

    def _setup_project(self, tmp_path, monkeypatch, project_dir="/p/q"):
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_core._project_slug(project_dir)
        d = tmp_path / slug
        d.mkdir()
        return d / "session.jsonl"

    def test_returns_age_of_user_record(self, tmp_path, monkeypatch):
        """A JSONL whose tail is a single user record returns that
        record's age."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [real_activity_record(now - 4, role="user")])
        age = ccm_core.read_jsonl_age("/p/q")
        assert 3 <= age <= 6

    def test_skips_away_summary_recap(self, tmp_path, monkeypatch):
        """The recap (system/away_summary) record at the end of the
        file is skipped; the previous assistant record's timestamp is
        used. This is the core treefold scenario."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            real_activity_record(now - 200, role="assistant"),
            system_record(now - 1, subtype="away_summary"),
        ])
        age = ccm_core.read_jsonl_age("/p/q")
        # Should reflect the assistant record (~200s), not the recap (~1s)
        assert 195 <= age <= 210

    def test_skips_multiple_trailing_system_records(self, tmp_path, monkeypatch):
        """treefold actually had stop_hook_summary + turn_duration +
        away_summary all stacked at the tail before the assistant
        record. Walk past all of them."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            real_activity_record(now - 300, role="assistant"),
            system_record(now - 3, subtype="stop_hook_summary"),
            system_record(now - 2, subtype="turn_duration"),
            system_record(now - 1, subtype="away_summary"),
        ])
        age = ccm_core.read_jsonl_age("/p/q")
        assert 295 <= age <= 310

    def test_skips_attachment_records(self, tmp_path, monkeypatch):
        """`type: attachment` (e.g. task_reminder) is system metadata,
        not real activity."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            real_activity_record(now - 50, role="user"),
            {"type": "attachment", "timestamp": _iso_ts(now - 1),
             "attachment": {"type": "task_reminder", "content": []}},
        ])
        age = ccm_core.read_jsonl_age("/p/q")
        assert 45 <= age <= 60

    def test_returns_minus_one_when_only_system_records(self, tmp_path, monkeypatch):
        """If the entire tail is system metadata, return -1 — there is
        no real activity. JSONL-fresh rules will not match this."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            system_record(now - 5, subtype="stop_hook_summary"),
            system_record(now - 3, subtype="away_summary"),
        ])
        assert ccm_core.read_jsonl_age("/p/q") == -1

    def test_falls_back_to_mtime_when_real_activity_lacks_timestamp(
        self, tmp_path, monkeypatch
    ):
        """Real activity record found but its `timestamp` field is
        missing or unparseable → fall back to the file mtime so the
        rule engine still has a usable signal."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        # user record without a timestamp field
        write_jsonl(f, [{"type": "user", "message": {"content": "no ts"}}])
        os.utime(f, (now - 7, now - 7))
        age = ccm_core.read_jsonl_age("/p/q")
        assert 6 <= age <= 9

    def test_caches_by_mtime(self, tmp_path, monkeypatch):
        """Two calls with no file write between them should hit the
        cache. Verified by patching open() on the second call: cache
        miss would trigger a real file read; cache hit avoids it."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [real_activity_record(now - 2)])
        a1 = ccm_core.read_jsonl_age("/p/q")
        # Patch open to detect re-reads (the cache should prevent this).
        opens = []
        real_open = open
        def tracking_open(path, *a, **kw):
            opens.append(str(path))
            return real_open(path, *a, **kw)
        with patch("builtins.open", side_effect=tracking_open):
            a2 = ccm_core.read_jsonl_age("/p/q")
        assert abs(a2 - a1) <= 1
        # The JSONL file itself should NOT have been re-opened on the
        # second call (cache hit).
        assert str(f) not in opens

    def test_invalidates_cache_on_mtime_change(self, tmp_path, monkeypatch):
        """A new file write must invalidate the cache and re-parse."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [real_activity_record(now - 100)])
        a1 = ccm_core.read_jsonl_age("/p/q")
        assert 95 <= a1 <= 110
        # Append a fresh record. New mtime → cache invalidates.
        write_jsonl(f, [
            real_activity_record(now - 100),
            real_activity_record(now - 1),
        ])
        # Force a different mtime so the cache key changes.
        os.utime(f, (now, now))
        # Also clear the path cache so _find_newest_jsonl re-checks.
        ccm_core._jsonl_path_cache.clear()
        a2 = ccm_core.read_jsonl_age("/p/q")
        assert a2 <= 3

    def test_handles_malformed_json_lines(self, tmp_path, monkeypatch):
        """Garbage lines in the middle of the tail are skipped; the
        next valid real-activity record is found."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        valid = json.dumps(real_activity_record(now - 5))
        f.write_text(valid + "\nnot-json-at-all\n{also bad\n")
        age = ccm_core.read_jsonl_age("/p/q")
        # The valid record is at -5, garbage after it is skipped.
        assert 4 <= age <= 8


# ─── JSONL tail stop_reason extraction ───
#
# read_jsonl_tail_info returns (age, last_assistant_stop_reason). The
# stop_reason is what the new `jsonl_tool_use_pending` detection rule
# keys on to hold BUSY authoritatively across tool-turn boundaries,
# replacing the 15 s `jsonl_holds_busy` heuristic cliff.

def assistant_record(unix_ts, stop_reason=None):
    """Minimal assistant record with optional stop_reason inside `message`."""
    msg = {"content": "x"}
    if stop_reason is not None:
        msg["stop_reason"] = stop_reason
    return {
        "type": "assistant",
        "timestamp": _iso_ts(unix_ts),
        "message": msg,
    }


class TestJsonlTailStopReason:
    def setup_method(self):
        ccm_core._jsonl_path_cache.clear()
        ccm_core._jsonl_activity_cache.clear()

    def teardown_method(self):
        ccm_core._jsonl_path_cache.clear()
        ccm_core._jsonl_activity_cache.clear()

    def _setup_project(self, tmp_path, monkeypatch, project_dir="/p/q"):
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_core._project_slug(project_dir)
        d = tmp_path / slug
        d.mkdir()
        return d / "session.jsonl"

    def test_returns_tool_use_from_latest_assistant(self, tmp_path, monkeypatch):
        """The most recent assistant record has stop_reason='tool_use'
        (Claude paused for a tool result) → that is what we return."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            {"type": "user", "timestamp": _iso_ts(now - 10),
             "message": {"content": "x"}},
            assistant_record(now - 5, stop_reason="tool_use"),
        ])
        age, stop = ccm_core.read_jsonl_tail_info("/p/q")
        assert 4 <= age <= 7
        assert stop == "tool_use"

    def test_returns_end_turn_from_latest_assistant(self, tmp_path, monkeypatch):
        """Response completed normally: stop_reason='end_turn'."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 2, stop_reason="end_turn"),
        ])
        age, stop = ccm_core.read_jsonl_tail_info("/p/q")
        assert stop == "end_turn"

    def test_walks_past_tool_result_user_record_to_prior_assistant(
        self, tmp_path, monkeypatch
    ):
        """After a tool call the tail looks like:
            assistant stop_reason=tool_use   (Claude requested a tool)
            user (tool_result)               (tool finished, result injected)
        The newest real-activity record is the `user` tool_result, but
        the assistant stop_reason that describes the in-flight turn is
        one record back. We must return that assistant's stop_reason,
        not None.
        """
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 10, stop_reason="tool_use"),
            {"type": "user", "timestamp": _iso_ts(now - 2),
             "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        ])
        age, stop = ccm_core.read_jsonl_tail_info("/p/q")
        # age is of the newest real record (user tool_result)
        assert 1 <= age <= 4
        # stop_reason comes from the assistant one record back
        assert stop == "tool_use"

    def test_skips_system_records_for_stop_reason(self, tmp_path, monkeypatch):
        """System records (recap / stop_hook_summary) are filtered out
        even when walking for stop_reason. The assistant record buried
        beneath trailing system records still surfaces."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 300, stop_reason="tool_use"),
            system_record(now - 3, subtype="stop_hook_summary"),
            system_record(now - 2, subtype="turn_duration"),
            system_record(now - 1, subtype="away_summary"),
        ])
        age, stop = ccm_core.read_jsonl_tail_info("/p/q")
        assert stop == "tool_use"

    def test_returns_none_when_no_assistant_record(self, tmp_path, monkeypatch):
        """A fresh session with only a user prompt and no assistant
        reply yet: stop_reason is None."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            {"type": "user", "timestamp": _iso_ts(now - 2),
             "message": {"content": "hello"}},
        ])
        age, stop = ccm_core.read_jsonl_tail_info("/p/q")
        assert stop is None

    def test_returns_none_when_assistant_lacks_stop_reason(
        self, tmp_path, monkeypatch
    ):
        """Older schema or partial record without a stop_reason → None,
        not a falsy empty string or crash."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [assistant_record(now - 2, stop_reason=None)])
        age, stop = ccm_core.read_jsonl_tail_info("/p/q")
        assert stop is None

    def test_returns_none_when_jsonl_missing(self, tmp_path, monkeypatch):
        """No JSONL file at all: (-1, None)."""
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        age, stop = ccm_core.read_jsonl_tail_info("/nonexistent")
        assert age == -1
        assert stop is None

    def test_cache_shared_with_read_jsonl_age(self, tmp_path, monkeypatch):
        """read_jsonl_age and read_jsonl_tail_info share the same
        (path, mtime, size)-keyed cache. After a read_jsonl_age call
        a subsequent read_jsonl_tail_info call must not re-open the
        JSONL file — otherwise every scan pays the parse cost twice."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [assistant_record(now - 2, stop_reason="tool_use")])
        # Prime cache via the legacy accessor.
        ccm_core.read_jsonl_age("/p/q")
        opens = []
        real_open = open
        def tracking_open(path, *a, **kw):
            opens.append(str(path))
            return real_open(path, *a, **kw)
        with patch("builtins.open", side_effect=tracking_open):
            age, stop = ccm_core.read_jsonl_tail_info("/p/q")
        assert stop == "tool_use"
        assert str(f) not in opens, (
            "read_jsonl_tail_info re-opened JSONL despite cache being primed"
        )


# ─── hook_busy_idle gap discriminator (recap fix Phase 2) ───
#
# These tests exercise the new `hook_after_real_activity_lt` Rule
# field on the `hook_busy_idle` rule. The intent: trust BUSY hook only
# when it fired within JSONL_HOOK_GAP_TOLERANCE seconds of (or after)
# the last real conversation activity. This rejects phantom BUSY hooks
# fired by Claude Code v2.1.108+ recap (`away_summary`) which write
# to JSONL and fire a BUSY hook with no surrounding real activity.

class TestHookBusyIdleGapDiscriminator:
    def test_genuine_long_thinking_holds_busy(self):
        """Long-thinking phase: hook and real activity age TOGETHER.
        At any age, the gap stays ~0 < 60 → rule matches → BUSY."""
        for age in (5, 30, 60, 120, 300, 599):
            rule, state = ccm_core.evaluate_rules(
                make_ctx(raw="IDLE", hook_state="BUSY",
                         hook_age=age, jsonl_age=age)
            )
            assert (rule.name, state) == ("hook_busy_idle", "BUSY"), (
                f"hook=jsonl={age} should still match hook_busy_idle"
            )

    def test_recap_phantom_hook_releases_busy(self):
        """The treefold scenario: hook just fired (recap), but the last
        real activity is several minutes old. Gap is large → rule does
        NOT match → fall through → release."""
        rule, state = ccm_core.evaluate_rules(
            # Mirror the screenshot timing: BUSY hook fired ~6 minutes
            # ago (recap), real activity is ~9.5 minutes ago.
            make_ctx(raw="IDLE", hook_state="BUSY",
                     hook_age=380, jsonl_age=569)
        )
        assert rule.name != "hook_busy_idle"

    def test_recap_phantom_hook_immediately_released(self):
        """Right at the moment recap fires: hook age 0, real activity
        from minutes ago. Gap is the entire idle interval → release."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY",
                     hook_age=1, jsonl_age=180)
        )
        assert rule.name != "hook_busy_idle"

    def test_busy_within_tolerance_holds(self):
        """real_activity slightly older than hook (within tolerance)
        is still treated as genuine."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY",
                     hook_age=5, jsonl_age=64)  # gap = 59 < 60
        )
        assert (rule.name, state) == ("hook_busy_idle", "BUSY")

    def test_busy_just_past_tolerance_releases(self):
        """One second past the tolerance → rule does not match."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY",
                     hook_age=5, jsonl_age=66)  # gap = 61 >= 60
        )
        assert rule.name != "hook_busy_idle"

    def test_real_activity_newer_than_hook_holds(self):
        """The classic case: a tool turn just produced an assistant
        record AFTER the BUSY hook fired. real_activity_age <
        hook_age → gap is negative → rule matches."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY",
                     hook_age=300, jsonl_age=5)
        )
        assert (rule.name, state) == ("hook_busy_idle", "BUSY")

    def test_no_jsonl_path_routes_to_no_jsonl_rule(self):
        """jsonl_age=-1 (no JSONL file) must NOT cause this rule to
        match — the gap check requires jsonl_age >= 0. Falls through
        to hook_busy_idle_no_jsonl instead."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY",
                     hook_age=5, jsonl_age=-1)
        )
        assert rule.name == "hook_busy_idle_no_jsonl"
        assert state == "BUSY"

    def test_fresh_busy_no_jsonl_falls_through(self):
        """hook_fresh_busy now also requires jsonl_age >= 0 (the gap
        discriminator can't run without it). When JSONL is missing,
        the fall-through hits hook_busy_idle_no_jsonl which still
        produces BUSY for any age — preserving the no-JSONL safety
        net for older Claude Code or test environments."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY",
                     hook_age=0, jsonl_age=-1)
        )
        # Not hook_fresh_busy anymore (gap check needs JSONL), but
        # state is still BUSY via the no-jsonl fallback rule.
        assert rule.name == "hook_busy_idle_no_jsonl"
        assert state == "BUSY"


# ─── hooks.log canary ───

class TestHooksLogWarning:
    def test_no_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_HOOKS_LOG", str(tmp_path / "missing.log"))
        assert ccm_core.hooks_log_size() == -1
        assert ccm_core.hooks_log_warning() == ""

    def test_small_file_returns_empty(self, tmp_path, monkeypatch):
        log = tmp_path / "hooks.log"
        log.write_text("a" * 1024)  # 1 KB
        monkeypatch.setattr(ccm_core, "CLAUDE_HOOKS_LOG", str(log))
        assert ccm_core.hooks_log_warning() == ""

    def test_bloated_file_returns_warning(self, tmp_path, monkeypatch):
        log = tmp_path / "hooks.log"
        log.write_text("x")  # tiny file
        monkeypatch.setattr(ccm_core, "CLAUDE_HOOKS_LOG", str(log))
        # Lower threshold so the tiny file qualifies
        monkeypatch.setattr(ccm_core, "HOOKS_LOG_WARN_BYTES", 0)
        msg = ccm_core.hooks_log_warning()
        assert "hooks.log" in msg
        assert "#16047" in msg
        assert ": > ~/.claude/hooks.log" in msg


# ─── disableAllHooks canary ───

class TestDisableAllHooksWarning:
    def test_no_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(tmp_path / "missing.json"))
        assert ccm_core.disable_all_hooks_warning() == ""

    def test_setting_absent(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"other": "value"}')
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_core.disable_all_hooks_warning() == ""

    def test_setting_false(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"disableAllHooks": false}')
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_core.disable_all_hooks_warning() == ""

    def test_setting_true_returns_warning(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"disableAllHooks": true}')
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(f))
        msg = ccm_core.disable_all_hooks_warning()
        assert "disableAllHooks" in msg
        assert "settings.json" in msg
        # v2.1.108 docs clarified that disableAllHooks also kills the
        # custom statusLine. The warning must tell the user so they
        # can correlate a missing embedded statusLine with this flag.
        assert "statusLine" in msg

    def test_malformed_json(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text("not json")
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_core.disable_all_hooks_warning() == ""


# ─── allowManagedHooksOnly canary (Claude Code v2.1.107) ───

class TestManagedHooksOnlyWarning:
    def test_no_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(tmp_path / "missing.json"))
        assert ccm_core.managed_hooks_only_warning() == ""

    def test_setting_absent(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"other": "value"}')
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_core.managed_hooks_only_warning() == ""

    def test_setting_false(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"allowManagedHooksOnly": false}')
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_core.managed_hooks_only_warning() == ""

    def test_setting_true_returns_warning(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"allowManagedHooksOnly": true}')
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(f))
        msg = ccm_core.managed_hooks_only_warning()
        assert "allowManagedHooksOnly" in msg
        assert "user-scope hooks" in msg

    def test_independent_from_disable_all_hooks(self, tmp_path, monkeypatch):
        """Both canaries can fire independently or together."""
        f = tmp_path / "settings.json"
        f.write_text('{"allowManagedHooksOnly": true, "disableAllHooks": true}')
        monkeypatch.setattr(ccm_core, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_core.managed_hooks_only_warning() != ""
        assert ccm_core.disable_all_hooks_warning() != ""


# ─── Runtime session info (~/.claude/sessions/<pid>.json) ───

class TestReadSessionInfo:
    def test_none_when_no_pid(self):
        assert ccm_core.read_session_info("") is None
        assert ccm_core.read_session_info(None) is None

    def test_reads_session_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "12345.json").write_text(
            '{"pid":12345,"sessionId":"abc-def","cwd":"/tmp/proj",'
            '"startedAt":1776048000000,"kind":"interactive","entrypoint":"cli"}'
        )
        info = ccm_core.read_session_info("12345")
        assert info["sessionId"] == "abc-def"
        assert info["cwd"] == "/tmp/proj"
        assert info["kind"] == "interactive"

    def test_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        assert ccm_core.read_session_info("99999") is None

    def test_none_on_malformed_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "1.json").write_text("not json")
        assert ccm_core.read_session_info("1") is None


class TestJsonlFromSessionInfo:
    def test_resolves_exact_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_SESSIONS_DIR", str(tmp_path / "sessions"))
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "500.json").write_text(
            '{"pid":500,"sessionId":"s-1","cwd":"/x/y","kind":"interactive"}'
        )
        slug_dir = tmp_path / "projects" / "-x-y"
        slug_dir.mkdir(parents=True)
        expected = slug_dir / "s-1.jsonl"
        expected.write_text("{}\n")

        path = ccm_core._jsonl_from_session_info("500")
        assert path == str(expected)

    def test_returns_none_for_headless_session(self, tmp_path, monkeypatch):
        """kind='cli' (headless -p mode) should be skipped — ccm tracks
        only interactive sessions."""
        monkeypatch.setattr(ccm_core, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "600.json").write_text(
            '{"pid":600,"sessionId":"s-2","cwd":"/a/b","kind":"cli"}'
        )
        assert ccm_core._jsonl_from_session_info("600") is None

    def test_returns_none_when_jsonl_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CLAUDE_SESSIONS_DIR", str(tmp_path / "sessions"))
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "700.json").write_text(
            '{"pid":700,"sessionId":"s-3","cwd":"/p/q","kind":"interactive"}'
        )
        # projects dir empty — no matching jsonl
        assert ccm_core._jsonl_from_session_info("700") is None

    def test_age_uses_session_info_path(self, tmp_path, monkeypatch):
        """read_jsonl_age prefers the session-info resolution when
        claude_pid is provided, even if the slug-based lookup would
        find a different newest file."""
        monkeypatch.setattr(ccm_core, "CLAUDE_SESSIONS_DIR", str(tmp_path / "sessions"))
        monkeypatch.setattr(ccm_core, "CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
        ccm_core._jsonl_path_cache.clear()
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "800.json").write_text(
            '{"pid":800,"sessionId":"active","cwd":"/w/x","kind":"interactive"}'
        )
        slug_dir = tmp_path / "projects" / "-w-x"
        slug_dir.mkdir(parents=True)
        active = slug_dir / "active.jsonl"
        other = slug_dir / "other.jsonl"
        now = time.time()
        write_jsonl(active, [real_activity_record(now - 10)])
        write_jsonl(other, [real_activity_record(now - 1)])
        os.utime(active, (now - 10, now - 10))
        os.utime(other, (now - 1, now - 1))  # "newer" by mtime but wrong session

        # With pid: picks active.jsonl, age ≈10
        age = ccm_core.read_jsonl_age("/w/x", claude_pid="800")
        assert 9 <= age <= 12

        ccm_core._jsonl_path_cache.clear()
        ccm_core._jsonl_activity_cache.clear()
        # Without pid: slug-based scan picks the newest by mtime (other.jsonl)
        age2 = ccm_core.read_jsonl_age("/w/x")
        assert age2 <= 2


# ─── detect_pane_state ───

class TestDetectPaneState:
    @patch("ccm_core.tmux_cmd")
    def test_shell_when_no_claude(self, mock_tmux):
        ps = make_ps_lines((100, 1, 100, "bash"))
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "SHELL"

    @patch("ccm_core.tmux_cmd")
    def test_idle_when_no_children(self, mock_tmux):
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_no_prompt(self, mock_tmux):
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Processing files...\nRunning tests..."
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_permit_text_ignored(self, mock_tmux):
        """Generic 'Do you want to allow this?' text without the v2.1+ footer
        markers does NOT trigger PERMIT — only 'Tab to amend' / 'ctrl+e to explain'
        do. Children + ordinary text still resolves to BUSY."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Do you want to allow this?\n  Yes    No"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_idle_with_children_and_input_prompt(self, mock_tmux):
        """Background workers (MCP servers) + visible prompt = IDLE."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Some output\n❯ "
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_and_accept_edits_prompt(self, mock_tmux):
        """Accept-edits prompt (❯❯) should NOT be treated as idle."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running tests...\n❯❯ accept edits on"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_busy_with_children_and_new_accept_edits_prompt(self, mock_tmux):
        """Accept-edits prompt (⏵⏵) with leading spaces should NOT be treated as idle."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running tests...\n  ⏵⏵ accept edits on (shift+tab to cycle)"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_footer_tab_to_amend(self, mock_tmux):
        """Permission dialog footer 'Tab to amend' → PERMIT (hook-independent).

        Fallback for when Claude Code stops firing PermissionRequest hooks
        mid-session (anthropics/claude-code#16047).
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "Do you want to proceed?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "\n"
            "Esc to cancel · Tab to amend · ctrl+e to explain"
        )
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_from_footer_indented(self, mock_tmux):
        """Footer with leading whitespace still matches."""
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = "  Esc to cancel · Tab to amend · ctrl+e to explain"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_permit_detected_even_with_children(self, mock_tmux):
        """PERMIT footer during parallel tool execution overrides BUSY."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "Running...\nEsc to cancel · Tab to amend · ctrl+e to explain"
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_slash_menu_footer(self, mock_tmux):
        """'Enter to confirm · Esc to cancel' (slash menu) is NOT permission."""
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = "Choose a model\n❯ Opus\n  Sonnet\nEnter to confirm · Esc to cancel"
        # No "Esc to cancel · Tab to amend" prefix → falls through to IDLE
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_response_body_mentioning_footer(self, mock_tmux):
        """Claude response body containing 'ctrl+e to explain' in prose
        must not false-trigger PERMIT. Pattern is anchored to the start
        of a line with the 'Esc to cancel · ' prefix; in-body mentions
        always have leading text before the phrase.
        """
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        mock_tmux.return_value = (
            "⏺ In permit dialogs you can use ctrl+e to explain the\n"
            "  command, or Tab to amend it before approving.\n"
            "❯ "
        )
        # Input prompt visible, no footer → IDLE
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_no_permit_from_inline_mention(self, mock_tmux):
        """Even a line that ENDS with 'ctrl+e to explain' but has other
        text first (e.g. a quoted example) should not match."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node")
        )
        mock_tmux.return_value = "  The footer says: Esc to cancel · Tab to amend · ctrl+e to explain"
        # Has indentation but the "The footer says:" prefix breaks the anchor
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "BUSY"

    @patch("ccm_core.tmux_cmd")
    def test_idle_when_grandchild_exists_but_prompt_visible(self, mock_tmux):
        """Leftover server (claude → zsh → ruby) + visible `❯ ` prompt
        → IDLE at the pane level. The v2.1+ case where `❯ ` appears
        above a STILL-ACTIVE tool is handled at the window level by
        hook_busy_idle / jsonl_fresh_activity, not here."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"),
            (200, 100, 100, "claude"),
            (300, 200, 200, "/bin/zsh"),            # leftover shell
            (400, 300, 300, "ruby"),                # dev server
        )
        mock_tmux.return_value = (
            "Task completed.\n"
            "─────\n"
            "❯ \n"
            "─────\n"
            "  ⏵⏵ accept edits on (shift+tab to cycle)"
        )
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"

    @patch("ccm_core.tmux_cmd")
    def test_idle_when_only_mcp_children_and_prompt_visible(self, mock_tmux):
        """No grandchildren (only MCP/LSP direct children) + visible prompt → IDLE.
        Regression guard: the new grandchild path must not break the
        established 'background workers + ❯ = IDLE' rule."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"),
            (200, 100, 100, "claude"),
            (300, 200, 200, "node"),               # MCP server
            (400, 200, 200, "sourcekit-lsp"),      # LSP
        )
        mock_tmux.return_value = "Some output\n❯ "
        assert ccm_core.detect_pane_state("100", "%0", ps, "99999") == "IDLE"


# ─── detect_window_raw ───

class TestDetectWindowRaw:
    def test_down_when_no_panes(self):
        assert ccm_core.detect_window_raw("0:1", [], [], "99999") == "DOWN"

    @patch("ccm_core.tmux_cmd")
    def test_busy_takes_priority_over_idle(self, mock_tmux):
        """PERMIT is hook-only; pane with children = BUSY, takes priority over IDLE."""
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 200, "node"),
            (101, 1, 101, "bash"), (201, 101, 101, "claude"),
        )
        mock_tmux.return_value = "Processing..."
        panes = [("0:1", "100", "%0"), ("0:1", "101", "%1")]
        assert ccm_core.detect_window_raw("0:1", panes, ps, "99999") == "BUSY"


# ─── detect_window_state with hooks ───

class TestDetectWindowStateHooks:
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_idle_plus_hook_busy_returns_busy(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=BUSY → BUSY (text generation)."""
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_hook_permit_with_raw_busy(self, mock_hook, mock_tmux):
        """raw=BUSY + hook=PERMIT → PERMIT.

        During permission dialog, process tree reports BUSY (background
        MCP servers etc.) and input prompt is not visible. PERMIT overrides.
        """
        hook_ts = int(time.time())
        mock_hook.return_value = (hook_ts, "PERMIT", "")
        mock_tmux.return_value = ""  # capture-pane: no input prompt visible
        # claude (200) has child process (300) → raw=BUSY
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"),
                           (300, 200, 100, "node"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_raw_permit_overrides_stale_busy_hook(self, mock_hook, mock_tmux):
        """capture-pane detects PERMIT footer + stale BUSY hook → PERMIT.

        End-to-end scenario for anthropics/claude-code#16047: Claude Code
        stopped firing PermissionRequest mid-session, so the hook signal
        is stuck on stale BUSY. The capture-pane fallback must win.
        """
        hook_ts = int(time.time()) - 600  # 10 min stale
        mock_hook.return_value = (hook_ts, "BUSY", "")
        mock_tmux.return_value = (
            "❯ 1. Yes\n  2. No\n"
            "Esc to cancel · Tab to amend · ctrl+e to explain"
        )
        # claude with no child (permission dialog pre-tool-spawn)
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_raw_permit_without_any_hook(self, mock_hook, mock_tmux):
        """capture-pane PERMIT + no hook signal at all → PERMIT."""
        mock_hook.return_value = None
        mock_tmux.return_value = (
            "  Esc to cancel · Tab to amend · ctrl+e to explain"
        )
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_permit_persists_when_raw_idle(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=PERMIT + prev=PERMIT → still PERMIT.

        After user responds to permission dialog, there's a brief IDLE gap
        before the tool subprocess starts. The fallback must NOT convert
        this to IDLE — keep PERMIT until a hook signal (BUSY) arrives.
        """
        hook_ts = int(time.time()) - 3
        mock_hook.return_value = (hook_ts, "PERMIT", "")
        mock_tmux.return_value = ""
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "PERMIT", panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_hook_busy_stays_busy_even_with_permit_text(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=BUSY + generic permission text (no footer marker) → BUSY.

        The v2.1+ capture-pane PERMIT fallback only triggers on 'Tab to amend'
        or 'ctrl+e to explain' — plain 'Do you want to proceed?' text still
        defers to the hook signal.
        """
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        mock_tmux.return_value = "Do you want to proceed?\n  1. Yes\n  2. No"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_shell_ignores_hooks(self, mock_hook, mock_tmux):
        """raw=SHELL → SHELL regardless of hook signals."""
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        ps = make_ps_lines((100, 1, 100, "bash"))  # No claude
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "SHELL"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_accept_edits_without_children_returns_idle(self, mock_hook, mock_tmux):
        """Safety net: ⏵⏵ visible, no children → IDLE (waiting for user action)."""
        mock_hook.return_value = None  # No hook signal (expired)
        mock_tmux.return_value = "Some output\n❯ \n  ⏵⏵ accept edits on (shift+tab to cycle)"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_no_prompt_no_hook_returns_idle_not_busy(self, mock_hook, mock_tmux):
        """Safety net removed: no prompt, no hook → IDLE (trust process tree)."""
        mock_hook.return_value = None  # No hook signal
        mock_tmux.return_value = "Some tool output without prompt"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_session_end_hook_ignored_when_idle(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=SHELL → IDLE (process tree authoritative; stale SHELL signal ignored)."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_session_end_hook_with_shell_raw(self, mock_hook, mock_tmux):
        """raw=SHELL + hook=SHELL → SHELL (consistent)."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"))  # no claude process
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        assert state == "SHELL"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_session_end_hook_ignored_when_busy(self, mock_hook, mock_tmux):
        """raw=BUSY + hook=SHELL should not happen in practice, but raw=BUSY takes priority."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 100, "node"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        # raw=BUSY (children running), SHELL hook is ignored since condition is raw in ("SHELL", "IDLE")
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_no_hook_no_capture_permit(self, mock_hook, mock_tmux):
        """Without hook signal, PERMIT text on screen does NOT trigger PERMIT (hook-only detection)."""
        mock_hook.return_value = None
        mock_tmux.return_value = "Do you want to allow this?\n  Yes  No"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_permit_overrides_raw_busy(self, mock_hook, mock_tmux):
        """raw=BUSY + hook=PERMIT → PERMIT (background processes don't mask permission prompt)."""
        hook_ts = int(time.time())
        mock_hook.return_value = (hook_ts, "PERMIT", "")
        # window_activity is older (no user response yet)
        mock_tmux.return_value = str(hook_ts - 5)
        # Claude has child processes → raw would be BUSY
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"),
            (300, 200, 200, "node"),  # MCP server or other child
        )
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_permit_expires_after_max_timeout(self, mock_hook, mock_tmux):
        """Stale PERMIT signal (older than PERMIT_MAX_TIMEOUT) is ignored in hook path.

        When PERMIT expires, the hook check falls through. With prev_state=PERMIT,
        the fallback keeps PERMIT until a new hook signal arrives. But with
        prev_state=IDLE (no prior PERMIT), it would stay IDLE.
        """
        old_ts = int(time.time()) - ccm_core.PERMIT_MAX_TIMEOUT - 10
        mock_hook.return_value = (old_ts, "PERMIT", "")
        mock_tmux.return_value = str(old_ts - 5)
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        # With prev_state=IDLE: expired PERMIT doesn't resurrect
        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.read_hook_signal")
    def test_busy_signal_clears_stale_permit(self, mock_hook, mock_tmux):
        """prev_state=PERMIT + hook=BUSY → BUSY (new signal clears old PERMIT)."""
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0")]

        state = ccm_core.detect_window_state(
            "0:1", "/tmp/project", "PERMIT", panes, ps, "99999"
        )
        assert state == "BUSY"



# ─── Declarative rule evaluation (pure) ───


def make_ctx(**overrides):
    """Build a DetectionContext with sensible defaults for rule testing."""
    now = int(time.time())
    defaults = dict(
        raw="IDLE",
        hook_state="",
        hook_ts=0,
        hook_age=-1,
        prev_state="IDLE",
        jsonl_age=-1,
        now=now,
    )
    defaults.update(overrides)
    return ccm_core.DetectionContext(**defaults)


class TestFourStateModel:
    """Tests defining the target 4-state detection model (PERMIT/BUSY/IDLE/SHELL).
    DONE is no longer a detection state. These tests must pass after the refactor."""

    def test_stop_hook_clears_busy_signal(self):
        """After Stop fires and deletes the signal file, hook_state becomes
        empty. With fresh JSONL (< 5s), jsonl_fresh_activity keeps BUSY
        briefly. After JSONL ages, state transitions to IDLE."""
        # hook_state="" (signal deleted by Stop), raw=IDLE, jsonl=2 (fresh)
        # -> jsonl_fresh_activity -> BUSY (bridging window)
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="", jsonl_age=2)
        )
        assert (rule.name, state) == ("jsonl_fresh_activity", "BUSY")

        # hook_state="", raw=IDLE, jsonl=10 (stale)
        # -> default -> IDLE
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="", jsonl_age=10)
        )
        assert state == "IDLE"

    def test_multi_turn_natural_bridge(self):
        """Between tool calls, Stop fires (deletes signal) but JSONL is
        fresh from the tool result. jsonl_fresh_activity bridges the gap.
        Next PreToolUse fires BUSY hook immediately."""
        # Tool 1 complete: hook="", jsonl=1 (tool result just written)
        # -> jsonl_fresh_activity -> BUSY
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="", jsonl_age=1, prev_state="BUSY")
        )
        assert (rule.name, state) == ("jsonl_fresh_activity", "BUSY")

        # Next tool starts: hook=BUSY, jsonl=0
        # -> hook_fresh_busy -> BUSY
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY", hook_age=0, jsonl_age=0,
                     prev_state="BUSY")
        )
        assert (rule.name, state) == ("hook_fresh_busy", "BUSY")

    def test_busy_to_idle_direct_transition(self):
        """When Claude finishes and JSONL ages past threshold, BUSY
        transitions directly to IDLE without passing through DONE."""
        # prev=BUSY, raw=IDLE, hook="", jsonl past JSONL_ACTIVE_THRESHOLD
        # -> fallback_busy_to_idle -> IDLE (not "DONE")
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="BUSY", hook_state="",
                     jsonl_age=ccm_core.JSONL_ACTIVE_THRESHOLD + 10)
        )
        assert (rule.name, state) == ("fallback_busy_to_idle", "IDLE")
        assert state != "DONE"

    def test_no_done_in_state_priority(self):
        """DONE should not appear in STATE_PRIORITY or STATE_ICONS."""
        assert "DONE" not in ccm_core.STATE_PRIORITY
        assert "DONE" not in ccm_core.STATE_ICONS

    def test_no_done_in_valid_hook_states(self):
        """DONE is no longer a valid hook state (Stop hook deletes
        the signal file rather than writing DONE)."""
        assert "DONE" not in ccm_core.VALID_HOOK_STATES

    def test_completed_at_set_on_busy_to_idle(self):
        """apply_actions sets @ccm_completed_at when transitioning
        from BUSY to IDLE."""
        rule = ccm_core.Rule(name="t", result="IDLE", action=ccm_core.Action.DEFAULT)
        ctx = make_ctx(prev_state="BUSY")
        with patch.object(ccm_core, "_set_win_state") as mock_set:
            with patch.object(ccm_core, "tmux_cmd") as mock_tmux:
                ccm_core.apply_actions("0:1", "/tmp/proj", ctx, rule, "IDLE")
        # Should have set @ccm_completed_at
        completed_calls = [c for c in mock_tmux.call_args_list
                           if len(c[0]) > 3 and "@ccm_completed_at" in str(c[0])]
        assert len(completed_calls) > 0

    def test_completed_at_set_on_permit_to_idle(self):
        """PERMIT -> IDLE also sets the completion marker."""
        rule = ccm_core.Rule(name="t", result="IDLE", action=ccm_core.Action.DEFAULT)
        ctx = make_ctx(prev_state="PERMIT")
        with patch.object(ccm_core, "_set_win_state") as mock_set:
            with patch.object(ccm_core, "tmux_cmd") as mock_tmux:
                ccm_core.apply_actions("0:1", "/tmp/proj", ctx, rule, "IDLE")
        completed_calls = [c for c in mock_tmux.call_args_list
                           if len(c[0]) > 3 and "@ccm_completed_at" in str(c[0])]
        assert len(completed_calls) > 0

    def test_completed_at_not_set_on_idle_to_idle(self):
        """IDLE -> IDLE does NOT set the marker (no transition)."""
        rule = ccm_core.Rule(name="t", result="IDLE", action=ccm_core.Action.DEFAULT)
        ctx = make_ctx(prev_state="IDLE")
        with patch.object(ccm_core, "_set_win_state") as mock_set:
            with patch.object(ccm_core, "tmux_cmd") as mock_tmux:
                ccm_core.apply_actions("0:1", "/tmp/proj", ctx, rule, "IDLE")
        completed_calls = [c for c in mock_tmux.call_args_list
                           if len(c[0]) > 3 and "@ccm_completed_at" in str(c[0])]
        assert len(completed_calls) == 0

    def test_permit_lifecycle_without_done(self):
        """PERMIT -> (user approves) -> BUSY (tool runs) -> IDLE.
        No DONE intermediate."""
        # PERMIT dialog visible
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="BUSY", prev_state="BUSY", hook_state="PERMIT", hook_age=0)
        )
        assert state == "PERMIT"
        # User approves; brief IDLE gap
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="PERMIT", hook_state="PERMIT", hook_age=6)
        )
        assert state == "PERMIT"  # held by fallback_permit_hold
        # Tool runs: fresh BUSY hook
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="PERMIT", hook_state="BUSY",
                     hook_age=0, jsonl_age=0)
        )
        assert state == "BUSY"
        # Tool finishes, JSONL ages past active threshold
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="BUSY", hook_state="",
                     jsonl_age=ccm_core.JSONL_ACTIVE_THRESHOLD + 10)
        )
        assert state == "IDLE"
        assert state != "DONE"


class TestEvaluateRules:
    """Pure unit tests: each case asserts (matched_rule_name, resolved_state).

    No tmux, ps, or filesystem mocking — the Context is built directly.
    """

    # --- process-level ---

    def test_raw_down(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="DOWN"))
        assert (rule.name, state) == ("process_down", "DOWN")

    def test_raw_shell(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="SHELL"))
        assert (rule.name, state) == ("process_shell", "SHELL")

    def test_shell_beats_hook_busy(self):
        """Process tree authoritative: SHELL beats any hook signal."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="SHELL", hook_state="BUSY", hook_age=0)
        )
        assert (rule.name, state) == ("process_shell", "SHELL")

    # --- fresh BUSY hook fast path ---

    def test_hook_fresh_busy_over_raw_idle(self):
        # Real UserPromptSubmit timing: hook fires and the user record
        # is written to JSONL essentially simultaneously, so the gap
        # between hook and last real activity is ~0.
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY", hook_age=1, jsonl_age=0)
        )
        assert (rule.name, state) == ("hook_fresh_busy", "BUSY")

    def test_hook_fresh_busy_over_raw_busy_is_noop(self):
        # Same realistic setup: gap=0 between hook and JSONL.
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="BUSY", hook_state="BUSY", hook_age=0, jsonl_age=0)
        )
        assert (rule.name, state) == ("hook_fresh_busy", "BUSY")

    def test_hook_stale_busy_slow_path(self):
        """Age >= 2 → slow path rule (hook_busy_idle).

        Requires a fresh JSONL so the staleness guard does not
        release the rule — mirrors a real session mid-activity.
        """
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY", hook_age=5, jsonl_age=3)
        )
        assert (rule.name, state) == ("hook_busy_idle", "BUSY")

    def test_hook_busy_trusted_when_jsonl_keeps_up(self):
        """Long-running tool / text generation: BUSY hook stays trusted
        as long as JSONL is also writing records — which Claude Code
        does continuously across tool turn boundaries.

        Regression guard: previously HOOK_TIMEOUT=300 capped this rule,
        causing fallback_busy_to_idle to fire false IDLE on long tasks.
        Now the rule is capped by BUSY_HOOK_JSONL_WINDOW — but with
        JSONL kept fresh by ongoing tool activity, the effective hook
        age is irrelevant.
        """
        for age in (60, 299, 400, 900, 3600, 86400):
            rule, state = ccm_core.evaluate_rules(
                make_ctx(raw="IDLE", hook_state="BUSY", hook_age=age,
                         prev_state="BUSY", jsonl_age=5)
            )
            assert (rule.name, state) == ("hook_busy_idle", "BUSY"), (
                f"age={age} should still match hook_busy_idle"
            )

    def test_hook_busy_no_jsonl_trusted_regardless_of_age(self):
        """When JSONL is absent entirely (edge case) there is no
        counterevidence — the BUSY hook is trusted indefinitely via
        the separate `hook_busy_idle_no_jsonl` rule."""
        for age in (60, 299, 400, 900, 3600, 86400):
            rule, state = ccm_core.evaluate_rules(
                make_ctx(raw="IDLE", hook_state="BUSY", hook_age=age,
                         prev_state="BUSY", jsonl_age=-1)
            )
            assert (rule.name, state) == ("hook_busy_idle_no_jsonl", "BUSY"), (
                f"age={age} should match hook_busy_idle_no_jsonl"
            )

    # --- PERMIT ---

    def test_hook_permit_blocking_raw_busy(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="BUSY", hook_state="PERMIT", hook_age=3)
        )
        assert (rule.name, state) == ("hook_permit_blocking", "PERMIT")

    def test_hook_permit_idle_falls_through_to_fallback(self):
        """raw=IDLE means user moved past dialog; don't force PERMIT via rule 4."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="PERMIT", hook_age=3, prev_state="PERMIT")
        )
        assert rule.name == "fallback_permit_hold"
        assert state == "PERMIT"

    def test_hook_permit_expired_no_hold(self):
        """Expired PERMIT + prev=IDLE → default rule, state=IDLE."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="PERMIT",
                hook_age=ccm_core.PERMIT_MAX_TIMEOUT + 10,
                prev_state="IDLE",
            )
        )
        assert rule.name == "default"
        assert state == "IDLE"

    # --- JSONL session-log signal ---

    def test_jsonl_fresh_overrides_idle_with_no_hook(self):
        """raw=IDLE + no hook + JSONL fresh → BUSY (turn boundary just happened)."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="BUSY", jsonl_age=2)
        )
        assert (rule.name, state) == ("jsonl_fresh_activity", "BUSY")

    def test_jsonl_stale_does_not_fire(self):
        """JSONL stale → rule does not match, fallback wins."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="IDLE", jsonl_age=120)
        )
        assert rule.name == "default"

    def test_jsonl_missing_does_not_fire(self):
        """No JSONL file → rule does not match."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="BUSY", jsonl_age=-1)
        )
        assert rule.name == "fallback_busy_to_idle"

    def test_jsonl_fresh_overrides_no_hook(self):
        """JSONL fresh + no hook signal → BUSY (turn boundary just happened)."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE", hook_state="", hook_age=-1,
                prev_state="IDLE", jsonl_age=1,
            )
        )
        assert (rule.name, state) == ("jsonl_fresh_activity", "BUSY")

    def test_jsonl_does_not_override_hook_busy(self):
        """hook=BUSY already wins via hook_busy_idle; JSONL has nothing to add."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE", hook_state="BUSY", hook_age=10,
                prev_state="BUSY", jsonl_age=1,
            )
        )
        assert (rule.name, state) == ("hook_busy_idle", "BUSY")

    def test_jsonl_only_overrides_when_raw_idle(self):
        """If raw is already BUSY/PERMIT, the JSONL rule must not interfere."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="BUSY", jsonl_age=1)
        )
        assert rule.name == "raw_not_idle"
        assert state == "BUSY"

    def test_jsonl_holds_busy_through_thinking_gap(self):
        """Short post-JSONL-fresh hold: prev=BUSY + JSONL within
        JSONL_ACTIVE_THRESHOLD but past JSONL_FRESH_THRESHOLD.
        Bridges the couple of seconds between "JSONL fresh" and
        the final IDLE transition so we don't flash IDLE right
        after a session finishes streaming.
        """
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                prev_state="BUSY",
                jsonl_age=10,  # past the 5s fresh threshold, within 15s hold window
            )
        )
        assert (rule.name, state) == ("jsonl_holds_busy", "BUSY")

    def test_jsonl_hold_stops_at_threshold(self):
        """Beyond JSONL_ACTIVE_THRESHOLD, the hold rule releases and
        fallback_busy_to_idle fires."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                prev_state="BUSY",
                jsonl_age=ccm_core.JSONL_ACTIVE_THRESHOLD + 10,
            )
        )
        assert (rule.name, state) == ("fallback_busy_to_idle", "IDLE")

    def test_jsonl_hold_requires_prev_busy(self):
        """The hold is scoped to BUSY continuation only — it should not
        promote an IDLE session to BUSY just because a record was
        written a minute ago."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="IDLE", jsonl_age=60)
        )
        # Should NOT match jsonl_holds_busy (prev != BUSY)
        assert rule.name != "jsonl_holds_busy"

    # --- hook_busy_idle staleness guard ---

    def test_hook_busy_idle_fresh_jsonl_holds(self):
        """hook=BUSY + JSONL fresh (well under window) → BUSY."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="BUSY",
                hook_age=200,
                prev_state="BUSY",
                jsonl_age=30,
            )
        )
        assert (rule.name, state) == ("hook_busy_idle", "BUSY")

    def test_hook_busy_idle_releases_when_jsonl_stale(self):
        """hook=BUSY for a long time + JSONL also stale past the
        BUSY_HOOK_JSONL_WINDOW → stop trusting the hook.

        Reproduces the rsyntaxtree scenario: Stop hook never fired
        after a completed turn 16 minutes ago, the BUSY signal from
        the last PreToolUse is ancient, and the session is actually
        idle (visible `❯ ` prompt, no tool activity in JSONL).
        """
        stale = ccm_core.BUSY_HOOK_JSONL_WINDOW + 300
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="BUSY",
                hook_age=stale,
                prev_state="BUSY",
                jsonl_age=stale,
            )
        )
        # Falls through to fallback_busy_to_idle → IDLE.
        assert (rule.name, state) == ("fallback_busy_to_idle", "IDLE")

    def test_hook_busy_idle_edge_just_under_window(self):
        """Exactly 1 second under the window still counts as fresh."""
        age = ccm_core.BUSY_HOOK_JSONL_WINDOW - 1
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="BUSY",
                hook_age=age,
                prev_state="BUSY",
                jsonl_age=age,
            )
        )
        assert (rule.name, state) == ("hook_busy_idle", "BUSY")

    def test_hook_busy_idle_no_jsonl_always_trusts_hook(self):
        """If there is no JSONL file at all (jsonl_age=-1), we have
        no counterevidence and must trust the BUSY hook regardless
        of how old it is. Covers projects without a Claude Code
        session log (e.g. older Claude Code or edge cases)."""
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                hook_state="BUSY",
                hook_age=9999,       # ancient
                prev_state="BUSY",
                jsonl_age=-1,        # no file
            )
        )
        assert (rule.name, state) == ("hook_busy_idle_no_jsonl", "BUSY")

    # --- fallback (no hooks) ---

    def test_fallback_busy_to_idle(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="BUSY")
        )
        assert (rule.name, state) == ("fallback_busy_to_idle", "IDLE")

    def test_fallback_permit_hold(self):
        rule, state = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE", prev_state="PERMIT",
                hook_state="PERMIT", hook_age=5,
            )
        )
        assert (rule.name, state) == ("fallback_permit_hold", "PERMIT")
        assert rule.action == ccm_core.Action.HOLD_NO_WRITE

    def test_fallback_permit_hold_requires_hook_signal(self):
        """Without an active PERMIT hook, do not hold PERMIT indefinitely.

        Regression guard: if Claude Code crashes during a permission
        dialog, the PERMIT hook eventually ages out but prev_state stays
        PERMIT in tmux. The old unconditional fallback_permit_hold would
        keep PERMIT forever. Now we require the hook signal to still be
        present and within PERMIT_MAX_TIMEOUT.
        """
        # No hook signal at all → fall through to default (IDLE)
        rule, state = ccm_core.evaluate_rules(
            make_ctx(raw="IDLE", prev_state="PERMIT")
        )
        assert rule.name == "default"
        assert state == "IDLE"

        # PERMIT hook present but expired → fall through
        rule2, state2 = ccm_core.evaluate_rules(
            make_ctx(
                raw="IDLE",
                prev_state="PERMIT",
                hook_state="PERMIT",
                hook_age=ccm_core.PERMIT_MAX_TIMEOUT + 10,
            )
        )
        assert rule2.name == "default"
        assert state2 == "IDLE"

    # --- raw_not_idle ---

    def test_raw_busy_passes_through(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="BUSY"))
        assert (rule.name, state) == ("raw_not_idle", "BUSY")

    def test_raw_permit_passes_through(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="PERMIT"))
        assert (rule.name, state) == ("raw_not_idle", "PERMIT")

    # --- default ---

    def test_default_pure_idle(self):
        rule, state = ccm_core.evaluate_rules(make_ctx(raw="IDLE"))
        assert rule.name == "default"
        assert state == "IDLE"


class TestRuleMatching:
    """Directly exercise Rule.matches() edge cases."""

    def test_hook_in_requires_signal_present(self):
        rule = ccm_core.Rule(name="t", hook_in=("BUSY",), hook_age_lt=10)
        # hook_state="" should NOT match even if hook_age_lt is satisfied by -1
        assert not rule.matches(make_ctx(hook_state="", hook_age=-1))

    def test_hook_age_lt_rejects_missing_signal(self):
        rule = ccm_core.Rule(name="t", hook_age_lt=10)
        assert not rule.matches(make_ctx(hook_age=-1))

    def test_wildcard_matches_all(self):
        rule = ccm_core.Rule(name="wild")
        assert rule.matches(make_ctx())
        assert rule.matches(make_ctx(raw="BUSY", hook_state="PERMIT"))


class TestApplyActions:
    """Direct tests for the side-effect layer.

    Uses a tmp dir for the hook signal path and mocks `_set_win_state`
    so we can assert both tmux writes and filesystem writes per action.
    """

    @pytest.fixture
    def project_dir(self, tmp_path, monkeypatch):
        # Redirect hook signal files to an isolated tmp dir
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        proj = tmp_path / "proj"
        proj.mkdir()
        return str(proj)

    def _run(self, rule, ctx, project_dir="", win_target="0:1"):
        with patch.object(ccm_core, "_set_win_state") as set_win:
            with patch.object(ccm_core, "tmux_cmd") as mock_tmux:
                result = ccm_core.apply_actions(
                    win_target, project_dir, ctx, rule, rule.result
                )
        return result, set_win, mock_tmux

    def test_default_writes_state(self):
        rule = ccm_core.Rule(name="t", result="BUSY", action=ccm_core.Action.DEFAULT)
        ctx = make_ctx()
        state, set_win, _ = self._run(rule, ctx)
        assert state == "BUSY"
        set_win.assert_called_once_with("0:1", "BUSY")

    def test_hold_no_write_skips_tmux(self):
        rule = ccm_core.Rule(
            name="t", result="PERMIT", action=ccm_core.Action.HOLD_NO_WRITE
        )
        ctx = make_ctx()
        state, set_win, _ = self._run(rule, ctx)
        assert state == "PERMIT"
        set_win.assert_not_called()

    def test_completed_at_set_on_busy_to_idle(self, project_dir):
        """apply_actions sets @ccm_completed_at on BUSY→IDLE transition."""
        rule = ccm_core.Rule(name="t", result="IDLE", action=ccm_core.Action.DEFAULT)
        ctx = make_ctx(prev_state="BUSY", now=12345)
        state, set_win, mock_tmux = self._run(rule, ctx, project_dir=project_dir)
        assert state == "IDLE"
        completed_calls = [c for c in mock_tmux.call_args_list
                           if len(c[0]) > 3 and "@ccm_completed_at" in str(c[0])]
        assert len(completed_calls) > 0


class TestFastPath:
    """evaluate_fast uses the same DETECTION_RULES as the slow path,
    so the statusline and dashboard can never disagree on state logic.
    """

    @pytest.fixture
    def project_dir(self, tmp_path, monkeypatch):
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        proj = tmp_path / "proj"
        proj.mkdir()
        return str(proj)

    def _write_hook(self, project_dir, state, age=0):
        path = ccm_core._hook_signal_path(project_dir)
        ts = int(time.time()) - age
        with open(path, "w") as f:
            f.write(f"{ts} {state}")

    # --- basic prev_state → state propagation ---

    def test_prev_idle_no_hook(self, project_dir):
        assert ccm_core.evaluate_fast("IDLE", project_dir) == "IDLE"

    def test_prev_busy_no_hook_stays_busy(self, project_dir):
        """Without ps info, prev=BUSY stays BUSY via rule raw_not_idle."""
        assert ccm_core.evaluate_fast("BUSY", project_dir) == "BUSY"

    def test_prev_permit_no_hook_stays_permit(self, project_dir):
        assert ccm_core.evaluate_fast("PERMIT", project_dir) == "PERMIT"

    def test_prev_shell_stays_shell(self, project_dir):
        assert ccm_core.evaluate_fast("SHELL", project_dir) == "SHELL"

    # --- hook overrides ---

    def test_hook_busy_overrides_idle(self, project_dir):
        self._write_hook(project_dir, "BUSY", age=1)
        assert ccm_core.evaluate_fast("IDLE", project_dir) == "BUSY"

    def test_hook_permit_overrides_busy(self, project_dir):
        self._write_hook(project_dir, "PERMIT", age=2)
        assert ccm_core.evaluate_fast("BUSY", project_dir) == "PERMIT"

    def test_hook_busy_trusted_regardless_of_age(self, project_dir):
        """Regression guard: no HOOK_TIMEOUT cap in fast path either."""
        self._write_hook(project_dir, "BUSY", age=900)
        assert ccm_core.evaluate_fast("IDLE", project_dir) == "BUSY"

    def test_hook_permit_expired(self, project_dir):
        """Stale PERMIT hook + prev=IDLE → IDLE (not stuck PERMIT)."""
        self._write_hook(
            project_dir, "PERMIT",
            age=ccm_core.PERMIT_MAX_TIMEOUT + 10,
        )
        assert ccm_core.evaluate_fast("IDLE", project_dir) == "IDLE"

    # --- no project dir ---

    def test_no_project_dir(self):
        """evaluate_fast with empty project_dir skips hook read gracefully."""
        assert ccm_core.evaluate_fast("BUSY", "") == "BUSY"


class TestLifecycleSequences:
    """End-to-end state transition sequences, evaluated as pure rule chains.

    Each test walks a realistic Claude Code lifecycle (user prompt → tool
    execution → permission → completion) and asserts that the rule table
    produces the right state at every step. No tmux/ps/file mocking —
    Context is constructed directly so we focus on detection logic.
    """

    def _eval(self, **ctx_kwargs):
        rule, state = ccm_core.evaluate_rules(make_ctx(**ctx_kwargs))
        return rule.name, state

    def test_simple_turn(self):
        """IDLE → BUSY(fresh) → BUSY(slow) → IDLE (direct, no DONE)."""
        # Initial idle
        assert self._eval(raw="IDLE", prev_state="IDLE") == ("default", "IDLE")
        # UserPromptSubmit fires BUSY hook (< 2s).
        assert self._eval(
            raw="IDLE", prev_state="IDLE", hook_state="BUSY", hook_age=0,
            jsonl_age=0,
        ) == ("hook_fresh_busy", "BUSY")
        # Text generation continues, pipeline still sees IDLE.
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="BUSY", hook_age=5,
            jsonl_age=3,
        ) == ("hook_busy_idle", "BUSY")
        # Stop fires — deletes signal file. JSONL still fresh (bridging).
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="", jsonl_age=1,
        ) == ("jsonl_fresh_activity", "BUSY")
        # JSONL ages past active threshold → IDLE directly
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="",
            jsonl_age=ccm_core.JSONL_ACTIVE_THRESHOLD + 10,
        ) == ("fallback_busy_to_idle", "IDLE")

    def test_multi_turn_natural_bridge(self):
        """BUSY → (Stop deletes signal) → JSONL bridges → BUSY continues.

        Real scenario: Claude uses Bash, Stop fires (deletes signal),
        but JSONL is fresh from the tool result. jsonl_fresh_activity
        bridges the gap. Next PreToolUse fires BUSY hook immediately.
        """
        # Tool execution in progress
        assert self._eval(
            raw="BUSY", prev_state="BUSY", hook_state="BUSY", hook_age=0,
            jsonl_age=0,
        ) == ("hook_fresh_busy", "BUSY")
        # Stop fires — deletes signal. JSONL fresh from tool result.
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="", jsonl_age=1,
        ) == ("jsonl_fresh_activity", "BUSY")
        # Next PreToolUse immediately fires BUSY again
        assert self._eval(
            raw="BUSY", prev_state="BUSY", hook_state="BUSY", hook_age=0,
            jsonl_age=0,
        ) == ("hook_fresh_busy", "BUSY")

    def test_permit_lifecycle(self):
        """BUSY → PERMIT → (user approves) → BUSY → IDLE (no DONE)."""
        # Tool wants permission
        assert self._eval(
            raw="BUSY", prev_state="BUSY", hook_state="PERMIT", hook_age=0,
        ) == ("hook_permit_blocking", "PERMIT")
        # User sees dialog for a while
        assert self._eval(
            raw="BUSY", prev_state="PERMIT", hook_state="PERMIT", hook_age=5,
        ) == ("hook_permit_blocking", "PERMIT")
        # User approves; brief IDLE gap before tool actually runs.
        assert self._eval(
            raw="IDLE", prev_state="PERMIT", hook_state="PERMIT", hook_age=6,
        ) == ("fallback_permit_hold", "PERMIT")
        # Tool runs: PreToolUse fires BUSY. JSONL fresh from tool start.
        assert self._eval(
            raw="IDLE", prev_state="PERMIT", hook_state="BUSY",
            hook_age=0, jsonl_age=0,
        ) == ("hook_fresh_busy", "BUSY")
        # Tool finishes: Stop deletes signal, JSONL ages past active threshold.
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="",
            jsonl_age=ccm_core.JSONL_ACTIVE_THRESHOLD + 10,
        ) == ("fallback_busy_to_idle", "IDLE")

    def test_fallback_no_hooks(self):
        """Hook signals absent entirely (old config or disabled)."""
        # Text generation: raw=BUSY from process tree
        assert self._eval(raw="BUSY", prev_state="IDLE") == (
            "raw_not_idle", "BUSY",
        )
        # Prompt returns: raw=IDLE, prev=BUSY → IDLE directly
        assert self._eval(raw="IDLE", prev_state="BUSY") == (
            "fallback_busy_to_idle", "IDLE",
        )

    def test_long_running_tool_stays_busy(self):
        """Long bash / text generation: BUSY hook goes stale but state must stay BUSY.

        Real incident (jwriter, 2026-04-10): a multi-minute tool chain
        produced no PreToolUse refresh for >5 min. With the old HOOK_TIMEOUT
        cap, the hook path failed and fallback_busy_to_done fired false DONE,
        then fallback_done_expired → IDLE.

        In the real incident, Claude Code's JSONL session log is
        updated at every conversation turn boundary independently of
        the hook pipeline (#25655 kills hooks but not JSONL writes).
        So while the hook ages, JSONL stays fresh as tool calls and
        message records accumulate. hook_busy_idle remains matched
        because the BUSY_HOOK_JSONL_WINDOW staleness guard is scoped
        to JSONL age, not hook age.
        """
        # Tool starts: fresh BUSY
        assert self._eval(
            raw="IDLE", hook_state="BUSY", hook_age=0, prev_state="BUSY",
            jsonl_age=0,
        ) == ("hook_fresh_busy", "BUSY")
        # 1 min in — JSONL bumped by a recent tool turn boundary.
        assert self._eval(
            raw="IDLE", hook_state="BUSY", hook_age=60, prev_state="BUSY",
            jsonl_age=10,
        ) == ("hook_busy_idle", "BUSY")
        # Past old HOOK_TIMEOUT boundary (was the regression).
        assert self._eval(
            raw="IDLE", hook_state="BUSY", hook_age=301, prev_state="BUSY",
            jsonl_age=20,
        ) == ("hook_busy_idle", "BUSY")
        # 15 minutes in, JSONL still being written by ongoing turns.
        assert self._eval(
            raw="IDLE", hook_state="BUSY", hook_age=900, prev_state="BUSY",
            jsonl_age=15,
        ) == ("hook_busy_idle", "BUSY")
        # Finally Stop fires — deletes signal. JSONL ages past active threshold.
        assert self._eval(
            raw="IDLE", hook_state="", prev_state="BUSY",
            jsonl_age=ccm_core.JSONL_ACTIVE_THRESHOLD + 10,
        ) == ("fallback_busy_to_idle", "IDLE")

    def test_shell_override_anywhere(self):
        """SHELL from process tree wins over any hook state, any prev."""
        for prev in ("IDLE", "BUSY", "PERMIT"):
            for hook in ("", "BUSY", "PERMIT"):
                name, state = self._eval(
                    raw="SHELL", prev_state=prev, hook_state=hook, hook_age=0,
                )
                assert state == "SHELL", f"prev={prev} hook={hook}"

    def test_tool_chain_holds_busy_via_stop_reason(self):
        """Between-tools gap with long tool execution: BUSY must hold.

        Real incident (monadic-chat, 2026-04-22): Claude runs a 1m22s
        Ruby test suite. Sequence:
          1. PreToolUse fires → hook=BUSY, grandchild present.
          2. Tool runs without JSONL writes for >15s.
          3. Tool completes → Stop deletes signal. Grandchild may
             momentarily disappear before the next PreToolUse fires.
          4. ccm scan at this exact moment sees raw=IDLE, hook="",
             grandchild absent. With only the old rules, neither
             jsonl_fresh_activity (<5s) nor jsonl_holds_busy (<15s)
             matched → fallback_busy_to_idle → false IDLE.

        The authoritative fix is `jsonl_tool_use_pending`: the latest
        assistant record's stop_reason is "tool_use" for the whole
        in-flight turn, so we can hold BUSY until the JSONL shows
        "end_turn" (or some other terminal reason).
        """
        # Mid-tool-chain: Stop deleted the hook signal, JSONL is stale
        # past the 15s fresh/active windows, but the assistant
        # stop_reason is still "tool_use".
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="",
            jsonl_age=45,
            jsonl_last_stop_reason="tool_use",
        ) == ("jsonl_tool_use_pending", "BUSY")
        # Response truly ends: last assistant is end_turn → IDLE.
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="",
            jsonl_age=45,
            jsonl_last_stop_reason="end_turn",
        ) == ("fallback_busy_to_idle", "IDLE")
        # max_tokens / stop_sequence are also terminal → IDLE.
        for terminal in ("max_tokens", "stop_sequence"):
            assert self._eval(
                raw="IDLE", prev_state="BUSY", hook_state="",
                jsonl_age=45, jsonl_last_stop_reason=terminal,
            ) == ("fallback_busy_to_idle", "IDLE"), terminal
        # Safety cap: tool_use pending but JSONL ages beyond
        # BUSY_HOOK_JSONL_WINDOW → give up, do not hold BUSY forever
        # on an abandoned session.
        assert self._eval(
            raw="IDLE", prev_state="BUSY", hook_state="",
            jsonl_age=ccm_core.BUSY_HOOK_JSONL_WINDOW + 10,
            jsonl_last_stop_reason="tool_use",
        ) == ("fallback_busy_to_idle", "IDLE")

    def test_tool_use_pending_does_not_override_fresh_hook(self):
        """A fresh BUSY hook should still win — higher priority than
        the stop_reason hold. This preserves the existing multi-project
        race guarantee (hook_fresh_busy at priority 3)."""
        assert self._eval(
            raw="IDLE", prev_state="BUSY",
            hook_state="BUSY", hook_age=0, jsonl_age=2,
            jsonl_last_stop_reason="tool_use",
        ) == ("hook_fresh_busy", "BUSY")

    def test_tool_use_pending_requires_prev_busy(self):
        """We only hold BUSY across tool turns when we were BUSY
        before. From a fresh/reset prev_state='' (e.g. just after
        reset_window_after_attach), the stop_reason hold does NOT
        fire — it would be wrong to promote a newly-attached window
        to BUSY based purely on a stale JSONL assistant record.
        With jsonl_age=60 (past the 5s/15s freshness windows) and
        prev_state='', the match falls through to `default` → IDLE."""
        rule, state = self._eval(
            raw="IDLE", prev_state="",  # post-attach reset
            hook_state="",
            jsonl_age=60, jsonl_last_stop_reason="tool_use",
        )
        assert rule == "default"
        assert state == "IDLE"


# ─── Formatting helpers ───

class TestFormatElapsed:
    def test_seconds(self):
        ts = int(time.time()) - 30
        assert ccm_core.format_elapsed(ts) == "30s"

    def test_minutes(self):
        ts = int(time.time()) - 180
        assert ccm_core.format_elapsed(ts) == "3m"

    def test_hours(self):
        ts = int(time.time()) - 7200
        assert ccm_core.format_elapsed(ts) == "2h"

    def test_days(self):
        ts = int(time.time()) - 172800
        assert ccm_core.format_elapsed(ts) == "2d"

    def test_zero_returns_empty(self):
        assert ccm_core.format_elapsed(0) == ""

    def test_none_returns_empty(self):
        assert ccm_core.format_elapsed(None) == ""


class TestFormatDir:
    def test_fits_full(self):
        assert ccm_core.format_dir("/short", 10, 80) == "/short"

    def test_truncates_to_parent_base(self):
        long_dir = "/very/long/path/to/project"
        result = ccm_core.format_dir(long_dir, 60, 80)
        assert "…/" in result or result == "project"

    def test_returns_empty_when_too_narrow(self):
        assert ccm_core.format_dir("/some/path", 75, 80) == ""


class TestHooksConfigured:
    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_false_when_no_settings(self, mock_open):
        assert ccm_core.hooks_configured() is False


# ─── tmux_batch ───

class TestTmuxBatch:
    @patch("subprocess.run")
    def test_single_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        ccm_core.tmux_batch(("set-option", "-wt", "0:1", "@key", "val"))
        args = mock_run.call_args[0][0]
        assert args == ["tmux", "set-option", "-wt", "0:1", "@key", "val"]

    @patch("subprocess.run")
    def test_multiple_commands(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        ccm_core.tmux_batch(
            ("set-option", "-wt", "0:1", "@key1", "val1"),
            ("set-option", "-wt", "0:1", "@key2", "val2"),
        )
        args = mock_run.call_args[0][0]
        assert ";" in args
        assert args.count(";") == 1

    @patch("subprocess.run")
    def test_empty_batch_no_call(self, mock_run):
        ccm_core.tmux_batch()
        mock_run.assert_not_called()


# ─── validate_name ───

class TestValidateName:
    def test_basic(self):
        assert ccm_core.validate_name("my-project") == "my-project"

    def test_whitespace_to_hyphens(self):
        assert ccm_core.validate_name("my project  name") == "my-project-name"

    def test_strip_dangerous_chars(self):
        assert ccm_core.validate_name("test;rm -rf") == "testrm-rf"

    def test_strip_quotes(self):
        assert ccm_core.validate_name("it's a \"test\"") == "its-a-test"

    def test_strip_leading_trailing_hyphens(self):
        assert ccm_core.validate_name("--foo--") == "foo"

    def test_empty_returns_empty(self):
        assert ccm_core.validate_name("") == ""

    def test_all_dangerous_returns_empty(self):
        assert ccm_core.validate_name("$();&") == ""

    def test_tabs_and_newlines(self):
        assert ccm_core.validate_name("a\tb\nc") == "a-b-c"


# ─── find_window / project_exists ───

class TestSanitizeSnapshotName:
    def test_basic(self):
        assert ccm_core._sanitize_snapshot_name("my-snapshot") == "my-snapshot"

    def test_path_traversal(self):
        assert ccm_core._sanitize_snapshot_name("../../etc/passwd") == "passwd"

    def test_slash(self):
        assert ccm_core._sanitize_snapshot_name("foo/bar") == "bar"

    def test_dots_only(self):
        with pytest.raises(SystemExit):
            ccm_core._sanitize_snapshot_name("..")

    def test_empty(self):
        with pytest.raises(SystemExit):
            ccm_core._sanitize_snapshot_name("")


class TestFindWindow:
    @patch("ccm_core.tmux_cmd")
    def test_found(self, mock_tmux):
        mock_tmux.return_value = "1\tmy-proj\n2\tother"
        assert ccm_core.find_window("main", "my-proj") == "1"

    @patch("ccm_core.tmux_cmd")
    def test_not_found(self, mock_tmux):
        mock_tmux.return_value = "1\tother"
        assert ccm_core.find_window("main", "missing") is None

    @patch("ccm_core.tmux_cmd")
    def test_empty_output(self, mock_tmux):
        mock_tmux.return_value = ""
        assert ccm_core.find_window("main", "any") is None


class TestProjectExists:
    @patch("ccm_core.find_window", return_value="1")
    def test_exists(self, _):
        assert ccm_core.project_exists("main", "proj") is True

    @patch("ccm_core.find_window", return_value=None)
    def test_not_exists(self, _):
        assert ccm_core.project_exists("main", "proj") is False


# ─── list_windows_raw ───

class TestListWindowsRaw:
    @patch("ccm_core.tmux_cmd")
    def test_returns_tagged_only(self, mock_tmux):
        mock_tmux.return_value = "1\twin1\tproj1\t/dir1\n2\twin2\t\t/dir2\n3\twin3\tproj3\t/dir3"
        result = ccm_core.list_windows_raw("main")
        assert len(result) == 2
        assert result[0] == ("1", "win1", "proj1", "/dir1")
        assert result[1] == ("3", "win3", "proj3", "/dir3")

    @patch("ccm_core.tmux_cmd")
    def test_empty(self, mock_tmux):
        mock_tmux.return_value = ""
        assert ccm_core.list_windows_raw("main") == []


# ─── snapshot save/list (with mocked tmux) ───

class TestSnapshotSave:
    @patch("ccm_core.tmux_cmd")
    def test_creates_json(self, mock_tmux, tmp_path):
        mock_tmux.return_value = "1\twin1\tproj1\t/home/user/dir1\n2\twin2\tproj2\t/home/user/dir2"
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)
        ccm_core.cmd_snapshot_save("test-snap", quiet=True)
        import json
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
        ccm_core.cmd_snapshot_save("test2", quiet=True)
        import json
        data = json.loads((tmp_path / "test2.json").read_text())
        assert len(data["projects"]) == 1


# ─── cmd_snapshot_load ───

class TestSnapshotLoad:
    def _write_snapshot(self, tmp_path, name, projects):
        import json
        snap = {"version": 1, "name": name, "created": "2025-01-01T00:00:00+0000",
                "projects": projects}
        fp = tmp_path / f"{name}.json"
        fp.write_text(json.dumps(snap))

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.hooks_configured", return_value=True)
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_core.get_session", return_value="main")
    def test_load_creates_windows(self, mock_session, mock_tmux, mock_batch, mock_hooks, mock_auto, tmp_path):
        """Loading a snapshot creates windows for each project."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        # Create a real temp directory for the project
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

        ccm_core.cmd_snapshot_load("test-snap")

        # Verify new-window was called
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

        ccm_core.cmd_snapshot_load("test-skip")

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

        ccm_core.cmd_snapshot_load("test-dup")

        captured = capsys.readouterr()
        assert "already exists" in captured.err

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir

    def test_load_nonexistent_snapshot(self, tmp_path):
        """Loading a non-existent snapshot exits with error."""
        orig_dir = ccm_core.CCM_SNAPSHOT_DIR
        ccm_core.CCM_SNAPSHOT_DIR = str(tmp_path)

        with pytest.raises(SystemExit):
            ccm_core.cmd_snapshot_load("nonexistent")

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
        ccm_core.cmd_snapshot_load("test-null")

        ccm_core.CCM_SNAPSHOT_DIR = orig_dir


# ─── cmd_add ───

class TestCmdAdd:
    @patch("ccm_core._autosave_trigger")
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

        ccm_core.cmd_add(str(proj_dir), "my-project")

        # Verify tmux_batch was called to set metadata
        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        tag_names = [a[3] for a in batch_args if len(a) > 3]
        assert "@ccm_project" in tag_names
        assert "@ccm_dir" in tag_names

    def test_add_missing_dir_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_add("/nonexistent/directory/xyz")

    def test_add_empty_dir_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_add("")

    @patch("ccm_core.find_window", return_value="1")
    @patch("ccm_core.get_session", return_value="main")
    def test_add_duplicate_name_exits(self, mock_session, mock_find, tmp_path):
        proj_dir = tmp_path / "dup"
        proj_dir.mkdir()
        with pytest.raises(SystemExit):
            ccm_core.cmd_add(str(proj_dir), "existing")

    @patch("ccm_core._autosave_trigger")
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

        ccm_core.cmd_add(str(proj_dir))

        # Check @ccm_project was set to basename
        batch_args = mock_batch.call_args[0]
        project_tag = [a for a in batch_args if len(a) > 3 and a[3] == "@ccm_project"]
        assert project_tag[0][4] == "cool-project"

    @patch("ccm_core._autosave_trigger")
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

        ccm_core.cmd_add(str(proj_dir), "loading-test", start_claude=False, _loading=True)

        mock_auto.assert_not_called()


# ─── cmd_unregister ───

class TestCmdUnregister:
    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_removes_tags(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_core.cmd_unregister("my-proj")

        # Should call tmux_batch to remove all tags
        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        # Every command should be a set-option -u (unset)
        for cmd in batch_args:
            assert "-u" in cmd

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_restores_original_name(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_core.cmd_unregister("my-proj")

        # Should call rename-window with original name
        rename_calls = [c for c in mock_tmux.call_args_list
                        if c[0][0] == "rename-window"]
        assert len(rename_calls) == 1
        assert rename_calls[0][0][-1] == "orig-name"

    def test_unregister_empty_name_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_unregister("")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_core.cmd_unregister("nonexistent")

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.tmux_cmd", return_value="orig-name")
    @patch("ccm_core.find_window", return_value="2")
    @patch("ccm_core.get_session", return_value="main")
    def test_unregister_triggers_autosave(self, mock_session, mock_find, mock_tmux, mock_batch, mock_auto):
        ccm_core.cmd_unregister("proj")
        mock_auto.assert_called_once()

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.cleanup_project_runtime_files")
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
        ccm_core.cmd_unregister("proj")
        mock_cleanup.assert_called_once_with("/x/proj-dir")


# ─── cmd_remove ───

class TestCmdRemove:
    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.cleanup_project_runtime_files")
    @patch("ccm_core.tmux_cmd", return_value="/x/proj-dir")
    @patch("ccm_core.find_window", return_value="3")
    @patch("ccm_core.get_session", return_value="main")
    def test_remove_kills_window_and_cleans(
        self, mock_session, mock_find, mock_tmux, mock_cleanup, mock_auto,
    ):
        ccm_core.cmd_remove("proj")
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
            ccm_core.cmd_remove("")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_remove_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_core.cmd_remove("nonexistent")


# ─── cmd_rename ───

class TestCmdRename:
    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_updates_tag_and_window(self, mock_session, mock_find, mock_batch, mock_auto):
        # find_window returns index for old name, None for new name (not duplicate)
        mock_find.side_effect = lambda sess, name: "1" if name == "old" else None

        ccm_core.cmd_rename("old", "new")

        assert mock_batch.called
        batch_args = mock_batch.call_args[0]
        # Should set @ccm_project to "new" and rename window
        set_cmd = [a for a in batch_args if "set-option" in a[0] and "@ccm_project" in a]
        assert set_cmd[0][-1] == "new"
        rename_cmd = [a for a in batch_args if "rename-window" in a[0]]
        assert rename_cmd[0][-1] == "new"

    def test_rename_empty_old_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_rename("", "new")

    def test_rename_empty_new_exits(self):
        with pytest.raises(SystemExit):
            ccm_core.cmd_rename("old", "")

    @patch("ccm_core.find_window", return_value=None)
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_not_found_exits(self, mock_session, mock_find):
        with pytest.raises(SystemExit):
            ccm_core.cmd_rename("nonexistent", "new")

    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_duplicate_exits(self, mock_session, mock_find):
        # Both old and new names exist
        mock_find.return_value = "1"
        with pytest.raises(SystemExit):
            ccm_core.cmd_rename("old", "taken")

    @patch("ccm_core._autosave_trigger")
    @patch("ccm_core.tmux_batch")
    @patch("ccm_core.find_window")
    @patch("ccm_core.get_session", return_value="main")
    def test_rename_triggers_autosave(self, mock_session, mock_find, mock_batch, mock_auto):
        mock_find.side_effect = lambda sess, name: "1" if name == "old" else None
        ccm_core.cmd_rename("old", "new")
        mock_auto.assert_called_once()


# ─── Per-project notification marker (cross-project collision fix) ───
#
# Before v0.2.0 the hook instant-notify path wrote a SINGLE global
# marker `${TMPDIR}/ccm-${UID}/hook-notified`, so any two ccm projects
# completing within 10 seconds of each other would silently dedup —
# the second project's COMPLETED notification was suppressed. The
# marker is now keyed on md5-of-cwd; these tests lock in that
# per-project isolation.

class TestProjectNotifyMarker:
    def _setup_tmp(self, tmp_path, monkeypatch):
        marker_dir = tmp_path / "notified"
        marker_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_NOTIFY_MARKER_DIR", str(marker_dir))
        return marker_dir

    def test_missing_marker_returns_none(self, tmp_path, monkeypatch):
        self._setup_tmp(tmp_path, monkeypatch)
        assert ccm_core.read_project_notify_marker("/no/such/project") is None

    def test_empty_project_dir_returns_none(self, tmp_path, monkeypatch):
        self._setup_tmp(tmp_path, monkeypatch)
        assert ccm_core.read_project_notify_marker("") is None
        assert ccm_core.read_project_notify_marker(None) is None

    def test_valid_marker_parses_ts_and_state(self, tmp_path, monkeypatch):
        marker_dir = self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/proj-a"
        key = ccm_core.md5_hash(ccm_core._resolve_project_dir(project))
        (marker_dir / key).write_text("1234567890 COMPLETED")
        result = ccm_core.read_project_notify_marker(project)
        assert result == (1234567890, "COMPLETED")

    def test_malformed_marker_returns_none(self, tmp_path, monkeypatch):
        marker_dir = self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/malformed"
        key = ccm_core.md5_hash(ccm_core._resolve_project_dir(project))
        # Missing state field
        (marker_dir / key).write_text("1234567890")
        assert ccm_core.read_project_notify_marker(project) is None

    def test_non_integer_ts_returns_none(self, tmp_path, monkeypatch):
        marker_dir = self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/bad-ts"
        key = ccm_core.md5_hash(ccm_core._resolve_project_dir(project))
        (marker_dir / key).write_text("not-a-number COMPLETED")
        assert ccm_core.read_project_notify_marker(project) is None

    def test_projects_are_isolated(self, tmp_path, monkeypatch):
        """The whole point of the fix: project A's marker must not
        appear when asking about project B, and vice versa."""
        marker_dir = self._setup_tmp(tmp_path, monkeypatch)
        project_a = "/x/proj-a"
        project_b = "/x/proj-b"
        key_a = ccm_core.md5_hash(ccm_core._resolve_project_dir(project_a))
        key_b = ccm_core.md5_hash(ccm_core._resolve_project_dir(project_b))
        (marker_dir / key_a).write_text("100 COMPLETED")
        (marker_dir / key_b).write_text("200 PERMIT")
        assert ccm_core.read_project_notify_marker(project_a) == (100, "COMPLETED")
        assert ccm_core.read_project_notify_marker(project_b) == (200, "PERMIT")


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

    def _populate(self, tmp_path, project_dir):
        """Create all runtime files for a project and return the md5 key."""
        key = ccm_core.md5_hash(ccm_core._resolve_project_dir(project_dir))
        (tmp_path / "hooks" / key).write_text("0 BUSY")
        (tmp_path / "hooks" / f"{key}.busy").write_text("0")
        (tmp_path / "hooks" / f"{key}.pending").write_text("0")
        (tmp_path / "notified" / key).write_text("0 COMPLETED")
        (tmp_path / "git-cache" / key).write_text("main")
        (tmp_path / "port-cache" / key).write_text("3000")
        return key

    def test_removes_all_runtime_files(self, tmp_path, monkeypatch):
        self._setup_tmp(tmp_path, monkeypatch)
        project = "/x/proj-a"
        key = self._populate(tmp_path, project)

        ccm_core.cleanup_project_runtime_files(project)

        for rel in (
            f"hooks/{key}",
            f"hooks/{key}.busy",
            f"hooks/{key}.pending",
            f"notified/{key}",
            f"git-cache/{key}",
            f"port-cache/{key}",
        ):
            assert not (tmp_path / rel).exists(), f"{rel} should be deleted"

    def test_leaves_other_projects_alone(self, tmp_path, monkeypatch):
        """Cleanup is keyed on md5-of-cwd; other projects' files
        must survive unaffected."""
        self._setup_tmp(tmp_path, monkeypatch)
        project_a = "/x/proj-a"
        project_b = "/x/proj-b"
        key_a = self._populate(tmp_path, project_a)
        key_b = self._populate(tmp_path, project_b)

        ccm_core.cleanup_project_runtime_files(project_a)

        # Project A files gone
        assert not (tmp_path / "hooks" / key_a).exists()
        assert not (tmp_path / "notified" / key_a).exists()
        # Project B files intact
        assert (tmp_path / "hooks" / key_b).exists()
        assert (tmp_path / "notified" / key_b).exists()
        assert (tmp_path / "git-cache" / key_b).exists()

    def test_missing_files_silent_noop(self, tmp_path, monkeypatch):
        """No files to delete (fresh project) must not raise — this
        is the common case when unregistering an idle project."""
        self._setup_tmp(tmp_path, monkeypatch)
        # Should not raise
        ccm_core.cleanup_project_runtime_files("/x/never-ran")

    def test_empty_project_dir_noop(self, tmp_path, monkeypatch):
        self._setup_tmp(tmp_path, monkeypatch)
        ccm_core.cleanup_project_runtime_files("")
        ccm_core.cleanup_project_runtime_files(None)


# ─── raise_on_die / CCMError ───

class TestRaiseOnDie:
    def test_ccm_die_exits_by_default(self):
        with pytest.raises(SystemExit):
            ccm_core.ccm_die("boom")

    def test_ccm_die_raises_inside_context(self):
        with ccm_core.raise_on_die():
            with pytest.raises(ccm_core.CCMError, match="boom"):
                ccm_core.ccm_die("boom")

    def test_context_restores_previous_mode(self):
        with ccm_core.raise_on_die():
            pass
        # After exit, default behavior (exit) must be restored
        with pytest.raises(SystemExit):
            ccm_core.ccm_die("after")

    def test_nested_context(self):
        with ccm_core.raise_on_die():
            with ccm_core.raise_on_die():
                with pytest.raises(ccm_core.CCMError):
                    ccm_core.ccm_die("inner")
            # Outer context still active
            with pytest.raises(ccm_core.CCMError):
                ccm_core.ccm_die("outer")

    def test_other_thread_unaffected(self):
        """raise_on_die() is thread-local: other threads keep exit behavior."""
        import threading
        result = {}

        def worker():
            try:
                ccm_core.ccm_die("from worker")
            except SystemExit:
                result["exited"] = True
            except ccm_core.CCMError:
                result["raised"] = True

        with ccm_core.raise_on_die():
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        assert result.get("exited") is True
        assert "raised" not in result


# ─── SHELL cluster detection (#48069 canary) ───

class TestShellClusterDetection:
    """Unit tests for the cluster-SHELL-transition canary that
    surfaces anthropics/claude-code#48069 (silent-exit regression)."""

    def _tmux_mock(self):
        """Build a tmux_cmd mock that maintains a fake per-option store
        in memory, so push and read round-trip correctly."""
        store = {}  # "target/opt" → value

        def fake_tmux(*args):
            # show-option -wqv -t <target> <name>
            if len(args) >= 5 and args[0] == "show-option":
                target = args[3]
                opt = args[4]
                return store.get(f"{target}/{opt}", "")
            # set-option -wt <target> <name> <value>
            if len(args) >= 5 and args[0] == "set-option" and "-u" not in args:
                target = args[2]
                opt = args[3]
                value = args[4]
                store[f"{target}/{opt}"] = value
                return ""
            return ""

        return fake_tmux, store

    def test_empty_history_no_warning(self, monkeypatch):
        fake, _store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        assert ccm_core.shell_cluster_warning("0:1", "proj") == ""

    def test_below_threshold_no_warning(self, monkeypatch):
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        store["0:1/@ccm_shell_history"] = f"{now},{now - 10}"  # only 2 entries
        assert ccm_core.shell_cluster_warning("0:1", "proj") == ""

    def test_at_threshold_fires_warning(self, monkeypatch):
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        store["0:1/@ccm_shell_history"] = f"{now},{now - 60},{now - 120}"  # 3 entries
        msg = ccm_core.shell_cluster_warning("0:1", "proj")
        assert "proj" in msg
        assert "#48069" in msg
        assert "3" in msg  # the count

    def test_stale_entries_ignored(self, monkeypatch):
        """Entries older than SHELL_CLUSTER_WINDOW are filtered out
        on read so the count only reflects recent events."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        # 2 recent + 2 ancient (past 10 min window by default 600s)
        store["0:1/@ccm_shell_history"] = (
            f"{now},{now - 60},"
            f"{now - ccm_core.SHELL_CLUSTER_WINDOW - 100},"
            f"{now - ccm_core.SHELL_CLUSTER_WINDOW - 200}"
        )
        # Only 2 recent entries — below 3 threshold
        assert ccm_core.shell_cluster_warning("0:1", "proj") == ""

    def test_push_prepends_and_trims_stale(self, monkeypatch):
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        # Start with 1 stale + 1 recent
        store["0:1/@ccm_shell_history"] = f"{now - 60},{now - ccm_core.SHELL_CLUSTER_WINDOW - 100}"
        ccm_core._push_shell_transition("0:1")
        history = ccm_core._read_shell_history("0:1")
        # New timestamp prepended, stale filtered out (on READ)
        assert len(history) == 2
        assert history[0] >= now  # newly pushed is newest

    def test_push_dedups_same_second(self, monkeypatch):
        """A second push within the same second should be a no-op
        so that two rule evaluations in the same cycle do not
        double-count one transition."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        ccm_core._push_shell_transition("0:1")
        ccm_core._push_shell_transition("0:1")
        history = ccm_core._read_shell_history("0:1")
        assert len(history) == 1

    def test_push_caps_history_length(self, monkeypatch):
        """History is capped at max(SHELL_CLUSTER_COUNT * 2, 6) to
        avoid unbounded growth of the tmux option value."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        # Seed with many entries (all recent, so none are trimmed by
        # the time-horizon filter)
        seed = [str(now - i) for i in range(20)]
        store["0:1/@ccm_shell_history"] = ",".join(seed)
        ccm_core._push_shell_transition("0:1")
        history = ccm_core._read_shell_history("0:1")
        cap = max(ccm_core.SHELL_CLUSTER_COUNT * 2, 6)
        assert len(history) <= cap

    def test_shell_cluster_warnings_iterates_projects(self, monkeypatch):
        """The list helper returns one message per crossing project."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        store["0:1/@ccm_shell_history"] = f"{now},{now - 60},{now - 120}"  # over threshold
        store["0:2/@ccm_shell_history"] = f"{now}"  # below threshold
        projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
            ccm_core.Project("0:2", "2", "beta",  "/tmp/b", "IDLE"),
        ]
        msgs = ccm_core.shell_cluster_warnings(projects)
        assert len(msgs) == 1
        assert "alpha" in msgs[0]
        assert "beta" not in msgs[0]

    def test_apply_actions_records_shell_transition(self, monkeypatch):
        """A rule that resolves to SHELL with a non-SHELL prev_state
        should trigger _push_shell_transition via apply_actions."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="IDLE")
        rule, state = ccm_core.evaluate_rules(ctx)
        assert state == "SHELL"  # process_shell fires
        ccm_core.apply_actions("0:5", "", ctx, rule, state)

        history = ccm_core._read_shell_history("0:5")
        assert len(history) == 1  # one new transition recorded

    def test_apply_actions_ignores_shell_to_shell(self, monkeypatch):
        """A steady-state SHELL (prev=SHELL → new=SHELL) should not
        push a new transition. Only transitions into SHELL count."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="SHELL")
        rule, state = ccm_core.evaluate_rules(ctx)
        ccm_core.apply_actions("0:5", "", ctx, rule, state)

        history = ccm_core._read_shell_history("0:5")
        assert history == []  # no transition recorded

    def test_apply_actions_ignores_empty_prev_state(self, monkeypatch):
        """Regression guard: `reset_window_after_attach()` (called from
        every attach path) explicitly resets `@ccm_prev_state`. The next
        scan then sees prev_state="" and might briefly observe SHELL
        before the new claude process is detected. Without filtering,
        this would inflate the SHELL cluster count by 1 per attach.

        The filter requires prev_state to be a known active state
        (BUSY / IDLE / PERMIT) before counting a transition.
        """
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        # Empty prev_state (post-attach phantom)
        ctx = make_ctx(raw="SHELL", prev_state="")
        rule, state = ccm_core.evaluate_rules(ctx)
        ccm_core.apply_actions("0:5", "", ctx, rule, state)

        assert ccm_core._read_shell_history("0:5") == []

    def test_apply_actions_ignores_down_to_shell(self, monkeypatch):
        """DOWN → SHELL is not a session crash either. DOWN means
        the window was momentarily without panes; the transition
        is tmux housekeeping, not a Claude exit."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="DOWN")
        rule, state = ccm_core.evaluate_rules(ctx)
        ccm_core.apply_actions("0:5", "", ctx, rule, state)

        assert ccm_core._read_shell_history("0:5") == []

    def test_apply_actions_records_busy_to_shell(self, monkeypatch):
        """The real-crash case: BUSY → SHELL is counted."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="BUSY")
        rule, state = ccm_core.evaluate_rules(ctx)
        ccm_core.apply_actions("0:5", "", ctx, rule, state)

        assert len(ccm_core._read_shell_history("0:5")) == 1

    def test_apply_actions_records_permit_to_shell(self, monkeypatch):
        """PERMIT → SHELL also counts."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="PERMIT")
        rule, state = ccm_core.evaluate_rules(ctx)
        ccm_core.apply_actions("0:5", "", ctx, rule, state)

        assert len(ccm_core._read_shell_history("0:5")) == 1

    def test_reset_window_after_attach_clears_shell_history(
        self, monkeypatch, tmp_path
    ):
        """reset_window_after_attach() is the canonical post-attach
        reset, called from cmd_attach and dashboard attach paths.
        It must wipe @ccm_shell_history so the cluster canary
        acknowledges the user's attention.
        """
        # Stub tmux_cmd: track set-option calls and serve show-option
        # for @ccm_dir / @ccm_shell_history.
        store = {"0:5/@ccm_dir": "/tmp/proj", "0:5/@ccm_shell_history": "1,2,3"}
        unset_calls = []

        def fake(*args):
            if len(args) >= 5 and args[0] == "show-option":
                target = args[3]
                opt = args[4]
                return store.get(f"{target}/{opt}", "")
            if args[0] == "set-option":
                # Two real shapes used by reset_window_after_attach():
                #   set-option -wt TARGET -u OPT
                #   set-option -wq -t TARGET OPT VALUE
                target = None
                if "-wt" in args:
                    target = args[args.index("-wt") + 1]
                elif "-t" in args:
                    target = args[args.index("-t") + 1]
                if "-u" in args:
                    opt = args[-1]
                    unset_calls.append((target, opt))
                    store.pop(f"{target}/{opt}", None)
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        # Avoid touching the real CCM_HOOK_DIR
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(tmp_path))

        ccm_core.reset_window_after_attach("0:5")

        # @ccm_shell_history should have been unset
        assert ("0:5", "@ccm_shell_history") in unset_calls
        assert "0:5/@ccm_shell_history" not in store

    def test_reset_window_after_attach_unsets_completed_at(
        self, monkeypatch, tmp_path
    ):
        """reset_window_after_attach() must unset @ccm_completed_at
        so the ✔ marker disappears immediately on attach.
        """
        store = {
            "0:5/@ccm_dir": "/tmp/proj",
            "0:5/@ccm_completed_at": "1700000000",
        }
        unset_calls = []

        def fake(*args):
            if len(args) >= 5 and args[0] == "show-option":
                target = args[3]
                opt = args[4]
                return store.get(f"{target}/{opt}", "")
            if args[0] == "set-option":
                target = None
                if "-wt" in args:
                    target = args[args.index("-wt") + 1]
                elif "-t" in args:
                    target = args[args.index("-t") + 1]
                if "-u" in args:
                    opt = args[-1]
                    unset_calls.append((target, opt))
                    store.pop(f"{target}/{opt}", None)
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(tmp_path))

        ccm_core.reset_window_after_attach("0:5")

        assert ("0:5", "@ccm_completed_at") in unset_calls
        assert "0:5/@ccm_completed_at" not in store


# ─── cmd_send ───

class TestCmdSend:
    """Unit tests for `ccm send` — the cross-project prompt injector."""

    def _make_project(self, name="blog", state="IDLE", win_target="0:5"):
        return ccm_core.Project(
            win_target=win_target,
            win_idx=win_target.split(":")[1],
            name=name,
            directory=f"/tmp/{name}",
            state=state,
        )

    def _patch_resolution(self, monkeypatch, project=None, session="0"):
        """Install stubs for get_session / find_window / build_project_list."""
        if project is None:
            project = self._make_project()
        monkeypatch.setattr(ccm_core, "get_session", lambda: session)
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: project.win_idx if name == project.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [project],
        )
        # Non-interactive by default so the confirmation prompt is skipped
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        return project

    def _tmux_calls(self, mock_tmux):
        """Return the positional args of every tmux_cmd call."""
        return [tuple(c.args) for c in mock_tmux.call_args_list]

    # --- happy path ---

    def test_basic_send(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # Cancel any stuck mode
        assert ("send-keys", "-t", "0:5", "-X", "cancel") in calls
        # Literal send of "hello"
        assert ("send-keys", "-t", "0:5", "-l", "hello") in calls
        # Final Enter
        assert ("send-keys", "-t", "0:5", "Enter") in calls
        # Enter comes after the literal send
        literal_i = calls.index(("send-keys", "-t", "0:5", "-l", "hello"))
        enter_i = calls.index(("send-keys", "-t", "0:5", "Enter"))
        assert enter_i > literal_i

    def test_send_concatenates_multiple_positional_args(self, monkeypatch):
        """`ccm send blog hello world` joins the remaining argv into a
        single message, matching how the shell passes unquoted words."""
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "hello", "world"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hello world") in calls

    def test_send_no_enter(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "--no-enter", "hi"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hi") in calls
        assert ("send-keys", "-t", "0:5", "Enter") not in calls

    def test_send_multiline_uses_m_enter_between_lines(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        message = "line1\nline2\nline3"
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", message])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "line1") in calls
        assert ("send-keys", "-t", "0:5", "-l", "line2") in calls
        assert ("send-keys", "-t", "0:5", "-l", "line3") in calls
        # Two M-Enter separators, one final Enter
        m_enter_count = sum(
            1 for c in calls
            if c == ("send-keys", "-t", "0:5", "M-Enter")
        )
        enter_count = sum(
            1 for c in calls
            if c == ("send-keys", "-t", "0:5", "Enter")
        )
        assert m_enter_count == 2
        assert enter_count == 1

    def test_send_from_file(self, monkeypatch, tmp_path):
        self._patch_resolution(monkeypatch)
        f = tmp_path / "msg.txt"
        f.write_text("from file")
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "--file", str(f)])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "from file") in calls

    def test_send_from_stdin(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("piped"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "--stdin"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "piped") in calls

    def test_send_dash_alias_for_stdin(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("piped2"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "-"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "piped2") in calls

    def test_send_stdin_from_tty_skips_confirmation(self, monkeypatch):
        """Regression guard for the silent-cancel bug:

        A TTY user running `ccm send blog --stdin` and typing a
        message terminated by Ctrl-D consumes stdin. The confirmation
        prompt's `input()` call would then raise EOFError because
        stdin is exhausted, and the `except EOFError` branch would
        silently cancel — the user sees "Cancelled" and never gets
        the preview, and the message is lost.

        Fix: reading stdin force-sets skip_confirm. This test
        simulates the scenario with isatty=True and a StringIO
        stdin, and asserts the message is still sent.
        """
        self._patch_resolution(monkeypatch)
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("typed body"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)   # TTY
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)  # TTY
        # Also patch builtins.input so that if the fix regressed
        # we would raise a clear error instead of EOFError.
        def _fail_input(*a, **k):
            raise AssertionError(
                "confirmation prompt should have been skipped after "
                "consuming stdin"
            )
        monkeypatch.setattr("builtins.input", _fail_input)

        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "--stdin"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "typed body") in calls
        assert ("send-keys", "-t", "0:5", "Enter") in calls

    def test_send_double_dash_ends_flag_parsing(self, monkeypatch):
        """`--` makes subsequent args positional even if they start with `-`."""
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "--", "--force-looking-message"])
        calls = self._tmux_calls(mock_tmux)
        assert (
            "send-keys", "-t", "0:5", "-l", "--force-looking-message"
        ) in calls

    def test_send_resolves_numeric_index(self, monkeypatch):
        project = self._make_project(win_target="0:7")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["7", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert any("0:7" in c for c in calls)

    def test_send_resolves_hash_index(self, monkeypatch):
        project = self._make_project(win_target="0:7")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["#7", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert any("0:7" in c for c in calls)

    # --- state gating ---

    def test_send_permit_rejected(self, monkeypatch):
        """PERMIT state is a hard guard — refuse unconditionally."""
        project = self._make_project(state="PERMIT")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_core.cmd_send(["blog", "hello"])

    def test_send_permit_rejected_even_with_force(self, monkeypatch):
        """Even --force cannot override a PERMIT guard."""
        project = self._make_project(state="PERMIT")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_core.cmd_send(["blog", "--force", "hello"])

    def test_send_busy_rejected_without_force(self, monkeypatch):
        project = self._make_project(state="BUSY")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_core.cmd_send(["blog", "hello"])

    def test_send_busy_allowed_with_force(self, monkeypatch):
        project = self._make_project(state="BUSY")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "--force", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hello") in calls

    def test_send_shell_rejected_without_start(self, monkeypatch):
        project = self._make_project(state="SHELL")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_core.cmd_send(["blog", "hello"])

    def test_send_shell_with_start_launches_claude_first(self, monkeypatch):
        project = self._make_project(state="SHELL")
        self._patch_resolution(monkeypatch, project=project)
        monkeypatch.setattr("time.sleep", lambda _s: None)  # skip the 2s wait
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # Claude launch command appears before the message payload.
        # The call tuple is ("send-keys", "-t", target, CLAUDE_CMD, "Enter")
        claude_i = next(
            (i for i, c in enumerate(calls) if ccm_core.CLAUDE_CMD in c),
            None,
        )
        literal_i = next(
            (i for i, c in enumerate(calls)
             if c == ("send-keys", "-t", "0:5", "-l", "hello")),
            None,
        )
        assert claude_i is not None, "Claude launch not issued"
        assert literal_i is not None, "Message not sent"
        assert claude_i < literal_i

    def test_send_idle_state_allowed(self, monkeypatch):
        project = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_core.cmd_send(["blog", "hi"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hi") in calls

    # --- error paths ---

    def test_send_unknown_project_rejected(self, monkeypatch):
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(ccm_core, "find_window", lambda s, n: None)
        monkeypatch.setattr(ccm_core, "build_project_list", lambda fast=False: [])
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_core.cmd_send(["nonexistent", "hi"])

    def test_send_no_target_rejected(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(SystemExit):
            ccm_core.cmd_send([])

    def test_send_empty_message_rejected(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_core.cmd_send(["blog", "   "])

    def test_send_dual_source_rejected(self, monkeypatch, tmp_path):
        """Positional message + --file is an error (exactly one source)."""
        self._patch_resolution(monkeypatch)
        f = tmp_path / "m.txt"
        f.write_text("from file")
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_core.cmd_send(["blog", "positional", "--file", str(f)])

    def test_send_unknown_flag_rejected(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_core.cmd_send(["blog", "--nope", "hi"])
