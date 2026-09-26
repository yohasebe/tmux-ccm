"""Default diagnostics retain problems while verbose retains evidence details."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import ccm_commands
import ccm_presentation
import ccm_core
import ccm_sidekick_notify as notices
import ccm_spool
from test_commands import TestCmdDoctor as _DoctorWorld


@pytest.fixture
def healthy(monkeypatch, tmp_path):
    helper = _DoctorWorld()
    helper._stub_world(monkeypatch, tmp_path, projects=(helper._one_project(),))
    md = tmp_path / 'home' / '.claude' / 'CLAUDE.md'
    md.parent.mkdir()
    md.write_text('ccm')
    return md


def test_normal_default_is_one_line_and_verbose_keeps_details(healthy, capsys):
    ccm_commands.cmd_doctor()
    assert capsys.readouterr().out == 'No issues found in the checks completed. For details, run ccm doctor --verbose.\n'
    ccm_commands.cmd_doctor(verbose=True)
    out = capsys.readouterr().out
    assert all(label in out for label in ('Environment', 'Active projects', 'auto-exit log', 'Configuration', 'Codex hooks: not installed'))


@pytest.mark.parametrize('verbose', [False, True])
def test_unreadable_file_is_not_healthy(healthy, capsys, verbose):
    healthy.write_bytes(b'\xff')
    ccm_commands.cmd_doctor(verbose=verbose)
    out = capsys.readouterr().out
    assert 'could not read' in out
    assert 'No issues found' not in out


@pytest.mark.parametrize('verbose', [False, True])
def test_errors_log_unreadable(healthy, capsys, verbose):
    Path(ccm_core.CCM_ERRORS_LOG).mkdir()
    ccm_commands.cmd_doctor(verbose=verbose)
    out = capsys.readouterr().out
    assert 'errors.log' in out and 'could not read' in out
    assert 'No issues found' not in out


def test_abnormal_default_preserves_waits_and_undelivered(healthy, monkeypatch, capsys):
    project = _DoctorWorld()._one_project()
    project.state = 'PERMIT'
    monkeypatch.setattr(ccm_core, 'build_project_list', lambda **kw: [project])
    monkeypatch.setattr(ccm_spool, 'spool_summary', lambda: dict(pending=0, held=2, expired=3))
    ccm_commands.cmd_doctor()
    out = capsys.readouterr().out
    assert 'Waiting for your response' in out
    assert '2 ' + ccm_presentation.record_spec('held').label in out
    assert '3 ' + ccm_presentation.record_spec('expired').label in out and '`u`' in out
    assert 'No issues found' not in out


@pytest.mark.parametrize('enabled', [False, True])
def test_codex_unused_vs_enabled_missing_hook(healthy, monkeypatch, capsys, enabled):
    def option(*args):
        if 'focus-events' in args:
            return 'on'
        if notices.OPTION in args:
            return 'on' if enabled else 'off'
        if notices.BINDING in args:
            return '{"session":"opaque-session","pid":1234}'
        return ''
    monkeypatch.setattr(ccm_core, 'tmux_cmd', option)
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *args: None)
    ccm_commands.cmd_doctor()
    out = capsys.readouterr().out
    assert ('Codex hooks: not installed' in out) == enabled
    assert ('no Codex hook reception observed' in out) == enabled
    assert 'opaque-session' not in out and 'binding=' not in out
    assert ('No issues found' in out) != enabled


def test_codex_invalid_settings_is_not_unused(healthy, capsys):
    path = notices.config_path()
    path.parent.mkdir()
    path.write_text('{invalid')
    ccm_commands.cmd_doctor()
    out = capsys.readouterr().out
    assert 'Codex hooks: unreadable' in out and 'repair' in out
    assert 'No issues found' not in out


def test_codex_reception_is_per_window(monkeypatch, tmp_path):
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *args: 'on' if notices.OPTION in args else '')
    state = Path(ccm_spool.SPOOL_ROOT) / '.sidekick' / 'one.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'received': 1, 'binding': {'window': '@1'}}))
    # Real projects are addressed as session:index; bindings hold window IDs.
    ids = {'main:3': '@1', 'main:4': '@2'}
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *args: ids.get(args[3]) if args[0] == 'display-message' else None)
    rows = notices.doctor_rows([SimpleNamespace(name='one', win_target='main:3'),
                               SimpleNamespace(name='two', win_target='main:4')])
    warnings = '\n'.join(text for warning, text in rows if warning)
    assert 'two: no Codex hook reception' in warnings
    assert 'one: no Codex hook reception' not in warnings
    assert json.loads(state.read_text())['received'] == 1


@pytest.mark.parametrize('status', ['expired', 'held', 'limited', 'uncertain', 'attempted', 'cancelled'])
def test_codex_nonzero_counts_always_surface(monkeypatch, status):
    monkeypatch.setattr(ccm_spool, '_iter_project_dirs', lambda: [('demo', 'demo')])
    monkeypatch.setattr(notices, '_notices', lambda path: [(None, {'status': status})])
    rows = notices.doctor_rows([])
    text = '\n'.join(text for warning, text in rows if warning)
    assert '1 ' in text and 'dashboard `u`' in text
    assert 'automatic notices are not resent' in text


def test_codex_zero_counts_are_diagnostic_only():
    assert not any(warning for warning, _ in notices.doctor_rows([]))


def test_setting_failure_remains_visible_beside_known_problem(healthy, monkeypatch, capsys):
    import ccm_canaries
    monkeypatch.setattr(ccm_canaries, 'disable_all_hooks_warning', lambda projects: 'hooks disabled in user settings')
    monkeypatch.setattr(ccm_canaries, 'unreadable_settings', lambda *args, **kw: ['project settings'])
    ccm_commands.cmd_doctor()
    out = capsys.readouterr().out
    assert 'hooks disabled in user settings' in out
    assert 'unknown — could not read: project settings' in out
    assert 'No issues found' not in out


def test_unreadable_hook_log_size(healthy, monkeypatch, capsys):
    import ccm_canaries
    monkeypatch.setattr(ccm_canaries, 'hooks_log_size', lambda: -2)
    ccm_commands.cmd_doctor()
    out = capsys.readouterr().out
    assert 'hooks.log size' in out and 'could not read' in out
    assert 'No issues found' not in out


def test_verbose_does_not_change_checks_or_evidence(healthy, monkeypatch, capsys, tmp_path):
    import ccm_canaries
    import ccm_runtime
    evidence = Path(ccm_runtime.auto_exit_log_path())
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"session":"synthetic-session"}\n')
    calls = []
    monkeypatch.setattr(ccm_canaries, 'shell_cluster_warnings', lambda projects: calls.append('cluster') or [])
    monkeypatch.setattr(ccm_canaries, 'hook_silence_enabled', lambda: True)
    monkeypatch.setattr(ccm_canaries, 'hook_silence_warnings', lambda projects: calls.append('silence') or [])
    monkeypatch.setattr(ccm_spool, 'spool_summary', lambda: calls.append('spool') or dict(pending=0, held=0, expired=0))
    ccm_commands.cmd_doctor()
    brief_calls = calls[:]
    brief = capsys.readouterr().out
    calls.clear()
    ccm_commands.cmd_doctor(verbose=True)
    verbose = capsys.readouterr().out
    assert calls == brief_calls == ['cluster', 'silence', 'spool']
    assert 'auto-exit log' not in brief and '1 session(s) closed by ccm' in verbose
    assert evidence.read_text() == '{"session":"synthetic-session"}\n'


@pytest.mark.parametrize('verbose', [False, True])
def test_unreadable_evidence_is_not_an_empty_log(healthy, capsys, verbose):
    import ccm_runtime
    path = Path(ccm_runtime.auto_exit_log_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\xff')
    ccm_commands.cmd_doctor(verbose=verbose)
    out = capsys.readouterr().out
    assert 'auto-exit log' in out and 'could not read' in out
    assert 'no sessions closed by ccm' not in out and 'No issues found' not in out
    assert path.read_bytes() == b'\xff'


def test_past_error_records_are_not_a_default_issue(healthy, monkeypatch, capsys):
    """Only an accumulating burst is a current problem; history is verbose."""
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *args: None)
    Path(ccm_core.CCM_ERRORS_LOG).parent.mkdir(parents=True, exist_ok=True)
    Path(ccm_core.CCM_ERRORS_LOG).write_text('old record\n' * 3)
    monkeypatch.setattr(ccm_commands.ccm_canaries, 'errors_log_burst_warning', lambda: None)
    ccm_commands.cmd_doctor()
    assert 'errors.log' not in capsys.readouterr().out
    ccm_commands.cmd_doctor(verbose=True)
    assert '3 record(s)' in capsys.readouterr().out


def test_queued_codex_notice_is_not_a_default_issue(monkeypatch):
    """A queued notice delivers by itself; nothing for the user to do."""
    monkeypatch.setattr(ccm_spool, '_iter_project_dirs', lambda: [('demo', 'demo')])
    monkeypatch.setattr(notices, '_notices', lambda path: [(None, {'status': 'pending'})])
    rows = notices.doctor_rows([])
    assert not [text for warning, text in rows if warning and 'Codex notices' in text]
    assert any('pending=1' in text for warning, text in rows if not warning)
