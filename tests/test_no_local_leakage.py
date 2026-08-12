"""Keep machine-specific detail out of the tracked tree.

The repository carries the tool, its tests and its documentation.
Anything that describes the machine it happens to be developed on —
home paths, addresses, credentials, a captured session's wall clock —
belongs nowhere in it, and comments are the easiest place for such a
detail to arrive unnoticed.

The checks match SHAPES rather than a list of forbidden strings. A
list would have to be written down here to be checked against, and it
would only ever cover what someone thought to add to it.
"""

import os
import re
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tracked_files():
    """Every file git tracks — the exact set that gets published."""
    out = subprocess.run(
        ["git", "-C", REPO_ROOT, "ls-files", "-z"],
        capture_output=True, text=True, check=True).stdout
    return [f for f in out.split("\0") if f]


def _read(path):
    try:
        with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


SELF = "tests/test_no_local_leakage.py"


def _scan(pattern, skip=()):
    """Return "path:line: text" for every tracked line matching.

    This file is always skipped: it necessarily spells out the shapes
    it forbids, so scanning itself would report its own patterns and
    the checks could never pass. Every pattern here is therefore
    illustrative — no example is ever copied from a live machine — and
    the pre-commit hook, which does scan this file, covers what these
    checks cannot see in it.
    """
    hits = []
    for f in _tracked_files():
        if f == SELF or f in skip:
            continue
        text = _read(f)
        if text is None:
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if pattern.search(line):
                hits.append(f"{f}:{i}: {line.strip()[:100]}")
    return hits


class TestNoPersonalIdentifiers:
    def test_no_home_directory_paths(self):
        """`~/Library/CloudStorage/...`, `/Users/<name>`, `/home/<name>`
        pin the tree to one machine. Documentation needs example paths,
        so the placeholder forms stay allowed and only concrete ones
        fail."""
        concrete = re.compile(
            r"(?:/Users|/home)/(?!alice\b|ann\b|bob\b|u\b|user\b|example\b|x\b)"
            r"[a-z][\w.-]{2,}"
            r"|~/Library/CloudStorage"
        )
        hits = _scan(concrete)
        assert not hits, (
            "concrete home paths in tracked files — use a placeholder "
            f"such as /path/to/ccm or ~/code/my-project:\n" + "\n".join(hits)
        )

    def test_no_email_addresses(self):
        pattern = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
        hits = [h for h in _scan(pattern) if "example." not in h]
        assert not hits, "email addresses in tracked files:\n" + "\n".join(hits)

    def test_no_credential_shaped_strings(self):
        """Catches the shapes, not any real secret — ccm never needs
        one, so any match is either a leak or a bad example."""
        pattern = re.compile(
            r"sk-[A-Za-z0-9_-]{16,}"
            r"|ghp_[A-Za-z0-9]{20,}"
            r"|AKIA[0-9A-Z]{12,}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"
        )
        hits = _scan(pattern)
        assert not hits, "credential-shaped strings:\n" + "\n".join(hits)


# Why there is no "incident cited by project name" test here.
#
# It was written, and it failed on citations like `phantom-subagent
# incident` and `frozen-status-bar incident` — the exact form the
# project wants to keep. A project name and a failure name
# are both lowercase hyphenated tokens, and nothing structural tells
# a repository name apart from a failure name, so a shape rule either
# misses the leak or rejects good prose, and a rule that cries wolf
# gets switched off.
#
# Telling them apart needs the list of real project names, which is
# environment-specific and must never enter this repository. That
# check therefore lives in a local pre-commit hook that reads the
# names from the running tmux server (scripts/install-redact-hook.sh
# installs it); the tests here stay pure and structural.


class TestFixturesAreSynthetic:
    """Replay fixtures must not carry a real session's wall clock.

    The event sequence is what the tests need; absolute timestamps add
    nothing and tie the corpus to one machine's clock. Fixtures are
    normalised to a fixed synthetic epoch, and this pins that.
    """

    SYNTHETIC_EPOCH = 1000000000  # fixtures start here, offsets preserved

    def _fixture_paths(self):
        return [f for f in _tracked_files()
                if f.startswith("tests/fixtures/traces/")]

    def test_fixtures_exist(self):
        assert self._fixture_paths(), "no replay fixtures found"

    def test_fixtures_start_at_the_synthetic_epoch(self):
        import json
        for path in self._fixture_paths():
            text = _read(path)
            stamps = [json.loads(l)["ts"]
                      for l in text.split("\n") if l.strip()]
            assert stamps, f"{path}: no records"
            assert min(stamps) == self.SYNTHETIC_EPOCH, (
                f"{path}: starts at {min(stamps)}, not the synthetic epoch "
                f"{self.SYNTHETIC_EPOCH} — re-normalise so the fixture "
                f"carries intervals, not a real session's clock"
            )

    def test_fixtures_carry_only_timestamp_and_type(self):
        import json
        for path in self._fixture_paths():
            for i, line in enumerate(_read(path).split("\n"), 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                assert set(record) == {"ts", "type"}, (
                    f"{path}:{i}: unexpected fields {sorted(record)} — a "
                    f"replay fixture carries the event sequence and nothing "
                    f"else (no cwd, no session id, no payload)"
                )
