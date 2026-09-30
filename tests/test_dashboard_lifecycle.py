"""Lifecycle UI delegates mutations to CLI and never hides their output."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import ccm_core
import ccm_dashboard_lifecycle as life
import ccm_snapshot as snapshot
import ccm_snapshot_store as store
from dashboard import Dashboard
from test_dashboard import _stub_dashboard_environment, _make_mock_stdscr
from snapshot_fixture import inventory_query


@pytest.fixture
def ui(monkeypatch, tmp_path):
    _stub_dashboard_environment(monkeypatch)
    monkeypatch.setattr(ccm_core, 'CCM_SNAPSHOT_DIR', str(tmp_path / 'snapshots'))
    d = Dashboard()
    d.projects = [SimpleNamespace(name='alpha', state='PERMIT')]
    d.selected = 0
    d._trigger_rebuild = Mock()
    d._spool_text = Mock()
    d._prompt = Mock(return_value='y')
    return d, _make_mock_stdscr()


@pytest.mark.parametrize('answer', ['y', 'N', '', None])
@pytest.mark.parametrize('operation', ['cancel', 'reset', 'exit', 'all', 'delete'])
def test_confirmation_default_no_and_shared_cli(ui, monkeypatch, answer, operation):
    d, screen = ui
    d._prompt.return_value = answer
    run = Mock()
    monkeypatch.setattr(d, '_run_terminal_command', run)
    if operation == 'cancel':
        d._do_prepare_logout(screen, cancel=True)
        function, args = snapshot.cmd_prepare_logout, ['--cancel']
    elif operation in ('reset', 'exit'):
        d._do_project_recovery(screen, operation)
        function = life.commands.cmd_reset if operation == 'reset' else life.commands.cmd_exit
        args = 'alpha' if operation == 'reset' else ['alpha', '-y']
        if operation == 'exit':
            assert 'PERMIT' in d._spool_text.call_args.args[2]
            assert 'rejects the pending tool call' in d._spool_text.call_args.args[2]
    elif operation == 'all':
        cli = Mock()
        monkeypatch.setattr(life.commands, 'cmd_exit', cli)
        d._do_exit_all(screen)
        if answer == 'y':
            run.call_args.args[2]()
            cli.assert_called_once_with(['alpha', '-y'])
    else:
        monkeypatch.setattr(life, 'snapshots', lambda: [('_autosave', 'sample')])
        screen.getch.side_effect = [ord('d'), 27]
        d._do_snapshots(screen)
        function, args = snapshot.cmd_snapshot_delete, '_autosave'
    assert run.call_count == (1 if answer == 'y' else 0)
    if answer == 'y' and operation != 'all':
        assert run.call_args.args[2:] == (function, args)


def test_prepare_delegates_without_bypassing_cli_confirmation(ui, monkeypatch):
    d, screen = ui
    d._run_terminal_command = Mock()
    d._do_prepare_logout(screen)
    assert d._run_terminal_command.call_args.args[2:] == (snapshot.cmd_prepare_logout, [])
    d._prompt.assert_not_called()


@pytest.mark.parametrize('failure', [None, 'die', 'exit', 'exception', 'interrupt'])
def test_cli_runs_outside_curses_and_result_remains_readable(ui, monkeypatch, capsys, failure):
    d, screen = ui
    events = []
    for name in ('def_prog_mode', 'endwin', 'reset_prog_mode', 'curs_set'):
        monkeypatch.setattr(life.curses, name, lambda *a, name=name: events.append(name))
    def command():
        assert events == ['def_prog_mode', 'endwin']
        print('Restored alpha; resume sidekick manually', flush=True)
        if failure == 'die':
            ccm_core.ccm_die('Directory missing: example')
        if failure == 'exit':
            print('Checkpoint protected', file=life.sys.stderr)
            raise SystemExit(1)
        if failure == 'exception':
            raise RuntimeError('Read failed: example')
        if failure == 'interrupt':
            raise KeyboardInterrupt
    result = d._run_terminal_command(screen, 'Load checkpoint', command)
    assert result is (failure is None)
    assert events[-2:] == ['reset_prog_mode', 'curs_set']
    body = d._spool_text.call_args.args[2]
    assert 'Restored alpha; resume sidekick manually' in body
    assert ('Completed.' in body) is (failure is None)
    if failure == 'die':
        assert 'Directory missing: example' in body
    elif failure == 'exit':
        assert 'Checkpoint protected' in body
    elif failure == 'exception':
        assert 'Read failed: example' in body
    assert 'Restored alpha' in capsys.readouterr().out
    d._trigger_rebuild.assert_called_once()


def test_snapshot_list_metadata_and_no_previous_checkpoint(ui, monkeypatch):
    d, screen = ui
    directory = store.directory()
    directory.mkdir()
    # Valid legacy snapshots can be listed alongside a real v2 collection.
    (directory / 'named.json').write_text(json.dumps({'version': 1, 'name': 'named', 'created': '2026-01-01', 'projects': []}))
    (directory / '_autosave.prev').write_text('{}')
    monkeypatch.setattr(ccm_core, 'tmux_query', inventory_query('1\tw\talpha\t/tmp/alpha'))
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '1 0 1 sh 00:10')
    monkeypatch.setattr(store, '_observed_state', lambda *a: 'SHELL')
    data = store.collect('_autosave', sealed=True)
    (directory / '_autosave.json').write_text(json.dumps(data))
    rows = life.snapshots()
    assert [r[0] for r in rows] == ['_autosave', 'named']
    assert 'v2' in rows[0][1] and 'protected' in rows[0][1] and '1 projects' in rows[0][1]
    assert 'v1' in rows[1][1] and '2026-01-01' in rows[1][1]
    d._run_terminal_command = Mock()
    screen.getch.side_effect = [life.curses.KEY_DOWN, 10, 27]
    d._do_snapshots(screen)
    assert d._run_terminal_command.call_args.args[2:] == (snapshot.cmd_snapshot_load, 'named')


@pytest.mark.parametrize('source', ['job', 'sealed', 'pending', 'none', 'broken'])
def test_restore_banner_and_continue_source(ui, monkeypatch, source):
    d, screen = ui
    directory = store.directory()
    directory.mkdir()
    if source == 'job':
        (directory / '.restore-state').write_text(json.dumps({'source': 'named'}))
    elif source == 'broken':
        (directory / '.restore-state').write_text('broken')
    monkeypatch.setattr(store, 'read', lambda path: {'version': 2, 'checkpoint': {'sealed': source == 'sealed'}})
    query = Mock(return_value='1' if source == 'pending' else '')
    line, name = life.restore_status(query)
    assert bool(line) is (source != 'none')
    assert name == {'job': 'named', 'sealed': '_autosave'}.get(source)
    d._run_terminal_command = Mock()
    d._do_snapshots = Mock()
    d._do_continue_restore(screen, query)
    if name:
        assert d._run_terminal_command.call_args.args[2:] == (snapshot.cmd_snapshot_load, name)
    else:
        d._do_snapshots.assert_called_once_with(screen)


def test_protected_delete_uses_cli_refusal_and_displays_reason(ui, monkeypatch):
    d, screen = ui
    directory = store.directory()
    directory.mkdir()
    path = directory / '_autosave.json'
    path.write_text('checkpoint bytes')
    monkeypatch.setattr(ccm_core, 'init_dirs', lambda: None)
    monkeypatch.setattr(store, 'read', lambda path: {'version': 2, 'checkpoint': {'sealed': True}})
    for name in ('def_prog_mode', 'endwin', 'reset_prog_mode', 'curs_set'):
        monkeypatch.setattr(life.curses, name, lambda *a: None)
    assert not d._run_terminal_command(screen, 'Delete checkpoint', snapshot.cmd_snapshot_delete, '_autosave')
    assert path.read_text() == 'checkpoint bytes'
    assert 'Snapshot protected' in d._spool_text.call_args.args[2]


def test_menu_load_never_invokes_cli_inside_curses(ui, monkeypatch):
    d, screen = ui
    d.menu_items = [('Saved checkpoints', 'load')]
    d.menu_selected = 0
    d._do_snapshots = Mock()
    d._handle_menu_key(10, screen)
    d._do_snapshots.assert_called_once_with(screen)
    d._prompt.assert_not_called()


def test_output_neutralizes_control_sequences():
    import io
    target = io.StringIO()
    output = life.TerminalOutput(target)
    output.write('\x1b[31malpha\x1b[0m\n\x1b]0;title\x07#(example)')
    assert '\x1b' not in target.getvalue() and '\x07' not in target.getvalue()
    assert '＃(example)' in target.getvalue()
    assert 'alpha\n' in target.getvalue()


@pytest.mark.parametrize('answer', ['y', 'N', '', '\x1b'])
def test_prepare_keeps_cli_consent_and_reports_protection(ui, monkeypatch, answer):
    import contextlib
    d, screen = ui
    data = {'version': 2, 'projects': [], 'checkpoint': {
        'sealed': True, 'interrupted': [{'name': 'alpha', 'state': 'PERMIT'}]}}
    monkeypatch.setattr(store, 'locked', contextlib.nullcontext)
    monkeypatch.setattr(store, 'collect', lambda *a, **k: data)
    monkeypatch.setattr(snapshot.ccm_restore, 'paused', lambda: False)
    write = Mock()
    monkeypatch.setattr(store, 'write', write)
    monkeypatch.setattr(snapshot, '_confirmation', lambda: answer)
    monkeypatch.setattr(life.sys.stdin, 'isatty', lambda: True)
    for name in ('def_prog_mode', 'endwin', 'reset_prog_mode', 'curs_set'):
        monkeypatch.setattr(life.curses, name, lambda *a: None)
    d._do_prepare_logout(screen)
    assert write.call_count == (1 if answer == 'y' else 0)
    body = d._spool_text.call_args.args[2]
    assert 'alpha: PERMIT' in body
    assert ('saved and protected' if answer == 'y' else 'Save cancelled') in body


def test_menu_scroll_keeps_selected_action_visible(ui):
    d, screen = ui
    screen.getmaxyx.return_value = (12, 80)
    d._build_menu()
    d.menu_selected = len(d.menu_items) - 1
    d._render_menu(screen)
    assert any('Quit' in str(c) for c in screen.addstr.call_args_list)


def test_long_result_uses_full_terminal_width(ui):
    d, screen = ui
    d._render_max_col = 25
    widths = []
    d._spool_text = lambda *a: widths.append(d._render_max_col)
    d._lifecycle_text(screen, 'Result', 'example')
    assert widths == [0]
    assert d._render_max_col == 25
