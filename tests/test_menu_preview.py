"""Menu help coverage, selection and panel layout without live tmux calls."""
from pathlib import Path
from unittest.mock import Mock
import re

import pytest
import dashboard
import ccm_menu_help as help_text
from ccm_render import display_width
from test_dashboard import _stub_dashboard_environment, _make_mock_stdscr, _screen_text


@pytest.fixture
def menu(monkeypatch):
    _stub_dashboard_environment(monkeypatch)
    d = dashboard.Dashboard(initial_mode='menu')
    d.preview_enabled = True
    d.preview_position = 'right'
    monkeypatch.setattr(dashboard, '_IS_MACOS', True)
    d._build_menu()
    return d


def select(d, action):
    d.menu_selected = next(i for i, (_, key) in enumerate(d.menu_items) if key == action)


@pytest.mark.parametrize('macos', [True, False])
def test_every_menu_action_has_help_and_bilingual_dashboard_documentation(menu, monkeypatch, macos):
    monkeypatch.setattr(dashboard, '_IS_MACOS', macos)
    menu._build_menu()
    actions = {action for _, action in menu.menu_items if action}
    assert actions <= help_text.MENU_HELP.keys()
    if macos:
        assert actions == help_text.MENU_HELP.keys()
    for lang in ('', '.ja'):
        doc = (Path(__file__).parents[1] / 'docs' / f'guide{lang}.md').read_text()
        # Only accept coverage in the dashboard section, not elsewhere in the guide.
        section = doc
        start = section.index('### Dashboard actions' if not lang else '### ダッシュボードの操作')
        end = section.find('\n## ', start)
        section = section[start:end if end != -1 else None]
        for label, action in menu.menu_items:
            if not action:
                continue
            name = re.sub(r'\s*\([^)]*\)', '', label.split(': ')[0])
            assert name in section, (lang, action)
            text = help_text.description(action, label)
            assert 3 <= len(text.splitlines()) <= 6
            assert '{current}' not in text


@pytest.mark.parametrize('enabled,position,width,height,visible', [
    (True, 'right', 100, 30, True), (True, 'right', 80, 30, True),
    (False, 'right', 100, 30, False), (True, 'right', 79, 30, False),
    (True, 'bottom', 100, 20, True), (True, 'bottom', 100, 19, False),
    (False, 'bottom', 100, 30, False),
])
def test_preview_toggle_position_and_size(menu, monkeypatch, enabled, position, width, height, visible):
    menu.preview_enabled, menu.preview_position = enabled, position
    screen = _make_mock_stdscr(width, height)
    selected = Mock(wraps=menu._render_menu_description)
    monkeypatch.setattr(menu, '_render_menu_description', selected)
    menu._render_menu(screen)
    assert selected.call_count == int(visible)
    if visible:
        *_, col, row, panel_width, panel_height = help_text.preview_geometry(width, height, enabled, position)
        assert selected.call_args.args[1:] == (col, row, panel_width, panel_height)
        output = _screen_text(screen)
        assert 'Same as: ccm add' in output
    else:
        assert not screen.addch.called


def test_selection_changes_help_without_queries_or_project_capture(menu, monkeypatch):
    screen = _make_mock_stdscr(160, 40)
    forbidden = Mock(side_effect=AssertionError('Menu rendering must not query tmux or capture a pane'))
    monkeypatch.setattr(dashboard, 'tmux_cmd', forbidden)
    monkeypatch.setattr(menu, '_update_preview', forbidden)
    menu.menu_selected = 0
    menu._render_menu(screen)
    first = _screen_text(screen)
    screen.reset_mock()
    menu._handle_menu_key(dashboard.curses.KEY_DOWN, screen)
    menu._render_menu(screen)
    second = _screen_text(screen)
    assert 'Same as: ccm add' in first
    assert 'Same as: ccm unregister' in second
    assert 'Same as: ccm add' not in second
    forbidden.assert_not_called()


@pytest.mark.parametrize('position,width,height', [('right', 80, 40), ('bottom', 80, 30)])
def test_description_wraps_inside_panel_and_menu_stays_separate(menu, position, width, height):
    menu.preview_position = position
    select(menu, 'exit')
    screen = _make_mock_stdscr(width, height)
    menu._render_menu(screen)
    lw, lh, col, row, pw, ph = help_text.preview_geometry(width, height, True, position)
    text_calls = [c.args for c in screen.addstr.call_args_list]
    help_calls = [(y, x, text) for y, x, text, *rest in text_calls if
                  (x >= col + 2 if position == 'right' else y > row)]
    assert len(help_calls) > 3
    assert all(x + display_width(text) < width for y, x, text in help_calls)
    assert all(y < (ph if position == 'right' else row + ph) for y, x, text in help_calls)
    assert any('Escape' in text for _, _, text in help_calls)
    assert any('Exit Claude in selected' in line and '▶' in line for line in _screen_text(screen).splitlines())


def test_current_value_and_label_are_neutralized(menu, monkeypatch):
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a: 'on\x1b\x07#(example)')
    menu._build_menu()
    select(menu, 'auto_restore')
    screen = _make_mock_stdscr(200, 40)
    menu._render_menu(screen)
    output = _screen_text(screen)
    assert 'Current: on' in output
    assert '\x1b' not in output and '\x07' not in output and '#(' not in output
    assert '＃(example)' in output


@pytest.mark.parametrize('width', [10, 20, 37])
def test_wrap_preserves_words_and_wide_character_columns(width):
    text = '選択した project ' + 'abcdefghij' * 5
    rows = help_text.wrap_text(text, width)
    assert all(display_width(row) <= width for row in rows)
    assert ''.join(rows).replace(' ', '') == text.replace(' ', '')


def test_setting_value_updates_after_menu_rebuild(menu, monkeypatch):
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a: 'off')
    menu._build_menu()
    select(menu, 'notify')
    label, action = menu.menu_items[menu.menu_selected]
    assert 'Current: off' in help_text.description(action, label)
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a: 'completed')
    menu._build_menu()
    label, action = menu.menu_items[menu.menu_selected]
    assert 'Current: completed' in help_text.description(action, label)
