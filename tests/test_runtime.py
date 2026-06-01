"""Tests for ccm_runtime — autosave + idle auto-exit + window-name
update helpers. The silent-NameError class of bug (caught by
`log_caught_exception` in production) makes these paths easy to
break without anyone noticing, so the regression coverage here
focuses on `actually called the underlying function` rather than
just `did not raise`."""

import os
from unittest.mock import patch, MagicMock

import pytest

import ccm_runtime
import ccm_core
import ccm_snapshot


class TestForceAutosave:
    """`_force_autosave` is called from `auto_exit_idle` after
    closing an idle window, so it must persist the snapshot or the
    user loses the project on the next `ccm start _autosave`."""

    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_calls_snapshot_save(self, mock_save):
        ccm_runtime._force_autosave()
        mock_save.assert_called_once_with("_autosave", quiet=True)

    @patch("ccm_core.log_caught_exception")
    @patch("ccm_snapshot.cmd_snapshot_save", side_effect=OSError("disk full"))
    def test_swallows_exception_and_logs(self, mock_save, mock_log):
        # Best-effort path: must not propagate, but must record.
        ccm_runtime._force_autosave()
        mock_log.assert_called_once_with("_force_autosave")


class TestPeriodicAutosave:
    """`periodic_autosave` runs every poll cycle (every ~2 s via
    `inject_status`). It must (a) be cheap to call repeatedly, (b)
    actually persist when 120 s have elapsed, (c) leave a stale
    snapshot alone when no projects exist."""

    @patch("ccm_core.tmux_cmd", return_value="")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_no_projects_skips_save(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_save.assert_not_called()

    @patch("ccm_core.tmux_cmd", return_value="proj1\nproj2\n")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_with_projects_and_no_marker_saves(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        # Fresh marker file (last_save=0) → 120 s threshold passed → save.
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_save.assert_called_once_with("_autosave", quiet=True)
        marker = os.path.join(tmp_path, "autosave-time")
        assert os.path.exists(marker)

    @patch("ccm_core.tmux_cmd", return_value="proj1\n")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_recent_marker_skips_save(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        import time
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        marker = os.path.join(tmp_path, "autosave-time")
        with open(marker, "w") as f:
            f.write(str(int(time.time())))  # just-saved marker
        ccm_runtime.periodic_autosave()
        mock_save.assert_not_called()

    @patch("ccm_core.log_caught_exception")
    @patch("ccm_core.tmux_cmd", return_value="proj1\n")
    @patch("ccm_snapshot.cmd_snapshot_save", side_effect=OSError("disk full"))
    def test_save_failure_logs_silently(
        self, mock_save, mock_tmux, mock_log, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_log.assert_called_once_with("periodic_autosave")


class TestAutoExitIdle:
    """`auto_exit_idle` walks ccm windows past the idle timeout, sends
    `/exit` to Claude, then `clear` to wipe the screen for the next
    attach. The `clear` step is the regression hot-spot: if `/exit`
    takes longer than the 0.5 s wait (heavy conversation history,
    confirmation modals, …), Claude is still the foreground process
    when `clear` arrives — and `send-keys "clear" "Enter"` delivers
    literal text into Claude's input box, submitting it as a user
    prompt. Empirically observed in long-session blog and ccm panes
    in 2026-05-31, with the same mechanism explaining earlier "test"
    one-word injections.

    Coverage:
      - Happy path: shell foreground (`zsh`) → `clear` IS sent.
      - Race path:  Claude still alive (`claude`) → `clear` skipped.
      - Failure path: tmux query returns "" → `clear` skipped (fail-safe).
    """

    @staticmethod
    def _build_tmux_side_effect(pane_current_command):
        """Wire `tmux_cmd` so it returns the right value for each
        query auto_exit_idle makes during a single past-timeout pass.

        - idle-timeout option lookup → ""  (use IDLE_EXIT_TIMEOUT)
        - display-message session → "main"
        - display-message window  → "0"
        - list-windows -a -F      → one expired window (non-focused)
        - display-message #{pane_current_command} → parameterised
        - send-keys / set-option  → ""  (side effects only)
        """
        def side_effect(*args):
            if args[:2] == ("show-option", "-gqv"):
                return ""  # fall through to IDLE_EXIT_TIMEOUT default
            if args[:2] == ("display-message", "-p"):
                fmt = args[2]
                if fmt == "#{session_name}":
                    return "main"
                if fmt == "#{window_index}":
                    return "0"  # current focused window
            if args[:2] == ("display-message", "-t"):
                # `display-message -t <target> -p <format>`
                fmt = args[-1]
                if fmt == "#{pane_current_command}":
                    return pane_current_command
            if args[0] == "list-windows":
                # Single ccm window at main:1 (NOT main:0 → not focused),
                # IDLE for 9999 s (well past the 600 s default timeout).
                old = "1"  # ancient unix ts; idle_since=max → 9999s+ ago
                return f"main:1\tblog\tIDLE\t{old}\t{old}"
            return ""  # send-keys, set-option, anything else — return value unused
        return side_effect

    def _run(self, pane_current_command):
        send_calls = []
        tmux_se = self._build_tmux_side_effect(pane_current_command)

        def tmux_recorder(*args):
            if args and args[0] == "send-keys":
                send_calls.append(args)
            return tmux_se(*args)

        with patch("ccm_core.tmux_cmd", side_effect=tmux_recorder), \
             patch("ccm_detection._set_win_state"), \
             patch("ccm_runtime._force_autosave"), \
             patch("ccm_runtime.time.sleep"):
            ccm_runtime.auto_exit_idle([])
        return send_calls

    def test_clear_sent_when_shell_foreground(self):
        """Happy path: `/exit` completed, pane returned to zsh, so
        `clear` is safe to send. This is the original intent of the
        auto-exit cleanup."""
        send_calls = self._run("zsh")
        sent_strings = [c[3] for c in send_calls if len(c) >= 4]
        assert "clear" in sent_strings, (
            f"expected `clear` to be sent to a shell pane, "
            f"got send-keys calls: {send_calls}"
        )

    def test_clear_skipped_when_claude_still_running(self):
        """Race path: `/exit` did not complete within the 0.5 s wait
        (heavy session, confirmation modal, slow shutdown). pane
        foreground is still `claude`. ccm MUST NOT send `clear` —
        otherwise the literal text leaks into Claude's input box as
        an unintended user prompt. This is the bug we're fixing."""
        send_calls = self._run("claude")
        sent_strings = [c[3] for c in send_calls if len(c) >= 4]
        assert "clear" not in sent_strings, (
            f"`clear` was sent while Claude was still the foreground "
            f"process — would land as user input. send-keys: {send_calls}"
        )

    def test_clear_skipped_when_pane_query_fails(self):
        """Failure path: `tmux_cmd` returns "" on subprocess timeout
        or non-zero exit. We treat that as "unknown foreground" and
        skip `clear` rather than risk a stray keystroke. Cosmetic
        cost (stale screen on next attach) is acceptable; safety
        gain (no clear leak) is the priority."""
        send_calls = self._run("")
        sent_strings = [c[3] for c in send_calls if len(c) >= 4]
        assert "clear" not in sent_strings, (
            f"`clear` was sent despite a failed pane_current_command "
            f"query — should fail-safe to skip. send-keys: {send_calls}"
        )
