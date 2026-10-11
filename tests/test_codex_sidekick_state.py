"""A Codex sidekick's state (PERMIT / BUSY / IDLE): read from its
screen, with its hook record as the fallback, and shown on the
`▸ codex` badge. Screens are shaped after captures of Codex 0.162.1."""
from types import SimpleNamespace

import pytest

import ccm_core
import ccm_pane_state
import ccm_render
from ccm_constants import codex_screen_state


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


class TestCodexScreenState:
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


def project(agents=("codex",), attention=(), busy=()):
    return SimpleNamespace(external_agents=agents, attention_agents=attention,
                           busy_agents=busy)


class TestBadge:
    def test_idle_badge_is_the_name(self):
        assert ccm_render.external_agent_badge(project()) == "▸ codex"
        assert ccm_render.external_agent_state(project()) == ""

    def test_busy_badge(self):
        p = project(busy=("codex",))
        assert ccm_render.external_agent_badge(p) == "▸ codex ◉"
        assert ccm_render.external_agent_state(p) == "BUSY"

    def test_waiting_outranks_working(self):
        p = project(attention=("codex",), busy=("codex",))
        assert ccm_render.external_agent_badge(p) == "▸ codex ⚠"
        assert ccm_render.external_agent_state(p) == "PERMIT"

    def test_ignored_claude_waiting_does_not_mark_the_badge(self):
        p = project(attention=("claude",))
        assert ccm_render.external_agent_badge(p) == "▸ codex"

    def test_no_sidekick_no_badge(self):
        assert ccm_render.external_agent_badge(project(agents=())) == ""


class TestScreenCaptureKeepsIndent:
    """The Codex reader tells a quote from the working line by its
    column, so the capture it reads must keep the first line's indent —
    tmux_cmd strips its output, which used to drop exactly that."""

    QUOTED_AT_TOP = ("  Working (4s • esc to interrupt)\n"
                     "› Ask Codex to do anything\n"
                     "  ← for agents · ? for shortcuts\n")

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
