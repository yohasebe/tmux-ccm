"""Tests for the Tier-1 external-agent presence badge.

A pane whose foreground command (`pane_current_command`) matches
EXTERNAL_AGENT_COMMANDS surfaces a dim `⚙<name>` badge on the
dashboard / `ccm status` / status-bar mode 2, plus a `(name)` note
on SHELL rows (window has no claude — SHELL keeps its meaning of
"no claude here" and the note says what IS running). Display-only:
no detection, no state machine, no hooks, no extra tmux
subprocesses (the bulk panes_cache is the only data source).
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import ccm_canaries
import ccm_constants
import ccm_core
import ccm_render
import ccm_signals
import inject_status


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s):
    return _ANSI_RE.sub("", s)


def _make_project(name, state, **kw):
    return ccm_core.Project(
        win_target="0:1", win_idx="1", name=name,
        directory=f"/tmp/{name}", state=state, **kw)


class TestResolveExternalAgentPanes:
    """`_resolve_external_agent_panes` reads the bulk panes_cache
    (index 3 = pane_current_command) — pure, zero subprocesses."""

    def test_matching_commands_collected_per_pane(self):
        cache = [
            ("0:1", "100", "%0", "claude", "1", "40", ""),
            ("0:1", "200", "%1", "kimi", "0", "40", ""),
            ("0:1", "300", "%2", "kimi-code", "0", "40", ""),
        ]
        assert ccm_core._resolve_external_agent_panes(cache, "0:1") == (
            "kimi", "kimi-code")

    def test_duplicate_panes_keep_count(self):
        cache = [
            ("0:1", "200", "%1", "kimi", "0", "40", ""),
            ("0:1", "300", "%2", "kimi", "0", "40", ""),
        ]
        assert ccm_core._resolve_external_agent_panes(cache, "0:1") == (
            "kimi", "kimi")

    def test_non_allowlisted_commands_ignored(self):
        # Parked editors / pagers / shells must NOT badge — that is
        # exactly why this is an allowlist, not "any non-shell".
        cache = [
            ("0:1", "100", "%0", "zsh", "1", "40", ""),
            ("0:1", "200", "%1", "vim", "0", "40", ""),
            ("0:1", "300", "%2", "less", "0", "40", ""),
        ]
        assert ccm_core._resolve_external_agent_panes(cache, "0:1") == ()

    def test_other_windows_not_counted(self):
        cache = [("0:2", "200", "%1", "kimi", "0", "40", "")]
        assert ccm_core._resolve_external_agent_panes(cache, "0:1") == ()

    def test_empty_cache(self):
        assert ccm_core._resolve_external_agent_panes([], "0:1") == ()


class TestExternalAgentLabel:
    """Shared badge label: first-seen order, `×N` for repeats."""

    def test_project_default_is_empty(self):
        # Backward compat: positional/kwarg-less construction (every
        # pre-existing test) must yield no badge.
        assert _make_project("alpha", "IDLE").external_agents == ()
        assert ccm_render.external_agent_label(
            _make_project("alpha", "IDLE")) == ""

    def test_single(self):
        p = _make_project("a", "IDLE", external_agents=("kimi",))
        assert ccm_render.external_agent_label(p) == "kimi"

    def test_duplicate_gets_count_suffix(self):
        p = _make_project("a", "IDLE", external_agents=("kimi", "kimi"))
        assert ccm_render.external_agent_label(p) == "kimi×2"

    def test_distinct_names_joined(self):
        p = _make_project("a", "IDLE",
                          external_agents=("kimi", "kimi-code"))
        assert ccm_render.external_agent_label(p) == "kimi,kimi-code"


class TestPrintStatusExternalAgent:
    """`ccm status` rendering: badge in the PROJECT column cluster,
    `(name)` note in the STATUS cell of SHELL rows."""

    def _run(self, projects, monkeypatch, capsys):
        monkeypatch.setattr(ccm_core, "build_project_list",
                            lambda fast=False: projects)
        monkeypatch.setattr(ccm_canaries, "hooks_log_warning", lambda: "")
        monkeypatch.setattr(ccm_canaries, "disable_all_hooks_warning",
                            lambda *a, **kw: "")
        monkeypatch.setattr(ccm_canaries, "managed_hooks_only_warning",
                            lambda *a, **kw: "")
        monkeypatch.setattr(ccm_canaries, "shell_cluster_warnings",
                            lambda projects_arg: [])
        monkeypatch.setattr(ccm_canaries, "hook_silence_warnings",
                            lambda projects_arg: [])
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: None)
        monkeypatch.setattr(ccm_core, "hooks_configured", lambda: True)
        ccm_render.print_status()
        return capsys.readouterr().out

    def test_badge_after_project_name(self, monkeypatch, capsys):
        projects = [_make_project("sidekick", "IDLE",
                                  pane_count=2,
                                  external_agents=("kimi",))]
        out = self._run(projects, monkeypatch, capsys)
        row = next(l for l in out.splitlines() if "sidekick" in l)
        assert "⚙kimi" in _strip_ansi(row)
        assert _strip_ansi(row).index("⚙kimi") > row.index("sidekick")

    def test_shell_row_gets_note_and_stays_shell(self, monkeypatch, capsys):
        """A kimi-only window is SHELL (no claude) — the state is not
        faked — with a `(kimi)` note attached to the STATUS cell."""
        projects = [_make_project("kimionly", "SHELL",
                                  pane_count=1,
                                  external_agents=("kimi",))]
        out = self._run(projects, monkeypatch, capsys)
        row = next(l for l in out.splitlines() if "kimionly" in l)
        plain = _strip_ansi(row)
        assert "SHELL" in plain
        assert "(kimi)" in plain
        # Note belongs to the STATUS cell: it precedes the name.
        assert plain.index("(kimi)") < plain.index("kimionly")

    def test_note_widens_status_column_consistently(
            self, monkeypatch, capsys):
        """When a note exists the whole STATUS column widens so the
        PROJECT column stays vertically aligned across rows."""
        projects = [
            _make_project("kimionly", "SHELL", external_agents=("kimi",)),
            _make_project("plain", "IDLE"),
        ]
        out = self._run(projects, monkeypatch, capsys)
        rows = [l for l in out.splitlines()
                if "kimionly" in l or "plain" in l]
        assert len(rows) == 2
        name_cols = {_strip_ansi(r).index(n)
                     for r, n in zip(rows, ("kimionly", "plain"))}
        assert len(name_cols) == 1, (
            f"PROJECT column must stay aligned, got {name_cols}")

    def test_no_badge_no_note_for_claude_only(self, monkeypatch, capsys):
        """Non-regression: a claude-only window renders exactly as
        before — no badge, no note, STATUS column stays 12 wide."""
        projects = [_make_project("plain", "IDLE")]
        out = self._run(projects, monkeypatch, capsys)
        assert "⚙" not in out
        header = next(l for l in out.splitlines() if "STATUS" in l)
        # "STATUS" padded to 12 then one space before PROJECT.
        assert _strip_ansi(header).startswith("STATUS" + " " * 7)


class TestBuildDetailEntriesExternalAgent:
    """Status-bar mode 2 (`with_extras=True`) carries the badge and
    the SHELL note; mode 1 (minimal format) is untouched."""

    def _entries(self, projects, with_extras, monkeypatch):
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: None)
        return inject_status.build_detail_entries(
            projects, with_extras=with_extras, current_win_target="0:9")

    def test_mode2_badge_present(self, monkeypatch):
        p = _make_project("sidekick", "IDLE", external_agents=("kimi",))
        entries = self._entries([p], True, monkeypatch)
        assert "⚙kimi" in entries[0]

    def test_mode2_shell_note_after_icon(self, monkeypatch):
        p = _make_project("kimionly", "SHELL", external_agents=("kimi",))
        entries = self._entries([p], True, monkeypatch)
        assert "■" in entries[0]
        assert "(kimi)" in entries[0]
        assert entries[0].index("(kimi)") > entries[0].index("■")

    def test_mode1_unchanged(self, monkeypatch):
        p = _make_project("sidekick", "IDLE", external_agents=("kimi",))
        entries = self._entries([p], False, monkeypatch)
        assert "⚙" not in entries[0]
        assert "(kimi)" not in entries[0]

    def test_no_badge_without_external_agents(self, monkeypatch):
        p = _make_project("plain", "IDLE")
        entries = self._entries([p], True, monkeypatch)
        assert "⚙" not in entries[0]


class TestDashboardExternalAgent:
    """Dashboard row: badge in the annotation cluster, note in the
    widened state cell. Captures `_addstr` calls on a mock stdscr."""

    def _render_texts(self, monkeypatch, projects):
        from unittest.mock import MagicMock
        from dashboard import Dashboard

        monkeypatch.setattr("dashboard.tmux_cmd", lambda *a, **k: "")
        monkeypatch.setattr("dashboard.hooks_configured", lambda: True)
        monkeypatch.setattr("dashboard.hooks_log_warning", lambda: "")
        monkeypatch.setattr("dashboard.disable_all_hooks_warning",
                            lambda *a, **kw: "")
        monkeypatch.setattr("dashboard.managed_hooks_only_warning",
                            lambda *a, **kw: "")
        monkeypatch.setattr("dashboard.shell_cluster_warnings", lambda p: [])
        monkeypatch.setattr("dashboard.hook_silence_warnings", lambda p: [])
        monkeypatch.setattr("dashboard.errors_log_burst_warning", lambda: "")
        monkeypatch.setattr("dashboard.get_session", lambda: "0")
        monkeypatch.setattr("dashboard.touch_popup_session", lambda: None)
        monkeypatch.setattr("dashboard.read_cache_file", lambda *a, **k: "")
        monkeypatch.setattr("dashboard.format_elapsed", lambda ts: "")
        monkeypatch.setattr("dashboard.format_dir", lambda d, col, w: d)
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: None)
        import curses as _curses
        monkeypatch.setattr(_curses, "color_pair", lambda n: 0)

        texts = []
        monkeypatch.setattr(
            Dashboard, "_addstr",
            lambda self, stdscr, y, x, text, attr=0, max_col=0:
                texts.append(text))
        d = Dashboard(initial_mode="dashboard")
        d.projects = projects
        stdscr = MagicMock()
        stdscr.getmaxyx.return_value = (40, 200)
        d.render(stdscr)
        return texts

    def test_badge_rendered_dim_in_cluster(self, monkeypatch):
        projects = [_make_project("sidekick", "IDLE", pane_count=2,
                                  external_agents=("kimi",))]
        texts = self._render_texts(monkeypatch, projects)
        assert "⚙kimi" in texts

    def test_shell_row_note_after_state(self, monkeypatch):
        projects = [_make_project("kimionly", "SHELL",
                                  external_agents=("kimi",))]
        texts = self._render_texts(monkeypatch, projects)
        assert " (kimi)" in texts
        # State cell still renders plain SHELL — not faked.
        assert any(t.strip().endswith("SHELL") or "SHELL" in t
                   for t in texts)

    def test_no_badge_for_claude_only(self, monkeypatch):
        projects = [_make_project("plain", "IDLE")]
        texts = self._render_texts(monkeypatch, projects)
        assert not any("⚙" in t for t in texts)
        assert not any("(kimi)" in t for t in texts)


class TestAllowlistMembership:
    """The allowlist decides which panes get a presence badge. Its two
    halves have different epistemic status — one name was measured
    against a running pane, the rest are the CLIs' binary names — and
    one name must never appear at all."""

    def test_claude_is_never_an_external_agent(self):
        """The badge marks a pane ccm shows but does not track. Claude
        is the pane it *does* track, so listing it would have one pane
        claim tracked and untracked at once — the asymmetry the sidekick
        diagram exists to draw. Easy to add by reflex when someone
        extends the list by vendor rather than by binary name."""
        for name in ("claude", "claude-code", "Claude Code"):
            assert name not in ccm_constants.EXTERNAL_AGENT_COMMANDS

    def test_entries_are_process_names_not_vendors(self):
        """The set is matched against `pane_current_command`, so it has
        to hold what tmux reports — a binary name. Vendor names
        (`openai`, `anthropic`, `google`) would silently never match,
        leaving a badge that looks configured but never appears."""
        for name in ("openai", "anthropic", "google", "xai", "moonshot"):
            assert name not in ccm_constants.EXTERNAL_AGENT_COMMANDS
        assert all(n == n.lower() and " " not in n
                   for n in ccm_constants.EXTERNAL_AGENT_COMMANDS)

    def test_diagram_examples_are_covered(self):
        """assets/sidekick-model.svg names Codex CLI, Gemini CLI and
        Kimi Code as sidekicks. A reader running one of those expects
        the badge the figure shows, so the figure and the allowlist have
        to move together."""
        for name in ("codex", "gemini", "kimi"):
            assert name in ccm_constants.EXTERNAL_AGENT_COMMANDS, (
                f"{name} is named in the sidekick diagram but would get "
                "no presence badge")


class TestPlatformSuffixedBinaries:
    """A launcher symlink can resolve to a platform-suffixed binary,
    and tmux reports the RESOLVED (truncated) name — Grok Build's
    `grok` arrives as `grok-macos-aarc` (measured,
    grok 0.2.118). Enumerating every platform/arch spelling, and
    guessing tmux's truncation width, is the fixed-shape mistake this
    project keeps paying for; a prefix stands in for the family."""

    @pytest.mark.parametrize("command,expected", [
        ("grok-macos-aarc", "grok"),      # measured, truncated by tmux
        ("grok-macos-aarch64", "grok"),   # untruncated
        ("grok-linux-x86_64", "grok"),    # another platform
        ("grok", "grok"),                 # plain launcher, still fine
        ("kimi", "kimi"),                 # exact-match set unaffected
    ])
    def test_known_agents_resolve_to_a_short_name(self, command, expected):
        assert ccm_constants.external_agent_name(command) == expected

    @pytest.mark.parametrize("command", [
        "grokking-notes",  # prefix must require the separator
        "zsh", "vim", "node", "",
    ])
    def test_unrelated_commands_do_not_match(self, command):
        assert ccm_constants.external_agent_name(command) == ""

    def test_claude_never_resolves_as_an_external_agent(self):
        """The pane ccm TRACKS must never also claim the presence
        badge — one pane cannot be both sides of the asymmetry."""
        assert ccm_constants.external_agent_name("claude") == ""

    def test_badge_label_uses_the_short_name(self):
        """`⚙grok-macos-aarc` is neither readable nor stable across
        machines; the badge carries the canonical name."""
        panes = [("0:2", "1", "%1", "grok-macos-aarc", "0", "40", "")]
        assert ccm_core._resolve_external_agent_panes(panes, "0:2") == ("grok",)
