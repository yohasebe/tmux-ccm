"""An Antigravity CLI (`agy`) sidekick's state (PERMIT / BUSY / IDLE),
read from its screen and shown on the `▸ agy` badge. Screens are shaped
after captures of Antigravity CLI 1.4.12."""
import pytest

import ccm_core
import ccm_pane_state
from ccm_constants import agy_screen_state, sidekick_screen_reading


def lines(text):
    return [line for line in text.split("\n") if line.strip()]


RULE = "─" * 100
BANNER_RULE = "─" * 60

IDLE = lines(f"""\
{BANNER_RULE}
> Summarize the example notes
● Explored 1 file (notes.md)
  The notes describe three steps.
{RULE}
>
{RULE}
? for shortcuts                                              Example Model · medium""")

WORKING = lines(f"""\
{BANNER_RULE}
> Summarize the example notes
⣟  Generating...
{RULE}
>
{RULE}
esc to cancel                                                Example Model · medium""")

APPROVAL = lines(f"""\
{BANNER_RULE}
> Run the example check
● Ran (make check) (ctrl+o to expand)
Command
{RULE}
Requesting permission for:
   make check
Run this command?
> 1. Yes, run command
  2. Yes, and always allow in this conversation for commands that start with 'make'
  3. No, cancel
  ↑/↓ Navigate · tab Amend · ctrl+g edit/expand command
esc to cancel                                                Example Model · medium""")

TRUST = lines("""\
Accessing workspace:
/example/project
Do you trust the contents of this project?
> Yes, I trust this folder
  No, exit
  ↑/↓ Navigate · enter Confirm
                                                             Example Model · medium""")

SUBAGENT_APPROVAL = lines(f"""\
{BANNER_RULE}
> Check the example notes
● Spawned 2 subagents
  Waiting for their reports.
 ┃
 ┃ python3 -c 'print(1)'
 ┃
 ┃ ctrl+k approve · alt+j manage
{RULE}
>
{RULE}
  ● Agent(self: Reviewer)  Blocked · Check the notes · 3m10s
  ● Agent(self: Reader)  Reading file · 3m12s
{RULE}
? for shortcuts                              auto · Example Model · medium · 2 subagent(s)""")


class TestScreenReading:
    """State and keys from one screen: keys are free only where the
    reader found its composer, held under any approval."""

    @pytest.mark.parametrize("screen, expected", [
        ("IDLE", ("IDLE", "free")),
        ("WORKING", ("BUSY", "free")),
        ("APPROVAL", ("PERMIT", "held")),
        ("SUBAGENT_APPROVAL", ("PERMIT", "held")),
    ])
    def test_agy(self, screen, expected):
        assert sidekick_screen_reading("agy", globals()[screen]) == expected

    @pytest.mark.parametrize("name, screen", [
        ("agy", lines("some transcript\nmore text\nand more\nlast")),
        ("agy", []),
        ("kimi", IDLE),
    ])
    def test_unknown(self, name, screen):
        assert sidekick_screen_reading(name, screen) == (None, None)


class TestAgyScreenState:
    def test_idle(self):
        assert agy_screen_state(IDLE) == "IDLE"

    def test_working(self):
        assert agy_screen_state(WORKING) == "BUSY"

    def test_approval_menu(self):
        assert agy_screen_state(APPROVAL) == "PERMIT"

    def test_other_menu_waiting_for_a_key(self):
        assert agy_screen_state(TRUST) == "PERMIT"

    def test_subagent_approval(self):
        assert agy_screen_state(SUBAGENT_APPROVAL) == "PERMIT"

    def test_empty_or_unreadable(self):
        assert agy_screen_state([]) is None
        assert agy_screen_state(["one line"]) is None

    def test_no_footer_is_unknown(self):
        assert agy_screen_state(lines("some transcript\nmore text")) is None
        assert agy_screen_state(IDLE[:-1]) is None

    def test_quoted_menu_hint_above_the_bottom_is_not_a_menu(self):
        quoted = IDLE[:2] + ["  ↑/↓ Navigate · enter Confirm"] + IDLE[2:]
        assert agy_screen_state(quoted) == "IDLE"

    def test_quoted_approval_box_away_from_the_composer_is_not_a_wait(self):
        quoted = IDLE[:2] + [" ┃ ctrl+k approve · alt+j manage"] + IDLE[2:]
        assert agy_screen_state(quoted) == "IDLE"

    def test_subagent_approval_while_working(self):
        working = SUBAGENT_APPROVAL[:-1] + ["esc to cancel        Example Model"]
        assert agy_screen_state(working) == "PERMIT"

    def test_quoted_approval_hint_right_above_the_composer_is_not_a_wait(self):
        quoted = IDLE[:4] + ["The documentation quotes this hint:",
                             " ┃ ctrl+k approve · alt+j manage"] + IDLE[4:]
        assert agy_screen_state(quoted) == "IDLE"

    def test_quoted_approval_box_right_above_the_composer_is_not_a_wait(self):
        quoted = IDLE[:4] + ["  The approval display contains these lines:",
                             " ┃ python3 -c example",
                             " ┃ ctrl+k approve · alt+j manage"] + IDLE[4:]
        assert agy_screen_state(quoted) == "IDLE"

    def test_approval_box_needs_a_blocked_agent_below(self):
        running = [line.replace("Blocked", "Reading file") for line in SUBAGENT_APPROVAL]
        assert agy_screen_state(running) == "IDLE"

    @pytest.mark.parametrize("row", [
        "  ● Agent(self: Reviewer)  Reading file · Check blocked requests · 3m10s",
        "  ● Agent(self: Blocked Reviewer)  Reading file · Check the notes · 3m10s",
        "  ● Agent(self: Review (dependencies)  Blocked tasks)  Reading file · Check requests · 3m10s",
        "  ● Agent(self: Reviewer)  Blocked tasks queue · Check requests · 3m10s",
    ])
    def test_blocked_only_in_a_name_or_task_is_not_a_wait(self, row):
        screen = [line for line in SUBAGENT_APPROVAL if "Agent(" not in line]
        at = screen.index(RULE, screen.index(">")) + 1
        assert agy_screen_state(screen[:at] + [row] + screen[at:]) == "IDLE"

    def test_quoted_menu_hint_at_the_bottom_is_not_a_menu(self):
        quoted = ["Here is the shortcut syntax:", "  ↑/↓ Navigate · enter Confirm",
                  "This line explains the example."]
        assert agy_screen_state(quoted) is None
        assert agy_screen_state(IDLE[:-2] + quoted[:2] + IDLE[-1:]) is None

    def test_menu_needs_its_selected_item(self):
        without_item = [line for line in APPROVAL if not line.startswith(">")]
        assert agy_screen_state(without_item) is None

    def test_menu_item_does_not_count_across_a_rule(self):
        assert agy_screen_state(APPROVAL[:-2] + [RULE] + APPROVAL[-2:]) is None

    @pytest.mark.parametrize("gap, state", [(9, "PERMIT"), (10, None)])
    def test_menu_item_lookback(self, gap, state):
        screen = ["> 1. Yes, run command"] + ["  more option text"] * gap + APPROVAL[-2:]
        assert agy_screen_state(["Run this command?", "context"] + screen) == state

    def test_quoted_footer_without_the_frame_is_unknown(self):
        quoted = IDLE[:4] + ["The documented hint is:", "esc to cancel means stopping a turn"]
        assert agy_screen_state(quoted) is None

    def test_echoed_prompt_does_not_stand_for_the_composer(self):
        # The banner rule and the echoed prompt look like a composer.
        assert agy_screen_state(WORKING[:2] + ["  more output"]) is None
        boxed = [" ┃", " ┃ ctrl+k approve · alt+j manage"] + WORKING[:2] + [
            "  Response text with no composer in view", RULE,
            "esc to cancel                                                Example Model · medium"]
        assert agy_screen_state(boxed) is None


WIN = "main:1"


@pytest.fixture
def window(monkeypatch):
    """One window holding a Claude pane (%1) and an agy pane (%2)."""
    world = {"screen": IDLE, "toggle": ""}
    panes = [(WIN, "100", "%1", "claude", "1", "40", "", ""),
             (WIN, "200", "%2", "agy", "0", "40", "", "")]
    row = {"win_target": WIN, "project": "demo", "proj_dir": "/example/demo",
           "completed_at_str": "", "win_activity_str": "", "bg_active_str": "",
           "cached_session_id": "", "attention_toggle": ""}
    monkeypatch.setattr(ccm_core, "tmux_cmd", lambda *a, **k: "rows")
    monkeypatch.setattr(ccm_core, "_parse_window_line",
                        lambda line: dict(row, attention_toggle=world["toggle"]))
    monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
    monkeypatch.setattr(ccm_core, "_build_panes_cache", lambda: panes)
    monkeypatch.setattr(ccm_core, "_resolve_window_state", lambda *a: "IDLE")
    monkeypatch.setattr(ccm_core, "read_cache_file", lambda *a: "")
    monkeypatch.setattr(ccm_core, "_resolve_ignored_panes", lambda *a: 0)
    monkeypatch.setattr(ccm_core.ccm_detection, "find_claude_pid",
                        lambda pid, lines: "101" if pid == "100" else None)
    prefetched = []
    monkeypatch.setattr(ccm_pane_state, "prefetch_visible", prefetched.extend)
    monkeypatch.setattr(ccm_pane_state, "capture_pane_screen",
                        lambda pane: world["screen"] if pane == "%2" else [])
    monkeypatch.setattr(ccm_core, "_read_attention_markers", lambda *a: {})
    import ccm_roles
    monkeypatch.setattr(ccm_roles, "reconcile", lambda *a: None)

    def build():
        [project] = ccm_core.build_project_list(fast=False)
        return project
    world["build"] = build
    world["prefetched"] = prefetched
    return world


class TestBuildProjectListAgy:
    @pytest.mark.parametrize("screen, attention, busy", [
        (IDLE, (), ()),
        (WORKING, (), ("agy",)),
        (APPROVAL, ("agy",), ()),
        (SUBAGENT_APPROVAL, ("agy",), ()),
        ([], (), ()),
    ])
    def test_screen_decides(self, window, screen, attention, busy):
        window["screen"] = screen
        p = window["build"]()
        assert p.attention_agents == attention
        assert p.busy_agents == busy

    def test_screen_is_captured_with_the_pass(self, window):
        window["build"]()
        assert "%2" in window["prefetched"]

    def test_watch_off_shows_neither(self, window):
        window["toggle"] = "off"
        window["screen"] = APPROVAL
        p = window["build"]()
        assert p.attention_agents == ()
        assert p.busy_agents == ()
