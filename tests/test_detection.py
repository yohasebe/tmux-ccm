"""Tests for ccm_detection.

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
    VALID_RESOLVED_STATES,
    iso_ts,
    make_ctx,
    make_ps_lines,
    real_activity_record,
    system_record,
    write_jsonl,
)

# Backward-compat alias used by some tests.
_iso_ts = iso_ts

class TestDetectWindowStateHooks:
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_raw_permit_overrides_stale_busy_hook(self, mock_hook, mock_tmux):
        """capture-pane detects PERMIT footer + stale BUSY hook → PERMIT.

        End-to-end scenario for anthropics/claude-code#16047: Claude Code
        stopped firing PermissionRequest mid-session, so the hook signal
        is stuck on stale BUSY. The capture-pane fallback must win.
        """
        hook_ts = int(time.time()) - 600  # 10 min stale
        mock_hook.return_value = (hook_ts, "BUSY", "")
        mock_tmux.return_value = (
            "❯ 1. Yes\n  2. No\n"
            "Esc to cancel · Tab to amend · ctrl+e to explain"
        )
        # claude with no child (permission dialog pre-tool-spawn)
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_raw_permit_without_any_hook(self, mock_hook, mock_tmux):
        """capture-pane PERMIT + no hook signal at all → PERMIT."""
        mock_hook.return_value = None
        mock_tmux.return_value = (
            "  Esc to cancel · Tab to amend · ctrl+e to explain"
        )
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_shell_ignores_hooks(self, mock_hook, mock_tmux):
        """raw=SHELL → SHELL regardless of hook signals."""
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        ps = make_ps_lines((100, 1, 100, "bash"))  # No claude
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "SHELL"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_accept_edits_without_children_returns_idle(self, mock_hook, mock_tmux):
        """Safety net: ⏵⏵ visible, no children → IDLE (waiting for user action)."""
        mock_hook.return_value = None  # No hook signal (expired)
        mock_tmux.return_value = "Some output\n❯ \n  ⏵⏵ accept edits on (shift+tab to cycle)"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_no_prompt_no_hook_returns_idle_not_busy(self, mock_hook, mock_tmux):
        """Safety net removed: no prompt, no hook → IDLE (trust process tree)."""
        mock_hook.return_value = None  # No hook signal
        mock_tmux.return_value = "Some tool output without prompt"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_session_end_hook_ignored_when_idle(self, mock_hook, mock_tmux):
        """raw=IDLE + hook=SHELL → IDLE (process tree authoritative; stale SHELL signal ignored)."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_session_end_hook_with_shell_raw(self, mock_hook, mock_tmux):
        """raw=SHELL + hook=SHELL → SHELL (consistent)."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"))  # no claude process
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        assert state == "SHELL"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_session_end_hook_ignored_when_busy(self, mock_hook, mock_tmux):
        """raw=BUSY + hook=SHELL should not happen in practice, but raw=BUSY takes priority."""
        mock_hook.return_value = (int(time.time()), "SHELL", "")
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"), (300, 200, 100, "node"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "BUSY", panes, ps, "99999"
        )
        # raw=BUSY (children running), SHELL hook is ignored since condition is raw in ("SHELL", "IDLE")
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_no_hook_no_capture_permit(self, mock_hook, mock_tmux):
        """Without hook signal, PERMIT text on screen does NOT trigger PERMIT (hook-only detection)."""
        mock_hook.return_value = None
        mock_tmux.return_value = "Do you want to allow this?\n  Yes  No"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    def test_permit_expires_after_max_timeout(self, mock_hook, mock_tmux):
        """Stale PERMIT signal (older than PERMIT_MAX_TIMEOUT) is ignored in hook path.

        When PERMIT expires, the hook check falls through. With prev_state=PERMIT,
        the fallback keeps PERMIT until a new hook signal arrives. But with
        prev_state=IDLE (no prior PERMIT), it would stay IDLE.
        """
        old_ts = int(time.time()) - ccm_core.PERMIT_MAX_TIMEOUT - 10
        mock_hook.return_value = (old_ts, "PERMIT", "")
        mock_tmux.return_value = str(old_ts - 5)
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]

        # With prev_state=IDLE: expired PERMIT doesn't resurrect
        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/project", "IDLE", panes, ps, "99999"
        )
        assert state == "IDLE"




class TestApplyActions:
    """Direct tests for the side-effect layer.

    Uses a tmp dir for the hook signal path and mocks `_set_win_state`
    so we can assert both tmux writes and filesystem writes per action.
    """

    @pytest.fixture
    def project_dir(self, tmp_path, monkeypatch):
        # Redirect hook signal files to an isolated tmp dir
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_HOOK_DIR", str(hook_dir))
        proj = tmp_path / "proj"
        proj.mkdir()
        return str(proj)

    def _run(self, rule, ctx, project_dir="", win_target="0:1"):
        with patch.object(ccm_detection, "_set_win_state") as set_win:
            with patch.object(ccm_core, "tmux_cmd") as mock_tmux:
                result = ccm_detection.apply_actions(
                    win_target, project_dir, ctx, rule, rule.result
                )
        return result, set_win, mock_tmux

    def test_default_writes_state(self):
        rule = ccm_rules.Rule(name="t", result="BUSY", action=ccm_rules.Action.DEFAULT)
        ctx = make_ctx()
        state, set_win, _ = self._run(rule, ctx)
        assert state == "BUSY"
        set_win.assert_called_once_with("0:1", "BUSY")

    def test_hold_no_write_skips_tmux(self):
        rule = ccm_rules.Rule(
            name="t", result="PERMIT", action=ccm_rules.Action.HOLD_NO_WRITE
        )
        ctx = make_ctx()
        state, set_win, _ = self._run(rule, ctx)
        assert state == "PERMIT"
        set_win.assert_not_called()

    def test_completed_at_set_on_busy_to_idle(self, project_dir):
        """apply_actions sets @ccm_completed_at on BUSY→IDLE transition."""
        rule = ccm_rules.Rule(name="t", result="IDLE", action=ccm_rules.Action.DEFAULT)
        ctx = make_ctx(prev_state="BUSY", now=12345)
        state, set_win, mock_tmux = self._run(rule, ctx, project_dir=project_dir)
        assert state == "IDLE"
        completed_calls = [c for c in mock_tmux.call_args_list
                           if len(c[0]) > 3 and "@ccm_completed_at" in str(c[0])]
        assert len(completed_calls) > 0

    def test_completed_at_cleared_on_idle_to_busy(self, project_dir):
        """When the project leaves IDLE, the stored @ccm_completed_at is
        cleared with `set-option -wut`. Display already filters by
        state, but clearing the stored value defends against a fresh
        IDLE re-entry that didn't go through BUSY/PERMIT (e.g. claude
        crash + restart) reviving a stale '* 5s' marker.
        """
        rule = ccm_rules.Rule(name="t", result="BUSY", action=ccm_rules.Action.DEFAULT)
        ctx = make_ctx(prev_state="IDLE", now=12345)
        state, _set_win, mock_tmux = self._run(rule, ctx, project_dir=project_dir)
        assert state == "BUSY"
        clear_calls = [
            c for c in mock_tmux.call_args_list
            if c[0][:2] == ("set-option", "-wut")
            and "@ccm_completed_at" in c[0]
        ]
        assert len(clear_calls) == 1, (
            f"Expected one `set-option -wut @ccm_completed_at` call on "
            f"IDLE→BUSY transition, got {len(clear_calls)}"
        )

    def test_completed_at_cleared_on_idle_to_shell(self, project_dir):
        """The clear also fires on IDLE→SHELL, covering the claude-crash
        edge case the rule was added for.
        """
        rule = ccm_rules.Rule(name="t", result="SHELL", action=ccm_rules.Action.DEFAULT)
        ctx = make_ctx(prev_state="IDLE", raw="SHELL", now=12345)
        state, _set_win, mock_tmux = self._run(rule, ctx, project_dir=project_dir)
        assert state == "SHELL"
        clear_calls = [
            c for c in mock_tmux.call_args_list
            if c[0][:2] == ("set-option", "-wut")
            and "@ccm_completed_at" in c[0]
        ]
        assert len(clear_calls) == 1


class TestLifecycleSequences:
    """End-to-end state transition sequences, evaluated as pure rule chains.

    Each test walks a realistic Claude Code lifecycle (user prompt → tool
    execution → permission → completion) and asserts that the rule table
    produces the right state at every step. No tmux/ps/file mocking —
    Context is constructed directly so we focus on detection logic.
    """

    def _eval(self, **ctx_kwargs):
        rule, state = ccm_rules.evaluate_rules(make_ctx(**ctx_kwargs))
        return rule.name, state

    def test_shell_override_anywhere(self):
        """SHELL from process tree wins over any hook state, any prev."""
        for prev in ("IDLE", "BUSY", "PERMIT"):
            for hook in ("", "BUSY", "PERMIT"):
                name, state = self._eval(
                    raw="SHELL", prev_state=prev, hook_state=hook, hook_age=0,
                )
                assert state == "SHELL", f"prev={prev} hook={hook}"

    def test_startup_transient_young_claude_shows_idle(self):
        """Real scenario: user `ccm attach`es to a SHELL window;
        auto-start fires `claude --continue`; MCP servers start up
        before the `❯` prompt is rendered. `detect_pane_state` sees
        `has_child=True + no prompt` and returns raw=BUSY — this
        signature looked identical to a streaming response, producing
        10+ s of false BUSY on every attach.

        The authoritative discriminator is the `claude` process's own
        age: real work cannot have started without a hook firing, so
        raw=BUSY + hook_missing + claude_pid_age < STARTUP_GRACE_SEC
        uniquely identifies MCP-loading startup."""
        # Claude has been running for 5 s, no hook signal, no prior
        # prev_state (fresh window). MCP servers are still connecting.
        rule, state = self._eval(
            raw="BUSY", prev_state="SHELL", hook_state="",
            jsonl_age=-1, claude_pid_age=5,
        )
        assert rule == "startup_transient_raw_busy"
        assert state == "IDLE"

    def test_startup_transient_applies_regardless_of_prev_state(self):
        """The scan sequence during startup is
            SHELL (prev) → IDLE (brief, claude with no MCP yet)
                        → BUSY (MCP appearing, no prompt yet)
        so by the time raw flips to BUSY, prev_state has already been
        written to IDLE by the `default` rule firing on the scan
        before. The rule must still fire on prev_state=IDLE so the
        dashboard doesn't revert to BUSY mid-startup. Process age is
        monotonic and authoritative — prev_state is not."""
        for prev in ("", "SHELL", "IDLE"):
            rule, state = self._eval(
                raw="BUSY", prev_state=prev, hook_state="",
                jsonl_age=-1, claude_pid_age=10,
            )
            assert rule == "startup_transient_raw_busy", \
                f"startup rule should fire for prev={prev!r}"
            assert state == "IDLE"

    def test_startup_transient_expires_after_grace(self):
        """After STARTUP_GRACE_SEC the rule no longer matches; raw=BUSY
        with no hook evidence falls back through `raw_busy_passthrough` → BUSY.
        This is the right outcome: if Claude is genuinely hung during
        startup (e.g. a malfunctioning MCP server keeps blocking the
        prompt render past 60 s), the user should see BUSY rather than
        a false IDLE."""
        rule, state = self._eval(
            raw="BUSY", prev_state="IDLE", hook_state="",
            jsonl_age=-1,
            claude_pid_age=ccm_core.STARTUP_GRACE_SEC + 10,
        )
        assert rule != "startup_transient_raw_busy"
        assert state == "BUSY"

    def test_startup_transient_holds_no_write(self):
        """The rule uses action=HOLD_NO_WRITE so the pipeline does not
        commit IDLE to @ccm_prev_state while Claude is still loading.
        Whatever prev_state was before the rule matched (SHELL, "",
        or a transient IDLE from a prior scan) is preserved."""
        ctx = make_ctx(
            raw="BUSY", prev_state="SHELL", hook_state="",
            jsonl_age=-1, claude_pid_age=5,
        )
        rule, state = ccm_rules.evaluate_rules(ctx)
        assert rule.name == "startup_transient_raw_busy"
        assert rule.action == ccm_rules.Action.HOLD_NO_WRITE

    def test_startup_transient_does_not_override_fresh_hook(self):
        """Once UserPromptSubmit fires (hook_state='BUSY'), the window
        is in real work and hook_fresh_busy wins. This preserves the
        "user typed a prompt right after attaching" lifecycle —
        startup_transient must NOT suppress a legitimate BUSY that
        the hook pipeline already proved."""
        rule, state = self._eval(
            raw="BUSY", prev_state="SHELL",
            hook_state="BUSY", hook_age=0, jsonl_age=0,
            claude_pid_age=5,
        )
        assert rule == "hook_fresh_busy"
        assert state == "BUSY"

    def test_startup_transient_requires_known_pid_age(self):
        """claude_pid_age=-1 (the ps snapshot had no etime column, or
        the claude pid was not in the snapshot) must NOT match the
        rule. Without a known process age we cannot distinguish
        startup from real work, and BUSY passthrough is the safer
        default."""
        rule, state = self._eval(
            raw="BUSY", prev_state="SHELL", hook_state="",
            jsonl_age=-1, claude_pid_age=-1,
        )
        assert rule != "startup_transient_raw_busy"
        assert state == "BUSY"

    def test_startup_transient_clears_once_prompt_renders(self):
        """When Claude finishes loading MCP and `❯` becomes visible,
        raw flips to IDLE. The startup rule no longer matches
        (raw_in=("BUSY",)); the terminal `default` rule fires
        USE_RAW → IDLE with DEFAULT action, writing prev_state=IDLE.
        From the next scan onward, we are in the normal lifecycle."""
        rule, state = self._eval(
            raw="IDLE", prev_state="SHELL", hook_state="", jsonl_age=-1,
            claude_pid_age=20,
        )
        assert rule == "default"
        assert state == "IDLE"

    # ─── Esc-interrupt and silent-completion lifecycles ───

# ─── Property / invariant tests ───
#
# These tests assert global invariants of the detection pipeline
# rather than scenario-specific outputs. They catch entire classes
# of regressions (e.g. "ever returning DOWN from a non-SHELL pid"
# would break dashboard rendering) and document the design contract
# that future rule edits must preserve.



class TestPipelineInvariants:
    """Run `evaluate_rules` over the full Cartesian product of
    inputs and verify each output respects the documented invariants.
    Pure unit-level — no mocks."""

    @pytest.mark.parametrize("raw", ["IDLE", "BUSY", "PERMIT", "SHELL", "DOWN"])
    @pytest.mark.parametrize("hook_state", ["", "BUSY", "PERMIT"])
    @pytest.mark.parametrize("prev_state", ["IDLE", "BUSY", "PERMIT", "SHELL"])
    def test_resolved_state_always_in_valid_set(
        self, raw, hook_state, prev_state
    ):
        """Every (raw × hook × prev) combination must resolve to one
        of the documented states. New rules must not introduce e.g.
        a stray "DONE" or lowercase variant — the dashboard renderer
        and ccm send dispatcher both key on this set."""
        rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw=raw, hook_state=hook_state, prev_state=prev_state,
                     hook_age=10 if hook_state else -1, jsonl_age=10)
        )
        assert state in VALID_RESOLVED_STATES, (
            f"raw={raw} hook={hook_state} prev={prev_state} → "
            f"rule={rule.name} state={state} (not in VALID_RESOLVED_STATES)"
        )

    @pytest.mark.parametrize("hook_state", ["", "BUSY", "PERMIT"])
    @pytest.mark.parametrize("prev_state", ["IDLE", "BUSY", "PERMIT", "SHELL"])
    def test_raw_shell_always_resolves_to_shell(self, hook_state, prev_state):
        """raw=SHELL is authoritative from the process tree (no claude
        process). Must always win over any hook signal or prev_state.
        Without this guarantee, a stale BUSY hook could keep the
        dashboard at BUSY for a project whose Claude was killed."""
        _rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw="SHELL", hook_state=hook_state, prev_state=prev_state,
                     hook_age=10 if hook_state else -1)
        )
        assert state == "SHELL"

    @pytest.mark.parametrize("hook_state", ["", "BUSY", "PERMIT"])
    def test_raw_down_always_resolves_to_down(self, hook_state):
        """raw=DOWN means no pane process at all (window deleted).
        Must always resolve to DOWN regardless of any other signal."""
        _rule, state = ccm_rules.evaluate_rules(
            make_ctx(raw="DOWN", hook_state=hook_state,
                     hook_age=10 if hook_state else -1)
        )
        assert state == "DOWN"

class TestDebugTraceHook:
    """The CCM_DEBUG_TRACE env var routes every scan cycle into a
    JSONL trace file. Used as a post-hoc debugging tool for
    false-BUSY / false-IDLE reports — the trace captures the full
    DetectionContext + matched rule + action at the time of each
    scan, without having to reproduce the issue while `ccm debug
    trace` is running.
    """

    def _basic_ctx(self):
        return make_ctx(
            raw="BUSY", hook_state="", hook_ts=0, hook_age=-1,
            prev_state="SHELL", jsonl_age=-1, claude_pid_age=5,
        )

    def test_writes_one_line_per_scan(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.log"
        monkeypatch.setenv("CCM_DEBUG_TRACE", str(trace))
        ctx = self._basic_ctx()
        rule, state = ccm_rules.evaluate_rules(ctx)
        with patch.object(ccm_detection, "_set_win_state"), \
             patch.object(ccm_core, "tmux_cmd"):
            ccm_detection.apply_actions("0:5", "/p", ctx, rule, state)
            ccm_detection.apply_actions("0:5", "/p", ctx, rule, state)

        lines = trace.read_text().strip().split("\n")
        assert len(lines) == 2
        # Each line is valid JSON with expected keys.
        for line in lines:
            rec = json.loads(line)
            assert rec["target"] == "0:5"
            assert rec["raw"] == "BUSY"
            assert rec["prev"] == "SHELL"
            assert rec["rule"] == "startup_transient_raw_busy"
            assert rec["state"] == "IDLE"
            assert rec["action"] == "hold_no_write"
            # Phase metadata must be included so trace consumers can
            # group records by session-lifecycle phase without a
            # separate rule→phase lookup.
            assert rec["phase"] == "startup"

    def test_no_env_var_no_write(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.log"
        # Do NOT set CCM_DEBUG_TRACE. The file must stay absent.
        monkeypatch.delenv("CCM_DEBUG_TRACE", raising=False)
        ctx = self._basic_ctx()
        rule, state = ccm_rules.evaluate_rules(ctx)
        with patch.object(ccm_detection, "_set_win_state"), \
             patch.object(ccm_core, "tmux_cmd"):
            ccm_detection.apply_actions("0:5", "/p", ctx, rule, state)
        assert not trace.exists()

    def test_unwritable_path_does_not_raise(self, tmp_path, monkeypatch):
        # Point at a path inside a nonexistent directory. open() will
        # raise FileNotFoundError; the trace hook must swallow it so
        # detection is never disrupted by a broken trace target.
        bad = tmp_path / "does-not-exist" / "trace.log"
        monkeypatch.setenv("CCM_DEBUG_TRACE", str(bad))
        ctx = self._basic_ctx()
        rule, state = ccm_rules.evaluate_rules(ctx)
        with patch.object(ccm_detection, "_set_win_state"), \
             patch.object(ccm_core, "tmux_cmd"):
            # Must not raise.
            ccm_detection.apply_actions("0:5", "/p", ctx, rule, state)

    def test_stops_appending_past_size_cap(self, tmp_path, monkeypatch):
        """When the trace file exceeds TRACE_MAX_BYTES, _trace_scan
        writes one sentinel line and stops appending. Guard against
        a forgotten CCM_DEBUG_TRACE filling the disk."""
        trace = tmp_path / "trace.log"
        # Pre-fill the trace file past the (lowered-for-test) cap so
        # the very next apply_actions hits the sentinel path. Use a
        # newline-terminated prefix so the sentinel lands on its own
        # line and can be recovered via split("\n")[-1] below.
        monkeypatch.setattr(ccm_detection, "TRACE_MAX_BYTES", 100)
        trace.write_text(("x" * 200) + "\n")
        monkeypatch.setenv("CCM_DEBUG_TRACE", str(trace))

        ctx = self._basic_ctx()
        rule, state = ccm_rules.evaluate_rules(ctx)
        with patch.object(ccm_detection, "_set_win_state"), \
             patch.object(ccm_core, "tmux_cmd"):
            # Two scans: the first trips the cap and writes the sentinel
            # (pushing size past cap+200); the second sees size >= that
            # and writes nothing.
            ccm_detection.apply_actions("0:5", "/p", ctx, rule, state)
            size_after_first = trace.stat().st_size
            ccm_detection.apply_actions("0:5", "/p", ctx, rule, state)
            size_after_second = trace.stat().st_size

        # Exactly one sentinel appended; the second call is a no-op.
        assert size_after_second == size_after_first
        # Last line must be the sentinel, identifiable by the
        # `event` field.
        last_line = trace.read_text().rstrip("\n").split("\n")[-1]
        rec = json.loads(last_line)
        assert rec.get("event") == "trace_cap_reached"
        assert rec.get("cap_bytes") == 100

    def test_only_diff_skips_agreeing_rows(self, tmp_path, monkeypatch):
        """CCM_TRACE_ONLY_DIFF=1 must skip rows where the legacy and
        event-log derivations agree. This is the filter that lets
        observe-mode run for days without hitting the size cap — on
        a quiet run the trace stays empty until a disagreement shows
        up."""
        trace = tmp_path / "trace.log"
        monkeypatch.setenv("CCM_DEBUG_TRACE", str(trace))
        monkeypatch.setenv("CCM_TRACE_ONLY_DIFF", "1")
        ctx = self._basic_ctx()
        rule, state = ccm_rules.evaluate_rules(ctx)
        with patch.object(ccm_detection, "_set_win_state"), \
             patch.object(ccm_core, "tmux_cmd"):
            # event_log_state == state → no diff, must be skipped.
            ccm_detection.apply_actions(
                "0:5", "/p", ctx, rule, state, event_log_state=state,
            )
        assert not trace.exists()

    def test_only_diff_writes_when_states_disagree(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.log"
        monkeypatch.setenv("CCM_DEBUG_TRACE", str(trace))
        monkeypatch.setenv("CCM_TRACE_ONLY_DIFF", "1")
        ctx = self._basic_ctx()
        rule, state = ccm_rules.evaluate_rules(ctx)
        # Pick any valid state different from the legacy result so
        # the diff actually triggers a write. Using IDLE here since
        # the basic ctx (raw=BUSY) resolves to BUSY.
        disagreeing = "IDLE" if state != "IDLE" else "PERMIT"
        with patch.object(ccm_detection, "_set_win_state"), \
             patch.object(ccm_core, "tmux_cmd"):
            ccm_detection.apply_actions(
                "0:5", "/p", ctx, rule, state, event_log_state=disagreeing,
            )
        assert trace.exists()
        rec = json.loads(trace.read_text().rstrip("\n"))
        assert rec["state"] == state
        assert rec["event_log_state"] == disagreeing
        assert rec.get("diff") is True

    def test_only_diff_skips_when_event_log_state_absent(
        self, tmp_path, monkeypatch
    ):
        """CCM_TRACE_ONLY_DIFF=1 with CCM_USE_EVENT_LOG unset (so no
        event_log_state is computed) must skip everything. Otherwise
        leaving the flag set by accident would produce a misleading
        "silent trace" that actually means "mode never activated"."""
        trace = tmp_path / "trace.log"
        monkeypatch.setenv("CCM_DEBUG_TRACE", str(trace))
        monkeypatch.setenv("CCM_TRACE_ONLY_DIFF", "1")
        ctx = self._basic_ctx()
        rule, state = ccm_rules.evaluate_rules(ctx)
        with patch.object(ccm_detection, "_set_win_state"), \
             patch.object(ccm_core, "tmux_cmd"):
            # event_log_state=None (default) → nothing to diff.
            ccm_detection.apply_actions("0:5", "/p", ctx, rule, state)
        assert not trace.exists()

    @pytest.mark.parametrize("value", ["0", "off", "false", "no", ""])
    def test_only_diff_falsy_values_disabled(
        self, tmp_path, monkeypatch, value
    ):
        """Falsy values for CCM_TRACE_ONLY_DIFF must leave the trace
        in its default "write every row" mode. Parallels the
        CCM_USE_EVENT_LOG parser semantics."""
        trace = tmp_path / "trace.log"
        monkeypatch.setenv("CCM_DEBUG_TRACE", str(trace))
        monkeypatch.setenv("CCM_TRACE_ONLY_DIFF", value)
        ctx = self._basic_ctx()
        rule, state = ccm_rules.evaluate_rules(ctx)
        with patch.object(ccm_detection, "_set_win_state"), \
             patch.object(ccm_core, "tmux_cmd"):
            # No diff, but flag is off → row is still written.
            ccm_detection.apply_actions(
                "0:5", "/p", ctx, rule, state, event_log_state=state,
            )
        assert trace.exists()


class TestEventLogEnabled:
    @pytest.mark.parametrize("raw,expected", [
        # Falsy values → False (diagnostic kill-switch)
        ("", False),
        ("0", False),
        ("off", False),
        ("OFF", False),
        ("no", False),
        ("false", False),
        # Anything else → True (the default behaviour)
        ("auto", True),
        ("AUTO", True),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("garbage", True),
    ])
    def test_normalizes_env_value(self, raw, expected, monkeypatch):
        monkeypatch.setenv("CCM_USE_EVENT_LOG", raw)
        assert ccm_detection._event_log_enabled() is expected

    def test_unset_defaults_to_enabled(self, monkeypatch):
        monkeypatch.delenv("CCM_USE_EVENT_LOG", raising=False)
        assert ccm_detection._event_log_enabled() is True

    def test_whitespace_trimmed_off(self, monkeypatch):
        monkeypatch.setenv("CCM_USE_EVENT_LOG", "  off  ")
        assert ccm_detection._event_log_enabled() is False


# ─── auto-mode detect_window_state integration ───
#
# Auto-mode wiring: when CCM_USE_EVENT_LOG=auto, the event-log
# state takes over per-project IFF the event log is non-empty;
# otherwise the legacy DETECTION_RULES result is committed. The
# tests below exercise the dispatch from detect_window_state — the
# pure derive function and the env-var parser are covered above.

class TestDetectWindowStateAutoMode:
    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    @patch("ccm_signals.read_events_tail")
    def test_auto_with_empty_events_uses_legacy(
        self, mock_events, mock_hook, mock_tmux, monkeypatch
    ):
        """No event log file (or empty file) → legacy state wins.
        With raw=BUSY (process tree shows children + no `❯`), the
        legacy `raw_busy_passthrough` rule commits BUSY. The
        event-log derive on empty events would have returned None
        (defer to legacy), so the dispatch must not short-circuit
        on the empty-events case."""
        monkeypatch.setenv("CCM_USE_EVENT_LOG", "auto")
        mock_events.return_value = ()  # no events recorded
        mock_hook.return_value = None
        # capture-pane: no input prompt → raw=BUSY (children exist).
        mock_tmux.return_value = "Some output"
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"),
            (300, 200, 200, "node"),  # claude grandchild → raw=BUSY
        )
        panes = [("0:1", "100", "%0", "claude", "1", "48")]
        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/proj", "IDLE", panes, ps, "99999"
        )
        # Legacy raw_busy_passthrough → BUSY.
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    @patch("ccm_signals.read_events_tail")
    def test_auto_with_events_uses_event_log_state(
        self, mock_events, mock_hook, mock_tmux, monkeypatch
    ):
        """Event log has a `pretool` event → event-log derive returns
        BUSY and that state is committed. To prove the dispatch wired
        up correctly we make the legacy path return IDLE (no hook
        signal, no jsonl activity) — only the event-log path can
        produce BUSY in this configuration."""
        monkeypatch.setenv("CCM_USE_EVENT_LOG", "auto")
        mock_events.return_value = (
            {"ts": int(time.time()), "type": "pretool"},
        )
        mock_hook.return_value = None  # no hook signal
        # input prompt visible → raw=IDLE per detect_pane_state
        mock_tmux.return_value = "❯ "
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]
        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/proj", "IDLE", panes, ps, "99999"
        )
        # Event-log derivation wins: latest event class start → BUSY.
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    @patch("ccm_signals.read_events_tail")
    def test_primary_falls_back_to_legacy_on_empty_events(
        self, mock_events, mock_hook, mock_tmux, monkeypatch
    ):
        """primary mode shares auto's None-aware dispatch: when
        derive returns None on empty events, the legacy state
        wins — same outcome as auto."""
        monkeypatch.setenv("CCM_USE_EVENT_LOG", "primary")
        mock_events.return_value = ()
        mock_hook.return_value = None
        mock_tmux.return_value = "Some output"
        ps = make_ps_lines(
            (100, 1, 100, "bash"), (200, 100, 100, "claude"),
            (300, 200, 200, "node"),  # raw=BUSY via grandchild
        )
        panes = [("0:1", "100", "%0", "claude", "1", "48")]
        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/proj", "IDLE", panes, ps, "99999"
        )
        # Empty events → derive returns None → legacy raw_busy_passthrough.
        assert state == "BUSY"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    @patch("ccm_signals.read_events_tail")
    def test_auto_with_empty_events_and_permit_pane_keeps_permit(
        self, mock_events, mock_hook, mock_tmux, monkeypatch
    ):
        """Scenario: event log file went temporarily
        missing for 2.7 hours while the pane was actually showing a
        PERMIT modal. Pre-fix behaviour was derive()→IDLE, auto mode
        committing IDLE, and `ccm send` happily injecting into the
        modal. Post-fix: derive returns None, legacy fires raw=PERMIT
        and the dashboard stays on PERMIT."""
        monkeypatch.setenv("CCM_USE_EVENT_LOG", "auto")
        mock_events.return_value = ()  # event log empty / missing
        mock_hook.return_value = None  # no hook signal either
        # capture-pane returns a PERMIT modal footer line.
        mock_tmux.return_value = "  Esc to cancel · Tab to amend"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]
        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/proj", "IDLE", panes, ps, "99999"
        )
        # Legacy raw=PERMIT fallback wins.
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    @patch("ccm_signals.read_events_tail")
    def test_auto_with_pretool_event_and_permit_pane_keeps_permit(
        self, mock_events, mock_hook, mock_tmux, monkeypatch
    ):
        """Scenario: PreToolUse fired
        first (latest event = pretool → derive→BUSY), then the modal
        appeared, and PermissionRequest had not yet fired. Pre-fix
        auto mode would have committed BUSY (event-log) and the
        dashboard would have lost the ⚠ prompt. Post-fix: A' raw=PERMIT
        override forces PERMIT regardless of latest event."""
        monkeypatch.setenv("CCM_USE_EVENT_LOG", "auto")
        mock_events.return_value = (
            {"ts": int(time.time()), "type": "pretool"},
        )
        mock_hook.return_value = (int(time.time()), "BUSY", "")
        # capture-pane shows PERMIT modal footer.
        mock_tmux.return_value = "  Esc to cancel · Tab to amend"
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]
        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/proj", "IDLE", panes, ps, "99999"
        )
        # raw=PERMIT override → derive returns PERMIT not BUSY.
        assert state == "PERMIT"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    @patch("ccm_signals.read_events_tail")
    def test_explicit_off_does_not_read_events(
        self, mock_events, mock_hook, mock_tmux, monkeypatch
    ):
        """`CCM_USE_EVENT_LOG=off` is the legacy-only opt-out — the
        event log file must not be touched at all (matches the
        the legacy-only fallback)."""
        monkeypatch.setenv("CCM_USE_EVENT_LOG", "off")
        mock_events.return_value = (
            {"ts": int(time.time()), "type": "pretool"},
        )
        mock_hook.return_value = None
        mock_tmux.return_value = "❯ "
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]
        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/proj", "IDLE", panes, ps, "99999"
        )
        # The reader is never called in the off path. Legacy-only
        # behaviour: ❯ visible + no hook → IDLE.
        mock_events.assert_not_called()
        assert state == "IDLE"

    @patch("ccm_core.tmux_cmd")
    @patch("ccm_signals.read_hook_signal")
    @patch("ccm_signals.read_events_tail")
    def test_unset_default_uses_auto_dispatch(
        self, mock_events, mock_hook, mock_tmux, monkeypatch
    ):
        """With CCM_USE_EVENT_LOG unset, the event
        log IS consulted — and the event-log state takes over per the
        auto-mode dispatch. Same scenario as
        test_auto_with_events_uses_event_log_state but env unset."""
        monkeypatch.delenv("CCM_USE_EVENT_LOG", raising=False)
        mock_events.return_value = (
            {"ts": int(time.time()), "type": "pretool"},
        )
        mock_hook.return_value = None  # legacy returns IDLE
        mock_tmux.return_value = "❯ "
        ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 100, "claude"))
        panes = [("0:1", "100", "%0", "claude", "1", "48")]
        state = ccm_detection.detect_window_state(
            "0:1", "/tmp/proj", "IDLE", panes, ps, "99999"
        )
        # Reader was called (events consulted under default mode).
        mock_events.assert_called()
        # Event-log derive returns BUSY for pretool → auto dispatch
        # commits BUSY (the same behaviour as explicit auto).
        assert state == "BUSY"
