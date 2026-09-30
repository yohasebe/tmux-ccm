"""Restored pane intent. These reservations are never detection state."""
import json
import unicodedata

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
                or r['role'] not in ('primary', 'sidekick', 'shell', 'unknown')
                or r['agent'] not in ('claude', 'codex', 'kimi', 'grok', 'gemini', 'unknown', None)
                or type(r['ignore']) is not bool
                or r['role'] == 'primary' and (r['agent'] != 'claude' or r['ignore'])):
            return None
        return r
    except (TypeError, ValueError):
        return None


def observed(reservation, agent):
    """Keep intent while a shell waits; an observed different agent wins."""
    if not reservation:
        return None
    if agent and agent != reservation['agent']:
        return None
    return reservation


def reconcile(pane, raw, agent):
    r = decode(raw)
    if r and agent and agent != r['agent']:
        # Positive agent evidence releases stale intent; reservations never
        # become PERMIT/BUSY/IDLE or live ignore state.
        ccm_core.tmux_cmd('set-option', '-pu', '-t', pane, ROLE_OPTION)
        return None
    return r


def primary(target, panes):
    """None: ordinary window; empty string: reserved window with no safe main."""
    if ccm_core.tmux_cmd('show-option', '-wqv', '-t', target, MANAGED_OPTION) != '1':
        return None
    candidates = []
    for p in panes:
        r = decode(ccm_core.tmux_cmd('show-option', '-pqv', '-t', p.pane_id, ROLE_OPTION))
        if r and r['role'] == 'primary' and not r['ignore'] and not p.ignored:
            candidates.append(p.pane_id)
    return candidates[0] if len(candidates) == 1 else ''


def unignore(pane):
    r = decode(ccm_core.tmux_cmd('show-option', '-pqv', '-t', pane, ROLE_OPTION))
    if r and r['ignore']:
        r['ignore'] = False
        ccm_core.tmux_cmd('set-option', '-p', '-t', pane, ROLE_OPTION, json.dumps(r))


def hint(r):
    if r['role'] == 'primary':
        return 'Claude: opens here when you attach'
    if r['agent'] == 'codex':
        return 'codex: choose a conversation in this directory with `codex resume`'
    if r['agent'] == 'claude' and r['ignore']:
        return 'Claude sidekick: start manually with CCM_IGNORE=1 from the first hook'
    if r['role'] == 'sidekick':
        return f"{r['agent'] or 'sidekick'}: resume manually in this directory"
    return r['role'] + ': no automatic agent launch'


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
            if ccm_core.tmux_query('set-option', '-pu', '-t', pane, ROLE_OPTION) is None:
                ccm_core.ccm_die('Cannot clear pane role')
            print(f'{pane}: role reservation cleared; start agents manually')
        else:
            print(clean(f"{pane} {cwd}: {hint(r) if r else 'no reserved role'}"))
