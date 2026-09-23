"""Idle evidence uses the configured full-detection cadence, not fast redraws."""
import json
from unittest.mock import Mock

import pytest

import ccm_commands
import ccm_core
import ccm_detection
import ccm_jsonl
import ccm_rules
from test_parked_activity import pane, NOW, SOURCE
from test_unresolved_idle import polling, OPTION


@pytest.mark.parametrize('step,reconcile,status,release', [
    (2, 20, 5, 62),       # dashboard
    (20, 20, 5, 80),      # periodic status only
    (30, 20, 15, 90),     # reconcile rounded to the next status tick
    (21, 20, 7, 63),
    (120, 120, 5, 360),   # user increases reconcile interval
    (90, 20, 90, 270),    # user increases status-interval
    (7, 0, 7, 63),        # reconciliation gate disabled
    (3600, 3600, 1, 10800),
])
def test_configured_detection_cadence_releases(polling, monkeypatch,
                                               step, reconcile, status, release):
    pane, options, screen, scan = polling
    monkeypatch.setenv('CCM_RECONCILE_INTERVAL', str(reconcile))
    options['status-interval'] = str(status)
    for t in range(0, release, step):
        assert scan(t) == 'BUSY'
    assert scan(release) == 'IDLE'
    assert scan(release + step) == 'IDLE'


def test_long_cadence_does_not_count_the_whole_unobserved_gap(polling, monkeypatch):
    pane, options, screen, scan = polling
    monkeypatch.setenv('CCM_RECONCILE_INTERVAL', '600')
    for t in (0, 600, 1200):
        assert scan(t) == 'BUSY'
    assert scan(1800) == 'IDLE'


def test_gap_beyond_configured_tolerance_restarts_evidence(polling, monkeypatch):
    pane, options, screen, scan = polling
    monkeypatch.setenv('CCM_RECONCILE_INTERVAL', '120')
    assert scan(0) == 'BUSY'
    assert scan(120) == 'BUSY'
    # Two expected periods plus two seconds are permitted; longer is not.
    assert scan(363) == 'BUSY'
    assert scan(483) == 'BUSY'
    assert scan(603) == 'BUSY'
    assert scan(723) == 'IDLE'


def test_hold_no_write_still_discards_idle_history(polling, monkeypatch):
    pane, options, screen, scan = polling
    monkeypatch.setattr(ccm_core, 'find_process_age', lambda *a: 10)
    monkeypatch.setattr(ccm_jsonl, 'read_session_info', lambda *a, **k: pane.info)
    for t in range(0, 41, 2):
        assert scan(t) == 'BUSY'
    screen['raw'] = 'BUSY'
    assert scan(42) == 'BUSY'
    assert OPTION not in options
    screen['raw'] = 'IDLE'
    for t in range(44, 105, 2):
        assert scan(t) == 'BUSY'
    assert scan(106) == 'IDLE'
    assert options['@ccm_prev_state'] == 'IDLE'
    assert scan(108) == 'IDLE'


def test_released_idle_cannot_match_current_hold_rule(polling, monkeypatch):
    pane, options, screen, scan = polling
    monkeypatch.setattr(ccm_core, 'find_process_age', lambda *a: 10)
    monkeypatch.setattr(ccm_jsonl, 'read_session_info', lambda *a, **k: pane.info)
    for t in range(0, 61, 2):
        assert scan(t) == 'BUSY'
    assert scan(62) == 'IDLE'
    ctx = ccm_detection.build_detection_context(
        'test:1', pane.project, options['@ccm_prev_state'],
        [('test:1', '40', '%1', 'claude', '1', '48')], [], '999')
    state, rule, _ = ccm_detection.resolve_state_from_context(ctx, pane.project)
    assert state == 'IDLE'
    assert rule.name == 'default'
    assert rule.action == ccm_rules.Action.DEFAULT


def test_old_history_without_credit_requires_new_observations(polling):
    pane, options, screen, scan = polling
    assert scan(0) == 'BUSY'
    old = json.loads(options[OPTION])
    old.pop('idle_seconds', None)
    options[OPTION] = json.dumps(old)
    for t in range(2, 63, 2):
        assert scan(t) == 'BUSY'
    assert scan(64) == 'IDLE'


@pytest.mark.parametrize('credit', [-1, True, '40', 10000])
def test_invalid_credit_restarts(polling, credit):
    pane, options, screen, scan = polling
    assert scan(0) == 'BUSY'
    record = json.loads(options[OPTION])
    record['idle_seconds'] = credit
    options[OPTION] = json.dumps(record)
    for t in range(2, 63, 2):
        assert scan(t) == 'BUSY'
    assert scan(64) == 'IDLE'


def test_bulk_query_carries_status_cadence(monkeypatch):
    history = '{"idle_seconds":20}'
    row = ccm_core._parse_window_line('\t'.join(
        ['test:1', 'synthetic', '/example', 'BUSY', '', '', '', '', '', '', '', history, '90']))
    assert row.get('status_interval') == '90'
    detect = Mock(return_value='BUSY')
    monkeypatch.setattr(ccm_detection, 'detect_window_state', detect)
    ccm_core._resolve_window_state(row, False, [], [], '999')
    assert detect.call_args.kwargs['cached_status_interval'] == '90'


class TraceFinished(Exception):
    pass


def test_trace_is_read_only_and_reports_persisted_credit(polling, monkeypatch, capsys):
    pane, options, screen, scan = polling
    assert scan(0) == 'BUSY'
    assert scan(20) == 'BUSY'
    options['@ccm_session_id'] = SOURCE
    before = dict(options)
    actual_tmux = ccm_core.tmux_cmd
    calls = []

    def tmux(*args):
        calls.append(args)
        if args[0] == 'list-panes':
            return 'test:1\t40\t%1\tclaude\t1\t48'
        return actual_tmux(*args)
    monkeypatch.setattr(ccm_core, 'tmux_cmd', tmux)
    monkeypatch.setattr(ccm_core, 'get_session', lambda: 'test')
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '')
    monkeypatch.setattr(ccm_commands, '_resolve_trace_target',
                        lambda _: ('test:1', 'synthetic', pane.project))
    monkeypatch.setattr('signal.signal', lambda *a: None)
    monkeypatch.setattr('time.monotonic', lambda: 0)
    ticks = []

    def sleep(_):
        ticks.append(1)
        if len(ticks) >= 3:
            raise TraceFinished
        screen['now'] += 2
    monkeypatch.setattr('time.sleep', sleep)
    screen['now'] = NOW + 22
    with pytest.raises(TraceFinished):
        ccm_commands.cmd_debug_trace('synthetic', interval=2)
    out, err = capsys.readouterr()
    assert 'idle_credit=20->22s' in out
    assert 'idle_credit=20->26s' in out
    assert 'read-only' in err
    assert options == before
    assert not any(c[0] == 'set-option' for c in calls)


def test_status_only_bulk_detection_at_default_reconcile_interval(polling):
    pane, options, screen, _ = polling
    options['status-interval'] = '5'
    for t in (0, 20, 40, 60, 80, 100):
        screen['now'] = NOW + t
        row = ccm_core._parse_window_line('\t'.join([
            'test:1', 'synthetic', pane.project, options['@ccm_prev_state'],
            '', '', '', options.get('@ccm_session_id', ''), '', '', '',
            options.get(OPTION, ''), options['status-interval']]))
        state = ccm_core._resolve_window_state(
            row, False, [('test:1', '40', '%1', 'claude', '1', '48')], [], '999')
        assert state == ('IDLE' if t > 60 else 'BUSY')
    assert options['@ccm_prev_state'] == 'IDLE'
