"""Tests for ccm_canaries.

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
import ccm_window
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

class TestHooksLogWarning:
    def test_no_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_canaries, "CLAUDE_HOOKS_LOG", str(tmp_path / "missing.log"))
        assert ccm_canaries.hooks_log_size() == -1
        assert ccm_canaries.hooks_log_warning() == ""

    def test_small_file_returns_empty(self, tmp_path, monkeypatch):
        log = tmp_path / "hooks.log"
        log.write_text("a" * 1024)  # 1 KB
        monkeypatch.setattr(ccm_canaries, "CLAUDE_HOOKS_LOG", str(log))
        assert ccm_canaries.hooks_log_warning() == ""

    def test_bloated_file_returns_warning(self, tmp_path, monkeypatch):
        log = tmp_path / "hooks.log"
        log.write_text("x")  # tiny file
        monkeypatch.setattr(ccm_canaries, "CLAUDE_HOOKS_LOG", str(log))
        # Lower threshold so the tiny file qualifies
        monkeypatch.setattr(ccm_canaries, "HOOKS_LOG_WARN_BYTES", 0)
        msg = ccm_canaries.hooks_log_warning()
        assert "hooks.log" in msg
        assert "#16047" in msg
        assert ": > ~/.claude/hooks.log" in msg


# ─── disableAllHooks canary ───

class TestDisableAllHooksWarning:
    def test_no_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(tmp_path / "missing.json"))
        assert ccm_canaries.disable_all_hooks_warning() == ""

    def test_setting_absent(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"other": "value"}')
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_canaries.disable_all_hooks_warning() == ""

    def test_setting_false(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"disableAllHooks": false}')
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_canaries.disable_all_hooks_warning() == ""

    def test_setting_true_returns_warning(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"disableAllHooks": true}')
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(f))
        msg = ccm_canaries.disable_all_hooks_warning()
        assert "disableAllHooks" in msg
        assert "settings.json" in msg
        # disableAllHooks also kills the custom statusLine. The
        # warning must tell the user so they can correlate a missing
        # embedded statusLine with this flag.
        assert "statusLine" in msg

    def test_malformed_json(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text("not json")
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_canaries.disable_all_hooks_warning() == ""


# ─── allowManagedHooksOnly canary ───

class TestManagedHooksOnlyWarning:
    def test_no_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(tmp_path / "missing.json"))
        assert ccm_canaries.managed_hooks_only_warning() == ""

    def test_setting_absent(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"other": "value"}')
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_canaries.managed_hooks_only_warning() == ""

    def test_setting_false(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"allowManagedHooksOnly": false}')
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_canaries.managed_hooks_only_warning() == ""

    def test_setting_true_returns_warning(self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text('{"allowManagedHooksOnly": true}')
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(f))
        msg = ccm_canaries.managed_hooks_only_warning()
        assert "allowManagedHooksOnly" in msg
        assert "user-scope hooks" in msg

    def test_independent_from_disable_all_hooks(self, tmp_path, monkeypatch):
        """Both canaries can fire independently or together."""
        f = tmp_path / "settings.json"
        f.write_text('{"allowManagedHooksOnly": true, "disableAllHooks": true}')
        monkeypatch.setattr(ccm_canaries, "CLAUDE_SETTINGS_FILE", str(f))
        assert ccm_canaries.managed_hooks_only_warning() != ""
        assert ccm_canaries.disable_all_hooks_warning() != ""

class TestShellClusterDetection:
    """Unit tests for the cluster-SHELL-transition canary that
    surfaces anthropics/claude-code#48069 (silent-exit regression)."""

    def _tmux_mock(self):
        """Build a tmux_cmd mock that maintains a fake per-option store
        in memory, so push and read round-trip correctly."""
        store = {}  # "target/opt" → value

        def fake_tmux(*args):
            # show-option -wqv -t <target> <name>
            if len(args) >= 5 and args[0] == "show-option":
                target = args[3]
                opt = args[4]
                return store.get(f"{target}/{opt}", "")
            # set-option -wt <target> <name> <value>
            if len(args) >= 5 and args[0] == "set-option" and "-u" not in args:
                target = args[2]
                opt = args[3]
                value = args[4]
                store[f"{target}/{opt}"] = value
                return ""
            # list-windows -a -F '#{session_name}:#{window_index}\t#{<opt>}'
            # Used by `_read_all_shell_histories` (batch path). Parse the
            # opt name from the format string and emit one line per
            # known target in the store that has that option set.
            if (len(args) >= 4 and args[0] == "list-windows"
                    and "-a" in args and "-F" in args):
                fmt = args[args.index("-F") + 1]
                # Format always looks like "...{<opt>}" — extract the
                # last `@<...>` reference.
                m = re.search(r"#\{(@[^}]+)\}$", fmt)
                if not m:
                    return ""
                opt = m.group(1)
                lines = []
                seen_targets = set()
                for k, v in store.items():
                    target, _, key = k.partition("/")
                    if key != opt or target in seen_targets:
                        continue
                    seen_targets.add(target)
                    lines.append(f"{target}\t{v}")
                return "\n".join(lines)
            return ""

        return fake_tmux, store

    def test_empty_history_no_warning(self, monkeypatch):
        fake, _store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        assert ccm_canaries.shell_cluster_warning("0:1", "proj") == ""

    def test_below_threshold_no_warning(self, monkeypatch):
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        store["0:1/@ccm_shell_history"] = f"{now},{now - 10}"  # only 2 entries
        assert ccm_canaries.shell_cluster_warning("0:1", "proj") == ""

    def test_at_threshold_fires_warning(self, monkeypatch):
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        store["0:1/@ccm_shell_history"] = f"{now},{now - 60},{now - 120}"  # 3 entries
        msg = ccm_canaries.shell_cluster_warning("0:1", "proj")
        assert "proj" in msg
        assert "#48069" in msg
        assert "3" in msg  # the count

    def test_stale_entries_ignored(self, monkeypatch):
        """Entries older than SHELL_CLUSTER_WINDOW are filtered out
        on read so the count only reflects recent events."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        # 2 recent + 2 ancient (past 10 min window by default 600s)
        store["0:1/@ccm_shell_history"] = (
            f"{now},{now - 60},"
            f"{now - ccm_canaries.SHELL_CLUSTER_WINDOW - 100},"
            f"{now - ccm_canaries.SHELL_CLUSTER_WINDOW - 200}"
        )
        # Only 2 recent entries — below 3 threshold
        assert ccm_canaries.shell_cluster_warning("0:1", "proj") == ""

    def test_push_prepends_and_trims_stale(self, monkeypatch):
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        # Start with 1 stale + 1 recent
        store["0:1/@ccm_shell_history"] = f"{now - 60},{now - ccm_canaries.SHELL_CLUSTER_WINDOW - 100}"
        ccm_canaries._push_shell_transition("0:1")
        history = ccm_canaries._read_shell_history("0:1")
        # New timestamp prepended, stale filtered out (on READ)
        assert len(history) == 2
        assert history[0] >= now  # newly pushed is newest

    def test_push_dedups_same_second(self, monkeypatch):
        """A second push within the same second should be a no-op
        so that two rule evaluations in the same cycle do not
        double-count one transition."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        ccm_canaries._push_shell_transition("0:1")
        ccm_canaries._push_shell_transition("0:1")
        history = ccm_canaries._read_shell_history("0:1")
        assert len(history) == 1

    def test_push_caps_history_length(self, monkeypatch):
        """History is capped at max(SHELL_CLUSTER_COUNT * 2, 6) to
        avoid unbounded growth of the tmux option value."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        # Seed with many entries (all recent, so none are trimmed by
        # the time-horizon filter)
        seed = [str(now - i) for i in range(20)]
        store["0:1/@ccm_shell_history"] = ",".join(seed)
        ccm_canaries._push_shell_transition("0:1")
        history = ccm_canaries._read_shell_history("0:1")
        cap = max(ccm_canaries.SHELL_CLUSTER_COUNT * 2, 6)
        assert len(history) <= cap

    def test_shell_cluster_warnings_iterates_projects(self, monkeypatch):
        """The list helper returns one message per crossing project."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        now = int(time.time())
        store["0:1/@ccm_shell_history"] = f"{now},{now - 60},{now - 120}"  # over threshold
        store["0:2/@ccm_shell_history"] = f"{now}"  # below threshold
        projects = [
            ccm_core.Project("0:1", "1", "alpha", "/tmp/a", "IDLE"),
            ccm_core.Project("0:2", "2", "beta",  "/tmp/b", "IDLE"),
        ]
        msgs = ccm_canaries.shell_cluster_warnings(projects)
        assert len(msgs) == 1
        assert "alpha" in msgs[0]
        assert "beta" not in msgs[0]

    def test_apply_actions_records_shell_transition(self, monkeypatch):
        """A rule that resolves to SHELL with a non-SHELL prev_state
        should trigger _push_shell_transition via apply_actions."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="IDLE")
        rule, state = ccm_rules.evaluate_rules(ctx)
        assert state == "SHELL"  # process_shell fires
        ccm_detection.apply_actions("0:5", "", ctx, rule, state)

        history = ccm_canaries._read_shell_history("0:5")
        assert len(history) == 1  # one new transition recorded

    def test_apply_actions_ignores_shell_to_shell(self, monkeypatch):
        """A steady-state SHELL (prev=SHELL → new=SHELL) should not
        push a new transition. Only transitions into SHELL count."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="SHELL")
        rule, state = ccm_rules.evaluate_rules(ctx)
        ccm_detection.apply_actions("0:5", "", ctx, rule, state)

        history = ccm_canaries._read_shell_history("0:5")
        assert history == []  # no transition recorded

    def test_apply_actions_ignores_empty_prev_state(self, monkeypatch):
        """Regression guard: `reset_window_after_attach()` (called from
        every attach path) explicitly resets `@ccm_prev_state`. The next
        scan then sees prev_state="" and might briefly observe SHELL
        before the new claude process is detected. Without filtering,
        this would inflate the SHELL cluster count by 1 per attach.

        The filter requires prev_state to be a known active state
        (BUSY / IDLE / PERMIT) before counting a transition.
        """
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        # Empty prev_state (post-attach phantom)
        ctx = make_ctx(raw="SHELL", prev_state="")
        rule, state = ccm_rules.evaluate_rules(ctx)
        ccm_detection.apply_actions("0:5", "", ctx, rule, state)

        assert ccm_canaries._read_shell_history("0:5") == []

    def test_apply_actions_ignores_down_to_shell(self, monkeypatch):
        """DOWN → SHELL is not a session crash either. DOWN means
        the window was momentarily without panes; the transition
        is tmux housekeeping, not a Claude exit."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="DOWN")
        rule, state = ccm_rules.evaluate_rules(ctx)
        ccm_detection.apply_actions("0:5", "", ctx, rule, state)

        assert ccm_canaries._read_shell_history("0:5") == []

    def test_apply_actions_records_busy_to_shell(self, monkeypatch):
        """The real-crash case: BUSY → SHELL is counted."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="BUSY")
        rule, state = ccm_rules.evaluate_rules(ctx)
        ccm_detection.apply_actions("0:5", "", ctx, rule, state)

        assert len(ccm_canaries._read_shell_history("0:5")) == 1

    def test_apply_actions_records_permit_to_shell(self, monkeypatch):
        """PERMIT → SHELL also counts."""
        fake, store = self._tmux_mock()
        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)

        ctx = make_ctx(raw="SHELL", prev_state="PERMIT")
        rule, state = ccm_rules.evaluate_rules(ctx)
        ccm_detection.apply_actions("0:5", "", ctx, rule, state)

        assert len(ccm_canaries._read_shell_history("0:5")) == 1

    def test_reset_window_after_attach_clears_shell_history(
        self, monkeypatch, tmp_path
    ):
        """reset_window_after_attach() is the canonical post-attach
        reset, called from cmd_attach and dashboard attach paths.
        It must wipe @ccm_shell_history so the cluster canary
        acknowledges the user's attention.
        """
        # Stub tmux_cmd: track set-option calls and serve show-option
        # for @ccm_dir / @ccm_shell_history.
        store = {"0:5/@ccm_dir": "/tmp/proj", "0:5/@ccm_shell_history": "1,2,3"}
        unset_calls = []

        def fake(*args):
            if len(args) >= 5 and args[0] == "show-option":
                target = args[3]
                opt = args[4]
                return store.get(f"{target}/{opt}", "")
            if args[0] == "set-option":
                # Two real shapes used by reset_window_after_attach():
                #   set-option -wt TARGET -u OPT
                #   set-option -wq -t TARGET OPT VALUE
                target = None
                if "-wt" in args:
                    target = args[args.index("-wt") + 1]
                elif "-t" in args:
                    target = args[args.index("-t") + 1]
                if "-u" in args:
                    opt = args[-1]
                    unset_calls.append((target, opt))
                    store.pop(f"{target}/{opt}", None)
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        # Avoid touching the real CCM_HOOK_DIR
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(tmp_path))

        ccm_window.reset_window_after_attach("0:5")

        # @ccm_shell_history should have been unset
        assert ("0:5", "@ccm_shell_history") in unset_calls
        assert "0:5/@ccm_shell_history" not in store

    def test_reset_window_after_attach_preserves_prev_state(
        self, monkeypatch, tmp_path
    ):
        """Regression guard for the startup_transient_raw_busy rule.
        The rule identifies MCP-loading transients by requiring
        `prev_state ∈ ("", "SHELL")`. If reset_window_after_attach
        wiped prev_state to "", the
        distinguisher would break: attaching to an already-BUSY
        session would also produce prev_state="", and the rule would
        misclassify real work as startup and flash IDLE.

        So: reset_window_after_attach MUST NOT touch @ccm_prev_state.
        The detection pipeline updates it every scan via
        apply_actions — that is the correct owner of the value.
        """
        store = {
            "0:5/@ccm_dir": "/tmp/proj",
            "0:5/@ccm_prev_state": "BUSY",
        }
        set_calls = []

        def fake(*args):
            if len(args) >= 5 and args[0] == "show-option":
                return store.get(f"{args[3]}/{args[4]}", "")
            if args[0] == "set-option":
                target = None
                if "-wt" in args:
                    target = args[args.index("-wt") + 1]
                elif "-t" in args:
                    target = args[args.index("-t") + 1]
                set_calls.append((args, target))
                if "-u" in args:
                    store.pop(f"{target}/{args[-1]}", None)
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(tmp_path))

        ccm_window.reset_window_after_attach("0:5")

        # No set-option call (unset or write) should have touched
        # @ccm_prev_state — the detection layer owns it.
        for args, _target in set_calls:
            assert "@ccm_prev_state" not in args, (
                f"reset_window_after_attach touched @ccm_prev_state: {args}"
            )
        # And the stored value must still be BUSY (pre-attach state).
        assert store["0:5/@ccm_prev_state"] == "BUSY"

    def test_reset_window_after_attach_unsets_completed_at(
        self, monkeypatch, tmp_path
    ):
        """reset_window_after_attach() must unset @ccm_completed_at
        so the ✔ marker disappears immediately on attach.
        """
        store = {
            "0:5/@ccm_dir": "/tmp/proj",
            "0:5/@ccm_completed_at": "1700000000",
        }
        unset_calls = []

        def fake(*args):
            if len(args) >= 5 and args[0] == "show-option":
                target = args[3]
                opt = args[4]
                return store.get(f"{target}/{opt}", "")
            if args[0] == "set-option":
                target = None
                if "-wt" in args:
                    target = args[args.index("-wt") + 1]
                elif "-t" in args:
                    target = args[args.index("-t") + 1]
                if "-u" in args:
                    opt = args[-1]
                    unset_calls.append((target, opt))
                    store.pop(f"{target}/{opt}", None)
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", fake)
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(tmp_path))

        ccm_window.reset_window_after_attach("0:5")

        assert ("0:5", "@ccm_completed_at") in unset_calls
        assert "0:5/@ccm_completed_at" not in store

    def test_reset_window_after_attach_invokes_auto_focus(
        self, monkeypatch
    ):
        """Regression guard for the wiring between
        reset_window_after_attach and auto_focus_attention_pane.
        `TestAutoFocusAttentionPane` (in `tests/test_window.py`)
        tests the helper in isolation — those cases all pass even
        if the call is removed from reset_window_after_attach. This
        case asserts the call actually happens during reset, so an
        accidental removal is caught."""
        called_with = []

        def stub_auto_focus(win_target):
            called_with.append(win_target)

        # Bypass tmux entirely; we only care that auto_focus is
        # invoked. show-option for @ccm_dir must return non-empty
        # so reset doesn't short-circuit.
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *args: "/tmp/proj"
                            if args[:2] == ("show-option", "-wqv")
                            else "")
        monkeypatch.setattr(ccm_window, "auto_focus_attention_pane",
                            stub_auto_focus)
        ccm_window.reset_window_after_attach("0:5")
        assert called_with == ["0:5"]

