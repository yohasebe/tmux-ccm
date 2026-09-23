"""An unidentified parked session needs sustained screen evidence to rest."""
import json

import pytest

import ccm_core
import ccm_detection
import ccm_jsonl
import ccm_signals
from test_parked_activity import pane, NOW, SOURCE, TARGET

OPTION = '@ccm_unresolved_idle'


@pytest.fixture
def polling(pane, monkeypatch):
    pane.roster.unlink()
    options = {'@ccm_prev_state': 'BUSY', '@ccm_session_id': SOURCE,
               'status-interval': '5'}
    monkeypatch.delenv('CCM_RECONCILE_INTERVAL', raising=False)
    screen = {'raw': 'IDLE', 'clock': None, 'now': NOW}

    def tmux(*args):
        if args[0] == 'show-option':
            return options.get(args[-1], '')
        if args[0] == 'set-option':
            key = next(a for a in args if a.startswith('@'))
            if '-u' in args or args[1] == '-wut':
                options.pop(key, None)
            else:
                options[key] = args[-1]
        return ''

    def raw(*args, **kwargs):
        if screen['clock'] is not None:
            kwargs['clock_out'].append(screen['clock'])
        return screen['raw']

    monkeypatch.setattr(ccm_core, 'tmux_cmd', tmux)
    monkeypatch.setattr(ccm_detection, 'detect_window_raw', raw)
    monkeypatch.setattr(ccm_detection.time, 'time', lambda: screen['now'])
    monkeypatch.setattr(ccm_core, 'find_process_age', lambda *a: screen['now'] - NOW + 1000)

    def scan(t):
        screen['now'] = NOW + t
        return ccm_detection.detect_window_state(
            'test:1', pane.project, options['@ccm_prev_state'],
            [('test:1', '40', '%1', 'claude', '1', '48')], [], '999',
            cached_session_id=options.get('@ccm_session_id', ''))

    return pane, options, screen, scan


@pytest.mark.parametrize('mode', ['missing', 'malformed', 'conflict', 'bad_short', 'wrong_prefix', 'custom_home'])
def test_unknown_mapping_releases_only_after_continuous_idle(polling, monkeypatch, mode):
    pane, options, screen, scan = polling
    if mode == 'malformed':
        pane.roster.write_text('{')
    elif mode == 'conflict':
        pane.roster.write_text(json.dumps({'workers': {TARGET[:8]: {'sessionId': TARGET}}}))
        job = pane.tmp_path / 'jobs' / TARGET[:8] / 'state.json'
        job.parent.mkdir(parents=True)
        job.write_text(json.dumps({'sessionId': SOURCE}))
    elif mode == 'bad_short':
        pane.info['parkedJobId'] = '../missing'
        pane.registration.write_text(json.dumps(pane.info))
    elif mode == 'wrong_prefix':
        pane.roster.write_text(json.dumps({'workers': {TARGET[:8]: {'sessionId': SOURCE}}}))
    elif mode == 'custom_home':
        monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(pane.tmp_path / 'custom'))

    # No source transcript, signal, or event can supply the release evidence.
    def no_transcript(*a, **k):
        pytest.fail('unresolved session borrowed a transcript')
    monkeypatch.setattr(ccm_jsonl, 'read_jsonl_tail_info', no_transcript)
    monkeypatch.setattr(ccm_jsonl, 'read_jsonl_tail_info_for_session', no_transcript)
    monkeypatch.setattr(ccm_signals, 'read_events_tail', no_transcript)
    for t in range(0, 61, 2):
        assert scan(t) == 'BUSY'
    assert scan(62) == 'IDLE'
    assert scan(64) == 'IDLE'
    assert options.get('@ccm_session_id', '') == ''


@pytest.mark.parametrize('raw', ['BUSY', 'PERMIT'])
def test_visible_activity_restarts_idle_measurement(polling, raw):
    pane, options, screen, scan = polling
    for t in range(0, 41, 2):
        assert scan(t) == 'BUSY'
    screen['raw'] = raw
    assert scan(42) == raw
    screen['raw'] = 'IDLE'
    for t in range(44, 105, 2):
        assert scan(t) == 'BUSY'
    assert scan(106) == 'IDLE'


@pytest.mark.parametrize('change', ['clock', 'clock_disappears', 'target', 'process', 'session'])
def test_observation_identity_changes_restart_measurement(polling, change):
    pane, options, screen, scan = polling
    if change == 'clock_disappears':
        screen['clock'] = '(1s)'
    for t in range(0, 41, 2):
        assert scan(t) == 'BUSY'
    if change == 'clock':
        screen['clock'] = '(2s)'
    elif change == 'clock_disappears':
        screen['clock'] = None
    else:
        key = {'target': 'parkedJobId', 'process': 'startedAt', 'session': 'sessionId'}[change]
        pane.info[key] = {'target': '33333333', 'process': (NOW - 999) * 1000,
                          'session': '33333333-3333-4333-8333-333333333333'}[change]
        pane.registration.write_text(json.dumps(pane.info))
    for t in range(42, 103, 2):
        assert scan(t) == 'BUSY'
    assert scan(104) == 'IDLE'


@pytest.mark.parametrize('gap', [63, 600])
def test_unobserved_time_is_not_idle_evidence(polling, gap):
    pane, options, screen, scan = polling
    for t in range(0, 41, 2):
        assert scan(t) == 'BUSY'
    start = 40 + gap
    for t in range(start, start + 61, 2):
        assert scan(t) == 'BUSY'
    assert scan(start + 62) == 'IDLE'


def test_ten_second_poll_gaps_are_accepted(polling):
    pane, options, screen, scan = polling
    for t in range(0, 61, 10):
        assert scan(t) == 'BUSY'
    assert scan(70) == 'IDLE'


def test_resolved_interval_discards_previous_idle_evidence(polling):
    pane, options, screen, scan = polling
    for t in range(0, 41, 2):
        assert scan(t) == 'BUSY'
    pane.roster.write_text(json.dumps({'workers': {TARGET[:8]: {'sessionId': TARGET}}}))
    assert scan(42) == 'PERMIT'
    assert OPTION not in options
    pane.roster.unlink()
    for t in range(44, 105, 2):
        assert scan(t) == 'BUSY'
    assert scan(106) == 'IDLE'


@pytest.mark.parametrize('history', ['', '{', 'null', '[]', '{}', '42'])
def test_invalid_history_starts_with_a_full_window(polling, history):
    pane, options, screen, scan = polling
    options[OPTION] = history
    for t in range(0, 61, 2):
        assert scan(t) == 'BUSY'
    assert scan(62) == 'IDLE'


@pytest.mark.parametrize('mutation', ['future', 'reversed', 'boolean', 'string', 'rollback'])
def test_invalid_history_times_cannot_earn_release(polling, mutation):
    pane, options, screen, scan = polling
    assert scan(0) == 'BUSY'
    assert OPTION in options
    history = json.loads(options[OPTION])
    history['since'] = NOW - 1000
    if mutation == 'future':
        history['seen'] = NOW + 1000
    elif mutation == 'reversed':
        history['since'] = NOW + 1
    elif mutation == 'boolean':
        history['since'] = True
    elif mutation == 'string':
        history['seen'] = str(NOW)
    options[OPTION] = json.dumps(history)
    assert scan(-1 if mutation == 'rollback' else 2) == 'BUSY'


def test_busy_after_release_restarts_hold(polling):
    pane, options, screen, scan = polling
    for t in range(0, 61, 2):
        assert scan(t) == 'BUSY'
    assert scan(62) == 'IDLE'
    screen['raw'] = 'BUSY'
    assert scan(64) == 'BUSY'
    screen['raw'] = 'IDLE'
    for t in range(66, 127, 2):
        assert scan(t) == 'BUSY'
    assert scan(128) == 'IDLE'


@pytest.mark.parametrize('raw', ['BUSY', 'PERMIT'])
def test_visible_activity_never_ages_into_idle(polling, raw):
    pane, options, screen, scan = polling
    screen['raw'] = raw
    for t in range(0, 1001, 10):
        assert scan(t) == raw
    assert OPTION not in options


def test_bulk_window_row_carries_history_without_another_query(monkeypatch):
    from unittest.mock import Mock
    history = '{"since":1700001000,"seen":1700001002}'
    row = ccm_core._parse_window_line(
        '\t'.join(['test:1', 'synthetic', '/example', 'BUSY', '', '', '', '', '', '', '', history]))
    assert row.get('unresolved_idle') == history
    detect = Mock(return_value='BUSY')
    monkeypatch.setattr(ccm_detection, 'detect_window_state', detect)
    assert ccm_core._resolve_window_state(row, False, [], [], '999') == 'BUSY'
    assert detect.call_args.kwargs['cached_unresolved_idle'] == history


@pytest.mark.parametrize('signal', ['BUSY', 'PERMIT'])
def test_hook_state_change_after_release_restarts_hold(polling, signal):
    pane, options, screen, scan = polling
    for t in range(0, 61, 2):
        assert scan(t) == 'BUSY'
    assert scan(62) == 'IDLE'
    options['@ccm_prev_state'] = signal
    for t in range(64, 125, 2):
        assert scan(t) == 'BUSY'
    assert scan(126) == 'IDLE'
