"""Tests for ccm_send — `ccm send` cross-pane prompt delivery with
state-based gating (PERMIT hard-refuse, BUSY behind --force,
SHELL behind --start)."""

import io

from unittest.mock import patch

import pytest

import ccm_constants
import ccm_core
import ccm_send


class TestCmdSend:
    """Unit tests for `ccm send` — the cross-project prompt injector."""

    def _make_project(self, name="blog", state="IDLE", win_target="0:5"):
        return ccm_core.Project(
            win_target=win_target,
            win_idx=win_target.split(":")[1],
            name=name,
            directory=f"/tmp/{name}",
            state=state,
        )

    def _patch_resolution(self, monkeypatch, project=None, session="0"):
        """Install stubs for get_session / find_window / build_project_list."""
        if project is None:
            project = self._make_project()
        monkeypatch.setattr(ccm_core, "get_session", lambda: session)
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: project.win_idx if name == project.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [project],
        )
        # Non-interactive by default so the confirmation prompt is skipped
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        return project

    def _tmux_calls(self, mock_tmux):
        """Return the positional args of every tmux_cmd call."""
        return [tuple(c.args) for c in mock_tmux.call_args_list]

    # --- happy path ---

    def test_basic_send(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # Cancel any stuck mode
        assert ("send-keys", "-t", "0:5", "-X", "cancel") in calls
        # Literal send of "hello"
        assert ("send-keys", "-t", "0:5", "-l", "hello") in calls
        # Final Enter
        assert ("send-keys", "-t", "0:5", "Enter") in calls
        # Enter comes after the literal send
        literal_i = calls.index(("send-keys", "-t", "0:5", "-l", "hello"))
        enter_i = calls.index(("send-keys", "-t", "0:5", "Enter"))
        assert enter_i > literal_i

    def test_send_concatenates_multiple_positional_args(self, monkeypatch):
        """`ccm send blog hello world` joins the remaining argv into a
        single message, matching how the shell passes unquoted words."""
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "hello", "world"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hello world") in calls

    def test_send_no_enter(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "--no-enter", "hi"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hi") in calls
        assert ("send-keys", "-t", "0:5", "Enter") not in calls

    def test_send_multiline_uses_m_enter_between_lines(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        message = "line1\nline2\nline3"
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", message])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "line1") in calls
        assert ("send-keys", "-t", "0:5", "-l", "line2") in calls
        assert ("send-keys", "-t", "0:5", "-l", "line3") in calls
        # Two M-Enter separators, one final Enter
        m_enter_count = sum(
            1 for c in calls if c == ("send-keys", "-t", "0:5", "M-Enter")
        )
        enter_count = sum(
            1 for c in calls if c == ("send-keys", "-t", "0:5", "Enter")
        )
        assert m_enter_count == 2
        assert enter_count == 1

    def test_send_from_file(self, monkeypatch, tmp_path):
        self._patch_resolution(monkeypatch)
        f = tmp_path / "msg.txt"
        f.write_text("from file")
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "--file", str(f)])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "from file") in calls

    def test_send_from_stdin(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO("piped"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "--stdin"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "piped") in calls

    def test_send_dash_alias_for_stdin(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO("piped2"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "-"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "piped2") in calls

    def test_send_stdin_from_tty_skips_confirmation(self, monkeypatch):
        """Regression guard for the silent-cancel bug:

        A TTY user running `ccm send blog --stdin` and typing a
        message terminated by Ctrl-D consumes stdin. The confirmation
        prompt's `input()` call would then raise EOFError because
        stdin is exhausted, and the `except EOFError` branch would
        silently cancel — the user sees "Cancelled" and never gets
        the preview, and the message is lost.

        Fix: reading stdin force-sets skip_confirm. This test simulates
        the scenario with isatty=True and a StringIO stdin, and asserts
        the message is still sent."""
        self._patch_resolution(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO("typed body"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)

        # Patch builtins.input so that if the fix regressed we would
        # raise a clear error instead of EOFError.
        def _fail_input(*a, **k):
            raise AssertionError(
                "confirmation prompt should have been skipped after "
                "consuming stdin"
            )
        monkeypatch.setattr("builtins.input", _fail_input)

        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "--stdin"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "typed body") in calls
        assert ("send-keys", "-t", "0:5", "Enter") in calls

    def test_send_double_dash_ends_flag_parsing(self, monkeypatch):
        """`--` makes subsequent args positional even if they start with `-`."""
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "--", "--force-looking-message"])
        calls = self._tmux_calls(mock_tmux)
        assert (
            "send-keys", "-t", "0:5", "-l", "--force-looking-message"
        ) in calls

    def test_send_resolves_numeric_index(self, monkeypatch):
        project = self._make_project(win_target="0:7")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["7", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert any("0:7" in c for c in calls)

    def test_send_resolves_hash_index(self, monkeypatch):
        project = self._make_project(win_target="0:7")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["#7", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert any("0:7" in c for c in calls)

    # --- state gating ---

    def test_send_permit_rejected(self, monkeypatch, capsys):
        """PERMIT state is a hard guard — refuse unconditionally."""
        project = self._make_project(state="PERMIT")
        self._patch_resolution(monkeypatch, project=project)
        # capture-pane returns the session-resume modal content
        resume_tail = (
            "This session is 1h 30m old and 25k tokens.\n"
            "1. Resume from summary (recommended)\n"
            "Enter to confirm · Esc to cancel"
        )
        with patch("ccm_core.tmux_cmd", return_value=resume_tail), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "hello"])
        err = capsys.readouterr().err
        # Refusal must expose classification + guidance + pane tail so
        # a caller (human or another Claude) can relay the situation
        # without peeking into the target pane themselves.
        assert "PERMIT state" in err
        assert "Classification: session-resume" in err
        assert "Guidance:" in err
        assert "Pane tail:" in err
        assert "Resume from summary" in err

    def test_send_permit_rejected_even_with_force(self, monkeypatch):
        """Even --force cannot override a PERMIT guard."""
        project = self._make_project(state="PERMIT")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd", return_value=""), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "--force", "hello"])

    def test_send_permit_refusal_classifies_permission_dialog(
        self, monkeypatch, capsys,
    ):
        """A tool permission dialog (Tab to amend footer) must be
        classified distinctly — the guidance text warns that this
        modal is dangerous to auto-dismiss."""
        project = self._make_project(state="PERMIT")
        self._patch_resolution(monkeypatch, project=project)
        perm_tail = (
            "Do you want to proceed?\n"
            "Run `ls /tmp`\n"
            "Esc to cancel · Tab to amend"
        )
        with patch("ccm_core.tmux_cmd", return_value=perm_tail), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "hello"])
        err = capsys.readouterr().err
        assert "Classification: permission-request" in err
        assert "DANGEROUS" in err or "dangerous" in err

    def test_send_idle_with_agents_tui_refused(self, monkeypatch, capsys):
        """An IDLE pane that's actually showing the `claude agents`
        TUI must refuse `ccm send`. The TUI shows an `❯` prompt that
        reads as IDLE in ccm's state detection, but keystrokes there
        spawn a NEW agent-view session rather than landing in any
        existing Claude conversation. Without this guard a casual
        `ccm send foo "..."` would silently dispatch a session."""
        project = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project=project)
        tui_tail = (
            "session a · idle  · 2m\n"
            "session b · working · 14s\n"
            "❯ \n"
            "enter to open · space to reply · ctrl+x to delete · ? for shortcuts"
        )
        with patch("ccm_core.tmux_cmd", return_value=tui_tail), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "hello"])
        err = capsys.readouterr().err
        assert "claude agents" in err
        assert "send refused" in err.lower() or "refused" in err.lower()
        # Pane tail surfaced so the caller can confirm classification
        assert "Pane tail:" in err or "pane tail" in err.lower()

    def test_send_agents_tui_refused_even_with_force(self, monkeypatch):
        """`--force` queues a message into a BUSY target; it does NOT
        map onto "dispatch a new agent" semantics. The TUI refusal is
        unconditional, mirroring PERMIT's behaviour."""
        project = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project=project)
        tui_tail = "enter to open · space to reply · ? for shortcuts"
        with patch("ccm_core.tmux_cmd", return_value=tui_tail), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "--force", "hello"])

    def test_send_busy_rejected_without_force(self, monkeypatch):
        project = self._make_project(state="BUSY")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "hello"])

    def test_send_busy_allowed_with_force(self, monkeypatch):
        project = self._make_project(state="BUSY")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "--force", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hello") in calls

    def test_send_shell_rejected_without_start(self, monkeypatch):
        project = self._make_project(state="SHELL")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "hello"])

    def _patch_start_polling(self, monkeypatch, initial, after_start):
        """Make `build_project_list` return `initial` on the first
        call (the lookup at the top of `cmd_send`) and `after_start`
        on every subsequent call (the `_wait_for_target_idle` poll).
        Skips the 0.5 s polling sleep so the test runs fast."""
        call_count = [0]
        def stub_build(fast=False):
            call_count[0] += 1
            return [initial if call_count[0] == 1 else after_start]
        monkeypatch.setattr(ccm_core, "build_project_list", stub_build)
        monkeypatch.setattr("time.sleep", lambda _s: None)

    def test_send_shell_with_start_launches_claude_first(self, monkeypatch):
        initial = self._make_project(state="SHELL")
        after_start = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project=initial)
        self._patch_start_polling(monkeypatch, initial, after_start)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # Claude launch command appears before the message payload.
        # The call tuple is ("send-keys", "-t", target, CLAUDE_CMD, "Enter").
        claude_i = next(
            (i for i, c in enumerate(calls) if ccm_constants.CLAUDE_CMD in c),
            None,
        )
        literal_i = next(
            (i for i, c in enumerate(calls)
             if c == ("send-keys", "-t", "0:5", "-l", "hello")),
            None,
        )
        assert claude_i is not None, "Claude launch not issued"
        assert literal_i is not None, "Message not sent"
        assert claude_i < literal_i

    def test_send_start_refuses_when_target_stays_busy(self, monkeypatch):
        """`/compact` auto-running on resume keeps the target in
        BUSY past the timeout. The send must refuse with a clear
        message and the captured pane tail — never silently
        deliver keystrokes to a non-prompt screen."""
        initial = self._make_project(state="SHELL")
        stuck_busy = self._make_project(state="BUSY")
        self._patch_resolution(monkeypatch, project=initial)
        self._patch_start_polling(monkeypatch, initial, stuck_busy)
        # Force timeout to one poll so the test is fast.
        monkeypatch.setattr(ccm_send, "START_WAIT_SEC", 0)
        with patch("ccm_core.tmux_cmd") as mock_tmux, pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # The literal message must NOT have been sent.
        literal_sent = any(
            c == ("send-keys", "-t", "0:5", "-l", "hello") for c in calls
        )
        assert not literal_sent, "Refused-send leaked a literal payload"

    def test_send_start_refuses_on_permit_short_circuit(self, monkeypatch):
        """If `claude --continue` resumes into a session-resume
        picker or other PERMIT modal, the keystrokes would land on
        a permission dialog and could accidentally approve/deny.
        Refuse without waiting for the full timeout."""
        initial = self._make_project(state="SHELL")
        permit = self._make_project(state="PERMIT")
        self._patch_resolution(monkeypatch, project=initial)
        self._patch_start_polling(monkeypatch, initial, permit)
        with patch("ccm_core.tmux_cmd") as mock_tmux, pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        literal_sent = any(
            c == ("send-keys", "-t", "0:5", "-l", "hello") for c in calls
        )
        assert not literal_sent

    def test_send_start_proceeds_after_brief_busy(self, monkeypatch):
        """A short BUSY transient (MCP loading) followed by IDLE
        should still result in a successful send. Polling must
        keep retrying until the timeout, not refuse on the first
        non-IDLE observation."""
        initial = self._make_project(state="SHELL")
        busy = self._make_project(state="BUSY")
        idle = self._make_project(state="IDLE")
        states = iter([initial, busy, busy, idle, idle])
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: initial.win_idx if name == initial.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list",
            lambda fast=False: [next(states)],
        )
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hello") in calls

    def test_send_idle_state_allowed(self, monkeypatch):
        project = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "hi"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "hi") in calls

    # --- error paths ---

    def test_send_unknown_project_rejected(self, monkeypatch):
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(ccm_core, "find_window", lambda s, n: None)
        monkeypatch.setattr(ccm_core, "build_project_list", lambda fast=False: [])
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_send.cmd_send(["nonexistent", "hi"])

    def test_send_no_target_rejected(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(SystemExit):
            ccm_send.cmd_send([])

    def test_send_empty_message_rejected(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "   "])

    def test_send_dual_source_rejected(self, monkeypatch, tmp_path):
        """Positional message + --file is an error (exactly one source)."""
        self._patch_resolution(monkeypatch)
        f = tmp_path / "m.txt"
        f.write_text("from file")
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "positional", "--file", str(f)])

    def test_send_unknown_flag_rejected(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd"), pytest.raises(SystemExit):
            ccm_send.cmd_send(["blog", "--nope", "hi"])


class TestSendTrace:
    """`CCM_SEND_TRACE=1` opt-in mechanism that records every
    send-keys call to `$CCM_TMP_DIR/send-trace.log`. Used to diff
    sender intent vs receiver-perceived content when an operator
    reports a drop. The trace itself must:

      - Be inert when the env var is unset (no file created, no
        extra subprocess calls).
      - Be active when the env var is set (every send-keys logged,
        send-start / send-end markers bracket the loop).
      - Never block the send (file write errors are swallowed).
    """

    def _patch_resolution(self, monkeypatch, project=None, session="0"):
        if project is None:
            project = ccm_core.Project(
                win_target="0:5", win_idx="5", name="blog",
                directory="/tmp/blog", state="IDLE",
            )
        monkeypatch.setattr(ccm_core, "get_session", lambda: session)
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: project.win_idx if name == project.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [project],
        )
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        return project

    def test_trace_off_creates_no_log(self, monkeypatch, tmp_path):
        """Default state — `CCM_SEND_TRACE` unset — leaves no trace
        artefact. Important because operators run `ccm send` in
        every interactive session; an always-on log file would
        balloon over time and could expose typed prompts to anyone
        reading $TMPDIR."""
        monkeypatch.delenv("CCM_SEND_TRACE", raising=False)
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd"):
            ccm_send.cmd_send(["blog", "hello\nworld"])
        assert not (tmp_path / "send-trace.log").exists()

    def test_trace_on_records_each_send_keys_call(self, monkeypatch, tmp_path):
        """`CCM_SEND_TRACE=1` records:
          - pre-cancel  (the `-X cancel` cleanup),
          - send-start  (one marker with project / line / byte counts),
          - one line:N  per non-empty line,
          - one newline:N  between lines,
          - final-submit  (the closing Enter),
          - send-end  (closing marker).

        Each row is tab-separated `<unix-ts>\\t<target>\\t<label>\\t<keys-repr>`."""
        monkeypatch.setenv("CCM_SEND_TRACE", "1")
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd"):
            ccm_send.cmd_send(["blog", "line1\nline2"])
        log = (tmp_path / "send-trace.log").read_text().splitlines()
        labels = [row.split("\t")[2] for row in log]
        assert "pre-cancel" in labels
        assert "send-start" in labels
        assert "line:0" in labels
        assert "newline:0" in labels
        assert "line:1" in labels
        assert "final-submit" in labels
        assert "send-end" in labels

    def test_trace_records_literal_line_content(self, monkeypatch, tmp_path):
        """The trace must preserve the literal text exactly as it was
        sent to `tmux send-keys -l <line>` — that's the whole point
        of capturing it. Embedded spaces, brackets, and non-ASCII
        must survive the `repr()` round-trip."""
        monkeypatch.setenv("CCM_SEND_TRACE", "1")
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        self._patch_resolution(monkeypatch)
        payload = "  indented 【test】 日本語"
        with patch("ccm_core.tmux_cmd"):
            ccm_send.cmd_send(["blog", payload])
        log = (tmp_path / "send-trace.log").read_text()
        # The line row should contain repr() of (`-l`, payload) tuple.
        assert repr(payload) in log
        assert "'-l'" in log

    @pytest.mark.parametrize("flag_value", ["0", "false", "off", "no", ""])
    def test_trace_off_for_negative_values(self, monkeypatch, tmp_path, flag_value):
        """`CCM_SEND_TRACE=0` (and other falsy spellings) must NOT
        enable tracing. Operators export the var globally for one
        debugging session and forget to unset it; reading falsy
        values as off keeps the leak window small."""
        monkeypatch.setenv("CCM_SEND_TRACE", flag_value)
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd"):
            ccm_send.cmd_send(["blog", "hi"])
        assert not (tmp_path / "send-trace.log").exists()

    def test_trace_write_failure_does_not_block_send(self, monkeypatch, tmp_path):
        """The trace path being non-writable (disk full, permissions,
        $TMPDIR unwritable) must not break the actual send. The
        feature is diagnostic — its absence is acceptable, but
        breaking the message delivery would be a serious regression."""
        monkeypatch.setenv("CCM_SEND_TRACE", "1")
        # Point CCM_TMP_DIR at a file (not a directory) so open()
        # raises OSError on every trace attempt.
        bad_path = tmp_path / "not-a-dir"
        bad_path.write_text("blocker")
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(bad_path))
        self._patch_resolution(monkeypatch)
        # The send itself must still complete (no exception).
        with patch("ccm_core.tmux_cmd") as mock_tmux:
            ccm_send.cmd_send(["blog", "hi"])
        # Specifically: the literal send-keys still fired despite
        # the trace write failures.
        calls = [tuple(c.args) for c in mock_tmux.call_args_list]
        assert ("send-keys", "-t", "0:5", "-l", "hi") in calls
