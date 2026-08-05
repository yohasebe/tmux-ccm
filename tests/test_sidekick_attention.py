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

from conftest import make_ps_lines


PANE = "%40"
# panes_cache 7-tuples: (target, pid, pane_id, cmd, active, height, ignore)
PANES = [
    # %1 is the tracked Claude pane. Its command is the versioned
    # launcher name tmux actually reports, not "claude" — see
    # test_claude_sidekick_marker_survives_the_pane_check.
    ("0:2", "100", "%1", "2.1.221", "1", "46", ""),
    ("0:2", "200", PANE, "kimi", "0", "46", ""),
]
PS_LINES = ["  100     1   100 zsh", "  101   100   100 claude"]


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
        live = ccm_core._read_attention_markers(PANES, PS_LINES)
        assert PANE in live
        assert live[PANE]["agent"] == "kimi"
        assert os.path.exists(path), "a live marker must not be reaped"

    def test_resolved_marker_is_kept_but_not_surfaced(self, attention_dir):
        """Fresh `resolved` stays on disk for slow consumers, but no
        display surface acts on it — the wait is over."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path = _write_marker(attention_dir, state="resolved",
                             resolved_ts=now)
        live = ccm_core._read_attention_markers(PANES, PS_LINES)
        assert live == {}
        assert os.path.exists(path)

    def test_old_resolved_marker_is_reaped(self, attention_dir):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 4000))
        path = _write_marker(attention_dir, state="resolved",
                             resolved_ts=old)
        ccm_core._read_attention_markers(PANES, PS_LINES)
        assert not os.path.exists(path)

    def test_waiting_marker_for_dead_pane_is_reaped(self, attention_dir):
        """The sidekick exited (pane gone, or back to a shell): a
        `waiting` marker with nobody waiting is the lie the GC
        exists to prevent — same self-heal shape as stale
        `@ccm_ignore`."""
        path = _write_marker(attention_dir, pane="%99")
        live = ccm_core._read_attention_markers(PANES, PS_LINES)
        assert live == {}
        assert not os.path.exists(path)

    def test_waiting_marker_past_hard_ttl_is_reaped(self, attention_dir):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 7200))
        path = _write_marker(attention_dir, ts=old)
        live = ccm_core._read_attention_markers(PANES, PS_LINES)
        assert live == {}
        assert not os.path.exists(path)

    def test_waiting_marker_past_expires_is_reaped(self, attention_dir):
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 5))
        path = _write_marker(attention_dir, expires=exp)
        live = ccm_core._read_attention_markers(PANES, PS_LINES)
        assert live == {}
        assert not os.path.exists(path)

    def test_unparseable_marker_is_reaped(self, attention_dir):
        path = os.path.join(attention_dir, f"{PANE}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json {")
        live = ccm_core._read_attention_markers(PANES, PS_LINES)
        assert live == {}
        assert not os.path.exists(path)

    def test_stale_stage_file_is_reaped(self, attention_dir):
        """Writers stage through `<marker>.json.tmp` before an atomic
        rename. One killed mid-write leaves the stage file behind, and
        the `.json` filter would let those accumulate forever."""
        stale = os.path.join(attention_dir, "%77.json.tmp")
        with open(stale, "w", encoding="utf-8") as f:
            f.write('{"partial":')
        os.utime(stale, (time.time() - 4000, time.time() - 4000))
        ccm_core._read_attention_markers(PANES, PS_LINES)
        assert not os.path.exists(stale)

    def test_fresh_stage_file_is_left_alone(self, attention_dir):
        """A stage file milliseconds old belongs to a write in
        flight — reaping it would destroy the very marker being
        written."""
        fresh = os.path.join(attention_dir, "%77.json.tmp")
        with open(fresh, "w", encoding="utf-8") as f:
            f.write('{"partial":')
        ccm_core._read_attention_markers(PANES, PS_LINES)
        assert os.path.exists(fresh)

    def test_missing_directory_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CCM_ATTENTION_DIR",
                            str(tmp_path / "never-created"))
        assert ccm_core._read_attention_markers(PANES, PS_LINES) == {}

    def test_claude_sidekick_marker_survives_the_pane_check(
            self, attention_dir):
        """An ignored Claude sidekick's marker must not be reaped as
        "sidekick exited".

        Its pane is identified by the process tree, NOT by
        `pane_current_command`: the versioned-install symlink makes
        tmux report the launcher name (`2.1.221`), never `claude`. A
        fixture saying "claude" here would pass against a name compare
        that is false in every real environment — which is exactly how
        this shipped broken (caught by Kimi's review, 2026-08-05)."""
        panes = PANES + [("0:2", "300", "%50", "2.1.221", "0", "46", "1")]
        ps_lines = make_ps_lines((300, 1, 300, "zsh"), (301, 300, 300, "claude"))
        path = _write_marker(attention_dir, pane="%50", agent="claude")
        live = ccm_core._read_attention_markers(panes, ps_lines)
        assert "%50" in live
        assert os.path.exists(path)

    def test_marker_on_a_plain_shell_pane_is_reaped(self, attention_dir):
        """The pane exists but hosts neither an agent CLI nor a claude
        process (the sidekick exited back to zsh): stale, reap it."""
        panes = PANES + [("0:2", "300", "%50", "zsh", "0", "46", "")]
        ps_lines = make_ps_lines((300, 1, 300, "zsh"))
        path = _write_marker(attention_dir, pane="%50", agent="claude")
        live = ccm_core._read_attention_markers(panes, ps_lines)
        assert live == {}
        assert not os.path.exists(path)


class TestAttentionToggleStillCollectsGarbage:
    """`@ccm-sidekick-attention off` silences ccm's display, not the
    writers: per-CLI hook scripts keep writing markers regardless of a
    ccm-side switch. Skipping the read while off would leave the
    directory growing with nobody to reap it, so the reader still runs
    and only its result is discarded."""

    def test_off_discards_the_result_but_still_reaps(
            self, tmp_path, monkeypatch):
        d = tmp_path / "attention"
        d.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_ATTENTION_DIR", str(d))
        dead = _write_marker(str(d), pane="%99")   # pane hosts nothing
        live_path = _write_marker(str(d))          # %40 hosts kimi

        rows = ["0:2\tproj\t/x/proj\tIDLE\t0\t123\t0\tsid\toff"]
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *a, **k: "\n".join(rows))
        monkeypatch.setattr(ccm_core, "_build_panes_cache", lambda: PANES)
        monkeypatch.setattr(ccm_core, "ps_snapshot",
                            lambda: "\n".join(PS_LINES))
        monkeypatch.setattr(ccm_core, "_resolve_window_state",
                            lambda *a, **k: "IDLE")
        monkeypatch.setattr(ccm_core, "read_cache_file", lambda *a, **k: "")

        projects = ccm_core.build_project_list(fast=False)
        assert projects[0].attention_agents == (), "off must not colour badges"
        assert not os.path.exists(dead), "GC stopped when toggled off"
        assert os.path.exists(live_path), "a live marker was reaped"


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


class TestGrokInstaller:
    """Grok Build reads `~/.grok/hooks/*.json`, so ccm ships its own
    file rather than editing the user's config — nothing of theirs to
    merge with or break, and removal is an unlink."""

    @pytest.fixture
    def grok_home(self, tmp_path, monkeypatch):
        home = tmp_path / ".grok"
        home.mkdir()
        monkeypatch.setenv("CCM_GROK_HOME", str(home))
        return home

    def test_install_writes_a_separate_file(self, grok_home, capsys):
        ccm_commands.cmd_setup_sidekick_hooks("grok")
        path = grok_home / "hooks" / "ccm-sidekick-attention.json"
        assert path.exists()
        data = json.loads(path.read_text())
        # Notification is the permission signal — Grok has no
        # PermissionRequest event at all (measured).
        assert "Notification" in data["hooks"]
        # And an activity event to close the wait, since Grok has no
        # resolution event either.
        assert "PostToolUse" in data["hooks"]
        assert "sidekick-attention.sh" in json.dumps(data)
        assert "NEW Grok sessions" in capsys.readouterr().out

    def test_install_touches_no_other_grok_file(self, grok_home):
        (grok_home / "config.toml").write_text('[cli]\ninstaller = "internal"\n')
        ccm_commands.cmd_setup_sidekick_hooks("grok")
        assert (grok_home / "config.toml").read_text() == (
            '[cli]\ninstaller = "internal"\n')

    def test_remove_unlinks_the_file(self, grok_home):
        ccm_commands.cmd_setup_sidekick_hooks("grok")
        ccm_commands.cmd_remove_sidekick_hooks("grok")
        assert not (grok_home / "hooks" / "ccm-sidekick-attention.json").exists()

    def test_missing_grok_install_refused(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CCM_GROK_HOME", str(tmp_path / "nope" / ".grok"))
        with pytest.raises(SystemExit):
            ccm_commands.cmd_setup_sidekick_hooks("grok")
        assert "installed" in capsys.readouterr().err

    def test_codex_refusal_names_the_upstream_gap(self, capsys):
        with pytest.raises(SystemExit):
            ccm_commands.cmd_setup_sidekick_hooks("codex")
        assert "11808" in capsys.readouterr().err
