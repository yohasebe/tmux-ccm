"""The selected command's explanation must actually reach the menu screen."""
import dashboard
from test_dashboard import _stub_dashboard_environment, _make_mock_stdscr


def test_menu_shows_selected_command_description(monkeypatch):
    _stub_dashboard_environment(monkeypatch)
    d = dashboard.Dashboard(initial_mode='menu')
    d.preview_enabled = True
    d.preview_position = 'right'
    d.menu_items = [('Add project', 'add')]
    d.menu_selected = 0
    screen = _make_mock_stdscr(160, 40)
    d._render_menu(screen)
    text = '\n'.join(str(c.args[2]) for c in screen.addstr.call_args_list)
    assert 'Same as: ccm add <dir> [name]' in text
