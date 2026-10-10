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
from ccm_snapshot_errors import WindowProblem

JOB = '.restore-state'
# Progress is a separate record so its frequent rewrites never touch the job
# a resumed load trusts. The run lock is held for the whole load and the
# kernel releases it when the process exits, so "the job file exists" can be
# told apart from "a restore is running now".
PROGRESS = '.restore-progress'
RUNNING = '.restore-running'
ETA_MIN_SAMPLES = 3
JOB_VERSION = 2
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
        left = int(record['total']) - int(record['done']) - len(record.get('failed') or {})
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


# How long each status-area notice stays up. Progress notices are renewed
# for every window, so each only has to outlive the slowest window.
ANNOUNCE_PROGRESS_MS = 15000
ANNOUNCE_DONE_MS = 10000
ANNOUNCE_STOPPED_MS = 30000


# A coloured badge and a dark body keep the notice distinct from the
# status bar around it; muted colours keep it from shouting. `none` first,
# so attributes from the user's message-style (reverse, underscore) do not
# carry over and change these colours.
_NOTICE_BADGES = {
    'progress': '#[none,bg=colour67,fg=colour255,bold] \u27f3 ccm restore ',
    'done': '#[none,bg=colour65,fg=colour255,bold] \u2714 ccm restore ',
    'stopped': '#[none,bg=colour131,fg=colour255,bold] \u2716 ccm restore ',
    'incomplete': '#[none,bg=colour136,fg=colour255,bold] \u26a0 ccm restore ',
}
_NOTICE_BODY = '#[none,bg=colour237,fg=colour250] '


def _announce(kind, body, duration_ms, session=None):
    """Show `body` after a `kind` badge in the status line of every client attached to the
    restoring session (the default client when the session is not known
    yet). A restore at tmux start has no terminal of its own, so this is
    how anyone sees it.

    `-C` keeps panes updating while the message is up; tmux without it
    (before 3.6) rejects the command, and nothing is shown rather than
    freezing panes. With a session, only its clients are told: when none
    is attached, or they cannot be listed, nothing is shown rather than
    telling whoever else is attached. Names and reasons are cleaned (`#` cannot start a
    format, so the body cannot restyle itself) and `%` is doubled so it
    is not read as a time format.
    Failures, such as no client attached yet, are ignored."""
    message = (_NOTICE_BADGES[kind] + _NOTICE_BODY
               + roles.clean(body).replace('%', '%%') + ' #[default]')
    try:
        clients = [None]
        if session:
            listing = ccm_core.tmux_query('list-clients', '-t', session, '-F', '#{client_name}')
            clients = [c for c in (listing or '').splitlines() if c]
        for client in clients:
            target = ['-c', client] if client else []
            ccm_core.tmux_query('display-message', '-C', '-d', str(duration_ms), *target, message)
    except Exception:
        pass


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


def released(w, project):
    """True when ccm finished publishing this window: pending cleared and
    the project's name and directory in place. Only meaningful while the
    job records that publication began."""
    return (w['@ccm_restore_pending'] == '0' and w['@ccm_project'] == project['name']
            and bool(w['@ccm_dir']) and canonical(w['@ccm_dir']) == canonical(project['dir']))


def owned_window(job, key, session_id, live):
    """The one window carrying this job's token for `key`, in the restoring
    session and linked nowhere else. Anything else stops the restore: a
    window that moved, was linked or lost its token is not adopted."""
    rows = [w for w in live if w['@ccm_restore_job'] == job['id'] + ':' + key]
    if len(rows) > 1 and len({w['window_id'] for w in rows}) == 1:
        raise WindowProblem('Linked project windows are not supported')
    if not rows:
        raise WindowProblem('Restore window closed or lost its marker')
    if len(rows) != 1:
        raise WindowProblem('Restore window ownership changed; inspect it before retrying')
    w = rows[0]
    if w['session_id'] != session_id:
        raise WindowProblem('Restore window moved to another session')
    if sum(x['window_id'] == w['window_id'] for x in live) != 1:
        raise WindowProblem('Linked project windows are not supported')
    return w


def panes(window):
    # Only this window's panes: the restore polls this while shells start,
    # and a whole-server listing grows with every window already restored.
    rows = [p for p in store._query('list-panes', PF, target=window) if p['window_id'] == window]
    return {p['pane_id']: {'cwd': canonical(p['pane_current_path']),
                          'rect': [int(p[k]) for k in ('pane_left', 'pane_top', 'pane_width', 'pane_height')],
                          'command': p['pane_current_command'], 'pid': p['pane_pid']} for p in rows}


def layout_geometry(window, rows):
    # pane_height excludes a pane-border-status line and zoom changes visible
    # pane dimensions. Layout cells are the stable geometry for split sizes.
    raw = run('display-message', '-p', '-t', window, '#{window_layout}')
    rects = {'%' + str(n['id']): layout.rect(n) for n in layout.leaves(layout.parse(raw))}
    if set(rects) != set(rows):
        raise WindowProblem('Layout and pane inventory disagree')
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
    if not processes:
        # Not observing processes is not one window's rc still running.
        raise store.SnapshotError('Cannot read the process list; retry this snapshot load')
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
            raise WindowProblem('Restore shell startup did not settle within 10 seconds; '
                                      'leave running processes intact, inspect shell rc/work, then retry')
        time.sleep(.05)


def preflight(data, session, job):
    """Check every directory and overlap before creating windows.

    Returns (existing, problems): the window already standing for each
    project key, and the reason a project cannot be restored now. Problems
    of the snapshot or the session as a whole raise; a problem of one
    project only keeps that project from being created or adopted. Every
    live window still takes part in the overlap checks of every project."""
    live = windows()
    all_panes = store._query('list-panes', PF)
    target_session = run('display-message', '-p', '-t', session, '#{session_id}')
    if not target_session:
        raise store.SnapshotError('Restore session is unavailable')
    if any(w['@ccm_project'] and w['session_id'] != target_session for w in live):
        raise store.SnapshotError('Multiple managed sessions are not supported')
    ids = [w['window_id'] for w in live if w['@ccm_project']]
    if len(ids) != len(set(ids)):
        raise store.SnapshotError('Linked project windows are not supported')
    for project in data['projects']:
        if ccm_core.validate_name(project['name']) != project['name']:
            raise store.SnapshotError('Invalid project name in snapshot: ' + roles.clean(project['name']))
        layout.parse(project['restore']['layout'])
    existing, problems = {}, {}
    for index, project in enumerate(data['projects']):
        key = str(index)
        try:
            found = _preflight_project(project, key, job, live, all_panes, target_session)
        except WindowProblem as exc:
            problems[key] = str(exc)
            continue
        if found:
            existing[key] = found
    return existing, problems


def _preflight_project(project, key, job, live, all_panes, target_session):
    token = job['id'] + ':' + key
    state = job.get('windows', {}).get(key) or {}
    paths = {canonical(project['dir'])} | {canonical(p['cwd']) for p in project['restore']['panes']}
    # A published window is the user's now; a directory removed since then
    # does not turn it back into a failure.
    if state.get('publish') not in ('started', 'done'):
        for path in paths:
            if not os.path.isdir(path):
                raise WindowProblem('Directory not found: ' + roles.clean(path))
    same = []
    for w in live:
        if w['@ccm_restore_job'] == token:
            if w['session_id'] != target_session:
                raise WindowProblem('Restore window moved to another session')
            same.append(w)
            continue
        name, directory = w['@ccm_project'], w['@ccm_dir']
        if name:
            if name == project['name'] and canonical(directory) == canonical(project['dir']):
                same.append(w)
            elif name == project['name'] or directory and canonical(directory) == canonical(project['dir']):
                raise WindowProblem('Registered window conflicts with ' + roles.clean(project['name']))
        else:
            overlap = any(p['window_id'] == w['window_id'] and canonical(p['pane_current_path']) in paths for p in all_panes)
            # Other windows of this job can share cwd with a saved sidekick.
            if w['@ccm_restore_job'].startswith(job['id'] + ':'):
                continue
            if w['window_name'] in (project['name'], roles.clean(project['name'])) or overlap:
                raise WindowProblem('Unregistered window overlaps ' + roles.clean(project['name']) + '; inspect/register it explicitly')
    if len(same) > 1:
        raise WindowProblem('Multiple windows match ' + roles.clean(project['name']))
    return same[0] if same else None


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
        raise WindowProblem('Incomplete window has no valid progress record; inspect it before retrying')
    try:
        tree = layout.scale(layout.parse(project['restore']['layout']), int(w['window_width']), int(w['window_height']))
    except store.SnapshotError as exc:
        raise WindowProblem(str(exc)) from exc
    cwd_by_leaf = {p['layout_id']: canonical(p['cwd']) for p in project['restore']['panes']}
    ops, leaf_keys = layout.plan(tree, cwd_by_leaf)
    first_cwd = cwd_by_leaf[layout.leaves(tree)[0]['id']]
    if next(iter(current.values()))['cwd'] != first_cwd:
        raise WindowProblem('New shell did not start in its saved directory')
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
        raise WindowProblem('Restore panes changed; refusing to split again')
    new_id = new.pop()
    expected = copy.deepcopy(before)
    expected[target]['rect'] = op['old_rect']
    expected[new_id] = {'cwd': op['cwd'], 'rect': op['new_rect']}
    if current != expected:
        raise WindowProblem('Split position or cwd differs from the restore plan')
    state['keys'][op['new_key']] = new_id
    state['expected'] = expected
    state['next'] += 1
    save_job(job)


def _verify(window, state):
    rows = panes(window)
    expected = state['expected']
    rows = layout_geometry(window, rows)
    if geometry(rows) != expected:
        raise WindowProblem('Restored pane count, position or cwd changed')
    return rows


def _verify_roles(window, project, state):
    mapping = {int(old): state['keys'][key] for old, key in state['leaves'].items()}
    for p in project['restore']['panes']:
        expected = {k: p[k] for k in ('role', 'agent', 'ignore')}
        actual = roles.decode(run('show-option', '-pqv', '-t', mapping[p['layout_id']], roles.ROLE_OPTION))
        if actual != expected:
            raise WindowProblem('Restored role changed; refusing to overwrite it')
    active = project['restore']['panes'][project['restore']['active_slot']]
    expected = mapping[active['layout_id']] + '\t' + ('1' if project['restore']['zoomed'] else '0')
    if run('display-message', '-p', '-t', window, '#{pane_id}\t#{window_zoomed_flag}') != expected:
        raise WindowProblem('Restored active pane or zoom differs from the checkpoint')


def restore_window(w, project, state, job, settled=False):
    """`settled`: the caller has just waited for this window's shells with
    nothing changed since, so a window without splits need not wait again."""
    window = w['window_id']
    if state['ready']:
        _verify(window, state)
        _verify_roles(window, project, state)
        return
    if state['ops'] or not settled:
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


def _upgrade_job(job):
    """Bring a job written by an earlier version to the current form. Its
    single `publishing` flag meant publication of every window had begun."""
    if job.get('version') != JOB_VERSION:
        if job.pop('publishing', False):
            for state in job.get('windows', {}).values():
                state.setdefault('publish', 'started')
        job['version'] = JOB_VERSION
    job.setdefault('windows', {})
    job.setdefault('failed', {})
    return job


def _restore_and_publish(job, key, project, session_id, created):
    """Restore one window and publish it, or finish a publication an
    earlier run began. A window is published only after its own checks;
    publication is recorded before the first write, so a stop part way
    through resumes without mistaking ccm's own writes for a change."""
    w = owned_window(job, key, session_id, windows())
    state = job['windows'].get(key)
    mark = w['@ccm_restore_pending']
    if state and state.get('publish') == 'started':
        if state['window'] != w['window_id']:
            raise WindowProblem('Restore window ownership changed; inspect it before retrying')
        if mark == '0':
            # Already usable, so possibly in use: confirm, never rewrite.
            if not released(w, project):
                raise WindowProblem('Restore window changed while being published; inspect it before retrying')
            state['publish'] = 'done'
            save_job(job)
            return
        if mark != '1':
            raise WindowProblem('Restore window pending mark changed; inspect it before retrying')
        if (w['@ccm_project'] not in ('', project['name'])
                or w['@ccm_dir'] and canonical(w['@ccm_dir']) != canonical(project['dir'])):
            raise WindowProblem('Restore window registration changed while being published; inspect it before retrying')
        # Not yet released: the structure may have changed meanwhile.
        _verify(state['window'], state)
        _verify_roles(state['window'], project, state)
        _publish_window(state, project, job)
        return
    if mark != '1':
        raise WindowProblem('Restore window was released outside this restore; inspect it before retrying')
    if w['@ccm_project']:
        raise WindowProblem('Restore window was registered outside this restore; inspect it before retrying')
    settled = created or not state or state['window'] != w['window_id']
    if settled:
        state = _initialize(w, project, job, key)
    restore_window(w, project, state, job, settled=settled)
    w = owned_window(job, key, session_id, windows())
    if w['window_id'] != state['window'] or w['@ccm_restore_pending'] != '1' or w['@ccm_project']:
        raise WindowProblem('Restore window ownership changed; inspect it before retrying')
    state['publish'] = 'started'
    save_job(job)
    _publish_window(state, project, job)


def _publish_window(state, project, job):
    window = state['window']
    run('set-option', '-w', '-t', window, '@ccm_project', project['name'])
    run('set-option', '-w', '-t', window, '@ccm_dir', canonical(project['dir']))
    run('rename-window', '-t', window, roles.clean(project['name']))
    run('set-option', '-w', '-t', window, 'automatic-rename', 'off')
    run('set-option', '-w', '-t', window, roles.PENDING_OPTION, '0')
    state['publish'] = 'done'
    save_job(job)


def _guidance(data, job, say):
    """Only what needs a hand: existing windows left alone and sidekicks
    to resume. Everything else is `ccm roles`."""
    for index, p in enumerate(data['projects']):
        key = str(index)
        if key in job['failed']:
            continue
        if key not in job['windows']:
            say(p['name'] + ': existing layout and roles retained')
            continue
        if p['restore']['primary_claude_slot'] is None:
            say(p['name'] + ': no primary reservation; opening the window may start '
                'Claude in an eligible shell using the normal selection rules')
        for pane in p['restore']['panes']:
            if pane['role'] in ('sidekick', 'manual'):
                say(p['name'] + ': ' + roles.hint(pane))


def _finish_incomplete(data, job, record, done, total, session, say, summary):
    """Every window that could be restored is published and usable; the
    rest keep the job, so the checkpoint stays protected and autosave paused
    until they are restored too."""
    failed = job['failed']
    say(f'Restored {done}/{total}; {len(failed)} window(s) need attention. '
        'The checkpoint stays protected and autosave paused until they are restored.')
    for key in sorted(failed, key=int):
        say(f"  {data['projects'][int(key)]['name']}: {failed[key]}")
    say('Fix the cause, then open Menu \u2192 Continue restore or load the same snapshot again.')
    _guidance(data, job, say)
    _publish(record, state='incomplete', done=done, current='', failed=dict(failed), summary=summary)
    _announce('incomplete', f'restored {done}/{total} \u00b7 {len(failed)} need attention \u00b7 '
              'dashboard menu: Continue restore', ANNOUNCE_STOPPED_MS, session)
    raise SystemExit(1)


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
                _announce('stopped', 'interrupted \u00b7 dashboard menu: Continue restore',
                          ANNOUNCE_STOPPED_MS, record.get('session'))
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
    if other.get('state') == 'incomplete':
        # Report that run's outcome, ending as it did; retrying is the
        # user's choice.
        print('That restore finished with windows needing attention:', flush=True)
        for line in other.get('summary') or []:
            print(roles.clean(str(line)), flush=True)
        raise SystemExit(1)
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
            job = _upgrade_job(job)
            session = ccm_core.require_session()
            record['session'] = session
            session_id = run('display-message', '-p', '-t', session, '#{session_id}')
            _announce('progress', f'restoring {total} window(s)\u2026', ANNOUNCE_PROGRESS_MS, session)
            existing, problems = preflight(data, session, job)
            if 'shell' not in job and any(
                    str(i) not in existing or existing[str(i)]['@ccm_restore_job'] == job['id'] + ':' + str(i)
                    for i in range(total)):
                job['shell'] = shell_command(session)
            failed = job['failed']
            save_job(job)

            def fail(key, reason):
                failed[key] = str(reason)
                save_job(job)
                record['failed'] = dict(failed)
                print(roles.clean(f"Needs attention: {data['projects'][int(key)]['name']}: {reason}"), flush=True)

            # Create every missing window first so the shells' login rc files
            # run side by side; each window is then initialized, checked and
            # published on its own. A window created here carries its token
            # and pending mark, so a stop before it is initialized is resumed
            # from the token like a lost new-window reply.
            created = set()
            pending = []
            for index, project in enumerate(data['projects']):
                key = str(index)
                w = existing.get(key)
                state = job['windows'].get(key) or {}
                owned = w and w['@ccm_restore_job'] == job['id'] + ':' + key
                _publish(record, done=done, current=(project['name'] if w else 'creating ' + project['name']))
                _announce('progress', describe(record), ANNOUNCE_PROGRESS_MS, session)
                if state.get('publish') == 'done':
                    # Published earlier: the window belongs to the user now,
                    # whatever became of it, and is never recreated.
                    done += 1
                    failed.pop(key, None)
                    print(roles.clean(f"Restored {done}/{total}: {project['name']} (already published)"), flush=True)
                    continue
                if key in problems:
                    fail(key, problems[key])
                    continue
                if state and not owned:
                    # This restore already began this window; only its own
                    # marked window may finish it. A window that lost the
                    # marker is not adopted, and none is created in its place.
                    fail(key, WindowProblem('Restore window closed or lost its marker; inspect it before retrying'))
                    continue
                if w and not owned:
                    done += 1
                    failed.pop(key, None)
                    print(roles.clean(f"Restored {done}/{total}: {project['name']} (matching registered window retained)"), flush=True)
                    continue
                if w:
                    pending.append(index)
                    continue
                # Repeat overlap checks so an external creator between
                # preflight and this iteration is never silently adopted.
                fresh, fresh_problems = preflight(data, session, job)
                if key in fresh_problems or key in fresh:
                    fail(key, fresh_problems.get(key) or WindowProblem('A window appeared during restore; inspect it before retrying'))
                    continue
                _create(session, project, index, job)
                created.add(key)
                pending.append(index)
            _publish(record, done=done, current='')
            for index in pending:
                project = data['projects'][index]
                key = str(index)
                _publish(record, done=done, current=project['name'])
                _announce('progress', describe(record), ANNOUNCE_PROGRESS_MS, session)
                started = time.monotonic()
                try:
                    _restore_and_publish(job, key, project, session_id, key in created)
                except WindowProblem as exc:
                    fail(key, exc)
                    continue
                except store.SnapshotError:
                    # A failed tmux command is a problem of this window only
                    # when a fresh listing shows its window gone; anything
                    # unexplained stops the whole restore.
                    if any(x['@ccm_restore_job'] == job['id'] + ':' + key for x in windows()):
                        raise
                    fail(key, WindowProblem('Restore window closed'))
                    continue
                failed.pop(key, None)
                save_job(job)
                done += 1
                record['samples'] += 1
                record['spent'] += time.monotonic() - started
                _publish(record, done=done, current='', failed=dict(failed))
                eta = remaining_seconds(record)
                left = f' · about {eta}s left' if eta is not None else ''
                print(roles.clean(f"Restored {done}/{total}: {project['name']}{left}"), flush=True)
            if failed:
                _finish_incomplete(data, job, record, done, total, session, say, summary)
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
            _guidance(data, job, say)
            if was_sealed and data['checkpoint']['interrupted']:
                say('Interrupted at save: these states are not restored. Review these projects:')
                for p in data['checkpoint']['interrupted']:
                    say(f"  {p['name']}: {p['state']}")
            _publish(record, state='done', done=total, current='', summary=summary)
            _announce('done', f'restored {total}/{total} window(s) \u00b7 opening a project starts Claude; '
                      'sidekicks are resumed by hand', ANNOUNCE_DONE_MS, session)
    except (store.SnapshotError, OSError, ValueError, KeyError) as exc:
        reason = roles.clean(exc)
        _publish(record, state='stopped', done=done, current='', error=reason)
        _announce('stopped', f'stopped at {done}/{total} \u00b7 dashboard menu: Continue restore \u00b7 {reason}',
                  ANNOUNCE_STOPPED_MS, record.get('session'))
        ccm_core.ccm_die(f'Restored {done} / incomplete {max(0, total - done)}: {reason}. Checkpoint retained; retry the same snapshot.')
