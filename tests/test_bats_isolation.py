"""Every Bats entry point must install and retain the shared tmux guard."""
from pathlib import Path
import re
import subprocess

import pytest


# Bats sources ordinary shell definitions as Bash. Do not anchor to the
# start of a line: definitions may follow a command or be inside a block.
# This is deliberately conservative, including quoted/commented examples.
# It is not a shell parser and does not attempt to interpret eval.
_FILE_HOOK_DEFINITION = re.compile(
    r"\b(?:setup_file|teardown_file)\s*\(\s*\)"
    r"|\bfunction\s+(?:setup_file|teardown_file)\b"
)


def test_all_bats_files_load_the_tmux_guard():
    files = list(Path(__file__).parent.glob("*.bats"))
    assert files
    for path in files:
        assert path.read_text().splitlines()[1] == "load helpers/tmux_guard.bash", path.name


def test_only_tmux_guard_defines_file_hooks():
    root = Path(__file__).parent
    files = sorted(root.glob("*.bats")) + sorted((root / "helpers").glob("*.bash"))
    assert files
    for path in files:
        if path == root / "helpers" / "tmux_guard.bash":
            continue
        match = _FILE_HOOK_DEFINITION.search(path.read_text())
        assert match is None, (
            f"{path.relative_to(root)}: reserved file hook definition "
            f"{match.group()!r}; only helpers/tmux_guard.bash may define file hooks"
        )


@pytest.mark.parametrize("definition", [
    "setup_file() { :; }",
    "  teardown_file () { :; }",
    "function setup_file { :; }",
    "function teardown_file() { :; }",
    "setup_file()\n{ :; }",
    ":; teardown_file() { :; }",
    "if true; then setup_file() { :; }; fi",
    "{ function teardown_file { :; }; }",
])
def test_definition_check_covers_bash_function_forms(definition):
    # Only inert definitions are evaluated, never repository shell files.
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c",
         definition + "\ndeclare -F setup_file; declare -F teardown_file; :"],
        text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() in ("setup_file", "teardown_file")
    assert _FILE_HOOK_DEFINITION.search(definition)


@pytest.mark.parametrize("text", [
    "setup_file", "teardown_file argument", "my_setup_file() { :; }",
])
def test_calls_and_other_function_names_are_not_definitions(text):
    assert _FILE_HOOK_DEFINITION.search(text) is None
