"""The dashboard's one-line prompt and its path completion, and the
check a docked dashboard makes for a running popup."""

import curses
import os

import pytest

import dashboard_input
import dashboard_refresh
from dashboard import Dashboard


class _Screen:
    def __init__(self, keys, height=10, width=60):
        self.keys, self.size = list(keys), (height, width)
        self.timeouts, self.moves, self.cleared = [], [], []

    def getmaxyx(self):
        return self.size

    def timeout(self, ms):
        self.timeouts.append(ms)

    def get_wch(self):
        assert self.keys, "the prompt read past the last key"
        key = self.keys.pop(0)
        if isinstance(key, Exception):
            raise key
        return key

    def move(self, y, x):
        self.moves.append((y, x))

    def clrtoeol(self):
        self.cleared.append(self.moves[-1][0])

    def refresh(self):
        pass


@pytest.fixture
def board(monkeypatch):
    d = object.__new__(Dashboard)
    d.drawn, d.cursor = [], []
    monkeypatch.setattr(curses, "color_pair", lambda n: n * 256)
    monkeypatch.setattr(curses, "curs_set", d.cursor.append)
    monkeypatch.setattr(Dashboard, "_addstr",
                        lambda self, s, y, x, text, attr=0:
                        self.drawn.append((y, x, text, attr)))
    return d


# ─── the prompt ───

@pytest.mark.parametrize("enter", ["\n", "\r", 10, 13, curses.KEY_ENTER])
def test_enter_returns_what_was_typed(board, enter):
    screen = _Screen(["a", "b", enter])
    assert board._prompt(screen, "Name: ") == "ab"
    assert screen.timeouts == [-1, 50]
    assert board.cursor == [1, 0]


@pytest.mark.parametrize("escape", ["\x1b", 27])
def test_escape_returns_none(board, escape):
    screen = _Screen(["a", escape])
    assert board._prompt(screen, "Name: ") is None
    assert screen.timeouts == [-1, 50]
    assert board.cursor == [1, 0]


@pytest.mark.parametrize("backspace", ["\x7f", "\b", 127, 8, curses.KEY_BACKSPACE])
def test_backspace_removes_a_whole_character(board, backspace):
    screen = _Screen(["a", "e", "\N{COMBINING ACUTE ACCENT}", backspace, "\n"])
    assert board._prompt(screen, "Name: ") == "a"


def test_backspace_on_nothing_keeps_nothing(board):
    assert board._prompt(_Screen(["\x7f", 127, "x", "\n"]), "Name: ") == "x"


def test_only_printable_characters_and_the_space_are_kept(board, monkeypatch):
    completed = []
    monkeypatch.setattr(Dashboard, "_complete_path",
                        lambda self, text, *a: completed.append(text) or text)
    keys = ["a", curses.error("none"), " ", "日", "\x01",
            "\N{IDEOGRAPHIC SPACE}", "\t", 9, "\n"]
    assert board._prompt(_Screen(keys), "Name: ") == "a 日"
    assert completed == []


def test_the_prompt_sits_on_the_last_row_with_the_cursor_after_the_text(board):
    screen = _Screen(["日", "x", "\n"], height=10)
    board._prompt(screen, "Name: ")
    assert (9, 0, "  Name: ", dashboard_input.C_DIM * 256) in board.drawn
    assert (9, 8, "日x", 0) in board.drawn
    assert screen.moves[-1] == (9, 8 + 3)


@pytest.mark.parametrize("tab", ["\t", 9])
def test_tab_completes_a_path_only_when_asked(board, monkeypatch, tab):
    seen = []

    def complete(self, text, s, row, col):
        seen.append((text, row, col))
        return text + "lib/"
    monkeypatch.setattr(Dashboard, "_complete_path", complete)
    assert board._prompt(_Screen(["~", "/", tab, "\n"]), "Dir: ",
                         path_completion=True) == "~/lib/"
    assert seen == [("~/", 9, 7)]


# ─── path completion ───

@pytest.fixture
def tree(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "projects-old").mkdir()
    (tmp_path / "notes.txt").touch()
    return tmp_path


def _complete(board, text, height=10):
    screen = _Screen([], height=height)
    return board._complete_path(text, screen, height - 1, 8), screen


def test_a_directory_gets_its_slash(board, tree):
    assert _complete(board, str(tree / "project"))[0] == f"{tree}/project/"


def test_a_single_match_is_completed(board, tree):
    assert _complete(board, str(tree / "no"))[0] == f"{tree}/notes.txt"
    assert _complete(board, str(tree / "projects"))[0] == f"{tree}/projects-old/"


@pytest.mark.parametrize("typed", ["pr", "projec"])
def test_several_matches_complete_to_what_they_share(board, tree, typed):
    assert _complete(board, str(tree / typed))[0] == f"{tree}/project"
    assert board.drawn == []


def test_a_tilde_path_stays_a_tilde_path(board, tree, monkeypatch):
    monkeypatch.setenv("HOME", str(tree))
    assert _complete(board, "~/no")[0] == "~/notes.txt"
    assert _complete(board, "~/pr")[0] == "~/project"


def test_ambiguous_matches_are_listed_above_the_prompt(board, tmp_path):
    for i in range(12):
        (tmp_path / f"f{i:02d}").touch()
    text = str(tmp_path / "f")
    result, screen = _complete(board, text)
    assert result == text
    shown = "  ".join(f"f{i:02d}" for i in range(10)) + "  (+2 more)"
    assert board.drawn == [(8, 2, shown, dashboard_input.C_DIM * 256)]
    assert screen.cleared == [8]


@pytest.mark.parametrize("text", ["zzz", "missing/dir/x"])
def test_nothing_to_complete_leaves_the_text(board, tree, text):
    assert _complete(board, str(tree / text))[0] == str(tree / text)
    assert board.drawn == []


# ─── a running popup ───

@pytest.fixture
def pid_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard_refresh, "CCM_TMP_DIR", str(tmp_path))
    return tmp_path


def test_a_live_popup_is_seen(pid_dir):
    (pid_dir / "dashboard.pid").write_text(str(os.getpid()), encoding="utf-8")
    assert dashboard_refresh.popup_dashboard_alive() is True


def test_the_dock_pid_file_is_not_a_popup(pid_dir):
    (pid_dir / "dashboard-dock.pid").write_text(str(os.getpid()), encoding="utf-8")
    assert dashboard_refresh.popup_dashboard_alive() is False


@pytest.mark.parametrize("content", ["", "not a pid"])
def test_an_unreadable_pid_file_means_no_popup(pid_dir, content):
    (pid_dir / "dashboard.pid").write_text(content, encoding="utf-8")
    assert dashboard_refresh.popup_dashboard_alive() is False


def test_a_finished_popup_is_not_seen(pid_dir, monkeypatch):
    (pid_dir / "dashboard.pid").write_text("4242", encoding="utf-8")

    def gone(pid, sig):
        raise ProcessLookupError(pid)
    monkeypatch.setattr(dashboard_refresh.os, "kill", gone)
    assert dashboard_refresh.popup_dashboard_alive() is False
