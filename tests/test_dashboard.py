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
    monkeypatch.setattr("dashboard.disable_all_hooks_warning", lambda *a, **kw: "")
    monkeypatch.setattr("dashboard.managed_hooks_only_warning", lambda *a, **kw: "")
    monkeypatch.setattr("dashboard.shell_cluster_warnings", lambda p: [])
    monkeypatch.setattr("dashboard.hook_silence_warnings", lambda p: [])
    monkeypatch.setattr("dashboard.errors_log_burst_warning", lambda: "")
    monkeypatch.setattr("dashboard.get_session", lambda: "0")
    monkeypatch.setattr("dashboard.touch_popup_session", lambda: None)
    monkeypatch.setattr("dashboard.read_cache_file", lambda *a, **k: "")
    monkeypatch.setattr("dashboard.format_elapsed", lambda ts: "")
    monkeypatch.setattr("dashboard.format_dir", lambda d, col, w: d)
    # signal_age_suffix (via ccm_render) reads the per-project hook
    # signal, which resolves @ccm_session_id through live tmux.
    import ccm_signals
    monkeypatch.setattr(ccm_signals, "read_hook_signal", lambda d, session_id=None: None)
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

    def test_render_current_forces_full_repaint(self, monkeypatch):
        """Every render must call stdscr.redrawwin() before drawing.

        tmux's popup overlay clipping (as of 3.7b) lets a pane
        streaming double-width CJK output behind the popup clobber
        cells INSIDE the popup. curses diffs against its own model
        of the physical screen, so without redrawwin() it believes
        the clobbered cells are still correct and never repaints
        them — the garbage sticks. redrawwin() marks the window
        corrupted so the following refresh re-emits every cell,
        self-healing within one render tick (observed live
, dashboard over a streaming CJK response)."""
        _stub_dashboard_environment(monkeypatch)
        for mode in ("dashboard", "tree", "menu"):
            d = Dashboard(initial_mode=mode)
            d.projects = []
            stdscr = _make_mock_stdscr()
            d._render_current(stdscr)
            assert stdscr.redrawwin.called, (
                f"mode={mode}: _render_current must force a full "
                "repaint via redrawwin()"
            )

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
                            lambda *a, **kw: "disableAllHooks set")
        monkeypatch.setattr("dashboard.managed_hooks_only_warning",
                            lambda *a, **kw: "managed hooks only")
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




class TestFastTick:
    """Hybrid refresh: `_fast_tick` overlays pushed-state TRANSITIONS
    (`@ccm_prev_state` changes between ticks) onto the displayed list
    so hook-driven changes appear in ~FAST_TICK_INTERVAL instead of
    waiting out the 2 s slow poll. Transition-gating is the contract
    under test: reacting to the absolute pushed value would re-fight
    the slow path wherever they legitimately diverge (HOLD_NO_WRITE
    displays), producing a 2 s flicker cycle."""

    def _dash(self, monkeypatch, listing):
        """Dashboard with one IDLE project and a mutable pushed-state
        listing. `listing` is a single-element list holding the
        list-windows output so tests can swap it between ticks."""
        _stub_dashboard_environment(monkeypatch)
        import ccm_core
        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
            ccm_core.Project("0:2", "2", "beta", "/tmp/b", "BUSY"),
        ]
        monkeypatch.setattr("dashboard.tmux_cmd",
                            lambda *a, **k: listing[0])
        return d

    def test_first_sample_is_baseline_only(self, monkeypatch):
        listing = ["0:1\talpha\tBUSY\n0:2\tbeta\tBUSY"]
        d = self._dash(monkeypatch, listing)
        d._fast_tick()
        assert d.projects[0].state == "IDLE"
        assert d.data_dirty is False

    def test_transition_overlays_state_and_marks_dirty(self, monkeypatch):
        listing = ["0:1\talpha\tIDLE\n0:2\tbeta\tBUSY"]
        d = self._dash(monkeypatch, listing)
        d._fast_tick()  # baseline
        listing[0] = "0:1\talpha\tPERMIT\n0:2\tbeta\tBUSY"
        d._fast_tick()
        assert d.projects[0].state == "PERMIT"
        assert d.projects[1].state == "BUSY"  # untouched
        assert d.data_dirty is True

    def test_steady_divergence_never_overlaid(self, monkeypatch):
        """Pushed value differs from the displayed state but does not
        CHANGE between ticks (a HOLD_NO_WRITE display) — the fast
        tick must not touch it, however many ticks pass."""
        listing = ["0:1\talpha\tBUSY\n0:2\tbeta\tBUSY"]
        d = self._dash(monkeypatch, listing)
        for _ in range(5):
            d._fast_tick()
        assert d.projects[0].state == "IDLE"
        assert d.data_dirty is False

    def test_empty_pushed_value_ignored(self, monkeypatch):
        """A window whose @ccm_prev_state is unset (fresh window, no
        commit yet) transitions "" → nothing; an empty NEW value must
        also never blank a displayed state."""
        listing = ["0:1\talpha\tIDLE\n0:2\tbeta\tBUSY"]
        d = self._dash(monkeypatch, listing)
        d._fast_tick()
        listing[0] = "0:1\talpha\t\n0:2\tbeta\tBUSY"
        d._fast_tick()
        assert d.projects[0].state == "IDLE"
        assert d.data_dirty is False

    def test_scan_failure_degrades_to_polling(self, monkeypatch):
        """A broken fast tick must not kill the refresh thread — it
        logs and returns, leaving plain 2 s polling in charge."""
        _stub_dashboard_environment(monkeypatch)
        import ccm_core
        d = Dashboard(initial_mode="dashboard")
        d.projects = [ccm_core.Project("0:1", "1", "a", "/tmp/a", "IDLE")]

        def _boom(*a, **k):
            raise RuntimeError("tmux gone")
        monkeypatch.setattr("dashboard.tmux_cmd", _boom)
        d._fast_tick()  # must not raise
        assert d.projects[0].state == "IDLE"


class TestStableDisplayOrder:
    """`_set_projects_stable` freezes the row order for the dashboard's
    lifetime and pins the selection to the same project by identity, so
    live state changes (which re-sort build_project_list's output)
    don't reshuffle rows under the cursor mid-interaction."""

    def _dash(self, monkeypatch):
        _stub_dashboard_environment(monkeypatch)
        return Dashboard(initial_mode="dashboard")

    def _mk(self, *specs):
        import ccm_core
        return [ccm_core.Project(t, t.split(":")[1], n, f"/tmp/{n}", s)
                for (t, n, s) in specs]

    def test_first_build_sets_frozen_order(self, monkeypatch):
        d = self._dash(monkeypatch)
        d._display_order = []
        d.selected = 0
        d.projects = []
        p = self._mk(("0:1", "a", "IDLE"), ("0:2", "b", "BUSY"))
        d._set_projects_stable(p)
        assert d._display_order == ["0:1", "0:2"]
        assert [x.name for x in d.projects] == ["a", "b"]

    def test_state_change_does_not_reorder(self, monkeypatch):
        d = self._dash(monkeypatch)
        d._display_order = []
        d.selected = 0
        d.projects = []
        d._set_projects_stable(self._mk(("0:1", "a", "IDLE"),
                                        ("0:2", "b", "BUSY")))
        # Next refresh: build_project_list would sort PERMIT first, so
        # it hands us b(PERMIT) before a(IDLE). Frozen order must win.
        d._set_projects_stable(self._mk(("0:2", "b", "PERMIT"),
                                        ("0:1", "a", "IDLE")))
        assert [x.name for x in d.projects] == ["a", "b"]
        assert d.projects[1].state == "PERMIT"  # state updated in place

    def test_selection_follows_project_by_identity(self, monkeypatch):
        d = self._dash(monkeypatch)
        d._display_order = []
        d.projects = []
        d._set_projects_stable(self._mk(("0:1", "a", "IDLE"),
                                        ("0:2", "b", "IDLE"),
                                        ("0:3", "c", "IDLE")))
        d.selected = 2  # cursor on "c"
        # "a" (above the cursor) vanishes → c's index would shift 2→1.
        d._set_projects_stable(self._mk(("0:2", "b", "IDLE"),
                                        ("0:3", "c", "IDLE")))
        assert d.projects[d.selected].name == "c", (
            "selection must stay on the same project, not the same index")

    def test_new_project_appends_and_keeps_frozen(self, monkeypatch):
        d = self._dash(monkeypatch)
        d._display_order = []
        d.projects = []
        d._set_projects_stable(self._mk(("0:1", "a", "IDLE"),
                                        ("0:2", "b", "IDLE")))
        # A newly-opened PERMIT project must NOT jump above the frozen
        # rows — it appends at the end.
        d._set_projects_stable(self._mk(("0:3", "c", "PERMIT"),
                                        ("0:1", "a", "IDLE"),
                                        ("0:2", "b", "IDLE")))
        assert [x.name for x in d.projects] == ["a", "b", "c"]
        assert d._display_order == ["0:1", "0:2", "0:3"]

    def test_vanished_project_returns_to_slot(self, monkeypatch):
        d = self._dash(monkeypatch)
        d._display_order = []
        d.projects = []
        d._set_projects_stable(self._mk(("0:1", "a", "IDLE"),
                                        ("0:2", "b", "IDLE"),
                                        ("0:3", "c", "IDLE")))
        d._set_projects_stable(self._mk(("0:1", "a", "IDLE"),
                                        ("0:3", "c", "IDLE")))  # b gone
        d._set_projects_stable(self._mk(("0:1", "a", "IDLE"),
                                        ("0:2", "b", "IDLE"),
                                        ("0:3", "c", "IDLE")))  # b back
        assert [x.name for x in d.projects] == ["a", "b", "c"]


class TestResolvePreviewPane:
    """`_resolve_preview_pane` picks the TRACKED claude pane to
    preview, not just the focused pane — so a split window with claude
    in a non-active pane (or a CCM_IGNORE'd sidekick focused) previews
    the right session. `capture-pane -t <window>` would otherwise grab
    the active pane."""

    def _pane(self, pane_id, pid, active=False, ignored=False, claude=False):
        from ccm_pane_state import PaneInfo
        return PaneInfo(pane_id, pid, active, "cmd", ignored,
                        int(pid) + 1 if claude else None)

    def _dash(self, monkeypatch, panes):
        _stub_dashboard_environment(monkeypatch)
        d = Dashboard(initial_mode="dashboard")
        monkeypatch.setattr("dashboard.ps_snapshot", lambda: "")
        monkeypatch.setattr("dashboard.enumerate_window_panes",
                            lambda win, ps: panes)
        return d

    def test_active_claude_pane_used(self, monkeypatch):
        # active pane hosts claude → use it (unchanged common case)
        d = self._dash(monkeypatch, [
            self._pane("%0", "100", active=True, claude=True),
            self._pane("%1", "200")])
        assert d._resolve_preview_pane("0:5") == "%0"

    def test_claude_in_non_active_pane_preferred_over_focus(self, monkeypatch):
        # active pane is a shell; claude is in the OTHER pane → preview
        # the claude pane, not the focused shell.
        d = self._dash(monkeypatch, [
            self._pane("%0", "100", active=True),          # active shell
            self._pane("%1", "200", claude=True)])         # claude
        assert d._resolve_preview_pane("0:5") == "%1"

    def test_ignored_sidekick_excluded(self, monkeypatch):
        # active pane is a CCM_IGNORE'd claude sidekick; the tracked
        # main claude is in a non-active pane → preview the main, never
        # the ignored sidekick.
        d = self._dash(monkeypatch, [
            self._pane("%0", "100", active=True, ignored=True, claude=True),
            self._pane("%1", "200", claude=True)])
        assert d._resolve_preview_pane("0:5") == "%1"

    def test_no_claude_falls_back_to_window(self, monkeypatch):
        d = self._dash(monkeypatch, [
            self._pane("%0", "100", active=True),
            self._pane("%1", "200")])
        assert d._resolve_preview_pane("0:5") == "0:5"

    def test_no_panes_falls_back_to_window(self, monkeypatch):
        d = self._dash(monkeypatch, [])
        assert d._resolve_preview_pane("0:5") == "0:5"


# ─── Project-key selection guard (bg rows) ───

class TestProjectKeySelectionGuard:
    """While the bg section is visible, rows below the project list
    are bg sessions, so `self.selected` can be >= len(projects). The
    project-scoped action keys (p/n/r/i) must no-op there — like
    Enter already does — instead of IndexError-ing into projects[]
    via `_do_preview` / `_do_rename` / `_do_remove` /
    `_do_ignore_toggle`."""

    PROJECT_KEYS = [
        (ord("p"), "_do_preview"),
        (ord("n"), "_do_rename"),
        (ord("r"), "_do_remove"),
        (ord("i"), "_do_ignore_toggle"),
    ]

    def _dash(self, monkeypatch, selected=1, bg_count=1):
        _stub_dashboard_environment(monkeypatch)
        import ccm_core
        import ccm_agentview
        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
        ]
        d.bg_visible = True
        d.bg_sessions = [
            ccm_agentview.BgSession(
                short="abcd1234", pid=100, cwd="/tmp",
                name="x", state="WORKING", raw_state="working",
                tempo="active", cli_version="", session_id="x",
                created_at=None, updated_at=None, source="",
            ),
        ][:bg_count]
        d.selected = selected
        return d

    @pytest.mark.parametrize("key,method", PROJECT_KEYS)
    def test_project_key_on_bg_row_is_noop(self, monkeypatch, key, method):
        # selected=1 with one project → the bg row.
        d = self._dash(monkeypatch, selected=1)
        calls = []
        monkeypatch.setattr(Dashboard, method,
                            lambda self, stdscr: calls.append(method))
        action = d._handle_key(key, _make_mock_stdscr())  # must not raise
        assert action == ""
        assert calls == [], f"{method} must not run on a bg row"

    @pytest.mark.parametrize("key,method", PROJECT_KEYS)
    def test_project_key_on_stale_row_is_noop(self, monkeypatch, key,
                                              method):
        # Stale selection: bg list was wiped by a refresh tick but
        # render() hasn't clamped self.selected yet.
        d = self._dash(monkeypatch, selected=1, bg_count=0)
        calls = []
        monkeypatch.setattr(Dashboard, method,
                            lambda self, stdscr: calls.append(method))
        d._handle_key(key, _make_mock_stdscr())  # must not raise
        assert calls == []

    @pytest.mark.parametrize("key,method", PROJECT_KEYS)
    def test_project_key_on_project_row_still_works(self, monkeypatch,
                                                    key, method):
        d = self._dash(monkeypatch, selected=0)
        calls = []
        monkeypatch.setattr(Dashboard, method,
                            lambda self, stdscr: calls.append(method))
        d._handle_key(key, _make_mock_stdscr())
        assert calls == [method]


# ─── _do_exit_all pane targeting ───

class TestDoExitAllTargetsClaudePane:
    """`_do_exit_all` must resolve the claude-hosting pane and send
    Escape + `/exit` to IT — never to the window target, which tmux
    routes to the window's ACTIVE pane. In a split window with a
    shell focused, sending to the window would inject `exit` into the
    user's shell and kill the pane (the shape `auto_exit_idle`'s
    find_claude_pid resolution already guards against)."""

    def _pane(self, pane_id, pid, active=False, ignored=False,
              claude=False):
        from ccm_pane_state import PaneInfo
        return PaneInfo(pane_id, pid, active, "cmd", ignored,
                        int(pid) + 1 if claude else None)

    def _dash(self, monkeypatch, panes, answer="y"):
        _stub_dashboard_environment(monkeypatch)
        import ccm_core
        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
        ]
        monkeypatch.setattr("dashboard.ps_snapshot", lambda: "")
        monkeypatch.setattr("dashboard.enumerate_window_panes",
                            lambda win, ps: panes)
        monkeypatch.setattr(Dashboard, "_prompt",
                            lambda self, s, text: answer)
        monkeypatch.setattr(Dashboard, "_show_message",
                            lambda self, s, msg, duration=1: None)
        monkeypatch.setattr(Dashboard, "_trigger_rebuild",
                            lambda self: None)
        captured = []
        monkeypatch.setattr("dashboard.tmux_cmd",
                            lambda *a, **k: captured.append(a) or "")
        return d, captured

    def test_exit_goes_to_claude_pane_not_active_shell(self,
                                                       monkeypatch):
        # Split window: active pane is a shell, claude lives in the
        # OTHER pane. Every keystroke must target the claude pane.
        panes = [
            self._pane("%0", "100", active=True),            # active shell
            self._pane("%1", "200", claude=True),            # claude
        ]
        d, captured = self._dash(monkeypatch, panes)
        d._do_exit_all(_make_mock_stdscr())

        sends = [c for c in captured if c and c[0] == "send-keys"]
        assert sends, "expected send-keys calls for the exit sequence"
        for c in sends:
            assert c[2] == "%1", (
                f"send-keys must target the claude pane %1, got {c}"
            )
        assert any("/exit" in c for c in sends)

    def test_exit_skipped_when_no_claude_pane(self, monkeypatch):
        # No pane hosts claude (window transitioning) — there is no
        # safe target, so nothing may be sent.
        panes = [
            self._pane("%0", "100", active=True),
            self._pane("%1", "200"),
        ]
        d, captured = self._dash(monkeypatch, panes)
        d._do_exit_all(_make_mock_stdscr())

        assert not [c for c in captured if c and c[0] == "send-keys"], (
            f"no send-keys expected without a claude pane: {captured}"
        )

    def test_ignored_claude_pane_is_never_targeted(self, monkeypatch):
        # The only claude pane is CCM_IGNORE'd — ccm keeps its hands
        # off ignored panes, so the exit is skipped rather than sent
        # to the window's active shell.
        panes = [
            self._pane("%0", "100", active=True),
            self._pane("%1", "200", ignored=True, claude=True),
        ]
        d, captured = self._dash(monkeypatch, panes)
        d._do_exit_all(_make_mock_stdscr())

        assert not [c for c in captured if c and c[0] == "send-keys"], (
            f"ignored claude pane must not receive /exit: {captured}"
        )


# ─── _do_attach auto-start routing ───

class TestDoAttachAutoStart:
    """Attaching to a SHELL project must launch Claude via
    `ccm_window.auto_start_claude` (which resolves a shell-foreground
    pane), not via a raw `send-keys -t <window>` that would type the
    command into whatever pane is active."""

    def test_shell_project_uses_safe_auto_start(self, monkeypatch):
        _stub_dashboard_environment(monkeypatch)
        import ccm_core
        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "SHELL"),
        ]
        d.selected = 0
        started = []
        monkeypatch.setattr("dashboard.auto_start_claude",
                            lambda wt: started.append(wt))
        monkeypatch.setattr("dashboard.reset_window_after_attach",
                            lambda wt: None)
        monkeypatch.setattr("os.path.isdir", lambda p: True)
        captured = []
        monkeypatch.setattr("dashboard.tmux_cmd",
                            lambda *a, **k: captured.append(a) or "")

        action = d._do_attach(_make_mock_stdscr())

        assert action == "attached"
        assert started == ["0:1"]
        # The launch command itself must not be sent straight to the
        # window target from the dashboard.
        assert not any(
            c and c[0] == "send-keys" and any("claude" in str(a) for a in c)
            for c in captured
        ), f"launch command bypassed auto_start_claude: {captured}"


# ─── Interactive handlers: rename / remove / ignore / add / register / search ───
#
# These exercise the representative dialog flows behind the
# dashboard's action keys (n/r/i/a/g//). Every prompt is stubbed via
# Dashboard._prompt, every external effect via dashboard.tmux_cmd or
# the imported cmd_* functions — the assertions pin WHICH command ran
# with WHICH arguments and whether a rebuild was triggered, so a
# regression in the dispatch logic (wrong branch, missing confirm
# gate, lost rebuild) fails loudly.


def _interaction_dash(monkeypatch, projects, prompt_answers=()):
    """Dashboard wired for interaction tests.

    Returns (dashboard, messages, rebuilds, tmux_calls). `_prompt`
    yields `prompt_answers` in order (then None, which every handler
    treats as cancel); `_show_message` and `_trigger_rebuild` record;
    `tmux_cmd` records and returns ""."""
    _stub_dashboard_environment(monkeypatch)
    d = Dashboard(initial_mode="dashboard")
    d.projects = projects
    d.selected = 0
    answers = iter(prompt_answers)
    monkeypatch.setattr(
        Dashboard, "_prompt",
        lambda self, s, text, path_completion=False: next(answers, None))
    messages = []
    monkeypatch.setattr(
        Dashboard, "_show_message",
        lambda self, s, msg, duration=1: messages.append(msg))
    rebuilds = []
    monkeypatch.setattr(
        Dashboard, "_trigger_rebuild",
        lambda self: rebuilds.append("rebuild"))
    tmux_calls = []
    monkeypatch.setattr("dashboard.tmux_cmd",
                        lambda *a, **k: tmux_calls.append(a) or "")
    return d, messages, rebuilds, tmux_calls


def _one_project(name="alpha", target="0:1", state="IDLE", **kw):
    import ccm_core
    return ccm_core.Project(target, target.split(":")[1], name,
                            f"/tmp/{name}", state, **kw)


class TestDoRename:
    def test_rename_happy_path(self, monkeypatch):
        d, messages, rebuilds, tmux_calls = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["beta-new"])
        d._do_rename(_make_mock_stdscr())

        assert ("set-option", "-wt", "0:1", "@ccm_project", "beta-new") in tmux_calls
        assert ("rename-window", "-t", "0:1", "beta-new") in tmux_calls
        assert rebuilds == ["rebuild"]
        assert any("beta-new" in m for m in messages)

    @pytest.mark.parametrize("answer", ["", None])
    def test_rename_cancelled_touches_nothing(self, monkeypatch, answer):
        d, messages, rebuilds, tmux_calls = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=[answer])
        d._do_rename(_make_mock_stdscr())

        assert tmux_calls == []
        assert rebuilds == []


class TestDoRemove:
    def _cmds(self, monkeypatch):
        calls = {"unregister": [], "remove": []}
        monkeypatch.setattr(
            "dashboard.cmd_unregister",
            lambda name: calls["unregister"].append(name))
        monkeypatch.setattr(
            "dashboard.cmd_remove",
            lambda name: calls["remove"].append(name))
        return calls

    def test_unregister_choice(self, monkeypatch):
        calls = self._cmds(monkeypatch)
        d, messages, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["u"])
        d._do_remove(_make_mock_stdscr())

        assert calls == {"unregister": ["alpha"], "remove": []}
        assert rebuilds == ["rebuild"]

    def test_delete_choice_confirms_then_removes(self, monkeypatch):
        """Delete closes a window and ends its session, so it costs
        one more keypress than unregister — the menu route already
        confirmed, and the single-key route being the harsher of the
        two was the wrong way around."""
        calls = self._cmds(monkeypatch)
        d, messages, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["d", "y"])
        d._do_remove(_make_mock_stdscr())

        assert calls == {"unregister": [], "remove": ["alpha"]}
        assert rebuilds == ["rebuild"]

    @pytest.mark.parametrize("second", ["", "n", "x", None])
    def test_delete_declined_at_confirmation_is_noop(
            self, monkeypatch, second):
        calls = self._cmds(monkeypatch)
        d, messages, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["d", second])
        d._do_remove(_make_mock_stdscr())

        assert calls == {"unregister": [], "remove": []}
        assert rebuilds == []

    def test_unregister_needs_no_second_confirmation(self, monkeypatch):
        """Unregister keeps the window and the session; symmetric
        friction with delete would teach users to confirm blindly."""
        calls = self._cmds(monkeypatch)
        d, messages, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["u", None])
        d._do_remove(_make_mock_stdscr())

        assert calls == {"unregister": ["alpha"], "remove": []}

    @pytest.mark.parametrize("answer", ["", "x", None])
    def test_unconfirmed_choice_is_noop(self, monkeypatch, answer):
        """Anything but u/d — including Esc (None) — must not touch
        the project: remove is destructive and gated on an explicit
        confirmation letter."""
        calls = self._cmds(monkeypatch)
        d, messages, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=[answer])
        d._do_remove(_make_mock_stdscr())

        assert calls == {"unregister": [], "remove": []}
        assert rebuilds == []

    def test_command_failure_shows_error_and_skips_rebuild(self, monkeypatch):
        import ccm_core

        def _boom(name):
            raise ccm_core.CCMError("no such project")
        monkeypatch.setattr("dashboard.cmd_unregister", _boom)
        monkeypatch.setattr("dashboard.cmd_remove", lambda name: None)

        d, messages, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["u"])
        d._do_remove(_make_mock_stdscr())

        assert any("no such project" in m for m in messages)
        assert rebuilds == []


class TestDoIgnoreToggle:
    def _cmds(self, monkeypatch):
        calls = {"ignore": [], "unignore": []}
        monkeypatch.setattr("dashboard.cmd_ignore",
                            lambda name: calls["ignore"].append(name))
        monkeypatch.setattr("dashboard.cmd_unignore",
                            lambda name: calls["unignore"].append(name))
        return calls

    def test_plain_project_gets_ignored(self, monkeypatch):
        calls = self._cmds(monkeypatch)
        d, _, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project(ignored_panes=0)])
        d._do_ignore_toggle(_make_mock_stdscr())

        assert calls == {"ignore": ["alpha"], "unignore": []}
        assert rebuilds == ["rebuild"]

    def test_ignored_project_gets_unignored(self, monkeypatch):
        calls = self._cmds(monkeypatch)
        d, _, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project(ignored_panes=1)])
        d._do_ignore_toggle(_make_mock_stdscr())

        assert calls == {"ignore": [], "unignore": ["alpha"]}
        assert rebuilds == ["rebuild"]


class TestDoAdd:
    def _cmd_add(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "dashboard.cmd_add",
            lambda directory, name, create_dir=False:
                calls.append((directory, name, create_dir)))
        return calls

    def test_add_existing_dir_default_name(self, monkeypatch):
        calls = self._cmd_add(monkeypatch)
        monkeypatch.setattr("os.path.isdir", lambda p: True)
        d, _, rebuilds, _ = _interaction_dash(
            monkeypatch, [],
            prompt_answers=["/tmp/existing", ""])  # empty name → basename
        d._do_add(_make_mock_stdscr())

        assert calls == [("/tmp/existing", "existing", False)]
        assert rebuilds == ["rebuild"]

    def test_add_cancelled_at_directory_prompt(self, monkeypatch):
        calls = self._cmd_add(monkeypatch)
        d, _, rebuilds, _ = _interaction_dash(
            monkeypatch, [], prompt_answers=[""])
        d._do_add(_make_mock_stdscr())

        assert calls == []
        assert rebuilds == []

    def test_add_missing_parent_shows_error(self, monkeypatch):
        calls = self._cmd_add(monkeypatch)
        monkeypatch.setattr("os.path.isdir", lambda p: False)
        d, messages, rebuilds, _ = _interaction_dash(
            monkeypatch, [], prompt_answers=["/no/such/parent/child"])
        d._do_add(_make_mock_stdscr())

        assert calls == []
        assert any("Parent does not exist" in m for m in messages)


class TestDoRegister:
    def test_register_untagged_window(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "dashboard.cmd_register",
            lambda win, name: calls.append((win, name)))
        d, _, rebuilds, _ = _interaction_dash(
            monkeypatch, [], prompt_answers=["free", ""])  # empty name → win
        monkeypatch.setattr(
            "dashboard.tmux_cmd",
            lambda *a, **k: ("0:1\talpha\tproj\n0:2\tfree\t"
                             if a[0] == "list-windows" else ""))
        d._do_register(_make_mock_stdscr())

        assert calls == [("free", "free")]
        assert rebuilds == ["rebuild"]

    def test_no_untagged_windows(self, monkeypatch):
        calls = []
        monkeypatch.setattr("dashboard.cmd_register",
                            lambda win, name: calls.append((win, name)))
        d, messages, rebuilds, _ = _interaction_dash(monkeypatch, [])
        monkeypatch.setattr(
            "dashboard.tmux_cmd",
            lambda *a, **k: ("0:1\talpha\tproj"
                             if a[0] == "list-windows" else ""))
        d._do_register(_make_mock_stdscr())

        assert calls == []
        assert any("No untagged windows" in m for m in messages)


class TestDoSearch:
    def _dash(self, monkeypatch):
        _stub_dashboard_environment(monkeypatch)
        import ccm_core
        d = Dashboard(initial_mode="dashboard")
        d.projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
            ccm_core.Project("0:2", "2", "beta", "/tmp/b", "IDLE"),
            ccm_core.Project("0:3", "3", "gamma", "/tmp/c", "IDLE"),
        ]
        d.selected = 0
        attached = []
        monkeypatch.setattr(
            Dashboard, "_do_attach",
            lambda self, s: attached.append(self.selected) or "attached")
        return d, attached

    def _stdscr(self, keys):
        stdscr = _make_mock_stdscr()
        stdscr.get_wch.side_effect = list(keys)
        return stdscr

    def test_filter_and_enter_attaches_match(self, monkeypatch):
        """Typing 'ga' narrows to gamma; Enter attaches the original
        project index (2), not the position in the filtered list."""
        d, attached = self._dash(monkeypatch)
        action = d._do_search(self._stdscr(["g", "a", "\n"]))

        assert action == "attached"
        assert attached == [2]
        assert d.selected == 2

    def test_enter_with_empty_query_attaches_first(self, monkeypatch):
        d, attached = self._dash(monkeypatch)
        action = d._do_search(self._stdscr(["\n"]))

        assert action == "attached"
        assert attached == [0]

    def test_esc_cancels_without_attaching(self, monkeypatch):
        d, attached = self._dash(monkeypatch)
        action = d._do_search(self._stdscr(["b", "\x1b"]))

        assert action == ""
        assert attached == []
        assert d.selected == 0

    def test_backspace_narrows_then_widens_filter(self, monkeypatch):
        """'be' would match beta only; deleting back to 'b' keeps beta
        but Enter must still land on the beta row (index 1)."""
        d, attached = self._dash(monkeypatch)
        action = d._do_search(self._stdscr(["b", "e", "\x7f", "\n"]))

        assert action == "attached"
        assert attached == [1]


def _dashboard_source():
    import inspect
    return inspect.getsource(dashboard)


def _method_source(name):
    import ast, inspect
    src = inspect.getsource(dashboard)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found")


def _list_literal(name, closing_indent):
    """Source text of a list literal, to its closing bracket.

    Matched to the bracket at a known indent rather than the first
    `]` — the first one belongs to `"[↑↓/jk] select"`, and stopping
    there made this check report every key as missing.
    """
    import re
    m = re.search(rf"{name} = \[(.*?)\n{closing_indent}\]",
                  _dashboard_source(), re.S)
    assert m, f"{name} literal not found"
    return m.group(1)


class TestDoMenuRemoval:
    """The menu route asks for a name because the menu has no
    selection of its own — but the list's selection survives into
    menu mode, so a plain Enter targets it rather than making the
    user retype what the dashboard is already pointing at."""

    def _cmds(self, monkeypatch):
        calls = {"unregister": [], "remove": [], "ignore": []}
        monkeypatch.setattr(
            "dashboard.cmd_unregister",
            lambda name: calls["unregister"].append(name))
        monkeypatch.setattr(
            "dashboard.cmd_remove",
            lambda name: calls["remove"].append(name))
        monkeypatch.setattr(
            "dashboard.cmd_ignore",
            lambda name: calls["ignore"].append(name))
        return calls

    def test_plain_enter_targets_the_selected_project(self, monkeypatch):
        calls = self._cmds(monkeypatch)
        d, _, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=[""])
        d._do_menu_removal(_make_mock_stdscr(), "unregister")
        assert calls["unregister"] == ["alpha"]
        assert rebuilds == ["rebuild"]

    def test_escape_cancels_despite_the_default(self, monkeypatch):
        calls = self._cmds(monkeypatch)
        d, _, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=[None])
        d._do_menu_removal(_make_mock_stdscr(), "unregister")
        assert calls == {"unregister": [], "remove": [], "ignore": []}
        assert rebuilds == []

    def test_a_typed_name_overrides_the_selection(self, monkeypatch):
        calls = self._cmds(monkeypatch)
        projects = [_one_project(), _one_project("beta", "0:2")]
        d, _, _, _ = _interaction_dash(
            monkeypatch, projects, prompt_answers=["beta"])
        d._do_menu_removal(_make_mock_stdscr(), "unregister")
        assert calls["unregister"] == ["beta"]

    def test_menu_delete_still_confirms(self, monkeypatch):
        """The default fills in the name, not the consent."""
        calls = self._cmds(monkeypatch)
        d, _, _, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["", "y"])
        d._do_menu_removal(_make_mock_stdscr(), "delete")
        assert calls["remove"] == ["alpha"]

    def test_menu_delete_declined_is_noop(self, monkeypatch):
        calls = self._cmds(monkeypatch)
        d, _, rebuilds, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["", "n"])
        d._do_menu_removal(_make_mock_stdscr(), "delete")
        assert calls["remove"] == []
        assert rebuilds == []

    def test_unknown_name_points_at_the_faster_route(self, monkeypatch):
        self._cmds(monkeypatch)
        d, messages, _, _ = _interaction_dash(
            monkeypatch, [_one_project()], prompt_answers=["nosuch"])
        d._do_menu_removal(_make_mock_stdscr(), "unregister")
        assert messages and "press r" in messages[0]


class TestEveryKeyAndOperationIsFindable:
    """What the dashboard can do has to be visible before you press
    anything.

    Unregistering a project was reachable only behind `r`, which the
    footer rendered as "remove" and the menu did not mention at all,
    so the operation read as unsupported. The instance is easy to fix;
    these tests are here so the class of oversight cannot come back.
    """

    def test_every_handled_letter_key_is_offered_in_the_help_line(self):
        """Scope: lowercase letter keys, which is what `ord("x")`
        extraction can see. `/`, `?`, arrows and Enter are outside it
        and are asserted onto the help line individually below.
        """
        import re
        handled = set(re.findall(r'ord\("([a-z])"\)',
                                 _method_source("_handle_key")))
        # The extraction itself has a failure mode: refactor the key
        # handling into a dispatch table and findall returns nothing,
        # leaving an empty `missing` and a test that checks nothing
        # while staying green. The floor turns that into a failure.
        assert len(handled) >= 10, (
            f"only {len(handled)} letter keys extracted from "
            f"_handle_key — the ord(...) pattern no longer matches how "
            f"keys are handled, so this test has stopped seeing them; "
            f"update the extraction to the new dispatch shape")
        block = _list_literal("help_items", " " * 12)
        # A key counts as offered only when a bracket names it: `[r]`
        # alone, a `/`-separated alternative (`[m/?]`), or a two-letter
        # vi pair (`jk` in `[↑↓/jk]`). Substring matching would let
        # `[Enter]` vouch for e, n, t and r; key names longer than a
        # pair stay excluded for the same reason.
        offered = set()
        for inner in re.findall(r"\[([^\]]+)\]", block):
            for alt in inner.split("/"):
                if len(alt) == 1:
                    offered.add(alt)
                elif len(alt) == 2 and alt.isalpha() and alt.islower():
                    offered.update(alt)
        missing = handled - offered
        assert not missing, (
            f"keys handled by the dashboard but absent from the help "
            f"line: {sorted(missing)} — a key nobody is told about is "
            f"a feature nobody finds")

    def test_the_non_letter_keys_are_offered_too(self):
        """The letter scan above cannot see these; name them one by
        one so the help line cannot quietly drop them."""
        block = _list_literal("help_items", " " * 12)
        for token in ("[↑↓/jk]", "[Enter]", "[/]", "[m/?]"):
            assert token in block, (
                f"help line no longer offers {token}")

    def test_the_menu_offers_the_known_ways_to_stop_managing_a_project(
            self):
        """Adding a project is in the menu; the ways back out have to
        be as well, or removal reads as unsupported.

        An enumeration, not a discovery: if a new way to stop managing
        a project is added, add it here — the test cannot find it on
        its own.
        """
        import re
        block = _list_literal(r"self\.menu_items", " " * 8)
        actions = set(re.findall(r'"([a-z_]+)"\s*\)', block))
        for action in ("add", "unregister", "delete", "ignore"):
            assert action in actions, (
                f"the menu does not offer '{action}' — the menu is where "
                f"a reader learns what ccm can do")
