"""Property-based tests for `derive_state_from_events`.

Where the parametric tests in `test_ccm_core.py::TestDeriveStateFromEvents`
exercise specific hook sequences, this file verifies invariants that
must hold across the entire input space:

  - never raises (any input → either a valid state or None)
  - returns a value drawn from the documented set
  - process-tree authority: pid_present=False → SHELL
  - capture-pane authority: raw=PERMIT → PERMIT or None (never BUSY/IDLE/SHELL)
  - state-set membership of returned values

Hypothesis generates events whose `type` field is drawn from the
EVENT_CLASSES vocabulary plus garbage to exercise the "unknown event
type → None" branch. Timestamps, raw, JSONL stop_reason, pid_age,
and jsonl_age are all drawn from realistic ranges.

Skipped silently when hypothesis is not installed so the suite stays
green on systems where the property-test extra has not been added.
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import ccm_core
from ccm_detection import derive_state_from_events, EVENT_CLASSES


hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st, settings, HealthCheck


VALID_RESULTS = {None, "SHELL", "BUSY", "IDLE", "PERMIT"}
KNOWN_EVENT_TYPES = tuple(EVENT_CLASSES.keys())
RAW_CHOICES = (None, "DOWN", "SHELL", "BUSY", "IDLE", "PERMIT")
TERMINAL_REASONS = ("end_turn", "max_tokens", "stop_sequence")
NONTERMINAL_REASONS = ("tool_use", "user_pending")
ALL_STOP_REASONS = TERMINAL_REASONS + NONTERMINAL_REASONS + (None,)

# Pool a typical "now" value plus a few extreme edges so the search
# explores boundary conditions where event_ts > now (clock skew),
# now=0 (unknown clock), and the normal forward-time case.
NOW_CHOICES = (0, 1_000_000, 1_777_777_777)


def _event(types_st):
    """Build an event-shaped dict whose `type` is drawn from `types_st`.

    Includes a small chance of producing a malformed record (no `type`,
    no `ts`, or non-dict) so the "malformed → None" branch gets covered.
    """
    return st.one_of(
        st.fixed_dictionaries({
            "ts": st.integers(min_value=0, max_value=2_000_000_000),
            "type": types_st,
        }),
        # Malformed shapes
        st.fixed_dictionaries({"ts": st.integers(min_value=0, max_value=10)}),
        st.fixed_dictionaries({"type": types_st}),
        st.just("not-a-dict"),
        st.just(None),
    )


# Event types: known names from EVENT_CLASSES, plus a small alphabet
# of unknown strings to exercise the "unknown event type" branch.
event_type_strategy = st.one_of(
    st.sampled_from(KNOWN_EVENT_TYPES),
    st.text(alphabet="abcdef_", min_size=1, max_size=12),
)

events_strategy = st.lists(
    _event(event_type_strategy),
    min_size=0, max_size=8,
).map(tuple)


@given(
    events=events_strategy,
    jsonl_stop_reason=st.sampled_from(ALL_STOP_REASONS),
    pid_present=st.booleans(),
    claude_pid_age=st.integers(min_value=-1, max_value=100_000),
    jsonl_age=st.integers(min_value=-1, max_value=100_000),
    raw=st.sampled_from(RAW_CHOICES),
    now=st.sampled_from(NOW_CHOICES),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_derive_never_raises_and_returns_valid_state(
    events, jsonl_stop_reason, pid_present, claude_pid_age, jsonl_age, raw, now
):
    """The function must be total over its input space: any
    combination of inputs yields a value in {None, SHELL, BUSY,
    IDLE, PERMIT}, never an exception."""
    result = derive_state_from_events(
        events=events,
        jsonl_stop_reason=jsonl_stop_reason,
        pid_present=pid_present,
        claude_pid_age=claude_pid_age,
        jsonl_age=jsonl_age,
        raw=raw,
        now=now,
    )
    assert result in VALID_RESULTS, (
        f"unexpected result {result!r} for events={events!r} "
        f"raw={raw!r} pid_present={pid_present} jsonl={jsonl_stop_reason!r}"
    )


@given(
    events=events_strategy,
    jsonl_stop_reason=st.sampled_from(ALL_STOP_REASONS),
    claude_pid_age=st.integers(min_value=-1, max_value=100_000),
    jsonl_age=st.integers(min_value=-1, max_value=100_000),
    raw=st.sampled_from(RAW_CHOICES),
    now=st.sampled_from(NOW_CHOICES),
)
@settings(max_examples=200)
def test_pid_absent_always_resolves_shell(
    events, jsonl_stop_reason, claude_pid_age, jsonl_age, raw, now
):
    """Process tree is authoritative for SHELL — when claude is not
    running, no event-log shape can override that."""
    result = derive_state_from_events(
        events=events,
        jsonl_stop_reason=jsonl_stop_reason,
        pid_present=False,
        claude_pid_age=claude_pid_age,
        jsonl_age=jsonl_age,
        raw=raw,
        now=now,
    )
    assert result == "SHELL"


@given(
    events=events_strategy,
    jsonl_stop_reason=st.sampled_from(ALL_STOP_REASONS),
    pid_present=st.booleans(),
    claude_pid_age=st.integers(min_value=-1, max_value=100_000),
    jsonl_age=st.integers(min_value=-1, max_value=100_000),
    now=st.sampled_from(NOW_CHOICES),
)
@settings(max_examples=200)
def test_raw_permit_never_resolves_to_busy_idle_shell(
    events, jsonl_stop_reason, pid_present, claude_pid_age, jsonl_age, now
):
    """Capture-pane authority: when a permission modal is physically
    on screen (raw=PERMIT), the result must be either PERMIT or None
    (defer to legacy, which itself returns PERMIT). The function
    must never overrule a visible modal with BUSY/IDLE/SHELL."""
    result = derive_state_from_events(
        events=events,
        jsonl_stop_reason=jsonl_stop_reason,
        pid_present=pid_present,
        claude_pid_age=claude_pid_age,
        jsonl_age=jsonl_age,
        raw="PERMIT",
        now=now,
    )
    # pid_present=False short-circuits to SHELL above; that path is
    # exempt because the process tree is authoritative for SHELL.
    if not pid_present:
        assert result == "SHELL"
    else:
        assert result in (None, "PERMIT"), (
            f"raw=PERMIT must not yield {result!r} when pid is alive "
            f"(events={events!r}, jsonl_reason={jsonl_stop_reason!r})"
        )


@given(
    events=events_strategy,
    jsonl_stop_reason=st.sampled_from(ALL_STOP_REASONS),
    claude_pid_age=st.integers(min_value=-1, max_value=100_000),
    jsonl_age=st.integers(min_value=-1, max_value=100_000),
    raw=st.sampled_from(("BUSY", "IDLE")),
    now=st.sampled_from(NOW_CHOICES),
)
@settings(max_examples=200)
def test_pid_present_does_not_resolve_shell(
    events, jsonl_stop_reason, claude_pid_age, jsonl_age, raw, now
):
    """When the process tree shows claude is alive (raw is BUSY or
    IDLE — both indicate a live pid), no event-log derivation may
    return SHELL. SHELL is reserved for the process-tree-absent
    case."""
    result = derive_state_from_events(
        events=events,
        jsonl_stop_reason=jsonl_stop_reason,
        pid_present=True,
        claude_pid_age=claude_pid_age,
        jsonl_age=jsonl_age,
        raw=raw,
        now=now,
    )
    assert result != "SHELL"


@given(
    events=st.just(()),
    jsonl_stop_reason=st.sampled_from(ALL_STOP_REASONS),
    pid_present=st.booleans(),
    claude_pid_age=st.integers(min_value=-1, max_value=100_000),
    jsonl_age=st.integers(min_value=-1, max_value=100_000),
    raw=st.sampled_from(RAW_CHOICES),
    now=st.sampled_from(NOW_CHOICES),
)
@settings(max_examples=80)
def test_empty_events_with_pid_returns_none_for_legacy_fallback(
    events, jsonl_stop_reason, pid_present, claude_pid_age, jsonl_age, raw, now
):
    """Empty event log + pid present means hooks have not yet
    written for this session. derive must return None to defer to
    legacy detection, except for the pid-absent shortcut which
    still resolves to SHELL."""
    result = derive_state_from_events(
        events=events,
        jsonl_stop_reason=jsonl_stop_reason,
        pid_present=pid_present,
        claude_pid_age=claude_pid_age,
        jsonl_age=jsonl_age,
        raw=raw,
        now=now,
    )
    if pid_present:
        assert result is None
    else:
        assert result == "SHELL"
