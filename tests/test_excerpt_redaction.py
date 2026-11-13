"""Only synthetic credentials are used; exact matches precede truncation."""
import json

import pytest

from ccm_sidekick_notify import clean


@pytest.mark.parametrize('prefix', ['sk-', 'sk-proj-', 'AIza', 'ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_', 'xai-', 'AKIA'])
def test_credential_shapes(prefix):
    assert clean('value: ' + prefix + 'synthetic_value012345') == 'value: [redacted]'


@pytest.mark.parametrize('name', ['API_KEY', 'accessToken', 'db-password', 'Client.Secret.Value', 'credentials'])
def test_environment_exact_value(monkeypatch, name):
    value = 'synthetic value with spaces 012345'
    monkeypatch.setenv(name, value)
    assert clean('before ' + value + ' after') == 'before [redacted] after'
    assert clean(value, limit=12) == '[redacted]'


def test_environment_overlapping_values_and_short_exclusion(monkeypatch):
    monkeypatch.setenv('DEMO_SECRET', 'synthetic012345')
    monkeypatch.setenv('DEMO_TOKEN', 'synthetic012345longer')
    monkeypatch.setenv('TINY_KEY', 'tiny')
    monkeypatch.setenv('ORDINARY_TEXT', 'public012345')
    assert clean('synthetic012345longer tiny public012345') == '[redacted] tiny public012345'


def test_bearer():
    assert clean('Authorization: Bearer synthetic012345+/==') == 'Authorization: Bearer [redacted]'


@pytest.mark.parametrize('key', ['api_key', 'accessToken', 'client-secret', 'PASSWORD'])
def test_json_secret_fields(key):
    raw = json.dumps({key: 'synthetic "quoted" value', 'message': 'completed'})
    assert json.loads(clean(raw)) == {key: '[redacted]', 'message': 'completed'}


def test_plain_prose_and_named_assignment():
    assert clean('Task completed successfully.') == 'Task completed successfully.'
    assert clean('api_key=synthetic012345') == 'api_key=[redacted]'
