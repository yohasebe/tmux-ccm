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
    if mode == 'performance':
        projects = []
        for i in range(46):
            p = copy.deepcopy(project)
            p['name'] = f'project-{i:02d}'
            path = base / p['name']
            path.mkdir()
            p['dir'] = str(path)
            for pane in p['restore']['panes']:
                pane['cwd'] = str(path)
            if i >= 4:
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
    start = time.monotonic()
    ccm_restore.load('_autosave')
    elapsed = time.monotonic() - start
    all_windows = ccm_restore.windows()
    managed = [w for w in all_windows if w['@ccm_project']]
    assert len(managed) == len(data['projects'])
    assert tmux('display-message', '-p', '-t', 'test', '#{window_index}') == '0'
    assert not marker.exists()
    assert not store.sealed(store.read(store.directory() / '_autosave.json'))
    assert not ccm_restore.paused()
    for w, p in zip(managed, data['projects']):
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
    if mode != 'performance':
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
