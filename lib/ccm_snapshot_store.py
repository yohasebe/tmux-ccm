"""Validated snapshot capture and serialized, recoverable file replacement.

All writers (including delete and unseal) share one flock. A fixed transaction
journal holds the previous files until both replacements are durable. Recovery
rolls back an interrupted transaction before another writer can collect state.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time

import ccm_core
import ccm_detection
import ccm_pane_state
import ccm_roles


class SnapshotError(RuntimeError):
    pass


class EmptySnapshot(SnapshotError):
    pass


WINDOW_FIELDS = ('session_id', 'window_id', 'window_index', '@ccm_project',
                 '@ccm_dir', 'window_layout', 'window_width', 'window_height',
                 'window_zoomed_flag', 'window_panes', '@ccm_prev_state', '@ccm_restore_pending')
PANE_FIELDS = ('window_id', 'pane_id', 'pane_index', 'pane_pid',
               'pane_current_command', 'pane_current_path', '@ccm_ignore',
               'pane_active', 'pane_height', '@ccm_restore_role')


def _query(command, fields):
    # A sentinel retains empty final fields despite tmux_query's strip().
    fmt = '\t'.join('#{' + f + '}' for f in fields) + '\tEND'
    raw = ccm_core.tmux_query(command, '-a', '-F', fmt)
    if raw is None:
        raise SnapshotError(f'Cannot collect {command}; snapshot unchanged')
    rows = []
    for line in raw.splitlines():
        parts = line.split('\t')
        if len(parts) != len(fields) + 1 or parts[-1] != 'END':
            raise SnapshotError('Incomplete or ambiguous tmux listing; snapshot unchanged')
        rows.append(dict(zip(fields, parts[:-1])))
    return rows


def _inventory():
    windows = [w for w in _query('list-windows', WINDOW_FIELDS) if w['@ccm_project']]
    if len({w['session_id'] for w in windows}) > 1:
        raise SnapshotError('Multiple managed sessions are not supported; snapshot unchanged')
    if any(w['@ccm_restore_pending'] == '1' for w in windows):
        raise SnapshotError('Restore in progress; snapshot unchanged')
    ids = [w['window_id'] for w in windows]
    if len(set(ids)) != len(ids):
        raise SnapshotError('Linked project windows are not supported; snapshot unchanged')
    panes = [p for p in _query('list-panes', PANE_FIELDS) if p['window_id'] in ids]
    return windows, panes


def _positive(value):
    return type(value) is int and value > 0


def _text(value):
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 for c in value)


def _layout_ids(layout):
    """Parse tmux's unzoomed layout tree; return leaf IDs, width and height."""
    if not isinstance(layout, str) or not re.fullmatch(r'[0-9a-f]{4},[0-9x,{}\[\]]+', layout):
        raise SnapshotError('Invalid layout')
    body = layout[5:]
    checksum = 0
    for byte in body.encode('ascii'):
        checksum = ((checksum >> 1) | ((checksum & 1) << 15)) + byte
        checksum &= 0xffff
    if checksum != int(layout[:4], 16):
        raise SnapshotError('Invalid layout checksum')

    def node(pos):
        m = re.match(r'([1-9][0-9]*)x([1-9][0-9]*),([0-9]+),([0-9]+)', body[pos:])
        if not m:
            raise SnapshotError('Invalid layout geometry')
        width, height = int(m[1]), int(m[2])
        pos += m.end()
        ids = []
        if pos < len(body) and body[pos] in '{[':
            closing = '}' if body[pos] == '{' else ']'
            pos += 1
            while True:
                leaves, _, _, pos = node(pos)
                ids.extend(leaves)
                if pos >= len(body):
                    raise SnapshotError('Incomplete layout')
                if body[pos] == closing:
                    pos += 1
                    break
                if body[pos] != ',':
                    raise SnapshotError('Invalid layout separator')
                pos += 1
        else:
            leaf = re.match(r',([0-9]+)', body[pos:])
            if not leaf:
                raise SnapshotError('Missing layout pane')
            ids.append(int(leaf[1]))
            pos += leaf.end()
        return ids, width, height, pos

    try:
        ids, width, height, end = node(0)
    except RecursionError as exc:
        raise SnapshotError('Layout nesting too deep') from exc
    if end != len(body) or len(ids) != len(set(ids)):
        raise SnapshotError('Invalid layout leaves')
    return ids, width, height


def validate(data):
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] not in (1, 2):
        raise SnapshotError('Unsupported or missing snapshot version')
    if not isinstance(data.get('projects'), list):
        raise SnapshotError('Snapshot projects must be a list')
    if data['version'] == 1:
        # v1's loader continues to skip malformed individual entries.
        return data
    meta = data.get('checkpoint', {})
    if (not isinstance(meta, dict) or meta.get('complete') is not True
            or meta.get('scope') != 'single-session' or type(meta.get('sealed')) is not bool
            or not _text(meta.get('session')) or not _text(data.get('created'))
            or not _text(data.get('name')) or not data['projects']):
        raise SnapshotError('Incomplete v2 checkpoint metadata')
    names = set()
    for project in data['projects']:
        if (not isinstance(project, dict) or not _text(project.get('name'))
                or project['name'] in names or not _text(project.get('dir'))
                or type(project.get('auto_start_claude')) is not bool):
            raise SnapshotError('Invalid v2 project')
        names.add(project['name'])
        r = project.get('restore', {})
        if not isinstance(r, dict):
            raise SnapshotError('Missing restore metadata')
        panes = r.get('panes')
        if (not isinstance(panes, list) or not panes
                or not _positive(r.get('width')) or not _positive(r.get('height'))
                or type(r.get('zoomed')) is not bool
                or type(r.get('index')) is not int or r['index'] < 0):
            raise SnapshotError('Invalid window geometry')
        slots = []
        layout_ids = []
        for p in panes:
            if (not isinstance(p, dict) or type(p.get('slot')) is not int or p['slot'] < 0
                    or type(p.get('layout_id')) is not int or p['layout_id'] < 0
                    or p.get('role') not in ('primary', 'sidekick', 'shell', 'unknown')
                    or p.get('agent') not in ('claude', 'codex', 'kimi', 'grok', 'gemini', 'unknown', None)
                    or type(p.get('ignore')) is not bool or not _text(p.get('cwd'))):
                raise SnapshotError('Invalid pane metadata')
            slots.append(p['slot'])
            layout_ids.append(p['layout_id'])
        leaves, width, height = _layout_ids(r.get('layout'))
        if (slots != list(range(len(panes))) or sorted(leaves) != sorted(layout_ids)
                or len(set(layout_ids)) != len(layout_ids)
                or (width, height) != (r['width'], r['height'])
                or type(r.get('active_slot')) is not int or r['active_slot'] not in slots):
            raise SnapshotError('Pane/layout mismatch')
        main = r.get('primary_claude_slot')
        primaries = [p['slot'] for p in panes if p['role'] == 'primary']
        if (main is not None and (type(main) is not int or main not in slots)
                or primaries != ([] if main is None else [main])
                or any(p['role'] == 'primary' and (p['agent'] != 'claude' or p['ignore']) for p in panes)):
            raise SnapshotError('Ambiguous primary pane')
    interrupted = meta.get('interrupted')
    if not isinstance(interrupted, list):
        raise SnapshotError('Missing interrupted project list')
    seen = set()
    for p in interrupted:
        if (not isinstance(p, dict) or set(p) != {'name', 'state'}
                or not _text(p.get('name')) or p.get('name') not in names or p.get('state') not in ('PERMIT', 'BUSY')
                or p['name'] in seen):
            raise SnapshotError('Invalid interrupted project')
        seen.add(p['name'])
    return data


def _observed_state(w, panes, ps_lines):
    # Resolve without apply_actions: preparing a checkpoint sends no keys,
    # clears no ignore markers and does not alter the state detector's caches.
    cache = [(w['window_id'], p['pane_pid'], p['pane_id'], p['pane_current_command'],
              p['pane_active'], p['pane_height'], p['@ccm_ignore']) for p in panes]
    ctx = ccm_detection.build_detection_context(
        w['window_id'], w['@ccm_dir'], w['@ccm_prev_state'], cache, ps_lines, str(os.getpgrp()), write_session_cache=False)
    return ccm_detection.resolve_state_from_context(ctx, w['@ccm_dir'])[0]


def collect(name, sealed=False):
    before = _inventory()
    windows, panes = before
    if not windows:
        raise EmptySnapshot('No active projects to save; snapshot unchanged')
    ps_lines = ccm_core.ps_snapshot().splitlines()
    if not ps_lines:
        raise SnapshotError('Cannot read process inventory; snapshot unchanged')
    projects, interrupted, observed_roles = [], [], []
    for w in windows:
        wp = sorted((p for p in panes if p['window_id'] == w['window_id']),
                    key=lambda p: int(p['pane_index']))
        if len(wp) != int(w['window_panes']):
            raise SnapshotError('Pane count changed during capture; retry')
        saved = []
        for slot, p in enumerate(wp):
            claude = ccm_pane_state.find_claude_pid(p['pane_pid'], ps_lines)
            agent = 'claude' if claude else ccm_core.external_agent_name(p['pane_current_command']) or None
            ignore = bool(p['@ccm_ignore'] and p['@ccm_ignore'] != '0')
            role = ('sidekick' if ignore or agent and agent != 'claude' else
                    'shell' if not agent and p['pane_current_command'] in ccm_core.SHELL_FOREGROUND_COMMANDS
                    else 'unknown')
            observed_roles.append((p['pane_id'], p['@ccm_restore_role'], agent))
            reserved = ccm_roles.observed(ccm_roles.decode(p['@ccm_restore_role']), agent)
            if reserved:
                role, agent = reserved['role'], reserved['agent']
                ignore = ignore or reserved['ignore']
            saved.append({'slot': slot, 'layout_id': int(p['pane_id'].lstrip('%')),
                          'role': role, 'agent': agent, 'ignore': ignore,
                          'cwd': ccm_core.shorten_home(p['pane_current_path'])})
        candidates = [p for p in saved if p['role'] == 'primary' and not p['ignore']]
        if not candidates:
            candidates = [p for p in saved if p['agent'] == 'claude' and not p['ignore']]
        if len(candidates) != 1:
            for p in saved:
                if p['role'] == 'primary':
                    p['role'] = 'unknown'
        primary = candidates[0]['slot'] if len(candidates) == 1 else None
        if primary is not None:
            saved[primary]['role'] = 'primary'
        active = [i for i, p in enumerate(wp) if p['pane_active'] == '1']
        if len(active) != 1:
            raise SnapshotError('Cannot identify active pane')
        projects.append({'name': w['@ccm_project'], 'dir': ccm_core.shorten_home(w['@ccm_dir']),
                         'auto_start_claude': True,
                         'restore': {'layout': w['window_layout'], 'width': int(w['window_width']),
                                     'height': int(w['window_height']), 'index': int(w['window_index']),
                                     'zoomed': w['window_zoomed_flag'] == '1', 'panes': saved,
                                     'active_slot': active[0], 'primary_claude_slot': primary}})
        state = _observed_state(w, wp, ps_lines) if sealed else w['@ccm_prev_state']
        if state in ('PERMIT', 'BUSY'):
            interrupted.append({'name': w['@ccm_project'], 'state': state})
    if _inventory() != before:
        raise SnapshotError('Window or pane state changed during capture; retry')
    for pane, raw, agent in observed_roles:
        ccm_roles.reconcile(pane, raw, agent)
    return validate({'version': 2, 'name': name,
                     'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'projects': projects,
                     'checkpoint': {'scope': 'single-session', 'session': windows[0]['session_id'],
                                    'complete': True, 'sealed': sealed, 'interrupted': interrupted}})


def directory():
    return Path(ccm_core.CCM_SNAPSHOT_DIR)


def read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return validate(json.load(f))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SnapshotError(f'Cannot read snapshot {Path(path).name}: {exc}') from exc


def sealed(data):
    return bool(data and data.get('version') == 2 and data['checkpoint']['sealed'])


def comparable(data):
    return {k: v for k, v in data.items() if k != 'created'}


def _sync_dir():
    fd = os.open(directory(), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stage(data):
    fd, path = tempfile.mkstemp(prefix='.snapshot-', dir=directory())
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            with open(path, encoding='utf-8') as verify:
                if json.load(verify) != data:
                    raise SnapshotError('Snapshot write verification failed')
            os.fsync(f.fileno())
        return path
    except BaseException:
        os.unlink(path)
        raise


def _put(path, data):
    if data is None:
        path.unlink(missing_ok=True)
    else:
        staged = _stage(data)
        try:
            os.replace(staged, path)
        finally:
            Path(staged).unlink(missing_ok=True)


def _rollback(transaction):
    name = transaction['name']
    if not isinstance(name, str) or Path(name).name != name or not name.endswith('.json'):
        raise SnapshotError('Invalid snapshot transaction; manual inspection required')
    for key in ('old', 'previous'):
        if transaction[key] is not None:
            validate(transaction[key])
    targets = [(name, 'old')]
    if name == '_autosave.json':
        targets.append(('_autosave.prev', 'previous'))
    for target, key in targets:
        path = directory() / target
        value = transaction[key]
        if read(path) == value:
            continue
        if value is None:
            path.unlink(missing_ok=True)
        else:
            staged = transaction[key + '_file']
            if (not isinstance(staged, str) or Path(staged).name != staged
                    or not staged.startswith('.snapshot-')):
                raise SnapshotError('Invalid rollback file')
            rollback = directory() / staged
            if read(rollback) != value:
                raise SnapshotError('Rollback file missing or invalid; manual inspection required')
            os.replace(rollback, path)
    _sync_dir()


def _recover():
    journal = directory() / '.snapshot-transaction'
    if not journal.exists():
        return
    with journal.open(encoding='utf-8') as f:
        transaction = json.load(f)
    _rollback(transaction)
    journal.unlink()
    _sync_dir()


@contextmanager
def locked():
    directory().mkdir(mode=0o700, parents=True, exist_ok=True)
    directory().chmod(0o700)
    fd = os.open(directory() / '.snapshot-lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _recover()
        # Only ccm's unique staging files are cleaned, under the writer lock.
        for path in directory().glob('.snapshot-*'):
            if path.name not in ('.snapshot-lock', '.snapshot-transaction'):
                path.unlink()
        yield
    finally:
        os.close(fd)


def write(name, data, *, backup=True):
    """Caller holds locked(). A durable journal makes two-file rollback possible."""
    validate(data)
    path = directory() / f'{name}.json'
    old = read(path)
    if old is not None and comparable(old) == comparable(data):
        return False
    previous = read(directory() / '_autosave.prev') if name == '_autosave' else None
    # A v1 backup must contain usable projects, not merely parse as JSON.
    if old and (not old['projects'] or any(not isinstance(p, dict) or not _text(p.get('name'))
                                        or not _text(p.get('dir')) for p in old['projects'])):
        raise SnapshotError('Existing snapshot is incomplete; refusing to replace it')
    staging = []
    transaction = None
    journal = directory() / '.snapshot-transaction'
    try:
        staged = _stage(data)
        staging.append(staged)
        transaction = {'name': path.name, 'old': old, 'previous': previous}
        for key, value in (('old', old), ('previous', previous)):
            rollback = _stage(value) if value is not None else None
            transaction[key + '_file'] = Path(rollback).name if rollback else None
            if rollback:
                staging.append(rollback)
        _put(journal, transaction)
        _sync_dir()
        os.replace(staged, path)
        _sync_dir()
        if name == '_autosave' and old is not None and backup:
            _put(directory() / '_autosave.prev', old)
            _sync_dir()
        journal.unlink()
        _sync_dir()
    except Exception:
        if transaction is not None:
            # Rollback files were fsynced before touching either destination.
            # Even persistent ENOSPC/fsync failures need no new data writes
            # to put the old files back in this process.
            _rollback(transaction)
            journal.unlink(missing_ok=True)
            _sync_dir()
        raise
    finally:
        # A pending journal owns its rollback files until recovery completes.
        if not journal.exists():
            for staged in staging:
                Path(staged).unlink(missing_ok=True)
    return True
