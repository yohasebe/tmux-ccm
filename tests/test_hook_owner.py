"""Tests for lib/ccm_hook_owner.py — the one definition of which hooks in
Claude Code's settings are ccm's, and the edits made to them."""
import copy
import json
import os
import subprocess
import sys

import pytest

import ccm_hook_owner as owner
from ccm_constants import HOOK_SCRIPTS


def _hook(command, timeout=5000):
    return {"type": "command", "command": command, "timeout": timeout}


def _settings(*entries_by_event):
    """_settings(("Stop", [entry, ...]), ...)"""
    return {"hooks": {event: entries for event, entries in entries_by_event}}


def _install_dir(path, scripts=HOOK_SCRIPTS, lib=True):
    path.mkdir(parents=True, exist_ok=True)
    for s in scripts:
        (path / s).write_text("#!/bin/sh\n")
    if lib:
        (path / "lib.sh").write_text("")
    return path


# ─── classify ───────────────────────────────────────────────────────

class TestClassify:

    def test_this_ccm_s_hooks_are_owned(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        cmd = f"{hooks}/on-stop.sh"
        owned, look = owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks))
        assert (owned, look) == ([cmd], [])

    def test_a_symlinked_spelling_of_this_ccm_s_directory_is_owned(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        alias = tmp_path / "alias"
        alias.symlink_to(hooks)
        cmd = f"{alias}/on-stop.sh"
        owned, _ = owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks))
        assert owned == [cmd]

    def test_this_ccm_s_directory_is_recognised_by_identity_alone(self, tmp_path):
        """Ownership does not depend on what the directory holds: this
        one has a single script and no lib.sh, and is reached through a
        symlink and two other spellings."""
        hooks = _install_dir(tmp_path / "ccm" / "hooks", scripts=["on-stop.sh"], lib=False)
        alias = tmp_path / "alias"
        alias.symlink_to(hooks)
        cmds = [f"{alias}/on-stop.sh", f"{hooks}//on-stop.sh", f"{hooks}/./on-stop.sh"]
        settings = _settings(("Stop", [{"hooks": [_hook(c) for c in cmds]}]))
        owned, look = owner.classify(settings, str(hooks))
        assert (sorted(owned), look) == (sorted(cmds), [])

    @pytest.mark.parametrize("spelling", ["{h}//on-stop.sh", "{h}/./on-stop.sh"])
    def test_other_spellings_of_this_ccm_s_directory_are_owned(self, tmp_path, spelling):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        cmd = spelling.format(h=hooks)
        owned, _ = owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks))
        assert owned == [cmd]

    def test_a_trailing_slash_on_the_hooks_dir_argument_changes_nothing(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        cmd = f"{hooks}/on-stop.sh"
        owned, _ = owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks) + "/")
        assert owned == [cmd]

    def test_a_symlinked_hooks_dir_owns_every_spelling_of_the_same_directory(self, tmp_path):
        real = _install_dir(tmp_path / "real" / "hooks")
        link = tmp_path / "link"
        link.symlink_to(real)
        alias = tmp_path / "alias"
        alias.symlink_to(real)
        cmds = [f"{real}/on-stop.sh", f"{link}/on-stop.sh", f"{alias}/on-stop.sh"]
        settings = _settings(("Stop", [{"hooks": [_hook(c) for c in cmds]}]))
        assert owner.classify(settings, str(link)) == (sorted(cmds), [])

    def test_dot_dot_counts_by_where_it_actually_leads(self, tmp_path):
        """`..` is resolved by the filesystem, not by string: back into
        this directory is ours, out of it is not, and through a missing
        directory leads nowhere."""
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        _install_dir(tmp_path / "ccm" / "outside")
        back_in = f"{hooks}/../hooks/on-stop.sh"
        out = f"{hooks}/../outside/on-stop.sh"
        nowhere = f"{tmp_path}/ccm/missing/../hooks/on-stop.sh"
        settings = _settings(("Stop", [{"hooks": [_hook(c) for c in (back_in, out, nowhere)]}]))
        assert owner.classify(settings, str(hooks)) == ([back_in], sorted([out, nowhere]))

    def test_another_case_of_this_directory_is_owned_where_the_filesystem_ignores_case(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccmcase" / "hooks")
        other_case = tmp_path / "CCMCASE" / "hooks"
        assert str(other_case) != str(hooks)
        if not other_case.exists():
            pytest.skip("this filesystem distinguishes case")
        cmd = f"{other_case}/on-stop.sh"
        assert owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks)) == ([cmd], [])

    def test_a_script_name_in_another_case_is_not_read_as_ccm_s(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        cmd = f"{hooks}/ON-STOP.SH"
        assert owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks)) == ([], [])

    @pytest.mark.parametrize("make", [
        lambda t: (t / "file").write_text("") * 0 or t / "file",
        lambda t: (t / "dangling").symlink_to(t / "nowhere") or t / "dangling",
    ], ids=["regular-file", "broken-symlink"])
    def test_a_hooks_dir_that_is_not_a_directory_owns_nothing(self, tmp_path, make):
        hooks_dir = make(tmp_path)
        cmd = f"{hooks_dir}/on-stop.sh"
        assert owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks_dir)) == ([], [cmd])

    @pytest.mark.parametrize("error", [PermissionError, ValueError])
    def test_a_comparison_that_fails_is_not_ownership(self, tmp_path, monkeypatch, error):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        cmd = f"{hooks}/on-stop.sh"

        def failing(a, b):
            raise error("cannot compare")
        monkeypatch.setattr(owner.os.path, "samefile", failing)
        assert owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks)) == ([], [cmd])

    @pytest.mark.parametrize("make", [
        lambda t: _install_dir(t / "there" / "hooks"),                          # a whole ccm install
        lambda t: _install_dir(t / "peer" / "hooks", scripts=["on-stop.sh"]),   # part of one
        lambda t: t / "gone" / "hooks",                                         # no longer there
    ], ids=["other-install", "partial", "vanished"])
    def test_every_other_directory_is_a_lookalike(self, tmp_path, make):
        """However much it resembles a ccm install, or however many ccm
        script names the settings tie to it: from the settings alone it
        cannot be told apart from another tool, so it is never edited."""
        here = _install_dir(tmp_path / "here" / "hooks")
        other = make(tmp_path)
        cmds = [f"{other}/{n}" for n in HOOK_SCRIPTS]
        settings = _settings(("Stop", [{"hooks": [_hook(c) for c in cmds]}]))
        owned, look = owner.classify(settings, str(here))
        assert (owned, look) == ([], sorted(cmds))

    def test_a_directory_whose_name_extends_this_one_is_a_lookalike(self, tmp_path):
        here = _install_dir(tmp_path / "ccm" / "hooks")
        near = _install_dir(tmp_path / "ccm" / "hooks2")
        cmd = f"{near}/on-stop.sh"
        assert owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(here)) == ([], [cmd])

    def test_a_hooks_dir_that_does_not_exist_owns_nothing(self, tmp_path):
        """Not even a command naming that same missing path: there is
        nothing on disk to say the path is this ccm's."""
        missing = tmp_path / "ccm" / "hooks"
        cmd = f"{missing}/on-stop.sh"
        assert owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(missing)) == ([], [cmd])

    @pytest.mark.parametrize("command", [
        "on-stop.sh",                          # not a path
        "hooks/on-stop.sh",                    # relative
        "/on-stop.sh",                         # no directory
        "{h}/on-stop.sh --flag",               # arguments
        "'{h}/on-stop.sh'",                    # quoted
        "bash {h}/on-stop.sh",                 # run through a shell
        "env X=1 {h}/on-stop.sh",              # run through env
        "$HOME/hooks/on-stop.sh",              # a variable
        "{h}/on-stop.sh;true",                 # a command list
        "{h}/other.sh",                        # not a ccm script name
    ], ids=["bare-name", "relative", "root", "args", "quoted", "bash", "env",
            "variable", "list", "other-script"])
    def test_commands_that_are_not_a_bare_absolute_path_to_a_ccm_script_are_neither(
            self, tmp_path, command):
        """Neither owned nor a look-alike: not read as a path at all."""
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        cmd = command.format(h=hooks)
        assert owner.classify(_settings(("Stop", [{"hooks": [_hook(cmd)]}])), str(hooks)) == ([], [])

    def test_shell_wrapped_commands_are_not_read_as_paths_into_this_directory(self, tmp_path):
        """`bash <this dir>/on-*.sh` is another tool running ccm's scripts
        its own way; it is not a hook ccm wrote, so it is not ccm's."""
        here = _install_dir(tmp_path / "here" / "hooks")
        cmds = [f"bash {here}/{n}" for n in list(HOOK_SCRIPTS)[:3]]
        settings = _settings(("Stop", [{"hooks": [_hook(c, 600) for c in cmds]}]))
        assert owner.classify(settings, str(here)) == ([], [])

    def test_strip_and_sync_ignore_a_command_that_is_not_a_string(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        odd = [{"command": []}, {"command": {}}, {"command": None}]
        settings = _settings(("Stop", [{"hooks": [_hook(f"{hooks}/on-stop.sh"), *odd]}]))
        owned, _ = owner.classify(settings, str(hooks))
        assert owner.strip(copy.deepcopy(settings), owned) == _settings(("Stop", [{"hooks": odd}]))
        synced = owner.sync_timeouts(copy.deepcopy(settings), owned, 5)
        assert synced["hooks"]["Stop"][0]["hooks"][1:] == odd

    @pytest.mark.parametrize("settings", [
        [], {"hooks": []}, {"hooks": {"Stop": 5}}, {"hooks": {"Stop": None}},
        {"hooks": {"Stop": [None]}}, {"hooks": {"Stop": [{"hooks": 5}]}},
        {"hooks": {"Stop": [{"hooks": [5]}]}}, {"hooks": {"Stop": [{"hooks": [{"command": 5}]}]}},
    ])
    def test_unexpected_shapes_classify_nothing(self, tmp_path, settings):
        assert owner.classify(settings, str(tmp_path)) == ([], [])


# ─── strip ──────────────────────────────────────────────────────────

class TestStrip:

    def test_another_tool_s_hook_in_the_same_entry_and_its_matcher_are_kept(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        ccm = _hook(f"{hooks}/on-stop.sh")
        peer = _hook("/peer/independent.sh", 600)
        settings = _settings(("Stop", [{"matcher": "m", "hooks": [ccm, peer]}]))
        owned, _ = owner.classify(settings, str(hooks))
        assert owner.strip(settings, owned) == _settings(("Stop", [{"matcher": "m", "hooks": [peer]}]))

    def test_other_spellings_of_this_directory_are_removed_and_other_tools_kept(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        _install_dir(tmp_path / "ccm" / "hooks2")
        alias = tmp_path / "alias"
        alias.symlink_to(hooks)
        mine = [f"{alias}/on-stop.sh", f"{hooks}/../hooks/on-stop.sh"]
        other_case = tmp_path / "CCM" / "hooks"
        if other_case.exists():
            mine.append(f"{other_case}/on-session-end.sh")
        theirs = [_hook(f"{tmp_path}/ccm/hooks2/on-stop.sh", 600), _hook("bash /x/on-stop.sh", 600)]
        settings = _settings(("Stop", [{"matcher": "m", "hooks": [*(_hook(c) for c in mine), *theirs]}]))
        owned, _ = owner.classify(settings, str(hooks))
        assert sorted(owned) == sorted(mine)
        synced = owner.sync_timeouts(copy.deepcopy(settings), owned, 5)
        assert synced["hooks"]["Stop"][0]["hooks"][len(mine):] == theirs
        assert owner.strip(settings, owned) == _settings(("Stop", [{"matcher": "m", "hooks": theirs}]))

    def test_a_lookalike_is_kept(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        look = _hook("/peer/hooks/on-stop.sh", 600)
        settings = _settings(("Stop", [{"hooks": [_hook(f"{hooks}/on-stop.sh")]},
                                       {"matcher": "x", "hooks": [look]}]))
        owned, _ = owner.classify(settings, str(hooks))
        assert owner.strip(settings, owned) == _settings(("Stop", [{"matcher": "x", "hooks": [look]}]))

    def test_an_emptied_entry_event_and_hooks_key_are_dropped(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        settings = {"model": "keep", **_settings(("Stop", [{"hooks": [_hook(f"{hooks}/on-stop.sh")]}]))}
        owned, _ = owner.classify(settings, str(hooks))
        assert owner.strip(settings, owned) == {"model": "keep"}

    def test_empty_things_the_user_already_had_are_left(self, tmp_path):
        """Only what the removal empties is dropped."""
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        settings = {"hooks": {"Stop": [{"hooks": [_hook(f"{hooks}/on-stop.sh")]}],
                              "Mine": [], "Theirs": [{"hooks": []}]}}
        owned, _ = owner.classify(settings, str(hooks))
        assert owner.strip(settings, owned) == {"hooks": {"Mine": [], "Theirs": [{"hooks": []}]}}
        assert owner.strip({"hooks": {}}, []) == {"hooks": {}}


# ─── sync ───────────────────────────────────────────────────────────

class TestSync:

    def test_only_ccm_s_hooks_change(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        alias = tmp_path / "alias"
        alias.symlink_to(hooks)
        settings = _settings(
            ("Stop", [{"matcher": "m", "hooks": [_hook(f"{hooks}/on-stop.sh"), _hook("/peer/x.sh", 600)]},
                      {"hooks": [_hook("/peer/hooks/on-stop.sh", 600)]}]),
            ("SessionEnd", [{"hooks": [_hook(f"{alias}/on-session-end.sh")]}]))
        before = copy.deepcopy(settings)
        owned, _ = owner.classify(settings, str(hooks))
        after = owner.sync_timeouts(settings, owned, 5)
        expected = copy.deepcopy(before)
        expected["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 5
        expected["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = 5
        assert after == expected


# ─── CLI, as lib/common.sh calls it ─────────────────────────────────

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")


def _cli(*args, stdin):
    return subprocess.run([sys.executable, os.path.join(LIB, "ccm_hook_owner.py"), *args],
                          input=stdin, capture_output=True, text=True)


@pytest.mark.live_subprocess
class TestCli:

    def test_sync_rejects_a_non_number(self, tmp_path):
        r = _cli("sync", str(tmp_path), "five", stdin="{}")
        assert r.returncode == 2 and "number of seconds" in r.stderr

    def test_invalid_json_fails(self, tmp_path):
        r = _cli("strip", str(tmp_path), stdin="{not json")
        assert r.returncode == 1

    def test_non_ascii_and_large_integers_survive(self, tmp_path):
        settings = {"note": "日本語", "big": 9007199254740993, "hooks": {}}
        r = _cli("strip", str(tmp_path), stdin=json.dumps(settings, ensure_ascii=False))
        assert r.returncode == 0 and json.loads(r.stdout) == settings
        assert "日本語" in r.stdout

    def test_lookalikes_prints_one_per_line(self, tmp_path):
        hooks = _install_dir(tmp_path / "ccm" / "hooks")
        settings = _settings(("Stop", [{"hooks": [_hook("/peer/hooks/on-stop.sh"),
                                                  _hook(f"{hooks}/on-stop.sh")]}]))
        r = _cli("lookalikes", str(hooks), stdin=json.dumps(settings))
        assert r.returncode == 0 and r.stdout.splitlines() == ["/peer/hooks/on-stop.sh"]
