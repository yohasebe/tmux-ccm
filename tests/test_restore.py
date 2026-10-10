"""Restore contracts that must hold before tmux mutations or agent launch."""
import copy
from pathlib import Path
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


REAL_ANNOUNCE = restore._announce


@pytest.fixture(autouse=True)
def announcements(monkeypatch):
    """Status-area notices the restore would show, instead of live tmux."""
    shown = []
    monkeypatch.setattr(restore, '_announce',
                        lambda kind, body, ms, session=None: shown.append((f'{kind}: {body}', ms)))
    return shown


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
    # A conflict of one project keeps only that project back; the session
    # or the snapshot being wrong stops the restore.
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
    if conflict in ('other-session', 'invalid-name'):
        with pytest.raises(store.SnapshotError) as caught:
            restore.preflight(data, 'test', job)
        assert not isinstance(caught.value, restore.WindowProblem)
    else:
        existing, problems = restore.preflight(data, 'test', job)
        assert existing == {}
        assert list(problems) == ['0']
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
    monkeypatch.setattr(ccm_core, 'tmux_query', command)
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
    updated = roles.reconcile('%1', value, 'claude')
    assert updated == {'role': 'unknown', 'agent': 'claude', 'ignore': False}
    command.assert_called_once_with('set-option', '-p', '-t', '%1', roles.ROLE_OPTION, json.dumps(updated))


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
])
def test_shell_name_does_not_hide_rc_children(monkeypatch, processes, expected):
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: processes)
    assert restore.idle_shells({'%1': {'pid': '100', 'command': 'zsh'}}) is expected


def test_unreadable_process_list_stops_the_restore_not_one_window(monkeypatch):
    # Not seeing processes says nothing about one window's rc, so it must
    # not become that window's startup timeout.
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '')
    with pytest.raises(store.SnapshotError) as caught:
        restore.idle_shells({'%1': {'pid': '100', 'command': 'zsh'}})
    assert not isinstance(caught.value, restore.WindowProblem)


@pytest.mark.parametrize('layout,expected', [
    ({'%1': 'shell'}, '%1'),                          # saved while Claude was stopped
    ({'%1': 'shell', '%2': 'sidekick'}, '%1'),        # sidekick pane is never chosen
    ({'%1': 'sidekick'}, None),                       # only a sidekick pane: refuse
])
def test_restored_window_without_primary_still_launches(monkeypatch, layout, expected):
    """A window saved while its Claude was not running has no primary
    reservation; opening it must still start Claude outside sidekicks."""
    panes = [ccm_pane_state.PaneInfo(pid, str(10 + i), i == len(layout) - 1, 'sh', False, None)
             for i, pid in enumerate(layout)]
    def tmux(*args):
        if args[-1] == roles.MANAGED_OPTION:
            return '1'
        if args[-1] == roles.ROLE_OPTION:
            pid = args[args.index('-t') + 1]
            agent = 'codex' if layout[pid] == 'sidekick' else None
            return json.dumps({'role': layout[pid], 'agent': agent, 'ignore': False})
        return ''
    command = Mock(side_effect=tmux)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', command)
    monkeypatch.setattr(ccm_core, 'tmux_query', command)
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '10 1 10 sh 00:10')
    monkeypatch.setattr(window, 'enumerate_window_panes', lambda *a: panes)
    monkeypatch.setattr(window, 'continue_blocker_notice', lambda *a: None)
    result = window.launch_claude('@1', honour_setting=False)
    sends = [c.args for c in command.call_args_list if c.args[0] == 'send-keys' and c.args[-1] == 'Enter']
    if expected:
        assert result.outcome == window.LAUNCHED and sends[0][2] == expected
    else:
        assert result.outcome == window.UNAVAILABLE and not sends


def _load_within(name, seconds=5):
    # A missing wait would block on the run lock this test still holds;
    # fail instead of hanging the suite.
    import threading
    errors = []

    def target():
        try:
            restore.load(name)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), 'load blocked on the run lock instead of waiting for it'
    if errors:
        raise errors[0]


def _retained_window(data, monkeypatch):
    w = {field: '' for field in restore.WF}
    w.update({'session_id': '$1', 'window_id': '@3', '@ccm_project': 'alpha', '@ccm_dir': data['projects'][0]['dir']})
    monkeypatch.setattr(restore, 'windows', lambda: [w])
    monkeypatch.setattr(store, '_query', lambda *a: [])
    monkeypatch.setattr(ccm_core, 'require_session', lambda: 'test')
    monkeypatch.setattr(restore, 'run', Mock(return_value='$1'))


def _hold(run_id=None):
    """Take the run lock as another run would, optionally with a known id."""
    fd, run = restore._acquire_running()
    assert fd is not None
    if run_id is not None:
        restore.os.ftruncate(fd, 0)
        restore.os.pwrite(fd, run_id.encode(), 0)
        run = run_id
    return fd, run


def test_run_lock_is_visible_while_held_and_released_after(checkpoint):
    assert restore.holder() is None and not restore.running()
    fd, run = restore._acquire_running()
    try:
        assert restore.holder() == run and restore.running()
        assert restore._acquire_running() == (None, None)
    finally:
        restore.os.close(fd)
    assert restore.holder() is None


@pytest.mark.parametrize('record, expected', [
    ({'total': 10, 'done': 2, 'samples': 2, 'spent': 4.0}, None),
    ({'total': 10, 'done': 4, 'samples': 4, 'spent': 8.0}, 12),
    ({'total': 10, 'done': 10, 'samples': 10, 'spent': 20.0}, None),
    ({'total': 'x'}, None),
])
def test_remaining_time_needs_rebuilt_samples(record, expected):
    assert restore.remaining_seconds(record) == expected


def test_progress_phrase_names_current_project_and_estimate():
    record = {'total': 45, 'done': 12, 'current': 'alpha\x1b[31m', 'samples': 12, 'spent': 18.0}
    assert restore.describe(record) == '12/45: alpha [31m · about 50s left'
    record.update(samples=3, spent=600.0)
    assert restore.describe(record).endswith('about 110 min left')
    assert restore.describe({}) == 'in progress'


def test_successful_load_records_done_with_summary(checkpoint, monkeypatch, capsys):
    _retained_window(checkpoint, monkeypatch)
    restore.load('_autosave')
    record = restore.read_progress()
    assert record['state'] == 'done' and record['done'] == record['total'] == 1
    assert any('existing layout and roles retained' in line for line in record['summary'])
    assert not restore.running()


def test_failed_load_records_reason_and_releases_lock(checkpoint, monkeypatch):
    _retained_window(checkpoint, monkeypatch)
    monkeypatch.setattr(restore, 'preflight', Mock(side_effect=store.SnapshotError(
        'Restore shell startup did not settle within 10 seconds')))
    with pytest.raises(SystemExit):
        restore.load('_autosave')
    record = restore.read_progress()
    assert record['state'] == 'stopped'
    assert 'did not settle' in record['error']
    assert not restore.running()


def test_unexpected_exception_marks_progress_stopped(checkpoint, monkeypatch, announcements):
    _retained_window(checkpoint, monkeypatch)
    monkeypatch.setattr(restore, 'preflight', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        restore.load('_autosave')
    assert restore.read_progress()['state'] == 'stopped'
    assert announcements[-1][0] == 'stopped: interrupted \u00b7 dashboard menu: Continue restore'
    assert not restore.running()


def test_stop_reason_survives_a_writer_cleaning_staged_snapshots(checkpoint, monkeypatch):
    # Another writer's lock cleanup runs between staging and replacing.
    _retained_window(checkpoint, monkeypatch)
    monkeypatch.setattr(restore, 'preflight', Mock(side_effect=store.SnapshotError('Project directory missing: alpha')))
    real_replace = restore.os.replace

    def replace(src, dst):
        # Only the stop record is written outside the writer lock.
        if str(dst).endswith(restore.PROGRESS) and '"stopped"' in open(src).read():
            with store.locked():
                pass
        return real_replace(src, dst)
    monkeypatch.setattr(restore.os, 'replace', replace)
    with pytest.raises(SystemExit):
        restore.load('_autosave')
    record = restore.read_progress()
    assert record['state'] == 'stopped' and 'directory missing' in record['error']


def test_second_load_waits_and_reports_instead_of_restoring(checkpoint, monkeypatch, capsys):
    held, run = _hold('first-run')
    restore._publish({'run': run, 'source': '_autosave', 'state': 'running', 'total': 45, 'done': 12,
                      'current': 'alpha', 'samples': 0, 'spent': 0.0})

    def sleep(_):
        restore._publish({'run': run, 'source': '_autosave', 'state': 'done', 'total': 45, 'done': 45,
                          'summary': ['Restored 45/45; snapshot protection released.']})
        restore.os.close(held)
    monkeypatch.setattr(restore.time, 'sleep', sleep)
    inner = Mock()
    monkeypatch.setattr(restore, '_load', inner)
    _load_within('_autosave')
    out = capsys.readouterr().out
    assert 'already running' in out and '12/45: alpha' in out
    assert 'That restore finished:' in out and 'Restored 45/45' in out
    inner.assert_not_called()
    assert not restore.running()


def test_old_success_is_not_reported_for_a_run_that_left_no_record(checkpoint, monkeypatch, capsys):
    # A previous run's `done` must not stand in for the run this load
    # waited for, which failed before it could publish anything.
    restore._publish({'run': 'earlier', 'source': '_autosave', 'state': 'done', 'summary': ['old summary']})
    held, _ = _hold('failed-run')

    def sleep(_):
        restore.os.close(held)
    monkeypatch.setattr(restore.time, 'sleep', sleep)
    inner = Mock()
    monkeypatch.setattr(restore, '_load', inner)
    _load_within('_autosave')
    out = capsys.readouterr().out
    assert 'finished' not in out and 'old summary' not in out
    inner.assert_called_once()


def test_every_waiter_reports_the_run_that_finished_while_it_waited(checkpoint, monkeypatch, capsys):
    # The first holder stops; another waiter takes over and finishes. A
    # later waiter must report that result instead of restoring again.
    first, _ = _hold('run-a')
    restore.save_job({'source': '_autosave'})
    steps = []

    def sleep(_):
        if not steps:
            restore._publish({'run': 'run-a', 'source': '_autosave', 'state': 'stopped', 'error': 'x'})
            restore.os.close(first)
            steps.append(_hold('run-b'))
        else:
            fd, _ = steps[-1]
            restore.job_path().unlink()
            restore._publish({'run': 'run-b', 'source': '_autosave', 'state': 'done', 'summary': ['done by b']})
            restore.os.close(fd)
    monkeypatch.setattr(restore.time, 'sleep', sleep)
    inner = Mock()
    monkeypatch.setattr(restore, '_load', inner)
    _load_within('_autosave')
    out = capsys.readouterr().out
    assert 'That restore finished:' in out and 'done by b' in out
    inner.assert_not_called()


def test_load_after_a_stopped_run_reports_reason_and_continues(checkpoint, monkeypatch, capsys):
    held, run = _hold('stopping-run')
    restore.save_job({'source': '_autosave'})

    def sleep(_):
        restore._publish({'run': run, 'source': '_autosave', 'state': 'stopped', 'error': 'tmux list-panes failed'})
        restore.os.close(held)
    monkeypatch.setattr(restore.time, 'sleep', sleep)
    inner = Mock()
    monkeypatch.setattr(restore, '_load', inner)
    _load_within('_autosave')
    assert 'That restore stopped: tmux list-panes failed' in capsys.readouterr().out
    inner.assert_called_once()


@pytest.mark.parametrize('state', ['done', 'stopped'])
@pytest.mark.parametrize('error', [KeyboardInterrupt, OSError])
def test_lock_is_released_when_reporting_the_waited_result_fails(checkpoint, monkeypatch, state, error):
    held, run = _hold('first-run')
    if state == 'stopped':
        restore.save_job({'source': '_autosave'})

    def sleep(_):
        restore._publish({'run': run, 'source': '_autosave', 'state': state, 'error': 'reason',
                          'summary': ['line']})
        restore.os.close(held)
    monkeypatch.setattr(restore.time, 'sleep', sleep)
    monkeypatch.setattr(restore, '_load', Mock())

    def broken_print(*args, **kwargs):
        if args and str(args[0]).startswith('That restore'):
            raise error('output failed')
    monkeypatch.setattr(restore, 'print', broken_print, raising=False)
    with pytest.raises(error):
        _load_within('_autosave')
    assert not restore.running()
    fd, _ = restore._acquire_running()
    assert fd is not None
    restore.os.close(fd)


def test_a_run_whose_id_cannot_be_written_does_not_start(checkpoint, monkeypatch, capsys):
    # An older id left in the lock file would let a waiter take an earlier
    # success for this run's result.
    (store.directory() / restore.RUNNING).write_text('earlier')
    restore._publish({'run': 'earlier', 'source': '_autosave', 'state': 'done', 'summary': ['OLD SUCCESS']})

    def fail(*args):
        raise OSError('read-only')
    monkeypatch.setattr(restore.os, 'ftruncate', fail)
    inner = Mock()
    monkeypatch.setattr(restore, '_load', inner)
    with pytest.raises(SystemExit):
        restore.load('_autosave')
    inner.assert_not_called()
    assert 'Cannot record the restore run' in capsys.readouterr().err
    assert not restore.running()


@pytest.mark.parametrize('call', ['ftruncate', 'pwrite'])
def test_lock_is_released_when_recording_the_run_is_interrupted(checkpoint, monkeypatch, call):
    real = getattr(restore.os, call)

    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(restore.os, call, interrupt)
    with pytest.raises(KeyboardInterrupt):
        restore.load('_autosave')
    # Restore only the syscall; undo() would also drop the fixture's directory.
    monkeypatch.setattr(restore.os, call, real)
    assert not restore.running()


def test_stop_reason_names_the_checkpoint_actually_read(checkpoint, monkeypatch):
    # A save under the same name lands while the restore waits for the
    # writer lock; the stop must be tied to what was read, not to the
    # content seen before waiting.
    _retained_window(checkpoint, monkeypatch)
    replaced = copy.deepcopy(checkpoint)
    replaced['projects'][0]['name'] = 'beta'
    real_locked = store.locked

    import contextlib

    @contextlib.contextmanager
    def locked_after_save():
        monkeypatch.setattr(store, 'locked', real_locked)
        with real_locked():
            store.write('_autosave', replaced)
        with real_locked():
            yield
    monkeypatch.setattr(store, 'locked', locked_after_save)
    monkeypatch.setattr(restore, 'preflight', Mock(side_effect=store.SnapshotError('Directory not found: beta')))
    with pytest.raises(SystemExit):
        restore.load('_autosave')
    record = restore.read_progress()
    assert record['state'] == 'stopped'
    assert record['file_digest'] == restore._file_digest('_autosave')



def test_progress_and_outcome_are_announced_in_the_status_area(checkpoint, monkeypatch, announcements):
    _retained_window(checkpoint, monkeypatch)
    restore.load('_autosave')
    texts = [text for text, _ in announcements]
    assert texts[0] == 'progress: restoring 1 window(s)\u2026'
    assert texts[1].startswith('progress: 0/1: alpha')
    assert texts[-1].startswith('done: restored 1/1 window(s)')
    assert announcements[-1][1] == restore.ANNOUNCE_DONE_MS


def test_a_stop_is_announced_with_its_reason(checkpoint, monkeypatch, announcements):
    _retained_window(checkpoint, monkeypatch)
    monkeypatch.setattr(restore, 'preflight', Mock(side_effect=store.SnapshotError('Directory not found: alpha')))
    with pytest.raises(SystemExit):
        restore.load('_autosave')
    assert [t for t, _ in announcements if 'interrupted' in t] == []
    text, ms = announcements[-1]
    assert text == 'stopped: stopped at 0/1 \u00b7 dashboard menu: Continue restore \u00b7 Directory not found: alpha'
    assert ms == restore.ANNOUNCE_STOPPED_MS


def test_a_waiting_load_does_not_announce(checkpoint, monkeypatch, announcements):
    held, run = _hold('first-run')

    def sleep(_):
        restore._publish({'run': run, 'source': '_autosave', 'state': 'done', 'summary': []})
        restore.os.close(held)
    monkeypatch.setattr(restore.time, 'sleep', sleep)
    monkeypatch.setattr(restore, '_load', Mock())
    _load_within('_autosave')
    assert announcements == []


def test_announcement_reaches_every_client_of_the_session_without_freezing(monkeypatch):
    calls = []

    def query(*args):
        calls.append(args)
        return 'c1\nc2\n' if args[0] == 'list-clients' else ''
    monkeypatch.setattr(restore.ccm_core, 'tmux_query', query)
    REAL_ANNOUNCE('progress', '1/2: #[bg=red]#(touch x) 100% /tmp/%Y \x1b[31m', 5000, 'work')
    message = (restore._NOTICE_BADGES['progress'] + restore._NOTICE_BODY
               + '1/2: \uff03[bg=red]\uff03(touch x) 100%% /tmp/%%Y  [31m #[default]')
    assert calls == [('list-clients', '-t', 'work', '-F', '#{client_name}'),
                     ('display-message', '-C', '-d', '5000', '-c', 'c1', message),
                     ('display-message', '-C', '-d', '5000', '-c', 'c2', message)]


def test_announcement_without_a_session_uses_the_default_client(monkeypatch):
    calls = []
    monkeypatch.setattr(restore.ccm_core, 'tmux_query', lambda *a: calls.append(a))
    REAL_ANNOUNCE('done', 'anything', 5000)
    expected = restore._NOTICE_BADGES['done'] + restore._NOTICE_BODY + 'anything #[default]'
    assert calls == [('display-message', '-C', '-d', '5000', expected)]

    def fail(*a):
        raise OSError('no server')
    monkeypatch.setattr(restore.ccm_core, 'tmux_query', fail)
    REAL_ANNOUNCE('stopped', 'anything', 5000, 'work')


@pytest.mark.parametrize('listing', ['', None])
def test_a_session_without_listable_clients_tells_no_one_else(monkeypatch, listing):
    calls = []

    def query(*args):
        calls.append(args)
        return listing if args[0] == 'list-clients' else ''
    monkeypatch.setattr(restore.ccm_core, 'tmux_query', query)
    REAL_ANNOUNCE('progress', '1/2', 5000, 'work')
    assert [c for c in calls if c[0] == 'display-message'] == []


def test_notice_styles_reset_inherited_attributes_first():
    # A reverse or underscore message-style would otherwise carry over.
    for style in (*restore._NOTICE_BADGES.values(), restore._NOTICE_BODY):
        assert style.startswith('#[none,')


def test_a_job_from_the_previous_version_resumes_publication_it_began():
    legacy = {'id': 'j', 'source': '_autosave', 'digest': 'd', 'publishing': True,
              'windows': {'0': {'window': '@1', 'ready': True}, '1': {'window': '@2', 'ready': True}}}
    job = restore._upgrade_job(copy.deepcopy(legacy))
    assert job['version'] == restore.JOB_VERSION
    assert 'publishing' not in job
    assert [s['publish'] for s in job['windows'].values()] == ['started', 'started']
    assert job['failed'] == {}
    plain = restore._upgrade_job({'id': 'j', 'windows': {'0': {'window': '@1'}}})
    assert 'publish' not in plain['windows']['0']


def _publishing(monkeypatch, project, mark, state=None, **window):
    w = {field: '' for field in restore.WF}
    w.update({'session_id': '$1', 'window_id': '@1', '@ccm_restore_job': 'j:0',
              '@ccm_restore_pending': mark})
    w.update(window)
    job = {'id': 'j', 'windows': {'0': state} if state else {}, 'failed': {}}
    monkeypatch.setattr(restore, 'windows', lambda: [w])
    writes = Mock(return_value='')
    monkeypatch.setattr(restore, 'run', writes)
    monkeypatch.setattr(restore, 'save_job', Mock())
    return job, writes


def test_a_window_released_before_a_stop_is_confirmed_never_rewritten(checkpoint, monkeypatch):
    project = checkpoint['projects'][0]
    job, writes = _publishing(monkeypatch, project, '0', {'window': '@1', 'publish': 'started'},
                              **{'@ccm_project': project['name'], '@ccm_dir': project['dir']})
    monkeypatch.setattr(restore, '_verify', Mock(side_effect=AssertionError('verified a usable window')))
    restore._restore_and_publish(job, '0', project, '$1', False)
    assert job['windows']['0']['publish'] == 'done'
    writes.assert_not_called()


@pytest.mark.parametrize('mark,state,window', [
    # released by ccm, then registered to something else
    ('0', {'window': '@1', 'publish': 'started'}, {'@ccm_project': 'other'}),
    # not yet released, but someone registered it elsewhere meanwhile
    ('1', {'window': '@1', 'publish': 'started'}, {'@ccm_project': 'other'}),
    # never published by ccm, yet released
    ('0', {'window': '@1'}, {}),
    # never published by ccm, yet registered
    ('1', None, {'@ccm_project': 'alpha'}),
])
def test_outside_changes_hold_back_only_that_window(checkpoint, monkeypatch, mark, state, window):
    project = checkpoint['projects'][0]
    job, writes = _publishing(monkeypatch, project, mark, state, **window)
    with pytest.raises(restore.WindowProblem):
        restore._restore_and_publish(job, '0', project, '$1', False)
    assert not [c for c in writes.call_args_list if c.args[0] in ('set-option', 'rename-window')]


def test_estimate_leaves_out_windows_already_held_back():
    record = {'samples': 3, 'spent': 6.0, 'total': 10, 'done': 3, 'failed': {'4': 'x', '5': 'y'}}
    assert restore.remaining_seconds(record) == 10


def test_a_waiter_reports_an_incomplete_run_instead_of_retrying(checkpoint, capsys):
    restore._publish({'run': 'r1', 'source': '_autosave', 'state': 'incomplete',
                      'summary': ['Restored 2/3; 1 window(s) need attention.']})
    with pytest.raises(SystemExit) as ended:
        restore.seen_result('_autosave', {'r1'})
    assert ended.value.code == 1
    out = capsys.readouterr().out
    assert 'windows needing attention' in out
    assert 'Restored 2/3' in out


def test_dashboard_names_an_incomplete_restore(checkpoint):
    import ccm_dashboard_lifecycle as life
    restore.save_job({'id': 'j', 'source': '_autosave', 'windows': {}, 'failed': {'1': 'x'}})
    restore._publish({'run': 'r1', 'source': '_autosave', 'state': 'incomplete', 'failed': {'1': 'x'}})
    line, name = life.restore_status(lambda *a: '')
    assert line.startswith('Restore incomplete: 1 window(s) need attention')
    assert name == '_autosave'


def test_quiet_autosave_does_not_wait_for_a_restore(checkpoint, monkeypatch):
    restore.save_job({'id': 'j', 'source': '_autosave', 'windows': {}, 'failed': {}})
    monkeypatch.setattr(store, 'locked', Mock(side_effect=AssertionError('waited for the writer lock')))
    assert snapshot.cmd_snapshot_save('_autosave', quiet=True) is False


@pytest.mark.parametrize('module', sorted(
    p.stem for p in (Path(restore.__file__).parent).glob('ccm_*.py')))
def test_every_module_imports_first_in_a_fresh_interpreter(module):
    # Import order differs between entry points (a fixture may import the
    # store first); a class built at import time from a module still being
    # initialized fails only in some of those orders.
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-c', f'import {module}'], capture_output=True, text=True,
                            env={'PYTHONPATH': str(Path(restore.__file__).parent), 'PATH': '/usr/bin:/bin'})
    assert result.returncode == 0, result.stderr


def test_finish_without_confirmation_is_refused_when_not_interactive(checkpoint, monkeypatch, capsys):
    calls = []

    def finish(confirm):
        calls.append(confirm({'source': '_autosave', 'dropped': ['beta'], 'archive': '_autosave-held-back-1234abcd'}))
        return {'source': '_autosave', 'dropped': ['beta'], 'archive': '_autosave-held-back-1234abcd', 'autosave': 'present'}
    monkeypatch.setattr(restore, 'finish', finish)
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    with pytest.raises(SystemExit):
        snapshot.cmd_finish_restore([])
    assert calls == []
    out = capsys.readouterr()
    assert 'beta' in out.out and '_autosave-held-back-1234abcd' in out.out
    assert 'use -y' in out.err


def test_finish_with_nothing_incomplete_changes_nothing(checkpoint, capsys):
    before = (store.directory() / '_autosave.json').read_bytes()
    with pytest.raises(SystemExit):
        snapshot.cmd_finish_restore(['-y'])
    assert 'No incomplete restore' in capsys.readouterr().err
    assert (store.directory() / '_autosave.json').read_bytes() == before
    assert not list(store.directory().glob('*-held-back*'))


@pytest.mark.parametrize('failing', ['@ccm_project', '@ccm_dir'])
def test_release_keeps_the_job_marker_when_unregistering_fails(checkpoint, monkeypatch, failing):
    # A held-back window part way through publication: if its registration
    # cannot be removed, the marker that lets a retry find it must stay.
    project = checkpoint['projects'][0]
    w = {field: '' for field in restore.WF}
    w.update({'session_id': '$1', 'window_id': '@1', '@ccm_restore_job': 'j:0', '@ccm_restore_pending': '1',
              '@ccm_project': project['name'], '@ccm_dir': project['dir']})
    monkeypatch.setattr(restore, 'windows', lambda: [dict(w)])
    unset = []

    def run(*args):
        if args[-1] == failing:
            raise store.SnapshotError('synthetic tmux failure')
        unset.append(args[-1])
        w[args[-1]] = ''
        return ''
    monkeypatch.setattr(restore, 'run', run)
    with pytest.raises(store.SnapshotError):
        restore._release_marks({'id': 'j'}, checkpoint)
    assert '@ccm_restore_job' not in unset
    assert w['@ccm_restore_job'] == 'j:0'


def test_release_checks_the_marks_are_gone(checkpoint, monkeypatch):
    project = checkpoint['projects'][0]
    w = {field: '' for field in restore.WF}
    w.update({'session_id': '$1', 'window_id': '@1', '@ccm_restore_job': 'j:0', '@ccm_restore_pending': '1'})
    monkeypatch.setattr(restore, 'windows', lambda: [dict(w)])
    monkeypatch.setattr(restore, 'run', Mock(return_value=''))  # reports success, changes nothing
    with pytest.raises(store.SnapshotError, match='could not be removed'):
        restore._release_marks({'id': 'j'}, checkpoint)


def test_dashboard_tells_a_running_finish_from_a_stopped_one(checkpoint):
    import ccm_dashboard_lifecycle as life
    restore.save_job({'id': 'j', 'source': '_autosave', 'windows': {}, 'failed': {},
                      'finishing': {'stage': 'clean', 'dropped': ['beta'], 'archive': None}})
    assert life.restore_status(lambda *a: '')[0].startswith('Finishing the restore stopped part way')
    held, _ = _hold()
    try:
        assert life.restore_status(lambda *a: '')[0] == 'Finishing the restore …'
    finally:
        restore.os.close(held)
