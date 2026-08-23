"""Tests for ccm_rules.

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

class TestFourStateModel:
    """Tests defining the 4-state detection model (PERMIT/BUSY/IDLE/SHELL).
    DONE is not a detection state."""

    def test_no_done_in_state_priority(self):
        """DONE should not appear in STATE_PRIORITY or STATE_ICONS."""
        assert "DONE" not in ccm_core.STATE_PRIORITY
        assert "DONE" not in ccm_core.STATE_ICONS

    def test_no_done_in_valid_hook_states(self):
        """DONE is not a valid hook state (Stop hook deletes the
        signal file rather than writing DONE)."""
        assert "DONE" not in ccm_signals.VALID_HOOK_STATES

    def test_completed_at_set_on_busy_to_idle(self):
        """apply_actions sets @ccm_completed_at when transitioning
        from BUSY to IDLE."""
        rule = ccm_rules.Rule(name="t", result="IDLE", action=ccm_rules.Action.DEFAULT)
        ctx = make_ctx(prev_state="BUSY")
        with patch.object(ccm_detection, "_set_win_state"):
            with patch.object(ccm_core, "tmux_cmd") as mock_tmux:
                ccm_detection.apply_actions("0:1", "/tmp/proj", ctx, rule, "IDLE")
        completed_calls = [c for c in mock_tmux.call_args_list
                           if len(c[0]) > 3 and "@ccm_completed_at" in str(c[0])]
        assert len(completed_calls) > 0

    def test_completed_at_set_on_permit_to_idle(self):
        """PERMIT -> IDLE also sets the completion marker."""
        rule = ccm_rules.Rule(name="t", result="IDLE", action=ccm_rules.Action.DEFAULT)
        ctx = make_ctx(prev_state="PERMIT")
        with patch.object(ccm_detection, "_set_win_state"):
            with patch.object(ccm_core, "tmux_cmd") as mock_tmux:
                ccm_detection.apply_actions("0:1", "/tmp/proj", ctx, rule, "IDLE")
        completed_calls = [c for c in mock_tmux.call_args_list
                           if len(c[0]) > 3 and "@ccm_completed_at" in str(c[0])]
        assert len(completed_calls) > 0

    def test_completed_at_not_set_on_idle_to_idle(self):
        """IDLE -> IDLE does NOT set the marker (no transition)."""
        rule = ccm_rules.Rule(name="t", result="IDLE", action=ccm_rules.Action.DEFAULT)
        ctx = make_ctx(prev_state="IDLE")
        with patch.object(ccm_detection, "_set_win_state"):
            with patch.object(ccm_core, "tmux_cmd") as mock_tmux:
                ccm_detection.apply_actions("0:1", "/tmp/proj", ctx, rule, "IDLE")
        completed_calls = [c for c in mock_tmux.call_args_list
                           if len(c[0]) > 3 and "@ccm_completed_at" in str(c[0])]
        assert len(completed_calls) == 0


class TestRulePhaseAnnotations:
    """Drift guard: every rule in DETECTION_RULES must declare its
    session-lifecycle phase (or explicitly None for catch-all
    passthroughs). Phase metadata is consumed by `ccm debug trace`
    and CCM_DEBUG_TRACE; a missing or typo-ed phase would silently
    break the debug output grouping.
    """

    # Catch-all rules that legitimately lack a fixed phase because
    # their semantic phase depends on ctx.raw at fire time.
    # `raw_busy_passthrough` and `raw_permit_passthrough` carry
    # concrete `midturn` / `permit` phases, so they are NOT in
    # this set.
    EXPECTED_NONE = {"default"}

    def test_every_rule_has_phase_or_is_listed_passthrough(self):
        import ccm_rules as det
        for rule in ccm_rules.DETECTION_RULES:
            if rule.name in self.EXPECTED_NONE:
                assert rule.phase is None, (
                    f"{rule.name} is registered as a passthrough but "
                    f"has phase={rule.phase!r}"
                )
            else:
                assert rule.phase is not None, (
                    f"{rule.name} has no phase annotation. Add one of "
                    f"{det.PHASES} to the Rule() call, or add the rule "
                    f"to TestRulePhaseAnnotations.EXPECTED_NONE if it "
                    f"is genuinely a catch-all passthrough."
                )
                assert rule.phase in det.PHASES, (
                    f"{rule.name} has phase={rule.phase!r} which is "
                    f"not a recognized PHASE value {det.PHASES}"
                )

    def test_specific_rule_phase_classifications(self):
        """Document the expected phase for each named rule. When a
        rule's phase intentionally changes, update this mapping
        (and ccm_detection.py) together so the intent is explicit
        in two places."""
        expected = {
            "process_down": "shell",
            "process_shell": "shell",
            "hook_fresh_busy": "midturn",
            "startup_transient_raw_busy": "startup",
            "raw_busy_passthrough": "midturn",
            "jsonl_user_prompt_pending": "midturn",
            "raw_permit_passthrough": "permit",
            "default": None,
        }
        actual = {r.name: r.phase for r in ccm_rules.DETECTION_RULES}
        assert actual == expected, (
            "Rule phase classifications drifted from the documented "
            "mapping. Update both ccm_detection.py and this test."
        )


class TestEvaluateRules:
    """Pure unit tests: each case asserts (matched_rule_name, resolved_state).

    No tmux, ps, or filesystem mocking — the Context is built directly.
    """

    # --- process-level ---

    def test_raw_down(self):
        rule, state = ccm_rules.evaluate_rules(make_ctx(raw="DOWN"))
        assert (rule.name, state) == ("process_down", "DOWN")

    def test_raw_shell(self):
        rule, state = ccm_rules.evaluate_rules(make_ctx(raw="SHELL"))
        assert (rule.name, state) == ("process_shell", "SHELL")

    def test_shell_beats_hook_busy(self):
        """Process tree authoritative: SHELL beats any hook signal."""
        rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw="SHELL", hook_state="BUSY", hook_age=0)
        )
        assert (rule.name, state) == ("process_shell", "SHELL")

    # --- fresh BUSY hook fast path ---

    def test_hook_fresh_busy_over_raw_idle(self):
        # Real UserPromptSubmit timing: hook fires and the user record
        # is written to JSONL essentially simultaneously, so the gap
        # between hook and last real activity is ~0.
        rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw="IDLE", hook_state="BUSY", hook_age=1, jsonl_age=0)
        )
        assert (rule.name, state) == ("hook_fresh_busy", "BUSY")

    def test_hook_fresh_busy_over_raw_busy_is_noop(self):
        # Same realistic setup: gap=0 between hook and JSONL.
        rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw="BUSY", hook_state="BUSY", hook_age=0, jsonl_age=0)
        )
        assert (rule.name, state) == ("hook_fresh_busy", "BUSY")

    # --- raw_busy_passthrough / raw_permit_passthrough ---

    def test_raw_busy_passes_through(self):
        rule, state = ccm_rules.evaluate_rules(make_ctx(raw="BUSY"))
        assert (rule.name, state) == ("raw_busy_passthrough", "BUSY")

    def test_raw_permit_passes_through(self):
        rule, state = ccm_rules.evaluate_rules(make_ctx(raw="PERMIT"))
        assert (rule.name, state) == ("raw_permit_passthrough", "PERMIT")

    # --- jsonl_user_prompt_pending ---

    def test_jsonl_user_pending_promotes_idle_to_busy(self):
        """User submitted a new prompt after a terminal assistant
        record; claude is processing it (extended thinking, no
        new assistant record yet). raw=IDLE because `❯` is visible
        in accept-edits mode. ccm must surface BUSY."""
        rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw="IDLE",
                     jsonl_last_stop_reason="user_pending",
                     jsonl_age=10)
        )
        assert (rule.name, state) == ("jsonl_user_prompt_pending", "BUSY")

    def test_jsonl_user_pending_gives_up_with_the_event_log(self):
        """Both paths stop claiming a turn is running at the same
        moment. The event-log path abstains past its release window;
        this rule used to keep asserting BUSY for ten times longer,
        so an Esc that landed before the answer began — writing no
        terminal record for either path to find — held the pane BUSY
        for ten minutes with nothing running."""
        from ccm_constants import BUSY_STALE_RELEASE_SEC
        inside = ccm_rules.evaluate_rules(
            make_ctx(raw="IDLE", jsonl_last_stop_reason="user_pending",
                     jsonl_age=BUSY_STALE_RELEASE_SEC - 1))
        outside = ccm_rules.evaluate_rules(
            make_ctx(raw="IDLE", jsonl_last_stop_reason="user_pending",
                     jsonl_age=BUSY_STALE_RELEASE_SEC + 1))
        assert inside[0].name == "jsonl_user_prompt_pending"
        assert outside[0].name != "jsonl_user_prompt_pending"

    def test_jsonl_user_pending_does_not_promote_when_raw_busy(self):
        """If raw is already BUSY, the earlier `raw_busy_passthrough`
        rule wins. The user_pending rule only matters for the
        accept-edits raw=IDLE case."""
        rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw="BUSY",
                     jsonl_last_stop_reason="user_pending",
                     jsonl_age=60)
        )
        assert rule.name == "raw_busy_passthrough"

    def test_jsonl_user_pending_falls_through_when_stale(self):
        """Past BUSY_HOOK_JSONL_WINDOW (10 min) without new activity,
        the user_pending rule abstains and the session falls through
        to the default rule. Showing BUSY indefinitely for a
        genuinely stalled session would be misleading."""
        rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw="IDLE",
                     jsonl_last_stop_reason="user_pending",
                     jsonl_age=900)   # 15 min, past the 600s window
        )
        assert rule.name == "default"
        assert state == "IDLE"

    # --- default ---

    def test_default_pure_idle(self):
        rule, state = ccm_rules.evaluate_rules(make_ctx(raw="IDLE"))
        assert rule.name == "default"
        assert state == "IDLE"


class TestRuleMatching:
    """Directly exercise Rule.matches() edge cases."""

    def test_hook_in_requires_signal_present(self):
        rule = ccm_rules.Rule(name="t", hook_in=("BUSY",), hook_age_lt=10)
        # hook_state="" should NOT match even if hook_age_lt is satisfied by -1
        assert not rule.matches(make_ctx(hook_state="", hook_age=-1))

    def test_hook_age_lt_rejects_missing_signal(self):
        rule = ccm_rules.Rule(name="t", hook_age_lt=10)
        assert not rule.matches(make_ctx(hook_age=-1))

    def test_wildcard_matches_all(self):
        rule = ccm_rules.Rule(name="wild")
        assert rule.matches(make_ctx())
        assert rule.matches(make_ctx(raw="BUSY", hook_state="PERMIT"))


class TestFastPath:
    """evaluate_fast uses the same DETECTION_RULES as the slow path,
    so the statusline and dashboard can never disagree on state logic.
    """

    # Synthetic session_id used by every TestFastPath case. The
    # fixture monkeypatches `_session_id_from_tmux` to return this
    # for any project_dir, so `_hook_signal_path(project_dir)`
    # consistently maps to `<HOOK_DIR>/<TEST_SESSION_ID>` without
    # needing a real tmux window with a cached `@ccm_session_id`.
    TEST_SESSION_ID = "test-session-fastpath"

    @pytest.fixture
    def project_dir(self, tmp_path, monkeypatch):
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        monkeypatch.setattr(
            ccm_signals, "_session_id_from_tmux",
            lambda _project_dir: self.TEST_SESSION_ID,
        )
        proj = tmp_path / "proj"
        proj.mkdir()
        return str(proj)

    def _write_hook(self, project_dir, state, age=0):
        path = ccm_signals._hook_signal_path(project_dir)
        ts = int(time.time()) - age
        with open(path, "w") as f:
            f.write(f"{ts} {state}")

    # --- basic prev_state → state propagation ---

    def test_prev_idle_no_hook(self, project_dir):
        assert ccm_rules.evaluate_fast("IDLE", project_dir) == "IDLE"

    def test_prev_busy_no_hook_stays_busy(self, project_dir):
        """Without ps info, prev=BUSY stays BUSY via rule raw_busy_passthrough."""
        assert ccm_rules.evaluate_fast("BUSY", project_dir) == "BUSY"

    def test_prev_permit_no_hook_stays_permit(self, project_dir):
        assert ccm_rules.evaluate_fast("PERMIT", project_dir) == "PERMIT"

    def test_prev_shell_stays_shell(self, project_dir):
        assert ccm_rules.evaluate_fast("SHELL", project_dir) == "SHELL"

    # --- hook overrides ---

    def test_hook_permit_expired(self, project_dir):
        """Stale PERMIT hook + prev=IDLE → IDLE (not stuck PERMIT)."""
        self._write_hook(
            project_dir, "PERMIT",
            age=ccm_core.PERMIT_MAX_TIMEOUT + 10,
        )
        assert ccm_rules.evaluate_fast("IDLE", project_dir) == "IDLE"

    # --- no project dir ---

    def test_no_project_dir(self):
        """evaluate_fast with empty project_dir skips hook read gracefully."""
        assert ccm_rules.evaluate_fast("BUSY", "") == "BUSY"


