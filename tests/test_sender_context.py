"""Sender provenance must not turn inherited pane metadata into a reply route."""
import os

import pytest

import ccm_core
import ccm_send
import ccm_spool


@pytest.fixture
def context(tmp_path, monkeypatch):
    old = tmp_path / 'old'
    current = tmp_path / 'current'
    old.mkdir()
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv('TMUX_PANE', '%1')
    state = {'pane_dir': str(old), 'pane_name': 'old',
             'windows': f'@1\told\t{old}\n@2\tcurrent\t{current}'}

    def query(*args, **kwargs):
        if args[0] == 'display-message':
            if args[-1] == '#{window_id}':
                return '@1'
            return (f"%1\t{state['pane_name']}\t{state['pane_dir']}"
                    + ('\t100' if '#{pane_pid}' in args[-1] else ''))
        if args[0] == 'show-option':
            return state['pane_name'] if args[-1] == '@ccm_project' else state['pane_dir']
        if args[0] == 'list-windows':
            return state['windows']
        raise AssertionError(f'unexpected operation: {args}')
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '')
    monkeypatch.setattr(ccm_core, 'tmux_cmd', query)
    monkeypatch.setattr(ccm_core, 'tmux_query', query)
    return state, old, current


def test_stale_pane_uses_unique_cwd_project(context):
    assert ccm_send._sender_label() == 'current'


@pytest.mark.parametrize('kind', ['nested', 'duplicate', 'none', 'unreadable'])
def test_ambiguous_or_missing_project_is_unknown(context, kind):
    state, old, current = context
    if kind == 'nested':
        state['windows'] += f'\n@3\tparent\t{current.parent}'
    elif kind == 'duplicate':
        state['windows'] += f'\n@3\tcopy\t{current}'
    else:
        state['windows'] = None if kind == 'unreadable' else ''
    assert ccm_send._sender_label() == 'unknown'


def test_matching_pane_wins_over_nested_candidates(context):
    state, old, current = context
    state.update(pane_dir=str(current), pane_name='current')
    state['windows'] += f'\n@3\tparent\t{current.parent}'
    assert ccm_send._sender_label() == 'current'


def test_subdirectory_matches(context, monkeypatch):
    state, old, current = context
    child = current / 'child'
    child.mkdir()
    monkeypatch.chdir(child)
    assert ccm_send._sender_label() == 'current'


def test_symlink_is_same_directory(context):
    state, old, current = context
    link = current.parent / 'alias'
    link.symlink_to(current, target_is_directory=True)
    state.update(pane_dir=str(link), pane_name='current')
    assert ccm_send._sender_label() == 'current'


def test_sibling_prefix_is_not_containment(context, monkeypatch):
    state, old, current = context
    sibling = current.parent / 'current-other'
    sibling.mkdir()
    monkeypatch.chdir(sibling)
    assert ccm_send._sender_label() == 'unknown'


def test_linked_window_is_one_candidate(context):
    state, old, current = context
    state['windows'] += f'\n@2\tcurrent\t{current}'
    assert ccm_send._sender_label() == 'current'


def test_no_pane_can_still_resolve_project(context, monkeypatch):
    monkeypatch.delenv('TMUX_PANE')
    assert ccm_send._sender_label() == 'current'


def test_unknown_envelope_has_no_reply_command():
    text = ccm_spool._envelope('unknown', 100, 200)
    assert 'from: unknown' in text
    assert 'ccm send unknown' not in text
    assert 'confirm' in text.lower()


@pytest.mark.parametrize("bad", ["", "relative/path", "missing", "broken-link"])
def test_invalid_registered_directory_is_unknown(context, bad):
    state, old, current = context
    if bad == "missing":
        bad = str(current / 'missing')
    elif bad == "broken-link":
        link = current.parent / 'broken'
        link.symlink_to(current / 'missing')
        bad = str(link)
    state['windows'] = f'@2\tcurrent\t{bad}'
    assert ccm_send._sender_label() == 'unknown'


def test_unregistered_pane_does_not_use_window_name(context):
    state, old, current = context
    state.update(pane_dir=str(current), pane_name='', windows='')
    assert ccm_send._sender_label() == 'unknown'


def test_cwd_unreadable_is_unknown(context, monkeypatch):
    def fail():
        raise OSError('cwd unavailable')
    monkeypatch.setattr(os, 'getcwd', fail)
    assert ccm_send._sender_label() == 'unknown'


def test_fallback_project_does_not_prove_a_pane(context):
    assert ccm_core.caller_context() == ('current', '')
    assert ccm_core.caller_context(resolve_project=False) == ('unknown', '')


def test_valid_pane_is_preserved(context):
    state, old, current = context
    state.update(pane_dir=str(current), pane_name='current')
    assert ccm_core.caller_context(resolve_project=False) == ('current', '%1')


def test_stale_hint_does_not_trigger_self_send_refusal(context, monkeypatch):
    from test_send import TestSendSelfDeliveryGuard
    real = ccm_core.caller_context
    calls = TestSendSelfDeliveryGuard()._patch(monkeypatch, '%1', '%1')
    monkeypatch.setattr(ccm_core, 'caller_context', real)
    code = None
    try:
        ccm_send.cmd_send(['demo', '--force', 'hi'])
    except SystemExit as exc:
        code = exc.code
    assert code is None
    assert any(c[0] == 'send-keys' and '-l' in c for c in calls)


def test_stale_sidekick_hint_refuses_before_typing(context, monkeypatch, capsys):
    from test_sidekick_send import TestSidekickSend
    real = ccm_core.caller_context
    calls = TestSidekickSend()._stub(monkeypatch)
    monkeypatch.setattr(ccm_core, 'caller_context', real)
    code = None
    try:
        ccm_send.cmd_sidekick_send(['hi'])
    except SystemExit as exc:
        code = exc.code
    assert code == 1
    assert 'Cannot verify the caller pane' in capsys.readouterr().err
    assert not any(c[0] == 'send-keys' for c in calls)


def test_stale_hint_cannot_ignore_another_pane(context):
    import ccm_commands
    result = None
    try:
        result = ccm_commands._resolve_ignore_targets('')
    except SystemExit:
        pass
    assert result is None


def test_queue_stores_correct_sender(context):
    ccm_send._queue_message('destination', 'test body', 'busy')
    folder = os.path.join(ccm_spool.SPOOL_ROOT, 'destination')
    names = os.listdir(folder)
    assert len(names) == 1
    assert names[0].endswith('-current.msg')


def test_known_envelope_retains_reply_route():
    assert 'reply with `ccm send current' in ccm_spool._envelope('current', 100, 200)


def test_sidekick_rechecks_caller_before_typing(context, monkeypatch, capsys):
    from test_sidekick_send import TestSidekickSend
    state, old, current = context
    real_context = ccm_core.caller_context
    original_query = ccm_core.tmux_query
    calls = TestSidekickSend()._stub(monkeypatch)
    monkeypatch.setattr(ccm_core, 'caller_context', real_context)
    count = [0]
    def query(*args, **kwargs):
        count[0] += 1
        state.update(pane_dir=str(current if count[0] == 1 else old), pane_name='origin')
        return original_query(*args, **kwargs)
    monkeypatch.setattr(ccm_core, 'tmux_query', query)
    code = None
    try:
        ccm_send.cmd_sidekick_send(['hi'])
    except SystemExit as exc:
        code = exc.code
    assert code == 1
    assert 'Cannot verify the caller pane' in capsys.readouterr().err
    assert not any(c[0] == 'send-keys' for c in calls)


def test_unregistered_last_window_does_not_hide_unique_sender(context):
    state, old, current = context
    state['windows'] += '\n@3'
    assert ccm_send._sender_label() == 'current'


@pytest.mark.parametrize('ancestor,matching,expected', [
    (False, False, ('current', '')),
    (False, True, ('old', '%1')),
    (True, True, ('old', '%1')),
    (True, False, ('old', '%1')),
])
def test_caller_ancestry_and_cwd(context, monkeypatch, ancestor, matching, expected):
    state, old, current = context
    monkeypatch.chdir(old if matching else current)
    monkeypatch.setattr(ccm_core.os, 'getpid', lambda: 300)
    parent = 100 if ancestor else 1
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda:
                        f"PID PPID PGID COMM ELAPSED\n300 200 300 cmd 00:01\n"
                        f"200 {parent} 200 shell 00:02\n100 1 100 shell 00:03\n")
    assert ccm_core.caller_context() == expected


@pytest.mark.parametrize('snapshot', ['', 'PID PPID\ninvalid data',
                                      '300 200\n200 300', '300 200'])
def test_missing_or_cyclic_ancestry_keeps_cwd_fallback(context, monkeypatch, snapshot):
    monkeypatch.setattr(ccm_core.os, 'getpid', lambda: 300)
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: snapshot)
    assert ccm_core.caller_context() == ('current', '')


@pytest.mark.parametrize('matching', [False, True])
def test_ps_denied_keeps_cwd_fallback(context, monkeypatch, matching):
    state, old, current = context
    monkeypatch.chdir(old if matching else current)

    def denied():
        raise PermissionError('ps denied')

    monkeypatch.setattr(ccm_core, 'ps_snapshot', denied)
    assert ccm_core.caller_context() == (('old', '%1') if matching else ('current', ''))


def test_unreachable_tmux_does_not_read_processes(context, monkeypatch):
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *a, **kw: None)

    def unexpected():
        pytest.fail('No pane metadata: ps should not run')

    monkeypatch.setattr(ccm_core, 'ps_snapshot', unexpected)
    assert ccm_core.caller_context() == ('unknown', '')


def test_scope_reuses_snapshot_but_refreshes_pane_and_next_command(context, monkeypatch):
    state, old, current = context
    monkeypatch.setattr(ccm_core.os, 'getpid', lambda: 300)
    calls = []

    def snapshot():
        calls.append(True)
        return '300 100\n100 1'

    monkeypatch.setattr(ccm_core, 'ps_snapshot', snapshot)
    with ccm_core.caller_process_scope():
        assert ccm_core.caller_context(resolve_project=False) == ('old', '%1')
        state['pane_name'] = 'renamed'
        assert ccm_core.caller_context() == ('renamed', '%1')
    assert len(calls) == 1
    with ccm_core.caller_process_scope():
        assert ccm_core.caller_context() == ('renamed', '%1')
    assert len(calls) == 2


def test_ancestor_verifies_pane_even_with_unreadable_cwd(context, monkeypatch):
    monkeypatch.setattr(ccm_core.os, 'getpid', lambda: 300)
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '300 100\n100 1')

    def missing():
        raise FileNotFoundError('cwd removed')

    monkeypatch.setattr(ccm_core.os, 'getcwd', missing)
    assert ccm_core.caller_context() == ('old', '%1')
