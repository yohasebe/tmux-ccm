"""A subagent's hook events, kept apart from the main conversation's.

Background agents keep running after the main turn has ended. Their
tool events must not make the conversation BUSY: the hooks keep only
the newest one's time (`agent_ts`), outside the event log. A
subagent's open permission request is kept in a file of its own and
passed as `agent_permit_ts`; it shows as PERMIT. While agents run, an
IDLE window carries the `(bg)` suffix. Logs written by earlier
versions hold `agent` / `agent_permit` lines; they read the same way.
"""

import ccm_activity
import ccm_core
import ccm_detection
import ccm_rules
import ccm_signals

from conftest import make_ctx

NOW = 1_000_000


def ev(t, age):
    return {"ts": NOW - age, "type": t}


def derive(events, raw="IDLE", stop="end_turn", jsonl_age=900, agent_ts=0,
           agent_permit_ts=0):
    return ccm_activity.derive_state_from_events(
        events, stop, pid_present=True, claude_pid_age=5000,
        raw=raw, jsonl_age=jsonl_age, now=NOW, agent_ts=agent_ts,
        agent_permit_ts=agent_permit_ts)


FOREGROUND_CALL = [ev("prompt", 130), ev("pretool", 120)]


ENDED_TURN = [ev("prompt", 1000), ev("pretool", 950), ev("stop", 900)]


class TestDerive:
    def test_agent_events_after_the_turn_ended_leave_it_idle(self):
        events = ENDED_TURN + [ev("agent", 3), ev("agent", 1)]
        assert derive(events) == "IDLE"

    def test_without_the_agent_type_the_same_events_read_busy(self):
        # Control: before the split, these were pretool events.
        events = ENDED_TURN + [ev("pretool", 3), ev("posttool", 1)]
        assert derive(events, jsonl_age=-1) == "BUSY"

    def test_an_agent_permission_request_waits(self):
        events = ENDED_TURN + [ev("agent", 5), ev("agent_permit", 2)]
        assert derive(events) == "PERMIT"

    def test_an_unanswered_agent_request_ages_by_its_own_clock(self):
        old = ccm_activity.PERMIT_MAX_TIMEOUT + 1
        events = ENDED_TURN + [ev("agent_permit", old)]
        assert derive(events) is None

    def test_an_answered_agent_permission_request_is_cleared(self):
        events = ENDED_TURN + [ev("agent_permit", 5), ev("agent", 2)]
        assert derive(events) == "IDLE"

    def test_an_agent_event_does_not_clear_the_main_request(self):
        events = [ev("prompt", 100), ev("pretool", 90), ev("permit_req", 60),
                  ev("agent", 2)]
        assert derive(events, stop="tool_use", jsonl_age=90) == "PERMIT"

    def test_a_main_tool_event_after_agents_is_busy(self):
        # The agents finished and the main conversation went on.
        events = ENDED_TURN + [ev("agent", 30), ev("prompt", 5), ev("pretool", 1)]
        assert derive(events, stop="tool_use", jsonl_age=1) == "BUSY"

    def test_a_turn_in_progress_stays_busy_while_its_agent_works(self):
        # A foreground agent: the main conversation's Agent call is
        # still the main conversation's latest event.
        events = [ev("prompt", 30), ev("pretool", 20), ev("agent", 2)]
        assert derive(events, raw="BUSY", stop="tool_use", jsonl_age=20) == "BUSY"


class TestOpenAgentRequest:
    def test_an_open_request_after_the_turn_waits(self):
        assert derive(ENDED_TURN, agent_permit_ts=NOW - 2) == "PERMIT"

    def test_no_open_request_stays_idle(self):
        assert derive(ENDED_TURN) == "IDLE"

    def test_a_request_in_the_same_second_as_the_stop_waits(self):
        events = [ev("prompt", 30), ev("pretool", 20), ev("stop", 5)]
        assert derive(events, jsonl_age=5, agent_permit_ts=NOW - 5) == "PERMIT"

    def test_a_main_event_after_the_request_decides(self):
        events = ENDED_TURN + [ev("prompt", 5), ev("pretool", 1)]
        assert derive(events, stop="tool_use", jsonl_age=1,
                      agent_permit_ts=NOW - 10) == "BUSY"

    def test_an_old_open_request_ages_by_its_own_clock(self):
        old = ccm_activity.PERMIT_MAX_TIMEOUT + 1
        assert derive(ENDED_TURN, jsonl_age=old + 900,
                      agent_permit_ts=NOW - old) is None

    def test_old_answer_lines_do_not_hide_a_new_request(self):
        events = ENDED_TURN + [ev("agent_permit", 50), ev("agent", 40)]
        assert derive(events, agent_permit_ts=NOW - 2) == "PERMIT"


class TestUnreadableTranscript:
    # With no readable transcript, an unfinished main call ages by its
    # newest event; a subagent working under it keeps it fresh.
    def test_a_working_foreground_agent_keeps_the_call_busy(self):
        assert derive(FOREGROUND_CALL, stop=None, jsonl_age=-1,
                      agent_ts=NOW - 1) == "BUSY"

    def test_without_agent_activity_the_old_call_is_released(self):
        assert derive(FOREGROUND_CALL, stop=None, jsonl_age=-1) is None

    def test_old_agent_activity_does_not_keep_it(self):
        old = ccm_activity.BUSY_STALE_RELEASE_SEC + 1
        assert derive(FOREGROUND_CALL, stop=None, jsonl_age=-1,
                      agent_ts=NOW - old) is None

    def test_a_readable_transcript_keeps_its_own_clock(self):
        old = ccm_activity.BUSY_STALE_RELEASE_SEC + 1
        assert derive(FOREGROUND_CALL, stop=None, jsonl_age=old,
                      agent_ts=NOW - 1) is None

    def test_agent_activity_does_not_keep_an_old_main_request(self):
        old = ccm_activity.PERMIT_MAX_TIMEOUT + 1
        events = [ev("prompt", old + 20), ev("pretool", old + 10),
                  ev("permit_req", old)]
        assert derive(events, stop=None, jsonl_age=-1,
                      agent_ts=NOW - 1) is None

    def test_agent_activity_after_the_turn_ended_stays_idle(self):
        assert derive(ENDED_TURN, jsonl_age=-1, agent_ts=NOW - 1) == "IDLE"


class TestBackgroundWorkReported:
    def _stop(self, ts, bg=None):
        e = ev("stop", ts)
        if bg is not None:
            e["bg"] = bg
        return e

    def test_a_stop_reporting_background_work(self):
        events = [ev("prompt", 30), self._stop(20, bg=1)]
        assert ccm_activity.background_work_reported(events)

    def test_a_stop_without_the_field(self):
        assert not ccm_activity.background_work_reported(ENDED_TURN)

    def test_a_later_stop_reporting_none_ends_it(self):
        events = [self._stop(30, bg=1), ev("prompt", 20), self._stop(10)]
        assert not ccm_activity.background_work_reported(events)

    def test_a_turn_that_ends_without_a_stop_keeps_it(self):
        """Esc ends a turn without a Stop; the work runs on."""
        events = [self._stop(30, bg=1), ev("prompt", 20), ev("pretool", 5)]
        assert ccm_activity.background_work_reported(events)

    def test_session_end_ends_it(self):
        events = [self._stop(30, bg=1), ev("session_end", 5)]
        assert not ccm_activity.background_work_reported(events)

    def test_idle_notice_and_subagent_lines_are_not_main_events(self):
        events = [self._stop(30, bg=2), ev("notify_idle", 20),
                  ev("agent_permit", 10), ev("agent", 5)]
        assert ccm_activity.background_work_reported(events)

    def test_a_malformed_count_is_none(self):
        for bg in (0, -1, "1", True, 1.5, None):
            events = [self._stop(30, bg=bg)]
            assert not ccm_activity.background_work_reported(events), bg

    def test_no_events(self):
        assert not ccm_activity.background_work_reported([])

    def test_the_kept_count_decides_when_the_tail_holds_no_stop(self):
        events = [ev("prompt", 20), ev("pretool", 5)]
        assert ccm_activity.background_work_reported(events, 1)
        assert not ccm_activity.background_work_reported(events, 0)

    def test_a_stop_in_the_tail_outranks_the_kept_count(self):
        assert not ccm_activity.background_work_reported([self._stop(5)], 1)
        events = [self._stop(30, bg=1), ev("session_end", 5)]
        assert not ccm_activity.background_work_reported(events, 1)

    def test_a_malformed_kept_count_is_none(self):
        for bg in (-1, "1", True, 1.5, None):
            assert not ccm_activity.background_work_reported([], bg), bg

    def test_counts_reported_turn_by_turn(self):
        """Each finishing piece of work runs a turn whose Stop reports
        what is left: 2, then 1, then none."""
        events = [self._stop(60, bg=2)]
        assert ccm_activity.background_work_reported(events)
        events += [ev("prompt", 50), self._stop(40, bg=1)]
        assert ccm_activity.background_work_reported(events)
        events += [ev("prompt", 30), self._stop(20)]
        assert not ccm_activity.background_work_reported(events)

    def test_an_old_report_has_no_time_limit(self):
        events = [self._stop(ccm_activity.BUSY_HOOK_JSONL_WINDOW * 100, bg=1)]
        assert ccm_activity.background_work_reported(events)


class TestBackgroundSuffix:
    def _apply(self, monkeypatch, state, **ctx):
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *a, **k: calls.append(a) or "")
        c = make_ctx(**ctx)
        rule, _ = ccm_rules.evaluate_rules(c)
        ccm_detection.apply_actions("0:5", "", c, rule, state)
        return [a for a in calls if "@ccm_bg_active" in a]

    def test_idle_with_agents_sets_bg(self, monkeypatch):
        writes = self._apply(monkeypatch, "IDLE", raw="IDLE",
                             background_work=True)
        assert writes == [("set-option", "-wt", "0:5", "@ccm_bg_active", "1")]

    def test_idle_without_agents_sets_nothing(self, monkeypatch):
        assert self._apply(monkeypatch, "IDLE", raw="IDLE") == []

    def test_agents_ending_clears_bg(self, monkeypatch):
        writes = self._apply(monkeypatch, "IDLE", raw="IDLE",
                             prev_bg_active=True)
        assert writes == [("set-option", "-wut", "0:5", "@ccm_bg_active")]


class TestContext:
    def test_resolve_passes_the_open_request(self, monkeypatch):
        monkeypatch.setattr(ccm_signals, "read_events_tail",
                            lambda *a, **k: tuple(ENDED_TURN))
        c = make_ctx(raw="IDLE", session_id="s1", jsonl_last_stop_reason="end_turn",
                     jsonl_age=900, now=NOW, claude_pid_age=5000,
                     agent_permit_ts=NOW - 2)
        _, _, event_state = ccm_detection.resolve_state_from_context(c, "/p")
        assert event_state == "PERMIT"

    def test_resolve_passes_the_agent_time(self, monkeypatch):
        monkeypatch.setattr(ccm_signals, "read_events_tail",
                            lambda *a, **k: tuple(FOREGROUND_CALL))
        c = make_ctx(raw="IDLE", session_id="s1", jsonl_last_stop_reason=None,
                     jsonl_age=-1, now=NOW, claude_pid_age=5000,
                     agent_ts=NOW - 1)
        _, _, event_state = ccm_detection.resolve_state_from_context(c, "/p")
        assert event_state == "BUSY"
