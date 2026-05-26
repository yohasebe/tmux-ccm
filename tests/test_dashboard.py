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
    monkeypatch.setattr("dashboard.errors_log_burst_warning", lambda: "")
    monkeypatch.setattr("dashboard.get_session", lambda: "0")
    monkeypatch.setattr("dashboard.touch_popup_session", lambda: None)
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

    def test_elapsed_marker_does_not_perturb_path_column(self, monkeypatch):
        """Whether or not a project has the `* elapsed` marker, every
        path must render at the same X column. Before this change,
        elapsed lived inside the inline annotation cluster between
        name and path, which pushed COL_DIR right (and every project
        row's path with it) whenever the marker was shown. With
        elapsed relocated to a right-anchored slot, the path column
        stays put across refresh ticks.

        We render the SAME project list twice — once with no recent
        completion (no elapsed) and once with `completed_at = now -
        10s` (elapsed shown) — and assert that the X position of
        `format_dir` calls is identical in both renders."""
        _stub_dashboard_environment(monkeypatch)

        # Track every (x, dir_str) pair format_dir produces. Using
        # the call's `prefix_len` argument (= the x ccm renders the
        # path at) is the cleanest signal — that's exactly the
        # "where does the path start" question this test is about.
        calls_no_elapsed = []
        calls_with_elapsed = []
        active_log = calls_no_elapsed

        def recording_format_dir(directory, prefix_len, cols):
            active_log.append((prefix_len, directory))
            return directory  # identity is fine for the assertion
        monkeypatch.setattr("dashboard.format_dir", recording_format_dir)

        # format_elapsed must return a non-empty 3-char string for
        # the "with elapsed" render so the right-anchored slot is
        # actually exercised.
        monkeypatch.setattr("dashboard.format_elapsed", lambda ts: "10s" if ts else "")

        import ccm_core
        now = __import__("time").time()
        projects_no_elapsed = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
            ccm_core.Project("0:2", "2", "beta",  "/tmp/b", "IDLE"),
        ]
        projects_with_elapsed = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE",
                             completed_at=int(now) - 10),
            ccm_core.Project("0:2", "2", "beta",  "/tmp/b", "IDLE"),
        ]

        d = Dashboard(initial_mode="dashboard")

        d.projects = projects_no_elapsed
        d.render(_make_mock_stdscr())

        active_log = calls_with_elapsed
        d.projects = projects_with_elapsed
        d.render(_make_mock_stdscr())

        # Compare the X positions of the path column across renders
        x_no_elapsed = [x for x, _ in calls_no_elapsed]
        x_with_elapsed = [x for x, _ in calls_with_elapsed]
        assert x_no_elapsed == x_with_elapsed, (
            f"path column X drifted when elapsed appeared: "
            f"no_elapsed={x_no_elapsed}, with_elapsed={x_with_elapsed}"
        )

    def test_render_with_bg_section_visible(self, monkeypatch):
        """`b` key reveals the background-sessions block below the
        project list. The renderer must tolerate both populated and
        empty bg_sessions without raising — empty is the more common
        case once a user disables their last `--bg` job but leaves
        the section toggled on."""
        _stub_dashboard_environment(monkeypatch)
        import ccm_core
        import ccm_agentview

        d = Dashboard(initial_mode="dashboard")
        d.projects = [ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE")]
        d.bg_visible = True

        # Populated case
        d.bg_sessions = [
            ccm_agentview.BgSession(
                short="abcd1234", pid=100, cwd="/tmp/proj",
                name="agent demo", state="WORKING", raw_state="working",
                tempo="active", cli_version="2.1.139",
                session_id="00000000-0000-0000-0000-000000000000",
                created_at=__import__("time").time() - 120,
                updated_at=__import__("time").time(),
                source="slash",
            )
        ]
        d.render(_make_mock_stdscr())  # must not raise

        # Empty case (daemon running but no active sessions)
        d.bg_sessions = []
        monkeypatch.setattr("ccm_agentview.daemon_running", lambda: True)
        d.render(_make_mock_stdscr())  # must not raise

        # Daemon-down case
        monkeypatch.setattr("ccm_agentview.daemon_running", lambda: False)
        d.render(_make_mock_stdscr())  # must not raise

    def test_bg_row_enter_opens_new_window_and_attaches(self, monkeypatch):
        """Enter on a bg row must create a non-ccm tmux window and
        dispatch `claude attach <short>` to it. We capture every
        tmux_cmd call and assert the sequence: new-window → send-keys
        with the right command → select-window.

        The new-window invocation must keep `-t <session>:` adjacent
        (any flag inserted between them makes tmux read the flag name
        as the target — a regression we hit once already)."""
        _stub_dashboard_environment(monkeypatch)

        captured = []
        def fake_tmux(*args, **kwargs):
            captured.append(args)
            if args and args[0] == "new-window":
                return "0:5"
            return ""

        monkeypatch.setattr("dashboard.tmux_cmd", fake_tmux)
        monkeypatch.setattr("dashboard.get_session", lambda: "0")
        monkeypatch.setattr("os.path.isdir", lambda p: True)

        import ccm_agentview
        d = Dashboard(initial_mode="dashboard")
        d.projects = []
        d.bg_visible = True
        d.bg_sessions = [
            ccm_agentview.BgSession(
                short="abcd1234", pid=100, cwd="/tmp/proj",
                name="my agent", state="WORKING", raw_state="working",
                tempo="active", cli_version="2.1.139",
                session_id="x", created_at=1.0, updated_at=2.0,
                source="slash",
            ),
        ]
        d.selected = 0  # First bg row

        action = d._handle_key(13, _make_mock_stdscr())  # Enter
        assert action == "attached"

        call_names = [c[0] if c else "" for c in captured]
        assert "new-window" in call_names
        assert "send-keys" in call_names
        assert "select-window" in call_names

        send_keys_call = next(c for c in captured if c and c[0] == "send-keys")
        assert "claude attach abcd1234" in send_keys_call

        # -t must be immediately followed by the session target.
        # Inserting `-c <cwd>` between them broke new-window silently
        # (tmux parsed `-c` as the target value and exited non-zero).
        new_window_call = next(c for c in captured if c and c[0] == "new-window")
        t_idx = new_window_call.index("-t")
        assert new_window_call[t_idx + 1] == "0:", (
            f"-t must point directly at the session target, got "
            f"{new_window_call[t_idx + 1]!r} (full call: {new_window_call})"
        )

    def test_bg_attach_skips_cwd_starting_with_dash(self, monkeypatch):
        """tmux `new-window -c <path>` parses any `-`-prefixed arg as
        a flag, so a malformed cwd like '-foo' must NOT be passed —
        we silently drop the -c pair instead (the new window then
        inherits the caller's pwd, which is the safest fallback)."""
        _stub_dashboard_environment(monkeypatch)

        captured = []
        def fake_tmux(*args, **kwargs):
            captured.append(args)
            if args and args[0] == "new-window":
                return "0:5"
            return ""
        monkeypatch.setattr("dashboard.tmux_cmd", fake_tmux)
        monkeypatch.setattr("dashboard.get_session", lambda: "0")
        monkeypatch.setattr("os.path.isdir", lambda p: True)

        import ccm_agentview
        d = Dashboard(initial_mode="dashboard")
        d.projects = []
        d.bg_visible = True
        d.bg_sessions = [
            ccm_agentview.BgSession(
                short="abcd1234", pid=1, cwd="-evil",
                name="x", state="WORKING", raw_state="working",
                tempo="active", cli_version="", session_id="",
                created_at=None, updated_at=None, source="",
            ),
        ]
        d.selected = 0
        d._handle_key(13, _make_mock_stdscr())

        new_window_call = next(c for c in captured if c and c[0] == "new-window")
        assert "-c" not in new_window_call, (
            f"new-window must not receive -c with a -prefixed path: "
            f"{new_window_call}"
        )

    def test_bg_attach_refuses_invalid_short(self, monkeypatch):
        """Defence-in-depth: even if a malformed short somehow reached
        bg_sessions (e.g. someone bypassed the agentview filter), the
        attach handler must refuse before tmux send-keys.

        Hits the receiving shell's metachar-interpretation surface."""
        _stub_dashboard_environment(monkeypatch)

        captured = []
        monkeypatch.setattr("dashboard.tmux_cmd",
                            lambda *a, **k: captured.append(a) or "")
        monkeypatch.setattr("dashboard.get_session", lambda: "0")

        import ccm_agentview
        d = Dashboard(initial_mode="dashboard")
        d.projects = []
        d.bg_visible = True
        d.bg_sessions = [
            ccm_agentview.BgSession(
                short="abc;rm", pid=1, cwd="/tmp",
                name="x", state="WORKING", raw_state="working",
                tempo="active", cli_version="", session_id="",
                created_at=None, updated_at=None, source="",
            ),
        ]
        d.selected = 0
        action = d._handle_key(13, _make_mock_stdscr())

        assert action != "attached"
        # No tmux operations should have been dispatched
        assert not any(c and c[0] in ("new-window", "send-keys") for c in captured)

    def test_b_key_toggle_on_fetches_immediately(self, monkeypatch):
        """Pressing `b` to show the section must do a synchronous
        fetch so the first paint after toggle is already populated.
        Without this the user sees "Background sessions (0)" for one
        refresh cycle before the real list appears."""
        _stub_dashboard_environment(monkeypatch)

        fetch_calls = []
        monkeypatch.setattr(
            "dashboard.Dashboard._fetch_bg_sessions",
            lambda self: fetch_calls.append("called") or [],
        )

        d = Dashboard(initial_mode="dashboard")
        d.projects = []
        d.bg_visible = False
        d.bg_sessions = []

        fetch_calls.clear()
        d._handle_key(ord("b"), _make_mock_stdscr())
        assert d.bg_visible is True
        assert len(fetch_calls) == 1, "toggle-on must fetch once"

        # Toggle off should NOT fetch (saves I/O)
        fetch_calls.clear()
        d._handle_key(ord("b"), _make_mock_stdscr())
        assert d.bg_visible is False
        assert fetch_calls == [], "toggle-off must not fetch"

    def test_enter_with_stale_bg_selection_does_not_crash(self, monkeypatch):
        """Race: user selected a bg row, refresh tick wiped the bg
        list, render() hasn't fired yet to clamp self.selected, user
        presses Enter. The Enter dispatch must NOT IndexError into
        the project list — it should silently do nothing until the
        next render normalises the selection."""
        _stub_dashboard_environment(monkeypatch)
        import ccm_core

        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
            ccm_core.Project("0:2", "2", "beta",  "/tmp/b", "IDLE"),
        ]
        d.bg_visible = True
        d.bg_sessions = []          # the refresh tick just cleared it
        d.selected = 2              # stale — pointed at the (now gone) bg row

        # Must not raise. Returns "" (no action).
        action = d._handle_key(13, _make_mock_stdscr())
        assert action != "attached"

    def test_b_key_clamps_selection_when_hiding_bg(self, monkeypatch):
        """Toggling bg off while a bg row is selected must move the
        ▶ marker back into the project list so subsequent navigation
        starts from a sane position."""
        _stub_dashboard_environment(monkeypatch)

        import ccm_core
        import ccm_agentview
        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
            ccm_core.Project("0:2", "2", "beta",  "/tmp/b", "IDLE"),
        ]
        d.bg_visible = True
        d.bg_sessions = [
            ccm_agentview.BgSession(
                short="abcd1234", pid=100, cwd="/tmp",
                name="x", state="WORKING", raw_state="working",
                tempo="active", cli_version="", session_id="x",
                created_at=None, updated_at=None, source="",
            ),
        ]
        d.selected = 2  # bg row (index 2 = projects(0,1) then bg(2))

        d._handle_key(ord("b"), _make_mock_stdscr())

        assert d.bg_visible is False
        # Selection must have moved back into the project list
        assert 0 <= d.selected < len(d.projects)

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


