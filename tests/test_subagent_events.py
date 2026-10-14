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


class TestSubagentsActive:
    def test_agent_activity_after_the_main_turn(self):
        assert ccm_activity.subagents_active(ENDED_TURN, NOW - 5, NOW)

    def test_no_agent_activity(self):
        assert not ccm_activity.subagents_active(ENDED_TURN, 0, NOW)

    def test_main_event_after_the_agents(self):
        events = ENDED_TURN + [ev("stop", 2)]
        assert not ccm_activity.subagents_active(events, NOW - 30, NOW)

    def test_main_event_in_the_same_second(self):
        events = ENDED_TURN + [ev("stop", 5)]
        assert not ccm_activity.subagents_active(events, NOW - 5, NOW)

    def test_old_agent_activity(self):
        old = ccm_activity.BUSY_HOOK_JSONL_WINDOW + 1
        assert not ccm_activity.subagents_active([], NOW - old, NOW)

    def test_answer_lines_are_not_main_events(self):
        # The answering line is written by the same event as the time.
        events = ENDED_TURN + [ev("agent_permit", 10), ev("agent", 5)]
        assert ccm_activity.subagents_active(events, NOW - 5, NOW)

    def test_no_main_event_yet(self):
        assert ccm_activity.subagents_active([], NOW - 5, NOW)


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
                             subagents_active=True)
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
