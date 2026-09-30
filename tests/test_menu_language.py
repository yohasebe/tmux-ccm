"""Exercise actual rendered text and the dashboard keys shown beside menu items."""
import curses
from unittest.mock import Mock
import pytest
import dashboard
import ccm_menu_help as help_text
from ccm_render import display_width
from test_dashboard import _stub_dashboard_environment, _make_mock_stdscr


def make_menu(monkeypatch, language=''):
    _stub_dashboard_environment(monkeypatch)
    monkeypatch.setattr(dashboard, '_IS_MACOS', True)
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a: language if a[-1] == '@ccm-lang' else '')
    d = dashboard.Dashboard(initial_mode='menu')
    d._build_menu()
    return d


def test_both_languages_cover_every_action(monkeypatch):
    d = make_menu(monkeypatch)
    assert set(help_text.MENU_HELP) == set(help_text.MENU_HELP_JA) == {a for _, a in d.menu_items if a}


@pytest.mark.parametrize('language,expected', [('', 'Create a project'), ('en', 'Create a project'), ('ja', 'プロジェクトのウィンドウ'), ('xx', 'Create a project')])
def test_option_controls_rendered_body_only(monkeypatch, language, expected):
    d = make_menu(monkeypatch, language)
    d.preview_enabled, d.preview_position = True, 'right'
    screen = _make_mock_stdscr(200, 40)
    d._render_menu(screen)
    text = '\n'.join(c.args[2] for c in screen.addstr.call_args_list)
    assert expected in text
    assert 'Add project' in text and 'Menu  (d=dashboard, q=quit)' in text


@pytest.mark.parametrize('action,method', [('add', '_do_add'), ('ignore', '_do_ignore_toggle'), ('spool', '_do_spool'), ('save', '_do_save'), ('tree', '_build_tree'), ('quit', None)])
def test_displayed_key_performs_dashboard_operation(monkeypatch, action, method):
    d = make_menu(monkeypatch)
    d.preview_enabled = False
    d.menu_items = [next(item for item in d.menu_items if item[1] == action)]
    screen = _make_mock_stdscr(100, 40)
    d._render_menu(screen)
    hints = [c.args for c in screen.addstr.call_args_list if c.args[2].startswith('[')]
    assert len(hints) == 1
    _, x, hint, attr = hints[0]
    assert x + display_width(hint) == 99
    assert attr == curses.color_pair(dashboard.C_DIM)
    d.mode = 'dashboard'
    d.projects = [{}]
    d.selected = 0
    handler = Mock(return_value='')
    if method:
        monkeypatch.setattr(d, method, handler)
    for char in (hint[1], hint[1].upper()):
        result = d._handle_key(ord(char), screen)
        if method:
            assert handler.call_count >= 1
            handler.reset_mock()
        else:
            assert result == 'quit'
        if action == 'tree':
            assert d.mode == 'tree'


def test_absent_and_narrow_hints(monkeypatch):
    d = make_menu(monkeypatch)
    d.preview_enabled = False
    assert ('Undelivered messages', 'spool') in d.menu_items
    for label, action in d.menu_items:
        d.menu_items = [(label, action)]
        screen = _make_mock_stdscr(160, 40)
        d._render_menu(screen)
        hints = [c.args[2] for c in screen.addstr.call_args_list if c.args[2].startswith('[')]
        assert bool(hints) == (action in help_text.MENU_KEYS)
    d.menu_items = [('Wide name ' * 8, 'add')]
    screen = _make_mock_stdscr(40, 40)
    d._render_menu(screen)
    text = '\n'.join(c.args[2] for c in screen.addstr.call_args_list)
    assert 'Wide name' in text and '[a]' not in text


@pytest.mark.parametrize('language', ['en', 'ja'])
@pytest.mark.parametrize('position', ['right', 'bottom'])
def test_current_value_cleaning_and_panel_bounds(monkeypatch, language, position):
    d = make_menu(monkeypatch, language)
    d.preview_enabled, d.preview_position = True, position
    d.menu_items = [('Auto-restore: 測定値\x1b\x07#(sample)', 'auto_restore')]
    screen = _make_mock_stdscr(80, 30)
    d._render_menu(screen)
    output = '\n'.join(c.args[2] for c in screen.addstr.call_args_list)
    assert ('現在値:' if language == 'ja' else 'Current:') in output
    assert '測定値' in output and '{current}' not in output
    assert '\x1b' not in output and '\x07' not in output and '#(' not in output
    for call in screen.addstr.call_args_list:
        y, x, text = call.args[:3]
        assert x + display_width(text) < 80 and y < 30
