"""Trace-replay regression corpus.

Replays REAL captured event logs (hooks/<sessionId>.events.jsonl
files lifted verbatim from production incidents) through
`derive_state_from_events` and asserts the expected state at probe
points along the timeline.

Why this exists: every detection bug ccm has had shares one shape —
the upstream hook vocabulary has holes (no "permission resolved"
event, no tool heartbeat, Stop missing on Esc, …) and ccm bridges
them with heuristics over noisy side channels (capture-pane, JSONL,
process tree). Each bridge trades off against the others (e.g. the
interactive-menu false-BUSY fix vs. the approved-long-tool false-
PERMIT it left behind). A corpus of real traces makes those
trade-offs *executable*: any future tuning of `classify_activity` /
`map_activity_to_state` must keep every recorded scenario green, so
a fix for one incident can no longer silently regress another.

Adding a new incident to the corpus:
  1. Copy the session's events.jsonl from `$TMPDIR/ccm-$UID/hooks/`
     into `tests/fixtures/traces/` (events are bare {ts, type}
     records — no sensitive content). Truncate at a natural boundary
     (e.g. session_end) so the fixture is deterministic.
  2. Add a probe table: (T, derive inputs at T, expected state).
     The JSONL / raw inputs are NOT in the trace — reconstruct them
     from the session's debug data and say so in the probe comment.
  3. Known-bad probes (the bug being investigated) get
     `xfail(strict=True)` with the DESIRED expectation: the test
     flips to an error the moment the fix lands, forcing the marker
     to be removed and the probe to become a permanent regression
     guard.
"""

import json
import os

import pytest

import ccm_activity

TRACES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "traces")


def load_trace(name):
    """Load a fixture trace into a tuple of {ts, type} dicts."""
    path = os.path.join(TRACES_DIR, name)
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return tuple(events)


def replay_at(events, now, jsonl_stop_reason, jsonl_age, raw,
              pid_present=True, claude_pid_age=7200):
    """Run derive on the slice of `events` visible at time `now`."""
    visible = tuple(e for e in events if e.get("ts", 0) <= now)
    return ccm_activity.derive_state_from_events(
        visible, jsonl_stop_reason, pid_present, claude_pid_age,
        raw=raw, jsonl_age=jsonl_age, now=now,
    )


class TestPermitLongTool:
    """An approved permission whose tool then ran for minutes.

    A test-suite run inside a container took 12+
    minutes. The PreToolUse for it raised a permission request
    (events: pretool → permit_req → notify_permit), the user
    approved, and the tool ran — but Claude Code does NOT re-fire
    PreToolUse after approval, so `permit_req`/`notify_permit`
    stayed the newest events until the tool's posttool landed
    ~5 minutes later. Throughout that window the dashboard showed
    ⚠PERMIT (with the honest `(3m)` age suffix) for a session that
    was actively executing — the contradiction is visible on screen,
    with the preview pane streaming the tool's output next to a
    PERMIT badge.

    Neither promotion path in `classify_activity`'s permit branch
    fired:
      - raw was IDLE, not BUSY: the claude pane kept its `❯` input
        prompt visible during the run (documented behaviour — the
        user can type queued input), and the window's second pane
        was a plain zsh.
      - JSONL was OLDER than the permit event: the assistant's
        tool_use record was written when the tool was *called*
        (before the permission), and a long-running shell command
        writes nothing further until it completes.

    Trace fixture: 1121 events, from the first `prompt` through three
    permit cycles, the bug window, `stop`, `notify_idle`, a phantom
    `subagent`, to `session_end`. Offsets below are seconds from the
    fixture's synthetic epoch.
    """

    EVENTS = load_trace("permit_long_tool.events.jsonl")

    # Key points in the trace (epoch + seconds):
    #   1000017061  pretool        ← the long tool's call
    #   1000017061  permit_req
    #   1000017067  notify_permit
    #   1000017381  posttool       ← the tool finished
    #   1000017409  pretool        ← churn resumed
    #   1000017473  stop
    #   1000017533  notify_idle
    #   1000017663  subagent       ← phantom (after idle)
    #   1000018263  session_end

    def test_mid_turn_churn_is_busy(self):
        """Steady pretool/posttool churn between permit cycles:
        plain BUSY. JSONL inputs reconstructed as 'fresh tool_use'
        (Claude was mid-turn calling tools every few seconds).

        Probe placement note: the first draft of this probe FAILED — it accidentally landed
        inside yet another permit window of the same shape as the headline incident. The
        trace contains at least four such windows; that density is
        itself evidence of how often the approved-permission gap
        occurs in real sessions."""
        state = replay_at(
            self.EVENTS, now=1000016420,  # latest=posttool
            jsonl_stop_reason="tool_use", jsonl_age=4, raw="IDLE",
        )
        assert state == "BUSY"

    def test_earlier_permit_cycle_resolves_via_posttool(self):
        """An earlier permit cycle: once its posttool
        lands, the event tail is start-class again and the state
        returns to BUSY. This is the only mechanism that ends a
        permit window today — the probe documents it."""
        state = replay_at(
            self.EVENTS, now=1000016560,  # 8s after that posttool
            jsonl_stop_reason="tool_use", jsonl_age=5, raw="IDLE",
        )
        assert state == "BUSY"

    def test_approved_permission_long_tool_is_busy_with_spinner_raw(self):
        """THE BUG WINDOW — 3.5 min after the permission was
        approved, with the tool still running.

        The fix landed at the RAW layer, not here: `detect_pane_state`
        now returns raw=BUSY when an active-work spinner footer
        (`… (elapsed · arrow Nk tokens)`) is visible alongside the
        `❯` composer (accept-edits keeps the composer on screen during
        execution). With raw=BUSY, classify_activity's permit branch
        promotes to IN_PROGRESS. So in production the dashboard now
        shows BUSY for this window.

        This probe pins the derive layer with raw="BUSY" — the value
        the spinner-aware detector now produces for a long tool
        running under an approved permission.

        Inputs verbatim from the trace except raw, which reflects
        the post-fix detector output:
          - jsonl_stop_reason="tool_use", jsonl_age=199 (unchanged)
          - raw="BUSY": spinner present → detect_pane_state(BUSY).
        """
        state = replay_at(
            self.EVENTS, now=1000017260,
            jsonl_stop_reason="tool_use", jsonl_age=199, raw="BUSY",
        )
        assert state == "BUSY"

    def test_classify_activity_alone_still_permit_on_raw_idle(self):
        """Layer boundary: the fix is in raw computation, NOT in
        classify_activity. If raw were still IDLE here (pre-fix
        detector, or a future UI change that hides the spinner),
        classify_activity STILL returns PERMIT — the event-log layer
        genuinely cannot distinguish an approved-running tool from a
        menu wait on its own (both: permit-latest, jsonl tool_use
        older, raw IDLE). This probe documents WHY the fix had to live
        at the raw layer: it is the only layer with the spinner
        signal. If a refactor moves the discriminator into
        classify_activity, this expectation changes and the move is
        forced to be deliberate."""
        state = replay_at(
            self.EVENTS, now=1000017260,
            jsonl_stop_reason="tool_use", jsonl_age=199, raw="IDLE",
        )
        assert state == "PERMIT"

    def test_posttool_after_long_run_releases_to_busy(self):
        """the rspec posttool finally lands and the
        permit window ends the only way it can today."""
        state = replay_at(
            self.EVENTS, now=1000017390,
            jsonl_stop_reason="tool_use", jsonl_age=9, raw="IDLE",
        )
        assert state == "BUSY"

    def test_stop_with_terminal_stop_reason_is_idle(self):
        """stop + JSONL end_turn → turn truly over."""
        state = replay_at(
            self.EVENTS, now=1000017500,
            jsonl_stop_reason="end_turn", jsonl_age=27, raw="IDLE",
        )
        assert state == "IDLE"

    def test_notify_idle_is_idle(self):
        state = replay_at(
            self.EVENTS, now=1000017560,
            jsonl_stop_reason="end_turn", jsonl_age=87, raw="IDLE",
        )
        assert state == "IDLE"

    def test_phantom_subagent_after_idle_stays_idle(self):
        """a subagent event fired 2 minutes into idle
        (the documented phantom-subagent upstream quirk; see memory
        project_phantom_subagent.md). `_strip_phantom_subagents`
        must drop it because the preceding non-subagent event is a
        rest marker, leaving the state IDLE. This probe exercises
        the phantom logic against a REAL phantom, not a synthetic
        one."""
        state = replay_at(
            self.EVENTS, now=1000017760,
            jsonl_stop_reason="end_turn", jsonl_age=287, raw="IDLE",
        )
        assert state == "IDLE"

    def test_session_end_with_pid_gone_is_shell(self):
        state = replay_at(
            self.EVENTS, now=1000018360,
            jsonl_stop_reason="end_turn", jsonl_age=900, raw="SHELL",
            pid_present=False,
        )
        assert state == "SHELL"

    def test_session_end_transient_with_live_pid_defers_to_legacy(self):
        """session_end is the newest event but a claude pid still
        exists (the brief gap between the SessionEnd hook and
        process exit, or a fast restart). derive must return None
        (legacy fallback decides from the process tree) rather than
        committing SHELL while a live claude runs."""
        state = replay_at(
            self.EVENTS, now=1000018310,
            jsonl_stop_reason="end_turn", jsonl_age=850, raw="IDLE",
            pid_present=True,
        )
        assert state is None


class TestMenuWait20260611:
    """incident-class : an
    `AskUserQuestion` (interactive menu) was on screen waiting for
    the user to pick an option.

    This is the OTHER side of the trade-off recorded in memory
    project_false_idle_long_tool.md. An interactive menu is a
    tool call (`AskUserQuestion`), so during the wait:
      - the newest event is permit_req / notify_permit (the menu is
        a permit-class hook), exactly like the approved-long-tool
        incident;
      - the JSONL latest assistant record has stop_reason=tool_use,
        ALSO exactly like the long-tool incident (measured live
 the menu tool_use record predates the wait);
      - raw is IDLE.

    So this trace is INDISTINGUISHABLE from TestPermitLongTool's bug
    window on all three signals (event-log / JSONL stop_reason /
    raw). The correct answer here is PERMIT (the user genuinely must
    act), while the correct answer there is BUSY (the tool is
    running). That is why a naive "promote tool_use to BUSY" fix is
    wrong: it would turn THIS into a false BUSY. The two probes
    below pin PERMIT so any future long-tool fix that regresses the
    menu case fails immediately.

    The original hypothesis (memory, since refuted) was that menus
    would NOT show stop_reason=tool_use. The live measurement that
    killed that hypothesis is what this fixture preserves.
    """

    EVENTS = load_trace("menu_wait.events.jsonl")

    # Key timestamps (from the trace):
    #   1000000585  pretool  ← AskUserQuestion call
    #   1000000591  notify_permit
    #   1000000668  posttool  ← user answered

    def test_menu_wait_is_permit(self):
        """During the AskUserQuestion wait, with the exact signals
        the long-tool bug window also produces (newest event
        permit-class, JSONL stop_reason=tool_use, JSONL older than
        the permit event, raw IDLE), the answer must be PERMIT —
        the user really is being asked to choose."""
        state = replay_at(
            self.EVENTS, now=1000000633,  # , mid-wait
            jsonl_stop_reason="tool_use", jsonl_age=48, raw="IDLE",
        )
        assert state == "PERMIT"

    def test_menu_wait_permit_holds_against_busy_promotion(self):
        """Explicit guard: this is the regression a long-tool fix
        must not cause. If someone adds a 'latest assistant is
        tool_use → BUSY' promotion to classify_activity's permit
        branch, THIS probe flips to BUSY and fails — which is the
        whole point of keeping both traces in the corpus."""
        state = replay_at(
            self.EVENTS, now=1000000663,  # , 5s before answer
            jsonl_stop_reason="tool_use", jsonl_age=78, raw="IDLE",
        )
        assert state == "PERMIT"

    def test_user_answer_releases_to_busy(self):
        """the user picks an option → posttool lands →
        event tail is start-class again → BUSY as the follow-up
        tools run."""
        state = replay_at(
            self.EVENTS, now=1000000673,
            jsonl_stop_reason="tool_use", jsonl_age=5, raw="IDLE",
        )
        assert state == "BUSY"


class TestRecapPhantomSubagent20260707:
    """incident (observed live
    during the hook-silence canary observe-first run).

    A real turn ran with tool use and permission prompts until
    ~then the turn ended WITHOUT a Stop event (hook silence)
    and the session went idle — the `❯` composer visible, no spinner
    (raw=IDLE). The event tail's newest entry stayed a permit-class
    event, so ccm showed a false PERMIT for ~15 min. Then at ,
    when the user returned to the session, a recap `system/away_summary`
    JSONL record landed AND a phantom SubagentStart hook fired at the
    same instant. That lone subagent became the newest event → derive
    resolved BUSY → the dashboard stuck at BUSY while the pane sat
    plainly idle.

    Unlike the phantom, this subagent is preceded
    by a permit sequence (not a rest marker), so neither the
    rest-marker phantom strip nor the all-subagent guard applies. The
    fix is the stale-BUSY-vs-idle-screen guard in map_activity_to_state:
    a BUSY candidate with raw=IDLE and a JSONL frozen past the
    long-tool window is stale, so derive defers (None) and legacy
    resolves raw=IDLE → IDLE.
    """

    EVENTS = load_trace("recap_phantom_subagent.events.jsonl")

    def test_recap_phantom_subagent_stuck_busy_now_idle(self):
        """3 min after the recap phantom subagent. JSONL
        real-activity frozen at (jsonl_age=1115, past the
        600 s window), raw=IDLE (❯ visible, no spinner). derive must
        DEFER (None) so legacy commits IDLE — not the stuck BUSY the
        phantom subagent would otherwise force."""
        state = replay_at(
            self.EVENTS, now=1000001228,
            jsonl_stop_reason="tool_use", jsonl_age=1115, raw="IDLE",
        )
        assert state is None  # → legacy reads raw=IDLE → IDLE

    def test_real_subagent_with_busy_spinner_stays_busy(self):
        """Guard boundary: the SAME phantom-subagent tail, but a
        genuinely running subagent shows a spinner → raw=BUSY. The
        guard requires raw=IDLE, so this stays BUSY. Ensures the fix
        does not blind ccm to a real in-progress Task."""
        state = replay_at(
            self.EVENTS, now=1000001228,
            jsonl_stop_reason="tool_use", jsonl_age=1115, raw="BUSY",
        )
        assert state == "BUSY"

    def test_fresh_jsonl_subagent_stays_busy(self):
        """Guard boundary: a subagent tail with FRESH JSONL (a real
        Task just dispatched, tool_use 8 s old) stays BUSY even at
        raw=IDLE — jsonl_age is well inside the window so the guard
        never applies."""
        state = replay_at(
            self.EVENTS, now=1000000702,
            jsonl_stop_reason="tool_use", jsonl_age=8, raw="IDLE",
        )
        assert state == "BUSY"


class TestFixturesAreSynthetic:
    """Replay fixtures carry an event sequence and nothing else.

    Absolute timestamps would tie the corpus to one machine's clock and
    extra fields would let a replay depend on something the detection
    path is not being asked about. Fixtures are normalised to a fixed
    epoch with their intervals preserved.
    """

    SYNTHETIC_EPOCH = 1000000000

    def _paths(self):
        return sorted(
            os.path.join(TRACES_DIR, f) for f in os.listdir(TRACES_DIR)
            if f.endswith(".jsonl")
        )

    def test_fixtures_exist(self):
        assert self._paths(), "no replay fixtures found"

    def test_fixtures_start_at_the_synthetic_epoch(self):
        for path in self._paths():
            with open(path, encoding="utf-8") as f:
                stamps = [json.loads(l)["ts"] for l in f if l.strip()]
            assert stamps, f"{path}: no records"
            assert min(stamps) == self.SYNTHETIC_EPOCH, (
                f"{path}: starts at {min(stamps)}, not {self.SYNTHETIC_EPOCH} "
                f"— re-normalise so the fixture carries intervals, not a clock"
            )

    def test_fixtures_carry_only_timestamp_and_type(self):
        for path in self._paths():
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    assert set(record) == {"ts", "type"}, (
                        f"{path}:{i}: unexpected fields {sorted(record)} — a "
                        f"replay fixture carries the event sequence only"
                    )
