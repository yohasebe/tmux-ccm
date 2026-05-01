"""Tests for dashboard.py — static/pure helpers that don't require curses."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import dashboard  # noqa: E402 — verify module imports cleanly
from dashboard import Dashboard


# ─── Module imports ───

class TestDashboardImports:
    """Smoke test: dashboard.py must import all the canary helpers
    it surfaces in the footer. Catches typos in the `from ccm_core
    import ...` block."""

    def test_canary_helpers_imported(self):
        assert hasattr(dashboard, "hooks_log_warning")
        assert hasattr(dashboard, "disable_all_hooks_warning")
        assert hasattr(dashboard, "managed_hooks_only_warning")
        assert hasattr(dashboard, "shell_cluster_warnings")
        assert hasattr(dashboard, "hooks_configured")


# ─── render() smoke tests ───
#
# These tests instantiate a Dashboard with all external side-effect
# helpers stubbed out, then call render() against a MagicMock stdscr.
# They are not pixel-level rendering tests — they just verify that
# render() executes to completion without raising on a few realistic
# project-list shapes. Catches the class of bug where a code edit
# references a local variable before it is bound (UnboundLocalError),
# which the old test suite never exercised because dashboard.py was
# untested at the function-body level.

def _stub_dashboard_environment(monkeypatch):
    """Mute every external call dashboard.render() reaches into so
    Dashboard() can be constructed and render() can run in a pytest
    process without curses, without tmux, and without filesystem
    side effects."""
    import curses as _curses

    monkeypatch.setattr("dashboard.tmux_cmd", lambda *a, **k: "")
    monkeypatch.setattr("dashboard.hooks_configured", lambda: True)
    monkeypatch.setattr("dashboard.hooks_log_warning", lambda: "")
    monkeypatch.setattr("dashboard.disable_all_hooks_warning", lambda: "")
    monkeypatch.setattr("dashboard.managed_hooks_only_warning", lambda: "")
    monkeypatch.setattr("dashboard.shell_cluster_warnings", lambda p: [])
    monkeypatch.setattr("dashboard.get_session", lambda: "0")
    monkeypatch.setattr("dashboard.touch_popup_session", lambda: None)
    monkeypatch.setattr("dashboard.read_hook_signal", lambda d: None)
    monkeypatch.setattr("dashboard.read_cache_file", lambda *a, **k: "")
    monkeypatch.setattr("dashboard.format_elapsed", lambda ts: "")
    monkeypatch.setattr("dashboard.format_dir", lambda d, col, w: d)
    monkeypatch.setattr(_curses, "color_pair", lambda n: 0)


def _make_mock_stdscr(width=200, height=40):
    from unittest.mock import MagicMock
    stdscr = MagicMock()
    stdscr.getmaxyx.return_value = (height, width)
    return stdscr


class TestRenderSmoke:
    def test_render_empty_project_list(self, monkeypatch):
        """The most basic render path: no projects at all."""
        _stub_dashboard_environment(monkeypatch)
        d = Dashboard(initial_mode="dashboard")
        d.projects = []
        d.render(_make_mock_stdscr())  # must not raise

    def test_render_with_projects(self, monkeypatch):
        """Renders a list with several state variations to walk
        more of the row-rendering code path."""
        _stub_dashboard_environment(monkeypatch)
        import ccm_core
        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
            ccm_core.Project("0:2", "2", "beta",  "/tmp/b", "BUSY"),
            ccm_core.Project("0:3", "3", "gamma", "/tmp/c", "IDLE"),
            ccm_core.Project("0:4", "4", "delta", "/tmp/d", "PERMIT"),
            ccm_core.Project("0:5", "5", "epsilon", "/tmp/e", "SHELL"),
        ]
        d.render(_make_mock_stdscr())

    def test_render_suppresses_completed_marker_for_non_idle(self, monkeypatch):
        """`* elapsed` is the "recently completed" marker — it must
        render only for IDLE projects. A project whose
        `@ccm_completed_at` was set on a BUSY/PERMIT → IDLE
        transition and then bounced back to BUSY (new prompt within
        COMPLETED_AT_TIMEOUT) would otherwise render a misleading
        `◉ BUSY * 5s`. We inject a counting stub for `format_elapsed`
        so the call count proves whether the renderer attempted to
        format the marker for non-IDLE projects.
        """
        _stub_dashboard_environment(monkeypatch)

        calls = {"format_elapsed": []}
        monkeypatch.setattr(
            "dashboard.format_elapsed",
            lambda ts: (calls["format_elapsed"].append(ts), "5s")[1],
        )

        import ccm_core
        recent = int(__import__("time").time()) - 5  # 5 seconds ago

        d = Dashboard(initial_mode="dashboard")
        # IDLE with completed_at — should call format_elapsed
        # BUSY with completed_at — must NOT call format_elapsed
        # PERMIT with completed_at — must NOT call format_elapsed
        d.projects = [
            ccm_core.Project("0:1", "1", "idle-recent", "/tmp/a",
                             "IDLE", completed_at=recent),
            ccm_core.Project("0:2", "2", "busy-stale-marker", "/tmp/b",
                             "BUSY", completed_at=recent),
            ccm_core.Project("0:3", "3", "permit-stale-marker", "/tmp/c",
                             "PERMIT", completed_at=recent),
        ]
        d.render(_make_mock_stdscr())

        # Only the IDLE project should have triggered format_elapsed
        assert len(calls["format_elapsed"]) == 1, (
            f"format_elapsed should be called exactly once (for the "
            f"IDLE project); got {len(calls['format_elapsed'])} calls"
        )

    def test_render_with_canary_warnings_active(self, monkeypatch):
        """Exercises every canary banner row at once. Catches
        off-by-one layout bugs and reference-before-assignment errors
        in the canary block — a stray edit that touches a variable
        before it is bound would raise UnboundLocalError on every
        dashboard open, and this test makes that immediate.
        """
        _stub_dashboard_environment(monkeypatch)
        # Re-stub three of the canaries to return non-empty messages
        monkeypatch.setattr("dashboard.hooks_log_warning",
                            lambda: "hooks.log too big — clear it")
        monkeypatch.setattr("dashboard.disable_all_hooks_warning",
                            lambda: "disableAllHooks set")
        monkeypatch.setattr("dashboard.managed_hooks_only_warning",
                            lambda: "managed hooks only")
        monkeypatch.setattr("dashboard.shell_cluster_warnings",
                            lambda p: ["alpha: silent exit cluster"])

        import ccm_core
        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "SHELL"),
        ]
        d.hooks_on = False  # also exercises the "Hooks: OFF" banner
        d.render(_make_mock_stdscr())


# ─── _strip_osc8_hyperlinks ───

class TestStripOsc8Hyperlinks:
    """Curses cannot render OSC 8 hyperlinks; the wrapper bytes leak
    into the preview as `^]8;id=...;URL` literal text. Stripping the
    wrappers and keeping the visible label preserves the user's
    intent without breaking the layout.
    """

    def test_strips_complete_sequence_keeps_label(self):
        text = "\x1b]8;id=abc;https://example.com/x\x1b\\#42\x1b]8;;\x1b\\"
        assert Dashboard._strip_osc8_hyperlinks(text) == "#42"

    def test_strips_with_bel_terminator(self):
        # Some terminals emit BEL (\x07) as the OSC 8 terminator.
        text = "\x1b]8;;https://example.com\x07link text\x1b]8;;\x07"
        assert Dashboard._strip_osc8_hyperlinks(text) == "link text"

    def test_keeps_surrounding_text(self):
        text = "PR \x1b]8;;https://x.com\x1b\\#11\x1b]8;;\x1b\\ done"
        assert Dashboard._strip_osc8_hyperlinks(text) == "PR #11 done"

    def test_drops_orphan_open_sequence(self):
        # Capture cut off mid-sequence — must not leave dangling chars.
        text = "leftover \x1b]8;id=foo;https://x.com\x1b\\rest"
        result = Dashboard._strip_osc8_hyperlinks(text)
        assert "\x1b]8" not in result

    def test_preserves_non_osc8_escapes(self):
        # SGR colour codes (\x1b[...m) must survive untouched.
        text = "\x1b[31mred\x1b[0m"
        assert Dashboard._strip_osc8_hyperlinks(text) == text

    def test_empty_input(self):
        assert Dashboard._strip_osc8_hyperlinks("") == ""


# ─── _strip_last_grapheme ───

class TestStripLastGrapheme:
    """Backspace should delete one user-perceived character (grapheme cluster)."""

    def test_ascii(self):
        assert Dashboard._strip_last_grapheme("abc") == "ab"

    def test_cjk(self):
        assert Dashboard._strip_last_grapheme("日本語") == "日本"

    def test_combining_mark(self):
        # é as e + combining acute accent (U+0301)
        assert Dashboard._strip_last_grapheme("cafe\u0301") == "caf"

    def test_multiple_combining_marks(self):
        # a + combining tilde + combining acute
        assert Dashboard._strip_last_grapheme("xa\u0303\u0301") == "x"

    def test_single_char(self):
        assert Dashboard._strip_last_grapheme("a") == ""

    def test_empty(self):
        assert Dashboard._strip_last_grapheme("") == ""

    def test_zwj_sequence(self):
        # 👨‍💻 = 👨 + ZWJ + 💻
        assert Dashboard._strip_last_grapheme("\U0001f468\u200d\U0001f4bb") == ""

    def test_zwj_after_text(self):
        assert Dashboard._strip_last_grapheme("x\U0001f468\u200d\U0001f4bb") == "x"


# Width helpers moved to ccm_render and re-exported via ccm_core.
# Dashboard's previous static methods are gone — its imports the
# canonical helpers, and the tests live in test_ccm_core.py.
