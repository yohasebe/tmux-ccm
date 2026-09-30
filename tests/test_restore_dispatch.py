"""v2 must reach the layout loader instead of the one-window v1 path."""
import json
from unittest.mock import Mock

import ccm_core
import ccm_snapshot
from snapshot_fixture import layout


def test_v2_uses_layout_loader(tmp_path, monkeypatch):
    data = {'version': 2, 'name': 'saved', 'created': '2026-01-01',
            'checkpoint': {'scope': 'single-session', 'session': '$1', 'complete': True, 'sealed': True, 'interrupted': []},
            'projects': [{'name': 'alpha', 'dir': str(tmp_path), 'auto_start_claude': True,
                          'restore': {'layout': layout('80x24,0,0,1'), 'width': 80, 'height': 24,
                                      'index': 1, 'zoomed': False, 'active_slot': 0, 'primary_claude_slot': None,
                                      'panes': [{'slot': 0, 'layout_id': 1, 'role': 'shell', 'agent': None,
                                                 'cwd': str(tmp_path), 'ignore': False}]}}]}
    (tmp_path / 'saved.json').write_text(json.dumps(data))
    monkeypatch.setattr(ccm_core, 'CCM_SNAPSHOT_DIR', str(tmp_path))
    monkeypatch.setattr(ccm_core, 'require_session', lambda: 'main')
    monkeypatch.setattr(ccm_core, 'project_exists', lambda *a: True)
    monkeypatch.setattr(ccm_snapshot, 'cmd_snapshot_save', Mock(return_value=False))
    loader = Mock()
    monkeypatch.setattr(ccm_snapshot, 'ccm_restore', loader, raising=False)
    ccm_snapshot.cmd_snapshot_load('saved')
    loader.load.assert_called_once_with('saved')
