"""Sidekick attention markers: the reader/GC, the Project surface,
and the per-CLI hook installer.

The writer side (hooks/sidekick-attention.sh) is covered in
tests/test_sidekick_attention.bats — these tests treat marker files
as given and pin the contract's reader half: what surfaces, what is
reaped, and that the whole feature stays display-only.
"""

import json
import os
import time

import pytest

import ccm_core
import ccm_commands


PANE = "%40"
# panes_cache 7-tuples: (target, pid, pane_id, cmd, active, height, ignore)
PANES = [
    ("0:2", "100", "%1", "claude", "1", "46", ""),
    ("0:2", "200", PANE, "kimi", "0", "46", ""),
]


def _write_marker(dirpath, pane=PANE, **overrides):
    marker = {
        "agent": "kimi", "state": "waiting",
        "id": "sess-1", "cwd": "/x/proj",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session": "sess", "summary": "Bash: ls", "pane": pane,
    }
    marker.update(overrides)
    path = os.path.join(dirpath, f"{pane}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(marker, f)
    return path


@pytest.fixture
def attention_dir(tmp_path, monkeypatch):
    d = tmp_path / "attention"
    d.mkdir()
    monkeypatch.setattr(ccm_core, "CCM_ATTENTION_DIR", str(d))
    return str(d)


class TestAttentionReader:
    """`_read_attention_markers` — surface live waits, reap the dead.

    ccm is the contract's designated garbage collector: writers
    OVERWRITE to `resolved` and never delete, so a consumer (ringi)
    can tell "resolved" from "stale file". Deletion here is what
    keeps that promise from filling the directory forever."""

    def test_live_waiting_marker_surfaces(self, attention_dir):
        path = _write_marker(attention_dir)
        live = ccm_core._read_attention_markers(PANES)
        assert PANE in live
        assert live[PANE]["agent"] == "kimi"
        assert os.path.exists(path), "a live marker must not be reaped"

    def test_resolved_marker_is_kept_but_not_surfaced(self, attention_dir):
        """Fresh `resolved` stays on disk for slow consumers, but no
        display surface acts on it — the wait is over."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path = _write_marker(attention_dir, state="resolved",
                             resolved_ts=now)
        live = ccm_core._read_attention_markers(PANES)
        assert live == {}
        assert os.path.exists(path)

    def test_old_resolved_marker_is_reaped(self, attention_dir):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 4000))
        path = _write_marker(attention_dir, state="resolved",
                             resolved_ts=old)
        ccm_core._read_attention_markers(PANES)
        assert not os.path.exists(path)

    def test_waiting_marker_for_dead_pane_is_reaped(self, attention_dir):
        """The sidekick exited (pane gone, or back to a shell): a
        `waiting` marker with nobody waiting is the lie the GC
        exists to prevent — same self-heal shape as stale
        `@ccm_ignore`."""
        path = _write_marker(attention_dir, pane="%99")
        live = ccm_core._read_attention_markers(PANES)
        assert live == {}
        assert not os.path.exists(path)

    def test_waiting_marker_past_hard_ttl_is_reaped(self, attention_dir):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 7200))
        path = _write_marker(attention_dir, ts=old)
        live = ccm_core._read_attention_markers(PANES)
        assert live == {}
        assert not os.path.exists(path)

    def test_waiting_marker_past_expires_is_reaped(self, attention_dir):
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 5))
        path = _write_marker(attention_dir, expires=exp)
        live = ccm_core._read_attention_markers(PANES)
        assert live == {}
        assert not os.path.exists(path)

    def test_unparseable_marker_is_reaped(self, attention_dir):
        path = os.path.join(attention_dir, f"{PANE}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json {")
        live = ccm_core._read_attention_markers(PANES)
        assert live == {}
        assert not os.path.exists(path)

    def test_missing_directory_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CCM_ATTENTION_DIR",
                            str(tmp_path / "never-created"))
        assert ccm_core._read_attention_markers(PANES) == {}


class TestWindowFormatToggle:
    """The @ccm-sidekick-attention toggle rides the bulk window
    query; `_parse_window_line` must carry it and stay tolerant of
    older lines without the field."""

    _BASE = "0:2\tproj\t/x/proj\tIDLE\t0\t123\t0\tsess-uuid"

    def test_toggle_field_parsed(self):
        row = ccm_core._parse_window_line(self._BASE + "\toff")
        assert row["attention_toggle"] == "off"

    def test_missing_toggle_field_defaults_empty(self):
        row = ccm_core._parse_window_line(self._BASE)
        assert row["attention_toggle"] == ""


class TestSidekickHooksInstaller:
    """`ccm setup-sidekick-hooks kimi` edits the user's Kimi config —
    the highest-consequence thing this feature does, since Kimi's
    strict TOML parser fails the WHOLE config load on a malformed
    `[[hooks]]` entry (measured constraint)."""

    @pytest.fixture
    def kimi_config(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / ".kimi-code"
        cfg_dir.mkdir()
        cfg = cfg_dir / "config.toml"
        cfg.write_text('model = "k3"\n')
        monkeypatch.setenv("CCM_KIMI_CONFIG", str(cfg))
        return cfg

    def test_install_appends_managed_block(self, kimi_config, capsys):
        ccm_commands.cmd_setup_sidekick_hooks("kimi")
        text = kimi_config.read_text()
        assert 'model = "k3"' in text, "user config must be preserved"
        for event in ("PermissionRequest", "PermissionResult",
                      "Interrupt", "Stop", "StopFailure", "SessionEnd"):
            assert f'event = "{event}"' in text
        assert "sidekick-attention.sh' kimi" in text
        # The new-session constraint is the one operational fact the
        # user cannot discover from a silent success (measured:
        # config loads at session start only).
        assert "NEW Kimi sessions" in capsys.readouterr().out

    def test_hooks_entries_carry_only_known_fields(self, kimi_config):
        """Kimi rejects the whole config on an unknown `[[hooks]]`
        field. Every non-comment line in the managed block must be a
        table header or one of event/command/timeout."""
        ccm_commands.cmd_setup_sidekick_hooks("kimi")
        text = kimi_config.read_text()
        block = text.split(ccm_commands._SIDEKICK_BLOCK_BEGIN)[1]
        block = block.split(ccm_commands._SIDEKICK_BLOCK_END)[0]
        for line in block.strip().split("\n"):
            line = line.strip()
            if not line or line == "[[hooks]]":
                continue
            key = line.split("=")[0].strip()
            assert key in ("event", "command", "timeout"), (
                f"field {key!r} would fail Kimi's strict config load")

    def test_install_is_idempotent(self, kimi_config):
        ccm_commands.cmd_setup_sidekick_hooks("kimi")
        ccm_commands.cmd_setup_sidekick_hooks("kimi")
        text = kimi_config.read_text()
        assert text.count(ccm_commands._SIDEKICK_BLOCK_BEGIN) == 1
        assert text.count('event = "PermissionRequest"') == 1

    def test_install_writes_backup(self, kimi_config):
        ccm_commands.cmd_setup_sidekick_hooks("kimi")
        bak = str(kimi_config) + ".ccm-bak"
        assert os.path.exists(bak)
        assert 'model = "k3"' in open(bak).read()

    def test_remove_strips_block_and_keeps_user_config(self, kimi_config):
        ccm_commands.cmd_setup_sidekick_hooks("kimi")
        ccm_commands.cmd_remove_sidekick_hooks("kimi")
        text = kimi_config.read_text()
        assert ccm_commands._SIDEKICK_BLOCK_BEGIN not in text
        assert "[[hooks]]" not in text
        assert 'model = "k3"' in text

    def test_unsupported_agent_refused(self, kimi_config, capsys):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_setup_sidekick_hooks("codex")
        err = capsys.readouterr().err
        assert "codex" in err
        # The refusal must say WHY codex cannot be supported yet, so
        # the user learns it is an upstream gap, not a ccm omission.
        assert "11808" in err

    def test_missing_kimi_install_refused(self, tmp_path, monkeypatch,
                                          capsys):
        monkeypatch.setenv(
            "CCM_KIMI_CONFIG", str(tmp_path / "no-such" / "config.toml"))
        with pytest.raises(SystemExit):
            ccm_commands.cmd_setup_sidekick_hooks("kimi")
        assert "installed" in capsys.readouterr().err
