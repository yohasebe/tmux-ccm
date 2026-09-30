"""Exit a verified Claude conversation while retaining its shell and window."""
import argparse
import time

import ccm_core
import ccm_pane_state
import ccm_roles


def pane_tail(pane):
    value = ccm_core.tmux_cmd('capture-pane', '-t', pane, '-p', '-S', '-10')
    return value if isinstance(value, str) else ''


def exit_pane(pane, capture=None, sleep=None):
    """Shared screen gate, exit sequence and foreground verification."""
    capture = capture or pane_tail
    sleep = sleep or time.sleep
    tail = capture(pane)
    if not tail.strip():
        return 'capture_unreadable'
    if ccm_core.is_agents_tui(tail):
        return 'agents_view'
    # A pane left scrolled in copy mode would take `/` as a search.
    # Leaving the mode is a no-op (and a harmless tmux error) otherwise.
    ccm_core.tmux_cmd('send-keys', '-t', pane, '-X', 'cancel')
    ccm_core.tmux_cmd('send-keys', '-t', pane, 'Escape')
    sleep(.1)
    ccm_core.tmux_cmd('send-keys', '-t', pane, '/exit', 'Enter')
    sleep(.5)
    current = ccm_core.tmux_cmd('display-message', '-t', pane, '-p', '#{pane_current_command}')
    if current in ccm_core.SHELL_FOREGROUND_COMMANDS:
        return 'exited'
    if ccm_core.is_agents_tui(capture(pane)):
        return 'agents_view_after_exit'
    return 'unconfirmed'


def cmd_exit(args):
    parser = argparse.ArgumentParser(prog='ccm exit', description='Exit Claude; retain the window and sidekicks.')
    parser.add_argument('name')
    parser.add_argument('-y', '--yes', action='store_true', help='Allow BUSY/PERMIT; Escape rejects the pending tool call in PERMIT')
    opts = parser.parse_args(args)
    project = next((p for p in ccm_core.build_project_list(fast=False) if p.name == opts.name), None)
    if project is None:
        ccm_core.ccm_die('Project not found: ' + ccm_roles.clean(opts.name))
    name = ccm_roles.clean(project.name)
    if ccm_roles.pending(project.win_target):
        ccm_core.ccm_die('Restore incomplete; continue restoration before exiting Claude')
    if project.state in ('BUSY', 'PERMIT') and not opts.yes:
        ccm_core.ccm_die(f'{name}: {project.state}; use -y to exit. In PERMIT, Escape rejects the pending tool call.')
    if project.state == 'SHELL':
        ccm_core.ccm_info(name + ': Claude is already stopped; window retained')
        return
    panes = ccm_pane_state.enumerate_window_panes(project.win_target, ccm_core.ps_snapshot().splitlines())
    candidates = [p for p in panes if not p.ignored and p.claude_pid
                  and str(p.claude_pid) != str(p.pane_pid)]
    primary = ccm_roles.primary(project.win_target, panes)
    if primary is not None:
        candidates = [p for p in candidates if p.pane_id == primary]
    if len(candidates) != 1:
        ccm_core.ccm_die(name + ': cannot identify one Claude pane with a shell; inspect it before exiting')
    outcome = exit_pane(candidates[0].pane_id)
    reasons = {
        'capture_unreadable': 'Cannot read the pane; no keys sent',
        'agents_view': 'Agent view is open; no keys sent',
        'agents_view_after_exit': 'Agent view appeared; Claude exit is not confirmed',
        'unconfirmed': 'Claude has not returned to a shell; inspect the pane before retrying',
    }
    if outcome != 'exited':
        ccm_core.ccm_die(name + ': ' + reasons[outcome])
    ccm_core.ccm_info(name + ': Claude exited; window and sidekicks retained')
