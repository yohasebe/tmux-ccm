"""Test material names only made-up projects and users.

Screens, paths and records in the tests are written for the tests, not
copied from a working machine. A project directory (`~/code/<name>`)
or a home directory (`/Users/<user>`, `/home/<user>`) in any test file
must come from the placeholder sets below; anything else fails here,
so a pasted capture cannot carry a real name into the repository.
"""
import os
import re

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {"__pycache__", ".pytest_cache"}

PLACEHOLDER_PROJECTS = {
    "ccm", "foo", "proj", "project-a", "my-project", "test_project",
    "Users", "api-server", "demo",
}
PLACEHOLDER_USERS = {
    "alice", "alice-backup", "ann", "bob", "user", "example", "u", "x", "x2",
}

_PROJECT_DIRS = r"(?:code|src|work|projects|dev|repos)"
PROJECT_PREFIX = re.compile(
    r"(?:~|(?:/Users|/home)/[^\s/]+)/" + _PROJECT_DIRS + r"/")
HOME_PREFIX = re.compile(r"(?:/Users|/home)/")
_QUOTES = "\"'`"
_TRAILING = ".,;:)]}>"


def _name_at(line, path_start, start):
    """The path component that starts at `start`. A path written as a
    quoted string (a quote right before it) runs to the closing quote.
    Elsewhere a tab, two spaces, a quote, or punctuation before a space
    also end it, as in a screen's columns or in prose. A `/` or a
    backslash ends it either way, and trailing punctuation is not part
    of it."""
    quote = line[path_start - 1:path_start] if path_start else ""
    if quote and quote in _QUOTES:
        m = re.compile(r"[/\\" + re.escape(quote) + r"]").search(line, start)
        return line[start:m.start() if m else len(line)]
    m = re.compile(r"[/\\\"'`\t]| {2,}|[,;:.)\]}>](?=\s|$)").search(line, start)
    return line[start:m.start() if m else len(line)].rstrip(_TRAILING).strip()


def names_in(line):
    """(kind, name) for every project and home name the line holds."""
    found = []
    for kind, prefix in (("project", PROJECT_PREFIX), ("user", HOME_PREFIX)):
        for m in prefix.finditer(line):
            name = _name_at(line, m.start(), m.end())
            if name:
                found.append((kind, name))
    return found


def unexpected(found):
    allowed = {"project": PLACEHOLDER_PROJECTS, "user": PLACEHOLDER_USERS}
    return [(kind, name) for kind, name in found if name not in allowed[kind]]


def _test_files():
    this = os.path.abspath(__file__)
    for root, dirs, files in os.walk(TESTS_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = os.path.join(root, name)
            if path != this:
                yield path


def test_test_material_names_only_placeholders():
    problems = []
    for path in _test_files():
        rel = os.path.relpath(path, TESTS_DIR)
        with open(path, "rb") as f:
            data = f.read()
        if b"\0" in data:
            continue                      # binary material holds no text
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            problems.append(f"{rel}: not UTF-8, could not be checked")
            continue
        for number, line in enumerate(lines, 1):
            for kind, name in unexpected(names_in(line)):
                problems.append(f"{rel}:{number}: {kind} {name!r}")
    assert not problems, (
        "test material names a project or user outside the placeholder "
        "sets; write a made-up name:\n  " + "\n  ".join(problems))


@pytest.mark.parametrize("text, expected", [
    ("  ~/code/" + "zq-tool" + "  main  Fable 5", [("project", "zq-tool")]),
    ("/Users/" + "zqname" + "/code/foo", [("user", "zqname")]),
    ("/home/" + "zqname", [("user", "zqname")]),
    ("~/code/" + "非公開", [("project", "非公開")]),
    ("/home/" + "非公開", [("user", "非公開")]),
    ("~/code/foo" + " zq", [("project", "foo zq")]),
    ("/Users/alice" + " zq/code/foo", [("user", "alice zq")]),
    ('"~/code/foo' + '  zq"', [("project", "foo  zq")]),
    ("~/code/foo" + "[zq]", [("project", "foo[zq")]),
    ("/Users/alice" + "(zq)/code/foo", [("user", "alice(zq")]),
    ('"/home/user' + '\tzq"', [("user", "user\tzq")]),
    ('"~/code/foo' + '."', [("project", "foo.")]),
    ('"~/code/foo' + ']"', [("project", "foo]")]),
    ('"/Users/alice' + ')"', [("user", "alice)")]),
    ('"~/code/foo' + ' "', [("project", "foo ")]),
])
def test_an_unknown_or_ambiguous_name_is_reported(text, expected):
    assert unexpected(names_in(text)) == expected


@pytest.mark.parametrize("text", [
    "  ~/code/my-project  main  Fable 5",
    "/Users/alice/code/foo",
    "/home/user/dir1",
    'cwd="/Users/alice/code/foo"',
    "~/code/foo\tmain\tidle",
    "(see `~/code/demo`).",
    "path ~/code/proj, then",
])
def test_placeholders_pass(text):
    assert unexpected(names_in(text)) == []
