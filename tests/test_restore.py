"""Restore contracts that must hold before tmux mutations or agent launch."""
import copy
import json
from unittest.mock import Mock

import pytest

import ccm_core
import ccm_layout as layout
import ccm_pane_state
import ccm_restore as restore
import ccm_roles as roles
import ccm_snapshot as snapshot
import ccm_snapshot_store as store
import ccm_window as window
from snapshot_fixture import inventory_query, layout as checksum


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(ccm_core, 'CCM_SNAPSHOT_DIR', str(tmp_path / 'snapshots'))
    monkeypatch.setattr(ccm_core, 'tmux_query', inventory_query(f'1\tw\talpha\t{tmp_path}'))
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '1 0 1 sh 00:10')
    data = store.collect('_autosave')
    data['checkpoint']['sealed'] = True
    with store.locked():
        store.write('_autosave', data)
    return data


def test_old_pane_ids_are_remapped_even_with_partial_overlap():
    tree = layout.parse(checksum('90x30,0,0{44x30,0,0,1,45x30,45,0[45x14,45,0,2,45x15,45,15,3]}'))
    mapping = {1: '%2', 2: '%3', 3: '%4'}
    restored = layout.parse(layout.render(tree, mapping))
    assert [n['id'] for n in layout.leaves(restored)] == [2, 3, 4]
    assert [layout.rect(n) for n in layout.leaves(restored)] == [[0, 0, 44, 30], [45, 0, 45, 14], [45, 15, 45, 15]]


@pytest.mark.parametrize('width,height', [(160, 50), (80, 24), (220, 70), (5, 5)])
def test_mixed_layout_scales_without_losing_boundaries(width, height):
    tree = layout.parse(checksum('90x30,0,0{44x30,0,0,1,45x30,45,0[45x14,45,0,2,45x15,45,15,3]}'))
    scaled = layout.scale(tree, width, height)
    round_trip = layout.parse(layout.render(scaled, {1: '%4', 2: '%5', 3: '%6'}))
    assert (round_trip['w'], round_trip['h']) == (width, height)
    assert all(n['w'] >= 2 and n['h'] >= 2 for n in layout.leaves(round_trip))
    assert len(layout.leaves(round_trip)) == 3


def test_too_small_and_bad_geometry_are_rejected():
    tree = layout.parse(checksum('90x30,0,0{44x30,0,0,1,45x30,45,0,2}'))
    with pytest.raises(store.SnapshotError, match='too small'):
        layout.scale(tree, 4, 10)
    with pytest.raises(store.SnapshotError, match='tile'):
        layout.parse(checksum('90x30,0,0{44x30,0,0,1,45x30,40,0,2}'))


@pytest.mark.parametrize('conflict', ['missing', 'tag-name', 'tag-cwd', 'untag-name', 'untag-cwd', 'other-session', 'invalid-name'])
def test_preflight_refuses_conflicts_before_creating(checkpoint, monkeypatch, conflict):
    data = copy.deepcopy(checkpoint)
    job = {'id': 'test-job'}
    cwd = data['projects'][0]['dir']
    w = {field: '' for field in restore.WF}
    w.update(session_id='$1', window_id='@2', window_name='unrelated')
    pc = {field: '' for field in restore.PF}
    pc.update(window_id='@2', pane_current_path='/tmp/unrelated')
    if conflict == 'invalid-name':
        data['projects'][0]['name'] = '#(touch example)'
    elif conflict == 'missing':
        data['projects'][0]['restore']['panes'][0]['cwd'] = '/tmp/no-such-restore-dir-908172'
    elif conflict == 'tag-name':
        w.update({'@ccm_project': 'alpha', '@ccm_dir': '/tmp/unrelated'})
    elif conflict == 'tag-cwd':
        w.update({'@ccm_project': 'beta', '@ccm_dir': cwd})
    elif conflict == 'untag-name':
        w['window_name'] = 'alpha'
    elif conflict == 'untag-cwd':
        pc['pane_current_path'] = cwd
    else:
        w.update({'@ccm_project': 'beta', '@ccm_dir': '/tmp/unrelated', 'session_id': '$2'})
    monkeypatch.setattr(restore, 'windows', lambda: [w])
    monkeypatch.setattr(store, '_query', lambda *a: [pc])
    commands = Mock(return_value='$1')
    monkeypatch.setattr(restore, 'run', commands)
    with pytest.raises(store.SnapshotError):
        restore.preflight(data, 'test', job)
    assert all(call.args[0] == 'display-message' for call in commands.call_args_list)


@pytest.mark.parametrize('sealed', [False, True])
def test_matching_window_retained_and_interrupted_only_for_sealed(checkpoint, monkeypatch, capsys, sealed):
    data = copy.deepcopy(checkpoint)
    data['checkpoint'].update(sealed=sealed, interrupted=[{'name': 'alpha', 'state': 'BUSY'}])
    with store.locked():
        store.write('_autosave', data)
    before_backup = (store.directory() / '_autosave.prev').read_bytes()
    w = {field: '' for field in restore.WF}
    w.update({'session_id': '$1', 'window_id': '@3', '@ccm_project': 'alpha', '@ccm_dir': data['projects'][0]['dir']})
    monkeypatch.setattr(restore, 'windows', lambda: [w])
    monkeypatch.setattr(store, '_query', lambda *a: [])
    monkeypatch.setattr(ccm_core, 'require_session', lambda: 'test')
    run = Mock(return_value='$1')
    monkeypatch.setattr(restore, 'run', run)
    restore.load('_autosave')
    output = capsys.readouterr().out
    assert ('Interrupted at save:' in output) is sealed
    assert 'matching registered window retained' in output
    assert all(c.args[0] == 'display-message' for c in run.call_args_list)
    assert not store.sealed(store.read(store.directory() / '_autosave.json'))
    assert (store.directory() / '_autosave.prev').read_bytes() == before_backup


@pytest.mark.parametrize('primary', ['%1', '', '%2'])
def test_launch_never_falls_back_to_active_sidekick(monkeypatch, primary):
    panes = [ccm_pane_state.PaneInfo('%1', '10', False, 'sh', False, None),
             ccm_pane_state.PaneInfo('%2', '11', True, 'sh', False, None)]
    role = {'role': 'primary', 'agent': 'claude', 'ignore': False}
    side = {'role': 'sidekick', 'agent': 'codex', 'ignore': True}
    def tmux(*args):
        if args[-1] == roles.MANAGED_OPTION:
            return '1'
        if args[-1] == roles.ROLE_OPTION:
            pid = args[args.index('-t') + 1]
            return json.dumps(role if pid == primary else side)
        return ''
    command = Mock(side_effect=tmux)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', command)
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '10 1 10 sh 00:10')
    monkeypatch.setattr(window, 'enumerate_window_panes', lambda *a: panes)
    notice = Mock(return_value='fresh hand-off evidence')
    monkeypatch.setattr(window, 'continue_blocker_notice', notice)
    result = window.launch_claude('@1', honour_setting=False)
    sends = [c.args for c in command.call_args_list if c.args[0] == 'send-keys' and c.args[-1] == 'Enter']
    if primary:
        assert result.outcome == window.LAUNCHED
        assert sends[0][2] == primary
        assert result.notice == 'fresh hand-off evidence'
    else:
        assert result.outcome == window.UNAVAILABLE
        assert not sends
    notice.assert_called_once_with('@1')


def test_pending_launch_never_reads_history_or_types(monkeypatch):
    monkeypatch.setattr(roles, 'pending', lambda *a: True)
    notice = Mock()
    monkeypatch.setattr(window, 'continue_blocker_notice', notice)
    assert window.launch_claude('@1').outcome == window.UNAVAILABLE
    notice.assert_not_called()


def test_ignore_reservation_survives_stale_live_marker_cleanup(monkeypatch):
    command = Mock(return_value='')
    monkeypatch.setattr(ccm_core, 'tmux_cmd', command)
    value = json.dumps({'role': 'sidekick', 'agent': 'codex', 'ignore': True})
    pc = ('main:1', '10', '%1', 'sh', '1', '24', '1', value)
    assert ccm_core._resolve_ignored_panes([pc], 'main:1', ['10 0 10 sh 00:10']) == 0
    assert command.call_args.args[-1] == '@ccm_ignore'
    assert all(roles.ROLE_OPTION not in c.args for c in command.call_args_list)


def test_unignore_releases_reserved_intent_and_mismatch_releases_role(monkeypatch):
    value = json.dumps({'role': 'sidekick', 'agent': 'codex', 'ignore': True})
    command = Mock(return_value=value)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', command)
    roles.unignore('%1')
    assert json.loads(command.call_args.args[-1])['ignore'] is False
    command.reset_mock()
    assert roles.reconcile('%1', value, None)['agent'] == 'codex'
    command.assert_not_called()
    assert roles.reconcile('%1', value, 'claude') is None
    command.assert_called_once_with('set-option', '-pu', '-t', '%1', roles.ROLE_OPTION)


def test_pending_job_blocks_unsealed_autosave_and_prepare(checkpoint):
    data = copy.deepcopy(checkpoint)
    data['checkpoint']['sealed'] = False
    with store.locked():
        store.write('_autosave', data)
        restore.save_job({'id': 'test-job', 'source': '_autosave'})
    before = (store.directory() / '_autosave.json').read_bytes()
    assert snapshot.cmd_snapshot_save('_autosave', quiet=True) is False
    with pytest.raises(SystemExit):
        snapshot.cmd_prepare_logout(['--cancel'])
    assert (store.directory() / '_autosave.json').read_bytes() == before


def test_guidance_neutralizes_control_characters_and_tmux_formats():
    assert '\x1b' not in roles.clean('alpha\x1b[31m')
    assert '#(' not in roles.clean('#(touch example)')
    assert '#{' not in roles.clean('#{pane_id}')
    assert '--last' not in roles.hint({'role': 'sidekick', 'agent': 'codex', 'ignore': True})


def test_v1_load_cannot_bypass_incomplete_v2_restore(checkpoint, monkeypatch):
    import ccm_commands
    v1 = {'version': 1, 'projects': [{'name': 'alpha', 'dir': checkpoint['projects'][0]['dir']}]}
    (store.directory() / 'legacy.json').write_text(json.dumps(v1))
    with store.locked():
        restore.save_job({'id': 'test-job', 'source': '_autosave'})
    added = Mock()
    monkeypatch.setattr(ccm_commands, 'cmd_add', added)
    with pytest.raises(SystemExit):
        snapshot.cmd_snapshot_load('legacy')
    added.assert_not_called()


@pytest.mark.parametrize('default', ['valid', '', None, 'relative/bash', '/tmp/bash -c x', '/missing/bash', 'directory', 'not-executable', 'unknown-name'])
def test_login_shell_uses_validated_default_then_environment(tmp_path, monkeypatch, default):
    preferred = tmp_path / 'bash'
    fallback = tmp_path / 'zsh'
    for path in (preferred, fallback):
        path.write_text('#!/bin/sh\n')
        path.chmod(0o700)
    value = default
    if default == 'valid':
        value = str(preferred)
    elif default == 'directory':
        preferred.unlink()
        preferred.mkdir()
        value = str(preferred)
    elif default == 'not-executable':
        preferred.chmod(0o600)
        value = str(preferred)
    elif default == 'unknown-name':
        other = tmp_path / 'editor'
        preferred.rename(other)
        value = str(other)
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *a: value)
    monkeypatch.setenv('SHELL', str(fallback))
    expected = preferred if default == 'valid' else fallback
    assert restore.shell_command('test') == [str(expected), '-l']


def test_invalid_environment_shell_falls_back_to_sh(monkeypatch):
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *a: None)
    monkeypatch.setenv('SHELL', 'bash -c unexpected')
    assert restore.shell_command('test') == ['/bin/sh', '-l']


@pytest.mark.parametrize('persistent', [False, True])
def test_startup_waits_for_transient_child_and_never_terminates_work(monkeypatch, persistent):
    from types import SimpleNamespace
    elapsed = [0.0]
    monkeypatch.setattr(restore, 'time', SimpleNamespace(
        monotonic=lambda: elapsed[0],
        sleep=lambda n: elapsed.__setitem__(0, elapsed[0] + n)))
    def observed(window):
        # Brief initial shell must not hide the following rc child process.
        child = elapsed[0] >= .05 and (persistent or elapsed[0] < .8)
        return {'%1': {'command': 'worker' if child else 'zsh', 'pid': '100', 'cwd': '/tmp/example', 'rect': [0, 0, 80, 24]}}
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '100 1 100 zsh 00:01')
    monkeypatch.setattr(restore, 'panes', observed)
    monkeypatch.setattr(restore, 'layout_geometry', lambda w, rows: rows)
    commands = Mock(side_effect=AssertionError('Startup waiting must not mutate tmux'))
    monkeypatch.setattr(restore, 'run', commands)
    if persistent:
        with pytest.raises(store.SnapshotError, match='leave running processes intact'):
            restore.stable_panes('@1')
        assert 10 <= elapsed[0] < 10.1
    else:
        assert restore.stable_panes('@1')['%1']['command'] == 'zsh'
        assert .9 <= elapsed[0] < 1.1
    commands.assert_not_called()


@pytest.mark.parametrize('processes,expected', [
    ('100 1 100 /bin/zsh 00:01', True),
    ('100 1 100 /bin/zsh 00:01\n101 100 100 sleep 00:01', False),
    ('100 1 100 /bin/zsh 00:01\n101 100 101 worker 00:01', True),
    ('100 1 100 worker 00:01', False),
    ('', False),
])
def test_shell_name_does_not_hide_rc_children(monkeypatch, processes, expected):
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: processes)
    assert restore.idle_shells({'%1': {'pid': '100', 'command': 'zsh'}}) is expected
