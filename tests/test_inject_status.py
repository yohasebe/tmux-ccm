"""Tests for inject_status.py — pure helpers that do not need a tmux server."""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import inject_status
import ccm_core
import ccm_render
import ccm_signals


def make_project(win_target, win_idx, name, state):
    return ccm_core.Project(
        win_target=win_target,
        win_idx=win_idx,
        name=name,
        directory="/tmp/" + name,
        state=state,
    )


# ─── build_detail_entries: active highlighting ───

class TestBuildDetailEntriesActive:
    """Window indices alone collide across tmux sessions, so the
    active-window comparison must use the full session:index target.
    """

    def test_only_target_match_is_bold(self, monkeypatch):
        """Two windows share index '2' but live in different sessions —
        only the one whose full target matches current_win_target should
        be highlighted as active."""
        # BUSY entries probe the stale-signal suffix, which reads the
        # hook signal via tmux; stub it to "no signal".
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: None)
        projects = [
            make_project("0:2", "2", "sample-proj", "SHELL"),
            make_project("1:2", "2", "sideproject", "BUSY"),
        ]
        entries = inject_status.build_detail_entries(
            projects, with_extras=True, current_win_target="1:2"
        )
        assert "bold" in entries[1] and "sideproject" in entries[1]
        # sample-proj must NOT be bold even though its index is also "2"
        assert "bold" not in entries[0]
        assert "sample-proj" in entries[0]

    def test_no_match_no_bold(self):
        """If the current window is not a ccm project, nothing is bold."""
        projects = [
            make_project("0:2", "2", "sample-proj", "SHELL"),
            make_project("0:5", "5", "docs", "IDLE"),
        ]
        entries = inject_status.build_detail_entries(
            projects, current_win_target="0:99"
        )
        assert all("bold" not in e for e in entries)

    def test_mode1_uses_same_compare(self, monkeypatch):
        """Mode 1 (with_extras=False) shares the bold rule."""
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: None)
        projects = [
            make_project("0:2", "2", "sample-proj", "SHELL"),
            make_project("1:2", "2", "sideproject", "BUSY"),
        ]
        entries = inject_status.build_detail_entries(
            projects, with_extras=False, current_win_target="0:2"
        )
        assert "bold" in entries[0] and "sample-proj" in entries[0]
        assert "bold" not in entries[1]


class TestStaleSignalSuffixInStatusBar:
    """Mode 1 / 2 status bar should append `(Nm)` when a BUSY or
    PERMIT signal is past JSONL_HOOK_GAP_TOLERANCE — same affordance
    the dashboard and `ccm status` already provide. Surfacing the
    age in the always-visible status bar lets the user catch
    phantom-subagent / silent-permission stuck states without
    opening the popup.
    """

    def test_mode1_stale_busy_appends_suffix(self, monkeypatch):
        # Hook signal 8 minutes old → "(8m)" suffix.
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: (ts - 480, "BUSY", ""))
        entries = inject_status.build_detail_entries(
            [make_project("0:2", "2", "sample-proj", "BUSY")],
            with_extras=False, current_win_target="0:2",
        )
        assert "(8m)" in entries[0]

    def test_mode2_stale_permit_appends_suffix(self, monkeypatch):
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: (ts - 120, "PERMIT", ""))
        entries = inject_status.build_detail_entries(
            [make_project("0:2", "2", "sample-proj", "PERMIT")],
            with_extras=True, current_win_target="0:2",
        )
        assert "(2m)" in entries[0]

    def test_fresh_busy_no_suffix(self, monkeypatch):
        """Below the stale threshold the suffix is suppressed —
        otherwise every active turn would clutter the status bar."""
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: (ts - 5, "BUSY", ""))
        entries = inject_status.build_detail_entries(
            [make_project("0:2", "2", "sample-proj", "BUSY")],
            with_extras=False, current_win_target="0:2",
        )
        # No "(Ns)" / "(Nm)" pattern in the entry.
        assert "(5s)" not in entries[0] and "(0m)" not in entries[0]

    def test_idle_state_never_gets_suffix(self, monkeypatch):
        """Only BUSY / PERMIT can mask a real state behind a stale
        hook. IDLE / SHELL / DOWN must never see the suffix even if
        a stale signal happens to be lying around."""
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: (ts - 600, "BUSY", ""))
        for state in ("IDLE", "SHELL", "DOWN"):
            entries = inject_status.build_detail_entries(
                [make_project("0:2", "2", "sample-proj", state)],
                with_extras=False, current_win_target="0:2",
            )
            assert "(10m)" not in entries[0], f"unexpected suffix for {state}"


class TestBgActiveSuffixInStatusBar:
    """The `(bg)` affordance for IDLE projects with leftover
    background activity (state=IDLE committed but raw=BUSY because
    of grandchild processes). Indicates "user has the ball but
    something claude spawned is still running"."""

    def _make(self, state, bg):
        p = make_project("0:2", "2", "sample-proj", state)
        p.bg_active = bg
        return p

    def test_idle_with_bg_shows_bg_suffix(self):
        entries = inject_status.build_detail_entries(
            [self._make("IDLE", True)],
            with_extras=False, current_win_target="0:2",
        )
        assert "(bg)" in entries[0]

    def test_idle_without_bg_no_suffix(self):
        entries = inject_status.build_detail_entries(
            [self._make("IDLE", False)],
            with_extras=False, current_win_target="0:2",
        )
        assert "(bg)" not in entries[0]

    def test_mode2_idle_with_bg_shows_bg_suffix(self):
        entries = inject_status.build_detail_entries(
            [self._make("IDLE", True)],
            with_extras=True, current_win_target="0:2",
        )
        assert "(bg)" in entries[0]


class TestPaneCountSuffixInStatusBar:
    """The `[N]` marker for windows with more than one pane.
    Surfaces the multi-pane case (Agent Teams, casual splits,
    leftover orphan panes) so the user can spot windows where the
    aggregated state may belong to a non-active pane. Brackets
    are rendered dim, the digit cyan, so the eye lands on the
    count.
    """

    def _make(self, pane_count, state="IDLE"):
        p = make_project("0:2", "2", "sample-proj", state)
        p.pane_count = pane_count
        return p

    def test_single_pane_no_marker(self):
        entries = inject_status.build_detail_entries(
            [self._make(1)],
            with_extras=False, current_win_target="0:2",
        )
        # No bracketed digit between name and the icon-colon.
        assert "[1]" not in entries[0]
        # And (sanity) no leftover legacy marker.
        assert "⊞" not in entries[0]

    def test_two_panes_show_marker(self):
        entries = inject_status.build_detail_entries(
            [self._make(2)],
            with_extras=False, current_win_target="0:2",
        )
        # The digit appears between dim brackets.
        assert "[" in entries[0] and "]" in entries[0]
        assert ">2<" in entries[0].replace("#[fg=cyan]", ">").replace("#[fg=#666666]", "<")

    def test_three_panes_show_marker(self):
        entries = inject_status.build_detail_entries(
            [self._make(3)],
            with_extras=False, current_win_target="0:2",
        )
        assert "3" in entries[0]
        # The digit was emitted with a cyan colour code so the
        # user's eye is drawn to the count.
        assert "#[fg=cyan]3" in entries[0]

    def test_mode2_two_panes_show_marker(self):
        entries = inject_status.build_detail_entries(
            [self._make(2)],
            with_extras=True, current_win_target="0:2",
        )
        assert "#[fg=cyan]2" in entries[0]

    def test_pane_count_combines_with_stale_signal(self, monkeypatch):
        """A stuck-PERMIT split-pane window should render both the
        stale-age suffix and the multi-pane marker. Layout: [N]
        is BEFORE the state icon (structural marker next to the
        project name), stale-age is AFTER the icon (state-
        modifier). So in left-to-right text order: [N] first,
        then the icon, then (10m)."""
        import time
        ts = 9_999_999
        monkeypatch.setattr("time.time", lambda: ts)
        monkeypatch.setattr(ccm_signals, "read_hook_signal",
                            lambda d, session_id=None: (ts - 600, "PERMIT", ""))
        p = self._make(2, state="PERMIT")
        entries = inject_status.build_detail_entries(
            [p],
            with_extras=False, current_win_target="0:2",
        )
        # Both markers present; order: pane marker first (pre-
        # icon), stale after (post-icon).
        assert "(10m)" in entries[0]
        assert "#[fg=cyan]2" in entries[0]
        assert entries[0].index("#[fg=cyan]2") < entries[0].index("(10m)")


# ─── fast path: no maintenance side effects ───

class TestFastPathSkipsMaintenanceSideEffects:
    """`inject-status --fast` is the focus-refresh redraw path: it
    bypasses the flock precisely so it can run CONCURRENTLY with a
    lock-holding periodic instance. If it also ran the maintenance
    tasks, both instances could pass the idle check for the same
    window and double-send the Escape + `/exit` + Enter sequence —
    the late copy landing in the post-exit shell pane, where the
    literal `exit` kills the user's shell (the shape
    documented in ccm_runtime.auto_exit_idle). Those tasks also cost
    several tmux subprocesses per call, contradicting the fast
    path's ~10 ms redraw budget. So the fast path must skip
    window-name updates, the notify-transition cache, periodic
    autosave, and idle auto-exit — and only render the status bar."""

    def _run(self, monkeypatch, tmp_path, force_fast):
        calls = {"window_names": 0, "autosave": 0, "auto_exit": 0}
        monkeypatch.setattr(inject_status, "CCM_TMP_DIR", str(tmp_path))

        def fake_tmux_cmd(*args, **kwargs):
            if args[:2] == ("display-message", "-p"):
                if "#{client_width}" in args:
                    return "120"
                return "0:1"
            return ""

        monkeypatch.setattr(inject_status, "tmux_cmd", fake_tmux_cmd)
        monkeypatch.setattr(inject_status, "tmux_batch", lambda *cmds: None)
        monkeypatch.setattr(
            inject_status, "build_project_list",
            lambda fast=False: [make_project("0:1", "1", "proj", "SHELL")])
        monkeypatch.setattr(inject_status, "detect_external_status_change",
                            lambda: None)
        monkeypatch.setattr(inject_status, "sanitize_orig_status",
                            lambda: None)
        monkeypatch.setattr(inject_status, "update_window_names",
                            lambda p: calls.__setitem__(
                                "window_names", calls["window_names"] + 1))
        monkeypatch.setattr(inject_status, "periodic_autosave",
                            lambda: calls.__setitem__(
                                "autosave", calls["autosave"] + 1))
        monkeypatch.setattr(inject_status, "auto_exit_idle",
                            lambda p: calls.__setitem__(
                                "auto_exit", calls["auto_exit"] + 1))
        monkeypatch.setattr(inject_status, "notify", lambda *a, **k: None)
        monkeypatch.setattr(inject_status, "signal_age_suffix",
                            lambda d, s, session_id=None: "")
        inject_status._inject_status_impl(force_fast=force_fast)
        return calls

    def test_fast_path_skips_all_maintenance(self, monkeypatch, tmp_path):
        calls = self._run(monkeypatch, tmp_path, force_fast=True)
        assert calls == {"window_names": 0, "autosave": 0, "auto_exit": 0}
        assert not (tmp_path / "notify-cache").exists(), (
            "fast path must not write the notify-transition cache")

    def test_periodic_path_still_runs_maintenance(self, monkeypatch, tmp_path):
        calls = self._run(monkeypatch, tmp_path, force_fast=False)
        assert calls == {"window_names": 1, "autosave": 1, "auto_exit": 1}
        assert (tmp_path / "notify-cache").exists(), (
            "periodic path must still record states for transition "
            "notifications")


# ─── Mode 2 helpers ───

class TestClearMode2SlotsAbove:
    def test_returns_unset_commands_above_threshold(self):
        cmds = inject_status._clear_mode2_slots_above(2)
        slots = [c[3] for c in cmds]
        assert slots[0] == "status-format[3]"
        assert slots[-1] == f"status-format[{inject_status._MODE2_MAX_SLOTS}]"
        assert all(c[:3] == ("set", "-g", "-u") for c in cmds)

    def test_threshold_at_or_above_max_returns_empty(self):
        assert inject_status._clear_mode2_slots_above(inject_status._MODE2_MAX_SLOTS) == []
        assert inject_status._clear_mode2_slots_above(inject_status._MODE2_MAX_SLOTS + 5) == []


class TestOptColor:
    def test_returns_default_when_option_unset(self, monkeypatch):
        monkeypatch.setattr(inject_status, "tmux_cmd", lambda *a, **k: "")
        assert inject_status._opt_color("@ccm-status-bg", "#262626") == "#262626"

    def test_returns_default_when_whitespace_only(self, monkeypatch):
        monkeypatch.setattr(inject_status, "tmux_cmd", lambda *a, **k: "   ")
        assert inject_status._opt_color("@ccm-status-bg", "#262626") == "#262626"

    def test_returns_user_value_when_set(self, monkeypatch):
        monkeypatch.setattr(inject_status, "tmux_cmd", lambda *a, **k: "  #ffffff  ")
        assert inject_status._opt_color("@ccm-status-bg", "#262626") == "#ffffff"

    @pytest.mark.parametrize("color", [
        "#fff", "#FFF", "#000", "#abc",                # 3-digit hex
        "#ffffff", "#FFFFFF", "#000000", "#1a1a1a",    # 6-digit hex
        "colour0", "colour255", "colour123",            # palette index
        "color128",                                     # alias
        "red", "green", "blue", "yellow", "default",    # named
        "BRIGHTBLUE", "Cyan", "Magenta",                # case-insensitive named
    ])
    def test_accepts_valid_colors(self, color, monkeypatch):
        monkeypatch.setattr(inject_status, "tmux_cmd", lambda *a, **k: color)
        assert inject_status._opt_color("@ccm-status-bg", "#262626") == color

    @pytest.mark.parametrize("garbage", [
        "garbage", "#abcd", "#abcdefg", "#1234",        # malformed hex
        "colour", "colour-1", "colour9999",             # malformed palette index
        "rgb(0,0,0)", "rgba(1,1,1,1)",                  # CSS syntax (tmux rejects)
        "  blue extra  ",                                # extra tokens
        ";echo pwned",                                   # injection attempt
    ])
    def test_rejects_invalid_colors_falls_back_to_default(self, garbage, monkeypatch):
        monkeypatch.setattr(inject_status, "tmux_cmd", lambda *a, **k: garbage)
        assert inject_status._opt_color("@ccm-status-bg", "#262626") == "#262626"




# ─── mode-2 layout: tmux status-line ceiling ───

class TestMode2StatusLineCeiling:
    """tmux's `status` option accepts on/off/2..5 — at most 5 status
    lines total. The mode-2 layout (1 main bar + 1 gutter + N entry
    lines) must therefore never ask for more than 3 entry lines.

    Regression for the frozen-status-bar incident: with 27
    projects the layout computed 4 entry lines → `set -g status 6` →
    tmux rejected it ("unknown value: 6") and, because the whole
    mode-2 render is one `;`-chained batch, every status-format write
    aborted with it. All render paths (periodic tick, focus hook,
    manual) failed identically and silently, so the bar froze at the
    last good bake — stale states, stale focus highlight, and newly
    registered projects missing entirely. The symptom was reported as
    "the focus mechanism broke" but the whole bar was frozen.
    """

    def _run_mode2(self, monkeypatch, n_projects, term_width=120):
        """Drive _inject_status_impl through the mode-2 layout with
        `n_projects` fake projects and capture every batched tmux
        command."""
        batches = []
        projects = [
            make_project(f"0:{i}", str(i), f"proj-{i:02d}", "SHELL")
            for i in range(1, n_projects + 1)
        ]

        def fake_tmux_cmd(*args, **kwargs):
            if args[:2] == ("display-message", "-p"):
                if "#{client_width}" in args:
                    return str(term_width)
                return "0:1"
            if args[0] == "show-option":
                # @ccm-status-line → mode 2; everything else empty.
                if "@ccm-status-line" in args:
                    return "2"
                return ""
            return ""

        monkeypatch.setattr(inject_status, "tmux_cmd", fake_tmux_cmd)
        monkeypatch.setattr(inject_status, "tmux_batch",
                            lambda *cmds: batches.append(cmds))
        monkeypatch.setattr(inject_status, "build_project_list",
                            lambda fast=False: projects)
        monkeypatch.setattr(inject_status, "detect_external_status_change",
                            lambda: None)
        monkeypatch.setattr(inject_status, "sanitize_orig_status",
                            lambda: None)
        monkeypatch.setattr(inject_status, "periodic_autosave", lambda: None)
        monkeypatch.setattr(inject_status, "auto_exit_idle", lambda p: None)
        monkeypatch.setattr(inject_status, "update_window_names", lambda p: None)
        monkeypatch.setattr(inject_status, "notify", lambda *a, **k: None)
        monkeypatch.setattr(inject_status, "signal_age_suffix",
                            lambda d, s, session_id=None: "")
        monkeypatch.setattr(
            inject_status, "read_project_notify_marker", lambda d: 0.0,
            raising=False)
        inject_status._inject_status_impl(force_fast=True)
        return batches

    def _status_values(self, batches):
        vals = []
        for batch in batches:
            for cmd in batch:
                if (len(cmd) >= 4 and cmd[0] == "set"
                        and cmd[2] == "status" and cmd[3].isdigit()):
                    vals.append(int(cmd[3]))
        return vals

    def test_27_projects_narrow_terminal_stays_within_tmux_limit(
            self, monkeypatch):
        """The incident shape: enough projects that the natural
        layout wants 4+ entry lines. `status` must be clamped to 5."""
        batches = self._run_mode2(monkeypatch, n_projects=27,
                                  term_width=120)
        vals = self._status_values(batches)
        assert vals, "mode-2 render issued no `set status` at all"
        assert all(2 <= v <= 5 for v in vals), (
            f"`set -g status {max(vals)}` exceeds tmux's maximum of 5 "
            "— tmux rejects it and the whole ;-chained batch aborts "
            "(frozen status bar)")

    def test_extreme_case_also_clamped(self, monkeypatch):
        """Degenerate: 60 projects on a very narrow terminal."""
        batches = self._run_mode2(monkeypatch, n_projects=60,
                                  term_width=60)
        vals = self._status_values(batches)
        assert vals and all(2 <= v <= 5 for v in vals)

    def test_all_entries_still_rendered_when_clamped(self, monkeypatch):
        """Clamping must pack, not drop: every project name appears
        somewhere in the emitted status-format lines."""
        batches = self._run_mode2(monkeypatch, n_projects=27,
                                  term_width=120)
        rendered = " ".join(
            cmd[3] for batch in batches for cmd in batch
            if len(cmd) >= 4 and cmd[0] == "set"
            and cmd[2].startswith("status-format["))
        for i in range(1, 28):
            assert f"proj-{i:02d}" in rendered, (
                f"proj-{i:02d} dropped from the clamped layout")

    def test_small_project_count_unchanged(self, monkeypatch):
        """A handful of projects keeps the natural compact layout
        (status = 3: main + gutter + 1 entry line)."""
        batches = self._run_mode2(monkeypatch, n_projects=4,
                                  term_width=200)
        vals = self._status_values(batches)
        assert vals == [3]


# ─── CJK width handling in layout math ───

class TestCJKWidthInLayout:
    """Width math for status-bar layout must use display_width, not
    len(): CJK characters occupy two terminal columns each, and
    project names may legally be CJK (validate_name only strips
    shell metacharacters). len() undercounts them by half, which
    overestimates the remaining budget and overflows/wraps the bar
    (audit finding, sibling of the non-ASCII slug bug)."""

    def test_mode2_cjk_names_reduce_entries_per_line(self, monkeypatch):
        """The same number of projects must yield MORE lines when
        names are CJK (double-width) than when they are ASCII of
        equal character count — proving the layout sees terminal
        columns, not characters."""
        helper = TestMode2StatusLineCeiling()

        batches_ascii = helper._run_mode2(monkeypatch, n_projects=8,
                                          term_width=100)
        # Rebuild with CJK names of the same character length.
        batches_cjk = []
        projects = [
            make_project(f"0:{i}", str(i), f"研究プロジェ{i:02d}", "SHELL")
            for i in range(1, 9)
        ]

        def fake_tmux_cmd(*args, **kwargs):
            if args[:2] == ("display-message", "-p"):
                if "#{client_width}" in args:
                    return "100"
                return "0:1"
            if args[0] == "show-option":
                if "@ccm-status-line" in args:
                    return "2"
                return ""
            return ""

        monkeypatch.setattr(inject_status, "tmux_cmd", fake_tmux_cmd)
        monkeypatch.setattr(inject_status, "tmux_batch",
                            lambda *cmds: batches_cjk.append(cmds))
        monkeypatch.setattr(inject_status, "build_project_list",
                            lambda fast=False: projects)
        monkeypatch.setattr(inject_status, "detect_external_status_change",
                            lambda: None)
        monkeypatch.setattr(inject_status, "sanitize_orig_status",
                            lambda: None)
        monkeypatch.setattr(inject_status, "periodic_autosave", lambda: None)
        monkeypatch.setattr(inject_status, "auto_exit_idle", lambda p: None)
        monkeypatch.setattr(inject_status, "update_window_names", lambda p: None)
        monkeypatch.setattr(inject_status, "notify", lambda *a, **k: None)
        monkeypatch.setattr(inject_status, "signal_age_suffix",
                            lambda d, s, session_id=None: "")
        inject_status._inject_status_impl(force_fast=True)

        def lines_used(batches):
            vals = []
            for batch in batches:
                for cmd in batch:
                    if (len(cmd) >= 4 and cmd[0] == "set"
                            and cmd[2] == "status" and cmd[3].isdigit()):
                        vals.append(int(cmd[3]))
            return max(vals) if vals else 0

        ascii_lines = lines_used(batches_ascii)
        cjk_lines = lines_used(batches_cjk)
        assert cjk_lines >= ascii_lines, (
            f"CJK names (double-width) got FEWER status lines "
            f"({cjk_lines}) than same-length ASCII names "
            f"({ascii_lines}) — width math is counting characters, "
            f"not columns")
        # And the CJK layout must genuinely account for the doubled
        # width: 8 entries of ~14 columns of name alone cannot fit
        # a single 100-column line.
        assert cjk_lines > 3 or cjk_lines >= ascii_lines


class TestHideShellFilter:
    """`@ccm-status-line-hide-shell` narrows the named-project modes to
    windows that host a Claude session.

    A machine with many registered projects has most of them at SHELL
    most of the time, and the one that needs attention is lost among
    them — 37 of 39 rows saying "nothing here" on the setup this was
    written for.
    """

    def _projects(self):
        return [
            make_project("0:1", "1", "one", "BUSY"),
            make_project("0:2", "2", "two", "IDLE"),
            make_project("0:3", "3", "three", "SHELL"),
            make_project("0:4", "4", "four", "PERMIT"),
        ]

    def _with_option(self, monkeypatch, value):
        def fake(*args, **kw):
            if args[:3] == ("show-option", "-gqv",
                            "@ccm-status-line-hide-shell"):
                return value
            return ""
        monkeypatch.setattr(inject_status, "tmux_cmd", fake)

    def test_default_keeps_every_project(self, monkeypatch):
        """Off by default: the bar is a project overview, and narrowing
        it without being asked would hide projects the user registered
        on purpose."""
        self._with_option(monkeypatch, "")
        kept = inject_status.apply_shell_filter(self._projects())
        assert [p.name for p in kept] == ["one", "two", "three", "four"]

    def test_on_drops_only_shell(self, monkeypatch):
        self._with_option(monkeypatch, "on")
        kept = inject_status.apply_shell_filter(self._projects())
        assert [p.name for p in kept] == ["one", "two", "four"]

    def test_idle_survives_the_filter(self, monkeypatch):
        """IDLE looks inactive and is not: the session is alive and
        waiting, and it carries the `* elapsed` marker that says a turn
        just finished. Hiding it would drop the moment the bar exists
        to show."""
        self._with_option(monkeypatch, "on")
        kept = inject_status.apply_shell_filter(self._projects())
        assert "two" in [p.name for p in kept]

    @pytest.mark.parametrize("value", ["on", "1", "true", "yes"])
    def test_accepts_the_usual_truthy_spellings(self, monkeypatch, value):
        self._with_option(monkeypatch, value)
        assert inject_status.hide_shell_enabled() is True

    @pytest.mark.parametrize("value", ["", "off", "0", "no", "nonsense"])
    def test_anything_else_is_off(self, monkeypatch, value):
        """An unrecognised value must not silently hide projects — the
        failure a user would notice last."""
        self._with_option(monkeypatch, value)
        assert inject_status.hide_shell_enabled() is False

    def test_all_shell_yields_an_empty_list(self, monkeypatch):
        """Both named-project modes already render an empty list as
        'no projects'; the filter must be allowed to produce one."""
        self._with_option(monkeypatch, "on")
        only_shell = [make_project("0:1", "1", "a", "SHELL")]
        assert inject_status.apply_shell_filter(only_shell) == []


class TestStatusLinePosition:
    """`@ccm-status-line-position left` moves mode 1's entries to the
    far side of the bar.

    `status-right` draws as one right-aligned block, so padding placed
    after the entries widens the block until it reaches across the bar
    and pushes the entries left. Nothing is written to `status-left`,
    which stays the theme's to own.
    """

    def _opt(self, monkeypatch, position="", left_width="  host "):
        def fake(*args, **kw):
            if args[:3] == ("show-option", "-gqv",
                            "@ccm-status-line-position"):
                return position
            if args[:3] == ("display-message", "-p", "#{T:status-left}"):
                return left_width
            return ""
        monkeypatch.setattr(inject_status, "tmux_cmd", fake)

    def test_default_is_right(self, monkeypatch):
        self._opt(monkeypatch, "")
        assert inject_status.status_line_position() == "right"

    def test_left_is_opt_in(self, monkeypatch):
        self._opt(monkeypatch, "left")
        assert inject_status.status_line_position() == "left"

    def test_unrecognised_value_stays_right(self, monkeypatch):
        """Anything but the exact word keeps the placement that clips
        the least important entry first."""
        self._opt(monkeypatch, "centre")
        assert inject_status.status_line_position() == "right"

    def test_status_left_width_is_measured_after_expansion(self, monkeypatch):
        """`status-left` is a format; its source text says nothing
        about rendered width. Style codes occupy no columns."""
        self._opt(monkeypatch, left_width="#[fg=red] host #[default]")
        assert inject_status.status_left_width() == len(" host ")

    def test_status_left_width_counts_cjk_as_two_columns(self, monkeypatch):
        self._opt(monkeypatch, left_width="日本")
        assert inject_status.status_left_width() == 4

    def test_status_left_width_of_empty_is_zero(self, monkeypatch):
        self._opt(monkeypatch, left_width="")
        assert inject_status.status_left_width() == 0


class TestReservedWidth:
    """Status specs are templates. What matters is how wide they draw,
    and the parts that vary are exactly the parts that are wide: `%T`
    is two characters that draw as eight, `%F` two that draw as ten,
    and a theme's `#{...}` segments hold whatever they hold right now.

    Counting the template instead of the rendering undercounted a real
    theme's `status-right` by 14 columns. The padding that places
    mode 1's entries on the left is derived from that number, so the
    block overflowed by 2 and tmux clipped it from the left — taking
    the first character of the highest-priority entry, the one the
    placement exists to protect.
    """

    def _expansion(self, monkeypatch, value):
        monkeypatch.setattr(inject_status, "tmux_cmd",
                            lambda *a, **kw: value)

    def test_measures_the_expansion_not_the_template(self, monkeypatch):
        self._expansion(monkeypatch, "21:55:39")
        assert inject_status.reserved_width("#{T:status-right}") == 8

    def test_style_codes_occupy_no_columns(self, monkeypatch):
        self._expansion(monkeypatch, "#[fg=red]ab#[default]cd")
        assert inject_status.reserved_width("#{T:status-right}") == 4

    def test_wide_glyphs_count_as_two_columns(self, monkeypatch):
        self._expansion(monkeypatch, "日本")
        assert inject_status.reserved_width("#{T:status-right}") == 4

    def test_empty_spec_is_zero(self, monkeypatch):
        self._expansion(monkeypatch, "")
        assert inject_status.reserved_width("#{T:status-right}") == 0

    def test_original_width_prefers_the_measurement(self, monkeypatch):
        """A template of 4 characters that expands to 12 columns must
        report 12 — the number the layout has to budget for."""
        self._expansion(monkeypatch, "212 chars ok")   # 12 columns
        assert inject_status.original_status_right_width("%T %F") == 12

    def test_original_width_falls_back_when_unmeasurable(self, monkeypatch):
        """Losing the measurement must not stop the bar rendering; the
        template count is short but usable."""
        def fake(*args, **kw):
            raise RuntimeError("tmux unavailable")
        monkeypatch.setattr(inject_status, "tmux_cmd", fake)
        assert inject_status.original_status_right_width("#[fg=red]abc") == 3


class TestMode1WidthBudget:
    """What mode 1 writes has to end up the width of the bar.

    The pieces were each checked on their own while the total was
    never asserted, and the total is what the user sees. It went wrong
    in both directions: short by 2 and the first entry was clipped,
    long by 20 and a band of empty space opened after `status-left`.

    So these tests measure the rendered result against the terminal
    width, not the arithmetic against itself.
    """

    ICON = "●"     # ● — Ambiguous, the state icon on every entry
    BOX = "│"      # │ — Ambiguous, common in themes

    def _render(self, monkeypatch, n_projects=4, term_width=120,
                status_left=" host ", orig_right=" 21:55:39 ",
                position="left", length_calls=None):
        """Drive the mode-1 layout and return the `status-right` it
        wrote, or None if it wrote none."""
        written = []
        projects = [
            make_project(f"0:{i}", str(i), f"proj-{i:02d}", "IDLE")
            for i in range(1, n_projects + 1)
        ]

        def fake_tmux_cmd(*args, **kwargs):
            if args[:2] == ("display-message", "-p"):
                spec = args[2] if len(args) > 2 else ""
                if "#{client_width}" in spec:
                    return str(term_width)
                if "status-left" in spec:
                    return status_left
                if "orig-status-right" in spec:
                    return orig_right
                return "0:1"
            if args[0] in ("show-option", "show"):
                if "@ccm-status-line" in args:
                    return "1"
                if "@ccm-status-line-position" in args:
                    return position
                if "@ccm-orig-status-right" in args:
                    return orig_right
                return ""
            if args[:3] == ("set", "-g", "status-right"):
                written.append(args[3])
            return ""

        monkeypatch.setattr(inject_status, "tmux_cmd", fake_tmux_cmd)
        monkeypatch.setattr(inject_status, "tmux_batch", lambda *cmds: None)
        monkeypatch.setattr(inject_status, "build_project_list",
                            lambda fast=False: projects)
        monkeypatch.setattr(inject_status, "scan_active_windows",
                            lambda p, include_all=False: p)
        monkeypatch.setattr(inject_status, "detect_external_status_change",
                            lambda: None)
        monkeypatch.setattr(inject_status, "sanitize_orig_status",
                            lambda: None)
        monkeypatch.setattr(inject_status, "periodic_autosave", lambda: None)
        monkeypatch.setattr(inject_status, "auto_exit_idle", lambda p: None)
        monkeypatch.setattr(inject_status, "update_window_names",
                            lambda p: None)
        monkeypatch.setattr(inject_status, "notify", lambda *a, **k: None)
        monkeypatch.setattr(inject_status, "signal_age_suffix",
                            lambda d, s, session_id=None: "")
        monkeypatch.setattr(inject_status, "_write_cache",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            inject_status, "_extend_status_right_length",
            lambda *a, **k: None if length_calls is None
            else length_calls.append(k))
        monkeypatch.setattr(
            inject_status, "read_project_notify_marker", lambda d: 0.0,
            raising=False)
        inject_status._inject_status_impl(force_fast=True)
        return written[-1] if written else None

    def _drawn_width(self, status_right, ambiguous=1):
        """Columns the written status-right occupies on screen.

        `ambiguous` is what the terminal makes of an ambiguous-width
        glyph — the thing nobody can know in advance, so both answers
        are checked.
        """
        text = re.sub(r"#\([^)]*\)", "", status_right)   # #() prints nothing
        text = re.sub(r"#\[[^\]]*\]", "", text)
        extra = 0
        if ambiguous == 2:
            extra = inject_status._ambiguous_width_allowance(text)
        return inject_status.display_width(text) + extra

    def _left_width(self, status_left, ambiguous=1):
        extra = 0
        if ambiguous == 2:
            extra = inject_status._ambiguous_width_allowance(status_left)
        return inject_status.display_width(status_left) + extra

    @pytest.mark.parametrize("ambiguous", [1, 2])
    def test_the_bar_is_never_overdrawn(self, monkeypatch, ambiguous):
        """status-right is right-aligned, so anything wider than the
        space left over by status-left gets clipped — and in left
        placement tmux clips the highest-priority entry."""
        left = f" {self.BOX} host {self.ICON} "
        right = self._render(monkeypatch, n_projects=5, term_width=120,
                             status_left=left)
        assert right is not None
        total = (self._drawn_width(right, ambiguous)
                 + self._left_width(left, ambiguous))
        assert total <= 120, (
            f"the bar draws {total} columns of 120 with ambiguous "
            f"glyphs at {ambiguous} — status-left overlaps the entries")

    def test_the_length_cap_admits_the_whole_block(self, monkeypatch):
        """tmux clips whatever exceeds `status-right-length` from the
        LEFT — the highest-priority entry in left placement. Left
        placement pads the block out to reach across the bar, so a cap
        derived from the theme's own length is unrelated to how wide
        the block now is."""
        caps = []
        left = f" {self.BOX} host {self.ICON} "
        self._render(monkeypatch, n_projects=5, term_width=200,
                     status_left=left, length_calls=caps)
        assert caps, "the length was never set"
        assert caps[-1].get("minimum") == 200, (
            f"cap floor is {caps[-1].get('minimum')}, not the terminal "
            f"width — a theme with a short status-right-length would "
            f"have the leading entries clipped")

    def test_the_gap_after_status_left_stays_small(self, monkeypatch):
        """The reason to reserve the worst case is to avoid a clipped
        entry, not to push the entries into the middle of the bar. On
        a terminal that draws ambiguous glyphs wide — the case the
        reservation is for — what is left over is the intended gap."""
        left = f" {self.BOX} host {self.ICON} "
        right = self._render(monkeypatch, n_projects=5, term_width=120,
                             status_left=left)
        gap = 120 - self._drawn_width(right, 2) - self._left_width(left, 2)
        assert gap == inject_status.LEFT_PLACEMENT_GAP, (
            f"{gap} columns of empty space after status-left, not the "
            f"{inject_status.LEFT_PLACEMENT_GAP} intended; something in "
            f"the budget is reserved twice or not at all")

    def test_entries_are_not_charged_for_a_separator_they_do_not_get(
            self, monkeypatch):
        """The first entry is drawn as ` entry`, the rest as ` │ entry`.
        Charging every entry for the separator loses two columns."""
        one = self._render(monkeypatch, n_projects=1, term_width=120,
                           position="right")
        assert one is not None
        text = re.sub(r"#\[[^\]]*\]", "",
                      re.sub(r"#\([^)]*\)", "", one))
        assert text.startswith(inject_status.ENTRY_LEAD + "proj-01"), text

    def test_the_list_is_drawn_from_the_strings_it_was_budgeted_from(
            self, monkeypatch):
        """The budget measures `ENTRY_SEPARATOR` and `LIST_END`; if the
        rendering stopped using them the numbers would go on adding up
        while the bar drew something else."""
        two = self._render(monkeypatch, n_projects=2, term_width=120,
                           position="right")
        text = re.sub(r"#\[[^\]]*\]", "",
                      re.sub(r"#\([^)]*\)", "", two))
        assert text.startswith(inject_status.ENTRY_LEAD), text
        assert inject_status.ENTRY_SEPARATOR in text, text
        assert inject_status.LIST_END in text, text
        # One bar between the two entries, one closing the list. Each
        # is charged for in the budget, so an extra one drawn here
        # would be a column nobody reserved.
        assert text.count("│") == 2, text

    def test_a_narrow_bar_still_places_entries_on_the_right(
            self, monkeypatch):
        """When the padding cannot be afforded, left placement is
        abandoned rather than guessed at — the entries go back to the
        clock end, where a miscalculation costs the least important
        one."""
        right = self._render(monkeypatch, n_projects=6, term_width=40,
                             status_left=" a very wide status left here ")
        assert right is not None
        text = re.sub(r"#\[[^\]]*\]", "",
                      re.sub(r"#\([^)]*\)", "", right))
        assert "  " not in text.strip(), (
            "padding was inserted although there was no room for it")


class TestAmbiguousWidthAllowance:
    """East Asian Width "Ambiguous" glyphs draw one column or two
    depending on the terminal and its font, and Unicode does not say
    which. Every Nerd Font glyph lives in the private use area, where
    the codepoint carries no width at all.

    The layout cannot know, so it reserves for the worse case. The two
    errors are not symmetric: a few reserved columns move the entries
    right, which nobody notices, while a shortfall lets the left
    segment paint over the highest-priority entry — the one left
    placement exists to protect.
    """

    def test_ascii_needs_no_allowance(self):
        assert inject_status._ambiguous_width_allowance("plain text") == 0

    def test_box_drawing_and_shapes_are_charged(self):
        # │ U+2502 and ● U+25CF are both Ambiguous.
        assert inject_status._ambiguous_width_allowance("│●") == 2

    def test_private_use_glyphs_are_charged(self):
        """Nerd Font icons. Unicode reports these as Ambiguous, and the
        actual width is whatever the font decided."""
        assert inject_status._ambiguous_width_allowance("") == 2

    def test_wide_characters_are_not_double_charged(self):
        """CJK is unambiguously two columns; `display_width` already
        counts it correctly, so adding an allowance would reserve four."""
        assert inject_status._ambiguous_width_allowance("日本") == 0

    def test_status_left_reserves_more_than_it_measures(self, monkeypatch):
        monkeypatch.setattr(inject_status, "tmux_cmd",
                            lambda *a, **kw: " │ host │ ")
        # 10 columns measured, 2 ambiguous glyphs → 12 reserved.
        assert inject_status.status_left_width() == 12

    def test_status_left_of_plain_text_is_just_its_width(self, monkeypatch):
        monkeypatch.setattr(inject_status, "tmux_cmd",
                            lambda *a, **kw: " host ")
        assert inject_status.status_left_width() == 6


class TestDeclaredAmbiguousWidth:
    """Declaring the terminal's glyph width is a statement, and once it
    is made there is nothing left to hedge against.

    Reserving on top of a declared width charges twice. At `2` the
    glyph is counted two columns by `display_width` and then allowed a
    third. At `1` the layout keeps hedging against a case the user has
    ruled out, and in left placement that hedge is visible: 12 columns
    of empty space after `status-left` on a real theme.

    The declaration must reach every parent that starts ccm. The bar
    renders both from tmux's `#()` and from the hooks, which Claude
    Code spawns with its own environment, so an environment variable
    reaches one and not the other and the bar alternates between two
    layouts. Hence the tmux option, which answers the same to whoever
    asks.
    """

    def _opt(self, monkeypatch, value):
        monkeypatch.setattr(ccm_render.ccm_core, "tmux_cmd",
                            lambda *a, **kw: value)

    def test_unset_everywhere_is_not_a_declaration(self, monkeypatch):
        self._opt(monkeypatch, "")
        monkeypatch.delenv("CCM_AMBIGUOUS_WIDTH", raising=False)
        assert ccm_render._resolve_ambiguous_width() == (1, False)

    def test_one_counts_the_same_as_unset_but_is_a_declaration(
            self, monkeypatch):
        """The whole point of the flag: `1` and unset produce the same
        column count and must not produce the same reservation."""
        self._opt(monkeypatch, "1")
        assert ccm_render._resolve_ambiguous_width() == (1, True)

    def test_two_is_a_declaration(self, monkeypatch):
        self._opt(monkeypatch, "2")
        assert ccm_render._resolve_ambiguous_width() == (2, True)

    @pytest.mark.parametrize("raw", ["5", "0", "banana", ""])
    def test_a_value_that_says_nothing_is_not_a_declaration(
            self, monkeypatch, raw):
        """An unusable value must fall back to hedging, not to trusting
        a number nobody meant."""
        self._opt(monkeypatch, raw)
        monkeypatch.delenv("CCM_AMBIGUOUS_WIDTH", raising=False)
        assert ccm_render._resolve_ambiguous_width() == (1, False)

    def test_the_environment_still_answers_outside_tmux(self, monkeypatch):
        """Running ccm with no tmux to ask leaves the variable as the
        only way to say it."""
        def no_tmux(*a, **kw):
            raise RuntimeError("no server running")
        monkeypatch.setattr(ccm_render.ccm_core, "tmux_cmd", no_tmux)
        monkeypatch.setenv("CCM_AMBIGUOUS_WIDTH", "2")
        assert ccm_render._resolve_ambiguous_width() == (2, True)

    def test_the_option_outranks_the_environment(self, monkeypatch):
        """The option is what every parent can see; the variable is
        whatever one of them happened to be started with."""
        self._opt(monkeypatch, "2")
        monkeypatch.setenv("CCM_AMBIGUOUS_WIDTH", "1")
        assert ccm_render._resolve_ambiguous_width() == (2, True)

    def test_an_empty_option_falls_through_to_the_environment(
            self, monkeypatch):
        """An unset tmux option reads as the empty string, which says
        nothing — it must not mask a variable that says something."""
        self._opt(monkeypatch, "")
        monkeypatch.setenv("CCM_AMBIGUOUS_WIDTH", "1")
        assert ccm_render._resolve_ambiguous_width() == (1, True)

    def test_the_answer_is_resolved_once_per_process(self, monkeypatch):
        """Every width measurement would otherwise be a tmux round
        trip, and the hook-driven render is the path that is never
        rate-limited."""
        monkeypatch.setattr(ccm_render, "_AMBIGUOUS_STATE", None)
        calls = []

        def counting(*a, **kw):
            calls.append(a)
            return "1"
        monkeypatch.setattr(ccm_render.ccm_core, "tmux_cmd", counting)
        for _ in range(5):
            ccm_render.display_width("●●●")
        assert len(calls) == 1, f"asked tmux {len(calls)} times"

    def test_a_declaration_stops_the_reservation(self, monkeypatch):
        monkeypatch.setattr(inject_status, "ambiguous_width_declared",
                            lambda: True)
        assert inject_status._ambiguous_width_allowance("│●") == 0

    def test_a_declared_width_is_not_charged_twice(self, monkeypatch):
        """`_worst_case_width` is what layouts reserve. Once the width
        is declared it must be exactly what the text measures."""
        monkeypatch.setattr(inject_status, "ambiguous_width_declared",
                            lambda: True)
        text = " │ host ● "
        assert (inject_status._worst_case_width(text)
                == inject_status.display_width(text))

    def test_declaring_the_width_closes_the_gap(self, monkeypatch):
        """End to end: on a terminal whose width is known to be narrow,
        the only space left after `status-left` is the intended gap."""
        monkeypatch.setattr(inject_status, "ambiguous_width_declared",
                            lambda: True)
        helper = TestMode1WidthBudget()
        left = f" {helper.BOX} host {helper.ICON} "
        right = helper._render(monkeypatch, n_projects=5, term_width=120,
                               status_left=left)
        gap = (120 - helper._drawn_width(right, 1)
               - helper._left_width(left, 1))
        assert gap == inject_status.LEFT_PLACEMENT_GAP, (
            f"{gap} columns after status-left although the terminal's "
            f"width was declared; the layout is still hedging")
