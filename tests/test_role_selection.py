"""Restored intent must be known before any automatic launch or exit."""
import json
from types import SimpleNamespace

import pytest

import ccm_core as core
import ccm_exit as exiting
import ccm_pane_state
import ccm_roles as roles
import ccm_window as window

REAL_QUERY = core.tmux_query


def reservation(kind='shell', ignore=False, agent=None):
    return json.dumps({'role': kind, 'agent': 'claude' if kind == 'primary' else agent,
                       'ignore': ignore})


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(managed='1', raw={'%1': reservation()}, calls=[],
                        active='%1', ignored=set(), commands={}, claudes=set())
    def panes(*args):
        return [ccm_pane_state.PaneInfo(pid, str(100+i), pid == w.active,
                    w.commands.get(pid, 'claude' if pid in w.claudes else 'sh'),
                    pid in w.ignored, str(200+i) if pid in w.claudes else None)
                for i, pid in enumerate(w.raw)]
    w.panes = panes
    def query(*a, **kw):
        w.calls.append(a)
        if a[0] == 'show-option':
            if a[-1] == roles.MANAGED_OPTION:
                return w.managed
            if a[-1] == roles.ROLE_OPTION:
                value = w.raw[a[a.index('-t')+1]]
                return value.pop(0) if isinstance(value, list) else value
            return ''
        if a[0] == 'list-panes':
            return '\n'.join(f'{pid}\t/tmp/alpha\t{raw or ""}\tEND' for pid,raw in w.raw.items())
        if a[0] == 'set-option':
            pid = a[a.index('-t')+1]
            w.raw[pid] = '' if '-pu' in a else a[-1]
            return ''
        if a[0] == 'capture-pane':
            return 'conversation prompt'
        if a[0] == 'display-message':
            return 'sh'
        return ''
    monkeypatch.setattr(core, 'tmux_query', query)
    monkeypatch.setattr(core, 'tmux_cmd', lambda *a, **kw: query(*a, **kw) or '')
    monkeypatch.setattr(core, 'ps_snapshot', lambda: '100 1 100 sh 00:10')
    monkeypatch.setattr(core, 'build_project_list', lambda **kw: [SimpleNamespace(
        name='alpha', state='IDLE', win_target='@1')])
    monkeypatch.setattr(ccm_pane_state, 'enumerate_window_panes', panes)
    monkeypatch.setattr(window, 'enumerate_window_panes', panes)
    monkeypatch.setattr(window, 'continue_blocker_notice', lambda *a: None)
    monkeypatch.setattr(exiting.time, 'sleep', lambda *a: None)
    w.launch = lambda **kw: window.launch_claude('@1', honour_setting=False, **kw)
    w.typed = lambda: [a for a in w.calls if a[0] == 'send-keys' and a[-1] == 'Enter']
    return w


@pytest.mark.parametrize('bad', [None, '{broken', '{}', reservation('unrecognized')])
def test_unreadable_sidekick_never_launches(world, bad):
    world.raw = {'%1': bad}
    assert world.launch().outcome == window.UNAVAILABLE
    assert not world.typed()


@pytest.mark.parametrize('bad', [None, '{broken', '{}', reservation('unrecognized')])
def test_unreadable_sidekick_never_exits(world, bad):
    world.raw = {'%1': bad}
    world.claudes = {'%1'}
    with core.raise_on_die():
        try:
            exiting.cmd_exit(['alpha'])
        except core.CCMError:
            pass
    assert not world.typed()


def test_duplicate_primary_never_launches(world):
    world.raw = {'%1': reservation('primary'), '%2': reservation('primary')}
    assert world.launch().outcome == window.UNAVAILABLE
    assert not world.typed()


def test_clear_keeps_manual_launch_contract(world):
    world.raw = {'%1': reservation('sidekick', agent='codex')}
    roles.cmd_roles(['%1', '--clear'])
    assert world.launch().outcome == window.UNAVAILABLE
    assert not world.typed()


@pytest.mark.parametrize('managed,kind', [('', 'ORDINARY'), ('0', 'ORDINARY'),
    ('1', 'NO_PRIMARY'), (None, 'BLOCKED'), ('broken', 'BLOCKED')])
def test_selection_distinguishes_window_status(world, managed, kind):
    world.managed = managed
    choice = roles.selection('@1', world.panes())
    assert choice.state.name == kind
    if kind == 'BLOCKED':
        assert 'ccm roles' in world.launch().reason
        world.claudes = {'%1'}
        with core.raise_on_die(), pytest.raises(core.CCMError, match='ccm roles'):
            exiting.cmd_exit(['alpha'])
        assert not world.typed()


def test_reservations_are_not_reread_when_no_primary(world):
    world.raw = {'%1': [reservation('sidekick', agent='codex'), None]}
    assert world.launch().outcome == window.UNAVAILABLE
    reads = [a for a in world.calls if a[-1] == roles.ROLE_OPTION]
    assert len(reads) == 1
    assert not world.typed()


def test_duplicate_primary_refuses_exit_even_with_one_claude(world):
    world.raw = {'%1': reservation('primary'), '%2': reservation('primary')}
    world.claudes = {'%1'}
    with core.raise_on_die(), pytest.raises(core.CCMError, match='Conflicting'):
        exiting.cmd_exit(['alpha'])
    assert not world.typed()


@pytest.mark.parametrize('case', ['ignored', 'excluded', 'editor'])
def test_unusable_primary_does_not_move_to_other_shell(world, case):
    world.raw = {'%1': reservation('primary'), '%2': reservation()}
    world.active = '%2'
    if case == 'ignored':
        world.ignored.add('%1')
    if case == 'editor':
        world.commands['%1'] = 'vim'
    result = world.launch(exclude_pane='%1' if case == 'excluded' else None)
    assert result.outcome == window.UNAVAILABLE and 'ccm roles' in result.reason
    assert not world.typed()


@pytest.mark.parametrize('case', ['ignored', 'no-shell-parent', 'other-claude'])
def test_unusable_primary_does_not_exit_other_claude(world, monkeypatch, case):
    world.raw = {'%1': reservation('primary'), '%2': reservation()}
    world.claudes = {'%2'}
    if case == 'ignored':
        world.ignored.add('%1')
        world.claudes.add('%1')
    elif case == 'no-shell-parent':
        original = world.panes
        monkeypatch.setattr(ccm_pane_state, 'enumerate_window_panes', lambda *a:
            [p._replace(claude_pid=p.pane_pid) if p.pane_id == '%1' else p for p in original()])
    with core.raise_on_die(), pytest.raises(core.CCMError):
        exiting.cmd_exit(['alpha'])
    assert not world.typed()


@pytest.mark.parametrize('kind', ['shell', 'unknown', 'sidekick'])
@pytest.mark.parametrize('reserved_ignore,live_ignore', [(False, False), (True, False), (False, True), (True, True)])
def test_ignore_and_sidekick_exclusion_for_launch_and_exit(world, kind, reserved_ignore, live_ignore):
    world.raw = {'%1': reservation(kind, reserved_ignore)}
    if live_ignore:
        world.ignored.add('%1')
    eligible = kind != 'sidekick' and not reserved_ignore and not live_ignore
    assert (world.launch().outcome == window.LAUNCHED) is eligible
    world.calls.clear()
    world.claudes = {'%1'}
    with core.raise_on_die():
        if eligible:
            exiting.cmd_exit(['alpha'])
        else:
            with pytest.raises(core.CCMError):
                exiting.cmd_exit(['alpha'])
    assert bool(world.typed()) is eligible


@pytest.mark.parametrize('active,other_command,exclude,expected', [
    ('%2', 'sh', None, '%2'), ('%3', 'sh', None, None),
    ('%2', 'vim', None, '%1'), ('%2', 'vim', '%1', None),
    ('%3', 'vim', None, '%1'), ('%3', 'vim', '%1', None),
])
def test_no_primary_uses_normal_shell_selection(world, active, other_command, exclude, expected):
    world.raw = {'%1': reservation(), '%2': reservation(), '%3': reservation('sidekick')}
    world.active = active
    world.commands['%2'] = other_command
    result = world.launch(exclude_pane=exclude)
    assert result.pane == expected
    assert bool(world.typed()) is (expected is not None)


def test_no_primary_exit_chooses_main_not_claude_sidekick(world):
    world.raw = {'%1': reservation(), '%2': reservation('sidekick', agent='claude')}
    world.claudes = {'%1', '%2'}
    with core.raise_on_die():
        exiting.cmd_exit(['alpha'])
    assert [a[2] for a in world.typed()] == ['%1']


@pytest.mark.parametrize('managed', ['1', ''])
def test_clear_role_is_explicit_and_remains_manual_after_observation(world, managed, capsys):
    world.managed = managed
    roles.cmd_roles(['%1', '--clear'])
    raw = world.raw['%1']
    assert roles.decode(raw)['role'] == 'manual'
    assert 'start agents manually' in capsys.readouterr().out
    assert world.launch().outcome == window.UNAVAILABLE
    roles.reconcile('%1', raw, 'claude')
    assert world.raw['%1'] == raw
    world.claudes = {'%1'}
    with core.raise_on_die(), pytest.raises(core.CCMError):
        exiting.cmd_exit(['alpha'])
    assert not world.typed()


@pytest.mark.parametrize('kind,expected', [('shell', 'opening the window'),
    ('unknown', 'opening the window'), ('sidekick', 'resume manually'), ('manual', 'no automatic launch or exit')])
def test_roles_guidance_matches_launch_policy(world, capsys, kind, expected):
    world.raw = {'%1': reservation(kind)}
    roles.cmd_roles(['%1'])
    assert expected in capsys.readouterr().out
    assert expected in roles.hint(roles.decode(world.raw['%1']))


@pytest.mark.parametrize('failure', ['timeout', 'nonzero'])
@pytest.mark.parametrize('failed_option', [roles.MANAGED_OPTION, roles.ROLE_OPTION])
@pytest.mark.parametrize('operation', ['launch', 'exit'])
def test_real_query_failure_blocks_selection(world, monkeypatch, failure, failed_option, operation):
    import subprocess
    # Exercise the real query adapter as well as the selection policy.
    def run(argv, **kw):
        if argv[-1] == failed_option:
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(argv, 5)
            return subprocess.CompletedProcess(argv, 1, b'', b'synthetic query failure')
        return subprocess.CompletedProcess(argv, 0, b'1', b'')
    monkeypatch.setattr(core.subprocess, 'run', run)
    monkeypatch.setattr(core, 'tmux_query', REAL_QUERY)
    if operation == 'launch':
        assert world.launch().outcome == window.UNAVAILABLE
    else:
        world.claudes = {'%1'}
        with core.raise_on_die(), pytest.raises(core.CCMError, match='Cannot read'):
            exiting.cmd_exit(['alpha'])
    assert not world.typed()


@pytest.mark.parametrize('manual', [False, True])
def test_launch_capture_restore_preserves_main_sidekick_and_manual(world, monkeypatch, tmp_path, manual):
    import ccm_layout
    import ccm_restore
    import ccm_snapshot_store as store
    from snapshot_fixture import layout

    world.raw = {'%1': reservation(), '%2': reservation('sidekick', True, 'codex')}
    if manual:
        roles.cmd_roles(['%1', '--clear'])
        assert world.launch().outcome == window.UNAVAILABLE
    else:
        assert world.launch().pane == '%1'
    shape = layout('100x30,0,0{49x30,0,0,1,50x30,50,0,2}')
    def inventory(command, *a, **kw):
        if command == 'list-windows':
            return f'$1\t@1\t0\talpha\t/tmp/alpha\t{shape}\t100\t30\t0\t2\tIDLE\t\tEND'
        assert command == 'list-panes'
        return '\n'.join(f'@1\t{pid}\t{i}\t{100+i}\tsh\t/tmp/alpha\t\t{int(i==0)}\t30\t{raw}\tEND'
                         for i, (pid, raw) in enumerate(world.raw.items()))
    with monkeypatch.context() as capture:
        capture.setattr(core, 'tmux_query', inventory)
        capture.setattr(core, 'ps_snapshot', lambda: '100 1 100 sh 00:10\n201 100 100 claude 00:10\n101 1 101 sh 00:10')
        capture.setattr(core, 'CCM_SNAPSHOT_DIR', str(tmp_path/'snapshots'))
        data = store.collect('_autosave')
        with store.locked():
            store.write('_autosave', data)
        project = store.read(store.directory()/'_autosave.json')['projects'][0]
    saved = project['restore']
    assert saved['primary_claude_slot'] == (None if manual else 0)
    assert saved['panes'][0]['role'] == ('manual' if manual else 'primary')
    assert saved['panes'][1]['agent'] == 'codex' and saved['panes'][1]['ignore'] is True
    restored = {}
    def run(*a):
        if a[0] == 'set-option':
            restored[a[a.index('-t')+1]] = a[-1]
        if a[0] == 'show-option':
            return restored[a[a.index('-t')+1]]
        if a[0] == 'display-message':
            return '%7\t0' if '\t' in a[-1] else '0'
        return ''
    # Geometry is covered by isolated tmux integration; keep the actual role
    # assignment, verification, serialization and later launch policy here.
    monkeypatch.setattr(ccm_restore, 'run', run)
    monkeypatch.setattr(ccm_restore, 'stable_panes', lambda *a: None)
    monkeypatch.setattr(ccm_restore, '_verify', lambda *a: None)
    monkeypatch.setattr(ccm_restore, 'save_job', lambda *a: None)
    state = {'ready': False, 'next': 0, 'ops': [], 'leaves': {'1':'a','2':'b'},
             'keys': {'a':'%7', 'b':'%8'}, 'tree': ccm_layout.parse(shape)}
    ccm_restore.restore_window({'window_id':'@2'}, project, state, {})
    assert state['ready']
    assert roles.decode(restored['%8']) == {'role':'sidekick','agent':'codex','ignore':True}
    world.raw = restored
    world.active = '%8'
    world.calls.clear()
    result = world.launch()
    assert result.pane == (None if manual else '%7')
    assert all(a[2] != '%8' for a in world.typed())


def test_observed_agent_replaces_stale_intent_without_leaving_missing_role(world):
    world.raw = {'%1': reservation('sidekick', agent='codex')}
    roles.reconcile('%1', world.raw['%1'], 'claude')
    assert roles.decode(world.raw['%1']) == {'role':'unknown','agent':'claude','ignore':False}
    assert world.launch().pane == '%1'
    roles.reconcile('%1', world.raw['%1'], 'codex')
    assert world.launch().outcome == window.UNAVAILABLE


def test_dashboard_displays_role_failure_reason(world, monkeypatch):
    import dashboard
    sent = []
    monkeypatch.setattr(dashboard, 'tmux_cmd', lambda *a: sent.append(a))
    world.raw = {'%1': None}
    result = world.launch()
    dashboard._announce_unlaunched(result)
    assert 'ccm roles' in sent[-1][-1] and 'Cannot read' in sent[-1][-1]


def test_exit_reads_roles_once_and_never_loses_sidekick(world):
    world.raw = {'%1': [reservation('sidekick', agent='claude'), None]}
    world.claudes = {'%1'}
    with core.raise_on_die(), pytest.raises(core.CCMError):
        exiting.cmd_exit(['alpha'])
    assert len([a for a in world.calls if a[-1] == roles.ROLE_OPTION]) == 1
    assert not world.typed()


def test_two_shells_with_active_editor_are_not_guessed(world):
    world.raw = {'%1': reservation(), '%2': reservation(), '%3': reservation('unknown')}
    world.active = '%3'
    world.commands['%3'] = 'vim'
    assert world.launch().outcome == window.UNAVAILABLE
    assert not world.typed()


@pytest.mark.parametrize('has_primary', [False, True])
@pytest.mark.parametrize('command', ['sh', 'codex', 'claude'])
def test_added_pane_without_reservation_does_not_block_window(world, has_primary, command):
    world.raw = {'%1': reservation('primary' if has_primary else 'shell'), '%2': ''}
    world.active = '%2'
    world.commands['%2'] = command
    if command == 'claude':
        world.claudes = {'%2'}
    choice = roles.selection('@1', world.panes())
    assert choice.state == (roles.SelectionState.PRIMARY if has_primary else roles.SelectionState.NO_PRIMARY)
    assert ('%2' in choice.eligible) is (command != 'codex')


@pytest.mark.parametrize('has_primary', [False, True])
@pytest.mark.parametrize('command', ['sh', 'codex'])
def test_unreserved_pane_launch_obeys_primary_and_current_command(world, has_primary, command):
    world.raw = {'%1': reservation('primary' if has_primary else 'shell'), '%2': ''}
    world.active = '%2'
    world.commands['%2'] = command
    result = world.launch()
    expected = '%1' if has_primary or command == 'codex' else '%2'
    assert result.outcome == window.LAUNCHED
    assert result.pane == expected
    assert [a[2] for a in world.typed()] == [expected]


@pytest.mark.parametrize('has_primary', [False, True])
def test_unreserved_claude_exit_obeys_primary(world, has_primary):
    world.raw = {'%1': reservation('primary' if has_primary else 'shell'), '%2': ''}
    world.claudes = {'%1', '%2'} if has_primary else {'%2'}
    with core.raise_on_die():
        exiting.cmd_exit(['alpha'])
    assert [a[2] for a in world.typed()] == ['%1' if has_primary else '%2']


@pytest.mark.parametrize('command', ['codex', 'kimi', 'grok', 'gemini'])
def test_unreserved_external_agent_is_never_exit_candidate(world, command):
    world.raw = {'%1': ''}
    world.commands['%1'] = command
    # Even a detected Claude child must not redirect exit into an external agent.
    world.claudes = {'%1'}
    with core.raise_on_die(), pytest.raises(core.CCMError):
        exiting.cmd_exit(['alpha'])
    assert not world.typed()


def test_old_reconcile_empty_role_recovers_shell_without_writing_intent(world):
    world.raw = {'%1': ''}
    assert roles.reconcile('%1', '', 'codex') is None
    assert world.raw == {'%1': ''}
    assert world.launch().pane == '%1'
    assert not any(a[0] == 'set-option' for a in world.calls)


def test_unreserved_live_ignore_still_excludes_pane(world):
    world.raw = {'%1': ''}
    world.ignored = {'%1'}
    assert roles.selection('@1', world.panes()).eligible == frozenset()


def test_roles_distinguishes_reservations_without_claiming_origin(world, capsys):
    world.raw = {'%1': reservation(), '%2': '', '%3': '{broken'}
    roles.cmd_roles(['@1'])
    lines = capsys.readouterr().out.splitlines()
    assert 'reserved: shell' in lines[0]
    assert 'no reservation:' in lines[1]
    assert 'invalid role;' in lines[2]
    assert 'added after restore' not in '\n'.join(lines)
