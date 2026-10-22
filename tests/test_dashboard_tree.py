"""The dashboard's tree view, through the names its own module looks up."""

import curses
import threading
from types import SimpleNamespace

import pytest

import dashboard
import dashboard_tree


def _tmux(responses, calls):
    def run(*args, **kwargs):
        calls.append(args)
        for prefix, out in responses:
            if args[:len(prefix)] == prefix:
                return out
        return ""
    return run


@pytest.fixture
def board(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda n: n * 256)
    monkeypatch.setattr(dashboard_tree, "get_session", lambda: "main")
    d = object.__new__(dashboard.Dashboard)
    d.lock = threading.Lock()
    d.projects = [SimpleNamespace(win_target="main:1", state="IDLE",
                                  name="alpha", branch="dev", ports="3000")]
    d.mode = "tree"
    d._before_switch = lambda: None
    return d


TMUX = [
    (("list-sessions",), "main\nother"),
    (("display-message", "-p", "#{window_index}"), "1"),
    (("list-windows", "-t", "main"), "1\talpha\talpha\t/w/alpha\n2\tscratch\t\t"),
    (("list-windows", "-t", "other"), "0\tlogs\t\t"),
    (("display-message", "-t", "main:2"), "/w/scratch"),
    (("display-message", "-t", "other:0"), "/w/logs"),
    (("list-panes", "-t", "main:1"), "%1\t/w/alpha\t80x24\n%2\t/w/alpha\t80x10"),
]


def test_the_tree_lists_sessions_windows_and_split_panes(board, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard_tree, "tmux_cmd", _tmux(TMUX, calls))
    board._build_tree()
    texts = [text for _, text, _, _ in board.tree_lines]
    assert texts[0] == "├── main ◀"
    assert texts[1].startswith("│   ├── ")
    assert "alpha (dev) [:3000] /w/alpha ◀" in texts[1]
    assert texts[2].endswith("%1 (80x24) /w/alpha")
    assert texts[3].endswith("%2 (80x10) /w/alpha")
    assert texts[4] == "│   └── scratch /w/scratch"
    assert texts[5] == "└── other"
    assert texts[6] == "    └── logs /w/logs"
    targets = [board.tree_lines[i][3] for i in board.tree_selectable]
    assert targets == ["main:1", "main:2", "other:0"]
    project_attr = board.tree_lines[1][2]
    assert project_attr == dashboard.STATE_COLOR_PAIR["IDLE"] * 256


def test_enter_on_a_window_switches_to_it(board, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard_tree, "tmux_cmd", _tmux(TMUX, calls))
    board._build_tree()
    calls.clear()
    reset = []
    monkeypatch.setattr(dashboard_tree, "reset_window_after_attach", reset.append)
    board.tree_selected = 2                       # other:0
    assert board._handle_tree_key(10, None) == "attached"
    assert reset == ["other:0"]
    assert ("switch-client", "-t", "other") in calls
    assert ("select-window", "-t", "other:0") in calls


def test_enter_on_a_shell_project_starts_claude_through_the_safe_path(
        board, monkeypatch):
    calls, started = [], []
    monkeypatch.setattr(dashboard_tree, "tmux_cmd", _tmux(TMUX, calls))
    board._build_tree()
    board.projects[0].state = "SHELL"
    monkeypatch.setattr(dashboard_tree, "reset_window_after_attach", lambda wt: None)
    monkeypatch.setattr(dashboard_tree, "auto_start_claude",
                        lambda wt: started.append(wt))
    monkeypatch.setattr(dashboard_tree, "_announce_unlaunched", lambda r: None)
    board.tree_selected = 0
    assert board._handle_tree_key(10, None) == "attached"
    assert started == ["main:1"]
    assert ("switch-client", "-t", "main") not in calls


@pytest.mark.parametrize("key, expected_selected", [
    (curses.KEY_DOWN, 1), (ord("j"), 1), (curses.KEY_UP, 2), (ord("k"), 2),
])
def test_arrow_keys_wrap_around(board, monkeypatch, key, expected_selected):
    monkeypatch.setattr(dashboard_tree, "tmux_cmd", _tmux(TMUX, []))
    board._build_tree()
    board._handle_tree_key(key, None)
    assert board.tree_selected == expected_selected


def test_d_returns_to_the_dashboard_and_q_quits(board):
    board.tree_lines, board.tree_selectable, board.tree_selected = [], [], 0
    assert board._handle_tree_key(ord("d"), None) == ""
    assert board.mode == "dashboard"
    assert board._handle_tree_key(ord("q"), None) == "quit"


def test_enter_leaves_copy_mode_and_resets_before_switching(board, monkeypatch):
    calls, order = [], []
    monkeypatch.setattr(dashboard_tree, "tmux_cmd", _tmux(TMUX, calls))
    board._build_tree()
    calls.clear()

    def tmux(*args, **kwargs):
        order.append(("tmux",) + args)
        return ""
    monkeypatch.setattr(dashboard_tree, "tmux_cmd", tmux)
    monkeypatch.setattr(dashboard_tree, "reset_window_after_attach",
                        lambda wt: order.append(("reset", wt)))
    board._before_switch = lambda: order.append(("before-switch",))
    board.tree_selected = 2                       # other:0
    board._handle_tree_key(10, None)
    assert order == [
        ("tmux", "send-keys", "-t", "other:0", "-X", "cancel"),
        ("reset", "other:0"),
        ("before-switch",),
        ("tmux", "switch-client", "-t", "other"),
        ("tmux", "select-window", "-t", "other:0"),
    ]


def test_a_shell_project_that_could_not_start_says_so(board, monkeypatch):
    import ccm_window
    import dashboard_shared
    calls, said = [], []
    monkeypatch.setattr(dashboard_tree, "tmux_cmd", _tmux(TMUX, calls))
    board._build_tree()
    board.projects[0].state = "SHELL"
    monkeypatch.setattr(dashboard_tree, "reset_window_after_attach", lambda wt: None)
    monkeypatch.setattr(dashboard_tree, "auto_start_claude",
                        lambda wt: ccm_window.LaunchResult(ccm_window.UNAVAILABLE))
    monkeypatch.setattr(dashboard_shared, "tmux_cmd",
                        lambda *a, **k: said.append(a) or "")
    board.tree_selected = 0
    board._handle_tree_key(10, None)
    assert len(said) == 1
    assert said[0][0] == "display-message"
    assert "not auto-started" in said[0][-1]


class _Screen:
    def __init__(self, height, width):
        self.size, self.writes = (height, width), []

    def erase(self):
        pass

    def refresh(self):
        pass

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, text, attr=0):
        self.writes.append((y, x, text, attr))


def _lines(n):
    return [(1, f"window {i}", 7, f"s:{i}") for i in range(n)]


def test_a_small_terminal_shows_only_its_size(board):
    screen = _Screen(5, 30)
    board.tree_lines, board.tree_selectable, board.tree_selected = _lines(3), [0], 0
    board._render_tree(screen)
    assert screen.writes == [(0, 0, "Terminal too small (30x5)", 0)]


def test_the_selected_line_is_marked_and_scrolled_into_view(board, monkeypatch):
    written = []
    monkeypatch.setattr(dashboard.Dashboard, "_addstr",
                        lambda self, scr, y, x, text, attr=0:
                        written.append((y, x, text, attr)))
    screen = _Screen(12, 80)                      # rows 1..10 hold lines
    board.tree_lines = _lines(30)
    board.tree_selectable = list(range(30))
    board.tree_selected = 25
    board._render_tree(screen)
    rows = {y: (text, attr) for y, x, text, attr in written if x == 2 and y > 0}
    assert rows[10] == ("window 25", 7 | curses.A_BOLD)
    assert rows[1] == ("window 16", 7)
    assert (10, 0, "▶ ", curses.A_BOLD) in written
    assert max(rows) == 10
