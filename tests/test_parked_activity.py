"""Activity identity, bounded transcript reads, and ambiguous Stop recovery."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import ccm_activity
import ccm_agentview
import ccm_core
import ccm_detection
import ccm_jsonl
import ccm_rules
from ccm_constants import JSONL_USER_PENDING
from conftest import iso_ts

SOURCE = '11111111-1111-4111-8111-111111111111'
TARGET = '22222222-2222-4222-8222-222222222222'
SHORT = TARGET[:8]
NOW = 1700001000


def record(kind='assistant', stop='end_turn', ts=NOW - 100):
    return {'type': kind, 'timestamp': iso_ts(ts),
            'message': {'stop_reason': stop, 'content': 'synthetic'}}


def write_lines(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    return path


@pytest.fixture
def pane(tmp_path, monkeypatch):
    project = str(tmp_path / 'work')
    projects = tmp_path / 'projects'
    sessions = tmp_path / 'sessions'
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    sessions.mkdir()
    monkeypatch.setattr(ccm_jsonl, 'CLAUDE_PROJECTS_DIR', str(projects))
    monkeypatch.setattr(ccm_jsonl, 'CLAUDE_SESSIONS_DIR', str(sessions))
    monkeypatch.setattr(ccm_core, 'CCM_HOOK_DIR', str(hooks))
    monkeypatch.setattr(ccm_agentview, 'DAEMON_ROSTER_PATH', str(tmp_path / 'roster.json'))
    monkeypatch.setattr(ccm_agentview, 'JOBS_DIR', str(tmp_path / 'jobs'))
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    monkeypatch.setattr(ccm_detection, 'detect_window_raw', lambda *a, **k: 'IDLE')
    monkeypatch.setattr(ccm_detection, 'read_work_clock', lambda *a, **k: None)
    monkeypatch.setattr(ccm_detection, 'find_claude_pid', lambda *a: '42')
    monkeypatch.setattr(ccm_core, 'find_process_age', lambda *a: 1000)
    monkeypatch.setattr(ccm_detection.time, 'time', lambda: NOW)
    tmux = Mock(return_value='')
    monkeypatch.setattr(ccm_core, 'tmux_cmd', tmux)
    info = {'sessionId': SOURCE, 'cwd': project, 'kind': 'interactive',
            'startedAt': (NOW - 1000) * 1000, 'parkedJobId': SHORT}
    registration = sessions / '42.json'
    registration.write_text(json.dumps(info))
    roster = tmp_path / 'roster.json'
    roster.write_text(json.dumps({'workers': {SHORT: {'sessionId': TARGET, 'cwd': project}}}))
    transcript_dir = projects / ccm_jsonl._project_slug(project)
    source = write_lines(transcript_dir / (SOURCE + '.jsonl'), [record()])
    target = write_lines(transcript_dir / (TARGET + '.jsonl'), [record(stop='tool_use')])
    write_lines(hooks / (SOURCE + '.events.jsonl'), [{'type': 'stop', 'ts': NOW - 100}])
    write_lines(hooks / (TARGET + '.events.jsonl'), [{'type': 'permit_req', 'ts': NOW - 90}])
    (hooks / TARGET).write_text(f'{NOW - 90} PERMIT\n')

    def context():
        return ccm_detection.build_detection_context(
            'test:1', project, 'BUSY', [('test:1', '40', '%1', 'claude', '1', '48')],
            [], '999', cached_session_id=SOURCE)

    return SimpleNamespace(**locals())


def test_parked_routes_events_transcript_and_cache_to_target(pane):
    ctx = pane.context()
    assert ctx.session_id == TARGET
    assert ctx.jsonl_last_stop_reason == 'tool_use'
    assert ctx.hook_state == 'PERMIT'
    assert ccm_detection.resolve_state_from_context(ctx, pane.project)[0] == 'PERMIT'
    pane.tmux.assert_any_call('set-option', '-w', '-t', 'test:1', '@ccm_session_id', TARGET)


def test_registration_read_once(pane, monkeypatch):
    reader = Mock(wraps=ccm_agentview.read_registry_record)
    monkeypatch.setattr(ccm_agentview, 'read_registry_record', reader)
    pane.context()
    reader.assert_called_once_with('42')


@pytest.mark.parametrize('mode', ['missing', 'malformed', 'conflict', 'bad_short', 'wrong_prefix', 'custom_home'])
def test_unresolved_parked_never_uses_old_terminal(pane, monkeypatch, mode):
    if mode == 'missing':
        pane.roster.unlink()
    elif mode == 'malformed':
        pane.roster.write_text('{')
    elif mode == 'conflict':
        job = pane.tmp_path / 'jobs' / SHORT / 'state.json'
        job.parent.mkdir(parents=True)
        job.write_text(json.dumps({'sessionId': SOURCE}))
    elif mode == 'bad_short':
        pane.info['parkedJobId'] = '../elsewhere'
        pane.registration.write_text(json.dumps(pane.info))
    elif mode == 'wrong_prefix':
        pane.roster.write_text(json.dumps({'workers': {SHORT: {'sessionId': SOURCE}}}))
    else:
        monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(pane.tmp_path / 'other'))
    ctx = pane.context()
    assert ctx.session_id is None
    assert ctx.jsonl_age == -1
    assert ctx.hook_state == ''
    assert ccm_detection.resolve_state_from_context(ctx, pane.project)[0] == 'BUSY'
    pane.tmux.assert_any_call('set-option', '-w', '-t', 'test:1', '-u', '@ccm_session_id')


def test_unresolved_still_honours_visible_permit(pane, monkeypatch):
    pane.roster.unlink()
    monkeypatch.setattr(ccm_detection, 'detect_window_raw', lambda *a, **k: 'PERMIT')
    assert ccm_detection.resolve_state_from_context(pane.context(), pane.project)[0] == 'PERMIT'


def test_saved_job_resolves_identity_without_claiming_liveness(pane):
    pane.roster.unlink()
    job = pane.tmp_path / 'jobs' / SHORT / 'state.json'
    job.parent.mkdir(parents=True)
    job.write_text(json.dumps({'sessionId': TARGET, 'cwd': pane.project, 'state': 'stopped'}))
    assert pane.context().session_id == TARGET


def test_unpark_returns_to_own_session(pane):
    assert pane.context().session_id == TARGET
    del pane.info['parkedJobId']
    pane.registration.write_text(json.dumps(pane.info))
    ctx = pane.context()
    assert ctx.session_id == SOURCE
    assert ccm_detection.resolve_state_from_context(ctx, pane.project)[0] == 'IDLE'


def test_target_missing_transcript_does_not_borrow_source(pane):
    pane.target.unlink()
    ctx = pane.context()
    assert ctx.jsonl_last_stop_reason is None
    assert ccm_detection.resolve_state_from_context(ctx, pane.project)[0] == 'PERMIT'


def test_target_stop_and_housekeeping_reaches_idle(pane):
    write_lines(pane.target, [record(), {'type': 'attachment', 'data': 'x' * 170000}])
    write_lines(pane.hooks / (TARGET + '.events.jsonl'), [{'type': 'stop', 'ts': NOW - 90}])
    (pane.hooks / TARGET).unlink()
    ctx = pane.context()
    assert ctx.session_id == TARGET
    assert ctx.jsonl_last_stop_reason == 'end_turn'
    assert ccm_detection.resolve_state_from_context(ctx, pane.project)[0] == 'IDLE'


@pytest.mark.parametrize('sid', [TARGET, ''])
def test_fast_known_identity_never_borrows_newest(pane, sid):
    pane.target.unlink()
    ctx = ccm_rules.build_fast_context('BUSY', pane.project, session_id=sid)
    assert ctx.jsonl_last_stop_reason is None


def test_connected_window_has_no_launch_warning(pane, monkeypatch):
    blocker = Mock(side_effect=AssertionError('running panes have no launch'))
    monkeypatch.setattr(ccm_agentview, 'continue_blocker', blocker)
    for state in ('BUSY', 'IDLE', 'PERMIT'):
        p = SimpleNamespace(state=state, dir=pane.project, win_target='test:1')
        assert ccm_agentview.continue_blockers([p]) == {}
    blocker.assert_not_called()


@pytest.mark.parametrize('kind,size', [('attachment', 170000), ('file-history-snapshot', 65000),
                                     ('cost-state', 41000)])
def test_large_housekeeping_keeps_terminal(tmp_path, kind, size):
    path = write_lines(tmp_path / 'tail.jsonl', [record('user', ts=NOW - 110), record(),
                       {'type': kind, 'data': 'x' * size}])
    assert ccm_jsonl._parse_jsonl_tail(str(path), 1, path.stat().st_size) == (NOW - 100, 'end_turn')


def test_large_new_prompt_is_not_skipped(tmp_path):
    prompt = record('user', ts=NOW)
    prompt['message']['content'] = 'x' * 170000
    path = write_lines(tmp_path / 'tail.jsonl', [record(), prompt])
    assert ccm_jsonl._parse_jsonl_tail(str(path), 1, path.stat().st_size) == (NOW, JSONL_USER_PENDING)


def test_reverse_read_budget_and_no_partial_record(tmp_path, monkeypatch):
    path = write_lines(tmp_path / 'tail.jsonl', [record(), {'type': 'attachment', 'data': 'x' * 2000000}])
    opened = []
    real_open = open

    class Reader:
        def __init__(self):
            self.f = real_open(path, 'rb')
            self.total = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.f.close()
        def seek(self, *args):
            return self.f.seek(*args)
        def tell(self):
            return self.f.tell()
        def read(self, size=-1):
            assert 0 <= size <= 32768
            data = self.f.read(size)
            self.total += len(data)
            return data

    def tracked(*args, **kwargs):
        reader = Reader()
        opened.append(reader)
        return reader
    monkeypatch.setattr(ccm_jsonl, 'open', tracked, raising=False)
    assert ccm_jsonl._parse_jsonl_tail(str(path), 1, path.stat().st_size) == (None, None)
    assert opened[0].total == 1024 * 1024
    assert ccm_jsonl._parse_jsonl_tail(str(path), 1, path.stat().st_size) == (None, None)
    assert len(opened) == 1


@pytest.mark.parametrize('padding', [32700, 32768, 32800, 65536])
def test_block_boundaries_and_utf8(tmp_path, padding):
    path = write_lines(tmp_path / 'tail.jsonl', [record('user', ts=NOW - 110), record(),
                                              {'type': 'attachment', 'data': '界' * padding}])
    assert ccm_jsonl._parse_jsonl_tail(str(path), 1, path.stat().st_size)[1] == 'end_turn'


@pytest.mark.parametrize('stop', [None, '', 'unknown'])
@pytest.mark.parametrize('age', [-1, 61, 1000])
def test_ambiguous_stop_releases_after_quiet_window(stop, age):
    assert ccm_activity.derive_state_from_events(
        [{'ts': NOW - 61, 'type': 'stop'}], stop, True, 1000,
        raw='IDLE', jsonl_age=age, now=NOW) is None


@pytest.mark.parametrize('stop', ['tool_use', JSONL_USER_PENDING])
def test_known_in_progress_stop_is_not_released(stop):
    assert ccm_activity.derive_state_from_events(
        [{'ts': NOW - 10000, 'type': 'stop'}], stop, True, 1000,
        raw='IDLE', jsonl_age=10000, now=NOW) == 'BUSY'


@pytest.mark.parametrize('event_age,jsonl_age,raw', [
    (60, -1, 'IDLE'), (1, 1000, 'IDLE'), (61, 60, 'IDLE'),
    (-1, 1000, 'IDLE'), (100, -1, 'BUSY'), (100, -1, 'PERMIT'),
])
def test_ambiguous_stop_preserves_recent_or_visible_work(event_age, jsonl_age, raw):
    assert ccm_activity.derive_state_from_events(
        [{'ts': NOW - event_age, 'type': 'stop'}], None, True, 1000,
        raw=raw, jsonl_age=jsonl_age, now=NOW) == ('PERMIT' if raw == 'PERMIT' else 'BUSY')


def test_housekeeping_line_limit(tmp_path):
    path = write_lines(tmp_path / 'tail.jsonl', [record()] +
                       [{'type': 'cost-state'}] * 201)
    assert ccm_jsonl._parse_jsonl_tail(str(path), 1, path.stat().st_size) == (None, None)


def test_fresh_assistant_stops_before_old_large_history(tmp_path, monkeypatch):
    path = write_lines(tmp_path / 'tail.jsonl', [
        {'type': 'attachment', 'data': 'x' * 2000000}, record()])
    reader = ccm_jsonl._reverse_tail_lines
    seen = []

    def tracked(path):
        for line in reader(path):
            seen.append(len(line))
            yield line
    monkeypatch.setattr(ccm_jsonl, '_reverse_tail_lines', tracked)
    assert ccm_jsonl._parse_jsonl_tail(str(path), 1, path.stat().st_size)[1] == 'end_turn'
    assert len(seen) <= 2


def test_nanosecond_cache_key_detects_same_size_rewrite(pane):
    import os
    write_lines(pane.target, [record(stop='tool_use')])
    os.utime(pane.target, ns=(NOW * 10**9, NOW * 10**9 + 1))
    assert ccm_jsonl.read_jsonl_tail_info_for_session(pane.project, TARGET)[1] == 'tool_use'
    write_lines(pane.target, [record(stop='end_turn')])
    os.utime(pane.target, ns=(NOW * 10**9, NOW * 10**9 + 2))
    assert ccm_jsonl.read_jsonl_tail_info_for_session(pane.project, TARGET)[1] == 'end_turn'


def test_pid_reuse_does_not_resolve_parked_mapping(pane, monkeypatch):
    pane.info['startedAt'] = (NOW - 5000) * 1000
    pane.registration.write_text(json.dumps(pane.info))
    resolver = Mock(wraps=ccm_agentview.resolve_pane_session)
    monkeypatch.setattr(ccm_agentview, 'resolve_pane_session', resolver)
    assert pane.context().session_id is None
    resolver.assert_called_once_with(None)


def test_ambiguous_stop_without_event_timestamp_stays_busy():
    assert ccm_activity.derive_state_from_events(
        [{'type': 'stop'}], None, True, 1000,
        raw='IDLE', jsonl_age=10000, now=NOW) == 'BUSY'
