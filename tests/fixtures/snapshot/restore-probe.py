"""Isolated tmux integration, launched only through the Bats socket guard."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import ccm_core
import ccm_layout
import ccm_restore
import ccm_roles
import ccm_snapshot
import ccm_snapshot_store as store

mode, base, restore_socket = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
control = base / 'control'
control.mkdir()
root = base / 'alpha'
root.mkdir()
side = base / 'side'
side.mkdir()

def tmux(*args):
    return subprocess.check_output(['tmux', *args], text=True).strip()

def new_server(width, height):
    tmux('new-session', '-d', '-x', str(width), '-y', str(height), '-s', 'test', '-c', str(control), '/bin/sh')
    tmux('set-option', '-t', 'test', 'default-shell', '/bin/sh')

try:
    new_server(160, 50)
    window = tmux('new-window', '-d', '-P', '-F', '#{window_id}', '-t', 'test:', '-c', str(root), '/bin/sh')
    tmux('set-option', '-w', '-t', window, '@ccm_project', 'alpha')
    tmux('set-option', '-w', '-t', window, '@ccm_dir', str(root))
    tmux('split-window', '-h', '-t', window, '-c', str(side), '/bin/sh')
    tmux('split-window', '-v', '-t', window, '-c', str(root), '/bin/sh')
    tmux('resize-pane', '-Z', '-t', window)
    data = store.collect('_autosave')
    project = data['projects'][0]
    r = project['restore']
    old_ids = {p['layout_id'] for p in r['panes']}
    r['panes'][0].update(role='primary', agent='claude')
    r['panes'][1].update(role='sidekick', agent='codex', ignore=True)
    r['primary_claude_slot'] = 0
    if mode == 'manual':
        r['panes'][0].update(role='manual', agent=None)
        r['primary_claude_slot'] = None
    data['checkpoint']['sealed'] = True
    data['checkpoint']['interrupted'] = [{'name': 'alpha', 'state': 'PERMIT'}]
    small = ('stop-after-create', 'ready-changed', 'moved', 'linked', 'token-lost',
             'publish-lost', 'unlink-failed', 'missing-dir', 'started-unmarked')
    if mode in ('performance', 'concurrent') + small:
        projects = []
        for i in range({'performance': 46, 'concurrent': 12}.get(mode, 3)):
            p = copy.deepcopy(project)
            p['name'] = f'project-{i:02d}'
            path = base / p['name']
            path.mkdir()
            p['dir'] = str(path)
            for pane in p['restore']['panes']:
                pane['cwd'] = str(path)
            if i >= (1 if mode in small else 4):
                rp = p['restore']
                rp['panes'] = [rp['panes'][0]]
                rp['active_slot'] = 0
                rp['zoomed'] = False
                tree = {'w': rp['width'], 'h': rp['height'], 'x': 0, 'y': 0, 'id': rp['panes'][0]['layout_id']}
                rp['layout'] = ccm_layout.render(tree, {tree['id']: '%' + str(tree['id'])})
            projects.append(p)
        data['projects'] = projects
        data['checkpoint']['interrupted'] = []
    with store.locked():
        store.write('_autosave', data)
    original = (store.directory() / '_autosave.json').read_bytes()
    tmux('kill-server')
    # kill-server can return while the old server still accepts connections.
    # A separately allocated socket gives restoration a fresh server and IDs.
    os.environ['CCM_TEST_SOCKET'] = restore_socket
    new_server(100, 30)
    tmux('set-option', '-g', 'pane-base-index', '7')
    tmux('split-window', '-h', '-t', 'test:0', '-c', str(control), '/bin/sh')
    if mode == 'borders':
        tmux('set-option', '-g', 'pane-border-status', 'top')
    # default-command must be bypassed by every restored window and split.
    marker = base / 'unexpected-default-command'
    tmux('set-option', '-g', 'default-command', 'touch ' + str(marker))
    ccm_core.require_session = lambda: 'test'
    if mode in ('login-shell', 'rc-work'):
        # A recognized synthetic shell records login argv, runs transient rc
        # work, then enters a real shell. It never reads the user's rc files.
        import shlex
        shell = base / 'shells' / 'bash'
        shell.parent.mkdir()
        stamp = base / 'shell-starts'
        rc = base / 'synthetic-bashrc'
        finished = base / 'rc-finished'
        if mode == 'rc-work':
            rc.write_text('exec /bin/sleep 30\n')
        else:
            rc.write_text('/bin/sleep 0.8\nprintf "done\\n" >> ' + shlex.quote(str(finished)) + '\n')
        shell.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + shlex.quote(str(stamp))
                         + '\nexec /bin/bash --noprofile --rcfile ' + shlex.quote(str(rc)) + ' -i\n')
        shell.chmod(0o700)
        tmux('set-option', '-t', 'test', 'default-shell', str(shell))
        if mode == 'rc-work':
            ccm_restore.SHELL_STARTUP_TIMEOUT = 1.0
            for attempt in range(2):
                try:
                    ccm_restore.load('_autosave')
                    raise AssertionError('Persistent rc work must pause restoration')
                except SystemExit as exc:
                    assert exc.code != 0
                owned = [w for w in ccm_restore.windows() if w['@ccm_restore_job']]
                assert len(owned) == 1
                wid = owned[0]['window_id']
                live = tmux('list-panes', '-t', wid, '-F', '#{pane_id}:#{pane_pid}')
                if attempt:
                    assert live == first_live, (live, first_live)
                first_live = live
                assert len(ccm_restore.panes(wid)) == 1
                assert next(iter(ccm_restore.panes(wid).values()))['command'] == 'sleep'
                assert stamp.read_text().splitlines() == ['-l']
                assert (store.directory() / '_autosave.json').read_bytes() == original
                assert ccm_restore.paused()
                assert not marker.exists()
                progress = ccm_restore.read_progress()
                assert progress['state'] == 'incomplete' and 'did not settle' in progress['failed']['0'], progress
                assert not ccm_restore.running()
            print(json.dumps({'mode': mode, 'preserved_on_retries': 2, 'shell_starts': 1}))
            sys.exit(0)
    if mode.startswith('retry'):
        original_run = ccm_restore.run
        fired = [False]
        def lost(*args):
            result = original_run(*args)
            if args[0] == {'retry': 'split-window', 'retry-new': 'new-window', 'retry-zoom': 'resize-pane'}[mode] and not fired[0]:
                fired[0] = True
                raise store.SnapshotError('synthetic lost split reply')
            return result
        ccm_restore.run = lost
        try:
            ccm_restore.load('_autosave')
            raise AssertionError('Expected incomplete restoration')
        except SystemExit as exc:
            assert exc.code != 0
        assert (store.directory() / '_autosave.json').read_bytes() == original
        assert ccm_restore.paused()
        assert ccm_snapshot.cmd_snapshot_save('_autosave', quiet=True) is False
        ccm_restore.run = original_run
    if mode == 'stop-after-create':
        # Every window is created before any is initialized; a stop after
        # the first window was published leaves the rest created but
        # uninitialized, all marked, and a retry resumes them without
        # duplicates.
        original_initialize = ccm_restore._initialize
        def stop_second(*args):
            if len(json.loads(ccm_restore.job_path().read_text())['windows']) == 1:
                raise store.SnapshotError('synthetic stop before initializing')
            return original_initialize(*args)
        ccm_restore._initialize = stop_second
        try:
            ccm_restore.load('_autosave')
            raise AssertionError('Expected incomplete restoration')
        except SystemExit as exc:
            assert exc.code != 0
        ccm_restore._initialize = original_initialize
        owned = [w for w in ccm_restore.windows() if w['@ccm_restore_job']]
        assert len(owned) == len(data['projects']), owned
        published = [w for w in owned if w['@ccm_project']]
        assert [w['@ccm_project'] for w in published] == [data['projects'][0]['name']], owned
        assert all(w['@ccm_restore_pending'] == '1' for w in owned if not w['@ccm_project']), owned
        assert (store.directory() / '_autosave.json').read_bytes() == original
        assert ccm_restore.paused()
    skip_checks = set()
    if mode in ('moved', 'linked', 'token-lost', 'missing-dir'):
        # One window's problem keeps only that window back: the others are
        # published and usable, the checkpoint stays protected and autosave
        # paused, and nothing is re-marked or repaired.
        bad = {'moved': '0', 'linked': '0', 'token-lost': '1', 'missing-dir': '1'}[mode]
        original_create, original_window = ccm_restore._create, ccm_restore.restore_window
        def after_last_create(session, project, index, job):
            result = original_create(session, project, index, job)
            if index == len(data['projects']) - 1 and mode in ('moved', 'linked'):
                first = next(w for w in ccm_restore.windows() if w['@ccm_restore_job'] == job['id'] + ':0')
                tmux('new-session', '-d', '-s', 'other', '-c', str(control), '/bin/sh')
                tmux('move-window' if mode == 'moved' else 'link-window', '-s', first['window_id'], '-t', 'other:')
            return result
        def during_restore(w, project, state, job, **kwargs):
            result = original_window(w, project, state, job, **kwargs)
            if mode == 'token-lost' and project['name'] == data['projects'][0]['name']:
                later = next(x for x in ccm_restore.windows() if x['@ccm_restore_job'] == job['id'] + ':1')
                tmux('set-option', '-wu', '-t', later['window_id'], '@ccm_restore_job')
            return result
        away = None
        if mode == 'missing-dir':
            gone = Path(data['projects'][1]['dir'])
            away = gone.with_name(gone.name + '-away')
            gone.rename(away)
        ccm_restore._create, ccm_restore.restore_window = after_last_create, during_restore
        try:
            ccm_restore.load('_autosave')
            raise AssertionError('A window with a problem must leave the restore incomplete')
        except SystemExit as exc:
            assert exc.code != 0
        ccm_restore._create, ccm_restore.restore_window = original_create, original_window
        progress = ccm_restore.read_progress()
        assert progress['state'] == 'incomplete', progress
        assert list(progress['failed']) == [bad], progress
        expected = {'moved': 'another session', 'linked': 'Linked',
                    'token-lost': 'lost its marker', 'missing-dir': 'Directory not found'}[mode]
        assert expected in progress['failed'][bad], progress
        published = [w for w in ccm_restore.windows() if w['@ccm_project']]
        assert sorted(w['@ccm_project'] for w in published) == sorted(
            p['name'] for i, p in enumerate(data['projects']) if str(i) != bad), published
        assert all(w['@ccm_restore_pending'] == '0' for w in published), published
        assert (store.directory() / '_autosave.json').read_bytes() == original
        assert ccm_restore.paused()
        assert ccm_snapshot.cmd_snapshot_save('_autosave', quiet=True) is False
        if mode == 'token-lost':
            job = json.loads(ccm_restore.job_path().read_text())
            assert not [w for w in ccm_restore.windows() if w['@ccm_restore_job'] == job['id'] + ':1']
        if mode != 'missing-dir':
            print(json.dumps({'mode': mode, 'published': len(published), 'failed': bad}))
            sys.exit(0)
        # Once the cause is fixed, the same restore finishes the rest.
        away.rename(gone)
    if mode == 'started-unmarked':
        # Publication of the first window stops after its project and
        # directory are set, then the window loses its marker. A retry does
        # not take it for a matching registered window: it is held back,
        # not re-marked, and the others are published.
        original_run = ccm_restore.run
        fired = [False]
        def lost_after_dir(*args):
            result = original_run(*args)
            if args[:2] == ('set-option', '-w') and '@ccm_dir' in args and not fired[0]:
                fired[0] = True
                raise store.SnapshotError('synthetic lost reply after setting the directory')
            return result
        ccm_restore.run = lost_after_dir
        try:
            ccm_restore.load('_autosave')
            raise AssertionError('Expected a stop during publication')
        except SystemExit as exc:
            assert exc.code != 0
        ccm_restore.run = original_run
        job = json.loads(ccm_restore.job_path().read_text())
        assert job['windows']['0']['publish'] == 'started', job
        first = job['windows']['0']['window']
        tmux('set-option', '-wu', '-t', first, '@ccm_restore_job')
        try:
            ccm_restore.load('_autosave')
            raise AssertionError('The unmarked window must leave the restore incomplete')
        except SystemExit as exc:
            assert exc.code != 0
        progress = ccm_restore.read_progress()
        assert progress['state'] == 'incomplete' and list(progress['failed']) == ['0'], progress
        assert 'lost its marker' in progress['failed']['0'], progress
        rows = {w['window_id']: w for w in ccm_restore.windows()}
        assert rows[first]['@ccm_restore_job'] == '' and rows[first]['@ccm_restore_pending'] == '1', rows[first]
        others = [w for w in rows.values() if w['@ccm_project'] and w['window_id'] != first]
        assert sorted(w['@ccm_project'] for w in others) == [p['name'] for p in data['projects'][1:]], others
        assert all(w['@ccm_restore_pending'] == '0' for w in others), others
        assert (store.directory() / '_autosave.json').read_bytes() == original
        assert ccm_restore.paused()
        print(json.dumps({'mode': mode, 'held_back': ['0']}))
        sys.exit(0)
    if mode == 'ready-changed':
        # A window published earlier is the user's: changing it while later
        # windows restore stops nothing, and it is not changed back.
        changed = {}
        original_window = ccm_restore.restore_window
        def during_restore(w, project, state, job, **kwargs):
            result = original_window(w, project, state, job, **kwargs)
            if project['name'] == data['projects'][1]['name']:
                first = job['windows']['0']
                active = tmux('display-message', '-p', '-t', first['window'], '#{pane_id}')
                changed['pane'] = next(p for p in first['keys'].values() if p != active)
                changed['window'] = first['window']
                tmux('select-pane', '-t', changed['pane'])
            return result
        ccm_restore.restore_window = during_restore
    if mode == 'publish-lost':
        # Publication stops after ccm cleared the first window's pending
        # mark; a retry finishes it instead of calling that a change.
        original_run = ccm_restore.run
        fired = [False]
        def lost_after_release(*args):
            result = original_run(*args)
            if args[:2] == ('set-option', '-w') and args[-2:] == (ccm_roles.PENDING_OPTION, '0') and not fired[0]:
                fired[0] = True
                raise store.SnapshotError('synthetic lost reply after clearing pending')
            return result
        ccm_restore.run = lost_after_release
        try:
            ccm_restore.load('_autosave')
            raise AssertionError('Expected incomplete publication')
        except SystemExit as exc:
            assert exc.code != 0
        ccm_restore.run = original_run
        marks = sorted(w['@ccm_restore_pending'] for w in ccm_restore.windows() if w['@ccm_restore_job'])
        assert marks == ['0', '1', '1'], marks
        assert ccm_restore.paused()
        assert (store.directory() / '_autosave.json').read_bytes() == original
        # The released window is usable; what the user does with it before
        # the retry is kept, not restored or verified again.
        used = next(w for w in ccm_restore.windows() if w['@ccm_restore_pending'] == '0')
        tmux('split-window', '-d', '-t', used['window_id'], '-c', str(control), '/bin/sh')
        tmux('rename-window', '-t', used['window_id'], 'renamed-by-user')
        used_panes = len(ccm_restore.panes(used['window_id']))
    if mode == 'unlink-failed':
        # The job cannot be removed once; the retry the warning asks for
        # releases it and leaves the windows as they are.
        original_unlink = Path.unlink
        fired = [False]
        def failing_unlink(self, *args, **kwargs):
            if self.name == ccm_restore.JOB and not fired[0]:
                fired[0] = True
                raise OSError('synthetic unlink failure')
            return original_unlink(self, *args, **kwargs)
        Path.unlink = failing_unlink
        try:
            ccm_restore.load('_autosave')
        finally:
            Path.unlink = original_unlink
        assert fired[0]
        assert ccm_restore.paused()
        before = sorted((w['window_id'], w['@ccm_project']) for w in ccm_restore.windows() if w['@ccm_project'])
        assert len(before) == len(data['projects'])
        ccm_restore.load('_autosave')
        assert not ccm_restore.paused()
        assert sorted((w['window_id'], w['@ccm_project']) for w in ccm_restore.windows() if w['@ccm_project']) == before
        print(json.dumps({'mode': mode, 'released_on_retry': True}))
        sys.exit(0)
    start = time.monotonic()
    if mode == 'concurrent':
        # A second load while the first runs reports progress and the first
        # run's result; it never restores the same snapshot again.
        import threading
        import ccm_dashboard_lifecycle as life
        # Hold the first run inside its first window until the second load
        # has reported that it is waiting, so the two always overlap.
        gate = threading.Event()
        original_window = ccm_restore.restore_window

        def gated(*args, **kwargs):
            assert gate.wait(30), 'second load never reported waiting'
            return original_window(*args, **kwargs)
        ccm_restore.restore_window = gated
        first = threading.Thread(target=ccm_restore.load, args=('_autosave',))
        first.start()
        deadline = time.monotonic() + 20
        banner = ''
        while not banner.startswith('Restoring ') and time.monotonic() < deadline:
            banner, _ = life.restore_status(lambda *a: '')
            time.sleep(0.05)
        assert banner.startswith('Restoring '), banner
        second = subprocess.Popen(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, sys.argv[1]); import ccm_core, ccm_restore; '
             'ccm_core.require_session = lambda: "test"; ccm_restore.load("_autosave")',
             str(Path(ccm_restore.__file__).parent)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_line = second.stdout.readline()
        assert 'already running' in first_line, first_line
        gate.set()
        rest, errors = second.communicate(timeout=60)
        output = first_line + rest
        first.join(60)
        ccm_restore.restore_window = original_window
        assert not first.is_alive()
        assert second.returncode == 0, errors
        assert 'That restore finished:' in output, output
        assert 'Restored 12/12' in output, output
        assert ccm_restore.read_progress()['state'] == 'done'
        assert life.restore_status(lambda *a: '')[0] == ''
    else:
        ccm_restore.load('_autosave')
    elapsed = time.monotonic() - start
    if mode == 'ready-changed':
        ccm_restore.restore_window = original_window
        assert tmux('display-message', '-p', '-t', changed['window'], '#{pane_id}') == changed['pane']
        skip_checks.add(changed['window'])
    all_windows = ccm_restore.windows()
    managed = [w for w in all_windows if w['@ccm_project']]
    assert len(managed) == len(data['projects'])
    assert tmux('display-message', '-p', '-t', 'test', '#{window_index}') == '0'
    assert not marker.exists()
    assert not store.sealed(store.read(store.directory() / '_autosave.json'))
    assert not ccm_restore.paused()
    for w, p in zip(managed, data['projects']):
        if w['window_id'] in skip_checks or mode == 'publish-lost' and w['window_id'] == used['window_id']:
            continue
        rows = ccm_restore.panes(w['window_id'])
        assert len(rows) == len(p['restore']['panes'])
        assert all(r['command'] in ccm_core.SHELL_FOREGROUND_COMMANDS for r in rows.values()), rows
        assert tmux('display-message', '-p', '-t', w['window_id'], '#{window_zoomed_flag}') == ('1' if p['restore']['zoomed'] else '0')
    if mode == 'login-shell':
        assert stamp.exists(), 'Configured user shell was not started'
        assert stamp.read_text().splitlines() == ['-l'] * 3
        assert finished.read_text().splitlines() == ['done'] * 3
        assert elapsed >= 2.4, elapsed
        assert all(r['command'] == 'bash' for r in rows.values()), rows
    if mode in ('stop-after-create', 'publish-lost'):
        assert len([w for w in all_windows if w['@ccm_restore_job']]) == len(data['projects'])
    if mode == 'publish-lost':
        assert tmux('display-message', '-p', '-t', used['window_id'], '#{window_name}') == 'renamed-by-user'
        assert len(ccm_restore.panes(used['window_id'])) == used_panes
    if mode not in ('performance', 'stop-after-create', 'ready-changed', 'publish-lost'):
        ids = {int(pid[1:]) for pid in ccm_restore.panes(managed[0]['window_id'])}
        assert old_ids & ids and old_ids != ids, (old_ids, ids)
    # The immediately following normal autosave retains reserved roles.
    assert ccm_snapshot.cmd_snapshot_save('_autosave', quiet=True)
    after = store.read(store.directory() / '_autosave.json')
    assert after['projects'][0]['restore']['panes'][1]['agent'] == 'codex'
    assert after['projects'][0]['restore']['panes'][1]['ignore'] is True
    assert after['projects'][0]['restore']['primary_claude_slot'] == (None if mode == 'manual' else 0)
    if mode == 'manual':
        assert after['projects'][0]['restore']['panes'][0]['role'] == 'manual'
    if mode == 'added-pane':
        import ccm_pane_state
        wid = managed[0]['window_id']
        tmux('resize-pane', '-Z', '-t', wid)
        added = tmux('split-window', '-h', '-P', '-F', '#{pane_id}', '-t', wid,
                     '-c', str(root), '/bin/sh')
        assert tmux('show-option', '-pqv', '-t', added, ccm_roles.ROLE_OPTION) == ''
        panes = ccm_pane_state.enumerate_window_panes(wid, ccm_core.ps_snapshot().splitlines())
        choice = ccm_roles.selection(wid, panes)
        assert choice.state == ccm_roles.SelectionState.PRIMARY, choice
        assert added in choice.eligible and choice.primary != added
        tmux('set-option', '-pu', '-t', choice.primary, ccm_roles.ROLE_OPTION)
        choice = ccm_roles.selection(wid, panes)
        assert choice.state == ccm_roles.SelectionState.NO_PRIMARY, choice
        assert added in choice.eligible
    print(json.dumps({'mode': mode, 'windows': len(managed), 'elapsed_seconds': round(elapsed, 3), 'agents_started': 0}))
finally:
    subprocess.run(['tmux', 'kill-server'], capture_output=True)
