"""Input loss after a ready composer must never cause another typing attempt."""
from unittest.mock import Mock

import pytest

import ccm_constants
import ccm_core
import ccm_send
from test_send import TestCmdSend as _SendWorld, composer_screen, _is_launch


@pytest.fixture
def dropped_input(monkeypatch):
    def make(message, mode='partial', prefix_size=12):
        helper = _SendWorld()
        ready_check = ccm_send._start_composer_ready
        initial, idle = helper._make_project(state='SHELL'), helper._make_project(state='IDLE')
        helper._patch_resolution(monkeypatch, initial)
        helper._patch_start_polling(monkeypatch, initial, idle)
        monkeypatch.setattr(ccm_send, '_start_composer_ready', ready_check)
        state = {'buffer': '', 'typed': [], 'keys': [], 'after_type': False}
        def snapshot():
            if state['after_type'] and mode == 'unreadable':
                return ''
            rows = state['buffer'].split('\n')
            screen = composer_screen('❯ ' + rows[0], *rows[1:], scrollback=False)
            if state['after_type'] and mode == 'transcript':
                return '❯ ' + message + '\n' + screen
            return screen
        def tmux(*args, **kwargs):
            if args[0] == 'capture-pane':
                return snapshot()
            if args[0] == 'send-keys':
                if _is_launch(args):
                    return ''
                state['keys'].append(args)
                if '-l' in args:
                    state['after_type'] = True
                    state['typed'].append(args[-1])
                    if mode == 'partial':
                        state['buffer'] += args[-1][:prefix_size]
                    elif mode == 'full':
                        state['buffer'] += args[-1]
                elif args[-1] == 'M-Enter':
                    state['buffer'] += '\n'
                # C-u is deliberately dropped: it cannot safely clear a retry.
            return ''
        monkeypatch.setattr(ccm_core, 'tmux_cmd', tmux)
        monkeypatch.setattr(ccm_send, 'capture_composer_snapshot', lambda pane: (snapshot(), None))
        monkeypatch.setattr(ccm_send, 'held_after_submit', lambda *args, **kwargs: False)
        queue = Mock(side_effect=AssertionError('A typed message must not be queued again'))
        monkeypatch.setattr(ccm_send, '_queue_message', queue)
        state['queue'] = queue
        return state
    return make


@pytest.mark.parametrize('no_enter', [False, True])
@pytest.mark.parametrize('prefix_size', [12, 45])
def test_partial_input_is_never_cleared_retyped_or_submitted(
    dropped_input, capsys, prefix_size, no_enter,
):
    message = '合成の監査依頼を確認してください。結果の内容と手順を確認して報告してください。' + '/tmp/fixture-docs/' + '資料/' * 12 + '結果.txt'
    state = dropped_input(message, prefix_size=prefix_size)
    exit_code = None
    try:
        ccm_send.cmd_send(
            ['demo', '--start', '--yes', message] + (['--no-enter'] if no_enter else [])
        )
    except SystemExit as error:
        exit_code = error.code
    assert state['buffer'] == message[:prefix_size]
    assert state['typed'] == [message]
    assert not any(call[-1] in ('C-u', 'C-c', 'Enter') for call in state['keys'])
    assert exit_code == 1
    state['queue'].assert_not_called()
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert 'remains in the input box' in out and 'No submit Enter was sent' in out
    assert 'clear any leftover text' in out and 'before deciding whether' in out
    assert 'Sent to' not in out and message[:12] not in out and 'was eaten' not in out


@pytest.mark.parametrize('mode,observation', [
    ('empty', 'No message text is visible'),
    ('unreadable', 'The input box could not be read'),
    ('transcript', 'No message text is visible'),
])
def test_no_visible_body_does_not_prove_no_residue(dropped_input, capsys, mode, observation):
    message = 'Inspect the synthetic result and report the conclusion.'
    state = dropped_input(message, mode)
    with pytest.raises(SystemExit):
        ccm_send.cmd_send(['demo', '--start', '--yes', message])
    assert state['typed'] == [message]
    assert not any(call[-1] in ('C-u', 'Enter') for call in state['keys'])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert observation in out and 'clear any leftover text' in out
    assert 'Sent to' not in out


@pytest.mark.parametrize('message', ['hi', 'First line\nSecond line', '日本語の依頼を確認してください。'])
@pytest.mark.parametrize('no_enter', [False, True])
def test_full_visible_body_is_typed_once(dropped_input, message, no_enter):
    state = dropped_input(message, 'full')
    args = ['demo', '--start', '--yes', message] + (['--no-enter'] if no_enter else [])
    ccm_send.cmd_send(args)
    assert state['typed'] == message.split('\n')
    assert state['buffer'] == message
    assert sum(call[-1] == 'Enter' for call in state['keys']) == (0 if no_enter else 1)
    assert not any(call[-1] == 'C-u' for call in state['keys'])


def test_short_dropped_body_is_not_submitted(dropped_input):
    state = dropped_input('hi', 'empty')
    with pytest.raises(SystemExit):
        ccm_send.cmd_send(['demo', '--start', '--yes', 'hi'])
    assert state['typed'] == ['hi']
    assert not any(call[-1] == 'Enter' for call in state['keys'])


def test_complete_match_reuses_composer_region_and_whitespace_folding():
    message = 'A message with wrapped 日本語 text'
    screen = composer_screen('❯ A message with', 'wrapped 日本語 text')
    assert ccm_constants.composer_has_message_prefix(screen, None, message, complete=True)
    partial = composer_screen('❯ ' + message[:12])
    assert not ccm_constants.composer_has_message_prefix(partial, None, message, complete=True)


@pytest.mark.parametrize('render_after', [0.0, 0.1, 1.9, None])
@pytest.mark.parametrize('no_enter', [False, True])
def test_render_wait_reads_only_until_match_or_deadline(
    monkeypatch, dropped_input, render_after, no_enter,
):
    message = 'Wait for the complete synthetic message to render.'
    state = dropped_input(message, 'full')
    original = ccm_send.capture_composer_snapshot
    reads = []
    initial_keys = []

    def capture(pane):
        if not state['after_type']:
            return original(pane)
        now = ccm_send.time.time()
        if not reads:
            initial_keys.extend(state['keys'])
        # Assert on every read, not just the final key count.
        assert state['keys'] == initial_keys
        reads.append(now)
        elapsed = now - reads[0]
        full = render_after is not None and elapsed + 1e-9 >= render_after
        return composer_screen('❯ ' + (message if full else message[:12])), None

    monkeypatch.setattr(ccm_send, 'capture_composer_snapshot', capture)
    code = 0
    try:
        ccm_send.cmd_send(
            ['demo', '--start', '--yes', message] + (['--no-enter'] if no_enter else [])
        )
    except SystemExit as error:
        code = error.code
    assert code == (1 if render_after is None else 0)
    assert state['typed'] == [message]
    assert not any(call[-1] in ('C-u', 'C-c') for call in state['keys'])
    assert sum(call[-1] == 'Enter' for call in state['keys']) == (
        1 if render_after is not None and not no_enter else 0
    )
    elapsed = reads[-1] - reads[0]
    assert elapsed == pytest.approx(2.0 if render_after is None else render_after)
    if render_after == 0.1:
        assert len(reads) == 2
    if render_after == 0:
        assert len(reads) == 1
    assert all(0 < b - a <= 0.100001 for a, b in zip(reads, reads[1:]))
    state['queue'].assert_not_called()


def test_info_is_flushed_before_error_even_with_buffered_stdout(monkeypatch):
    import io
    import sys

    sink = io.BytesIO()
    stdout = io.TextIOWrapper(sink, encoding='utf-8')
    stderr = io.TextIOWrapper(sink, encoding='utf-8', write_through=True)
    with monkeypatch.context() as patcher:
        patcher.setattr(sys, 'stdout', stdout)
        patcher.setattr(sys, 'stderr', stderr)
        ccm_core.ccm_info('Starting Claude in demo...')
        with pytest.raises(SystemExit):
            ccm_core.ccm_die('Delivery could not be confirmed')
        stdout.flush()  # Model the eventual flush at process exit.
        output = sink.getvalue().decode('utf-8')
    assert output.index('Starting Claude') < output.index('Error:')
