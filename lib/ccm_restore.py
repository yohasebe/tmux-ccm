"""Resumable v2 restoration. Only windows carrying this job's token are edited."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import ccm_core
import ccm_layout as layout
import ccm_roles as roles
import ccm_snapshot_store as store

JOB = '.restore-state'
# Progress is a separate record so its frequent rewrites never touch the job
# a resumed load trusts. The run lock is held for the whole load and the
# kernel releases it when the process exits, so "the job file exists" can be
# told apart from "a restore is running now".
PROGRESS = '.restore-progress'
RUNNING = '.restore-running'
ETA_MIN_SAMPLES = 3
SHELL_STARTUP_TIMEOUT = 10.0
SHELL_QUIET_INTERVAL = 0.1
WF = ('session_id', 'window_id', 'window_name', '@ccm_project', '@ccm_dir',
      '@ccm_restore_job', '@ccm_restore_pending', 'window_width', 'window_height')
PF = ('window_id', 'pane_id', 'pane_current_path', 'pane_left', 'pane_top',
      'pane_width', 'pane_height', 'pane_current_command', 'pane_pid')


def run(*args):
    result = ccm_core.tmux_query(*args)
    if result is None:
        raise store.SnapshotError(f'tmux {args[0]} failed; retry this snapshot load')
    return result


def canonical(path):
    return os.path.realpath(os.path.expanduser(path))


def escaped(path):
    # -c expands tmux formats even when passed as a single argv value.
    return str(path).replace('#', '##')


def job_path():
    return store.directory() / JOB


def paused():
    return job_path().exists()


def identity(data):
    data = copy.deepcopy(data)
    data['checkpoint']['sealed'] = False
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def progress_path():
    return store.directory() / PROGRESS


def read_progress():
    try:
        record = json.loads(progress_path().read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _lock_running():
    """The run lock's fd, or None while another process holds it."""
    store.directory().mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(store.directory() / RUNNING, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as exc:
        os.close(fd)
        if isinstance(exc, BlockingIOError):
            return None
        raise
    return fd


def _acquire_running():
    """The run lock and this run's id, or (None, None) while another holds it.
    The holder writes its id into the lock file so a waiter can tell which
    run it waited for; the kernel drops the lock if the holder dies. A run
    whose id cannot be written does not start: an older id left in the
    file would let a waiter mistake an earlier result for this run's."""
    fd = _lock_running()
    if fd is None:
        return None, None
    # The fd is ours until returned: any interruption here must close it,
    # because the caller has not received it and cannot release it.
    try:
        run = uuid.uuid4().hex
        os.ftruncate(fd, 0)
        if os.pwrite(fd, run.encode(), 0) != len(run):
            raise OSError('short write')
    except BaseException as exc:
        os.close(fd)
        if isinstance(exc, OSError):
            raise store.SnapshotError(f'Cannot record the restore run ({exc}); retry this snapshot load') from exc
        raise
    return fd, run


def holder():
    """Id of the run holding the lock, '' if unreadable, None if free."""
    try:
        fd = _lock_running()
    except OSError:
        return None
    if fd is not None:
        os.close(fd)
        return None
    try:
        return (store.directory() / RUNNING).read_text().strip()
    except OSError:
        return ''


def running():
    """True while some process is inside load()."""
    return holder() is not None


def remaining_seconds(record):
    """Estimate from windows that were actually rebuilt; retained windows
    finish instantly and would make the estimate meaningless."""
    try:
        samples, spent = int(record['samples']), float(record['spent'])
        left = int(record['total']) - int(record['done'])
    except (KeyError, TypeError, ValueError):
        return None
    if samples < ETA_MIN_SAMPLES or left <= 0:
        return None
    return round(spent / samples * left)


def describe(record):
    """One progress phrase, e.g. `12/45: alpha · about 50s left`."""
    try:
        text = f"{int(record['done'])}/{int(record['total'])}"
    except (KeyError, TypeError, ValueError):
        return 'in progress'
    if record.get('current'):
        text += ': ' + roles.clean(record['current'])
    eta = remaining_seconds(record)
    if eta is not None:
        text += f' · about {eta}s left' if eta < 90 else f' · about {round(eta / 60)} min left'
    return text


def _publish(record, **changes):
    """Atomically replace the progress record. Its temporary file is not a
    `.snapshot-*` name, so a writer's cleanup can never remove it mid-write."""
    import tempfile
    record.update(changes, updated=time.time())
    try:
        fd, staged = tempfile.mkstemp(prefix=PROGRESS + '.', dir=store.directory())
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(record, f)
            os.replace(staged, progress_path())
        finally:
            Path(staged).unlink(missing_ok=True)
    except OSError:
        pass  # Progress is advisory; it must never fail the restore itself.


def _wait_for_running(name, poll=1.0):
    """Report other runs' progress until the lock is ours; never take a run
    over. Returns the lock, our run id and every run id seen holding it."""
    seen, shown = set(), None
    print('Another restore is already running; waiting for it to finish.', flush=True)
    while True:
        fd, run = _acquire_running()
        if fd is not None:
            return fd, run, seen
        current = holder()
        if current:
            seen.add(current)
        record = read_progress() or {}
        ours = bool(current) and record.get('run') == current and record.get('source') == name
        phrase = describe(record) if ours else 'in progress'
        if phrase != shown:
            print('  ' + phrase, flush=True)
            shown = phrase
        time.sleep(poll)


def save_job(job):
    store._put(job_path(), job)
    store._sync_dir()


def windows():
    return store._query('list-windows', WF)


def panes(window):
    rows = [p for p in store._query('list-panes', PF) if p['window_id'] == window]
    return {p['pane_id']: {'cwd': canonical(p['pane_current_path']),
                          'rect': [int(p[k]) for k in ('pane_left', 'pane_top', 'pane_width', 'pane_height')],
                          'command': p['pane_current_command'], 'pid': p['pane_pid']} for p in rows}


def layout_geometry(window, rows):
    # pane_height excludes a pane-border-status line and zoom changes visible
    # pane dimensions. Layout cells are the stable geometry for split sizes.
    raw = run('display-message', '-p', '-t', window, '#{window_layout}')
    rects = {'%' + str(n['id']): layout.rect(n) for n in layout.leaves(layout.parse(raw))}
    if set(rects) != set(rows):
        raise store.SnapshotError('Layout and pane inventory disagree')
    for pid in rows:
        rows[pid]['rect'] = rects[pid]
    return rows


def geometry(rows):
    return {k: {f: v[f] for f in ('cwd', 'rect')} for k, v in rows.items()}


def idle_shells(rows):
    if not rows or any(p['command'] not in ccm_core.SHELL_FOREGROUND_COMMANDS for p in rows.values()):
        return False
    # During rc, children can share the shell's foreground process group;
    # tmux may report the shell's name even while it waits for that child.
    # Background jobs in separate groups do not prevent layout restoration.
    processes = [line.split() for line in ccm_core.ps_snapshot().splitlines()]
    processes = [p for p in processes if len(p) >= 4 and p[0].isdigit()]
    by_pid = {p[0]: p for p in processes}
    for row in rows.values():
        parent = by_pid.get(row['pid'])
        if not parent or os.path.basename(parent[3]).lstrip('-') not in ccm_core.SHELL_FOREGROUND_COMMANDS:
            return False
        if any(p[1] == parent[0] and p[2] == parent[2] for p in processes):
            return False
    return True


def shell_command(target):
    """Use a validated shell executable as argv, never a shell command string."""
    default = ccm_core.tmux_query('show-option', '-qv', '-t', target, 'default-shell')
    for candidate in (default, os.environ.get('SHELL'), '/bin/sh'):
        if (isinstance(candidate, str) and os.path.isabs(candidate)
                and os.path.basename(candidate) in ccm_core.SHELL_FOREGROUND_COMMANDS
                and os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            return [candidate, '-l']
    raise store.SnapshotError('No executable login shell is available')


def stable_panes(window):
    # Login rc files can briefly put a child in the foreground. Require a
    # quiet interval at every structural step, including the final layout.
    # Never send keys, respawn, or terminate a process to obtain a shell.
    deadline = time.monotonic() + SHELL_STARTUP_TIMEOUT
    quiet_since = None
    while True:
        rows = panes(window)
        now = time.monotonic()
        if idle_shells(rows):
            if quiet_since is None:
                quiet_since = now
            if now - quiet_since >= SHELL_QUIET_INTERVAL:
                return layout_geometry(window, rows)
        else:
            quiet_since = None
        if now >= deadline:
            raise store.SnapshotError('Restore shell startup did not settle within 10 seconds; '
                                      'leave running processes intact, inspect shell rc/work, then retry')
        time.sleep(.05)


def preflight(data, session, job):
    """Check every directory and overlap before creating the first window."""
    live = windows()
    all_panes = store._query('list-panes', PF)
    target_session = run('display-message', '-p', '-t', session, '#{session_id}')
    if not target_session:
        raise store.SnapshotError('Restore session is unavailable')
    if any(w['@ccm_project'] and w['session_id'] != target_session for w in live):
        raise store.SnapshotError('Multiple managed sessions are not supported')
    ids = [w['window_id'] for w in live if w['@ccm_project'] or w['@ccm_restore_job']]
    if len(ids) != len(set(ids)):
        raise store.SnapshotError('Linked project windows are not supported')
    existing = {}
    for index, project in enumerate(data['projects']):
        if ccm_core.validate_name(project['name']) != project['name']:
            raise store.SnapshotError('Invalid project name in snapshot: ' + roles.clean(project['name']))
        paths = {canonical(project['dir'])} | {canonical(p['cwd']) for p in project['restore']['panes']}
        for path in paths:
            if not os.path.isdir(path):
                raise store.SnapshotError('Directory not found: ' + roles.clean(path))
        layout.parse(project['restore']['layout'])
        token = job['id'] + ':' + str(index)
        same = []
        for w in live:
            if w['@ccm_restore_job'] == token:
                if w['session_id'] != target_session:
                    raise store.SnapshotError('Restore window moved to another session')
                same.append(w)
                continue
            name, directory = w['@ccm_project'], w['@ccm_dir']
            if name:
                if name == project['name'] and canonical(directory) == canonical(project['dir']):
                    same.append(w)
                elif name == project['name'] or directory and canonical(directory) == canonical(project['dir']):
                    raise store.SnapshotError('Registered window conflicts with ' + roles.clean(project['name']))
            else:
                overlap = any(p['window_id'] == w['window_id'] and canonical(p['pane_current_path']) in paths for p in all_panes)
                # Other windows of this job can share cwd with a saved sidekick.
                if w['@ccm_restore_job'].startswith(job['id'] + ':'):
                    continue
                if w['window_name'] in (project['name'], roles.clean(project['name'])) or overlap:
                    raise store.SnapshotError('Unregistered window overlaps ' + roles.clean(project['name']) + '; inspect/register it explicitly')
        if len(same) > 1:
            raise store.SnapshotError('Multiple windows match ' + roles.clean(project['name']))
        if same:
            existing[str(index)] = same[0]
    return existing


def _create(session, project, index, job):
    key = str(index)
    token = job['id'] + ':' + key
    name = 'ccm-restore-' + job['id'] + '-' + key
    first = layout.leaves(layout.parse(project['restore']['layout']))[0]['id']
    cwd = next(p['cwd'] for p in project['restore']['panes'] if p['layout_id'] == first)
    # Name and marker are set in one server command queue. A lost reply can be
    # recovered by the marker; no old pane/window ID is used to claim ownership.
    result = run('new-window', '-d', '-P', '-F', '#{window_id}', '-t', session + ':',
                 '-n', name, '-c', escaped(canonical(cwd)), *job['shell'],
                 ';', 'set-option', '-w', '-t', session + ':=' + name, '@ccm_restore_job', token,
                 ';', 'set-option', '-w', '-t', session + ':=' + name, roles.PENDING_OPTION, '1',
                 ';', 'set-option', '-w', '-t', session + ':=' + name, roles.MANAGED_OPTION, '1')
    window = result.splitlines()[0] if result else ''
    match = next((w for w in windows() if w['@ccm_restore_job'] == token), None)
    if not match or match['window_id'] != window:
        raise store.SnapshotError('New window ownership could not be verified')
    return match


def _initialize(w, project, job, key):
    current = stable_panes(w['window_id'])
    if len(current) != 1:
        raise store.SnapshotError('Incomplete window has no valid progress record; inspect it before retrying')
    tree = layout.scale(layout.parse(project['restore']['layout']), int(w['window_width']), int(w['window_height']))
    cwd_by_leaf = {p['layout_id']: canonical(p['cwd']) for p in project['restore']['panes']}
    ops, leaf_keys = layout.plan(tree, cwd_by_leaf)
    first_cwd = cwd_by_leaf[layout.leaves(tree)[0]['id']]
    if next(iter(current.values()))['cwd'] != first_cwd:
        raise store.SnapshotError('New shell did not start in its saved directory')
    state = {'window': w['window_id'], 'tree': tree, 'ops': ops,
             'leaves': {str(k): v for k, v in leaf_keys.items()},
             'keys': {'0': next(iter(current))}, 'next': 0, 'expected': geometry(current), 'ready': False}
    job['windows'][key] = state
    save_job(job)
    return state


def _split(state, op, job):
    window = state['window']
    rows = stable_panes(window)
    before = state['expected']
    current = geometry(rows)
    target = state['keys'][op['key']]
    new = set(current) - set(before)
    if current == before:
        run('split-window', '-d', '-P', '-F', '#{pane_id}', '-t', target,
            '-h' if op['axis'] == '{' else '-v', '-l', str(op['size']),
            '-c', escaped(op['cwd']), *job['shell'])
        rows = stable_panes(window)
        current = geometry(rows)
        new = set(current) - set(before)
    if len(new) != 1:
        raise store.SnapshotError('Restore panes changed; refusing to split again')
    new_id = new.pop()
    expected = copy.deepcopy(before)
    expected[target]['rect'] = op['old_rect']
    expected[new_id] = {'cwd': op['cwd'], 'rect': op['new_rect']}
    if current != expected:
        raise store.SnapshotError('Split position or cwd differs from the restore plan')
    state['keys'][op['new_key']] = new_id
    state['expected'] = expected
    state['next'] += 1
    save_job(job)


def _verify(window, state):
    rows = panes(window)
    expected = state['expected']
    rows = layout_geometry(window, rows)
    if geometry(rows) != expected:
        raise store.SnapshotError('Restored pane count, position or cwd changed')
    return rows


def _verify_roles(window, project, state):
    mapping = {int(old): state['keys'][key] for old, key in state['leaves'].items()}
    for p in project['restore']['panes']:
        expected = {k: p[k] for k in ('role', 'agent', 'ignore')}
        actual = roles.decode(run('show-option', '-pqv', '-t', mapping[p['layout_id']], roles.ROLE_OPTION))
        if actual != expected:
            raise store.SnapshotError('Restored role changed; refusing to overwrite it')
    active = project['restore']['panes'][project['restore']['active_slot']]
    expected = mapping[active['layout_id']] + '\t' + ('1' if project['restore']['zoomed'] else '0')
    if run('display-message', '-p', '-t', window, '#{pane_id}\t#{window_zoomed_flag}') != expected:
        raise store.SnapshotError('Restored active pane or zoom differs from the checkpoint')


def restore_window(w, project, state, job):
    window = w['window_id']
    if state['ready']:
        _verify(window, state)
        _verify_roles(window, project, state)
        return
    while state['next'] < len(state['ops']):
        _split(state, state['ops'][state['next']], job)
    stable_panes(window)
    _verify(window, state)
    mapping = {int(old): state['keys'][key] for old, key in state['leaves'].items()}
    run('select-layout', '-t', window, layout.render(state['tree'], mapping))
    _verify(window, state)
    for p in project['restore']['panes']:
        pid = mapping[p['layout_id']]
        reservation = {k: p[k] for k in ('role', 'agent', 'ignore')}
        run('set-option', '-p', '-t', pid, roles.ROLE_OPTION, json.dumps(reservation))
        # A reservation carries ignore intent; no live @ccm_ignore is forged.
    active = project['restore']['panes'][project['restore']['active_slot']]
    run('select-pane', '-t', mapping[active['layout_id']])
    zoomed = run('display-message', '-p', '-t', window, '#{window_zoomed_flag}') == '1'
    if zoomed != project['restore']['zoomed']:
        run('resize-pane', '-Z', '-t', mapping[active['layout_id']])
    _verify(window, state)
    _verify_roles(window, project, state)
    state['ready'] = True
    save_job(job)


def _file_digest(name):
    try:
        return hashlib.sha256((store.directory() / (name + '.json')).read_bytes()).hexdigest()
    except OSError:
        return None


def load(name):
    fd, seen = None, None
    try:
        try:
            fd, run = _acquire_running()
            if fd is None:
                fd, run, seen = _wait_for_running(name)
        except store.SnapshotError as exc:
            ccm_core.ccm_die(roles.clean(exc))
        if seen_result(name, seen):
            return
        record = {'run': run, 'pid': os.getpid(), 'source': name, 'state': 'running',
                  'started': time.time(), 'total': 0, 'done': 0, 'current': '',
                  'samples': 0, 'spent': 0.0, 'file_digest': None}
        try:
            _publish(record)
            _load(name, record)
        except BaseException:
            if record['state'] == 'running':
                _publish(record, state='stopped', error='interrupted before finishing', current='')
            raise
    finally:
        if fd is not None:
            os.close(fd)


def seen_result(name, seen):
    """Report the result of a run this waiter saw holding the lock. True
    when that run finished this snapshot and nothing is left to do; an
    older record with the same source proves nothing."""
    if not seen:
        return False
    other = read_progress() or {}
    if other.get('run') not in seen or other.get('source') != name:
        return False
    if other.get('state') == 'done' and not job_path().exists():
        print('That restore finished:', flush=True)
        for line in other.get('summary') or []:
            print(roles.clean(str(line)), flush=True)
        return True
    if other.get('state') == 'stopped' and other.get('error'):
        print('That restore stopped: ' + roles.clean(str(other['error'])), flush=True)
    return False


def _load(name, record):
    total, done = 0, 0
    summary = []

    def say(line):
        line = roles.clean(line)
        summary.append(line)
        print(line, flush=True)

    try:
        with store.locked():
            # Taken under the writer lock, so it names the content actually
            # restored; a later save under the same name clears this stop.
            _publish(record, file_digest=_file_digest(name))
            data = store.read(store.directory() / (name + '.json'))
            if not data or data['version'] != 2:
                raise store.SnapshotError('Expected a v2 snapshot')
            total = len(data['projects'])
            _publish(record, total=total)
            digest = identity(data)
            if job_path().exists():
                job = json.loads(job_path().read_text())
                if job.get('source') != name or job.get('digest') != digest:
                    raise store.SnapshotError('Another restore is incomplete; retry snapshot ' + roles.clean(job.get('source', '?')))
            else:
                job = {'id': uuid.uuid4().hex, 'source': name, 'digest': digest, 'windows': {}}
            session = ccm_core.require_session()
            existing = preflight(data, session, job)
            if 'shell' not in job and any(
                    str(i) not in existing or existing[str(i)]['@ccm_restore_job'] == job['id'] + ':' + str(i)
                    for i in range(total)):
                job['shell'] = shell_command(session)
            save_job(job)
            for index, project in enumerate(data['projects']):
                key = str(index)
                _publish(record, done=done, current=project['name'])
                w = existing.get(key)
                owned = w and w['@ccm_restore_job'] == job['id'] + ':' + key
                if w and not owned:
                    print(roles.clean(f"Restored {index + 1}/{total}: {project['name']} (matching registered window retained)"), flush=True)
                    done += 1
                    continue
                started = time.monotonic()
                created = not w
                if not w:
                    # Repeat overlap checks so an external creator between
                    # preflight and this iteration is never silently adopted.
                    fresh = preflight(data, session, job)
                    if key in fresh:
                        raise store.SnapshotError('A window appeared during restore; retry')
                    w = _create(session, project, index, job)
                state = job['windows'].get(key)
                if created or not state or state['window'] != w['window_id']:
                    state = _initialize(w, project, job, key)
                restore_window(w, project, state, job)
                done += 1
                record['samples'] += 1
                record['spent'] += time.monotonic() - started
                _publish(record, done=done, current='')
                eta = remaining_seconds(record)
                left = f' · about {eta}s left' if eta is not None else ''
                print(roles.clean(f"Restored {done}/{total}: {project['name']}{left}"), flush=True)
            # Publish only verified windows. Pending remains set until every
            # new window is ready; the durable job pauses all snapshot writers.
            for key, state in job['windows'].items():
                p = data['projects'][int(key)]
                window = state['window']
                run('set-option', '-w', '-t', window, '@ccm_project', p['name'])
                run('set-option', '-w', '-t', window, '@ccm_dir', canonical(p['dir']))
                run('rename-window', '-t', window, roles.clean(p['name']))
                run('set-option', '-w', '-t', window, 'automatic-rename', 'off')
            for state in job['windows'].values():
                run('set-option', '-w', '-t', state['window'], roles.PENDING_OPTION, '0')
            was_sealed = store.sealed(data)
            if was_sealed:
                unsealed = copy.deepcopy(data)
                unsealed['checkpoint']['sealed'] = False
                store.write(name, unsealed, backup=False)
            # No post-load recapture: it could replace the checkpoint with a
            # subset or drop reservations before the normal collector sees them.
            try:
                job_path().unlink()
                store._sync_dir()
            except OSError:
                ccm_core.ccm_warn('Restore completed; progress cleanup failed. Retry this snapshot load to release autosave.')
            say(f'Restored {total}/{total}; snapshot protection released. ccm did not launch agents; '
                'Opening a project starts Claude only in an eligible pane; inspect ccm roles.')
            # Only what needs a hand: existing windows left alone and
            # sidekicks to resume. Everything else is `ccm roles`.
            for index, p in enumerate(data['projects']):
                if str(index) not in job['windows']:
                    say(p['name'] + ': existing layout and roles retained')
                    continue
                if p['restore']['primary_claude_slot'] is None:
                    say(p['name'] + ': no primary reservation; opening the window may start '
                        'Claude in an eligible shell using the normal selection rules')
                for pane in p['restore']['panes']:
                    if pane['role'] in ('sidekick', 'manual'):
                        say(p['name'] + ': ' + roles.hint(pane))
            if was_sealed and data['checkpoint']['interrupted']:
                say('Interrupted at save: these states are not restored. Review these projects:')
                for p in data['checkpoint']['interrupted']:
                    say(f"  {p['name']}: {p['state']}")
            _publish(record, state='done', done=total, current='', summary=summary)
    except (store.SnapshotError, OSError, ValueError, KeyError) as exc:
        reason = roles.clean(exc)
        _publish(record, state='stopped', done=done, current='', error=reason)
        ccm_core.ccm_die(f'Restored {done} / incomplete {max(0, total - done)}: {reason}. Checkpoint retained; retry the same snapshot.')
