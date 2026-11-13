"""What the dashboard's main view draws: the preview pane with its ANSI
colours, the help line and the docked logo."""

import curses

import pytest

import dashboard_render
import dashboard_shared
from dashboard import Dashboard


class _Screen:
    def __init__(self):
        self.drawn, self.chars = [], []

    def addstr(self, y, x, text, attr=0):
        self.drawn.append((y, x, text, attr))

    def addch(self, y, x, ch, attr=0):
        self.chars.append((y, x, ch, attr))


@pytest.fixture
def colors(monkeypatch):
    """Colour pairs the terminal was asked for, as (pair, fg, bg)."""
    made = []
    monkeypatch.setattr(curses, "color_pair", lambda n: n * 256)
    monkeypatch.setattr(curses, "init_pair",
                        lambda pair, fg, bg: made.append((pair, fg, bg)))
    monkeypatch.setattr(Dashboard, "_color_pair_cache", {})
    monkeypatch.setattr(Dashboard, "_next_pair_id", 50)
    return made


@pytest.fixture
def board():
    return object.__new__(Dashboard)


@pytest.mark.parametrize("codes, attr", [
    ([0], 0),
    ([1], curses.A_BOLD),
    ([1, 2, 4, 7], curses.A_BOLD | curses.A_DIM | curses.A_UNDERLINE
     | curses.A_REVERSE),
    ([1, 0], 0),
])
def test_sgr_styles_map_to_curses_attributes(colors, codes, attr):
    assert Dashboard._ansi_to_curses_attr(codes) == attr
    assert colors == []


@pytest.mark.parametrize("codes, fg", [
    ([31], 1),
    ([97], 15),
    ([38, 5, 208], 208),
    ([38, 5, 1], 1),                              # the index is not a style
    ([38, 2, 255, 0, 0], 196),                    # nearest cube colour
    ([38, 2, 0, 0, 255], 21),
    ([38, 2, 0, 0, 1], 16),
])
def test_sgr_colours_become_a_pair_with_that_foreground(colors, codes, fg):
    assert Dashboard._ansi_to_curses_attr(codes) == 50 * 256
    assert colors == [(50, fg, -1)]


def test_the_default_colour_code_clears_the_colour(colors):
    assert Dashboard._ansi_to_curses_attr([1, 31, 39]) == curses.A_BOLD
    assert colors == []


def test_a_colour_pair_is_made_once_per_colour(colors):
    first = Dashboard._get_color_pair(3)
    assert Dashboard._get_color_pair(3) == first
    assert Dashboard._get_color_pair(4) == 51 * 256
    assert colors == [(50, 3, -1), (51, 4, -1)]


def test_a_colour_the_terminal_refuses_is_drawn_plain(colors, monkeypatch):
    def refuse(pair, fg, bg):
        raise curses.error("no such colour")
    monkeypatch.setattr(curses, "init_pair", refuse)
    assert Dashboard._get_color_pair(200) == 0
    assert Dashboard._color_pair_cache == {}


def test_the_side_preview_draws_the_tail_in_its_colours(colors, board):
    board._preview_lines = ["old", "\x1b[1mab\x1b[0mc", "日本語"]
    screen = _Screen()
    board._render_preview(screen, 10, 1, 6, 2)
    assert screen.chars == [(1, 10, "│", dashboard_render.C_DIM * 256),
                            (2, 10, "│", dashboard_render.C_DIM * 256)]
    # Three columns of content: the wide line stops before overflowing.
    assert screen.drawn == [
        (1, 12, "a", curses.A_BOLD),
        (1, 13, "b", curses.A_BOLD),
        (1, 14, "c", 0),
        (2, 12, "日", 0),
    ]


def test_the_bottom_preview_keeps_a_row_for_its_rule(colors, board):
    board._preview_lines = ["one", "two", "\x1b[31mred"]
    screen = _Screen()
    board._render_preview_bottom(screen, 5, 6, 3)
    assert screen.drawn[0] == (5, 0, "─────", dashboard_render.C_DIM * 256)
    assert screen.drawn[1:] == [
        (6, 1, "t", 0), (6, 2, "w", 0), (6, 3, "o", 0),
        (7, 1, "r", 50 * 256), (7, 2, "e", 50 * 256), (7, 3, "d", 50 * 256),
    ]


@pytest.fixture
def written(monkeypatch):
    calls = []
    monkeypatch.setattr(curses, "color_pair", lambda n: n * 256)
    monkeypatch.setattr(Dashboard, "_addstr",
                        lambda self, scr, y, x, text, attr=0:
                        calls.append((y, x, text, attr)))
    return calls


def test_the_help_line_brightens_the_keys(board, written):
    board._render_help_line(None, 4, 2, "[q] quit")
    dim = dashboard_render.C_DIM * 256
    key = dashboard_render.C_CYAN * 256 | curses.A_BOLD
    assert written == [(4, 2, "[", dim), (4, 3, "q", key), (4, 4, "]", dim),
                       (4, 5, " ", dim), (4, 6, "q", dim), (4, 7, "u", dim),
                       (4, 8, "i", dim), (4, 9, "t", dim)]


def test_the_help_line_stops_short_of_the_render_edge(board, written):
    board._render_max_col = 6
    board._render_help_line(None, 0, 2, "[q] quit")
    assert [x for _, x, _, _ in written] == [2, 3, 4]


def test_the_docked_logo_is_drawn_before_the_title(board, written):
    assert board._draw_logo(None, 0) == 2 + 9 + len(" Dashboard") + 2
    assert written == [
        (0, 2, " c ", dashboard_shared.C_LOGO_C1 * 256 | curses.A_BOLD),
        (0, 5, " c ", dashboard_shared.C_LOGO_C2 * 256 | curses.A_BOLD),
        (0, 8, " m ", dashboard_shared.C_LOGO_M * 256 | curses.A_BOLD),
        (0, 11, " Dashboard", curses.A_BOLD),
    ]
