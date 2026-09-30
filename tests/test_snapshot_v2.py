"""Checkpoint protection, torn-write recovery and fresh tmux capture."""
import copy
import errno
import json
import os
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

import ccm_commands
import ccm_core
import ccm_runtime
import ccm_snapshot as snapshot
import ccm_snapshot_store as store
from snapshot_fixture import inventory_query, layout


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'snapshots'
    monkeypatch.setattr(ccm_core, 'CCM_SNAPSHOT_DIR', str(root))
    monkeypatch.setattr(ccm_core, 'CCM_TMP_DIR', str(tmp_path))
    monkeypatch.setenv('CCM_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('CCM_SNAPSHOT_DIR', str(root))
    monkeypatch.setattr(ccm_core, 'tmux_query', inventory_query('1\twin\talpha\t/tmp/alpha'))
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '1 0 1 zsh 00:10')
    monkeypatch.setattr(store, '_observed_state', lambda *a: 'IDLE')
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    return root


def contents(root):
    return tuple((root / n).read_bytes() if (root / n).exists() else None
                 for n in ('_autosave.json', '_autosave.prev'))


def seed(root):
    snapshot.cmd_snapshot_save('_autosave', quiet=True)
    data = store.read(root / '_autosave.json')
    data['projects'][0]['name'] = 'beta'
    with store.locked():
        store.write('_autosave', data)
    return contents(root)


def test_v2_and_unknown_version_refused_before_tmux(env, monkeypatch):
    snapshot.cmd_snapshot_save('_autosave', quiet=True)
    data = store.read(env / '_autosave.json')
    assert data['version'] == 2
    data['version'] = 99
    (env / 'future.json').write_text(json.dumps(data))
    session = Mock(side_effect=AssertionError('must not mutate or connect'))
    monkeypatch.setattr(ccm_core, 'require_session', session)
    with pytest.raises(SystemExit):
        snapshot.cmd_snapshot_load('future')
    session.assert_not_called()


def test_same_content_preserves_files_and_permissions(env):
    snapshot.cmd_snapshot_save('_autosave', quiet=True)
    stat = (env / '_autosave.json').stat()
    assert snapshot.cmd_snapshot_save('_autosave', quiet=True) is False
    assert (env / '_autosave.json').stat().st_mtime_ns == stat.st_mtime_ns
    assert not (env / '_autosave.prev').exists()
    assert stat.st_mode & 0o777 == 0o600
    assert env.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('answer', ['N', '', '\x1b', 'yes'])
def test_decline_keeps_both_files(env, monkeypatch, answer):
    before = seed(env)
    monkeypatch.setattr(store, '_observed_state', lambda *a: 'PERMIT')
    monkeypatch.setattr(snapshot, '_confirmation', lambda: answer)
    with pytest.raises(SystemExit):
        snapshot.cmd_prepare_logout([])
    assert contents(env) == before


@pytest.mark.parametrize('failure', [EOFError, KeyboardInterrupt])
def test_interrupted_prompt_keeps_checkpoint(env, monkeypatch, failure):
    before = seed(env)
    monkeypatch.setattr(store, '_observed_state', lambda *a: 'BUSY')
    def interrupt():
        raise failure()
    monkeypatch.setattr(snapshot, '_confirmation', interrupt)
    with pytest.raises(SystemExit):
        snapshot.cmd_prepare_logout([])
    assert contents(env) == before


@pytest.mark.parametrize('state', ['PERMIT', 'BUSY'])
@pytest.mark.parametrize('mode', ['y', 'flag', 'noninteractive'])
def test_confirmation_records_only_name_and_state(env, monkeypatch, capsys, state, mode):
    before = seed(env)
    monkeypatch.setattr(store, '_observed_state', lambda *a: state)
    monkeypatch.setattr(snapshot, '_confirmation', lambda: 'y')
    monkeypatch.setattr('sys.stdin.isatty', lambda: mode == 'y')
    if mode == 'noninteractive':
        with pytest.raises(SystemExit):
            snapshot.cmd_prepare_logout([])
        assert contents(env) == before
        return
    snapshot.cmd_prepare_logout(['-y'] if mode == 'flag' else [])
    data = store.read(env / '_autosave.json')
    assert data['checkpoint']['sealed'] is True
    assert data['checkpoint']['interrupted'] == [{'name': 'alpha', 'state': state}]
    assert f'alpha: {state}' in capsys.readouterr().out
    assert any(f'alpha ({state})' in row[1] for row in snapshot.snapshot_diagnostics())


def test_change_during_confirmation_is_not_saved(env, monkeypatch):
    before = seed(env)
    monkeypatch.setattr(store, '_observed_state', lambda *a: 'BUSY')
    def answer():
        monkeypatch.setattr(ccm_core, 'tmux_query', inventory_query('1\tw\tgamma\t/tmp/gamma'))
        return 'y'
    monkeypatch.setattr(snapshot, '_confirmation', answer)
    with pytest.raises(SystemExit):
        snapshot.cmd_prepare_logout([])
    assert contents(env) == before


@pytest.mark.parametrize('source', ['periodic', 'lifecycle', 'auto-exit', 'stop', 'load', 'manual'])
def test_every_save_path_respects_seal(env, monkeypatch, source):
    snapshot.cmd_prepare_logout([])
    before = contents(env)
    monkeypatch.setattr(ccm_core, 'tmux_query', Mock(side_effect=AssertionError('sealed must skip capture')))
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a, **k: 'alpha')
    if source == 'periodic':
        ccm_runtime.periodic_autosave()
    elif source == 'lifecycle':
        ccm_commands._autosave_trigger()
    elif source == 'auto-exit':
        ccm_runtime._force_autosave()
    elif source == 'manual':
        snapshot.cmd_snapshot_save('_autosave')
    elif source == 'load':
        monkeypatch.setattr(ccm_core, 'require_session', lambda: 'main')
        monkeypatch.setattr(ccm_commands, 'cmd_add', Mock())
        monkeypatch.setattr(ccm_core, 'project_exists', lambda *a: True)
        snapshot.cmd_snapshot_load('_autosave')
    else:
        monkeypatch.setattr(ccm_core, 'get_session', lambda: 'main')
        monkeypatch.setattr(ccm_core, 'init_dirs', lambda: None)
        monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a, **k: '1\tw\talpha\t/tmp/alpha' if a[0] == 'list-windows' else '')
        ccm_commands.cmd_stop('--all')
    assert contents(env) == before


def test_cancel_retains_checkpoint_and_previous(env):
    seed(env)
    snapshot.cmd_prepare_logout([])
    before = store.read(env / '_autosave.json')
    previous = (env / '_autosave.prev').read_bytes()
    snapshot.cmd_prepare_logout(['--cancel'])
    after = store.read(env / '_autosave.json')
    before['checkpoint']['sealed'] = False
    assert after == before
    assert (env / '_autosave.prev').read_bytes() == previous
    assert not store.sealed(after)


def test_sealed_delete_refused_and_previous_hidden(env, capsys):
    seed(env)
    snapshot.cmd_prepare_logout([])
    before = contents(env)
    with pytest.raises(SystemExit):
        snapshot.cmd_snapshot_delete('_autosave')
    snapshot.cmd_snapshot_list()
    assert '_autosave.prev' not in capsys.readouterr().out
    assert contents(env) == before


@pytest.mark.parametrize('bad', ['empty', 'query-failed', 'multi-session', 'partial', 'split', 'ps-failed'])
def test_incomplete_capture_keeps_checkpoint_and_backup(env, monkeypatch, bad):
    before = seed(env)
    query = inventory_query('1\tw\talpha\t/tmp/alpha')
    calls = [0]
    def changed(command, *args, **kwargs):
        calls[0] += 1
        raw = query(command)
        if bad == 'empty':
            return ''
        if bad == 'query-failed':
            return None
        if bad == 'partial':
            return raw[:-4]
        if bad == 'multi-session' and command == 'list-windows':
            return raw + '\n' + raw.replace('$1\t@1', '$2\t@2')
        if bad == 'split' and calls[0] >= 3:
            return raw.replace('80x24', '81x24')
        return raw
    monkeypatch.setattr(ccm_core, 'tmux_query', changed)
    if bad == 'ps-failed':
        monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '')
    if bad == 'empty':
        assert snapshot.cmd_snapshot_save('_autosave', quiet=True) is False
    else:
        with pytest.raises((store.SnapshotError, ValueError)):
            snapshot.cmd_snapshot_save('_autosave', quiet=True)
    assert contents(env) == before


def test_split_roles_cwd_zoom_and_ambiguous_main(env, monkeypatch):
    split = layout('120x40,0,0{59x40,0,0,1,60x40,60,0,2}')
    def query(command, *a, **k):
        if command == 'list-windows':
            return f'$1\t@1\t3\talpha\t/tmp/alpha\t{split}\t120\t40\t1\t2\tIDLE\tEND'
        return ('@1\t%1\t0\t11\tzsh\t/tmp/alpha\t\t0\t40\tEND\n'
                '@1\t%2\t1\t12\tcodex\t/tmp/sidekick\t1\t1\t40\tEND')
    monkeypatch.setattr(ccm_core, 'tmux_query', query)
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '21 11 11 claude 00:10\n12 0 12 codex 00:10')
    data = store.collect('_autosave')
    r = data['projects'][0]['restore']
    assert r['zoomed'] and r['active_slot'] == 1 and r['primary_claude_slot'] == 0
    assert r['panes'][1] == {'slot': 1, 'layout_id': 2, 'role': 'sidekick', 'agent': 'codex', 'ignore': True, 'cwd': '/tmp/sidekick'}
    # Explicit ignore intent survives the agent exiting into a shell.
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *a, **k: query(*a, **k).replace('codex', 'zsh'))
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '11 0 11 zsh 00:10\n12 0 12 zsh 00:10')
    r = store.collect('_autosave')['projects'][0]['restore']
    assert r['primary_claude_slot'] is None
    assert r['panes'][1]['role'] == 'sidekick' and r['panes'][1]['agent'] is None


@pytest.mark.parametrize('point', ['write', 'fsync', 'rename-before', 'rename-after', 'backup', 'final-fsync'])
def test_io_failure_rolls_back_both_files(env, monkeypatch, point):
    before = seed(env)
    real_stage, real_sync, real_replace = store._stage, store._sync_dir, store.os.replace
    fired = []
    def fail_once():
        if not fired:
            fired.append(True)
            raise OSError(errno.ENOSPC, 'synthetic disk full')
    def stage(data):
        if point == 'write':
            fail_once()
        return real_stage(data)
    def replace(src, dst):
        if Path(dst).name == '_autosave.json':
            if point == 'rename-before':
                fail_once()
            real_replace(src, dst)
            if point == 'rename-after':
                fail_once()
        elif Path(dst).name == '_autosave.prev' and point == 'backup':
            fail_once()
            real_replace(src, dst)
        else:
            real_replace(src, dst)
    sync_calls = [0]
    def sync():
        sync_calls[0] += 1
        if (point == 'fsync' and sync_calls[0] == 2 or
                point == 'final-fsync' and sync_calls[0] == 4):
            fail_once()
        real_sync()
    monkeypatch.setattr(store, '_stage', stage)
    monkeypatch.setattr(store.os, 'replace', replace)
    monkeypatch.setattr(store, '_sync_dir', sync)
    with pytest.raises(OSError):
        snapshot.cmd_snapshot_save('_autosave', quiet=True)
    assert fired
    assert contents(env) == before


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize('target', ['_autosave.json', '_autosave.prev'])
@pytest.mark.parametrize('after', [False, True])
def test_process_stop_recovers_previous_files(env, monkeypatch, target, after):
    before = seed(env)
    replace = store.os.replace
    def crash(src, dst):
        if Path(dst).name == target:
            if after:
                replace(src, dst)
            raise SimulatedCrash()
        replace(src, dst)
    monkeypatch.setattr(store.os, 'replace', crash)
    with pytest.raises(SimulatedCrash):
        snapshot.cmd_snapshot_save('_autosave', quiet=True)
    assert (env / '.snapshot-transaction').exists()
    monkeypatch.setattr(store.os, 'replace', replace)
    with store.locked():
        assert contents(env) == before
    assert not (env / '.snapshot-transaction').exists()
    assert [p.name for p in env.glob('.snapshot-*')] == ['.snapshot-lock']


def test_periodic_waiting_for_prepare_does_not_collect_old_state(env, monkeypatch):
    seed(env)
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_collect = store.collect
    original_flock = store.fcntl.flock
    def collect(name, sealed=False):
        assert threading.current_thread().name == 'prepare'
        entered.set()
        assert release.wait(5), 'capture gate timed out'
        return original_collect(name, sealed)
    def flock(fd, operation):
        if threading.current_thread().name == 'periodic':
            waiting.set()
        return original_flock(fd, operation)
    monkeypatch.setattr(store, 'collect', collect)
    monkeypatch.setattr(store.fcntl, 'flock', flock)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a, **k: 'alpha')
    def run(fn):
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)
    prepare = threading.Thread(name='prepare', target=run, args=(lambda: snapshot.cmd_prepare_logout([]),))
    periodic = threading.Thread(name='periodic', target=run, args=(ccm_runtime.periodic_autosave,))
    try:
        prepare.start()
        assert entered.wait(5)
        periodic.start()
        assert waiting.wait(5)
    finally:
        release.set()
        prepare.join(5)
        if periodic.ident:
            periodic.join(5)
    assert not prepare.is_alive() and not periodic.is_alive()
    assert not errors
    assert store.sealed(store.read(env / '_autosave.json'))


def test_v1_implementation_ignores_v2_and_overwrites_protection(env, tmp_path, monkeypatch):
    import runpy
    # Frozen pre-v2 implementation, not a model of its behavior.
    legacy = runpy.run_path(str(Path(__file__).parent / 'fixtures/snapshot/v1-implementation.txt'))
    snapshot.cmd_prepare_logout([])
    v2 = store.read(env / '_autosave.json')
    v2['projects'][0]['dir'] = str(tmp_path)
    (env / '_autosave.json').write_text(json.dumps(v2))
    monkeypatch.setattr(ccm_core, 'require_session', lambda: 'main')
    monkeypatch.setattr(ccm_core, 'project_exists', lambda *a: False)
    added = Mock()
    monkeypatch.setattr(ccm_commands, 'cmd_add', added)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a, **k: f'1\tw\talpha\t{tmp_path}')
    legacy['cmd_snapshot_load']('_autosave')
    added.assert_called_once_with(str(tmp_path), 'alpha', start_claude=False, _loading=True)
    downgraded = json.loads((env / '_autosave.json').read_text())
    assert downgraded['version'] == 1
    assert 'checkpoint' not in downgraded
    assert 'restore' not in downgraded['projects'][0]


@pytest.mark.parametrize('mutation', ['missing-restore', 'bad-slot', 'bad-layout', 'incomplete', 'bad-interrupted'])
def test_invalid_v2_load_never_creates_windows(env, monkeypatch, mutation):
    snapshot.cmd_prepare_logout([])
    data = store.read(env / '_autosave.json')
    if mutation == 'missing-restore':
        del data['projects'][0]['restore']
    elif mutation == 'bad-slot':
        data['projects'][0]['restore']['active_slot'] = 7
    elif mutation == 'bad-layout':
        data['projects'][0]['restore']['layout'] = layout('80x24,0,0,7')
    elif mutation == 'incomplete':
        data['checkpoint']['complete'] = False
    else:
        data['checkpoint']['interrupted'] = [{'name': 'alpha', 'state': 'PERMIT', 'command': 'example'}]
    (env / 'bad.json').write_text(json.dumps(data))
    added = Mock()
    monkeypatch.setattr(ccm_commands, 'cmd_add', added)
    with pytest.raises(SystemExit):
        snapshot.cmd_snapshot_load('bad')
    added.assert_not_called()


def test_file_fsync_failure_preserves_both_files(env, monkeypatch):
    before = seed(env)
    def fail(fd):
        raise OSError(errno.EIO, 'synthetic fsync failure')
    monkeypatch.setattr(store.os, 'fsync', fail)
    with pytest.raises(OSError):
        snapshot.cmd_snapshot_save('_autosave', quiet=True)
    assert contents(env) == before


def test_same_content_ignores_created_timestamp(env):
    snapshot.cmd_snapshot_save('_autosave', quiet=True)
    before = contents(env)
    data = store.read(env / '_autosave.json')
    data['created'] = '2000-01-01T00:00:00+0000'
    with store.locked():
        assert store.write('_autosave', data) is False
    assert contents(env) == before


@pytest.mark.parametrize('at_final_sync', [False, True])
def test_persistent_io_failure_still_restores_original_files(env, monkeypatch, at_final_sync):
    before = seed(env)
    replace, fsync, sync = store.os.replace, store.os.fsync, store._sync_dir
    failing = [False]
    sync_calls = [0]
    def bad_fsync(fd):
        if failing[0]:
            raise OSError(errno.EIO, 'persistent synthetic fsync failure')
        return fsync(fd)
    def bad_replace(src, dst):
        if not at_final_sync and Path(dst).name == '_autosave.prev' and not failing[0]:
            failing[0] = True
            raise OSError(errno.ENOSPC, 'persistent synthetic disk full')
        return replace(src, dst)
    def bad_sync():
        sync_calls[0] += 1
        if at_final_sync and sync_calls[0] == 4:
            failing[0] = True
        return sync()
    monkeypatch.setattr(store.os, 'fsync', bad_fsync)
    monkeypatch.setattr(store.os, 'replace', bad_replace)
    monkeypatch.setattr(store, '_sync_dir', bad_sync)
    with pytest.raises(OSError):
        snapshot.cmd_snapshot_save('_autosave', quiet=True)
    assert contents(env) == before


@pytest.mark.parametrize('keys,expected', [(b'y\r', 'y'), (b'N\r', 'N'), (b'\r', ''), (b'\x1b', ''), (b'\x04', ''), (b'\x03', '')])
def test_terminal_confirmation_restores_terminal_and_escape_needs_no_enter(monkeypatch, keys, expected):
    import io
    import pty
    import sys
    import termios
    master, slave = pty.openpty()
    ready = threading.Event()
    outcome = []
    class Output(io.StringIO):
        def write(self, text):
            if '[y/N]' in text:
                ready.set()
            return super().write(text)
    with os.fdopen(slave, 'r') as terminal:
        previous = termios.tcgetattr(slave)
        monkeypatch.setattr(sys, 'stdin', terminal)
        monkeypatch.setattr(sys, 'stdout', Output())
        def read_answer():
            try:
                outcome.append(snapshot._confirmation())
            except BaseException as exc:
                outcome.append(exc)
        reader = threading.Thread(target=read_answer)
        try:
            reader.start()
            assert ready.wait(5), 'confirmation prompt gate timed out'
            os.write(master, keys)
            reader.join(5)
            assert not reader.is_alive(), 'confirmation did not finish'
            assert outcome == [expected]
            assert termios.tcgetattr(slave) == previous
        finally:
            if reader.is_alive():
                os.write(master, b'\x1b')
                reader.join(5)
            os.close(master)


def test_dashboard_does_not_call_protected_checkpoint_newly_saved(env, monkeypatch):
    import dashboard
    snapshot.cmd_prepare_logout([])
    before = contents(env)
    screen = object()
    ui = Mock()
    ui._prompt.return_value = '_autosave'
    monkeypatch.setattr(dashboard, 'cmd_snapshot_save', snapshot.cmd_snapshot_save)
    dashboard.Dashboard._do_save(ui, screen)
    ui._show_message.assert_called_once_with(screen, 'Snapshot unchanged, empty or protected: _autosave', 1.5)
    assert contents(env) == before
