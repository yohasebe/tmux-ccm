"""What the dashboard's live-filter search draws."""

import curses
from types import SimpleNamespace

import pytest

import dashboard
import dashboard_search


class _Screen:
    def __init__(self, height, width):
        self.size, self.cursor = (height, width), None

    def redrawwin(self):
        pass

    def erase(self):
        pass

    def refresh(self):
        pass

    def getmaxyx(self):
        return self.size

    def move(self, y, x):
        self.cursor = (y, x)


@pytest.fixture
def drawn(monkeypatch):
    writes = []
    monkeypatch.setattr(curses, "color_pair", lambda n: n * 256)
    monkeypatch.setattr(dashboard.Dashboard, "_addstr",
                        lambda self, scr, y, x, text, attr=0:
                        writes.append((y, x, text, attr)))
    board = object.__new__(dashboard.Dashboard)
    return board, writes


def _projects(n, state="IDLE"):
    return [(i, SimpleNamespace(win_idx=str(i + 1), state=state,
                                name=f"proj{i}")) for i in range(n)]


def _row(writes, y):
    return {x: (text, attr) for wy, x, text, attr in writes if wy == y}


def test_the_matches_show_index_state_and_name(drawn):
    board, writes = drawn
    screen = _Screen(10, 60)
    board._render_search(screen, "pro", _projects(2), 1, 5)
    first, second = _row(writes, 2), _row(writes, 3)
    assert first[0][0] == "    "
    assert second[0][0] == "  ▶ "
    assert first[4][0] == "#1"
    icon = dashboard_search.STATE_ICONS["IDLE"]
    assert second[8] == (f"{icon} IDLE  ",
                         dashboard_search.STATE_COLOR_PAIR["IDLE"] * 256)
    assert second[17] == ("proj1", curses.A_BOLD)
    assert first[17] == ("proj0", 0)


def test_no_match_says_so(drawn):
    board, writes = drawn
    board._render_search(_Screen(10, 60), "zzz", [], 0, 5)
    assert (2, 4, "(no match)", dashboard_search.C_DIM * 256) in writes


def test_the_prompt_shows_the_filter_count_and_cursor(drawn):
    board, writes = drawn
    screen = _Screen(10, 60)
    board._render_search(screen, "ab", _projects(3), 0, 16)
    prompt = _row(writes, 8)
    assert prompt[2][0] == "Filter: "
    assert prompt[10][0] == "ab"
    assert prompt[60 - len("3/16") - 2][0] == "3/16"
    assert screen.cursor == (8, 12)
    assert _row(writes, 9)[2][0].startswith("[↑↓] select")


def test_the_selection_is_scrolled_into_view(drawn):
    board, writes = drawn
    board._render_search(_Screen(10, 60), "", _projects(20), 12, 20)
    # Rows 2..7 hold six matches; the selection is the last of them.
    names = {y: _row(writes, y)[17][0] for y in range(2, 8)}
    assert names == {2: "proj7", 3: "proj8", 4: "proj9", 5: "proj10",
                     6: "proj11", 7: "proj12"}
    assert _row(writes, 7)[0][0] == "  ▶ "


def test_the_cursor_follows_the_drawn_width_of_a_wide_query(drawn):
    board, writes = drawn
    screen = _Screen(10, 60)
    board._render_search(screen, "日本", _projects(1), 0, 1)
    assert screen.cursor == (8, 10 + 4)           # two wide characters


def test_a_long_query_drops_the_count_rather_than_overlap_it(drawn):
    board, writes = drawn
    screen = _Screen(10, 30)
    query = "q" * 14                               # ends at column 24
    board._render_search(screen, query, _projects(3), 0, 16)
    assert "3/16" not in [text for _, _, text, _ in writes]
    board._render_search(screen, "q" * 13, _projects(3), 0, 16)
    assert "3/16" not in [text for _, _, text, _ in writes]
    writes.clear()
    board._render_search(screen, "q" * 11, _projects(3), 0, 16)
    assert (8, 30 - 4 - 2, "3/16", dashboard_search.C_DIM * 256) in writes


def test_a_tiny_screen_draws_no_list_rows(drawn):
    board, writes = drawn
    board._render_search(_Screen(4, 40), "", _projects(5), 0, 5)
    assert not [w for w in writes if w[0] == 2 and w[1] == 17]
    assert _row(writes, 2)[2][0] == "Filter: "
    assert _row(writes, 3)[2][0].startswith("[↑↓] select")
