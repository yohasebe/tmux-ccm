"""Scope-aware notification creation, delivery, diagnostics and dashboard reads."""
from types import SimpleNamespace
import pytest
import ccm_core
import ccm_commands
import ccm_spool
import ccm_sidekick_notify as notices
from test_codex_notifications import world, records
from test_doctor_display import healthy

CASES = [
    ({'window': 'on'}, True, 'window'),
    ({'global': 'on'}, True, 'global (-g)'),
    ({'global-window': 'on'}, True, 'global (-gw)'),
    ({'window': 'off', 'global': 'on'}, False, 'window'),
    ({'window': 'off', 'global-window': 'on'}, False, 'window'),
    ({}, False, 'default'),
    ({'global-window': 'off', 'global': 'on'}, False, 'global (-gw)'),
]


def scopes(monkeypatch, values):
    previous = ccm_core.tmux_query
    def query(*args, **kw):
        if args[0] == 'show-options' and args[-1] in (notices.OPTION, notices.LIMIT, notices.EXCERPT):
            scope = {'-wqv': 'window', '-gwqv': 'global-window', '-gqv': 'global'}[args[1]]
            return values.get(scope, {}).get(args[-1], '')
        return previous(*args, **kw)
    monkeypatch.setattr(ccm_core, 'tmux_query', query)


@pytest.mark.parametrize('values,enabled,source', CASES)
def test_hook_creates_notice_from_effective_setting(world, monkeypatch, values, enabled, source):
    scopes(monkeypatch, {scope: {notices.OPTION: value} for scope, value in values.items()})
    world['emit'](last_assistant_message='Example result')
    assert bool(records()) == enabled


@pytest.mark.parametrize('values,enabled,source', CASES)
def test_delivery_rechecks_effective_setting(world, monkeypatch, values, enabled, source):
    world['emit'](last_assistant_message='Example result')
    assert len(records()) == 1
    scopes(monkeypatch, {scope: {notices.OPTION: value} for scope, value in values.items()})
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == ('delivered' if enabled else 'cancelled')
    assert bool(world['bodies']) == enabled


@pytest.mark.parametrize('values,enabled,source', CASES)
def test_doctor_verbose_reports_effective_value_and_source(healthy, monkeypatch, capsys, values, enabled, source):
    scopes(monkeypatch, {scope: {notices.OPTION: value} for scope, value in values.items()})
    from test_doctor_display import _DoctorWorld
    project = _DoctorWorld()._one_project()
    project.external_agents = ('codex',)  # only a Codex window is expected to report
    monkeypatch.setattr(ccm_core, 'build_project_list', lambda **kw: [project])
    ccm_commands.cmd_doctor(verbose=True)
    text = capsys.readouterr().out
    assert f'notify={"on" if enabled else "off"} [{source}]' in text
    assert ('no Codex hook reception observed' in text) == enabled
    assert 'limit=20/hour [default]' in text and 'excerpt=on [default]' in text


@pytest.mark.parametrize('scope', ['window', 'global', 'global-window'])
def test_limit_and_excerpt_apply_to_hook_delivery_and_dashboard(world, monkeypatch, scope):
    # Create an excerpt before policy changes, then verify all readers hide it.
    world['emit'](last_assistant_message='Example excerpt')
    scopes(monkeypatch, {scope: {notices.OPTION: 'on', notices.LIMIT: '0', notices.EXCERPT: 'off'}})
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == 'limited'
    assert not world['bodies']
    rows = ccm_spool.attention_records()
    assert rows and all('Example excerpt' not in row['preview'] for row in rows)
    assert 'Example excerpt' not in ccm_spool.read_record(rows[0]['kind'], rows[0]['id'], 'demo')
    world['emit'](turn='next-turn', last_assistant_message='Second excerpt')
    assert next(r for r in records() if r['status'] == 'pending')['excerpt'] == ''
    text = '\n'.join(notices.doctor_lines([SimpleNamespace(name='demo', win_target='@1')]))
    assert 'limit=0/hour [' in text and 'excerpt=off [' in text


@pytest.mark.parametrize('scope', ['global', 'global-window'])
def test_delivery_redacts_existing_excerpt_with_global_setting(world, monkeypatch, scope):
    world['emit'](last_assistant_message='Example excerpt')
    scopes(monkeypatch, {scope: {notices.OPTION: 'on', notices.EXCERPT: 'off'}})
    notices.reconcile([SimpleNamespace(name='demo', state='IDLE')], set())
    assert records()[0]['status'] == 'delivered'
    assert 'Example excerpt' not in world['bodies'][0]


@pytest.mark.parametrize('name,local,global_value', [(notices.OPTION, 'off', 'on'), (notices.LIMIT, '3', '20'), (notices.EXCERPT, 'off', 'on')])
def test_local_values_win_over_both_global_tables(world, monkeypatch, name, local, global_value):
    scopes(monkeypatch, {'window': {name: local}, 'global-window': {name: global_value}, 'global': {name: global_value}})
    assert notices.effective_option('@1', name) == (local, 'window')


def test_failed_window_query_does_not_fall_through_to_global_on(world, monkeypatch):
    monkeypatch.setattr(ccm_core, 'tmux_query', lambda *a, **kw: None if a[1] == '-wqv' else 'on')
    assert notices.effective_option('@1', notices.OPTION) == ('off', 'unavailable')
    assert notices.effective_option('@1', notices.EXCERPT) == ('off', 'unavailable')
