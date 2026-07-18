"""Tests for ccm_render.

Auto-split from test_ccm_core.py. Shared fixtures + helpers
(write_jsonl, make_ps_lines, real_activity_record, system_record,
iso_ts) live in conftest.py; import them here when used.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

import pytest

import ccm_core
import ccm_activity
import ccm_canaries
import ccm_commands
import ccm_detection
import ccm_jsonl
import ccm_notify
import ccm_pane_state
import ccm_render
import ccm_rules
import ccm_runtime
import ccm_signals

from conftest import (
    iso_ts,
    make_ctx,
    make_ps_lines,
    real_activity_record,
    system_record,
    write_jsonl,
)

# Backward-compat alias used by some tests.
_iso_ts = iso_ts

class TestFormatElapsed:
    # Fixed-width (3 visible cols, right-aligned digits) is a hard
    # contract — the dashboard's right-anchored elapsed slot relies on
    # this so the marker's right edge doesn't wobble at 1↔2 digit
    # boundaries as the counter ticks. Don't relax these without also
    # widening `ELAPSED_RIGHT_SLOT` in `lib/dashboard.py`.

    def test_seconds_two_digit(self):
        ts = int(time.time()) - 30
        assert ccm_render.format_elapsed(ts) == "30s"

    def test_seconds_single_digit_padded(self):
        """Single-digit seconds get a leading space so the field
        width stays at 3 visible cols."""
        ts = int(time.time()) - 5
        assert ccm_render.format_elapsed(ts) == " 5s"

    def test_minutes_single_digit_padded(self):
        ts = int(time.time()) - 180
        assert ccm_render.format_elapsed(ts) == " 3m"

    def test_minutes_two_digit(self):
        ts = int(time.time()) - 30 * 60
        assert ccm_render.format_elapsed(ts) == "30m"

    def test_hours_single_digit_padded(self):
        ts = int(time.time()) - 2 * 3600
        assert ccm_render.format_elapsed(ts) == " 2h"

    def test_days_single_digit_padded(self):
        ts = int(time.time()) - 2 * 86400
        assert ccm_render.format_elapsed(ts) == " 2d"

    def test_zero_returns_empty(self):
        assert ccm_render.format_elapsed(0) == ""

    def test_none_returns_empty(self):
        assert ccm_render.format_elapsed(None) == ""

    def test_below_min_display_returns_empty(self):
        """The `* elapsed` marker is suppressed for the first few
        seconds after a BUSY→IDLE transition, to avoid flicker
        during `/goal`-style auto-loops (BUSY → ~2 s IDLE → BUSY).
        See the comment on `MIN_ELAPSED_DISPLAY_SEC` in
        `ccm_render.py` for the why/when-to-remove rationale —
        this test exists to make sure that intentional suppression
        survives any incidental refactors of `format_elapsed`."""
        for age in range(0, ccm_render.MIN_ELAPSED_DISPLAY_SEC):
            ts = int(time.time()) - age
            assert ccm_render.format_elapsed(ts) == "", (
                f"elapsed={age}s should be hidden by the auto-loop "
                f"flicker guard (threshold = "
                f"{ccm_render.MIN_ELAPSED_DISPLAY_SEC}s)"
            )

    def test_at_min_display_threshold_renders(self):
        ts = int(time.time()) - ccm_render.MIN_ELAPSED_DISPLAY_SEC
        rendered = ccm_render.format_elapsed(ts)
        # MIN_ELAPSED_DISPLAY_SEC is currently 3, so " 3s" (padded).
        # Use the constant in the expected so the test self-updates
        # if the threshold ever changes.
        assert rendered == f"{ccm_render.MIN_ELAPSED_DISPLAY_SEC:>2d}s"

    def test_width_is_constant_three_cols(self):
        """All non-empty returns from format_elapsed must be 3 visible
        cols. The right-anchored dashboard slot relies on this — a
        4-char return (e.g. from a future "999s" overflow) would push
        the elapsed marker past the row's right edge."""
        now = int(time.time())
        # Sample a few values per magnitude band
        for age in (3, 5, 10, 30, 59, 60, 120, 1799, 3600, 7200, 86399, 86400, 172800):
            ts = now - age
            s = ccm_render.format_elapsed(ts)
            assert s == "" or len(s) == 3, (
                f"elapsed={age}s produced {s!r} (len={len(s)}); "
                f"required width is 3"
            )


class TestSignalAgeSuffix:
    """The stale-signal display affordance for the dashboard /
    `ccm status` UI. Surfaces a hook signal age (e.g. " (8m)")
    when state is BUSY or PERMIT and the signal is older than
    SIGNAL_STALE_DISPLAY_THRESHOLD."""

    def _patch_signal(self, monkeypatch, ts_or_none):
        if ts_or_none is None:
            monkeypatch.setattr(ccm_signals, "read_hook_signal",
                                lambda d: None)
        else:
            monkeypatch.setattr(ccm_signals, "read_hook_signal",
                                lambda d: (ts_or_none, "BUSY", ""))

    @pytest.mark.parametrize("state", ["IDLE", "SHELL", "DOWN"])
    def test_non_busy_permit_states_return_empty(self, state, monkeypatch):
        """Only BUSY / PERMIT can mask a real state behind a stale
        hook. Other states either have no hook signal or the
        signal is freshness-irrelevant."""
        self._patch_signal(monkeypatch, int(time.time()) - 600)
        assert ccm_render.signal_age_suffix("/p", state) == ""

    def test_no_signal_returns_empty(self, monkeypatch):
        self._patch_signal(monkeypatch, None)
        assert ccm_render.signal_age_suffix("/p", "BUSY") == ""

    def test_empty_dir_returns_empty(self, monkeypatch):
        # Should not even attempt to read the signal.
        self._patch_signal(monkeypatch, int(time.time()) - 600)
        assert ccm_render.signal_age_suffix("", "BUSY") == ""
        assert ccm_render.signal_age_suffix(None, "PERMIT") == ""

    def test_fresh_signal_returns_empty(self, monkeypatch):
        """Below the display threshold the suffix is suppressed —
        otherwise every active turn would clutter the dashboard."""
        self._patch_signal(monkeypatch,
                           int(time.time()) - (ccm_render.SIGNAL_STALE_DISPLAY_THRESHOLD - 1))
        assert ccm_render.signal_age_suffix("/p", "BUSY") == ""

    def test_stale_permit_minutes_format(self, monkeypatch):
        self._patch_signal(monkeypatch, int(time.time()) - 480)  # 8 min
        assert ccm_render.signal_age_suffix("/p", "PERMIT") == " (8m)"

    def test_stale_busy_hours_format(self, monkeypatch):
        self._patch_signal(monkeypatch, int(time.time()) - 7200)  # 2 h
        assert ccm_render.signal_age_suffix("/p", "BUSY") == " (2h)"

    def test_read_signal_exception_returns_empty(self, monkeypatch):
        """Best-effort: never propagate I/O errors to the UI."""
        def raiser(d):
            raise OSError("boom")
        monkeypatch.setattr(ccm_signals, "read_hook_signal", raiser)
        assert ccm_render.signal_age_suffix("/p", "BUSY") == ""


class TestFormatDir:
    def test_fits_full(self):
        assert ccm_render.format_dir("/short", 10, 80) == "/short"

    def test_truncates_to_parent_base(self):
        long_dir = "/very/long/path/to/project"
        result = ccm_render.format_dir(long_dir, 60, 80)
        assert "…/" in result or result == "project"

    def test_returns_empty_when_too_narrow(self):
        assert ccm_render.format_dir("/some/path", 75, 80) == ""

    def test_japanese_dir_uses_display_width(self):
        # "日本" + "/" + "プロジェクト" = (4 + 1 + 12) = 17 cols.
        # If `format_dir` mistakenly used `len()` (= 9 codepoints) it
        # would think the path fit at avail=10 and render it
        # mid-overflow. Display-width-based check rejects it.
        d = "/日本/プロジェクト"  # 18 cols
        # avail = cols(20) - prefix_len(0) - 4 = 16 cols
        result = ccm_render.format_dir(d, 0, 20)
        # Either a truncated form or empty; never the full string,
        # which would not fit.
        assert result != d
        assert ccm_render.display_width(result) <= 16


class TestDisplayWidth:
    """`display_width` is the single source of truth for terminal
    column counts of strings containing CJK / emoji / combining
    marks. Used by every column-alignment site in dashboard, status
    bar, and `format_dir`."""

    def test_ascii(self):
        assert ccm_render.display_width("hello") == 5

    def test_empty(self):
        assert ccm_render.display_width("") == 0

    def test_cjk(self):
        assert ccm_render.display_width("日本語") == 6  # 3 wide chars

    def test_korean(self):
        assert ccm_render.display_width("한글") == 4

    def test_chinese(self):
        assert ccm_render.display_width("中文") == 4

    def test_emoji_wide(self):
        assert ccm_render.display_width("🚀") == 2

    def test_combining_mark_zero_width(self):
        # "é" as a + COMBINING ACUTE: width 1, not 2
        assert ccm_render.display_width("é") == 1

    def test_zwj_zero_width(self):
        # ZWJ between two ASCII chars adds nothing
        assert ccm_render.display_width("a‍b") == 2

    def test_mixed(self):
        assert ccm_render.display_width("ab日c") == 5  # 1+1+2+1

    def test_ambiguous_default_is_one(self):
        # `●` (U+25CF, BLACK CIRCLE) is East Asian Ambiguous.
        # Default behavior: 1 column (non-CJK terminal convention).
        import ccm_render
        with patch.object(ccm_render, "_AMBIGUOUS_WIDTH", 1):
            assert ccm_render.display_width("●") == 1

    def test_ambiguous_two_columns_when_opted_in(self):
        # CJK locale users set `CCM_AMBIGUOUS_WIDTH=2` to get the
        # 2-column treatment that matches their terminal's rendering.
        import ccm_render
        with patch.object(ccm_render, "_AMBIGUOUS_WIDTH", 2):
            assert ccm_render.display_width("●") == 2


class TestTruncateToWidth:
    def test_no_truncation_needed(self):
        assert ccm_render.truncate_to_width("abc", 10) == "abc"

    def test_ascii_truncation(self):
        assert ccm_render.truncate_to_width("abcdef", 3) == "abc"

    def test_zero_width_returns_empty(self):
        assert ccm_render.truncate_to_width("anything", 0) == ""

    def test_negative_width_returns_empty(self):
        assert ccm_render.truncate_to_width("anything", -1) == ""

    def test_cjk_boundary_keeps_whole_chars(self):
        # "日本語" = 6 cols. With max=5, "日本" (4) fits, "語" (2)
        # would push to 6, so we stop. Never returns half a glyph.
        result = ccm_render.truncate_to_width("日本語", 5)
        assert result == "日本"
        assert ccm_render.display_width(result) <= 5

    def test_cjk_exact(self):
        assert ccm_render.truncate_to_width("日本語", 6) == "日本語"

    def test_combining_mark_kept_with_base(self):
        # Truncating "éf" to width 1 should keep "é"
        # (the combining mark attaches to the preceding base char).
        result = ccm_render.truncate_to_width("éf", 1)
        assert result == "é"


class TestPadToWidth:
    """`pad_to_width` is the CJK-safe replacement for the f-string
    `<N` spec. CLI tables (`ccm status`, `ccm list`, `ccm ports`,
    `ccm snapshot list`) use it so column alignment survives
    Japanese / emoji project names."""

    def test_ascii_pads_to_width(self):
        assert ccm_render.pad_to_width("abc", 5) == "abc  "

    def test_already_at_width(self):
        assert ccm_render.pad_to_width("hello", 5) == "hello"

    def test_overlong_returned_unchanged(self):
        # Same as f-string `<` spec: longer-than-target strings pass
        # through unchanged. Callers that need truncation use
        # `truncate_to_width` first.
        assert ccm_render.pad_to_width("toolong", 3) == "toolong"

    def test_cjk_pads_by_columns_not_codepoints(self):
        # `len("日本") == 2` but visible width is 4 cols. f-string
        # `<10` would pad with 8 spaces (total visible width 12);
        # `pad_to_width` pads with 6 spaces (total visible width 10).
        result = ccm_render.pad_to_width("日本", 10)
        assert ccm_render.display_width(result) == 10

    def test_emoji_pads_by_columns(self):
        result = ccm_render.pad_to_width("🚀", 5)
        assert ccm_render.display_width(result) == 5


class TestPrintStatus:
    """Capture-stdout tests for the `ccm status` CLI rendering.
    Locks in the cross-renderer convention that the `[N]`
    pane-count marker belongs to the PROJECT column (not the
    STATUS column) and gets the dim-bracket / cyan-digit
    treatment, matching the dashboard and status bar."""

    def _run_print_status(self, projects, monkeypatch, capsys):
        # build_project_list does file I/O; bypass with a stub.
        monkeypatch.setattr(ccm_core, "build_project_list",
                            lambda fast=False: projects)
        # Other side effects in print_status: hooks_log_warning,
        # disable_all_hooks_warning, managed_hooks_only_warning,
        # shell_cluster_warnings — stub all to empty so the test
        # focuses on per-project rendering.
        monkeypatch.setattr(ccm_canaries, "hooks_log_warning", lambda: "")
        monkeypatch.setattr(ccm_canaries, "disable_all_hooks_warning",
                            lambda: "")
        monkeypatch.setattr(ccm_canaries, "managed_hooks_only_warning",
                            lambda: "")
        monkeypatch.setattr(ccm_canaries, "shell_cluster_warnings",
                            lambda projects_arg: [])
        monkeypatch.setattr(ccm_core, "hooks_configured",
                            lambda: True)
        ccm_render.print_status()
        return capsys.readouterr().out

    def test_no_pane_marker_for_single_pane_window(
        self, monkeypatch, capsys
    ):
        projects = [
            ccm_core.Project(
                win_target="0:1", win_idx="1", name="solo",
                directory="/tmp/solo", state="IDLE",
                pane_count=1,
            ),
        ]
        out = self._run_print_status(projects, monkeypatch, capsys)
        # Find the project's row.
        row = next(line for line in out.splitlines()
                   if "solo" in line)
        # No bracketed digit anywhere in the row.
        assert "[1]" not in row
        assert "[2]" not in row

    def test_pane_marker_after_project_name_with_cyan(
        self, monkeypatch, capsys
    ):
        """`[N]` marker belongs to the PROJECT column, not the
        STATUS column. The convention is to render it after the
        project name so the user's eye lands on the count next
        to the identity it belongs to."""
        projects = [
            ccm_core.Project(
                win_target="0:1", win_idx="1", name="myproject",
                directory="/tmp/myproject", state="SHELL",
                pane_count=2,
            ),
        ]
        out = self._run_print_status(projects, monkeypatch, capsys)
        row = next(line for line in out.splitlines()
                   if "myproject" in line)
        # The bracketed digit appears in the project column
        # (i.e. after the name) — assert by character order.
        assert "myproject" in row
        # The digit "2" must appear AFTER "myproject" (project
        # column), and the cyan ANSI code (\033[36m) must wrap
        # the digit so the user's eye lands on the count.
        name_pos = row.index("myproject")
        # Find the cyan-coded digit "2" after the project name.
        cyan_seg = "\033[36m2"
        assert cyan_seg in row, (
            f"expected cyan-coded '2' in row, got: {row!r}"
        )
        assert row.index(cyan_seg) > name_pos, (
            f"[N] marker must follow the project name, got: {row!r}"
        )
        # And the marker must NOT appear inside the STATUS
        # column (before the project name).
        assert row.index(cyan_seg) > name_pos

    def test_three_pane_marker_renders_digit_3(
        self, monkeypatch, capsys
    ):
        projects = [
            ccm_core.Project(
                win_target="0:1", win_idx="1", name="trio",
                directory="/tmp/trio", state="BUSY",
                pane_count=3,
            ),
        ]
        out = self._run_print_status(projects, monkeypatch, capsys)
        row = next(line for line in out.splitlines()
                   if "trio" in line)
        assert "\033[36m3" in row

    def test_mode_column_renders_cli_vocabulary(self, monkeypatch, capsys):
        """Payload "default" renders as `manual` — the CLI label users
        actually type (`--permission-mode manual`)."""
        projects = [
            ccm_core.Project(
                win_target="0:1", win_idx="1", name="modeproj",
                directory="/tmp/modeproj", state="IDLE",
                permission_mode="default",
            ),
        ]
        out = self._run_print_status(projects, monkeypatch, capsys)
        assert "MODE" in out  # header column
        row = next(line for line in out.splitlines() if "modeproj" in line)
        assert "manual" in row
        assert "default" not in row

    def test_bypass_mode_gets_warning_color(self, monkeypatch, capsys):
        projects = [
            ccm_core.Project(
                win_target="0:1", win_idx="1", name="yolo",
                directory="/tmp/yolo", state="BUSY",
                permission_mode="bypassPermissions",
            ),
        ]
        out = self._run_print_status(projects, monkeypatch, capsys)
        row = next(line for line in out.splitlines() if "yolo" in line)
        # bold yellow (same as PERMIT) wraps the badge
        assert "\033[1;33mbypass" in row

    def test_unknown_mode_renders_dash(self, monkeypatch, capsys):
        projects = [
            ccm_core.Project(
                win_target="0:1", win_idx="1", name="shellproj",
                directory="/tmp/shellproj", state="SHELL",
            ),
        ]
        out = self._run_print_status(projects, monkeypatch, capsys)
        row = next(line for line in out.splitlines() if "shellproj" in line)
        assert _strip_ansi(row).split()[3] == "-"  # MODE column shows "-"


# ─── print_bg_sessions ───

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s):
    return _ANSI_RE.sub("", s)


class TestPrintBgSessions:
    """Capture-stdout tests for the `ccm bg list` CLI. The function
    is mostly column formatting on top of the well-tested agent-view
    reader, so we don't need exhaustive coverage — just the three
    branches a user will actually see (populated / empty-daemon-up /
    empty-daemon-down) plus that ANSI escapes survive into stdout
    without breaking the layout."""

    def _run(self, monkeypatch, capsys, sessions, daemon_running=True):
        import ccm_agentview
        monkeypatch.setattr(ccm_agentview, "list_bg_sessions",
                            lambda: list(sessions))
        monkeypatch.setattr(ccm_agentview, "daemon_running",
                            lambda: daemon_running)
        ccm_render.print_bg_sessions()
        return capsys.readouterr().out

    def test_empty_with_daemon_running(self, monkeypatch, capsys):
        out = self._run(monkeypatch, capsys, [], daemon_running=True)
        assert "No active background sessions" in _strip_ansi(out)
        # Must NOT advertise the daemon as missing
        assert "is not running" not in out

    def test_empty_with_daemon_down(self, monkeypatch, capsys):
        out = self._run(monkeypatch, capsys, [], daemon_running=False)
        plain = _strip_ansi(out)
        assert "daemon is not running" in plain
        # Hint to start one
        assert "claude --bg" in plain or "claude agents" in plain

    def test_populated_renders_short_state_name(self, monkeypatch, capsys):
        import ccm_agentview
        now = time.time()
        sessions = [
            ccm_agentview.BgSession(
                short="8f7bfb5b", pid=11974, cwd="/Users/u/proj",
                name="Continue agent view work", state="WORKING",
                raw_state="working", tempo="active",
                cli_version="2.1.139", session_id="x",
                created_at=now - 120, updated_at=now,
                source="slash",
            ),
        ]
        out = self._run(monkeypatch, capsys, sessions)
        plain = _strip_ansi(out)
        assert "8f7bfb5b" in plain
        assert "WORKING" in plain
        assert "Continue agent view work" in plain
        # Age column should render minutes (~2m since created_at)
        assert "2m" in plain
        # Header row must precede the data row
        header_pos = plain.find("SHORT")
        data_pos = plain.find("8f7bfb5b")
        assert 0 <= header_pos < data_pos

    def test_state_icon_present(self, monkeypatch, capsys):
        """Each state must produce its agent-view icon (so users
        moving between `claude agents` TUI and ccm see the same
        glyphs)."""
        import ccm_agentview
        sessions = [
            ccm_agentview.BgSession(
                short=f"abc0000{i}", pid=i, cwd="/tmp",
                name=f"s{i}", state=state, raw_state=state.lower(),
                tempo="idle", cli_version="", session_id="",
                created_at=None, updated_at=None, source="",
            )
            for i, state in enumerate(["WORKING", "NEEDS", "IDLE",
                                       "DONE", "FAILED", "UNKNOWN"])
        ]
        out = self._run(monkeypatch, capsys, sessions)
        plain = _strip_ansi(out)
        # Icons defined in ccm_agentview.STATE_ICONS
        for icon in ("✽", "✻", "●", "✓", "✕", "?"):
            assert icon in plain, (
                f"missing icon {icon!r} in bg list output: {plain!r}"
            )


