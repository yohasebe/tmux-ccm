"""Synthetic Codex hooks: no real model, settings, tmux or spool."""
import json
from pathlib import Path

import pytest

import ccm_commands
import ccm_core


def test_codex_hook_setup_supported(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    result = None
    try:
        ccm_commands.cmd_setup_sidekick_hooks('codex')
    except SystemExit as exc:
        result = exc.code
    assert result is None, 'Codex hook setup must be supported'
    data = json.loads((tmp_path / 'hooks.json').read_text())
    assert 'PermissionRequest' in data['hooks']
    assert 'Stop' in data['hooks']

from types import SimpleNamespace

import ccm_sidekick_notify as notices
import ccm_presentation
import ccm_spool
import ccm_send
import ccm_notify
from ccm_pane_state import PaneInfo

REAL_BODY_LANDED = ccm_send._body_landed
REAL_SEND_KEYS = ccm_send._send_keys  # before any fixture replaces it


@pytest.fixture
def world(tmp_path, monkeypatch):
    directory = tmp_path / 'demo'
    directory.mkdir()
    monkeypatch.setattr(ccm_core, 'CCM_ATTENTION_DIR', str(tmp_path / 'attention'))
    monkeypatch.setenv('TMUX_PANE', '%999')
    monkeypatch.chdir(tmp_path)  # Neither environment nor process cwd identifies the source.
    state = {'windows': f'@1\tdemo\t{directory}', 'now': 10000,
             'options': {notices.OPTION: 'on'}, 'source_birth': 'birth-A',
             'panes': [PaneInfo('%1', '100', True, 'claude', False, '101'),
                       PaneInfo('%2', '200', False, 'codex', False, None)],
             'keys': [], 'bodies': [], 'desktop': []}

    def query(*args, **kwargs):
        if args[0] == 'list-windows':
            return state['windows']
        if args[0] == 'display-message':
            return 'server-example:1:socket-example'
        if args[0] == 'show-options':
            return state['options'].get(args[-1], '')
        if args[0] == 'set-option':
            state['options'][args[-2]] = args[-1]
            return ''
        raise AssertionError(args)

    monkeypatch.setattr(ccm_core, 'tmux_query', query)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *args, **kw: '')
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '100 1 100 zsh\n101 100 100 claude\n200 1 200 zsh\n201 200 200 codex')
    monkeypatch.setattr(notices.ccm_pane_state, 'enumerate_window_panes', lambda *args: state['panes'])
    monkeypatch.setattr(notices, '_process_identity', lambda pids: [[p, state['source_birth'] if p in ('200','201') else 'target-birth'] for p in sorted(pids)])
    monkeypatch.setattr(notices.time, 'time', lambda: state['now'])
    monkeypatch.setattr(ccm_spool, '_deliverable_pane', lambda win, expected=None: ('%1', None))
    monkeypatch.setattr(ccm_send, '_send_keys', lambda *a, **k: state['keys'].append(a))
    monkeypatch.setattr(ccm_send, '_type_body', lambda pane, lines, **kw: state['bodies'].append('\n'.join(lines)))
    monkeypatch.setattr(ccm_send, 'held_after_submit', lambda *a, **k: False)
    monkeypatch.setattr(ccm_send, 'capture_composer_snapshot', lambda *a: ('readable', False))
    monkeypatch.setattr(ccm_send, '_body_landed', lambda *a: True)
    monkeypatch.setattr(ccm_notify, 'sidekick_attention', lambda *a: state['desktop'].append(a))

    def emit(event='Stop', turn='turn-one', **kwargs):
        payload = dict(cwd=str(directory), session_id='session-demo', turn_id=turn,
                       hook_event_name=event, **kwargs)
        notices.receive(payload)

    state.update(emit=emit, directory=directory)
    return state


def records():
    return [r for _, r in notices._notices(Path(ccm_spool.SPOOL_ROOT) / 'demo')]


def marker():
    return json.loads((Path(ccm_core.CCM_ATTENTION_DIR) / '%2.json').read_text())


def test_synthetic_approval_and_interrupt_sequences(world):
    events = json.loads((Path(__file__).parent / 'fixtures/codex-events.json').read_text())
    for i, event in enumerate(events):
        notices.receive(dict(event, cwd=str(world['directory'])))
        if i in (3, 8):
            assert marker()['state'] == 'waiting'
            assert marker()['pane'] == '%2'
            notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
        if i in (4, 9):
            assert marker()['state'] == 'resolved'
    assert len(records()) == 1  # Interrupt alone never invents a Stop.
    assert records()[0]['excerpt'] == 'The example is complete.'
    assert len(world['desktop']) == 2
    assert notices.BINDING in world['options']


@pytest.mark.parametrize('windows', ['', '@1\tdemo\t{cwd}\n@2\tother\t{cwd}', None])
def test_unmatched_or_ambiguous_cwd_has_no_artifact(world, windows):
    world['windows'] = windows.format(cwd=world['directory']) if windows else windows
    world['emit'](last_assistant_message='must not persist')
    assert not Path(ccm_spool.SPOOL_ROOT).exists()


@pytest.mark.parametrize('count', [0, 2])
def test_nonunique_codex_has_no_artifact(world, count):
    world['panes'] = world['panes'][:1] + [PaneInfo(f'%{n+2}', str(200+n), False, 'codex', False, None) for n in range(count)]
    world['emit']()
    assert not records()


@pytest.mark.parametrize('mode', ['ignored', 'two', 'none'])
def test_no_unique_nonignored_claude_no_completion(world, mode):
    if mode == 'ignored':
        world['panes'][0] = world['panes'][0]._replace(ignored=True)
    elif mode == 'two':
        world['panes'].append(PaneInfo('%3', '300', False, 'claude', False, '301'))
    else:
        world['panes'] = world['panes'][1:]
    world['emit']()
    assert not records()


def test_ignored_codex_still_counts_as_sidekick(world):
    world['panes'][1] = world['panes'][1]._replace(ignored=True)
    world['emit']()
    assert len(records()) == 1


def test_optin_off_still_allows_attention_but_no_claude_notice(world):
    world['options'][notices.OPTION] = 'off'
    world['emit']('PermissionRequest', tool_name='Bash')
    world['emit']()
    assert not records()
    assert marker()['state'] == 'resolved'


def test_unrelated_post_does_not_resolve_wait(world):
    world['emit']('PreToolUse', tool_name='Bash', tool_input={}, tool_use_id='right')
    world['emit']('PermissionRequest', tool_name='Bash', tool_input={})
    world['emit']('PostToolUse', tool_use_id='wrong')
    assert marker()['state'] == 'waiting'
    world['emit']('PostToolUse', tool_use_id='right')
    assert marker()['state'] == 'resolved'


def test_ambiguous_tool_id_waits_for_turn_end(world):
    for tid in ('one', 'two'):
        world['emit']('PreToolUse', tool_name='Bash', tool_input={}, tool_use_id=tid)
    world['emit']('PermissionRequest', tool_name='Bash', tool_input={})
    world['emit']('PostToolUse', tool_use_id='one')
    assert marker()['state'] == 'waiting'
    world['emit']('Interrupt')
    assert marker()['state'] == 'resolved'


def test_parallel_waits_preserve_other_tool(world):
    for tid in ('one', 'two'):
        world['emit']('PreToolUse', tool_name=tid, tool_use_id=tid)
        world['emit']('PermissionRequest', tool_name=tid)
    world['emit']('PostToolUse', tool_use_id='one')
    assert marker()['state'] == 'waiting'
    world['emit']('PostToolUse', tool_use_id='two')
    assert marker()['state'] == 'resolved'


def test_late_wait_after_interrupt_does_not_reopen(world):
    world['emit']('Interrupt')
    world['emit']('PermissionRequest', tool_name='Bash')
    assert not (Path(ccm_core.CCM_ATTENTION_DIR) / '%2.json').exists()
    world['emit']()
    assert not records()


def test_session_end_resolves_wait_without_completion(world):
    world['emit']('PermissionRequest', tool_name='Bash')
    world['emit']('SessionEnd')
    assert marker()['state'] == 'resolved'
    assert not records()


def test_duplicates_and_coalescing(world):
    world['emit'](last_assistant_message='old')
    world['emit'](last_assistant_message='duplicate')
    world['emit'](turn='turn-two', last_assistant_message='latest')
    pending = [r for r in records() if r['status'] == 'pending']
    assert len(pending) == 1
    assert pending[0]['count'] == 2
    assert pending[0]['excerpt'] == 'latest'
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['bodies']) == 1
    assert 'reply with' not in world['bodies'][0]
    assert 'no acknowledgement-only reply' in world['bodies'][0].lower()
    assert 'latest' in world['bodies'][0]


def test_expired_not_silently_coalesced(world):
    world['emit']()
    world['now'] += ccm_spool.SPOOL_TTL_SEC + 1
    world['emit'](turn='turn-two')
    assert sorted(r['status'] for r in records()) == ['expired', 'pending']
    assert any(r['kind'] == 'notice-expired' for r in ccm_spool.attention_records())


def test_normal_messages_have_priority(world):
    world['emit']()
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], {'demo'})
    assert not world['keys']
    assert records()[0]['status'] == 'pending'
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == 'delivered'


@pytest.mark.parametrize('reason', ['BUSY', 'PERMIT', 'SHELL', 'draft', 'capture failed'])
def test_unready_never_types(world, monkeypatch, reason):
    monkeypatch.setattr(ccm_spool, '_deliverable_pane', lambda *args: (None, reason))
    world['emit']()
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert not world['keys']
    assert records()[0]['status'] == 'pending'


def test_hourly_limit_records_loss_then_recovers(world):
    world['options'][notices.LIMIT] = '1'
    world['emit']()
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    world['emit'](turn='turn-two')
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert sorted(r['status'] for r in records()) == ['delivered', 'limited']
    assert 'notice-limited' in [r['kind'] for r in ccm_spool.attention_records()]
    world['now'] += 3601
    world['emit'](turn='turn-three')
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['bodies']) == 2


@pytest.mark.parametrize('mode', ['exception', 'crash', 'held'])
def test_uncertain_and_held_never_retry(world, monkeypatch, mode):
    world['emit']()
    if mode == 'exception':
        def fail(*a, **kw):
            raise OSError('unknown result')
        monkeypatch.setattr(ccm_send, '_type_body', fail)
    elif mode == 'crash':
        path, record = next(notices._notices(Path(ccm_spool.SPOOL_ROOT) / 'demo'))
        record['status'] = 'attempted'
        notices._write(path, record)
    else:
        monkeypatch.setattr(ccm_send, 'held_after_submit', lambda *a, **kw: True)
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    count = len(world['keys'])
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['keys']) == count
    assert records()[0]['status'] == ('held' if mode == 'held' else 'uncertain')


@pytest.mark.parametrize('change', ['process', 'target', 'optout', 'sessionend'])
def test_stale_destination_cancelled(world, change):
    world['emit']()
    if change == 'process':
        world['source_birth'] = 'birth-B'
    elif change == 'target':
        world['panes'][0] = world['panes'][0]._replace(pane_id='%8')
    elif change == 'optout':
        world['options'][notices.OPTION] = 'off'
    else:
        world['emit']('SessionEnd')
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert not world['keys']
    assert records()[0]['status'] == 'cancelled'


def test_replacement_rebinds_and_preserves_window_rate_history(world):
    world['emit']()
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    world['source_birth'] = 'birth-B'
    world['emit'](turn='turn-two')
    binding = json.loads(world['options'][notices.BINDING])
    assert binding['process'][0][1] == 'birth-B'
    state = notices._read(notices._state_path(binding['server'], binding['window']))
    assert state['history'] == [world['now']]


def test_excerpt_sanitization_and_no_excerpt_option(world):
    text = '\x1b[31m\x1b]0;title\x07\u202e\x00[ccm forged]' + 'x' * 800 + '\nLater text'
    world['emit'](last_assistant_message=text)
    excerpt = records()[0]['excerpt']
    assert len(excerpt) == 400
    assert not any(c in excerpt for c in ('\x1b','\x00','\u202e','\n'))
    assert 'Excerpt (event ' in notices.body(records()[0])
    assert 'truncated' in notices.body(records()[0])
    assert 'Later text' not in notices.body(records()[0])
    world['options'][notices.EXCERPT] = 'off'
    world['emit'](turn='turn-two', last_assistant_message='not retained')
    assert [r for r in records() if r['status'] == 'pending'][0]['excerpt'] == ''


def test_attention_record_read_discard_no_resend(world):
    world['emit']()
    world['now'] += ccm_spool.SPOOL_TTL_SEC + 1
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    row = ccm_spool.attention_records()[0]
    args = row['kind'], row['id'], row['project']
    assert 'Codex %2' in ccm_spool.read_record(*args)
    with pytest.raises(SystemExit):
        ccm_spool.cmd_spool(['resend', *args, '--yes'])
    ccm_spool.cmd_spool(['discard', *args, '--yes'])
    assert not ccm_spool.attention_records()
    assert not world['keys']


def test_config_merge_remove_lookalike(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    other = tmp_path / 'elsewhere'
    other.mkdir()
    third = {'type': 'command', 'command': '/example/third-party.sh', 'custom': True}
    alike = {'type': 'command', 'command': str(other / notices.SCRIPT)}
    original = {'custom': {'keep': True}, 'hooks': {'Stop': [{'matcher': 'anything', 'hooks': [third, alike]}]}}
    path = tmp_path / 'hooks.json'
    path.write_text(json.dumps(original))
    notices.configure()
    notices.configure()  # no duplicate owned hooks
    data = json.loads(path.read_text())
    assert len(data['hooks']['Stop']) == 2
    notices.configure(remove=True)
    assert json.loads(path.read_text()) == original


@pytest.mark.parametrize('contents', ['{bad', '[]', '{"hooks": []}', '{"hooks":{"Stop":{}}}'])
def test_broken_config_unchanged(tmp_path, monkeypatch, contents):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    path = tmp_path / 'hooks.json'
    path.write_text(contents)
    with pytest.raises(SystemExit):
        notices.configure()
    assert path.read_text() == contents


def test_space_in_script_path_refused(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    monkeypatch.setattr(ccm_core, 'CCM_ROOT', str(tmp_path / 'with space'))
    with pytest.raises(SystemExit):
        notices.configure()
    assert not (tmp_path / 'hooks.json').exists()


def test_doctor_describes_reception_not_trust(world, monkeypatch, tmp_path):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    world['emit']()
    text = '\n'.join(notices.doctor_lines([]))
    assert 'pending=1' in text
    assert 'not proof of current trust' in text
    assert 'not installed' in text


def test_busy_project_lock_defers_events_in_order(world, monkeypatch):
    acquire = ccm_spool._acquire_lock
    monkeypatch.setattr(ccm_spool, '_acquire_lock', lambda path: False)
    world['emit']('PreToolUse', tool_name='Bash', tool_input={'command': 'echo token=hidden'}, tool_use_id='right')
    world['emit']('PermissionRequest', tool_name='Bash', tool_input={'command': 'echo token=hidden'})
    world['emit']('PostToolUse', tool_use_id='right')
    world['emit'](last_assistant_message='finished')
    inbox = Path(ccm_spool.SPOOL_ROOT) / '.sidekick' / 'inbox'
    assert len(list(inbox.glob('*.json'))) == 4
    assert all('token=hidden' not in p.read_text() for p in inbox.glob('*.json'))
    monkeypatch.setattr(ccm_spool, '_acquire_lock', acquire)
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert marker()['state'] == 'resolved'
    assert len(world['bodies']) == 1
    assert not list(inbox.glob('*.json'))


def test_deferred_notice_keeps_original_expiry(world, monkeypatch):
    acquire = ccm_spool._acquire_lock
    monkeypatch.setattr(ccm_spool, '_acquire_lock', lambda path: False)
    world['emit']()
    monkeypatch.setattr(ccm_spool, '_acquire_lock', acquire)
    world['now'] += ccm_spool.SPOOL_TTL_SEC + 1
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == 'expired'
    assert not world['keys']


def test_session_switch_without_session_start_still_notifies(world):
    world['emit']('SessionStart')
    notices.receive({'cwd': str(world['directory']), 'hook_event_name': 'Stop',
                     'session_id': 'switched', 'turn_id': 'switched-turn'})
    assert [r['binding']['session'] for r in records()] == ['switched']
    assert json.loads(world['options'][notices.BINDING])['session'] == 'switched'


def test_late_event_from_left_session_is_ignored(world):
    world['emit']('SessionStart')
    notices.receive({'cwd': str(world['directory']), 'hook_event_name': 'UserPromptSubmit',
                     'session_id': 'switched', 'turn_id': 'switched-turn'})
    world['emit']('Stop', turn='late-turn')  # the session this process already left
    assert not records()
    assert json.loads(world['options'][notices.BINDING])['session'] == 'switched'


def test_binding_option_written_only_on_change(world):
    writes = []
    query = notices.ccm_core.tmux_query

    def counting(*args, **kwargs):
        if args[0] == 'set-option':
            writes.append(args)
        return query(*args, **kwargs)

    notices.ccm_core.tmux_query = counting
    try:
        world['emit']('SessionStart')
        world['emit']('UserPromptSubmit')
        world['emit']('Stop')
    finally:
        notices.ccm_core.tmux_query = query
    assert len(writes) == 1


def test_project_busy_even_if_raw_pane_ready(world):
    world['emit']()
    notices.reconcile([SimpleNamespace(name='demo', state='BUSY')], set())
    assert not world['keys']


def test_normal_message_arriving_after_initial_scan_has_priority(world):
    world['emit']()
    ccm_spool.enqueue('demo', 'sender', 'ordinary')
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert not world['keys']


def test_unreadable_post_send_capture_is_uncertain(world, monkeypatch):
    world['emit']()
    monkeypatch.setattr(ccm_send, 'capture_composer_snapshot', lambda *a: ('', False))
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == 'uncertain'


def test_checked_send_keys_rejects_unconfirmed_tmux(monkeypatch):
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *a, **kw: None)
    with pytest.raises(OSError):
        ccm_send._send_keys('%1', 'Enter', checked=True)


def test_checked_body_propagates_failure(monkeypatch):
    calls = []
    def query(*args, **kwargs):
        calls.append(args)
        return None
    monkeypatch.setattr(ccm_core, 'tmux_query', query)
    with pytest.raises(OSError):
        ccm_send._type_body('%1', ['line one', 'line two'], checked=True)
    assert len(calls) == 1


def test_uninstall_resolves_wait_cancels_pending_and_disables_loaded_hooks(world, tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    notices.configure()
    world['emit']()
    world['emit']('PermissionRequest', turn='turn-two', tool_name='Bash')
    notices.configure(remove=True)
    assert marker()['state'] == 'resolved'
    assert records()[0]['status'] == 'cancelled'
    world['emit'](turn='turn-three')
    assert len(records()) == 1


def test_actual_shared_readiness_rejects_two_claudes(monkeypatch):
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '')
    panes = [PaneInfo('%1', '100', True, 'claude', False, '101'),
             PaneInfo('%3', '300', False, 'claude', False, '301')]
    monkeypatch.setattr(ccm_spool, 'enumerate_window_panes', lambda *a: panes)
    pane, reason = ccm_spool._deliverable_pane('@1', '%1')
    assert pane is None
    assert 'changed' in reason


@pytest.mark.parametrize('raw', [None, '', '200 Sep 1 00:00:00 2026\n'])
def test_process_birth_requires_all_pids(monkeypatch, raw):
    if raw is None:
        def denied(*a, **kw):
            raise PermissionError('denied')
        monkeypatch.setattr(notices.subprocess, 'run', denied)
    else:
        monkeypatch.setattr(notices.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=raw))
    assert notices._process_identity(['200', '201']) is None


def test_process_birth_stable_and_complete(monkeypatch):
    def run(*args, **kwargs):
        assert kwargs['env']['TZ'] == 'UTC'
        return SimpleNamespace(returncode=0, stdout='200 Wed Sep 1 00:00:00 2026\n201 Wed Sep 1 00:00:01 2026\n')
    monkeypatch.setattr(notices.subprocess, 'run', run)
    assert notices._process_identity(['200', '201']) == [
        ['200', 'Wed Sep 1 00:00:00 2026'], ['201', 'Wed Sep 1 00:00:01 2026']]


def test_hook_main_always_silent_on_malformed_input(monkeypatch, capsys):
    import io
    monkeypatch.setattr('sys.stdin', SimpleNamespace(buffer=io.BytesIO(b'{invalid')))
    notices.hook_main()
    assert capsys.readouterr() == ('', '')


def test_hook_main_caps_payload(monkeypatch):
    import io
    monkeypatch.setattr('sys.stdin', SimpleNamespace(buffer=io.BytesIO(b'x' * (256 * 1024 + 1))))
    monkeypatch.setattr(notices, 'receive', lambda *a: pytest.fail('oversize input must be ignored'))
    notices.hook_main()


def test_periodic_spool_driver_delivers_typed_notice(world):
    world['emit']()
    ccm_spool.reconcile_spools([SimpleNamespace(name='demo', state='IDLE')])
    assert records()[0]['status'] == 'delivered'


def test_rechecking_destination_after_readiness_prevents_typing(world, monkeypatch):
    world['emit']()
    def ready(*args):
        world['options'][notices.OPTION] = 'off'
        return '%1', None
    monkeypatch.setattr(ccm_spool, '_deliverable_pane', ready)
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert not world['keys']


def test_attention_toggle_suppresses_desktop_not_marker(world, monkeypatch):
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *args, **kw: 'off')
    world['emit']('PermissionRequest', tool_name='Bash')
    notices.reconcile([], set())
    assert marker()['state'] == 'waiting'
    assert not world['desktop']


def test_marker_v1_is_read_by_existing_reader(world):
    world['emit']('PermissionRequest', tool_name='Bash')
    panes = [('example:0', '200', '%2', 'codex', '0', '40', '')]
    result = ccm_core._read_attention_markers(panes, [])
    assert result['%2']['agent'] == 'codex'
    assert result['%2']['state'] == 'waiting'


def test_wait_identity_preserved_on_duplicate_and_resolution(world):
    world['emit']('PreToolUse', tool_name='Bash', tool_use_id='id')
    world['emit']('PermissionRequest', tool_name='Bash')
    before = marker()['id']
    world['emit']('PermissionRequest', tool_name='Bash')
    assert marker()['id'] == before
    world['emit']('PostToolUse', tool_use_id='id')
    assert marker()['id'] == before
    assert marker()['state'] == 'resolved'


def test_config_external_edit_detected_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    path = tmp_path / 'hooks.json'
    path.write_text('{}')
    import fcntl
    def concurrent_change(*args):
        path.write_text('{"external":true}')
    monkeypatch.setattr(fcntl, 'flock', concurrent_change)
    with pytest.raises(SystemExit):
        notices.configure()
    assert json.loads(path.read_text()) == {'external': True}


def test_corrupt_notification_is_not_executed(world):
    world['emit']()
    path = next((Path(ccm_spool.SPOOL_ROOT) / 'demo').glob('*.notice'))
    path.write_text('{"version":1}')
    notices.reconcile([], set())
    assert not world['keys']


def test_coalescing_during_lock_acquisition_never_sends_old_snapshot(world, monkeypatch):
    world['emit'](last_assistant_message='obsolete')
    real_acquire = ccm_spool._acquire_lock
    triggered = []
    def acquire(path):
        if not triggered:
            triggered.append(True)
            world['emit'](turn='turn-new', last_assistant_message='latest')
        return real_acquire(path)
    monkeypatch.setattr(ccm_spool, '_acquire_lock', acquire)
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['bodies']) == 1
    assert 'latest' in world['bodies'][0]
    assert 'obsolete' in world['bodies'][0]
    assert world['bodies'][0].index('obsolete') < world['bodies'][0].index('latest')


def test_dashboard_u_shows_limited_notice_without_resend(world, monkeypatch):
    world['options'][notices.LIMIT] = '0'
    world['emit']()
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    from test_undelivered import make_dashboard
    d, screen = make_dashboard(monkeypatch, [ord('r'), ord('q')])
    monkeypatch.setattr(d, '_prompt', lambda *a: pytest.fail('No resend for an automatic notice'))
    d._do_spool(screen)
    text = ' '.join(str(c.args) for c in screen.addstr.call_args_list)
    assert ccm_presentation.record_spec('notice-limited').label in text
    assert 'notice-limited' not in text
    assert 'r resend' not in text


def test_dashboard_u_reads_notification(world, monkeypatch):
    world['options'][notices.LIMIT] = '0'
    world['emit'](last_assistant_message='quoted result')
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    from test_undelivered import make_dashboard
    d, screen = make_dashboard(monkeypatch, [13, ord('q')])
    shown = []
    monkeypatch.setattr(d, '_spool_text', lambda screen, title, body: shown.append(body))
    d._do_spool(screen)
    assert 'quoted result' in shown[0]
    assert 'Status: limited' in shown[0]


def test_new_session_start_rebinds_without_manual_operation(world):
    world['emit']('SessionStart')
    payload = {'cwd': str(world['directory']), 'session_id': 'next-session'}
    notices.receive(dict(payload, hook_event_name='SessionStart'))
    notices.receive(dict(payload, hook_event_name='Stop', turn_id='next-turn'))
    assert records()[0]['binding']['session'] == 'next-session'
    world['emit']('SessionEnd')  # delayed event from the old session
    assert json.loads(world['options'][notices.BINDING])['session'] == 'next-session'



def test_missing_notification_id_in_capture_is_uncertain(world, monkeypatch):
    world['emit']()
    monkeypatch.setattr(ccm_send, '_body_landed', lambda *a: False)
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == 'uncertain'


def test_delivery_survives_cancel_outside_copy_mode(world, monkeypatch):
    """tmux fails `send-keys -X cancel` when the pane is not in a mode."""
    monkeypatch.setattr(ccm_send, '_send_keys', REAL_SEND_KEYS)
    sent = []
    query = ccm_core.tmux_query

    def tmux(*args, **kwargs):
        if args[0] == 'send-keys':
            if '-X' in args:
                return None  # "not in a mode", exit status 1
            sent.append(args)
            return ''
        return query(*args, **kwargs)

    monkeypatch.setattr(ccm_core, 'tmux_query', tmux)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a, **k: '')
    world['emit']()
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == 'delivered'
    assert any('Enter' in a for a in sent)


def pending_notices():
    return sorted((r for r in records() if r['status'] == 'pending'), key=lambda r: r['created'])


def test_three_turns_preserve_first_lines_on_recoalescing(world):
    heads = ['Request A complete: result-a.md', 'Request B complete: result-b.md',
             'Request C complete: result-c.md']
    for n, head in enumerate(heads):
        world['now'] += 1
        world['emit'](turn=f'turn-{n}', last_assistant_message=head + '\nOther details')
    pending = pending_notices()
    assert len(pending) == 1 and pending[0]['count'] == 3
    text = notices.body(pending[0])
    # Also runs on the original implementation as an AssertionError control.
    assert all(head in text for head in heads)
    assert [i['excerpt'] for i in pending[0]['items']] == heads
    assert len({i['id'] for i in pending[0]['items']}) == 3
    assert text.index(heads[0]) < text.index(heads[1]) < text.index(heads[2])
    assert 'Other details' not in text
    world['emit'](turn='turn-1', last_assistant_message='Duplicate should not replace evidence')
    assert pending_notices() == pending


@pytest.mark.parametrize('line,total', [('Brief result', 19), ('結果' * 200, 5),
                                       ('"\\' * 200, 7), ('😀' * 400, 5)])
def test_overflow_stays_pending_and_every_batch_delivers(world, line, total):
    for n in range(total):
        world['now'] += 1
        world['emit'](turn=f'turn-{n}', last_assistant_message=line)
    pending = pending_notices()
    assert len(pending) > 1
    ids = [i['id'] for r in pending for i in r['items']]
    assert len(ids) == len(set(ids)) == total
    assert all(r['count'] <= 8 and len(notices.body(r).encode('utf-8')) <= 4096 for r in pending)
    for _ in pending:
        notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['bodies']) == len(pending)
    assert not pending_notices()
    assert all(sum(f'Excerpt (event {event[:12]})' in b for b in world['bodies']) == 1 for event in ids)


def test_coalescing_preserves_earliest_expiry(world):
    world['emit'](last_assistant_message='Earlier')
    first = pending_notices()[0]
    world['now'] += 30
    world['emit'](turn='turn-two', last_assistant_message='Later')
    record = pending_notices()[0]
    assert record['expires'] == first['expires']
    assert record['items'][1]['expires'] == first['expires'] + 30
    world['now'] = first['expires']
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert not world['keys'] and not pending_notices()
    assert any(r['status'] == 'expired' for r in records())


@pytest.mark.parametrize('status', ['attempted', 'held', 'uncertain', 'limited', 'cancelled', 'delivered'])
def test_terminal_and_attempted_records_are_not_requeued(world, status):
    world['emit'](last_assistant_message='Earlier')
    path, record = next(notices._notices(Path(ccm_spool.SPOOL_ROOT) / 'demo'))
    record['status'] = status
    notices._write(path, record)
    world['emit'](turn='turn-two', last_assistant_message='Later')
    assert pending_notices()[0]['count'] == 1
    assert 'Earlier' not in notices.body(pending_notices()[0])
    assert notices._read(path)['status'] == status


def test_binding_change_does_not_merge_reports(world):
    world['emit'](last_assistant_message='Earlier')
    world['source_birth'] = 'birth-B'
    world['emit'](turn='turn-two', last_assistant_message='Later')
    assert len(pending_notices()) == 2
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['bodies']) == 1 and 'Earlier' not in world['bodies'][0]
    assert any(r['status'] == 'cancelled' for r in records())


@pytest.mark.parametrize('deferred', [False, True])
def test_redaction_before_first_line_and_length_limits(world, monkeypatch, deferred):
    secret = 'sk-' + 'synthetic' * 9
    monkeypatch.setenv('CCM_TEST_TOKEN', secret)
    # Exact environment values are redacted even across a line boundary.
    multiline = 'synthetic-first\nsynthetic-second'
    monkeypatch.setenv('CCM_TEST_PASSWORD', multiline)
    acquire = ccm_spool._acquire_lock
    if deferred:
        monkeypatch.setattr(ccm_spool, '_acquire_lock', lambda path: False)
    text = 'x' * 390 + secret + '\n' + 'Discard this second line'
    world['emit'](last_assistant_message=text)
    world['now'] += 1
    world['emit'](turn='turn-two', last_assistant_message='Result ' + multiline + ' tail\nHidden details')
    if deferred:
        inbox = Path(ccm_spool.SPOOL_ROOT) / '.sidekick' / 'inbox'
        saved = ''.join(p.read_text() for p in inbox.glob('*.json'))
        assert secret not in saved and 'synthetic-first' not in saved
        assert 'Discard this second line' not in saved
        monkeypatch.setattr(ccm_spool, '_acquire_lock', acquire)
        notices._drain_inbox()
    record = pending_notices()[0]
    assert record['items'][0]['excerpt'] == 'x' * 390 + '[redacted]'
    assert record['items'][1]['excerpt'] == 'Result [redacted] tail'
    assert secret not in notices.body(record)
    assert 'Hidden details' not in notices.body(record)


@pytest.mark.parametrize('deferred', [False, True])
def test_truncation_marks_partial_location_and_defer_keeps_flag(world, monkeypatch, deferred):
    acquire = ccm_spool._acquire_lock
    if deferred:
        monkeypatch.setattr(ccm_spool, '_acquire_lock', lambda path: False)
    world['emit'](last_assistant_message='Result: ' + 'x' * 500 + '.md\nLater details')
    if deferred:
        monkeypatch.setattr(ccm_spool, '_acquire_lock', acquire)
        notices._drain_inbox()
    record = pending_notices()[0]
    assert len(record['items'][0]['excerpt']) == 400
    text = notices.body(record)
    assert 'truncated' in text and 'full text/location' in text
    assert 'Later details' not in text
    assert len(text.encode('utf-8')) <= 4096


@pytest.mark.parametrize('deferred', [False, True])
def test_excerpt_off_suppresses_all_heads(world, monkeypatch, deferred):
    world['emit'](last_assistant_message='Earlier result')
    world['options'][notices.EXCERPT] = 'off'
    acquire = ccm_spool._acquire_lock
    if deferred:
        monkeypatch.setattr(ccm_spool, '_acquire_lock', lambda path: False)
    world['emit'](turn='turn-two', last_assistant_message='Later result')
    monkeypatch.setattr(ccm_spool, '_acquire_lock', acquire)
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['bodies']) == 1
    assert 'Earlier result' not in world['bodies'][0] and 'Later result' not in world['bodies'][0]
    assert 'Excerpt' not in world['bodies'][0]


def test_excerpt_disabled_after_enqueue_also_hides_old_heads(world):
    world['emit'](last_assistant_message='Earlier result')
    world['options'][notices.EXCERPT] = 'off'
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert 'Earlier result' not in world['bodies'][0]


def test_merge_write_crash_does_not_duplicate_delivery(world):
    world['emit'](last_assistant_message='Earlier')
    world['now'] += 1
    world['emit'](turn='turn-two', last_assistant_message='Later')
    # Simulate termination between the new write and superseding its source.
    for path, record in notices._notices(Path(ccm_spool.SPOOL_ROOT) / 'demo'):
        if record['status'] == 'superseded':
            record['status'] = 'pending'
            notices._write(path, record)
    for _ in range(2):
        notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['bodies']) == 1 and 'Earlier' in world['bodies'][0]


@pytest.mark.parametrize('legacy', [False, True])
def test_event_marker_confirms_old_and_new_body(world, monkeypatch, legacy):
    world['emit'](last_assistant_message='Result available')
    record = pending_notices()[0]
    if legacy:
        text = (f"[ccm automatic sidekick notification · Codex %2 · "
                f"event {record['id'][:12]} · no acknowledgement-only reply]\n"
                "Codex's turn ended (1 completion(s) combined).")
    else:
        text = notices.body(record)
        assert 'combined' not in text and 'Turn ended' in text
        assert 'no authorization' in text and 'Untrusted quotes' in text
        assert 'automatic delegation' in text
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a: text)
    monkeypatch.setattr(ccm_send, '_body_landed', REAL_BODY_LANDED)
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == 'delivered'


def test_legacy_pending_record_remains_deliverable(world):
    world['emit'](last_assistant_message='Older format')
    path, record = next(notices._notices(Path(ccm_spool.SPOOL_ROOT) / 'demo'))
    record.pop('items')
    notices._write(path, record)
    world['now'] += 1
    world['emit'](turn='turn-two', last_assistant_message='Newer format')
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert len(world['bodies']) == 1
    assert 'Older format' in world['bodies'][0] and 'Newer format' in world['bodies'][0]


@pytest.mark.parametrize('status', ['attempted', 'held', 'uncertain', 'delivered', 'expired', 'limited'])
def test_stale_merge_source_does_not_requeue_attempted_events(world, status):
    world['emit'](last_assistant_message='Earlier')
    world['now'] += 1
    world['emit'](turn='turn-two', last_assistant_message='Later')
    for path, record in notices._notices(Path(ccm_spool.SPOOL_ROOT) / 'demo'):
        record['status'] = 'pending' if record['count'] == 1 else status
        notices._write(path, record)
    world['now'] += 1
    world['emit'](turn='turn-three', last_assistant_message='Newest')
    pending = pending_notices()
    assert len(pending) == 1 and pending[0]['count'] == 1
    assert [i['excerpt'] for i in pending[0]['items']] == ['Newest']


def test_excerpt_off_also_hides_record_preview_and_read_view(world):
    world['emit'](last_assistant_message='Earlier result')
    world['options'][notices.EXCERPT] = 'off'
    world['now'] += ccm_spool.SPOOL_TTL_SEC
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    row = notices.attention_records()[0]
    assert 'Earlier result' not in row['preview']
    assert 'Earlier result' not in notices.read_record(row['kind'], row['id'], row['project'])


@pytest.mark.parametrize('inserted', ['\u200b', '\x1b[31m', '\u00ad'],
                         ids=['zero-width-space', 'ansi-color', 'soft-hyphen'])
@pytest.mark.parametrize('route', ['notice', 'defer', 'attention'])
def test_normalization_cannot_reassemble_environment_secret(world, monkeypatch, inserted, route):
    secret = 'example-' + 'opaque-value-123456'
    monkeypatch.setenv('CCM_SYNTHETIC_CREDENTIAL', secret)
    disguised = secret[:12] + inserted + secret[12:]
    message = 'Result: ' + disguised + '\nOther details'
    if route == 'attention':
        world['emit']('PermissionRequest', tool_name='Bash',
                      tool_input={'description': message})
        output = marker()['summary']
    else:
        acquire = ccm_spool._acquire_lock
        if route == 'defer':
            monkeypatch.setattr(ccm_spool, '_acquire_lock', lambda path: False)
        world['emit'](last_assistant_message=message)
        if route == 'defer':
            inbox = Path(ccm_spool.SPOOL_ROOT) / '.sidekick' / 'inbox'
            saved = ''.join(p.read_text() for p in inbox.glob('*.json'))
            leaked_in_inbox = secret in saved
            assert not leaked_in_inbox, 'Credential appeared in deferred record'
            monkeypatch.setattr(ccm_spool, '_acquire_lock', acquire)
            notices._drain_inbox()
        output = notices.body(pending_notices()[0])
    # Keep even the expected failure in the negative control free of values.
    leaked = secret in output
    assert not leaked, 'Normalization reassembled an environment credential'
    assert '[redacted]' in output


def test_redaction_still_precedes_normalization_and_line_selection(monkeypatch):
    secret = 'example-' + 'first\nsecond-value'
    monkeypatch.setenv('CCM_SYNTHETIC_CREDENTIAL', secret)
    assert notices._first_line('Result: ' + secret + '\nLater') == {
        'text': 'Result: [redacted]', 'truncated': False}
