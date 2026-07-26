"""Tests for ccm_activity.

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
    VALID_RESOLVED_STATES,
    iso_ts,
    make_ctx,
    make_ps_lines,
    real_activity_record,
    system_record,
    write_jsonl,
)

# Backward-compat alias used by some tests.
_iso_ts = iso_ts

class TestDeriveInvariants:
    """Property tests for `derive_state_from_events`."""

    @pytest.mark.parametrize("event_type", [
        "prompt", "pretool", "posttool", "subagent", "compact",
        "stop", "permit_req", "notify_permit", "notify_idle",
        "session_end",
    ])
    @pytest.mark.parametrize("stop_reason", [
        None, "tool_use", "end_turn", "max_tokens", "stop_sequence",
        "future_unknown",
    ])
    def test_derive_returns_valid_state_or_none(
        self, event_type, stop_reason
    ):
        """For every (event_type × stop_reason) combination, derive
        must return a member of VALID_RESOLVED_STATES or None.
        Catches schema drift bugs where a new event type silently
        produces a junk return value."""
        result = ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": event_type},),
            jsonl_stop_reason=stop_reason,
            pid_present=True, claude_pid_age=300,
            jsonl_age=5, now=200,
        )
        assert result is None or result in VALID_RESOLVED_STATES, (
            f"event={event_type} stop_reason={stop_reason} → {result!r}"
        )

    @pytest.mark.parametrize("event_type", [
        "prompt", "pretool", "posttool", "subagent", "compact",
        "stop", "permit_req", "notify_permit", "notify_idle",
        "session_end",
    ])
    @pytest.mark.parametrize("mode", [
        "default", "acceptEdits", "bypassPermissions", "future_mode",
    ])
    def test_extra_mode_field_is_opaque_to_derive(self, event_type, mode):
        """Event records may carry a `mode` annotation (permission
        mode badge, written by hooks/lib.sh). Fields other than
        ts/type must not change derive's judgment — the state model
        never reads the mode. Locks the contract that lets us extend
        the event schema without touching detection."""
        common = dict(
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=300,
            jsonl_age=5, now=200,
        )
        bare = ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": event_type},), **common)
        annotated = ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": event_type, "mode": mode},),
            **common)
        assert bare == annotated, (
            f"mode annotation changed derive: {bare!r} → {annotated!r} "
            f"(event={event_type}, mode={mode})"
        )

    @pytest.mark.parametrize("raw", [None, "IDLE", "BUSY", "PERMIT"])
    @pytest.mark.parametrize("event_type", [
        "prompt", "pretool", "posttool", "stop", "permit_req",
    ])
    def test_derive_pid_absent_always_shell(self, raw, event_type):
        """pid_present=False is the most authoritative signal —
        process tree says claude is gone. derive must short-circuit
        to SHELL regardless of event log content or raw value."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": event_type},),
            jsonl_stop_reason="end_turn",
            pid_present=False, claude_pid_age=-1,
            jsonl_age=5, now=200, raw=raw,
        ) == "SHELL"

    @pytest.mark.parametrize("event_type", [
        "prompt", "pretool", "posttool", "subagent", "compact",
        "permit_req", "notify_permit", "stop",
    ])
    def test_raw_permit_overrides_any_non_permit_candidate(self, event_type):
        """The A' override: raw=PERMIT (capture-pane footer match)
        always resolves to PERMIT, regardless of latest event class.
        Modal on screen is the most authoritative signal we have."""
        for stop_reason in (None, "tool_use", "end_turn"):
            result = ccm_activity.derive_state_from_events(
                events=({"ts": 100, "type": event_type},),
                jsonl_stop_reason=stop_reason,
                pid_present=True, claude_pid_age=300,
                jsonl_age=5, now=200, raw="PERMIT",
            )
            # Either None (defer to legacy, which also lands on PERMIT)
            # or PERMIT directly.
            assert result in (None, "PERMIT"), (
                f"event={event_type} stop_reason={stop_reason} → {result!r}"
            )

class TestStripPhantomSubagents:
    """`_strip_phantom_subagents` is the input normaliser run by
    `classify_activity` before the decision tree. Trailing subagent
    events that follow a rest marker (notify_idle / stop /
    session_end) are spurious upstream firings and get trimmed;
    mid-conversation subagents (after a real activity event) are
    legitimate Task-tool invocations and must stay."""

    def _strip(self, events):
        from ccm_activity import _strip_phantom_subagents
        return _strip_phantom_subagents(tuple(events))

    def test_empty_returns_empty(self):
        assert self._strip(()) == ()

    def test_no_trailing_subagent_unchanged(self):
        events = ({"ts": 1, "type": "prompt"},
                  {"ts": 2, "type": "pretool"})
        assert self._strip(events) == events

    def test_trailing_subagent_after_notify_idle_trimmed(self):
        events = ({"ts": 1, "type": "stop"},
                  {"ts": 2, "type": "notify_idle"},
                  {"ts": 3, "type": "subagent"})
        assert self._strip(events) == events[:2]

    def test_trailing_subagent_after_stop_trimmed(self):
        events = ({"ts": 1, "type": "stop"},
                  {"ts": 2, "type": "subagent"})
        assert self._strip(events) == (events[0],)

    def test_trailing_subagent_after_session_end_trimmed(self):
        events = ({"ts": 1, "type": "session_end"},
                  {"ts": 2, "type": "subagent"})
        assert self._strip(events) == (events[0],)

    def test_stacked_phantom_subagents_all_trimmed(self):
        events = ({"ts": 1, "type": "notify_idle"},
                  {"ts": 2, "type": "subagent"},
                  {"ts": 3, "type": "subagent"},
                  {"ts": 4, "type": "subagent"})
        assert self._strip(events) == (events[0],)

    def test_legitimate_subagent_after_pretool_kept(self):
        # Task tool spawning a subagent mid-conversation. The
        # immediately preceding non-subagent event is `pretool`, not
        # a rest marker, so the trailing subagent is legitimate.
        events = ({"ts": 1, "type": "prompt"},
                  {"ts": 2, "type": "pretool"},
                  {"ts": 3, "type": "subagent"})
        assert self._strip(events) == events

    def test_all_subagent_events_kept_unchanged(self):
        # No rest marker to anchor against; classifier handles this
        # via UNKNOWN further along.
        events = ({"ts": 1, "type": "subagent"},
                  {"ts": 2, "type": "subagent"})
        assert self._strip(events) == events

    def test_malformed_event_in_tail_treated_as_non_subagent(self):
        events = ({"ts": 1, "type": "stop"},
                  "not-a-dict",
                  {"ts": 3, "type": "subagent"})
        # Subagent is trailing; non-subagent walking stops at the
        # malformed entry, which is not in _REST_MARKERS, so the
        # filter declines to trim. Keeps everything as-is.
        assert self._strip(events) == events


class TestClassifyActivity:
    """`classify_activity` produces the (Activity, evidence_event)
    pair that drives the decision tree. Each branch corresponds to
    a "what is Claude doing right now?" answer."""

    def _classify(self, **kwargs):
        from ccm_activity import classify_activity
        return classify_activity(**kwargs)

    def test_empty_events_unknown(self):
        from ccm_activity import ACTIVITY_UNKNOWN
        a, e = self._classify(
            events=(), jsonl_stop_reason=None,
            jsonl_age=-1, raw="IDLE", now=100,
        )
        assert a == ACTIVITY_UNKNOWN and e is None

    def test_notify_idle_at_rest(self):
        from ccm_activity import ACTIVITY_AT_REST
        a, e = self._classify(
            events=({"ts": 100, "type": "notify_idle"},),
            jsonl_stop_reason=None,
            jsonl_age=-1, raw="IDLE", now=200,
        )
        assert a == ACTIVITY_AT_REST
        assert e["type"] == "notify_idle"

    def test_stop_with_terminal_stop_reason_at_rest(self):
        from ccm_activity import ACTIVITY_AT_REST
        a, _e = self._classify(
            events=({"ts": 100, "type": "stop"},),
            jsonl_stop_reason="end_turn",
            jsonl_age=5, raw="IDLE", now=200,
        )
        assert a == ACTIVITY_AT_REST

    def test_stop_with_tool_use_in_progress(self):
        from ccm_activity import ACTIVITY_IN_PROGRESS
        a, _e = self._classify(
            events=({"ts": 100, "type": "stop"},),
            jsonl_stop_reason="tool_use",
            jsonl_age=5, raw="BUSY", now=200,
        )
        assert a == ACTIVITY_IN_PROGRESS

    def test_permit_event_awaiting_permit(self):
        from ccm_activity import ACTIVITY_AWAITING_PERMIT
        a, _e = self._classify(
            events=({"ts": 100, "type": "permit_req"},),
            jsonl_stop_reason="tool_use",
            jsonl_age=50, raw="PERMIT", now=200,
        )
        assert a == ACTIVITY_AWAITING_PERMIT

    def test_permit_event_with_terminal_jsonl_at_rest(self):
        # Esc-dismiss + Claude wrote a terminal stop_reason.
        from ccm_activity import ACTIVITY_AT_REST
        a, _e = self._classify(
            events=({"ts": 100, "type": "permit_req"},),
            jsonl_stop_reason="end_turn",
            jsonl_age=2, raw="IDLE", now=110,
        )
        assert a == ACTIVITY_AT_REST

    def test_pretool_event_in_progress(self):
        from ccm_activity import ACTIVITY_IN_PROGRESS
        a, _e = self._classify(
            events=({"ts": 100, "type": "pretool"},),
            jsonl_stop_reason="tool_use",
            jsonl_age=5, raw="BUSY", now=110,
        )
        assert a == ACTIVITY_IN_PROGRESS

    def test_session_end_unknown(self):
        from ccm_activity import ACTIVITY_UNKNOWN
        a, _e = self._classify(
            events=({"ts": 100, "type": "session_end"},),
            jsonl_stop_reason=None,
            jsonl_age=-1, raw="IDLE", now=110,
        )
        assert a == ACTIVITY_UNKNOWN

    def test_unknown_event_type_unknown(self):
        from ccm_activity import ACTIVITY_UNKNOWN
        a, _e = self._classify(
            events=({"ts": 100, "type": "newfangled_upstream_event"},),
            jsonl_stop_reason=None,
            jsonl_age=-1, raw="IDLE", now=110,
        )
        assert a == ACTIVITY_UNKNOWN

    def test_combined_stale_start_event_unknown(self):
        # event ts=0 (very old), jsonl_age past long-tool window,
        # raw != PERMIT → defer to legacy via UNKNOWN.
        from ccm_activity import ACTIVITY_UNKNOWN
        a, _e = self._classify(
            events=({"ts": 100, "type": "pretool"},),
            jsonl_stop_reason="tool_use",
            jsonl_age=99999, raw="IDLE",
            now=99999,  # event 99899s old, jsonl 99999s
        )
        assert a == ACTIVITY_UNKNOWN

    def test_phantom_subagent_normalised_before_classify(self):
        # Trailing subagent after notify_idle is stripped; classifier
        # then sees notify_idle as latest → AT_REST.
        from ccm_activity import ACTIVITY_AT_REST
        a, e = self._classify(
            events=(
                {"ts": 100, "type": "stop"},
                {"ts": 110, "type": "notify_idle"},
                {"ts": 200, "type": "subagent"},
            ),
            jsonl_stop_reason="end_turn",
            jsonl_age=400, raw="IDLE", now=500,
        )
        assert a == ACTIVITY_AT_REST
        assert e["type"] == "notify_idle"

    def test_all_subagent_log_unknown(self):
        """An events log consisting ONLY of subagent events defers
        to legacy (UNKNOWN). Real SubagentStart always happens inside
        a turn whose prompt/pretool events precede it — a lone
        subagent means either a phantom fired outside any turn or
        hooks went silent mid-turn, and the event log is
        untrustworthy either way. 2026-07-04 jwriter incident: hooks
        were silent for a whole real turn, then a single phantom
        subagent event at the recap moment held a false BUSY for the
        entire 10-minute staleness window because this guard —
        documented in _strip_phantom_subagents — was never
        implemented."""
        from ccm_activity import ACTIVITY_UNKNOWN
        a, e = self._classify(
            events=({"ts": 1000, "type": "subagent"},),
            jsonl_stop_reason="end_turn",
            jsonl_age=10800,  # last real turn ~3h ago
            raw="IDLE", now=1200,  # event only 200s old (fresh!)
        )
        assert a == ACTIVITY_UNKNOWN
        assert e["type"] == "subagent"

    def test_all_subagent_log_multiple_events_unknown(self):
        from ccm_activity import ACTIVITY_UNKNOWN
        a, _e = self._classify(
            events=({"ts": 1000, "type": "subagent"},
                    {"ts": 1010, "type": "subagent"}),
            jsonl_stop_reason="end_turn",
            jsonl_age=50, raw="IDLE", now=1100,
        )
        assert a == ACTIVITY_UNKNOWN

    def test_subagent_with_turn_context_still_in_progress(self):
        """The all-subagent guard must not over-trigger: a subagent
        preceded by the turn's prompt/pretool events is legitimate
        Task-tool work and stays IN_PROGRESS."""
        from ccm_activity import ACTIVITY_IN_PROGRESS
        a, e = self._classify(
            events=(
                {"ts": 100, "type": "prompt"},
                {"ts": 110, "type": "pretool"},
                {"ts": 120, "type": "subagent"},
            ),
            jsonl_stop_reason="tool_use",
            jsonl_age=5, raw="BUSY", now=200,
        )
        assert a == ACTIVITY_IN_PROGRESS
        assert e["type"] == "subagent"


class TestMapActivityToState:
    """`map_activity_to_state` is the small decision tree that maps
    an activity classification to a definite state, or None to defer
    to legacy. raw=PERMIT can promote a committed candidate to
    PERMIT, but UNKNOWN activity remains None even with raw=PERMIT
    (preserving the "no event signal → legacy decides" invariant)."""

    def _map(self, activity, raw="IDLE",
             jsonl_stop_reason=None, jsonl_age=-1):
        from ccm_activity import map_activity_to_state
        return map_activity_to_state(activity, raw, jsonl_stop_reason, jsonl_age)

    def test_at_rest_returns_idle(self):
        from ccm_activity import ACTIVITY_AT_REST
        assert self._map(ACTIVITY_AT_REST, raw="IDLE") == "IDLE"

    def test_in_progress_returns_busy(self):
        from ccm_activity import ACTIVITY_IN_PROGRESS
        assert self._map(ACTIVITY_IN_PROGRESS, raw="BUSY") == "BUSY"

    def test_stale_busy_with_idle_screen_defers(self):
        """BUSY candidate + raw=IDLE + JSONL frozen past the long-tool
        window → defer to legacy (None → IDLE). A genuinely working
        session shows a spinner (raw=BUSY), so raw=IDLE here means the
        event log is stale — a hook-silent turn end or a recap-moment
        phantom SubagentStart holding a stuck BUSY (2026-07-07
        monadic-chat incident)."""
        from ccm_activity import ACTIVITY_IN_PROGRESS, BUSY_HOOK_JSONL_WINDOW
        assert self._map(
            ACTIVITY_IN_PROGRESS, raw="IDLE",
            jsonl_stop_reason="tool_use",
            jsonl_age=BUSY_HOOK_JSONL_WINDOW + 1,
        ) is None

    def test_stale_busy_guard_does_not_touch_fresh_jsonl(self):
        """The guard is bounded by the long-tool window: a BUSY
        candidate with FRESH JSONL (raw=IDLE, e.g. accept-edits) stays
        BUSY. This is the approved-long-tool case whose fix keeps the
        composer on screen — jsonl_age is small, well inside the
        window, so the guard never applies."""
        from ccm_activity import ACTIVITY_IN_PROGRESS, BUSY_HOOK_JSONL_WINDOW
        assert self._map(
            ACTIVITY_IN_PROGRESS, raw="IDLE",
            jsonl_stop_reason="tool_use",
            jsonl_age=BUSY_HOOK_JSONL_WINDOW - 1,
        ) == "BUSY"

    def test_stale_busy_guard_does_not_touch_raw_busy(self):
        """raw=BUSY (spinner visible) is a genuinely active session —
        the guard requires raw=IDLE, so a long tool stays BUSY even
        with a stale JSONL."""
        from ccm_activity import ACTIVITY_IN_PROGRESS
        assert self._map(
            ACTIVITY_IN_PROGRESS, raw="BUSY",
            jsonl_stop_reason="tool_use", jsonl_age=99999) == "BUSY"

    def test_stale_permit_with_idle_screen_releases_to_legacy(self):
        """permit-latest + raw=IDLE + JSONL frozen past
        PERMIT_MAX_TIMEOUT defers to legacy (→ IDLE) instead of
        holding PERMIT forever.

        This inverts the pre-2026-07-26 behaviour. PERMIT used to be
        excluded from the staleness guard because an idle screen was
        thought indistinguishable from an interactive choice menu
        awaiting a selection. Measurement killed that premise: a live
        menu renders a footer `PATTERN_PERMIT_FOOTER` matches, so it
        arrives here as raw=PERMIT (covered by
        `test_stale_permit_with_modal_on_screen_stays_permit`), never
        raw=IDLE. What raw=IDLE actually means is a permission that
        was already resolved — typically Esc'd, which fires no Stop
        hook, so a permit event stays "latest" indefinitely
        (2026-07-26 macos-config incident: `⚠ PERMIT` for 15+ minutes
        on an empty `❯` prompt)."""
        from ccm_activity import ACTIVITY_AWAITING_PERMIT
        assert self._map(
            ACTIVITY_AWAITING_PERMIT, raw="IDLE",
            jsonl_stop_reason="tool_use", jsonl_age=99999) is None

    def test_stale_permit_with_modal_on_screen_stays_permit(self):
        """The safety property of the release above: while the modal
        is physically on screen (raw=PERMIT — a live choice menu
        included), age is irrelevant and PERMIT is held. A menu the
        user leaves open for hours must never read as IDLE."""
        from ccm_activity import ACTIVITY_AWAITING_PERMIT
        assert self._map(
            ACTIVITY_AWAITING_PERMIT, raw="PERMIT",
            jsonl_stop_reason="tool_use", jsonl_age=99999) == "PERMIT"

    def test_fresh_permit_with_idle_screen_stays_permit(self):
        """Inside the window the permit is still trusted, so the
        ordinary "you must answer this" case is unaffected — only a
        permit stale past `PERMIT_MAX_TIMEOUT` is released."""
        from ccm_activity import ACTIVITY_AWAITING_PERMIT
        assert self._map(
            ACTIVITY_AWAITING_PERMIT, raw="IDLE",
            jsonl_stop_reason="tool_use", jsonl_age=30) == "PERMIT"

    def test_permit_and_busy_windows_are_independently_tunable(self):
        """The two candidates read different constants
        (`PERMIT_MAX_TIMEOUT` vs `BUSY_HOOK_JSONL_WINDOW`), so the
        PERMIT axis can be tuned without touching the BUSY axis.
        Both default to 600 s; this pins that they are *separate*
        knobs rather than one shared threshold."""
        import ccm_activity
        from ccm_activity import ACTIVITY_AWAITING_PERMIT, ACTIVITY_IN_PROGRESS
        with patch.object(ccm_activity, "PERMIT_MAX_TIMEOUT", 10), \
                patch.object(ccm_activity, "BUSY_HOOK_JSONL_WINDOW", 10_000):
            # 50 s: past the permit window, well inside the BUSY one.
            assert self._map(
                ACTIVITY_AWAITING_PERMIT, raw="IDLE",
                jsonl_stop_reason="tool_use", jsonl_age=50) is None
            assert self._map(
                ACTIVITY_IN_PROGRESS, raw="IDLE",
                jsonl_stop_reason="tool_use", jsonl_age=50) == "BUSY"

    def test_awaiting_permit_with_modal_on_screen_returns_permit(self):
        from ccm_activity import ACTIVITY_AWAITING_PERMIT
        assert self._map(ACTIVITY_AWAITING_PERMIT, raw="PERMIT") == "PERMIT"

    def test_awaiting_permit_maps_to_permit_regardless_of_jsonl(self):
        # The auto-approved-tool promotion now lives in
        # `classify_activity` (where event_age is accessible to
        # bound the heuristic to recent permits). Once the
        # classifier has decided the activity is AWAITING_PERMIT,
        # the mapper unconditionally returns PERMIT — fresh
        # tool_use no longer overrides here. Coverage for the
        # promotion path lives in `TestClassifyActivity` and the
        # `TestDeriveStateFromEvents` end-to-end cases.
        from ccm_activity import ACTIVITY_AWAITING_PERMIT
        assert self._map(
            ACTIVITY_AWAITING_PERMIT, raw="BUSY",
            jsonl_stop_reason="tool_use", jsonl_age=5,
        ) == "PERMIT"
        # raw=IDLE + a *fresh* JSONL likewise maps straight to PERMIT.
        # (The stale variant is the one released by the
        # PERMIT_MAX_TIMEOUT guard — see
        # `test_stale_permit_with_idle_screen_releases_to_legacy`.)
        assert self._map(
            ACTIVITY_AWAITING_PERMIT, raw="IDLE",
            jsonl_stop_reason="tool_use", jsonl_age=5,
        ) == "PERMIT"

    def test_unknown_returns_none(self):
        from ccm_activity import ACTIVITY_UNKNOWN
        assert self._map(ACTIVITY_UNKNOWN, raw="IDLE") is None

    def test_unknown_with_raw_permit_still_returns_none(self):
        # Capture-pane authority does not override the
        # "UNKNOWN → legacy" invariant; raw=PERMIT will be picked up
        # by legacy's `raw_permit_passthrough` rule consistently.
        from ccm_activity import ACTIVITY_UNKNOWN
        assert self._map(ACTIVITY_UNKNOWN, raw="PERMIT") is None

    def test_at_rest_promoted_to_permit_when_raw_permit(self):
        # AT_REST committed candidate → IDLE; raw=PERMIT promotes.
        from ccm_activity import ACTIVITY_AT_REST
        assert self._map(ACTIVITY_AT_REST, raw="PERMIT") == "PERMIT"

    def test_in_progress_promoted_to_permit_when_raw_permit(self):
        from ccm_activity import ACTIVITY_IN_PROGRESS
        assert self._map(ACTIVITY_IN_PROGRESS, raw="PERMIT") == "PERMIT"


class TestDeriveStateFromEvents:
    """Parametrized coverage of the pure state-derivation function.

    The function is the phase-2 replacement for the legacy
    DETECTION_RULES pipeline: state is a pure function of (event log
    tail, JSONL stop_reason, pid presence + age). These tests
    encode the full truth table; any semantic change must update
    both the function and the matching row below.
    """

    # ─── SHELL: pid absent ───

    def test_pid_absent_shell(self):
        assert ccm_activity.derive_state_from_events(
            events=(), jsonl_stop_reason=None,
            pid_present=False, claude_pid_age=-1,
        ) == "SHELL"

    # ─── 2026-07-04 jwriter incident replay ───

    def test_lone_phantom_subagent_defers_to_legacy(self):
        """Exact replay of the 2026-07-04 jwriter stuck-BUSY: hooks
        silent through a real turn (last JSONL end_turn ~3 h old),
        then a single phantom SubagentStart fires at the recap
        moment. The lone fresh subagent event must NOT hold BUSY —
        derive defers (None) and legacy resolves from raw=IDLE."""
        event_ts = 1783142525   # 14:22:05 phantom subagent
        jsonl_ts = 1783132130   # 11:28:50 last real end_turn
        for offset in (55, 265, 445):  # 14:23 / 14:26:30 / 14:29:30
            now = event_ts + offset
            assert ccm_activity.derive_state_from_events(
                events=({"ts": event_ts, "type": "subagent"},),
                jsonl_stop_reason="end_turn",
                pid_present=True, claude_pid_age=11000,
                raw="IDLE", jsonl_age=now - jsonl_ts, now=now,
            ) is None, f"stuck BUSY reproduced at +{offset}s"

    def test_pid_absent_with_events_still_shell(self):
        """Process tree is authoritative over stale event log."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "prompt"},),
            jsonl_stop_reason=None,
            pid_present=False, claude_pid_age=-1,
        ) == "SHELL"

    # ─── No events: defer to legacy ───

    def test_pid_present_no_events_returns_none(self):
        """Empty event log → None (caller falls back to legacy).
        The previous behaviour returned IDLE here; that masked a
        real outage scenario where the event
        log file went temporarily missing while the pane was
        actually showing a PERMIT modal."""
        assert ccm_activity.derive_state_from_events(
            events=(), jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=2,
        ) is None

    def test_pid_present_no_events_old_pid_returns_none(self):
        assert ccm_activity.derive_state_from_events(
            events=(), jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=5000,
        ) is None

    # ─── Start-class events → BUSY ───

    @staticmethod
    def _start_events(event_type, ts=100):
        """A start-class event as the log tail. `subagent` gets its
        turn's preceding `prompt` for context — a LONE subagent log
        defers to legacy since the all-subagent guard (2026-07-04
        jwriter phantom incident); with context it is legitimate
        Task-tool work and still start-class."""
        if event_type == "subagent":
            return ({"ts": ts - 10, "type": "prompt"},
                    {"ts": ts, "type": event_type})
        return ({"ts": ts, "type": event_type},)

    @pytest.mark.parametrize("event_type", [
        "prompt", "pretool", "posttool", "subagent", "compact",
    ])
    def test_start_class_events_busy(self, event_type):
        assert ccm_activity.derive_state_from_events(
            events=self._start_events(event_type),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=100,
        ) == "BUSY"

    # ─── Permit-class events → PERMIT ───

    @pytest.mark.parametrize("event_type", [
        "permit_req", "notify_permit",
    ])
    def test_permit_class_events_permit(self, event_type):
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": event_type},),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=100,
        ) == "PERMIT"

    def test_permit_no_time_limit(self):
        """PERMIT must not decay by timer — only by subsequent event.
        This guards the Option-2 design decision (indefinite PERMIT)."""
        # Event is from 1 hour ago; still PERMIT.
        assert ccm_activity.derive_state_from_events(
            events=({"ts": int(time.time()) - 3600, "type": "permit_req"},),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=4000,
        ) == "PERMIT"

    def test_permit_with_running_tool_and_raw_busy_returns_busy(self):
        """The user grants the permission and Claude starts a tool.
        The pane now shows tool output (no `❯` prompt at column 0,
        claude has children) so the capture-pane classifier returns
        raw=BUSY. We trust raw to discriminate "tool running" from
        "user back at prompt", regardless of whether PreToolUse
        hooks have fired.

        Without this branch the latest event stays at `notify_permit`
        (PreToolUse silently failed) and the dashboard would show a
        stale PERMIT for the entire duration of the tool execution.
        """
        now = int(time.time())
        assert ccm_activity.derive_state_from_events(
            events=({"ts": now - 30, "type": "notify_permit"},),
            jsonl_stop_reason="tool_use",
            jsonl_age=31,                     # JSONL just before permit
            pid_present=True,
            claude_pid_age=400,
            raw="BUSY",
            now=now,
        ) == "BUSY"

    def test_permit_with_idle_pane_and_jsonl_older_than_event_stays_permit(self):
        """raw=IDLE with JSONL `tool_use` OLDER than the permit
        event = ambiguous shape. Two real workflows produce it:

        (A) post-accept extended-thinking briefly between modal
            dismiss and the next PreToolUse fire (typically a few
            seconds, since the permit interrupts an in-flight tool);
        (B) interactive choice menu where Claude rendered an option
            list as a permit-class event and the user is still
            reading — JSONL last `tool_use` predates the menu.

        We surface PERMIT here. Case (A) lasts only the brief gap
        until pretool fires, so a few seconds of "false PERMIT"
        during thinking is cosmetic. Case (B) lasts as long as the
        user takes to choose, so a "false BUSY" there persists
        until the heuristic's window expires — confusing because
        the dashboard claims activity while the user is genuinely
        awaiting selection. Holding PERMIT is the correct call for
        (B) and acceptably brief for (A)."""
        now = int(time.time())
        assert ccm_activity.derive_state_from_events(
            events=({"ts": now - 30, "type": "permit_req"},),
            jsonl_stop_reason="tool_use",
            jsonl_age=35,                      # older than event_age=30
            pid_present=True,
            claude_pid_age=400,
            raw="IDLE",
            now=now,
        ) == "PERMIT"

    def test_permit_with_running_tool_and_no_raw_keeps_permit(self):
        """When the caller does not provide a `raw` value (None) we
        cannot discriminate between grant and dismiss. Keep PERMIT
        — the (Nm) stale-signal age suffix surfaces the "stuck"
        nature of the state to the user.
        """
        now = int(time.time())
        assert ccm_activity.derive_state_from_events(
            events=({"ts": now - 30, "type": "notify_permit"},),
            jsonl_stop_reason="tool_use",
            jsonl_age=31,
            pid_present=True,
            claude_pid_age=400,
            now=now,
            # no raw passed
        ) == "PERMIT"

    def test_permit_with_stale_tool_use_stays_permit(self):
        """If JSONL has not been touched for longer than
        BUSY_HOOK_JSONL_WINDOW (10 min), the `tool_use` signal is no
        longer trustworthy as "tool currently running" — the tool
        either finished without a JSONL record (unlikely) or the
        session has been idle since. Stay in PERMIT (cosmetic stuck
        state, surfaced via the (Nm) suffix).
        """
        now = int(time.time())
        assert ccm_activity.derive_state_from_events(
            events=({"ts": now - 1200, "type": "notify_permit"},),
            jsonl_stop_reason="tool_use",
            jsonl_age=1200,                   # 20 min — beyond BUSY_HOOK_JSONL_WINDOW
            pid_present=True,
            claude_pid_age=2000,
            now=now,
        ) == "PERMIT"

    # ─── session_end with live pid → defer to legacy ───

    def test_session_end_with_live_pid_returns_none(self):
        """Latest event session_end + pid_present=True is the brief
        window between SessionEnd hook and the next prompt of a
        restarted Claude. Returning SHELL here would falsely flag
        the pane as "claude not running" until the next event
        landed; legacy detection (which sees the live pid) is
        more accurate. None means "fall back to legacy"."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "session_end"},),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=100,
        ) is None

    def test_session_end_with_dead_pid_shell(self):
        """pid_present=False short-circuits to SHELL before events
        are consulted. session_end only matters when pid is alive."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "session_end"},),
            jsonl_stop_reason=None,
            pid_present=False, claude_pid_age=-1,
        ) == "SHELL"


    # ─── notify_idle → IDLE ───

    def test_notify_idle_event_idle(self):
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "notify_idle"},),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=100,
        ) == "IDLE"

    # ─── stop + stop_reason discriminator ───

    @pytest.mark.parametrize("reason", [
        "end_turn", "max_tokens", "stop_sequence",
    ])
    def test_stop_terminal_reason_idle(self, reason):
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "stop"},),
            jsonl_stop_reason=reason,
            pid_present=True, claude_pid_age=100,
        ) == "IDLE"

    def test_stop_tool_use_busy(self):
        """The tool_use mid-turn case;
        collapsed into BUSY for state-model purity (both mean "user
        waits", which is the action-need axis the state captures)."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "stop"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=100,
        ) == "BUSY"

    def test_stop_unknown_reason_busy(self):
        """Missing/unknown stop_reason is conservative BUSY so long
        tools without clear evidence never flip to false IDLE."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "stop"},),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=100,
        ) == "BUSY"

    def test_stop_arbitrary_nonterminal_busy(self):
        """Any stop_reason outside TERMINAL_STOP_REASONS is BUSY."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "stop"},),
            jsonl_stop_reason="future_unknown",
            pid_present=True, claude_pid_age=100,
        ) == "BUSY"

    # ─── Latest event wins (event log ordering) ───

    def test_latest_event_wins_busy_over_stop(self):
        """After Stop → PreToolUse for the next tool, state is BUSY."""
        assert ccm_activity.derive_state_from_events(
            events=(
                {"ts": 100, "type": "stop"},
                {"ts": 101, "type": "pretool"},
            ),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=100,
        ) == "BUSY"

    def test_latest_event_wins_stop_over_busy(self):
        """After PreToolUse → Stop, state depends on stop_reason."""
        assert ccm_activity.derive_state_from_events(
            events=(
                {"ts": 100, "type": "pretool"},
                {"ts": 101, "type": "stop"},
            ),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=100,
        ) == "IDLE"

    def test_latest_event_wins_prompt_clears_stale_permit(self):
        """Stale permit_req + new prompt → BUSY. The PERMIT-dismiss
        path: user dismissed a dialog long ago, then submitted a new
        prompt. New event supersedes stale PERMIT."""
        assert ccm_activity.derive_state_from_events(
            events=(
                {"ts": 100, "type": "permit_req"},
                {"ts": 200, "type": "prompt"},
            ),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=500,
        ) == "BUSY"

    # ─── Unknown / malformed: defer to legacy ───

    def test_unknown_event_type_returns_none(self):
        """Unknown types (upstream schema drift) must not hard-error
        and must not silently commit IDLE — let legacy decide."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "future_event"},),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=100,
        ) is None

    def test_latest_event_bad_shape_returns_none(self):
        """Malformed latest record (e.g. not a dict after aggressive
        schema change) defers to legacy."""
        assert ccm_activity.derive_state_from_events(
            events=("not a dict",),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=100,
        ) is None

    # ─── Lifecycle scenarios ───

    def test_long_running_tool_stays_busy(self):
        """Regression from project_false_idle_long_tool: hook went
        silent mid-build, prev decayed to IDLE in legacy pipeline.
        In the event-log model the tail still ends at `stop` with
        tool_use (intermediate Stop boundary), so state stays BUSY
        authoritatively. The tool_use mid-turn pause classifies as BUSY
        because both states represent "ball is on Claude's side"
        (user-centered design principle)."""
        events = (
            {"ts": 100, "type": "prompt"},
            {"ts": 101, "type": "pretool"},
            {"ts": 102, "type": "stop"},
        )
        assert ccm_activity.derive_state_from_events(
            events=events, jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
        ) == "BUSY"

    def test_permit_dismiss_then_new_prompt_recovers(self):
        """Dismissed permit with no follow-up → PERMIT (indefinite),
        then new user prompt → BUSY. The indefinite PERMIT is the
        Option-2 choice from the design discussion."""
        # Before new prompt:
        events_before = ({"ts": 100, "type": "permit_req"},)
        assert ccm_activity.derive_state_from_events(
            events=events_before, jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
        ) == "PERMIT"
        # After new prompt:
        events_after = events_before + (
            {"ts": 500, "type": "prompt"},
        )
        assert ccm_activity.derive_state_from_events(
            events=events_after, jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
        ) == "BUSY"

    def test_iterator_input_accepted(self):
        """derive must handle arbitrary iterables, not just tuples.
        The event log reader returns a tuple today but we defend
        against that shape changing later."""
        def gen():
            yield {"ts": 100, "type": "prompt"}
            yield {"ts": 101, "type": "stop"}
        assert ccm_activity.derive_state_from_events(
            events=gen(), jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=100,
        ) == "IDLE"

    def test_stale_permit_with_running_tool_stays_permit(self):
        """The exact scenario from project_false_idle_long_tool.md:
        user accepted a permission dialog, Claude started a long tool,
        but PreToolUse fired silently (Claude Code #16047-class bug).
        In the legacy pipeline raw=IDLE + stale hook + no prev=BUSY
        fell through `default → IDLE`, and ccm send would then quietly
        inject keystrokes into a pane running a tool.

        With the event log as the source of truth, the latest event
        is still `permit_req` (the dropped PreToolUse left no trace),
        so state stays PERMIT. That is a cosmetic mismatch (no dialog
        on screen) but a safe one: PERMIT refuses ccm send even with
        --force, which is the whole point of refusing to decay by
        timer."""
        events = (
            {"ts": 100, "type": "prompt"},
            {"ts": 150, "type": "permit_req"},
            # PreToolUse event missing — Claude Code dropped it.
        )
        assert ccm_activity.derive_state_from_events(
            events=events, jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
        ) == "PERMIT"

    def test_multi_turn_tool_chain_transitions(self):
        """Full lifecycle walk of a multi-turn response: Claude runs a
        tool, pauses at a Stop boundary with stop_reason=tool_use (an
        intermediate, not final, Stop), then the next turn starts with
        another PreToolUse, and finally ends with Stop + end_turn.

        The event-log pipeline must produce BUSY at the intermediate
        boundary (tool pending, ball still on Claude's side) and stay
        BUSY through the next start event. CONT was collapsed into
        BUSY (at this resolution the user does not need
        to distinguish "actively running tool" from "between tools"
        since neither requires user action."""
        # Turn 1: prompt, tool runs, intermediate Stop.
        turn1 = (
            {"ts": 100, "type": "prompt"},
            {"ts": 101, "type": "pretool"},
            {"ts": 102, "type": "posttool"},
            {"ts": 103, "type": "stop"},
        )
        assert ccm_activity.derive_state_from_events(
            events=turn1, jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
        ) == "BUSY"

        # Turn 2 start: next PreToolUse keeps BUSY.
        turn2_start = turn1 + (
            {"ts": 110, "type": "pretool"},
        )
        assert ccm_activity.derive_state_from_events(
            events=turn2_start, jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
        ) == "BUSY"

        # Final Stop with end_turn → IDLE (response complete).
        turn2_end = turn2_start + (
            {"ts": 115, "type": "posttool"},
            {"ts": 116, "type": "stop"},
        )
        assert ccm_activity.derive_state_from_events(
            events=turn2_end, jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=3000,
        ) == "IDLE"

    # ─── A' override: raw=PERMIT trumps non-PERMIT derivation ───

    @pytest.mark.parametrize("event_type,jsonl_stop", [
        ("pretool", "tool_use"),
        ("posttool", "end_turn"),
        ("prompt", None),
        ("stop", "tool_use"),     # would otherwise be BUSY (mid-tool)
        ("stop", "end_turn"),     # would otherwise be IDLE
        ("notify_idle", None),
    ])
    def test_raw_permit_overrides_non_permit_candidate(
        self, event_type, jsonl_stop
    ):
        """When the capture-pane footer detects a permission modal
        (raw="PERMIT"), the event log lagging behind PermissionRequest
        must not down-classify the state. This is the exact scenario
        concrete scenario: latest event was
        pretool (BUSY), but a PERMIT modal was already on screen."""
        events = ({"ts": 100, "type": event_type},)
        assert ccm_activity.derive_state_from_events(
            events=events, jsonl_stop_reason=jsonl_stop,
            pid_present=True, claude_pid_age=300,
            raw="PERMIT",
        ) == "PERMIT"

    def test_raw_permit_with_permit_event_still_permit(self):
        """raw=PERMIT and event log permit_req agree → PERMIT.
        No-op override, but verifies the override does not corrupt
        the already-correct case."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "permit_req"},),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=300,
            raw="PERMIT",
        ) == "PERMIT"

    @pytest.mark.parametrize("raw_value", ["IDLE", "BUSY", "SHELL", "DOWN", None])
    def test_non_permit_raw_does_not_override(self, raw_value):
        """Only raw="PERMIT" is authoritative. raw="BUSY" must NOT
        override an event-log IDLE — that is the leftover-dev-server
        scenario where the legacy process-tree heuristic falsely
        flags BUSY but the event log correctly says the conversation
        is IDLE. Letting BUSY override would re-introduce the false
        BUSY that the event log was designed to fix."""
        events = (
            {"ts": 100, "type": "stop"},
        )
        assert ccm_activity.derive_state_from_events(
            events=events, jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            raw=raw_value,
        ) == "IDLE"

    def test_raw_permit_does_not_override_session_end_fallback(self):
        """session_end + pid present already returns None (legacy
        fallback). raw="PERMIT" should NOT promote that to PERMIT —
        the override is for cases where the event log said BUSY or
        IDLE but the modal is on screen. session_end means we have
        no event signal to override, so the right answer is still
        "ask legacy" (which sees raw=PERMIT and returns PERMIT)."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "session_end"},),
            jsonl_stop_reason=None,
            pid_present=True, claude_pid_age=300,
            raw="PERMIT",
        ) is None

    # ─── Esc-interrupt / hook-silence fallback ───

    @pytest.mark.parametrize("event_type", [
        "prompt", "pretool", "posttool", "subagent", "compact",
    ])
    @pytest.mark.parametrize("stop_reason", [
        "end_turn", "max_tokens", "stop_sequence",
    ])
    def test_start_event_with_fresh_terminal_jsonl_returns_idle(
        self, event_type, stop_reason
    ):
        """Latest event is start-class but JSONL shows the response
        actually completed (terminal stop_reason fresher than the
        event itself). The Stop hook never wrote a `stop` event —
        either Esc-interrupt or hook silence (#16047 class). derive
        must return IDLE directly: deferring to legacy here doesn't
        help here because legacy alone cannot disambiguate the
        stale BUSY signal that the missing Stop hook would have
        cleared.

        Event age (now - event_ts) must be greater than jsonl_age
        for the fallback to trigger — that is the discriminator
        against the "fresh prompt right after previous turn ended"
        false-positive."""
        # event_ts=100, now=200 → event_age=100; jsonl_age=10. JSONL
        # is much fresher than the event → IDLE.
        assert ccm_activity.derive_state_from_events(
            events=self._start_events(event_type),
            jsonl_stop_reason=stop_reason,
            pid_present=True, claude_pid_age=300,
            jsonl_age=10, now=200,
        ) == "IDLE"

    def test_fresh_prompt_after_previous_turn_stays_busy(self):
        """Regression guard: a fresh `prompt` event
        submitted right after a previous turn ended must NOT trigger
        the Esc-fallback. The JSONL terminal stop_reason in that
        case belongs to the prior turn, not the current event.

        Pre-fix this scenario produced a phantom IDLE flicker —
        without it, while Claude is clearly
        Incubating but the dashboard rendered ● IDLE ✓0s."""
        # event_ts=200 (just now), now=205 → event_age=5
        # jsonl_age=10 (previous turn ended 10 s ago)
        # event_age (5) < jsonl_age (10) → JSONL terminal is OLDER
        # than the event → fallback must NOT trigger → BUSY.
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 200, "type": "prompt"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10, now=205,
        ) == "BUSY"

    def test_start_event_with_stale_terminal_jsonl_stays_busy(self):
        """If the JSONL terminal stop_reason is older than the
        gap-tolerance window, it likely belongs to a previous turn,
        not the current start event. Hold BUSY — claude is genuinely
        in a long thinking phase or tool wait, NOT a Esc-interrupt
        aftermath."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "prompt"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=300,  # 5 min old, well past 60 s window
        ) == "BUSY"

    def test_start_event_with_tool_use_jsonl_stays_busy(self):
        """JSONL stop_reason=tool_use means a tool is in flight —
        the response has NOT completed. Don't trigger fallback even
        if jsonl_age is small."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "pretool"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=300,
            jsonl_age=2,
        ) == "BUSY"

    def test_start_event_no_jsonl_info_stays_busy(self):
        """jsonl_age=-1 (sentinel for "no JSONL data") must NOT
        trigger the fallback. Without a JSONL signal we cannot
        confirm completion; default to the conservative BUSY."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "prompt"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            jsonl_age=-1,  # default sentinel
        ) == "BUSY"

    def test_raw_permit_overrides_esc_interrupt_fallback(self):
        """A' override (raw=PERMIT → PERMIT) must win over the new
        Esc-interrupt fallback. If the user pressed Esc but the
        capture-pane footer still shows a permission modal (rare
        race), the capture-pane is more authoritative than JSONL —
        the modal is literally on screen waiting for a keypress."""
        # Without raw=PERMIT, the fallback would return None.
        # With raw=PERMIT, it must short-circuit to PERMIT instead.
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "prompt"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10,
            raw="PERMIT",
        ) == "PERMIT"

    # ─── PERMIT release fallback (mirror of start-class fallback) ───

    @pytest.mark.parametrize("event_type", ["permit_req", "notify_permit"])
    def test_permit_event_with_fresh_terminal_jsonl_releases(self, event_type):
        """Mirror of start-class fallback for the PERMIT axis. If
        JSONL terminal stop_reason is fresher than the latest permit
        event AND raw is not PERMIT (no modal visible), the dialog
        has been resolved silently — return IDLE.

        Concrete scenario
        where stuck PERMIT persisted 5 minutes after permission
        approval in accept-edits mode (raw=BUSY)."""
        # event_ts=100, now=200 → event_age=100; jsonl_age=10. JSONL
        # terminal is fresher than the permit event → release.
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": event_type},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10, now=200,
            raw="BUSY",  # accept-edits mode — modal NOT on screen
        ) == "IDLE"

    def test_permit_event_with_raw_permit_keeps_permit(self):
        """raw=PERMIT trumps the release: capture-pane footer is
        the authoritative signal for an on-screen modal, regardless
        of how stale the permit event is."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "permit_req"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10, now=200,
            raw="PERMIT",  # modal on screen
        ) == "PERMIT"

    def test_permit_event_raw_busy_promotes_despite_fresher_permit(self):
        """raw=BUSY is authoritative current evidence: capture-pane
        shows Claude actively working (an active-work spinner, or
        children running with no prompt). A real permission wait
        BLOCKS execution — no spinner, and its options make raw
        PERMIT or IDLE, never BUSY. So raw=BUSY promotes to BUSY even
        when the permit event is fresher than the last JSONL terminal.

        This scenario (permit fresher than a prior end_turn, raw=BUSY)
        is EXACTLY the 2026-06-30 monadic-chat false-PERMIT: a new
        turn dispatched a subagent whose WebFetch raised a permit,
        the fetch ran for minutes (spinner → raw=BUSY), but the old
        'fresher permit ⇒ keep PERMIT' rule stuck the dashboard at
        PERMIT. The current on-screen raw signal wins."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 200, "type": "permit_req"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10, now=205,
            raw="BUSY",
        ) == "BUSY"

    @pytest.mark.parametrize("event_type", ["permit_req", "notify_permit"])
    def test_permit_event_with_tool_use_jsonl_promotes_to_busy(self, event_type):
        """Auto-approved permit case (mirror of legacy
        permit-tool-use override branch): permit signal survives,
        raw=BUSY (the `⏵⏵` accept-edits spinner is showing), and
        JSONL says a tool is in flight → claude is actively
        running tools → BUSY (not PERMIT). The user is being
        attended to, not waiting.

        Scoped to raw=BUSY only — raw=IDLE with the same JSONL is
        the dismiss case (tool finished, user dismissed dialog,
        claude plainly idle), which keeps PERMIT (cosmetic)."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": event_type},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10, now=200,
            raw="BUSY",
        ) == "BUSY"

    def test_permit_event_with_tool_use_jsonl_keeps_permit_when_modal_visible(self):
        """raw=PERMIT (modal physically on screen) trumps the
        tool_use BUSY promotion — modal is the authoritative
        on-screen signal."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "permit_req"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10, now=200,
            raw="PERMIT",
        ) == "PERMIT"

    def test_permit_event_raw_busy_promotes_despite_stale_tool_use(self):
        """A stale JSONL tool_use (past BUSY_HOOK_JSONL_WINDOW) no
        longer forces PERMIT when raw=BUSY. The JSONL age governs
        only the raw=IDLE promotion (where JSONL is the sole activity
        evidence); raw=BUSY is a fresh on-screen signal that Claude
        is working NOW, independent of how old the JSONL record is.
        The old 'stale tool_use ⇒ PERMIT' rule ignored the live raw
        and produced the sticky false-PERMIT this fix removes."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "permit_req"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=900,
            jsonl_age=700, now=900,
            raw="BUSY",
        ) == "BUSY"

    def test_permit_event_subagent_webfetch_raw_busy_no_jsonl_tool_use(self):
        """2026-06-30 monadic-chat incident, distilled: a background
        subagent's WebFetch raised a permit and ran for minutes
        (spinner → raw=BUSY), but its tool_use record landed in the
        SUBAGENT's JSONL, so the main session's JSONL showed no fresh
        tool_use (stop_reason end_turn / None). The old code gated
        the raw=BUSY promotion behind main-session `jsonl tool_use`
        and stuck the dashboard at PERMIT for the whole fetch. raw=
        BUSY must promote to BUSY regardless of the JSONL stop_reason."""
        for sr in ("end_turn", None):
            assert ccm_activity.derive_state_from_events(
                events=({"ts": 100, "type": "notify_permit"},),
                jsonl_stop_reason=sr,
                pid_present=True, claude_pid_age=300,
                jsonl_age=150, now=250,
                raw="BUSY",
            ) == "BUSY", f"raw=BUSY must be BUSY with jsonl_stop_reason={sr!r}"

    def test_permit_event_with_idle_pane_and_fresh_tool_use_promotes_to_busy(self):
        """raw=IDLE with tool_use AND JSONL fresher than the permit
        event = `accept edits on` mode where `⏵⏵` lives on a
        separate line from the `❯` prompt and detect_pane_state
        returns raw=IDLE despite the tool actively running.
        Should promote to BUSY (consistent with raw=BUSY case)."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "permit_req"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10, now=200,  # event_age=100, jsonl fresher
            raw="IDLE",
        ) == "BUSY"

    def test_permit_event_post_dismiss_with_jsonl_older_stays_permit(self):
        """raw=IDLE with JSONL strictly OLDER than the permit
        event remains PERMIT. The interactive choice menu case
        (Claude renders option list as permit-class event, user
        reads while JSONL last record is from BEFORE the menu)
        and the post-accept-thinking case both produce this shape;
        absent positive evidence (raw=BUSY or fresher JSONL) we
        treat the lingering permit as awaiting attention rather
        than holding cosmetic BUSY for 10+ minutes."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 200, "type": "permit_req"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=300,
            jsonl_age=120, now=205,           # JSONL older than event
            raw="IDLE",
        ) == "PERMIT"

    def test_permit_event_interactive_menu_case_returns_permit_immediately(self):
        """User-reported scenario (2026-05-08): Claude rendered an
        interactive choice menu as a permit-class hook. Latest
        event is notify_permit, JSONL last `tool_use` is from BEFORE
        the menu was rendered. Earlier versions held false BUSY for
        the entire `BUSY_HOOK_JSONL_WINDOW` (10 min) until the JSONL
        aged out of the window. The fix surfaces PERMIT immediately
        so the dashboard correctly says "this project needs your
        attention".

        `raw` reflects what the pane actually shows. A menu ON SCREEN
        matches `PATTERN_PERMIT_FOOTER` (measured 2026-07-26:
        `Enter to select · ↑/↓ to navigate · n to add notes · Esc to
        cancel`), so it arrives as raw=PERMIT and is held at any age
        — that is the case a real waiting menu produces, and it is
        asserted last here. raw=IDLE with a permit-class latest event
        means the modal is NOT on screen (already resolved, typically
        Esc'd), so it is trusted only inside `PERMIT_MAX_TIMEOUT`;
        past that it releases to legacy → IDLE
        (`test_stale_permit_with_idle_screen_releases_to_legacy`)."""
        # 2 seconds after permit
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 1000, "type": "notify_permit"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=2000,
            jsonl_age=40, now=1002,            # event_age=2, jsonl_age=40
            raw="IDLE",
        ) == "PERMIT"
        # 2 minutes after permit
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 1000, "type": "notify_permit"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=2000,
            jsonl_age=158, now=1120,           # event_age=120, jsonl_age=158
            raw="IDLE",
        ) == "PERMIT"
        # 10 minutes after permit, modal ON SCREEN (raw=PERMIT) — the
        # shape a menu the user has left open actually produces. Held
        # as PERMIT no matter how old, because the footer proves the
        # selection is still pending.
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 1000, "type": "notify_permit"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=2000,
            jsonl_age=638, now=1600,           # event_age=600, jsonl_age=638
            raw="PERMIT",
        ) == "PERMIT"
        # Same age, but nothing on screen (raw=IDLE) → the permit was
        # resolved and the event log is stale; defer to legacy.
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 1000, "type": "notify_permit"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=2000,
            jsonl_age=638, now=1600,
            raw="IDLE",
        ) is None

    def test_permit_event_post_dismiss_with_terminal_jsonl_returns_idle(self):
        """Esc-cancel resolved: the user dismissed the modal AND
        Claude has already written a terminal `stop_reason` (e.g.
        `end_turn`) into JSONL. The terminal-fresher-than-event
        branch above returns IDLE before the tool_use promotion
        runs, so a clean cancel never gets the brief false BUSY
        that the post-dismiss-with-stale-JSONL path can briefly
        show."""
        # latest event ts=200, JSONL at ts ≈ now (jsonl_age=2) —
        # fresher than the event AND a terminal stop reason.
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 200, "type": "permit_req"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            jsonl_age=2, now=210,
            raw="IDLE",
        ) == "IDLE"

    def test_phantom_subagent_after_notify_idle_returns_idle(self):
        """Concrete scenario:
        upstream Claude Code fires a spurious `subagent` event in
        an otherwise-idle period (status line / auto-memory / etc).
        Pattern: `... stop, notify_idle, subagent` with no follow-up.
        Real subagent events always come mid-conversation. The
        latest=subagent + prev=notify_idle pattern is exclusive to
        the phantom case. notify_idle is Claude's own "I am idle"
        signal, so committing IDLE is authoritative even if raw
        briefly disagrees."""
        events = (
            {"ts": 100, "type": "stop"},
            {"ts": 110, "type": "notify_idle"},
            {"ts": 200, "type": "subagent"},  # phantom
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=400, now=500,
            raw="IDLE",
        )
        assert result == "IDLE"

    def test_phantom_subagent_after_notify_idle_overrides_raw_busy(self):
        """The whole point of returning IDLE explicitly (rather than
        deferring): if raw=BUSY (e.g. `❯` briefly scrolled off
        screen), legacy's `raw_busy_passthrough` would otherwise
        latch BUSY. notify_idle is the strongest-evidence signal
        Claude is at rest, so we override the visual transient."""
        events = (
            {"ts": 100, "type": "stop"},
            {"ts": 110, "type": "notify_idle"},
            {"ts": 200, "type": "subagent"},
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=400, now=500,
            raw="BUSY",
        )
        assert result == "IDLE"

    def test_phantom_subagent_after_terminal_stop_returns_idle(self):
        """`stop` with terminal `stop_reason` resolves identically to
        a direct `stop` latest event — the phantom did not change
        anything. Same logic as EVENT_CLASS_PAUSE handling.
        Observed in the wild: idle_prompt has documented latency
        (anthropics/claude-code#5186), so claude can sit at rest
        for many minutes after `stop` without `notify_idle` ever
        landing — yet phantom subagent events still fire."""
        events = (
            {"ts": 100, "type": "stop"},
            {"ts": 200, "type": "subagent"},  # phantom; no notify_idle yet
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=300, now=400,
            raw="IDLE",
        )
        assert result == "IDLE"

    def test_phantom_subagent_after_mid_tool_stop_returns_busy(self):
        """`stop` with mid-tool stop_reason (`tool_use`) means a tool
        is still running — Claude is genuinely BUSY. Stripping the
        trailing phantom subagent leaves the `stop` as the latest
        real event, which the activity classifier maps to
        IN_PROGRESS → BUSY. This is symmetric with how a bare
        `[stop]` events list is handled; the phantom guard normalises
        the input so the same answer falls out, rather than
        introducing a special "defer to legacy" case for the
        post-phantom shape."""
        events = (
            {"ts": 100, "type": "stop"},
            {"ts": 200, "type": "subagent"},
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=10, now=210,
            raw="BUSY",
        )
        assert result == "BUSY"

    def test_phantom_subagent_after_session_end_defers(self):
        """`session_end` is a rest-state marker indicating claude has
        exited. With pid_present=True we are in the brief transient
        between SessionEnd hook and the new session's first event;
        defer to legacy so raw (which sees the live process tree)
        is authoritative."""
        events = (
            {"ts": 100, "type": "session_end"},
            {"ts": 200, "type": "subagent"},
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=200, now=300,
            raw="IDLE",
        )
        assert result is None

    def test_phantom_subagent_stacked_chain_resolves_via_notify_idle(self):
        """Multiple phantom subagent events stack up over time.
        Walk back through them; landing on `notify_idle` gives
        IDLE just like a single phantom would."""
        events = (
            {"ts": 100, "type": "stop"},
            {"ts": 110, "type": "notify_idle"},
            {"ts": 200, "type": "subagent"},
            {"ts": 300, "type": "subagent"},
            {"ts": 400, "type": "subagent"},  # latest
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=200, now=500,
            raw="IDLE",
        )
        assert result == "IDLE"

    def test_legitimate_subagent_after_prompt_stays_busy(self):
        """A `subagent` event after a `prompt` (with no notify_idle
        in between) is a legitimate Task tool invocation. Must NOT
        defer — claude is genuinely running a subagent."""
        events = (
            {"ts": 100, "type": "stop"},
            {"ts": 110, "type": "notify_idle"},
            {"ts": 200, "type": "prompt"},  # new user prompt
            {"ts": 220, "type": "subagent"},  # Task tool started
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=10, now=230,
            raw="BUSY",
        )
        assert result == "BUSY"

    def test_legitimate_subagent_mid_tool_chain_stays_busy(self):
        """`subagent` after `posttool` (mid tool chain) is normal."""
        events = (
            {"ts": 100, "type": "prompt"},
            {"ts": 110, "type": "pretool"},
            {"ts": 120, "type": "posttool"},
            {"ts": 130, "type": "subagent"},  # Task spawned
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=5, now=140,
            raw="BUSY",
        )
        assert result == "BUSY"

    def test_phantom_subagent_with_raw_permit_keeps_permit(self):
        """raw=PERMIT (modal on screen) wins even over the phantom-
        subagent shortcut. The override logic for permit-class
        events runs after phantom check and re-applies for safety."""
        events = (
            {"ts": 100, "type": "stop"},
            {"ts": 110, "type": "notify_idle"},
            {"ts": 200, "type": "subagent"},
        )
        result = ccm_activity.derive_state_from_events(
            events=events,
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=3000,
            jsonl_age=400, now=500,
            raw="PERMIT",
        )
        # The phantom-subagent rule has `raw != "PERMIT"` guard so it
        # does NOT fire; falls through to BUSY candidate; final A'
        # override pulls to PERMIT.
        assert result == "PERMIT"

    def test_start_event_phantom_timeout_defers_to_legacy(self):
        """Phantom subagent / abandoned start event scenario:
        latest event is start-class (>10 min old) AND JSONL is
        also stale (>10 min old). claude is clearly not actively
        working — defer to legacy which resolves via
        the legacy fallback to IDLE.

        Concrete scenario: phantom
        `subagent` fired 41 minutes ago, no follow-up, JSONL stale
        from 44 min ago. event-log path returned BUSY indefinitely
        until this fallback was added."""
        # event_ts=100, now=900 → event_age=800 > 600
        # jsonl_age=2658 > 600
        result = ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "subagent"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=7000,
            jsonl_age=2658, now=900,
            raw="IDLE",
        )
        assert result is None  # defer to legacy

    def test_start_event_phantom_timeout_skipped_when_jsonl_fresh(self):
        """Stale event but FRESH JSONL = claude is currently active
        (e.g. responding from a long thinking burst that finally
        emitted text). Don't apply the phantom-timeout fallback."""
        result = ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "prompt"},),
            jsonl_stop_reason="tool_use",
            pid_present=True, claude_pid_age=7000,
            jsonl_age=10, now=900,  # event old, JSONL fresh
            raw="IDLE",
        )
        # Falls through to terminal-release check (jsonl_stop=tool_use
        # not terminal, so no release) → BUSY
        assert result == "BUSY"

    def test_pause_event_with_terminal_jsonl_still_idle(self):
        """The existing PAUSE branch (latest event = stop) already
        returns IDLE for terminal stop_reasons. Verify the new
        fallback doesn't break it."""
        assert ccm_activity.derive_state_from_events(
            events=({"ts": 100, "type": "stop"},),
            jsonl_stop_reason="end_turn",
            pid_present=True, claude_pid_age=300,
            jsonl_age=10,
        ) == "IDLE"

