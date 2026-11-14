"""Claude Code's configuration home: CLAUDE_CONFIG_DIR, or ~/.claude."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import ccm_claude_home

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")


def test_unset_is_the_default_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ccm_claude_home.claude_home({}) == os.path.join(str(tmp_path), ".claude")


def test_an_absolute_value_is_the_home(tmp_path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
    assert ccm_claude_home.claude_home(env) == str(tmp_path / "cfg")
    assert ccm_claude_home.user_settings_file(env) == str(tmp_path / "cfg" / "settings.json")
    assert ccm_claude_home.user_claude_md(env) == str(tmp_path / "cfg" / "CLAUDE.md")
    assert ccm_claude_home.problem(env) is None


@pytest.mark.parametrize("value", ["", "cfg", "./cfg", "~/cfg"])
def test_an_empty_or_relative_value_is_refused(monkeypatch, tmp_path, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = {"CLAUDE_CONFIG_DIR": value}
    with pytest.raises(ValueError, match="not an absolute path"):
        ccm_claude_home.claude_home(env)
    assert "not an absolute path" in ccm_claude_home.problem(env)
    # Readers fall back to the default home; doctor reports the problem.
    assert ccm_claude_home.readable_home(env) == os.path.join(str(tmp_path), ".claude")


@pytest.mark.live_subprocess
def test_the_cli_prints_the_home_or_refuses(tmp_path):
    script = os.path.join(LIB, "ccm_claude_home.py")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(tmp_path))
    run = subprocess.run([sys.executable, script], env=env, capture_output=True, text=True)
    assert (run.returncode, run.stdout.strip()) == (0, str(tmp_path))
    env["CLAUDE_CONFIG_DIR"] = "relative"
    run = subprocess.run([sys.executable, script], env=env, capture_output=True, text=True)
    assert run.returncode == 2 and "not an absolute path" in run.stderr


def test_settings_are_read_from_the_configured_home(monkeypatch, tmp_path):
    import ccm_core
    (tmp_path / "settings.json").write_text('{"hooks": {}}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert ccm_core.hooks_configured() is False


def test_the_canary_reads_the_home_set_after_import(monkeypatch, tmp_path):
    import ccm_canaries
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "later"))
    sources = dict(ccm_canaries._settings_sources([]))
    assert sources["user settings"] == str(tmp_path / "later" / "settings.json")
