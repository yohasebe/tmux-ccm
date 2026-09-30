"""Exit shares auto-exit's screen checks and retains other panes."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import ccm_core
import ccm_commands
import ccm_exit as exiting
import ccm_pane_state
import ccm_roles


@pytest.fixture
def env(monkeypatch):
    project = SimpleNamespace(name='alpha', state='IDLE', win_target='0:1')
    monkeypatch.setattr(ccm_core, 'build_project_list', lambda **kw: [project])
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '')
    panes = [ccm_pane_state.PaneInfo('%1', '100', False, 'claude', False, '101'),
             ccm_pane_state.PaneInfo('%2', '200', True, 'zsh', False, None)]
    monkeypatch.setattr(ccm_pane_state, 'enumerate_window_panes', lambda *a: panes)
    monkeypatch.setattr(ccm_roles, 'pending', lambda *a: False)
    monkeypatch.setattr(ccm_roles, 'primary', lambda *a: None)
    monkeypatch.setattr(exiting.time, 'sleep', lambda n: None)
    calls = []
    def tmux(*args):
        calls.append(args)
        return 'zsh' if args[0] == 'display-message' else 'conversation prompt'
    monkeypatch.setattr(ccm_core, 'tmux_cmd', tmux)
    monkeypatch.setattr(ccm_core, 'is_agents_tui', lambda text: text == 'agents screen')
    return project, panes, calls


@pytest.mark.parametrize('state,yes,allowed', [('IDLE', False, True), ('BUSY', False, False),
    ('PERMIT', False, False), ('BUSY', True, True), ('PERMIT', True, True), ('SHELL', False, False)])
def test_exit_state_policy_and_pane_target(env, state, yes, allowed, capsys):
    project, _, calls = env
    project.state = state
    with ccm_core.raise_on_die():
        if state in ('BUSY', 'PERMIT') and not yes:
            with pytest.raises(ccm_core.CCMError, match='use -y'):
                ccm_commands.cmd_exit(['alpha'])
        else:
            ccm_commands.cmd_exit(['alpha'] + (['-y'] if yes else []))
    sends = [a for a in calls if a[0] == 'send-keys']
    assert bool(sends) is allowed
    assert all(a[2] == '%1' for a in sends)
    assert not any(a[0] in ('kill-pane', 'kill-window') for a in calls)
    if allowed:
        assert 'Claude exited; window and sidekicks retained' in capsys.readouterr().out


@pytest.mark.parametrize('case', ['ignore', 'own-process', 'ambiguous', 'no-primary', 'pending', 'missing'])
def test_refuse_unsafe_exit_target(env, monkeypatch, case):
    project, panes, calls = env
    if case == 'ignore':
        panes[0] = panes[0]._replace(ignored=True)
    elif case == 'own-process':
        panes[0] = panes[0]._replace(claude_pid='100')
    elif case == 'ambiguous':
        panes[1] = panes[1]._replace(claude_pid='201')
    elif case == 'no-primary':
        monkeypatch.setattr(ccm_roles, 'primary', lambda *a: '')
    elif case == 'pending':
        monkeypatch.setattr(ccm_roles, 'pending', lambda *a: True)
    else:
        project.name = 'beta'
    with ccm_core.raise_on_die(), pytest.raises(ccm_core.CCMError):
        ccm_commands.cmd_exit(['alpha', '-y'])
    assert not [a for a in calls if a[0] == 'send-keys']


def test_reserved_primary_wins_over_active_sidekick(env, monkeypatch):
    _, panes, calls = env
    panes[1] = panes[1]._replace(claude_pid='201')
    monkeypatch.setattr(ccm_roles, 'primary', lambda *a: '%1')
    ccm_commands.cmd_exit(['alpha'])
    assert all(a[2] == '%1' for a in calls if a[0] == 'send-keys')


@pytest.mark.parametrize('before,after,foreground,expected,sends', [
    ('', '', 'zsh', 'capture_unreadable', 0),
    ('agents screen', '', 'zsh', 'agents_view', 0),
    ('conversation', 'agents screen', 'claude', 'agents_view_after_exit', 2),
    ('conversation', 'conversation', 'claude', 'unconfirmed', 2),
    ('conversation', '', '', 'unconfirmed', 2),
    ('conversation', '', 'zsh', 'exited', 2),
])
def test_screen_gate_and_post_exit_verification(env, monkeypatch, before, after, foreground, expected, sends):
    capture = Mock(side_effect=[before, after])
    calls = Mock(return_value=foreground)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', calls)
    assert exiting.exit_pane('%1', capture=capture) == expected
    # Count typed keys; leaving copy mode (-X cancel) types nothing.
    keys = [c.args for c in calls.call_args_list if c.args[0] == 'send-keys' and '-X' not in c.args]
    assert len(keys) == sends
    assert not any('clear' in a for a in keys)


def test_unconfirmed_cli_exit_is_failure(env, monkeypatch, capsys):
    monkeypatch.setattr(exiting, 'exit_pane', lambda pane: 'unconfirmed')
    with ccm_core.raise_on_die(), pytest.raises(ccm_core.CCMError, match='has not returned to a shell'):
        ccm_commands.cmd_exit(['alpha'])
    assert 'Claude exited' not in capsys.readouterr().out


@pytest.mark.parametrize('args', [['alpha'], ['alpha', '-y'], ['--yes', 'alpha']])
def test_exit_dispatch_passes_flags_to_shared_handler(monkeypatch, args):
    handler = Mock()
    monkeypatch.setattr(ccm_commands, 'cmd_exit', handler)
    ccm_core.dispatch(['exit', *args])
    handler.assert_called_once_with(args)


def test_exit_leaves_copy_mode_before_typing(monkeypatch):
    """A scrolled pane must not take `/exit` as a copy-mode search."""
    import ccm_exit
    sent = []
    monkeypatch.setattr(ccm_exit.ccm_core, 'tmux_cmd',
                        lambda *a: sent.append(a) or ('zsh' if a[0] == 'display-message' else ''))
    assert ccm_exit.exit_pane('%9', capture=lambda p: '❯ \n', sleep=lambda s: None) == 'exited'
    keys = [a[3:] for a in sent if a[0] == 'send-keys']
    assert keys[0] == ('-X', 'cancel')
    assert keys.index(('-X', 'cancel')) < keys.index(('Escape',)) < keys.index(('/exit', 'Enter'))
