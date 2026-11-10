"""A Codex sidekick's state (PERMIT / BUSY / IDLE): read from its
screen, with its hook record as the fallback, and shown on the
`▸ codex` badge. Screens are shaped after captures of Codex 0.162.1."""
from types import SimpleNamespace

import pytest

import ccm_core
import ccm_pane_state
import ccm_render
from ccm_constants import codex_screen_holds_keys, codex_screen_state


def lines(text):
    return [line for line in text.split("\n") if line.strip()]


IDLE = lines("""\
• HTTP/2 200

  Worked for 3m 6s • 08:27

› Ask Codex to do anything

  GPT-6-Astra medium · ~/example · Continue
  ← for agents · ? for shortcuts""")

WORKING = lines("""\
• Running sleep 8; curl -sI https://example.com | head -1

Working (10s • esc to interrupt)
  └ Tip: Use /statusline to configure which items appear.

› Ask Codex to do anything

  GPT-6-Astra medium · ~/example · ⠋
  ← for agents · ? for shortcuts""")

# Another wording of the working line, measured on a live pane.
WAITING_FOR_TERMINAL = lines("""\
    + 25 lines (⌃t to expand)
Waiting for background terminal (19m 03s • esc to interrupt) · 1 background terminal runnin…
  └ cp target/example target/example-copy
  └ Tip: Use /statusline to configure which items appear in the status line.
› Ask Codex to do anything
  GPT-6-Astra medium fast · ~/example · Continue
  ← for agents · ? for shortcuts""")

APPROVAL = lines("""\
• Running sleep 8; curl -sI https://example.com | head -1

  Would you like to run the following command?

  Environment: local

  Reason: May I run this command outside the sandbox?

  $ sleep 8; curl -sI https://example.com | head -1

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `sleep 8` (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel""")

HOOK_REVIEW = lines("""\
  Hooks need review
  8 hooks are new or changed.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
  enter confirm · esc skip""")


# Text selected in the transcript while a turn runs: the view's key
# hint replaces the footer, the working line and the composer stay.
SELECTION_WORKING = lines("""\
• Running the example task.
Working (2m 10s • esc to interrupt) · 1 background terminal running …
  └ Tip: Use /statusline to configure which items appear.
› Ask Codex to do anything
  GPT-6-Astra medium Fast · ~/example · Continue
  New activity · ⌃c copy · enter copy & follow · esc clear""")

# The same view with no turn running.
SELECTION_IDLE = lines("""\
  Worked for 1m 0s
› Ask Codex to do anything
  GPT-6-Astra medium · ~/example · Continue
  ⌃c copy · enter copy · esc clear""")


class TestCodexScreenState:
    def test_selection_during_turn_is_busy(self):
        assert codex_screen_state(SELECTION_WORKING) == "BUSY"

    def test_selection_without_turn_stays_permit(self):
        """Nothing tells this view from a dialog once the turn is over,
        so it keeps the side that does not type into it."""
        assert codex_screen_state(SELECTION_IDLE) == "PERMIT"

    def test_working_line_above_dialog_option_is_not_busy(self):
        """A dialog's selected option starts with `›` like the
        composer; a working line above it does not make it a turn."""
        screen = (["Working (3s • esc to interrupt)"] + APPROVAL[-4:])
        assert codex_screen_state(screen) == "PERMIT"

    def test_numbered_draft_during_turn_is_busy(self):
        screen = lines("""\
Working (5s • esc to interrupt)
› 1. Review the changes
  ? for shortcuts""")
        assert codex_screen_state(screen) == "BUSY"

    def test_numbered_draft_under_selection_reads_as_dialog(self):
        """With a key hint below, a numbered draft cannot be told from
        a dialog's option, so it keeps the side that does not type."""
        screen = lines("""\
Working (5s • esc to interrupt)
› 1. Review the changes
  Example Model · ~/example
  ⌃c copy · enter copy & follow · esc clear""")
        assert codex_screen_state(screen) == "PERMIT"
        assert codex_screen_holds_keys(screen)

    @pytest.mark.parametrize("screen, held", [
        (SELECTION_WORKING, True), (SELECTION_IDLE, True), (APPROVAL, True),
        (HOOK_REVIEW, True), (WORKING, False), (IDLE, False), ([], False)])
    def test_holds_keys(self, screen, held):
        assert codex_screen_holds_keys(screen) is held

    def test_idle(self):
        assert codex_screen_state(IDLE) == "IDLE"

    def test_working(self):
        assert codex_screen_state(WORKING) == "BUSY"

    def test_working_line_in_other_words(self):
        assert codex_screen_state(WAITING_FOR_TERMINAL) == "BUSY"

    def test_approval_dialog(self):
        assert codex_screen_state(APPROVAL) == "PERMIT"

    def test_other_dialog_waiting_for_a_key(self):
        assert codex_screen_state(HOOK_REVIEW) == "PERMIT"

    def test_empty_or_unreadable(self):
        assert codex_screen_state([]) is None

    def test_no_composer_is_unknown(self):
        # An overlay or a scrolled view: none of Codex's marks.
        assert codex_screen_state(lines("some transcript\nmore text")) is None

    def test_quoted_dialog_hint_above_the_bottom_is_not_a_dialog(self):
        quoted = lines("""\
• The dialog ends with "Press enter to confirm or esc to cancel".
› Ask Codex to do anything
  ← for agents · ? for shortcuts""")
        assert codex_screen_state(quoted) == "IDLE"

    @pytest.mark.parametrize("quote", [
        "• Working (4s • esc to interrupt) is the line ccm reads.",
        "  Working (4s • esc to interrupt)",
        "    `Working (4s • esc to interrupt)`",
    ])
    def test_quoted_working_line_is_not_work(self, quote):
        screen = lines(f"""\
{quote}
› Ask Codex to do anything
  ← for agents · ? for shortcuts""")
        assert codex_screen_state(screen) == "IDLE"

    def test_working_line_far_above_the_composer_is_old(self):
        screen = (WORKING[:3]
                  + [f"  line {i}" for i in range(7)]
                  + lines("› Ask Codex to do anything"))
        assert codex_screen_state(screen) == "IDLE"

    def test_working_line_just_inside_the_lookback(self):
        screen = (["Working (3s • esc to interrupt)"]
                  + [f"  └ line {i}" for i in range(5)]
                  + lines("› Ask Codex to do anything"))
        assert codex_screen_state(screen) == "BUSY"


WIN = "main:1"


@pytest.fixture
def window(monkeypatch):
    """One window holding a Claude pane (%1) and a Codex pane (%2)."""
    world = {"screen": IDLE, "hooks": {}, "marker": None, "toggle": "",
             "server_reads": 0, "hook_reads": 0,
             "identity": [["200", "birth-A"]]}
    panes = [(WIN, "100", "%1", "claude", "1", "40", "", ""),
             (WIN, "200", "%2", "codex", "0", "40", "", "")]
    row = {"win_target": WIN, "project": "demo", "proj_dir": "/example/demo",
           "completed_at_str": "", "win_activity_str": "", "bg_active_str": "",
           "cached_session_id": "", "attention_toggle": ""}
    monkeypatch.setattr(ccm_core, "tmux_cmd", lambda *a, **k: (
        world.__setitem__("server_reads", world["server_reads"] + 1)
        or "server-example" if a[0] == "display-message" else "rows"))
    monkeypatch.setattr(ccm_core, "_parse_window_line",
                        lambda line: dict(row, attention_toggle=world["toggle"]))
    monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
    monkeypatch.setattr(ccm_core, "_build_panes_cache", lambda: panes)
    monkeypatch.setattr(ccm_core, "_resolve_window_state", lambda *a: "IDLE")
    monkeypatch.setattr(ccm_core, "read_cache_file", lambda *a: "")
    monkeypatch.setattr(ccm_core, "_resolve_ignored_panes", lambda *a: 0)
    monkeypatch.setattr(ccm_core.ccm_detection, "find_claude_pid",
                        lambda pid, lines: "101" if pid == "100" else None)
    monkeypatch.setattr(ccm_pane_state, "prefetch_visible", lambda ids: None)
    monkeypatch.setattr(ccm_pane_state, "capture_pane_screen",
                        lambda pane: world["screen"] if pane == "%2" else [])
    monkeypatch.setattr(ccm_core, "_read_attention_markers", lambda *a: (
        {"%2": world["marker"]} if world["marker"] else {}))
    import ccm_sidekick_notify

    def activity(server, now=None):
        world["hook_reads"] += 1
        return world["hooks"]
    monkeypatch.setattr(ccm_sidekick_notify, "pane_activity", activity)
    monkeypatch.setattr(ccm_sidekick_notify, "process_identity",
                        lambda pane_pid, ps_lines: world["identity"]
                        if pane_pid == "200" else None)
    import ccm_roles
    monkeypatch.setattr(ccm_roles, "reconcile", lambda *a: None)

    def build():
        [project] = ccm_core.build_project_list(fast=False)
        return project
    world["build"] = build
    return world


class TestBuildProjectListCodex:
    def test_working_screen_is_busy(self, window):
        window["screen"] = WORKING
        p = window["build"]()
        assert p.busy_agents == ("codex",)
        assert p.attention_agents == ()

    def test_dialog_screen_is_attention_without_a_marker(self, window):
        window["screen"] = APPROVAL
        p = window["build"]()
        assert p.attention_agents == ("codex",)

    def test_answered_dialog_clears_a_waiting_marker(self, window):
        # The hook marker still says waiting (the approved command is
        # running); the screen shows the work, not the dialog.
        window["marker"] = {"agent": "codex", "state": "waiting"}
        window["screen"] = WORKING
        p = window["build"]()
        assert p.attention_agents == ()
        assert p.busy_agents == ("codex",)

    def test_legible_screen_does_not_read_the_hooks(self, window):
        window["screen"] = IDLE
        window["build"]()
        assert window["hook_reads"] == 0

    @pytest.mark.parametrize("hook, attention, busy", [
        ("PERMIT", ("codex",), ()),
        ("BUSY", (), ("codex",)),
        ("IDLE", (), ()),
    ])
    def test_illegible_screen_falls_back_to_the_hooks(self, window, hook,
                                                       attention, busy):
        window["screen"] = []
        window["hooks"] = {"%2": (hook, [["200", "birth-A"]])}
        p = window["build"]()
        assert p.attention_agents == attention
        assert p.busy_agents == busy

    @pytest.mark.parametrize("now_running", [
        [["200", "birth-B"]],   # a Codex restarted in the same pane
        None,                   # the running process cannot be identified
    ])
    def test_hook_record_of_another_process_is_not_used(self, window, now_running):
        window["screen"] = []
        window["identity"] = now_running
        window["hooks"] = {"%2": ("PERMIT", [["200", "birth-A"]])}
        p = window["build"]()
        assert p.attention_agents == ()
        assert p.busy_agents == ()

    def test_hook_record_without_a_process_is_not_used(self, window):
        window["screen"] = []
        window["hooks"] = {"%2": ("BUSY", None)}
        p = window["build"]()
        assert p.busy_agents == ()

    def test_illegible_screen_without_a_hook_record_is_idle(self, window):
        window["screen"] = []
        p = window["build"]()
        assert p.attention_agents == ()
        assert p.busy_agents == ()

    def test_watch_off_shows_neither(self, window):
        window["toggle"] = "off"
        window["screen"] = APPROVAL
        p = window["build"]()
        assert p.attention_agents == ()
        assert p.busy_agents == ()


class TestObservation:
    """One observation per sidekick pane: state, its source and whether
    keys reach the composer, with unknown kept as None rather than idle."""

    def observe(self, window):
        cache = ccm_core._build_panes_cache()
        return {o.pane: o for o in ccm_core.observe_sidekicks(
            cache, WIN, ccm_core._read_attention_markers(cache, []), [], [])}

    @pytest.mark.parametrize("screen, state, keys", [
        ("IDLE", "IDLE", "free"),
        ("WORKING", "BUSY", "free"),
        ("APPROVAL", "PERMIT", "held"),
        ("SELECTION_WORKING", "BUSY", "held"),
    ])
    def test_screen(self, window, screen, state, keys):
        window["screen"] = globals()[screen]
        o = self.observe(window)["%2"]
        assert (o.agent, o.state, o.state_source, o.keys) == (
            "codex", state, "screen", keys)

    def test_the_claude_pane_is_not_a_sidekick(self, window):
        assert set(self.observe(window)) == {"%2"}

    @pytest.mark.parametrize("hook, keys", [
        ("PERMIT", "held"), ("BUSY", None), ("IDLE", None)])
    def test_illegible_screen_takes_a_valid_hook_record(self, window, hook, keys):
        window["screen"] = []
        window["hooks"] = {"%2": (hook, [["200", "birth-A"]])}
        o = self.observe(window)["%2"]
        assert (o.state, o.state_source, o.keys) == (hook, "hook", keys)

    @pytest.mark.parametrize("hooks, identity", [
        ({}, [["200", "birth-A"]]),                              # no record
        ({"%2": (None, [["200", "birth-A"]])}, [["200", "birth-A"]]),  # past its age
        ({"%2": ("IDLE", [["200", "birth-A"]])}, [["200", "birth-B"]]),  # another process
        ({"%2": ("IDLE", [["200", "birth-A"]])}, None),          # cannot tell
    ])
    def test_unknown_is_none_not_idle(self, window, hooks, identity):
        window["screen"] = []
        window["hooks"] = hooks
        window["identity"] = identity
        o = self.observe(window)["%2"]
        assert (o.state, o.state_source, o.keys) == (None, None, None)

    def test_toggle_off_hides_the_badge_state_not_the_observation(self, window):
        window["toggle"] = "off"
        window["screen"] = APPROVAL
        assert window["build"]().attention_agents == ()
        assert self.observe(window)["%2"].state == "PERMIT"

    def test_badge_states_keep_only_permit_and_busy(self):
        O = ccm_core.SidekickObservation
        observed = [O("codex", "%2", "BUSY", "screen", "free"),
                    O("kimi", "%3", "PERMIT", "marker", "held"),
                    O("agy", "%4", "IDLE", "screen", "free"),
                    O("grok", "%5", None, None, None)]
        assert ccm_core.sidekick_pane_states(observed) == {
            "%2": ("codex", "BUSY"), "%3": ("kimi", "PERMIT")}


class TestObservationWithoutAScreenReader:
    """Kimi, Grok and an ignored Claude are known only from a waiting
    marker; no marker says nothing about them."""

    PANES = [(WIN, "100", "%1", "claude", "1", "40", "", ""),
             (WIN, "300", "%3", "kimi", "0", "40", "", ""),
             (WIN, "400", "%4", "2.1.321", "0", "40", "1", ""),
             (WIN, "500", "%5", "zsh", "0", "40", "", "")]

    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch):
        monkeypatch.setattr(ccm_core.ccm_detection, "find_claude_pid",
                            lambda pid, lines: {"100": "101", "400": "401"}.get(pid))
        monkeypatch.setattr(ccm_pane_state, "capture_pane_screen",
                            lambda pane: pytest.fail("no screen to read"))

    def observe(self, markers):
        return {o.pane: o for o in ccm_core.observe_sidekicks(
            self.PANES, WIN, markers, [], ["ps"])}

    def test_no_marker_is_unknown(self):
        observed = self.observe({})
        assert set(observed) == {"%3", "%4"}
        assert observed["%3"] == ("kimi", "%3", None, None, None)
        assert observed["%4"] == ("claude", "%4", None, None, None)

    def test_waiting_marker_is_permit_with_held_keys(self):
        observed = self.observe({"%3": {"agent": "kimi", "state": "waiting"},
                                 "%4": {"agent": "claude", "state": "waiting"}})
        assert observed["%3"] == ("kimi", "%3", "PERMIT", "marker", "held")
        assert observed["%4"] == ("claude", "%4", "PERMIT", "marker", "held")


def project(agents=("codex",), attention=(), busy=()):
    return SimpleNamespace(external_agents=agents, attention_agents=attention,
                           busy_agents=busy)


class TestBadge:
    def test_idle_badge_is_the_name(self):
        assert ccm_render.external_agent_badge(project()) == "▸ codex"
        assert ccm_render.external_agent_segments(project()) == [("codex", "")]

    def test_busy_badge(self):
        p = project(busy=("codex",))
        assert ccm_render.external_agent_badge(p) == "▸ codex ◉"
        assert ccm_render.external_agent_segments(p) == [("codex ◉", "BUSY")]

    def test_waiting_outranks_working(self):
        p = project(attention=("codex",), busy=("codex",))
        assert ccm_render.external_agent_badge(p) == "▸ codex ⚠"
        assert ccm_render.external_agent_segments(p) == [("codex ⚠", "PERMIT")]

    def test_ignored_claude_waiting_does_not_mark_the_badge(self):
        p = project(attention=("claude",))
        assert ccm_render.external_agent_badge(p) == "▸ codex"

    def test_each_sidekick_shows_its_own_state(self):
        p = project(agents=("codex", "agy"), attention=("agy",), busy=("codex",))
        assert ccm_render.external_agent_badge(p) == "▸ codex ◉,agy ⚠"
        assert ccm_render.external_agent_segments(p) == [
            ("codex ◉", "BUSY"), (",", ""), ("agy ⚠", "PERMIT")]

    def test_a_quiet_sidekick_beside_a_busy_one_stays_plain(self):
        p = project(agents=("codex", "agy", "agy"), busy=("agy",))
        assert ccm_render.external_agent_badge(p) == "▸ codex,agy×2 ◉"

    def test_no_sidekick_no_badge(self):
        assert ccm_render.external_agent_badge(project(agents=())) == ""


class TestScreenCaptureKeepsIndent:
    """The Codex reader tells a quote from the working line by its
    column, so the capture it reads must keep the first line's indent —
    tmux_cmd strips its output, which used to drop exactly that."""

    QUOTED_AT_TOP = ("  Working (4s • esc to interrupt)\n"
                     "› Ask Codex to do anything\n"
                     "  ← for agents · ? for shortcuts\n")

    @pytest.fixture(autouse=True)
    def _drop_prefetched(self):
        """A batch capture outlives its test for a second; later tests
        that capture the same pane id must not be served from it."""
        yield
        ccm_pane_state.clear_prefetched()

    def _tmux_stdout(self, monkeypatch, stdout_for):
        import subprocess

        def run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=stdout_for(cmd).encode(), stderr=b"")
        monkeypatch.setattr(ccm_core.subprocess, "run", run)
        ccm_pane_state.clear_prefetched()

    def test_single_capture(self, monkeypatch):
        self._tmux_stdout(monkeypatch, lambda cmd: self.QUOTED_AT_TOP)
        screen = ccm_pane_state.capture_pane_screen("%2")
        assert screen[0] == "  Working (4s • esc to interrupt)"
        assert codex_screen_state(screen) == "IDLE"

    def test_batch_capture(self, monkeypatch):
        def stdout(cmd):
            sep = cmd[cmd.index("display-message") + 2]
            return self.QUOTED_AT_TOP + sep + "\nother pane\n"
        self._tmux_stdout(monkeypatch, stdout)
        ccm_pane_state.prefetch_visible(["%2", "%3"])
        screen = ccm_pane_state.capture_pane_screen("%2")
        assert screen[0] == "  Working (4s • esc to interrupt)"
        assert codex_screen_state(screen) == "IDLE"

    def test_batch_capture_keeps_other_readers_unchanged(self, monkeypatch):
        def stdout(cmd):
            sep = cmd[cmd.index("display-message") + 2]
            return "  first\nsecond\n" + sep + "\nother pane\n"
        self._tmux_stdout(monkeypatch, stdout)
        ccm_pane_state.prefetch_visible(["%2", "%3"])
        assert ccm_pane_state.capture_pane_visible("%2") == ["first", "second"]
