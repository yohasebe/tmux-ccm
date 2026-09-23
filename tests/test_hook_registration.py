"""One input set exercises the public probe, core, CLI and shell wrapper."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import ccm_core
import ccm_hook_owner

ROOT = Path(__file__).resolve().parents[1]
COMPLETE = json.loads((ROOT / "tests/fixtures/hooks-complete.json").read_text())


def registration_cases():
    yield "complete", COMPLETE, True
    yield "empty", {}, False
    metadata = {"notes": list(ccm_core.HOOK_SCRIPTS) + [
        "PostToolUseFailure", "SubagentStop", "PreCompact", "PostCompact"],
        "matcher": "elicitation_dialog"}
    yield "metadata_only", metadata, False
    yield "array_root", [metadata], None
    yield "null_root", None, None
    partial = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "/plugin/hooks/" + script}
        for script in ccm_core.HOOK_SCRIPTS]}]}}
    yield "scripts_without_events", partial, False
    for event in ("PostToolUseFailure", "SubagentStop", "PreCompact", "PostCompact"):
        for value in (None, [], [{"hooks": []}]):
            settings = copy.deepcopy(COMPLETE)
            settings["hooks"][event] = value
            yield f"empty_{event}_{value}", settings, False
    for event in ("UserPromptSubmit", "Stop", "PermissionDenied"):
        settings = copy.deepcopy(COMPLETE)
        del settings["hooks"][event]
        yield f"missing_script_{event}", settings, False
    settings = copy.deepcopy(COMPLETE)
    settings["hooks"]["Notification"][0]["matcher"] = "idle_prompt"
    settings["matcher"] = "elicitation_dialog"
    yield "matcher_outside_notification", settings, False
    settings = copy.deepcopy(COMPLETE)
    settings["hooks"]["Notification"][0]["matcher"] = "idle_prompt"
    settings["hooks"]["Notification"].append({"matcher": "elicitation_dialog", "hooks": []})
    yield "empty_elicitation_registration", settings, False
    settings = copy.deepcopy(COMPLETE)
    settings["hooks"]["Notification"] = "elicitation_dialog"
    yield "malformed_notification", settings, False
    settings = copy.deepcopy(COMPLETE)
    settings["hooks"]["Stop"][0]["hooks"][0]["type"] = "prompt"
    yield "noncommand_script", settings, False


CASES = list(registration_cases())


def check_consumers(path, expected, monkeypatch):
    code = 2 if expected is None else (0 if expected else 1)
    # Use the current test interpreter in the shell helper as well, so a
    # developer's version manager cannot pick another Python under tmp.
    launcher = path.parent / "bin"
    launcher.mkdir(exist_ok=True)
    (launcher / "python3").symlink_to(sys.executable)
    env = dict(os.environ, HOME=str(path.parent.parent),
               PATH=str(launcher) + os.pathsep + os.environ["PATH"])
    # A broken core reader must also fail within a deadline on FIFO.
    core = subprocess.run([sys.executable, "-c",
                           "import sys,json; sys.path.insert(0,sys.argv[1]); "
                           "import ccm_core; print(json.dumps(ccm_core.hooks_configured()))",
                           str(ROOT / "lib")], env=env,
                          capture_output=True, text=True, timeout=5)
    assert core.returncode == 0, core.stderr
    assert json.loads(core.stdout) is expected
    shell = subprocess.run([
        "/bin/bash", "-c", 'source "$1"; ccm_hooks_configured',
        "probe", str(ROOT / "lib/common.sh")], env=env,
        capture_output=True, text=True, timeout=5)
    assert shell.returncode == code, shell.stderr
    cli = subprocess.run([sys.executable, str(ROOT / "lib/ccm_hook_owner.py"),
                          "configured", str(path)], input="not stdin JSON",
                         capture_output=True, text=True, timeout=5)
    assert cli.returncode == code, cli.stderr
    assert cli.stdout == ""


@pytest.mark.parametrize("name,settings,expected", CASES, ids=[c[0] for c in CASES])
def test_registration_parity(tmp_path, monkeypatch, name, settings, expected):
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps(settings))
    assert ccm_hook_owner.registration_complete(settings) is expected
    check_consumers(path, expected, monkeypatch)


@pytest.mark.parametrize("kind", ["missing", "invalid_json", "invalid_utf8", "fifo"])
def test_unreadable_registration_parity(tmp_path, monkeypatch, kind):
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    if kind == "invalid_json":
        path.write_text("{")
    elif kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif kind == "fifo":
        os.mkfifo(path)
    check_consumers(path, None, monkeypatch)
