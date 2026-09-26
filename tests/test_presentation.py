"""Record vocabulary is independent of storage and execution permissions."""
import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

import pytest

import ccm_commands
import ccm_core
import ccm_presentation as view
import ccm_sidekick_notify as notices
import ccm_spool
import dashboard
from test_commands import TestCmdDoctor as _DoctorWorld
from test_undelivered import make_dashboard, record


@pytest.mark.parametrize('kinds,label,category,action', [
    (('expired',), 'Expired before delivery', 'message', 'read'),
    (('held',), 'Waiting in input box', 'message', 'open'),
    (('pending',), 'Queued; not delivered yet', 'message', 'read'),
    (('notice-pending',), 'Queued; not delivered yet', 'notice', 'read'),
    (('notice-expired',), 'Expired before delivery', 'notice', 'read'),
    (('notice-held',), 'Waiting in input box', 'notice', 'open'),
    (('notice-uncertain', 'notice-attempted'), 'Delivery unconfirmed', 'notice', 'open'),
    (('notice-limited',), 'Not sent: hourly limit', 'notice', 'read'),
    (('notice-cancelled',), 'Cancelled before delivery', 'notice', 'read'),
])
def test_record_catalog(kinds, label, category, action):
    for kind in kinds:
        spec = view.record_spec(kind)
        assert (spec.label, spec.category, spec.action_id) == (label, category, action)
        assert spec.attention == ('info' if kind.endswith('pending') else 'warning')


def test_unknown_and_catalog_are_immutable():
    assert view.record_spec('future').label == 'Unrecognized record; review diagnostic details.'
    with pytest.raises(TypeError):
        view.RECORDS['future'] = view.record_spec('expired')
    with pytest.raises(FrozenInstanceError):
        view.record_spec('expired').label = 'different'


def test_action_catalog():
    assert {key: spec.key_hint for key, spec in view.ACTIONS.items()} == {
        'read': 'Enter read', 'open': 'o open', 'discard': 'd discard', 'resend': 'r resend'}
    assert 'input box stay unchanged' in view.ACTIONS['discard'].description
    assert 'new message' in view.ACTIONS['resend'].description


def test_presentation_has_no_runtime_dependencies():
    tree = ast.parse(Path(view.__file__).read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports == {'dataclasses', 'types', 'typing'}
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))


@pytest.mark.parametrize('kind', ['expired', 'held', 'notice-expired', 'notice-held',
                                 'notice-limited', 'notice-uncertain', 'notice-attempted', 'notice-cancelled'])
def test_dashboard_uses_catalog_and_private_fields_stay_hidden(monkeypatch, kind):
    item = dict(kind=kind, project='demo', sender='sender', age='1m', preview='Review result',
                id='opaque-record-id', session='opaque-session', binding={'pid': 9876})
    monkeypatch.setattr(ccm_spool, 'attention_records', lambda: [item])
    reader = Mock(return_value='selected body')
    monkeypatch.setattr(ccm_spool, 'read_record', reader)
    d, screen = make_dashboard(monkeypatch, [10, ord('q')])
    viewer = Mock()
    monkeypatch.setattr(d, '_spool_text', viewer)
    d._do_spool(screen)
    out = ' '.join(str(c.args) for c in screen.addstr.call_args_list)
    spec = view.record_spec(kind)
    assert spec.label in out
    assert view.ACTIONS[spec.action_id].description in out
    title = viewer.call_args.args[1]
    assert spec.label in title and 'demo' in title
    assert ('Completion notice' in title) == kind.startswith('notice-')
    assert reader.call_args.args == (kind, 'opaque-record-id', 'demo')
    assert viewer.call_args.args[2] == 'selected body'
    for sensitive in ('opaque-record-id', 'opaque-session', 'binding', '9876'):
        assert sensitive not in out + title


def test_doctor_regular_uses_catalog_without_zero_counts(monkeypatch, tmp_path, capsys):
    _DoctorWorld()._stub_world(monkeypatch, tmp_path)
    path = record('held', body='evidence')
    monkeypatch.setattr(ccm_spool, 'spool_summary', lambda: dict(pending=0, held=2, expired=3))
    ccm_commands.cmd_doctor()
    out = capsys.readouterr().out
    assert '2 ' + view.record_spec('held').label in out
    assert '3 ' + view.record_spec('expired').label in out
    assert view.record_spec('pending').label not in out and '`u`' in out
    assert path.read_text() == 'evidence'


def test_doctor_notices_use_catalog_without_zero_counts(monkeypatch):
    monkeypatch.setattr(ccm_spool, '_iter_project_dirs', lambda: [('demo', 'demo')])
    monkeypatch.setattr(notices, '_notices', lambda path: [(None, {'status': 'uncertain', 'binding': 'opaque-binding'})])
    text = '\n'.join(text for warn, text in notices.doctor_rows([]) if warn)
    assert '1 ' + view.record_spec('notice-uncertain').label in text
    assert view.record_spec('notice-held').label not in text
    assert 'opaque-binding' not in text and '0 ' not in text
    assert 'automatic notices are not resent' in text


def test_unknown_kind_preserves_record_and_offers_no_operations(monkeypatch):
    path = record('future', body='withheld unknown body')
    item = dict(kind='future', project='demo', sender='sender', age='1m',
                preview='withheld unknown body', id=path.stem)
    monkeypatch.setattr(ccm_spool, 'attention_records', lambda: [item])
    d, screen = make_dashboard(monkeypatch, [ord('d'), ord('r'), 10, ord('o'), ord('q')])
    action, prompt, read, opened = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(d, '_run_cmd', action)
    monkeypatch.setattr(d, '_prompt', prompt)
    monkeypatch.setattr(ccm_spool, 'read_record', read)
    monkeypatch.setattr(dashboard, 'build_project_list', opened)
    d._do_spool(screen)
    out = ' '.join(str(c.args) for c in screen.addstr.call_args_list)
    assert view.record_spec('future').label in out
    assert 'withheld unknown body' not in out
    for spec in view.ACTIONS.values():
        assert spec.key_hint not in out
    for call in (action, prompt, read, opened):
        call.assert_not_called()
    assert path.read_text() == 'withheld unknown body'


@pytest.mark.parametrize('kind', ['held', 'notice-held', 'notice-uncertain', 'notice-expired', 'future'])
def test_catalog_cannot_grant_resend(monkeypatch, kind):
    # Even a mislabeled record cannot add execution-layer affordances.
    monkeypatch.setattr(view, 'RECORDS', {kind: view.record_spec('expired')})
    assert 'resend' not in ccm_spool.record_actions(kind)
    if kind == 'future':
        assert ccm_spool.record_actions(kind) == ()


def test_unknown_cli_selector_cannot_discard_or_resend():
    path = record('future', body='keep evidence')
    for action in ('discard', 'resend'):
        with pytest.raises(SystemExit):
            ccm_spool.cmd_spool([action, 'future', path.stem, 'demo', '--yes'])
        assert path.read_text() == 'keep evidence'
