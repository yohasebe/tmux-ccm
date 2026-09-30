"""Assertions for the model-free, isolated tmux capture in Bats."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import ccm_snapshot_store as store

data = store.validate(json.loads(Path(sys.argv[1]).read_text()))
assert data['version'] == 2 and data['checkpoint']['sealed']
assert data['checkpoint']['interrupted'] == []
r = data['projects'][0]['restore']
assert len(r['panes']) == 3 and r['zoomed']
assert (r['width'], r['height']) == (160, 50)
assert r['active_slot'] == 2 and r['primary_claude_slot'] is None
assert r['panes'][2]['ignore'] is True
assert r['panes'][2]['role'] == 'sidekick'
assert all(p['agent'] is None for p in r['panes'])
