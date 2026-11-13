"""tmux 3.8 reports window layouts as JSON; ccm keeps the classic form.
Each JSON/classic pair below was printed by one tmux 3.8 server: the
JSON by `display -p '#{window_layout}'`, the classic string by the same
command through a control-mode client, which tmux still answers in the
classic form."""
import json

import pytest

import ccm_layout
import ccm_snapshot_store as store

SPLIT_JSON = (
    '{"V":2,"L":{"t":"h","w":150,"h":44,"x":0,"y":0,"c":['
    '{"t":"p","w":30,"h":44,"x":0,"y":0,"l":2,"i":0,"I":"%0"},'
    '{"t":"p","w":44,"h":44,"x":31,"y":0,"a":true,"i":1,"I":"%3"},'
    '{"t":"v","w":74,"h":44,"x":76,"y":0,"c":['
    '{"t":"p","w":74,"h":22,"x":76,"y":0,"l":1,"i":2,"I":"%1"},'
    '{"t":"p","w":74,"h":21,"x":76,"y":23,"l":0,"i":3,"I":"%2"}]}]}}')
SPLIT_CLASSIC = ('d767,150x44,0,0{30x44,0,0,0,44x44,31,0,3,'
                 '74x44,76,0[74x22,76,0,1,74x21,76,23,2]}')

SINGLE_JSON = ('{"V":2,"L":{"t":"p","w":150,"h":44,"x":0,"y":0,'
               '"a":true,"i":0,"I":"%4"}}')
SINGLE_CLASSIC = 'bbc1,150x44,0,0,4'

# A floating pane (`new-pane`): a leaf with a stacking order `z`.
FLOATING_JSON = (
    '{"V":2,"L":{"t":"h","w":150,"h":44,"x":0,"y":0,"c":['
    '{"t":"p","w":75,"h":44,"x":0,"y":0,"l":1,"i":0,"I":"%0"},'
    '{"t":"p","w":74,"h":44,"x":76,"y":0,"l":0,"i":1,"I":"%1"},'
    '{"t":"p","w":38,"h":8,"x":6,"y":4,"a":true,"i":2,"z":0,"I":"%2"}]}}')


class TestNormalizeLayout:
    @pytest.mark.parametrize("raw, classic", [
        (SPLIT_JSON, SPLIT_CLASSIC),
        (SINGLE_JSON, SINGLE_CLASSIC),
    ])
    def test_json_becomes_the_classic_string_tmux_prints(self, raw, classic):
        assert store.normalize_layout(raw) == classic

    def test_classic_passes_through(self):
        assert store.normalize_layout(SPLIT_CLASSIC) == SPLIT_CLASSIC

    def test_result_is_accepted_by_the_classic_parser(self):
        ids, width, height = store._layout_ids(store.normalize_layout(SPLIT_JSON))
        assert sorted(ids) == [0, 1, 2, 3]
        assert (width, height) == (150, 44)
        tree = ccm_layout.parse(store.normalize_layout(SPLIT_JSON))
        assert tree["axis"] == "{"

    def test_floating_pane_is_refused(self):
        with pytest.raises(store.SnapshotError, match="floating"):
            store.normalize_layout(FLOATING_JSON)

    def test_unknown_version_is_refused(self):
        data = json.loads(SINGLE_JSON)
        data["V"] = 3
        with pytest.raises(store.SnapshotError, match="version"):
            store.normalize_layout(json.dumps(data))

    @pytest.mark.parametrize("raw", [
        "{not json",
        '{"V":2}',
        '{"V":2,"L":{"t":"p","w":1,"h":1,"x":0,"y":0,"I":"0"}}',
        '{"V":2,"L":{"t":"p","w":-1,"h":1,"x":0,"y":0,"I":"%0"}}',
        '{"V":2,"L":{"t":"p","w":true,"h":1,"x":0,"y":0,"I":"%0"}}',
        '{"V":2,"L":{"t":"h","w":4,"h":1,"x":0,"y":0,"c":[]}}',
        '{"V":2,"L":{"t":"q","w":4,"h":1,"x":0,"y":0,"c":[]}}',
    ])
    def test_malformed_json_is_refused(self, raw):
        with pytest.raises(store.SnapshotError):
            store.normalize_layout(raw)
