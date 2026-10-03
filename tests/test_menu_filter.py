"""Menu headings, live filter and styling, without live tmux calls."""
import curses

import pytest
import dashboard
from test_dashboard import _stub_dashboard_environment, _make_mock_stdscr, _screen_text


@pytest.fixture
def menu(monkeypatch):
    _stub_dashboard_environment(monkeypatch)
    # Distinguish color pairs so styling is observable.
    monkeypatch.setattr(curses, 'color_pair', lambda n: n << 8)
    d = dashboard.Dashboard(initial_mode='menu')
    d.preview_enabled = True
    d.preview_position = 'right'
    monkeypatch.setattr(dashboard, '_IS_MACOS', True)
    d._build_menu()
    return d


def index_of(d, action):
    return next(i for i, (_, a) in enumerate(d.menu_items) if a == action)


def test_groups_have_headings_that_are_never_selected(menu):
    headings = [label for label, action in menu.menu_items if not action]
    assert headings == ['Actions', 'Settings', 'Navigate']
    assert menu.menu_items[menu.menu_selected][1] == 'add'
    for _ in range(len(menu.menu_items) * 2):
        menu._handle_menu_key(curses.KEY_DOWN, None)
        assert menu.menu_items[menu.menu_selected][1]
    # A stale index on a heading is moved to the next item.
    menu.menu_selected = 0
    menu._normalize_menu_selection()
    assert menu.menu_items[menu.menu_selected][1] == 'add'


def test_auto_start_sits_with_the_other_settings(menu):
    assert index_of(menu, 'status_mode') < index_of(menu, 'auto_start') < index_of(menu, 'notify')


@pytest.mark.parametrize('query,language,expected', [
    ('LOGOUT', 'en', {'prepare_logout', 'cancel_logout'}),
    ('ログアウト', 'ja', {'prepare_logout', 'cancel_logout'}),
    ('ログアウト', 'en', set()),
    ('preview', 'en', {'preview_toggle', 'preview_position'}),
    ('Settings', 'en', set()),
])
def test_filter_matches_labels_and_summary_in_help_language(menu, query, language, expected):
    menu.help_language = language
    found = {menu.menu_items[i][1] for i in menu._menu_matches(query)}
    assert expected <= found
    if not expected:
        assert not found
    assert all(menu.menu_items[i][1] for i in menu._menu_matches(''))


def drive(menu, keys, monkeypatch):
    screen = _make_mock_stdscr(120, 40)
    screen.get_wch.side_effect = list(keys)
    monkeypatch.setattr(curses, 'curs_set', lambda n: 1)
    return screen, menu._do_menu_filter(screen)


def test_enter_runs_the_selected_match_through_the_menu_handler(menu, monkeypatch):
    called = []
    monkeypatch.setattr(menu, '_do_prepare_logout', lambda s, cancel=False: called.append(cancel))
    screen, result = drive(menu, ['p', 'r', 'e', 'p', 'a', 'r', 'e', ' ', 'f', '\n'], monkeypatch)
    assert called == [False]
    assert menu.menu_items[menu.menu_selected][1] == 'prepare_logout'
    screen.timeout.assert_called_with(50)


def test_arrows_move_within_matches_and_esc_keeps_the_selection(menu, monkeypatch):
    called = []
    monkeypatch.setattr(menu, '_do_prepare_logout', lambda s, cancel=False: called.append(cancel))
    _, result = drive(menu, ['l', 'o', 'g', 'o', 'u', 't', curses.KEY_DOWN, '\x1b'], monkeypatch)
    assert result == '' and not called
    assert menu.menu_items[menu.menu_selected][1] == 'cancel_logout'


def test_editing_keys_and_no_match(menu, monkeypatch):
    screen, result = drive(menu, ['z', 'z', 'z', '\n', '\x7f', '\x15', 'q', 'u', 'i', 't', '\x1b'], monkeypatch)
    assert result == ''
    text = _screen_text(screen)
    assert '(no match)' in text and 'Filter: zzz' in text and '0/' in text
    assert menu.menu_items[menu.menu_selected][1] == 'quit'


def test_filtered_view_lists_only_matches_and_highlights_the_query(menu, monkeypatch):
    screen = _make_mock_stdscr(120, 40)
    matches = menu._menu_matches('sound')
    menu.menu_selected = matches[0]
    menu._render_menu(screen, 'sound', matches)
    text = _screen_text(screen)
    assert 'Notification sound' in text and 'Sound name' in text
    assert 'Add project' not in text and 'Settings' not in text.split('Filter')[0].split('\n', 1)[1]
    assert f'2/{sum(1 for _, a in menu.menu_items if a)}' in text
    highlighted = {c.args[2] for c in screen.addstr.call_args_list
                   if c.args[3] == (dashboard.C_YELLOW << 8) | curses.A_BOLD}
    assert {'sound', 'Sound'} <= highlighted


def test_setting_values_and_preview_lines_are_styled(menu):
    screen = _make_mock_stdscr(120, 40)
    menu.menu_selected = index_of(menu, 'add')
    menu._render_menu(screen)
    attrs = {c.args[2]: c.args[3] for c in screen.addstr.call_args_list}
    assert attrs['Same as:'] == dashboard.C_DIM << 8
    assert attrs[' ccm add <dir> [name]'] == dashboard.C_YELLOW << 8
    assert attrs['Add project'] & curses.A_BOLD
    value = menu.menu_items[index_of(menu, 'auto_start')][0].partition(': ')[2]
    assert attrs[value] == menu._menu_value_attr(value)
    assert menu._menu_value_attr('on') == dashboard.C_COMPLETED << 8
    assert menu._menu_value_attr('off') == dashboard.C_DIM << 8
    assert menu._menu_value_attr('Dedicated line') == dashboard.C_CYAN << 8


def test_slash_opens_the_filter(menu, monkeypatch):
    seen = []
    monkeypatch.setattr(menu, '_do_menu_filter', lambda s: seen.append(s) or 'x')
    assert menu._handle_menu_key(ord('/'), 'screen') == 'x' and seen == ['screen']


def test_keypad_enter_runs_the_selection_too(menu, monkeypatch):
    called = []
    monkeypatch.setattr(menu, '_do_add', lambda s: called.append(1))
    drive(menu, [curses.KEY_ENTER], monkeypatch)
    assert called == [1]


def test_no_highlight_when_casefold_changes_the_query_length(menu):
    pieces = menu._menu_label_segments('Background sessions: off', 'bg_section', False, 'ß')
    assert all(attr != (dashboard.C_YELLOW << 8) | curses.A_BOLD for _, attr in pieces)
    assert ''.join(text for text, _ in pieces) == 'Background sessions: off'


def test_long_query_scrolls_and_keeps_the_cursor_in_the_list(menu):
    screen = _make_mock_stdscr(120, 40)
    query = 'x' * 55 + 'END'
    menu._render_menu(screen, query, [])
    list_width = dashboard.ccm_menu_help.preview_geometry(120, 40, True, 'right')[0]
    y, x = screen.move.call_args.args
    assert x < list_width - 1
    assert any(str(c.args[2]).endswith('END') for c in screen.addstr.call_args_list)


def test_label_starts_after_the_marker_with_ambiguous_width_two(menu, monkeypatch):
    import ccm_render
    monkeypatch.setattr(ccm_render, '_AMBIGUOUS_STATE', (2, True))
    screen = _make_mock_stdscr(120, 40)
    menu._render_menu(screen)
    label = next(c.args for c in screen.addstr.call_args_list if c.args[2] == 'Add project')
    assert label[1] == dashboard.display_width('  ▶ ') == 5


def test_scrolling_drops_whole_characters_not_their_marks(menu):
    screen = _make_mock_stdscr(120, 40)
    menu._render_menu(screen, 'á' * 48, [])
    shown = next(str(c.args[2]) for c in screen.addstr.call_args_list
                 if c.args[1] == 10 and '́' in str(c.args[2]))
    assert shown.startswith('á') and shown.endswith('á')


@pytest.mark.parametrize('text,expected', [
    ('', ''), ('ab', 'b'), ('áb', 'b'), ('\U0001F468‍\U0001F469x', 'x'), ('日本', '本'),
    ('a\u200d', ''), ('\U0001F468\u200d\U0001F469\u200d', '')])
def test_first_grapheme_matches_the_backspace_clustering(text, expected):
    assert dashboard.Dashboard._strip_first_grapheme(text) == expected
