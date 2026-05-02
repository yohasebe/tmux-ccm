"""User-centric activity classification + event-log state derivation.

This module owns the **primary** detection path. It decides what
Claude is *doing* — `AT_REST` / `AWAITING_PERMIT` / `IN_PROGRESS` /
`UNKNOWN` — based on the event log tail (written by the bash
hooks under `hooks/`) plus JSONL stop_reason and process-lifecycle
signals. The activity is then mapped to the user-facing state
(`SHELL` / `IDLE` / `BUSY` / `PERMIT`) by a small decision tree.

The split into two phases (`classify_activity` →
`map_activity_to_state`) is deliberate: activity answers the only
question the user really cares about ("do I need to do something
right now?"), and the mapping layer adds the on-screen overrides
(raw=PERMIT promotes, JSONL tool_use within the long-tool window
keeps BUSY, etc.).

`derive_state_from_events` is the public entry point. It returns
None when no authoritative answer is available (empty event log,
malformed records, unknown event types, `session_end` with a live
pid). The orchestrator treats None as "fall back to the legacy
`ccm_rules.DETECTION_RULES` table".

Phantom-subagent normalisation (`_strip_phantom_subagents`) lives
here too: a trailing run of `subagent` events whose immediate
predecessor is a rest marker (`notify_idle` / `stop` /
`session_end`) is upstream noise — Task tools that fire after the
turn already ended. Stripping them lets the classifier see the
real terminator at the end of the tail.
"""

from typing import Optional

from ccm_constants import BUSY_HOOK_JSONL_WINDOW, TERMINAL_STOP_REASONS
from ccm_jsonl import JSONL_HOOK_GAP_TOLERANCE


# ─── Event class taxonomy ───
# Event type → state class mapping. Keep in sync with the
# normalised vocabulary emitted by `hooks/lib.sh::ccm_append_event`.
# Adding a new upstream hook event means: pick its normalized name
# at the writer, add it here, and add a parametrized test.
EVENT_CLASS_START = "start"   # → BUSY
EVENT_CLASS_PERMIT = "permit"  # → PERMIT
EVENT_CLASS_PAUSE = "pause"   # → IDLE (terminal) or BUSY (tool_use)
EVENT_CLASS_IDLE = "idle"     # → IDLE (explicit)
EVENT_CLASS_END = "end"       # → SHELL

EVENT_CLASSES = {
    "prompt": EVENT_CLASS_START,
    "pretool": EVENT_CLASS_START,
    "posttool": EVENT_CLASS_START,
    "subagent": EVENT_CLASS_START,
    "compact": EVENT_CLASS_START,
    "stop": EVENT_CLASS_PAUSE,
    "permit_req": EVENT_CLASS_PERMIT,
    "notify_permit": EVENT_CLASS_PERMIT,
    "notify_idle": EVENT_CLASS_IDLE,
    "session_end": EVENT_CLASS_END,
}


# ─── Activity taxonomy ───
ACTIVITY_AT_REST = "at_rest"
ACTIVITY_AWAITING_PERMIT = "awaiting_permit"
ACTIVITY_IN_PROGRESS = "in_progress"
ACTIVITY_UNKNOWN = "unknown"

_REST_MARKERS = ("notify_idle", "stop", "session_end")


# ─── Time-comparison primitives ───

def _jsonl_fresher_than_event(latest, jsonl_age, now):
    """Return True iff JSONL was updated AFTER the latest event-log
    record. Bare time-comparison primitive — no stop_reason or
    window checks. Callers add their own conditions on top.

    Used by the Esc / hook-silence release paths to reject the
    "fresh prompt right after a previous turn ended" false-positive
    (the JSONL terminal in that case predates the new event).

    Returns False when:
      - jsonl_age is unavailable (-1)
      - the latest event has no parsable timestamp
      - `now` is unavailable (0)
      - the latest event is fresher than (or equal to) JSONL
    """
    if jsonl_age < 0:
        return False
    event_ts = latest.get("ts", 0) if isinstance(latest, dict) else 0
    if not (now > 0 and event_ts > 0):
        return False
    event_age = now - event_ts
    return event_age > jsonl_age


def _jsonl_terminal_fresher_than_event(latest, jsonl_stop_reason,
                                       jsonl_age, now):
    """Combines `_jsonl_fresher_than_event` with the terminal
    stop_reason check + the `JSONL_HOOK_GAP_TOLERANCE` window. Used
    by the start-class and permit-class Esc-fallback paths."""
    if jsonl_stop_reason not in TERMINAL_STOP_REASONS:
        return False
    if jsonl_age > JSONL_HOOK_GAP_TOLERANCE:
        return False
    return _jsonl_fresher_than_event(latest, jsonl_age, now)


# ─── Event-log normalisation ───

def _strip_phantom_subagents(events_seq):
    """Return events_seq with trailing phantom subagent records
    removed. Subagents at the end of the log are phantom iff the
    immediately-preceding non-subagent event is a rest marker
    (`notify_idle` / `stop` / `session_end`). Mid-conversation
    subagents (preceded by `prompt` / `pretool` / `posttool` /
    etc.) are legitimate Task-tool invocations and stay untouched.

    Pure function; takes and returns a tuple."""
    if not events_seq:
        return events_seq
    i = len(events_seq) - 1
    while i >= 0:
        e = events_seq[i]
        if isinstance(e, dict) and e.get("type") == "subagent":
            i -= 1
            continue
        break
    if i < 0:
        # All-subagent log — no rest marker to test against. Keep
        # as-is; the classifier will fall through to UNKNOWN.
        return events_seq
    if i == len(events_seq) - 1:
        return events_seq  # no trailing subagents
    pred = events_seq[i]
    pred_t = pred.get("type") if isinstance(pred, dict) else None
    if pred_t in _REST_MARKERS:
        return events_seq[: i + 1]
    return events_seq


# ─── Activity classification ───

def classify_activity(events, jsonl_stop_reason, jsonl_age, raw, now):
    """Inspect the event-log tail (after phantom-subagent stripping)
    and JSONL signals to decide what Claude is doing right now.
    Returns (activity, evidence_event) where activity is one of the
    `ACTIVITY_*` constants and evidence_event is the event the
    decision was based on (or None for UNKNOWN cases without a
    contributing event)."""
    events_seq = tuple(events) if not isinstance(events, tuple) else events
    events_seq = _strip_phantom_subagents(events_seq)
    if not events_seq:
        return ACTIVITY_UNKNOWN, None

    latest = events_seq[-1]
    t = latest.get("type") if isinstance(latest, dict) else None
    if not t:
        return ACTIVITY_UNKNOWN, None
    klass = EVENT_CLASSES.get(t)
    if klass is None:
        # Unknown upstream event type — let legacy decide.
        return ACTIVITY_UNKNOWN, latest

    if klass == EVENT_CLASS_END:
        # session_end with live pid: brief transient between
        # SessionEnd hook and the new session's first event. The
        # process tree (raw) is authoritative; defer.
        return ACTIVITY_UNKNOWN, latest

    if klass == EVENT_CLASS_IDLE:
        return ACTIVITY_AT_REST, latest

    if klass == EVENT_CLASS_PAUSE:
        # `stop` event. Terminal stop_reason → turn truly ended,
        # at rest. tool_use OR missing stop_reason → claude paused
        # awaiting a tool result, in progress (conservative for
        # missing data: long-running tools without clear evidence
        # must not flip to false IDLE).
        if jsonl_stop_reason in TERMINAL_STOP_REASONS:
            return ACTIVITY_AT_REST, latest
        return ACTIVITY_IN_PROGRESS, latest

    if klass == EVENT_CLASS_PERMIT:
        # Permit event raised. If JSONL shows the response actually
        # ended with a terminal stop_reason fresher than the permit
        # event, the modal was Esc-dismissed cleanly and Claude
        # wrote a final assistant record — at rest.
        if _jsonl_terminal_fresher_than_event(
                latest, jsonl_stop_reason, jsonl_age, now):
            return ACTIVITY_AT_REST, latest
        return ACTIVITY_AWAITING_PERMIT, latest

    if klass == EVENT_CLASS_START:
        # prompt / pretool / posttool / compact / non-phantom
        # subagent. Esc-interrupt path: a terminal JSONL stop_reason
        # newer than the start event proves the response ended
        # even though no Stop hook fired (Esc bypasses Stop, or
        # hooks went silent under #16047-class regressions).
        if (raw != "PERMIT" and _jsonl_terminal_fresher_than_event(
                latest, jsonl_stop_reason, jsonl_age, now)):
            return ACTIVITY_AT_REST, latest
        # Combined-stale: latest start event AND JSONL both stale
        # past the long-tool window — the session has effectively
        # abandoned the in-progress claim. Let legacy decide.
        if raw != "PERMIT":
            event_ts = latest.get("ts", 0) if isinstance(latest, dict) else 0
            if (now > 0 and event_ts > 0
                    and now - event_ts > BUSY_HOOK_JSONL_WINDOW
                    and 0 <= jsonl_age
                    and jsonl_age > BUSY_HOOK_JSONL_WINDOW):
                return ACTIVITY_UNKNOWN, latest
        return ACTIVITY_IN_PROGRESS, latest

    # Defensive: a new EVENT_CLASS_* added to the vocabulary but
    # not handled here surfaces as legacy fallback rather than a
    # hard error.
    return ACTIVITY_UNKNOWN, latest


# ─── Activity → state mapping ───

def map_activity_to_state(activity, raw, jsonl_stop_reason, jsonl_age):
    """Map (activity, raw observation, JSONL signals) to a definite
    state, or None to defer to legacy detection.

    Ordering invariant: activity decides whether derive commits at
    all, then raw=PERMIT (capture-pane authority for "modal on
    screen") promotes a committed non-PERMIT candidate to PERMIT.
    UNKNOWN activity returns None even when raw=PERMIT — the rule
    "no event-log signal → legacy fallback" is preserved end-to-end
    so any future legacy-only logic (e.g. raw_permit_passthrough)
    keeps working consistently."""
    candidate = None

    if activity == ACTIVITY_AT_REST:
        candidate = "IDLE"
    elif activity == ACTIVITY_IN_PROGRESS:
        candidate = "BUSY"
    elif activity == ACTIVITY_AWAITING_PERMIT:
        # Modal raised by the event log. raw=PERMIT (modal still on
        # screen) is handled by the override below. Otherwise the
        # modal was dismissed — accept (tool resumed) or Esc (tool
        # aborted). JSONL `tool_use` within the long-tool window
        # plus raw in {BUSY, IDLE} means a tool is actively running
        # post-dismiss; promote to BUSY rather than holding cosmetic
        # PERMIT for the tool's whole duration. Any other shape
        # stays PERMIT (the `(Nm)` stale-signal suffix surfaces the
        # stuck nature to the user).
        if (jsonl_stop_reason == "tool_use"
                and 0 <= jsonl_age <= BUSY_HOOK_JSONL_WINDOW
                and raw in ("BUSY", "IDLE")):
            candidate = "BUSY"
        else:
            candidate = "PERMIT"
    # ACTIVITY_UNKNOWN: candidate stays None.

    if candidate is None:
        return None

    # Capture-pane authority. raw=PERMIT (modal physically rendered)
    # promotes a non-PERMIT candidate to PERMIT — the modal could
    # have appeared after the latest event landed, or the
    # PermissionRequest hook could have failed silently
    # (anthropics/claude-code#16047 class).
    if raw == "PERMIT" and candidate != "PERMIT":
        return "PERMIT"
    return candidate


# ─── Public entry point ───

def derive_state_from_events(events, jsonl_stop_reason,
                             pid_present, claude_pid_age, raw=None,
                             jsonl_age=-1, now=0):
    """Pure function: resolve state from event-log tail + JSONL + pid.

    Arguments:
        events: iterable of {"ts": int, "type": str} dicts, newest last.
            Empty iterable means "no event log for this project yet".
        jsonl_stop_reason: string or None. The stop_reason of the most
            recent assistant record in the JSONL tail.
        pid_present: bool. True iff a `claude` process currently runs
            for this project's window.
        claude_pid_age: int. Seconds since the `claude` process
            started, or -1 if unknown. Used to suppress the
            false-IDLE that would otherwise apply during MCP-loading
            startup with no events recorded yet.
        raw: optional capture-pane classification ("IDLE" / "BUSY" /
            "PERMIT" / "SHELL" / "DOWN"). When `raw=="PERMIT"` the
            function returns "PERMIT" regardless of event-log
            content — the pane footer match is the authoritative
            signal for modal-blocked panes.
        jsonl_age: optional. Seconds since the most recent JSONL
            real-activity record, or -1 if unavailable.

    Returns one of: "SHELL", "PERMIT", "BUSY", "IDLE", or None.
    None means "no authoritative answer" — the caller should commit
    the legacy state instead.
    """
    if not pid_present:
        return "SHELL"

    events_seq = tuple(events) if not isinstance(events, tuple) else events

    activity, _evidence = classify_activity(
        events_seq, jsonl_stop_reason, jsonl_age, raw, now,
    )
    return map_activity_to_state(
        activity, raw, jsonl_stop_reason, jsonl_age,
    )
