"""The docked dashboard: layout arithmetic, toggle decisions, and the
checkpoint setting the dock aside. No live tmux; see test_dock.bats."""
from unittest.mock import Mock

import pytest

import ccm_core
import ccm_dock as dock
import ccm_layout as layout
import ccm_snapshot_store as store
from snapshot_fixture import layout as checksum


def test_without_drops_a_top_row_and_gives_its_rows_back():
    split = checksum('80x24,0,0[80x9,0,0,9,80x14,0,10{40x14,0,10,1,39x14,41,10,2}]')
    assert layout.without(split, '%9') == checksum('80x24,0,0{40x24,0,0,1,39x24,41,0,2}')


def test_without_rescales_remaining_siblings_in_proportion():
    stack = checksum('80x24,0,0[80x9,0,0,9,80x7,0,10,1,80x6,0,18,2]')
    assert layout.without(stack, 9) == checksum('80x24,0,0[80x12,0,0,1,80x11,0,13,2]')


@pytest.mark.parametrize('shape,pane', [
    ('80x24,0,0,1', '%1'),
    ('80x24,0,0[80x12,0,0,1,80x11,0,13,2]', '%7'),
])
def test_without_refuses_what_it_cannot_remove(shape, pane):
    with pytest.raises(store.SnapshotError):
        layout.without(checksum(shape), pane)


@pytest.fixture
def tmux(monkeypatch, tmp_path):
    """A scripted tmux: replies by command name, records every call."""
    monkeypatch.setattr(ccm_core, 'CCM_TMP_DIR', str(tmp_path))
    calls, replies = [], {}

    def query(*args, **kwargs):
        calls.append(args)
        reply = replies.get(args[0], '')
        return reply(*args) if callable(reply) else reply
    monkeypatch.setattr(ccm_core, 'tmux_query', query)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a, **k: replies.get(('opt',) + a[2:3], ''))
    return calls, replies


def names(calls):
    return [c[0] for c in calls]


@pytest.mark.parametrize('value,expected', [('top', 'top'), ('bottom', 'bottom'), ('', ''), ('left', ''), ('off', '')])
def test_position_accepts_only_top_or_bottom(tmux, value, expected):
    _, replies = tmux
    replies[('opt', dock.POSITION_OPTION)] = value
    assert dock.position() == expected


@pytest.mark.parametrize('size,height,rows', [('40', 40, 16), ('', 40, 16), ('5', 100, 10), ('95', 100, 80), ('x', 20, 10)])
def test_rows_are_clamped_and_never_below_what_the_dashboard_draws(tmux, size, height, rows):
    _, replies = tmux
    replies[('opt', dock.SIZE_OPTION)] = size
    assert dock.rows_for(height) == rows


def test_toggle_opens_a_full_width_pane_above_and_marks_it(tmux):
    calls, replies = tmux
    replies[('opt', dock.POSITION_OPTION)] = 'top'
    replies['list-panes'] = ''
    replies['display-message'] = '80x40,0,0,1\t40'
    replies['split-window'] = '%5'
    dock.toggle('@1')
    split = next(c for c in calls if c[0] == 'split-window')
    assert split[:5] == ('split-window', '-b', '-f', '-v', '-l')
    assert ('-t', '@1') == split[6:8]
    assert ('set-option', '-p', '-t', '%5', dock.DOCK_OPTION, '1') in calls


def test_toggle_in_the_docks_window_closes_and_restores_the_layout(tmux):
    calls, replies = tmux
    replies['list-panes'] = '%5\t@1\t1\n%1\t@1\t'
    split = checksum('80x24,0,0[80x9,0,0,5,80x14,0,10,1]')
    replies['display-message'] = f'{split}\t24'
    dock.toggle('@1')
    restore = [c for c in calls if c[0] in ('kill-pane', 'select-layout')]
    assert restore == [('kill-pane', '-t', '%5'), ('select-layout', '-t', '@1', checksum('80x24,0,0,1'))]


@pytest.mark.parametrize('rest_unchanged', [True, False])
def test_an_untouched_window_gets_its_exact_previous_layout_back(tmux, rest_unchanged):
    calls, replies = tmux
    before = checksum('80x24,0,0[80x14,0,0,1,80x9,0,15,2]')
    split = checksum('80x24,0,0[80x9,0,0,5,80x8,0,10,1,80x5,0,19,2]')
    rest = layout.without(split, '%5')
    replies['list-panes'] = '%5\t@1\t1'
    replies['display-message'] = f'{split}\t24'
    options = {dock.BEFORE_OPTION: before, dock.AFTER_OPTION: rest if rest_unchanged else 'something else'}
    replies['show-option'] = lambda *a: options.get(a[-1], '')
    dock.close()
    chosen = next(c for c in calls if c[0] == 'select-layout')[-1]
    assert chosen == (before if rest_unchanged else rest)
    assert ('set-option', '-wu', '-t', '@1', dock.BEFORE_OPTION) in calls


def test_toggle_elsewhere_moves_the_dock_and_focuses_it(tmux):
    calls, replies = tmux
    replies[('opt', dock.POSITION_OPTION)] = 'top'
    replies['list-panes'] = '%5\t@1\t1'
    split = checksum('80x24,0,0[80x9,0,0,5,80x14,0,10,1]')
    replies['display-message'] = f'{split}\t24'
    dock.toggle('@2')
    join = next(c for c in calls if c[0] == 'join-pane')
    assert join[:5] == ('join-pane', '-b', '-d', '-f', '-v')
    assert join[-4:] == ('-s', '%5', '-t', '@2')
    assert ('select-layout', '-t', '@1', checksum('80x24,0,0,1')) in calls
    assert calls[-1] == ('select-pane', '-t', '%5')


def _display():
    split = checksum('80x24,0,0[80x9,0,0,5,80x14,0,10,1]')
    return lambda *a: f'{split}\t24'


def test_follow_does_nothing_without_a_dock(tmux):
    calls, replies = tmux
    replies['list-panes'] = ''
    dock.follow('@2')
    assert names(calls) == ['list-panes']


@pytest.mark.parametrize('clients,hint,target', [
    ('100\t0\t@2\n90\t0\t@3', '@3', '@2'),   # a late hook for an older window loses
    ('100\t0\t@2', '@2', '@2'),
    ('', '@2', '@2'),                            # no client readable: the hook's window
    ('100\t0\t@9\n100\t0\t@10', '@10', '@10'),  # same second: the hook's window wins
    ('100\t0\t@3\n200\t1\t@4', '@4', '@3'),    # a screenless control client does not lead
    ('200\t1\t@4', '@4', '@4'),                 # unless it is the only client
])
def test_follow_moves_to_the_latest_clients_window_and_keeps_focus(tmux, clients, hint, target):
    calls, replies = tmux
    replies['list-panes'] = '%5\t@1\t1'
    replies['list-clients'] = clients
    replies['display-message'] = _display()
    dock.follow(hint)
    join = next(c for c in calls if c[0] == 'join-pane')
    assert join[-2:] == ('-t', target)
    assert 'select-pane' not in names(calls)


def test_follow_is_a_no_op_when_the_dock_is_where_the_latest_client_looks(tmux):
    calls, replies = tmux
    replies['list-panes'] = '%5\t@2\t1'
    replies['list-clients'] = '100\t0\t@2\n90\t0\t@1'
    dock.follow('@1')
    assert 'join-pane' not in names(calls)


def test_a_failed_move_leaves_the_source_layout_alone(tmux):
    calls, replies = tmux
    replies['list-panes'] = '%5\t@1\t1'
    replies['display-message'] = _display()
    replies['join-pane'] = None
    dock.follow('@2')
    assert 'select-layout' not in names(calls)


def test_the_dashboard_command_is_this_plugins_ccm_and_ignores_the_environment(monkeypatch):
    import os
    import shlex
    monkeypatch.setenv('CCM_DOCK_COMMAND', 'sleep 300')
    ccm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(dock.__file__))), 'ccm')
    assert dock.dashboard_command() == f'{shlex.quote(ccm)} dashboard --docked'


# --- the checkpoint sets the dock aside ------------------------------------

def _inventory(dock_active):
    split = checksum('120x40,0,0[120x16,0,0,5,120x23,0,17{60x23,0,17,1,59x23,61,17,2}]')

    def query(command, *a, **k):
        if command == 'list-windows':
            return f'$1\t@1\t3\talpha\t/tmp/alpha\t{split}\t120\t40\t0\t3\tIDLE\t\tEND'
        return '\n'.join([
            f'@1\t%5\t0\t15\tpython3\t/tmp/alpha\t\t{int(dock_active)}\t16\t\t1\t0\tEND',
            f'@1\t%1\t1\t11\tzsh\t/tmp/alpha\t\t{int(not dock_active)}\t23\t\t\t1\tEND',
            '@1\t%2\t2\t12\tzsh\t/tmp/alpha\t\t0\t23\t\t\t0\tEND'])
    return query


@pytest.mark.parametrize('dock_active', [False, True])
def test_checkpoint_records_the_window_without_the_dock(monkeypatch, tmp_path, dock_active):
    monkeypatch.setattr(ccm_core, 'CCM_SNAPSHOT_DIR', str(tmp_path))
    monkeypatch.setattr(ccm_core, 'tmux_query', _inventory(dock_active))
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '1 0 1 sh 00:10')
    monkeypatch.setattr(store.ccm_roles, 'reconcile', Mock())
    data = store.collect('_autosave')
    saved = data['projects'][0]['restore']
    assert saved['layout'] == checksum('120x40,0,0{60x40,0,0,1,59x40,61,0,2}')
    assert [p['layout_id'] for p in saved['panes']] == [1, 2]
    # With the dock focused, the pane focused before it is recorded.
    assert saved['active_slot'] == 0


# --- docked dashboard quits through tmux, stays open on attach ------------

@pytest.mark.parametrize('close_on_open', ['', 'off', 'on'])
def test_docked_dashboard_on_attach_follows_the_setting_and_closes_through_tmux(monkeypatch, close_on_open):
    import dashboard
    d = dashboard.Dashboard.__new__(dashboard.Dashboard)
    d.docked = True
    asked, marks = [], []
    monkeypatch.setattr(dock, 'request_close', lambda: asked.append(1))
    monkeypatch.setattr(dock, 'mark_leaving', lambda pane, leaving=True: marks.append(leaving))
    monkeypatch.setattr(dashboard.time, 'sleep', lambda s: None)
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a, **k: close_on_open)
    monkeypatch.setenv('TMUX_PANE', '%5')
    closes = close_on_open == 'on'
    d._before_switch()
    assert d._should_stop('attached') is closes and asked == ([1] if closes else [])
    asked.clear()
    assert d._should_stop('quit') is True and asked == [1]
    d.docked = False
    asked.clear()
    assert d._should_stop('attached') is True and asked == []


def test_the_close_decision_made_before_the_switch_is_kept(monkeypatch):
    # The option turning off (or failing to read) between the mark and the
    # close must not leave a marked dock open.
    import dashboard
    d = dashboard.Dashboard.__new__(dashboard.Dashboard)
    d.docked = True
    value = {'v': 'on'}
    asked = []
    monkeypatch.setattr(dock, 'request_close', lambda: asked.append(1))
    monkeypatch.setattr(dock, 'mark_leaving', lambda *a, **k: None)
    monkeypatch.setattr(dashboard.time, 'sleep', lambda s: None)
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a, **k: value['v'])
    d._before_switch()
    value['v'] = ''
    assert d._should_stop('attached') is True and asked == [1]


def test_a_mark_without_a_switch_is_cleared(monkeypatch):
    import dashboard
    d = dashboard.Dashboard.__new__(dashboard.Dashboard)
    d.docked = True
    marks = []
    monkeypatch.setattr(dock, 'mark_leaving', lambda pane, leaving=True: marks.append((pane, leaving)))
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a, **k: 'on')
    monkeypatch.setenv('TMUX_PANE', '%5')
    d._before_switch()
    assert d._should_stop('') is False
    assert marks == [('%5', True), ('%5', False)]
    assert d._closing_after_switch is False


def test_a_new_background_attach_window_is_created_unselected(monkeypatch):
    import dashboard
    from test_dashboard import _stub_dashboard_environment
    _stub_dashboard_environment(monkeypatch)
    d = dashboard.Dashboard(initial_mode='dashboard', docked=True)
    order = []

    def tmux_cmd(*args, **kwargs):
        order.append(args[0])
        if args[0] == 'new-window':
            assert '-d' in args
            return 'w:3'
        return 'on' if args[-1] == '@ccm-dashboard-dock-close-on-open' else ''
    monkeypatch.setattr(dashboard, 'tmux_cmd', tmux_cmd)
    monkeypatch.setattr(dock, 'mark_leaving', lambda *a, **k: order.append('mark'))
    monkeypatch.setattr(d, '_find_bg_attach_windows', lambda *a: ('none', []))
    from types import SimpleNamespace
    d.bg_sessions = [SimpleNamespace(short='a1b2c3d4', cwd='', state='working')]
    d.projects = []
    monkeypatch.setattr(dashboard, 'get_session', lambda: 'w')
    assert d._do_attach_bg(None, 0) == 'attached'
    assert order.index('mark') < order.index('select-window')
    assert order.index('new-window') < order.index('mark')


# --- the dock is not a project pane ----------------------------------------

def test_bulk_pane_cache_drops_the_dock(monkeypatch):
    rows = ['w:0\t11\t%1\tclaude\t1\t20\t\t\t',
            'w:0\t15\t%5\tpython3\t0\t10\t\t\t1']
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a, **k: '\n'.join(rows))
    assert [pc[2] for pc in ccm_core._build_panes_cache()] == ['%1']


def test_window_enumeration_drops_the_dock(monkeypatch):
    import ccm_pane_state
    monkeypatch.setattr(ccm_core, 'tmux_cmd',
                        lambda *a, **k: '%1\t11\t1\tclaude\t\t\n%5\t15\t0\tpython3\t\t1')
    panes = ccm_pane_state.enumerate_window_panes('w:0', [])
    assert [p.pane_id for p in panes] == ['%1']


def test_popup_and_dock_keep_separate_pid_files(monkeypatch, tmp_path):
    import dashboard
    monkeypatch.setattr(dashboard, 'CCM_TMP_DIR', str(tmp_path))
    killed = []
    monkeypatch.setattr(dashboard, '_pid_is_dashboard', lambda pid: True)
    monkeypatch.setattr(dashboard.os, 'kill', lambda pid, sig: killed.append(pid))
    (tmp_path / 'dashboard-dock.pid').write_text('424242')
    dashboard.acquire_pidfile('dashboard.pid')
    assert killed == []  # a popup leaves the dock running
    dashboard.acquire_pidfile('dashboard-dock.pid')
    assert 424242 in killed  # a new dock replaces the old one


def test_auto_focus_reaches_a_lone_work_pane_when_the_dock_has_focus(monkeypatch):
    import ccm_window
    listing = '100\t%1\tclaude\t0\t23\t\n200\t%2\tpython3\t1\t16\t1'
    selected = []

    def tmux_cmd(*args, **kwargs):
        if args[:1] == ('show-option',):
            return '/tmp/alpha'
        if args[:1] == ('list-panes',):
            return listing
        if args[:1] == ('select-pane',):
            selected.append(args[-1])
        return ''
    monkeypatch.setattr(ccm_core, 'tmux_cmd', tmux_cmd)
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '')
    monkeypatch.setattr(ccm_window, 'read_work_clock', lambda *a, **k: None)
    monkeypatch.setattr(ccm_window, 'detect_pane_state', lambda pid, *a, **k: 'PERMIT' if pid == '100' else 'SHELL')
    ccm_window.auto_focus_attention_pane('w:0')
    assert selected == ['%1']


@pytest.mark.parametrize('docked', [True, False])
def test_docked_dashboard_draws_the_logo_on_its_first_line(monkeypatch, docked):
    import dashboard
    from test_dashboard import _stub_dashboard_environment, _make_mock_stdscr, _screen_text
    _stub_dashboard_environment(monkeypatch)
    d = dashboard.Dashboard(initial_mode='dashboard', docked=docked)
    d.projects = []
    d.initial_load = False
    screen = _make_mock_stdscr(120, 30)
    d.render(screen)
    first = _screen_text(screen).splitlines()[0]
    assert (' c  c  m  Dashboard' in first) is docked


def _menu_with(monkeypatch, options):
    import dashboard
    from test_dashboard import _stub_dashboard_environment
    _stub_dashboard_environment(monkeypatch)
    writes, saved = [], []

    def tmux_cmd(*args, **kwargs):
        if args[0] == 'set':
            options[args[-2]] = args[-1]
            writes.append(args)
        return options.get(args[-1], '')
    monkeypatch.setattr(dashboard, 'tmux_cmd', tmux_cmd)
    monkeypatch.setattr(dashboard, 'save_tmux_conf_setting', saved.append)
    d = dashboard.Dashboard(initial_mode='menu')
    d._show_message = lambda *a, **k: None
    d._build_menu()
    return d, writes, saved


def _press(d, action):
    d.menu_selected = next(i for i, (_, a) in enumerate(d.menu_items) if a == action)
    return d._handle_menu_key(10, None)


def test_display_mode_cycles_popup_top_bottom_and_saves(monkeypatch):
    options = {}
    d, writes, saved = _menu_with(monkeypatch, options)
    for expected in ('top', 'bottom', 'off'):
        _press(d, 'dock_mode')
        assert options['@ccm-dashboard-dock'] == expected
        assert saved[-1] == f'set -g @ccm-dashboard-dock {expected}'


def test_turning_docking_off_from_a_docked_dashboard_closes_it(monkeypatch):
    options = {'@ccm-dashboard-dock': 'bottom'}
    d, _, _ = _menu_with(monkeypatch, options)
    d.docked = True
    assert _press(d, 'dock_mode') == 'quit'
    options['@ccm-dashboard-dock'] = 'top'
    d._build_menu()
    d.docked = False
    assert _press(d, 'dock_mode') != 'quit'


def test_close_after_open_toggles_and_saves(monkeypatch):
    options = {'@ccm-dashboard-dock': 'top'}
    d, _, saved = _menu_with(monkeypatch, options)
    _press(d, 'dock_close')
    assert options['@ccm-dashboard-dock-close-on-open'] == 'on'
    assert saved[-1] == 'set -g @ccm-dashboard-dock-close-on-open on'
    _press(d, 'dock_close')
    assert options['@ccm-dashboard-dock-close-on-open'] == 'off'


def test_follow_leaves_a_closing_dock_in_place(tmux):
    calls, replies = tmux
    replies['list-panes'] = '%5\t@1\t1\t1'
    replies['list-clients'] = '100\t0\t@2'
    dock.follow('@2')
    assert 'join-pane' not in names(calls)
    # A plain toggle or close still finds it.
    assert dock.find() == ('%5', '@1')


@pytest.mark.parametrize('docked,close_on_open,marked', [
    (True, 'on', True), (True, 'off', False), (False, 'on', False)])
def test_the_dashboard_marks_its_dock_only_when_it_will_close(monkeypatch, docked, close_on_open, marked):
    import dashboard
    d = dashboard.Dashboard.__new__(dashboard.Dashboard)
    d.docked = docked
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a, **k: close_on_open)
    monkeypatch.setenv('TMUX_PANE', '%5')
    marks = []
    monkeypatch.setattr(dock, 'mark_leaving', marks.append)
    d._before_switch()
    assert marks == (['%5'] if marked else [])
