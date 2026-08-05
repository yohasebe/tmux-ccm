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
    # Sidekick attention installers: deliberately unlisted while the
    # feature is exploratory. They only do anything for someone
    # running Kimi Code or Grok Build as a sidekick — a tiny audience
    # — and the CLI table is where readers look to learn what ccm can
    # do, so a row naming products most readers do not have costs
    # every reader attention to buy almost none of them anything. The
    # commands work; the guide documents them. Promote to rows when
    # the arrangement is common enough that the row earns its space.
    "setup-sidekick-hooks", "remove-sidekick-hooks",
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


def _mentions_flag(text, flag):
    """Whether `text` documents `flag` as a standalone token.

    Substring matching is useless here: `--` occurs inside `--file`,
    `--stdin` and every other long flag, so a naive `in` check reports
    the bare `--` as documented no matter what the text says. That is
    a test passing for the wrong reason — worse than no test, since it
    certifies the one flag most likely to be forgotten."""
    return re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", text) is not None


def _send_flags():
    """Flags `cmd_send`'s argument loop actually accepts, read from
    its comparisons. Mechanical extraction keeps the list honest: a
    flag added to the parser and forgotten in the docs shows up here
    without anyone maintaining a second list."""
    body = (REPO / "lib" / "ccm_send.py").read_text()
    body = body.split("def cmd_send(")[1].split("\n    # State-based gating")[0]
    flags = set()
    for m in re.finditer(r'arg (?:==|in) \(?((?:"[^"]+"(?:,\s*)?)+)\)?', body):
        flags |= set(re.findall(r'"([^"]+)"', m.group(1)))
    return {f for f in flags if f.startswith("-")}


class TestSendFlags:
    """`ccm send`'s flags are the part of the CLI a user is most
    likely to need mid-task and least likely to find by guessing.
    The 2026-07-27 audit found `--` — the only way to send a message
    beginning with a dash — documented in the guide but missing from
    both help outputs, which is precisely where someone whose message
    was just eaten as flags will look."""

    def test_extraction_sanity(self):
        flags = _send_flags()
        assert {"--file", "--stdin", "--force", "--start"} <= flags, flags

    def test_every_flag_appears_in_a_help_output(self):
        """`ccm send --help` and `ccm help` are the two surfaces
        reachable without leaving the terminal."""
        import ccm_send
        helps = ccm_send._SEND_USAGE + "\n" + (REPO / "ccm").read_text()
        missing = sorted(f for f in _send_flags()
                         if not _mentions_flag(helps, f))
        assert not missing, f"flags absent from CLI help: {missing}"

    @pytest.mark.parametrize("guide", ["docs/guide.md", "docs/guide.ja.md"])
    def test_every_flag_is_documented_in_the_guide(self, guide):
        body = (REPO / guide).read_text()
        missing = sorted(f for f in _send_flags()
                         if not _mentions_flag(body, f))
        assert not missing, f"{guide} does not document: {missing}"


class TestUninstall:
    """Uninstall instructions are audited least and break loudest.
    ccm registers hooks into `~/.claude/settings.json` using absolute
    paths inside the plugin directory, so an uninstall that skips
    `ccm remove-hooks` leaves Claude Code invoking scripts that no
    longer exist on every event — ccm breaking a *different* tool on
    its way out. The `CLAUDE.md` section is the same shape of
    leftover. Both must be undone while ccm is still installed, since
    the commands that undo them ship with it."""

    @pytest.mark.parametrize("name", list(READMES))
    def test_uninstall_detaches_from_claude_code(self, name):
        body = (REPO / name).read_text()
        heading = "## Uninstall" if name.endswith("ja.md") is False else "## アンインストール"
        assert heading in body, f"{name}: uninstall heading not found"
        section = body.split(heading)[1].split("\n## ")[0]
        for cmd in ("ccm remove-hooks", "ccm remove-claude-md"):
            assert cmd in section, f"{name}: uninstall omits `{cmd}`"

    @pytest.mark.parametrize("name", list(READMES))
    def test_detach_comes_before_removing_the_plugin(self, name):
        """Ordering is the whole point: once the plugin directory is
        gone, so are the commands."""
        body = (REPO / name).read_text()
        heading = "## Uninstall" if name.endswith("ja.md") is False else "## アンインストール"
        section = body.split(heading)[1].split("\n## ")[0]
        assert section.index("ccm remove-hooks") < section.index("@plugin"), (
            f"{name}: detaching from Claude Code must precede removing the "
            "plugin from ~/.tmux.conf"
        )
