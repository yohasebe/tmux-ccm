"""Docs-vs-implementation consistency.

These pin the parts of the README that are mechanically checkable
against the code, so documentation drift fails the suite instead of
waiting to be caught by a manual audit. The 2026-07-27 audit found
three drifts at once (a missing subcommand, two missing display
markers, and a stale copy of the setup-claude-md template); each was
invisible until someone read both sides line by line.

Deliberately narrow: only claims with a single source of truth in the
code are asserted. Prose is not checked — it cannot be, and pretending
otherwise would produce brittle tests that punish good writing.
"""

import re

import pytest


from ccm_constants import STATE_ICONS

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent

#: Section headings per README. The Japanese edition translates them,
#: so every extraction is keyed by language rather than assuming the
#: English wording.
READMES = {
    "README.md": {
        "cli": "### CLI Commands",
        "icons": "Status Icons",
        "claude_md": "Aware of Other Projects",
        "template_heading": "## Multi-Project Environment",
    },
    "README.ja.md": {
        "cli": "### CLIコマンド",
        "icons": "ステータスアイコン",
        "claude_md": "他プロジェクトの存在を教える",
        "template_heading": "## マルチプロジェクト環境",
    },
}

#: Subcommands intentionally absent from the user-facing table:
#: internal plumbing, aliases, and the help/version pair.
UNDOCUMENTED_OK = {
    "reset-window",       # internal post-attach plumbing (bash wrapper)
    "inject-status",      # driven by the status bar, documented separately
    "tree-interactive",   # reached via @ccm-key-tree, not typed by hand
    "dash", "d", "ls", "st", "a", "rm", "reg", "unreg", "mv", "cap",
    "snap", "sl", "ti",   # aliases (documented as a TIP, not rows)
    "help", "version", "--help", "-h", "--version", "-v",
}


def _dispatcher_commands():
    """Command names the bash dispatcher accepts, from its `case`
    labels — the authoritative list of what `ccm <x>` will run."""
    out = set()
    for line in (REPO / "ccm").read_text().splitlines():
        m = re.match(r"\s{4}([a-z][a-z0-9|_-]*)\)", line)
        if m:
            out.update(m.group(1).split("|"))
    return out


def _section(name, key):
    """Text of a README section, located by that edition's heading.
    Raises rather than returning empty if the heading is gone — a
    silently missing section would make every assertion vacuous."""
    body = (REPO / name).read_text()
    heading = READMES[name][key]
    assert heading in body, f"{name}: heading {heading!r} not found"
    return body.split(heading)[1]


def _readme_commands(name):
    """Command names listed in a README's CLI table."""
    block = _section(name, "cli").split("```")[1]
    return {
        m.group(1)
        for m in (re.match(r"ccm ([a-z-]+)", ln.strip())
                  for ln in block.strip().splitlines())
        if m
    }


class TestCliTable:
    def test_dispatcher_sanity(self):
        """Guard the extraction itself: if the dispatcher's shape
        changes and the regex stops matching, every other assertion
        here would pass vacuously."""
        cmds = _dispatcher_commands()
        assert {"add", "status", "send", "capture"} <= cmds
        assert len(cmds) > 25

    @pytest.mark.parametrize("name", list(READMES))
    def test_every_command_is_documented(self, name):
        missing = _dispatcher_commands() - _readme_commands(name) - UNDOCUMENTED_OK
        assert not missing, (
            f"{name}'s CLI table is missing: {sorted(missing)}. Add a row, "
            "or list the name in UNDOCUMENTED_OK with the reason."
        )

    @pytest.mark.parametrize("name", list(READMES))
    def test_no_phantom_commands(self, name):
        """The costlier direction: a documented command that does not
        exist sends users at something that will just error."""
        phantom = _readme_commands(name) - _dispatcher_commands()
        assert not phantom, f"{name} documents non-existent: {sorted(phantom)}"

    def test_both_languages_list_the_same_commands(self):
        en, ja = (_readme_commands(n) for n in READMES)
        assert en == ja, f"EN-only {sorted(en - ja)}, JA-only {sorted(ja - en)}"


class TestStatusIcons:
    @pytest.mark.parametrize("name", list(READMES))
    def test_every_state_icon_is_documented(self, name):
        """The icons table must cover every state ccm can render —
        an undocumented icon is one the user cannot look up."""
        table = _section(name, "icons").split("###")[0]
        for state, icon in STATE_ICONS.items():
            row = [l for l in table.splitlines()
                   if l.startswith("|") and state in l]
            assert row, f"{name}: no row for state {state}"
            assert icon in row[0], (
                f"{name}: {state} row shows {row[0].strip()!r}, "
                f"but STATE_ICONS says {icon!r}"
            )


class TestClaudeMdTemplate:
    """`ccm setup-claude-md` writes into the user's global Claude Code
    instructions, so the README must not understate what it adds. It
    used to embed a copy of the template that had drifted to a third
    of its length, dropping `ccm send` entirely — a session was told
    how to type into other projects while the README implied only
    read-only commands. The copy is gone; these assert the properties
    that replaced it."""

    def _template(self):
        import subprocess
        out = subprocess.run(
            ["bash", "-c",
             f"source {REPO}/lib/common.sh && _ccm_claude_md_section"],
            capture_output=True, text=True, timeout=10,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout

    @pytest.mark.live_subprocess
    def test_template_still_teaches_send_and_permit_policy(self):
        """If either disappears from the template, the README prose
        describing them becomes wrong in the direction that matters."""
        t = self._template()
        assert "ccm send" in t
        assert "PERMIT" in t and "force" in t

    @pytest.mark.parametrize("name", list(READMES))
    def test_readme_does_not_re_embed_the_template(self, name):
        """No second copy to drift. The section may quote command
        names in prose, but must not reproduce the template's own
        heading — that is what turns into a stale duplicate."""
        section = _section(name, "claude_md").split("###")[0]
        assert READMES[name]["template_heading"] not in section

    @pytest.mark.parametrize("name", list(READMES))
    def test_readme_discloses_the_write_capability(self, name):
        """Whatever the wording, the section must name `ccm send`:
        users decide whether to run setup-claude-md based on it."""
        section = _section(name, "claude_md").split("###")[0]
        assert "ccm send" in section, (
            f"{name}: the setup-claude-md section must disclose that the "
            "template teaches `ccm send`, which writes to other sessions"
        )
