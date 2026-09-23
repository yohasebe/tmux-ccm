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


# Fixture source belongs in non-.bats files: older Bats collectors also
# recognize test declarations in heredoc bodies. Track heredocs rather
# than trying to distinguish intended tests from their names.
_HEREDOC = re.compile(
    r'''(?<!<)<<(-?)(?!<)[ \t]*(?:'([^']*)'|"([^"]*)"|\\?([A-Za-z_]\w*))'''
)


def _heredoc_test_lines(text):
    pending = []
    for number, line in enumerate(text.splitlines(), 1):
        if pending:
            delimiter, strip_tabs = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                pending.pop(0)
            elif re.match(r"\s*@test\b", line):
                yield number
        else:
            for match in _HEREDOC.finditer(line):
                pending.append((next(value for value in match.groups()[1:]
                                     if value is not None), bool(match[1])))


def test_bats_files_do_not_embed_test_declarations_in_heredocs():
    for path in Path(__file__).parent.glob("*.bats"):
        embedded = list(_heredoc_test_lines(path.read_text()))
        assert not embedded, f"{path.name}: heredoc test declaration at lines {embedded}"


@pytest.mark.parametrize("redirect,indent", [
    ("<<EOF", ""), ("<<'EOF'", ""), ('<<"EOF"', ""), ("<<-EOF", "\t"),
])
def test_heredoc_declarations_are_detected(redirect, indent):
    text = (f"cat {redirect}\n{indent}@test \"child\" {{ :; }}\n"
            f"{indent}EOF\n@test \"parent\" {{ :; }}\n")
    assert list(_heredoc_test_lines(text)) == [2]


def test_here_strings_and_parent_tests_are_not_heredoc_declarations():
    text = 'cat <<< "data"\n@test "parent" { :; }\n'
    assert list(_heredoc_test_lines(text)) == []
