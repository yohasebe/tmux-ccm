"""Restored pane intent. These reservations are never detection state."""
from dataclasses import dataclass
from enum import Enum
import json
import unicodedata
from typing import FrozenSet, Optional

import ccm_core

ROLE_OPTION = '@ccm_restore_role'
MANAGED_OPTION = '@ccm_restore_managed'
PENDING_OPTION = '@ccm_restore_pending'


def clean(text):
    return ''.join(c if unicodedata.category(c)[0] != 'C' else ' ' for c in str(text)).replace('#', '＃')


def pending(target, query=None):
    return (query or ccm_core.tmux_cmd)('show-option', '-wqv', '-t', target, PENDING_OPTION) == '1'


def decode(raw):
    if not raw:
        return None
    try:
        r = json.loads(raw)
        if (not isinstance(r, dict) or set(r) != {'role', 'agent', 'ignore'}
                or r['role'] not in ('primary', 'sidekick', 'shell', 'unknown', 'manual')
                or r['agent'] not in ('claude', 'codex', 'kimi', 'grok', 'gemini', 'unknown', None)
                or type(r['ignore']) is not bool
                or r['role'] == 'primary' and (r['agent'] != 'claude' or r['ignore'])):
            return None
        return r
    except (TypeError, ValueError):
        return None


def observed(reservation, agent):
    """Preserve manual intent; other reservations yield to a different agent."""
    if not reservation:
        return None
    if reservation['role'] == 'manual':
        return reservation
    if agent and agent != reservation['agent']:
        return None
    return reservation


def reconcile(pane, raw, agent):
    r = decode(raw)
    if r and r['role'] != 'manual' and agent and agent != r['agent']:
        # Observed agents release stale intent, but must leave a readable
        # reservation for subsequent automatic operations in a restored window.
        r = {'role': 'unknown' if agent == 'claude' else 'sidekick',
             'agent': agent, 'ignore': False}
        ccm_core.tmux_cmd('set-option', '-p', '-t', pane, ROLE_OPTION, json.dumps(r))
    return r


class SelectionState(Enum):
    ORDINARY = 'ordinary'
    NO_PRIMARY = 'no-primary'
    PRIMARY = 'primary'
    BLOCKED = 'blocked'


@dataclass(frozen=True)
class Selection:
    state: SelectionState
    eligible: FrozenSet[str] = frozenset()
    primary: Optional[str] = None
    reason: str = ''


def selection(target, panes):
    """Read each reservation once; distinguish absent intent from failed reads."""
    def blocked(reason):
        return Selection(SelectionState.BLOCKED, reason=reason +
                         '; inspect with ccm roles, or use ccm roles --clear for manual operation')

    managed = ccm_core.tmux_query('show-option', '-wqv', '-t', target, MANAGED_OPTION)
    if managed is None:
        return blocked('Cannot read restored-window status')
    if managed not in ('', '0', '1'):
        return blocked('Invalid restored-window status')
    found = {}
    for p in panes:
        raw = ccm_core.tmux_query('show-option', '-pqv', '-t', p.pane_id, ROLE_OPTION)
        if raw is None:
            return blocked('Cannot read restored pane roles')
        if raw == '':
            continue
        r = decode(raw)
        if r is None:
            return blocked('Invalid restored pane role')
        found[p.pane_id] = r
    eligible = frozenset(
        p.pane_id for p in panes if not p.ignored and
        ((not found[p.pane_id]['ignore'] and
          found[p.pane_id]['role'] not in ('sidekick', 'manual'))
         if p.pane_id in found else not ccm_core.external_agent_name(p.current_command)))
    if managed != '1':
        return Selection(SelectionState.ORDINARY, eligible)
    primaries = [pid for pid, r in found.items() if r['role'] == 'primary']
    if len(primaries) > 1:
        return blocked('Conflicting primary pane reservations')
    if primaries:
        return Selection(SelectionState.PRIMARY, eligible, primaries[0])
    return Selection(SelectionState.NO_PRIMARY, eligible)


def unignore(pane):
    r = decode(ccm_core.tmux_cmd('show-option', '-pqv', '-t', pane, ROLE_OPTION))
    if r and r['ignore']:
        r['ignore'] = False
        ccm_core.tmux_cmd('set-option', '-p', '-t', pane, ROLE_OPTION, json.dumps(r))


def hint(r):
    if r['role'] == 'manual':
        return 'manual: start agents manually; no automatic launch or exit'
    if r['role'] == 'primary':
        return 'Claude: opens here when you attach'
    if r['agent'] == 'codex':
        return 'codex: choose a conversation in this directory with `codex resume`'
    if r['agent'] == 'claude' and r['ignore']:
        return 'Claude sidekick: start manually with CCM_IGNORE=1 from the first hook'
    if r['role'] == 'sidekick':
        return f"{r['agent'] or 'sidekick'}: resume manually in this directory"
    if r['ignore']:
        return r['role'] + ': ignored reservation; start agents manually'
    return r['role'] + ': without a primary reservation, opening the window may start Claude in an eligible shell'


def cmd_roles(args):
    import argparse
    parser = argparse.ArgumentParser(prog='ccm roles')
    parser.add_argument('target', nargs='?', default='', help='Pane %%ID or tmux window target')
    parser.add_argument('--clear', action='store_true', help='Release role reservations; does not change live ignore marks')
    opts = parser.parse_args(args)
    target = opts.target or ccm_core.tmux_cmd('display-message', '-p', '#{pane_id}')
    if not target or pending(target):
        ccm_core.ccm_die('Restore is incomplete or target is unavailable; retry the snapshot load first')
    fmt = '#{pane_id}\t#{pane_current_path}\t#{' + ROLE_OPTION + '}\tEND'
    raw = ccm_core.tmux_query('list-panes', '-t', target, '-F', fmt)
    if raw is None:
        ccm_core.ccm_die('Cannot read pane roles')
    for line in raw.splitlines():
        fields = line.split('\t')
        if len(fields) != 4 or fields[-1] != 'END':
            ccm_core.ccm_die('Cannot parse pane roles')
        pane, cwd, value, _ = fields
        if target.startswith('%') and pane != target:
            continue
        r = decode(value)
        if opts.clear:
            manual = json.dumps({'role': 'manual', 'agent': None, 'ignore': False})
            if ccm_core.tmux_query('set-option', '-p', '-t', pane, ROLE_OPTION, manual) is None:
                ccm_core.ccm_die('Cannot clear pane role')
            print(f'{pane}: role reservation cleared; start agents manually')
        else:
            if value == '':
                description = 'no reservation: follows current command; a unique primary takes priority'
            elif r:
                description = 'reserved: ' + hint(r)
            else:
                description = 'invalid role; use ccm roles --clear for manual operation'
            print(clean(f"{pane} {cwd}: {description}"))
