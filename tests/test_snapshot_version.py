"""The writer must persist observed layout, not silently fall back to v1."""
import json

import ccm_core
import ccm_snapshot
from snapshot_fixture import inventory_query


def test_saved_format_has_observed_layout(tmp_path, monkeypatch):
    listing = '1\twin\talpha\t/tmp/alpha'
    monkeypatch.setattr(ccm_core, 'CCM_SNAPSHOT_DIR', str(tmp_path))
    monkeypatch.setattr(ccm_core, 'tmux_cmd', lambda *a, **k: listing)
    monkeypatch.setattr(ccm_core, 'tmux_query', inventory_query(listing))
    monkeypatch.setattr(ccm_core, 'ps_snapshot', lambda: '1 0 1 zsh 00:10')
    ccm_snapshot.cmd_snapshot_save('_autosave', quiet=True)
    data = json.loads((tmp_path / '_autosave.json').read_text())
    assert data['version'] == 2
    assert data['checkpoint']['complete'] is True
    assert data['projects'][0]['restore']['panes'][0]['cwd'] == '/tmp/alpha'
