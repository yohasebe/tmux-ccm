"""Tests for ccm_jsonl.

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
import ccm_constants
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

class TestJsonlFreshness:
    def setup_method(self):
        # Reset the in-process caches so tests do not leak across cases
        ccm_jsonl._jsonl_path_cache.clear()
        ccm_jsonl._jsonl_activity_cache.clear()

    def teardown_method(self):
        ccm_jsonl._jsonl_path_cache.clear()
        ccm_jsonl._jsonl_activity_cache.clear()

    def test_slug_simple(self):
        assert ccm_jsonl._project_slug("/Users/yo/code/foo") == "-Users-yo-code-foo"

    def test_slug_with_tilde(self):
        # ~ should expand; trailing slash and structure preserved
        slug = ccm_jsonl._project_slug("~/code/foo")
        home = os.path.expanduser("~")
        assert slug == (home + "/code/foo").replace("/", "-")

    def test_slug_non_ascii_chars_each_become_dash(self):
        """Claude Code dashes EVERY non-alphanumeric character, one
        dash per char — including multi-byte CJK. ccm used to replace
        only `/`, which missed the JSONL for any non-ASCII project
        path entirely: an unresolvable JSONL means a trailing `stop`
        event cannot be confirmed terminal, producing an indefinite
        false BUSY that even combined-stale cannot release (it needs
        a valid jsonl_age). The placeholder path below is fictional
        (hoge/fuga/piyo dummies); it exercises pure hiragana, a
        CJK+digit segment (digits survive), and a CJK+ASCII segment."""
        assert ccm_jsonl._project_slug(
            "/Users/example/ほげ/ふが2000/ぴよAB"
        ) == "-Users-example------2000---AB"

    def test_slug_underscore_becomes_dash(self):
        # Verified against a real slug: code/test_project →
        # ...-code-test-project in ~/.claude/projects.
        assert ccm_jsonl._project_slug(
            "/Users/yo/code/test_project") == "-Users-yo-code-test-project"

    def test_slug_dots_and_spaces_become_dashes(self):
        assert ccm_jsonl._project_slug(
            "/Users/yo/my proj/v1.2") == "-Users-yo-my-proj-v1-2"

    def test_age_minus_one_when_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        assert ccm_jsonl.read_jsonl_age("/nonexistent/path/foo") == -1

    def test_age_minus_one_when_no_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_jsonl._project_slug("/x/y")
        (tmp_path / slug).mkdir()
        # empty dir
        assert ccm_jsonl.read_jsonl_age("/x/y") == -1

    def test_age_reads_newest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_jsonl._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        old = d / "old.jsonl"
        new = d / "new.jsonl"
        now = time.time()
        write_jsonl(old, [real_activity_record(now - 1000)])
        write_jsonl(new, [real_activity_record(now - 3)])
        os.utime(old, (now - 1000, now - 1000))
        os.utime(new, (now - 3, now - 3))
        age = ccm_jsonl.read_jsonl_age("/x/y")
        assert 2 <= age <= 5  # newest record is ~3s old

    def test_for_session_reads_specific_file_not_newest(
        self, tmp_path, monkeypatch):
        """`read_jsonl_tail_info_for_session` must read the named
        session's JSONL even when another session in the same slug dir
        (a same-cwd sidekick) wrote more recently. Newest-by-mtime
        would return the sidekick's fresh activity; the session-scoped
        read returns the tracked session's (stale) activity."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_jsonl._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        now = time.time()
        main = d / "sid-main.jsonl"
        side = d / "sid-side.jsonl"       # the sidekick, freshest
        write_jsonl(main, [real_activity_record(now - 900)])
        write_jsonl(side, [real_activity_record(now - 2)])
        os.utime(main, (now - 900, now - 900))
        os.utime(side, (now - 2, now - 2))
        age, _stop = ccm_jsonl.read_jsonl_tail_info_for_session(
            "/x/y", "sid-main")
        assert 890 <= age <= 910, "must read sid-main (stale), not the sidekick"

    def test_for_session_missing_file_returns_minus_one(
        self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        (tmp_path / ccm_jsonl._project_slug("/x/y")).mkdir()
        assert ccm_jsonl.read_jsonl_tail_info_for_session(
            "/x/y", "sid-absent") == (-1, None)

    def test_ignores_non_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_jsonl._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        (d / "foo.txt").write_text("ignored")
        (d / "bar.log").write_text("ignored")
        assert ccm_jsonl.read_jsonl_age("/x/y") == -1

    def test_cache_returns_stable_path(self, tmp_path, monkeypatch):
        """Calling twice should not re-listdir if cache is hot.
        We assert by deleting the dir between calls — second call
        still returns the cached path's age (until path vanishes)."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_jsonl._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        f = d / "session.jsonl"
        write_jsonl(f, [real_activity_record(time.time())])
        a1 = ccm_jsonl.read_jsonl_age("/x/y")
        assert a1 >= 0
        # Cache hit on second call: same file, same age (within 1s)
        a2 = ccm_jsonl.read_jsonl_age("/x/y")
        assert abs(a2 - a1) <= 1

    def test_cache_recovers_when_file_disappears(self, tmp_path, monkeypatch):
        """If the cached file is deleted, the next call must re-glob
        and either find a replacement or return -1."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_jsonl._project_slug("/x/y")
        d = tmp_path / slug
        d.mkdir()
        f = d / "session.jsonl"
        write_jsonl(f, [real_activity_record(time.time())])
        assert ccm_jsonl.read_jsonl_age("/x/y") >= 0
        f.unlink()
        # No replacement → -1
        assert ccm_jsonl.read_jsonl_age("/x/y") == -1


# ─── JSONL real-activity filter ───
#
# These tests exercise read_jsonl_age()'s filtering of Claude Code
# housekeeping records (recap / `away_summary`, `turn_duration`,
# `attachment`, …) so that they do not register as fresh activity.

class TestJsonlRealActivityFilter:
    def setup_method(self):
        ccm_jsonl._jsonl_path_cache.clear()
        ccm_jsonl._jsonl_activity_cache.clear()

    def teardown_method(self):
        ccm_jsonl._jsonl_path_cache.clear()
        ccm_jsonl._jsonl_activity_cache.clear()

    def _setup_project(self, tmp_path, monkeypatch, project_dir="/p/q"):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_jsonl._project_slug(project_dir)
        d = tmp_path / slug
        d.mkdir()
        return d / "session.jsonl"

    def test_returns_age_of_user_record(self, tmp_path, monkeypatch):
        """A JSONL whose tail is a single user record returns that
        record's age."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [real_activity_record(now - 4, role="user")])
        age = ccm_jsonl.read_jsonl_age("/p/q")
        assert 3 <= age <= 6

    def test_skips_away_summary_recap(self, tmp_path, monkeypatch):
        """The recap (system/away_summary) record at the end of the
        file is skipped; the previous assistant record's timestamp is
        used. This is the core recap-skip scenario."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            real_activity_record(now - 200, role="assistant"),
            system_record(now - 1, subtype="away_summary"),
        ])
        age = ccm_jsonl.read_jsonl_age("/p/q")
        # Should reflect the assistant record (~200s), not the recap (~1s)
        assert 195 <= age <= 210

    def test_skips_multiple_trailing_system_records(self, tmp_path, monkeypatch):
        """Real captured tail had stop_hook_summary + turn_duration +
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
        age = ccm_jsonl.read_jsonl_age("/p/q")
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
        age = ccm_jsonl.read_jsonl_age("/p/q")
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
        assert ccm_jsonl.read_jsonl_age("/p/q") == -1

    def test_no_timestamp_record_is_skipped(self, tmp_path, monkeypatch):
        """Records without a parseable `timestamp` are NOT counted as
        real activity. Claude Code emits housekeeping records
        (`permission-mode`, `file-history-snapshot`, `last-prompt`)
        at `--continue` startup that lack a timestamp field. ccm
        only treats records in the `JSONL_ACTIVITY_TYPES` whitelist
        with a parseable timestamp as activity; this test asserts
        the timestamp guard directly so a whitelisted record that
        somehow loses its timestamp cannot promote via mtime.
        """
        f = self._setup_project(tmp_path, monkeypatch)
        # Hypothetical future "real" type with a malformed record
        # (no timestamp). Must NOT promote via mtime.
        write_jsonl(f, [{"type": "user", "message": {"content": "no ts"}}])
        age = ccm_jsonl.read_jsonl_age("/p/q")
        assert age == -1

    def test_caches_by_mtime(self, tmp_path, monkeypatch):
        """Two calls with no file write between them should hit the
        cache. Verified by patching open() on the second call: cache
        miss would trigger a real file read; cache hit avoids it."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [real_activity_record(now - 2)])
        a1 = ccm_jsonl.read_jsonl_age("/p/q")
        # Patch open to detect re-reads (the cache should prevent this).
        opens = []
        real_open = open
        def tracking_open(path, *a, **kw):
            opens.append(str(path))
            return real_open(path, *a, **kw)
        with patch("builtins.open", side_effect=tracking_open):
            a2 = ccm_jsonl.read_jsonl_age("/p/q")
        assert abs(a2 - a1) <= 1
        # The JSONL file itself should NOT have been re-opened on the
        # second call (cache hit).
        assert str(f) not in opens

    def test_invalidates_cache_on_mtime_change(self, tmp_path, monkeypatch):
        """A new file write must invalidate the cache and re-parse."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [real_activity_record(now - 100)])
        a1 = ccm_jsonl.read_jsonl_age("/p/q")
        assert 95 <= a1 <= 110
        # Append a fresh record. New mtime → cache invalidates.
        write_jsonl(f, [
            real_activity_record(now - 100),
            real_activity_record(now - 1),
        ])
        # Force a different mtime so the cache key changes.
        os.utime(f, (now, now))
        # Also clear the path cache so _find_newest_jsonl re-checks.
        ccm_jsonl._jsonl_path_cache.clear()
        a2 = ccm_jsonl.read_jsonl_age("/p/q")
        assert a2 <= 3

    def test_handles_malformed_json_lines(self, tmp_path, monkeypatch):
        """Garbage lines in the middle of the tail are skipped; the
        next valid real-activity record is found."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        valid = json.dumps(real_activity_record(now - 5))
        f.write_text(valid + "\nnot-json-at-all\n{also bad\n")
        age = ccm_jsonl.read_jsonl_age("/p/q")
        # The valid record is at -5, garbage after it is skipped.
        assert 4 <= age <= 8

    def test_skips_startup_housekeeping_records(self, tmp_path, monkeypatch):
        """Claude Code writes a burst of `permission-mode`,
        `file-history-snapshot`, and `last-prompt` records (none of
        which carry timestamps) at `--continue` startup. ccm must not
        treat them as real activity, otherwise an attach to a SHELL
        window would show ~10 s of false BUSY while MCP loads.
        """
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        # Simulate a --continue startup tail: a real assistant record
        # from hours ago (end of the previous turn), followed by the
        # burst of no-timestamp housekeeping records written during
        # startup.
        write_jsonl(f, [
            real_activity_record(now - 3600, role="assistant"),
            {"type": "permission-mode", "permissionMode": "default",
             "sessionId": "xyz"},
            {"type": "file-history-snapshot", "messageId": "m1",
             "snapshot": {}, "isSnapshotUpdate": False},
            {"type": "last-prompt"},
            {"type": "permission-mode", "permissionMode": "default",
             "sessionId": "xyz"},
        ])
        age = ccm_jsonl.read_jsonl_age("/p/q")
        # Age must reflect the assistant record (~3600 s), not the
        # housekeeping burst (~0 s).
        assert 3590 <= age <= 3620, (
            f"jsonl_age={age}: housekeeping records leaked through the filter"
        )


# ─── JSONL tail stop_reason extraction ───
#
# read_jsonl_tail_info returns (age, last_assistant_stop_reason). The
# stop_reason is what the event-log detection path
# keys on to hold BUSY authoritatively across tool-turn boundaries,
# uses to hold BUSY across tool-turn boundaries.

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
        ccm_jsonl._jsonl_path_cache.clear()
        ccm_jsonl._jsonl_activity_cache.clear()

    def teardown_method(self):
        ccm_jsonl._jsonl_path_cache.clear()
        ccm_jsonl._jsonl_activity_cache.clear()

    def _setup_project(self, tmp_path, monkeypatch, project_dir="/p/q"):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        slug = ccm_jsonl._project_slug(project_dir)
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
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert 4 <= age <= 7
        assert stop == "tool_use"

    def test_returns_end_turn_from_latest_assistant(self, tmp_path, monkeypatch):
        """Response completed normally: stop_reason='end_turn'."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 2, stop_reason="end_turn"),
        ])
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop == "end_turn"

    def test_promotes_to_user_pending_after_terminal_assistant(
        self, tmp_path, monkeypatch
    ):
        """User submitted a fresh prompt after a terminal assistant
        record. claude is now processing the new prompt (extended-
        thinking phase, no new assistant record yet). The synthetic
        `user_pending` stop_reason signals this so detection knows
        to surface BUSY rather than treating the stale `end_turn`
        as authoritative."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 60, stop_reason="end_turn"),
            {"type": "user", "timestamp": _iso_ts(now - 30),
             "message": {"content": "next question"}},
        ])
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop == ccm_jsonl.JSONL_USER_PENDING

    def test_does_not_promote_when_assistant_was_tool_use(
        self, tmp_path, monkeypatch
    ):
        """User record after assistant `tool_use` is a tool_result,
        not a fresh prompt. Stop_reason stays at `tool_use`."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 30, stop_reason="tool_use"),
            {"type": "user", "timestamp": _iso_ts(now - 10),
             "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        ])
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop == "tool_use"

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
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        # age is of the newest real record (user tool_result)
        assert 1 <= age <= 4
        # stop_reason comes from the assistant one record back
        assert stop == "tool_use"

    def test_slash_command_records_do_not_promote_to_user_pending(
        self, tmp_path, monkeypatch
    ):
        """A local slash command (/model, /status, …) writes up to
        three `user` records: <command-name> (isMeta absent),
        <local-command-stdout> (isMeta absent), and
        <local-command-caveat> (isMeta: true). None triggers an
        assistant turn, so NONE must promote to user_pending —
        otherwise running a slash command while idle falsely shows
        BUSY for ~10 min. This mirrors the exact real-world /model
        sequence observed."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 60, stop_reason="end_turn"),
            {"type": "user", "timestamp": _iso_ts(now - 5),
             "message": {"content": "<command-name>/model</command-name>\n"
                                    "<command-message>model</command-message>\n"
                                    "<command-args></command-args>"}},
            {"type": "user", "timestamp": _iso_ts(now - 5),
             "message": {"content": "<local-command-stdout>Set model to Fable 5"
                                    "</local-command-stdout>"}},
            {"type": "user", "timestamp": _iso_ts(now - 4), "isMeta": True,
             "message": {"content": "<local-command-caveat>Caveat:…"
                                    "</local-command-caveat>"}},
        ])
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        # All three slash-command records skipped: stop_reason stays
        # end_turn (→ IDLE), not user_pending (→ false BUSY).
        assert stop == "end_turn"

    def test_real_prompt_still_promotes_despite_prior_slash_command(
        self, tmp_path, monkeypatch
    ):
        """A genuine prompt after slash-command records still promotes
        to user_pending — the skip must not suppress real prompts."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 60, stop_reason="end_turn"),
            {"type": "user", "timestamp": _iso_ts(now - 20),
             "message": {"content": "<command-name>/model</command-name>"}},
            {"type": "user", "timestamp": _iso_ts(now - 5),
             "message": {"content": "real follow-up question"}},
        ])
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop == ccm_jsonl.JSONL_USER_PENDING

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
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
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
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop is None

    def test_returns_none_when_assistant_lacks_stop_reason(
        self, tmp_path, monkeypatch
    ):
        """Older schema or partial record without a stop_reason → None,
        not a falsy empty string or crash."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [assistant_record(now - 2, stop_reason=None)])
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop is None

    def test_returns_none_when_jsonl_missing(self, tmp_path, monkeypatch):
        """No JSONL file at all: (-1, None)."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        age, stop = ccm_jsonl.read_jsonl_tail_info("/nonexistent")
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
        ccm_jsonl.read_jsonl_age("/p/q")
        opens = []
        real_open = open
        def tracking_open(path, *a, **kw):
            opens.append(str(path))
            return real_open(path, *a, **kw)
        with patch("builtins.open", side_effect=tracking_open):
            age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop == "tool_use"
        assert str(f) not in opens, (
            "read_jsonl_tail_info re-opened JSONL despite cache being primed"
        )

class TestReadSessionInfo:
    def test_none_when_no_pid(self):
        assert ccm_jsonl.read_session_info("") is None
        assert ccm_jsonl.read_session_info(None) is None

    def test_reads_session_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "12345.json").write_text(
            '{"pid":12345,"sessionId":"abc-def","cwd":"/tmp/proj",'
            '"startedAt":1776048000000,"kind":"interactive","entrypoint":"cli"}'
        )
        info = ccm_jsonl.read_session_info("12345")
        assert info["sessionId"] == "abc-def"
        assert info["cwd"] == "/tmp/proj"
        assert info["kind"] == "interactive"

    def test_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        assert ccm_jsonl.read_session_info("99999") is None

    def test_none_on_malformed_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "1.json").write_text("not json")
        assert ccm_jsonl.read_session_info("1") is None

    def test_pid_reuse_staleness_check_rejects_old_session(
            self, tmp_path, monkeypatch):
        """If `startedAt` in the json predates the live process's
        etime-derived start time by more than the drift tolerance,
        the file belongs to a previous session whose pid was
        recycled. read_session_info must return None so the caller
        falls through to legacy detection rather than reading
        the wrong session's events."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        # Live process: etime=10s → started 10s ago
        # File: startedAt = 1 hour ago (very different)
        now = int(time.time())
        old_started_ms = (now - 3600) * 1000
        (tmp_path / "12345.json").write_text(
            f'{{"pid":12345,"sessionId":"stale-uuid",'
            f'"cwd":"/tmp/p","startedAt":{old_started_ms}}}'
        )
        ps_lines = [f"12345 99 12345 claude 00:10"]  # etime=10s
        # Without ps_lines: file is accepted (no cross-check)
        assert ccm_jsonl.read_session_info("12345")["sessionId"] == "stale-uuid"
        # With ps_lines: rejected as stale
        assert ccm_jsonl.read_session_info("12345", ps_lines=ps_lines) is None

    def test_pid_staleness_check_accepts_current_session(
            self, tmp_path, monkeypatch):
        """startedAt within the drift tolerance of the live process's
        start time → accept. Normal case for an actively-running
        Claude session."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        now = int(time.time())
        # Live process: etime=10s, started 10s ago (live_started ≈ now-10)
        # File: startedAt = now - 12s (2s drift, within 10s tolerance)
        recent_started_ms = (now - 12) * 1000
        (tmp_path / "555.json").write_text(
            f'{{"pid":555,"sessionId":"current-uuid",'
            f'"cwd":"/tmp/p","startedAt":{recent_started_ms}}}'
        )
        ps_lines = ["555 99 555 claude 00:10"]
        info = ccm_jsonl.read_session_info("555", ps_lines=ps_lines)
        assert info is not None
        assert info["sessionId"] == "current-uuid"

    def test_pid_staleness_check_skipped_when_etime_unknown(
            self, tmp_path, monkeypatch):
        """If find_process_age can't parse etime (-1), the
        cross-check is silently skipped — we accept the file.
        Otherwise a malformed ps row would erase a perfectly good
        session_info read."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        now = int(time.time())
        old_started_ms = (now - 3600) * 1000  # would be rejected with valid etime
        (tmp_path / "777.json").write_text(
            f'{{"pid":777,"sessionId":"u",'
            f'"cwd":"/tmp/p","startedAt":{old_started_ms}}}'
        )
        # ps line missing the etime column (find_process_age returns -1)
        ps_lines = ["777 99 777 claude"]
        info = ccm_jsonl.read_session_info("777", ps_lines=ps_lines)
        assert info is not None  # accepted because etime unknown


class TestJsonlFromSessionInfo:
    def test_resolves_exact_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path / "sessions"))
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "500.json").write_text(
            '{"pid":500,"sessionId":"s-1","cwd":"/x/y","kind":"interactive"}'
        )
        slug_dir = tmp_path / "projects" / "-x-y"
        slug_dir.mkdir(parents=True)
        expected = slug_dir / "s-1.jsonl"
        expected.write_text("{}\n")

        path = ccm_jsonl._jsonl_from_session_info("500")
        assert path == str(expected)

    def test_returns_none_for_headless_session(self, tmp_path, monkeypatch):
        """kind='cli' (headless -p mode) should be skipped — ccm tracks
        only interactive sessions."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "600.json").write_text(
            '{"pid":600,"sessionId":"s-2","cwd":"/a/b","kind":"cli"}'
        )
        assert ccm_jsonl._jsonl_from_session_info("600") is None

    def test_returns_none_when_jsonl_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path / "sessions"))
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "700.json").write_text(
            '{"pid":700,"sessionId":"s-3","cwd":"/p/q","kind":"interactive"}'
        )
        # projects dir empty — no matching jsonl
        assert ccm_jsonl._jsonl_from_session_info("700") is None

    def test_age_uses_session_info_path(self, tmp_path, monkeypatch):
        """read_jsonl_age prefers the session-info resolution when
        claude_pid is provided, even if the slug-based lookup would
        find a different newest file."""
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path / "sessions"))
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
        ccm_jsonl._jsonl_path_cache.clear()
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
        age = ccm_jsonl.read_jsonl_age("/w/x", claude_pid="800")
        assert 9 <= age <= 12

        ccm_jsonl._jsonl_path_cache.clear()
        ccm_jsonl._jsonl_activity_cache.clear()
        # Without pid: slug-based scan picks the newest by mtime (other.jsonl)
        age2 = ccm_jsonl.read_jsonl_age("/w/x")
        assert age2 <= 2


class TestReadSessionVersions:
    """`read_session_versions` builds a sessionId→version map from
    every `~/.claude/sessions/*.json` file. Used by `ccm doctor`
    to surface the per-session Claude Code version (catches the
    "auto-update mid-day" case where windows end up on different
    versions)."""

    def test_empty_dir_returns_empty_map(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        assert ccm_jsonl.read_session_versions() == {}

    def test_collects_session_id_to_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "100.json").write_text(
            '{"pid":100,"sessionId":"sid-a","version":"2.1.126"}'
        )
        (tmp_path / "200.json").write_text(
            '{"pid":200,"sessionId":"sid-b","version":"2.1.127"}'
        )
        m = ccm_jsonl.read_session_versions()
        assert m == {"sid-a": "2.1.126", "sid-b": "2.1.127"}

    def test_skips_malformed_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "100.json").write_text("not json")
        (tmp_path / "200.json").write_text(
            '{"sessionId":"sid-ok","version":"2.1.126"}'
        )
        m = ccm_jsonl.read_session_versions()
        assert m == {"sid-ok": "2.1.126"}

    def test_skips_files_without_sessionid_or_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "no-sid.json").write_text('{"version":"2.1.126"}')
        (tmp_path / "no-ver.json").write_text('{"sessionId":"sid-x"}')
        (tmp_path / "ok.json").write_text(
            '{"sessionId":"sid-y","version":"2.1.126"}'
        )
        m = ccm_jsonl.read_session_versions()
        assert m == {"sid-y": "2.1.126"}

    def test_non_string_values_ignored(self, tmp_path, monkeypatch):
        # Defensive: future Claude Code versions might emit different
        # types — we should only accept str→str entries, never crash.
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "100.json").write_text(
            '{"sessionId":123,"version":"2.1.126"}'
        )
        assert ccm_jsonl.read_session_versions() == {}

    def test_ansi_escapes_stripped(self, tmp_path, monkeypatch):
        # Defense-in-depth: a malformed/tampered session JSON must
        # not inject ANSI colour codes into the doctor output.
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "100.json").write_text(
            '{"sessionId":"sid-\\u001b[31mevil","version":"2\\u001b[0m.1.126"}'
        )
        m = ccm_jsonl.read_session_versions()
        assert m == {"sid-evil": "2.1.126"}



class TestJsonlEscInterruptRecord:
    """Esc-interrupt notes in the transcript.

    Claude Code fires no Stop hook on a user interrupt — long taken to
    mean an interrupted turn leaves no trace at all, which is why
    detection had to wait out an aging guard with an idle screen. It
    does leave a trace: a `user` record reading "[Request interrupted
    by user…]" (measured, 8 occurrences in one session).
    Read correctly it is the missing terminal; read naively it is
    worse than nothing, because it is NEWER than the assistant turn it
    cut short and would promote to `user_pending`.
    """

    def setup_method(self):
        ccm_jsonl._jsonl_path_cache.clear()
        ccm_jsonl._jsonl_activity_cache.clear()

    teardown_method = setup_method

    def _setup_project(self, tmp_path, monkeypatch, project_dir="/p/q"):
        monkeypatch.setattr(ccm_jsonl, "CLAUDE_PROJECTS_DIR", str(tmp_path))
        d = tmp_path / ccm_jsonl._project_slug(project_dir)
        d.mkdir()
        return d / "session.jsonl"

    @staticmethod
    def _interrupt(ts, text="[Request interrupted by user]"):
        return {"type": "user", "timestamp": _iso_ts(ts),
                "message": {"role": "user",
                            "content": [{"type": "text", "text": text}]}}

    @pytest.mark.parametrize("text", [
        "[Request interrupted by user]",
        "[Request interrupted by user for tool use]",
    ])
    def test_interrupt_overrides_the_cut_short_stop_reason(
            self, tmp_path, monkeypatch, text):
        """The interrupted assistant record says `tool_use` — the one
        value with no release path. The interrupt is newer, and ends
        the turn."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 300, stop_reason="tool_use"),
            self._interrupt(now - 5, text),
        ])
        _age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop == ccm_constants.JSONL_INTERRUPTED

    def test_interrupt_does_not_count_as_activity(
            self, tmp_path, monkeypatch):
        """Counting it would reset the aging guard's clock to the
        moment of the interrupt — restarting the very wait the release
        is trying to end."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 300, stop_reason="tool_use"),
            self._interrupt(now - 5),
        ])
        age, _stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert age >= 290, "the interrupt note refreshed the activity age"

    def test_a_real_prompt_after_the_interrupt_wins(
            self, tmp_path, monkeypatch):
        """Esc then a new prompt: the session is working again, and
        must not read as interrupted."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 300, stop_reason="tool_use"),
            self._interrupt(now - 60),
            {"type": "user", "timestamp": _iso_ts(now - 2),
             "message": {"content": "next task please"}},
        ])
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop != ccm_constants.JSONL_INTERRUPTED
        assert age <= 5, "the new prompt is real activity"

    def test_an_assistant_turn_after_the_interrupt_wins(
            self, tmp_path, monkeypatch):
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 300, stop_reason="tool_use"),
            self._interrupt(now - 60),
            assistant_record(now - 2, stop_reason="end_turn"),
        ])
        _age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop == "end_turn"

    @pytest.mark.parametrize("text", [
        "why does Request interrupted appear twice?",
        "the marker is [Request interrupted by user] - see backlog",
        "[Request interrupted by user] and then I typed this",
    ])
    def test_a_message_merely_mentioning_the_phrase_is_a_prompt(
            self, tmp_path, monkeypatch, text):
        """Matching as a substring released working sessions.

        Claude's note is the ENTIRE content of its record, so anything
        with the phrase embedded in a longer message is a human
        talking about interrupts, not an interrupt. Getting this wrong
        is a false IDLE — the dangerous direction: `ccm send` would
        deliver into a working session and auto-exit could eventually
        kill it. Not hypothetical: a naive substring scan of the
        session that built this feature returned 41 hits for 7 real
        interrupts, all the extras being its own discussion of the
        marker (independently hit downstream)."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 300, stop_reason="tool_use"),
            {"type": "user", "timestamp": _iso_ts(now - 5),
             "message": {"role": "user",
                         "content": [{"type": "text", "text": text}]}},
        ])
        age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop != ccm_constants.JSONL_INTERRUPTED
        assert age <= 10, "a real message must count as activity"

    def test_bare_interrupt_variant_is_recognised(
            self, tmp_path, monkeypatch):
        """A third spelling, "[Request interrupted]", appears in
        a consumer's corpus of ~165. The trailing clause is the part that
        gets reworded, so it is left open while the anchoring is
        not."""
        f = self._setup_project(tmp_path, monkeypatch)
        now = time.time()
        write_jsonl(f, [
            assistant_record(now - 300, stop_reason="tool_use"),
            self._interrupt(now - 5, "[Request interrupted]"),
        ])
        _age, stop = ccm_jsonl.read_jsonl_tail_info("/p/q")
        assert stop == ccm_constants.JSONL_INTERRUPTED
