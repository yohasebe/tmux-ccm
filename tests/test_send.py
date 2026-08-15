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

    def _make_project(self, name="demo", state="IDLE", win_target="0:5"):
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
        # _resolve_delivery_pane shells out to ps; with tmux_cmd stubbed
        # to "" pane enumeration comes back empty either way, so an
        # empty snapshot preserves the window-target fallback these
        # tests assert against.
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
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
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # Cancel any stuck mode
        assert ("send-keys", "-t", "0:5", "-X", "cancel") in calls
        # Literal send of "hello"
        assert ("send-keys", "-t", "0:5", "-l", "--", "hello") in calls
        # Final Enter
        assert ("send-keys", "-t", "0:5", "Enter") in calls
        # Enter comes after the literal send
        literal_i = calls.index(("send-keys", "-t", "0:5", "-l", "--", "hello"))
        enter_i = calls.index(("send-keys", "-t", "0:5", "Enter"))
        assert enter_i > literal_i

    def test_send_concatenates_multiple_positional_args(self, monkeypatch):
        """`ccm send demo hello world` joins the remaining argv into a
        single message, matching how the shell passes unquoted words."""
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "hello", "world"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "hello world") in calls

    def test_send_no_enter(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--no-enter", "hi"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "hi") in calls
        assert ("send-keys", "-t", "0:5", "Enter") not in calls

    def test_hyphen_leading_lines_are_flag_safe(self, monkeypatch):
        """Lines starting with `-` (Markdown bullets, CLI examples)
        must reach send-keys behind a `--` terminator. Without it,
        tmux parses the line as a flag cluster, errors with "invalid
        flag", and the line is SILENTLY dropped while surrounding
        M-Enters land — the receiver gets the message with every
        bullet line missing (mangled three real cross-project briefs
        before diagnosis,..14). The delivery-verification
        signature survived in non-bullet lines, so only a
        content-level comparison would have caught it."""
        self._patch_resolution(monkeypatch)
        message = "header\n- bullet one\n- bullet two\n--flag-like"
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", message])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "- bullet one") in calls
        assert ("send-keys", "-t", "0:5", "-l", "--", "- bullet two") in calls
        assert ("send-keys", "-t", "0:5", "-l", "--", "--flag-like") in calls
        # Every literal-line send must carry the terminator: no
        # bare ("-l", <text>) form may survive anywhere.
        for c in calls:
            if len(c) >= 5 and c[0] == "send-keys" and c[3] == "-l":
                assert c[4] == "--", f"unterminated literal send: {c}"

    def test_send_multiline_uses_m_enter_between_lines(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        message = "line1\nline2\nline3"
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", message])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "line1") in calls
        assert ("send-keys", "-t", "0:5", "-l", "--", "line2") in calls
        assert ("send-keys", "-t", "0:5", "-l", "--", "line3") in calls
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
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--file", str(f)])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "from file") in calls

    def test_send_from_stdin(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO("piped"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--stdin"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "piped") in calls

    def test_send_dash_alias_for_stdin(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO("piped2"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "-"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "piped2") in calls

    def test_send_stdin_from_tty_skips_confirmation(self, monkeypatch):
        """Regression guard for the silent-cancel bug:

        A TTY user running `ccm send demo --stdin` and typing a
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

        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--stdin"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "typed body") in calls
        assert ("send-keys", "-t", "0:5", "Enter") in calls

    def test_send_double_dash_ends_flag_parsing(self, monkeypatch):
        """`--` makes subsequent args positional even if they start with `-`."""
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--", "--force-looking-message"])
        calls = self._tmux_calls(mock_tmux)
        assert (
            "send-keys", "-t", "0:5", "-l", "--", "--force-looking-message"
        ) in calls

    def test_send_resolves_numeric_index(self, monkeypatch):
        project = self._make_project(win_target="0:7")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["7", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert any("0:7" in c for c in calls)

    def test_send_resolves_hash_index(self, monkeypatch):
        project = self._make_project(win_target="0:7")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["#7", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert any("0:7" in c for c in calls)

    def test_send_prefers_name_match_over_numeric_index(self, monkeypatch):
        """A legacy digit-only project name ("123") must resolve by
        NAME, not as window index 123. validate_name now rejects new
        digit-only names, but projects created before that guard stay
        reachable — name match wins, `#123` remains the explicit
        index escape hatch."""
        project = self._make_project(name="123", win_target="0:7")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["123", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:7", "-l", "--", "hello") in calls

    # --- same-directory second window (seen_dirs dedup fallout) ---

    def _patch_same_dir_second_window(self, monkeypatch, sibling_state):
        """Stub resolution for a send to `dup`, a second window
        (0:9) registered against the same directory as the tracked
        sibling `main` (0:5). build_project_list drops `dup` via
        seen_dirs, so only the sibling appears in the project list."""
        sibling = ccm_core.Project(
            win_target="0:5", win_idx="5", name="main",
            directory="/tmp/shared", state=sibling_state,
        )
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: "9" if name == "dup" else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [sibling],
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)

        def fake_tmux(*args):
            if args[0] == "show-option":
                if "@ccm_project" in args:
                    return "dup"
                if "@ccm_dir" in args:
                    return "/tmp/shared"
            return ""
        return fake_tmux

    def test_send_same_dir_second_window_accepted(self, monkeypatch):
        """Regression: a send to the second same-dir window used to
        die with "not a registered ccm project" because the window is
        missing from build_project_list (seen_dirs dedup). The send
        must succeed, borrowing the tracked sibling's state for
        gating while delivering to the target window itself."""
        fake_tmux = self._patch_same_dir_second_window(monkeypatch, "IDLE")
        with patch("ccm_core.tmux_cmd", side_effect=fake_tmux) as mock_tmux:
            ccm_send.cmd_send(["dup", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # Delivery targets the SECOND window (0:9), not the sibling.
        assert ("send-keys", "-t", "0:9", "-l", "--", "hello") in calls

    def test_send_same_dir_second_window_gated_by_sibling_state(
        self, monkeypatch, capsys
    ):
        """The borrowed gating state comes from the tracked sibling:
        sibling PERMIT → send refused. The refusal must name the
        window the user addressed ("dup"), not the sibling."""
        fake_tmux = self._patch_same_dir_second_window(monkeypatch, "PERMIT")
        with patch("ccm_core.tmux_cmd", side_effect=fake_tmux), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["dup", "--now", "hello"])
        err = capsys.readouterr().err
        assert "dup is in PERMIT state" in err

    def test_send_same_dir_second_window_sibling_shell_refused(
        self, monkeypatch, capsys
    ):
        """Order-dependent limit of the seen_dirs dedup, pinned as a
        regression test: when the FIRST same-dir window (the one the
        dedup keeps) is a bare shell and the second window hosts the
        running claude, the borrowed gating state is SHELL, so a
        plain send to the second window is refused even though Claude
        is actually running there. The current approximation accepts
        this — the workaround is `--start`, or swapping which window
        was opened first."""
        fake_tmux = self._patch_same_dir_second_window(monkeypatch, "SHELL")
        with patch("ccm_core.tmux_cmd", side_effect=fake_tmux), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["dup", "--now", "hello"])
        err = capsys.readouterr().err
        assert "dup is in SHELL state" in err

    def test_send_unregistered_window_still_rejected(self, monkeypatch):
        """A window with no ccm tags at all (empty @ccm_project /
        @ccm_dir) must still die as unregistered — the same-dir
        fallback is not a blanket bypass."""
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd", return_value=""), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["6", "hello"])

    # --- state gating ---

    def test_send_permit_rejected(self, monkeypatch, capsys):
        """PERMIT state is a hard guard — refuse unconditionally.
        (Via --now: by default the message is spooled instead; the
        queued default is covered in tests/test_spool.py.)"""
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
            ccm_send.cmd_send(["demo", "--now", "hello"])
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
            ccm_send.cmd_send(["demo", "--force", "--now", "hello"])

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
            ccm_send.cmd_send(["demo", "--now", "hello"])
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
            ccm_send.cmd_send(["demo", "--now", "hello"])
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
            ccm_send.cmd_send(["demo", "--force", "--now", "hello"])

    def test_send_busy_rejected_without_force(self, monkeypatch):
        project = self._make_project(state="BUSY")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd", return_value=""), pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])

    def test_send_busy_allowed_with_force(self, monkeypatch):
        project = self._make_project(state="BUSY")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--force", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "hello") in calls

    def test_send_shell_rejected_without_start(self, monkeypatch):
        project = self._make_project(state="SHELL")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd", return_value=""), pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])

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
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # Claude launch command appears before the message payload.
        # The call tuple is ("send-keys", "-t", target, CLAUDE_CMD, "Enter").
        claude_i = next(
            (i for i, c in enumerate(calls) if ccm_constants.CLAUDE_CMD in c),
            None,
        )
        literal_i = next(
            (i for i, c in enumerate(calls)
             if c == ("send-keys", "-t", "0:5", "-l", "--", "hello")),
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
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux, pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        # The literal message must NOT have been sent.
        literal_sent = any(
            c == ("send-keys", "-t", "0:5", "-l", "--", "hello") for c in calls
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
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux, pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        literal_sent = any(
            c == ("send-keys", "-t", "0:5", "-l", "--", "hello") for c in calls
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
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--start", "hello"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "hello") in calls

    def test_send_idle_state_allowed(self, monkeypatch):
        project = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project=project)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "hi"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "hi") in calls

    # --- post-launch delivery verification (premature-IDLE fix) ---

    # A message long enough to clear `_DELIVERY_SIG_MIN_LEN` so the
    # verification path actually engages (short messages skip it).
    _VERIFY_MSG = "delegate the region-shade implementation task"

    def _run_start_with_captures(self, monkeypatch, message, capture_responses):
        """Drive a SHELL + --start send where `build_project_list`
        reports IDLE after launch. The FIRST `capture-pane` call is the
        composer-draft guard's, which runs before any typing, so it is
        answered with a bare composer; each LATER call (the
        delivery-verification `_body_landed`) returns the next item of
        `capture_responses` (last item repeats). Returns
        (send_calls, raised) where `raised` is True iff cmd_send exited
        via ccm_die (delivery unconfirmed)."""
        initial = self._make_project(state="SHELL")
        idle = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project=initial)
        self._patch_start_polling(monkeypatch, initial, idle)

        cap_idx = [0]
        send_calls = []

        def tmux_side_effect(*args):
            if args and args[0] == "send-keys":
                send_calls.append(args)
                return ""
            if args and args[0] == "capture-pane":
                if cap_idx[0] == 0:
                    cap_idx[0] += 1
                    # Pre-typing read by the composer-draft guard.
                    return "❯ \n"
                i = min(cap_idx[0] - 1, len(capture_responses) - 1)
                cap_idx[0] += 1
                return capture_responses[i]
            return ""

        raised = False
        with patch("ccm_core.tmux_cmd", side_effect=tmux_side_effect):
            try:
                ccm_send.cmd_send(["demo", "--start", "--yes", message])
            except SystemExit:
                raised = True
        return send_calls, raised

    def test_start_delivery_verified_then_submits(self, monkeypatch):
        """Happy path: after --start launch reaches IDLE, the typed
        body IS present in the composer on the first capture, so the
        final Enter is committed and the send completes."""
        send_calls, raised = self._run_start_with_captures(
            monkeypatch, self._VERIFY_MSG,
            capture_responses=[f"❯ {self._VERIFY_MSG}"],
        )
        assert not raised, "verified delivery should not refuse"
        assert ("send-keys", "-t", "0:5", "-l", "--", self._VERIFY_MSG) in send_calls
        # The committing Enter (bare, no payload) was sent.
        assert ("send-keys", "-t", "0:5", "Enter") in send_calls

    # A long body whose head has scrolled out of the composer. Observed
    # against Kimi K3: a ~30-line brief rendered as
    # `↑ 24 more` with only the trailing lines on screen.
    _LONG_MSG = "\n".join(
        [f"opening line about the region-shade task, part {i}" for i in range(3)]
        + [f"middle line {i} of the same brief" for i in range(20)]
        + ["closing instruction: reply with ccm send when done"]
    )

    def test_start_verifies_against_the_tail_when_head_scrolled_off(
        self, monkeypatch
    ):
        """A composer showing only the END of a long body still counts
        as landed.

        Claude's composer grows upward and keeps the leading row, but a
        body that outgrows the pane scrolls and keeps the trailing row
        instead. Matching the head alone would report "did not land"
        for a message sitting right there — and on this path that means
        re-typing a body that already arrived, then refusing the send."""
        tail = "closing instruction: reply with ccm send when done"
        send_calls, raised = self._run_start_with_captures(
            monkeypatch, self._LONG_MSG,
            capture_responses=[f"↑ 24 more\n  {tail}"],
        )
        assert not raised, "a visible tail must satisfy verification"
        assert ("send-keys", "-t", "0:5", "Enter") in send_calls

    def test_start_verifies_against_the_head_when_tail_scrolled_off(
        self, monkeypatch
    ):
        """The mirror case, so fixing the tail did not trade away the
        head: a composer showing only the opening rows also counts."""
        send_calls, raised = self._run_start_with_captures(
            monkeypatch, self._LONG_MSG,
            capture_responses=[
                "❯ opening line about the region-shade task, part 0"],
        )
        assert not raised, "a visible head must satisfy verification"
        assert ("send-keys", "-t", "0:5", "Enter") in send_calls

    def test_start_premature_idle_refuses_without_false_sent(self, monkeypatch):
        """Premature-IDLE bug: the composer shows but the
        input handler eats the keystrokes, so the body never lands.
        Every capture returns only the placeholder. After retries the
        send must refuse (SystemExit) and must NOT commit the Enter —
        no false 'Sent', no half-delivered prompt."""
        placeholder = '❯ Try "how does .tags work?"'
        send_calls, raised = self._run_start_with_captures(
            monkeypatch, self._VERIFY_MSG,
            capture_responses=[placeholder],  # never contains the body
        )
        assert raised, "unverified delivery must refuse, not claim Sent"
        # The committing Enter must NOT have been sent.
        assert ("send-keys", "-t", "0:5", "Enter") not in send_calls
        # It retried: the body was typed more than once.
        body_types = [
            c for c in send_calls
            if c == ("send-keys", "-t", "0:5", "-l", "--", self._VERIFY_MSG)
        ]
        assert len(body_types) >= 2, "expected at least one retry re-type"
        # Each retry cleared the composer first.
        assert ("send-keys", "-t", "0:5", "C-u") in send_calls

    def test_start_premature_idle_retry_then_succeeds(self, monkeypatch):
        """The body is eaten on the first attempt but lands on a
        retry (input handler became ready). The send then commits the
        Enter and succeeds — the retry rescued the delivery."""
        placeholder = '❯ Try "how does .tags work?"'
        send_calls, raised = self._run_start_with_captures(
            monkeypatch, self._VERIFY_MSG,
            # 1st capture: not landed → retry; 2nd capture: landed.
            capture_responses=[placeholder, f"❯ {self._VERIFY_MSG}"],
        )
        assert not raised, "a successful retry should not refuse"
        assert ("send-keys", "-t", "0:5", "Enter") in send_calls

    def test_start_short_message_skips_verification(self, monkeypatch):
        """A message shorter than the signature minimum cannot be
        matched in the pane without false positives, so verification
        is skipped and the send proceeds as before (no capture-based
        refusal). Guards against the fix breaking tiny --start sends."""
        initial = self._make_project(state="SHELL")
        idle = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project=initial)
        self._patch_start_polling(monkeypatch, initial, idle)
        # capture-pane would return empty (no body), but a short
        # message skips verification entirely, so no refusal.
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "--start", "--yes", "hi"])
        calls = self._tmux_calls(mock_tmux)
        assert ("send-keys", "-t", "0:5", "-l", "--", "hi") in calls
        assert ("send-keys", "-t", "0:5", "Enter") in calls

    # --- error paths ---

    def test_send_unknown_project_rejected(self, monkeypatch):
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(ccm_core, "find_window", lambda s, n: None)
        monkeypatch.setattr(ccm_core, "build_project_list", lambda fast=False: [])
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        with patch("ccm_core.tmux_cmd", return_value=""), pytest.raises(SystemExit):
            ccm_send.cmd_send(["nonexistent", "hi"])

    def test_send_no_target_rejected(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(SystemExit):
            ccm_send.cmd_send([])

    def test_send_empty_message_rejected(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd", return_value=""), pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "   "])

    def test_send_dual_source_rejected(self, monkeypatch, tmp_path):
        """Positional message + --file is an error (exactly one source)."""
        self._patch_resolution(monkeypatch)
        f = tmp_path / "m.txt"
        f.write_text("from file")
        with patch("ccm_core.tmux_cmd", return_value=""), pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "positional", "--file", str(f)])

    def test_send_unknown_flag_rejected(self, monkeypatch):
        self._patch_resolution(monkeypatch)
        with patch("ccm_core.tmux_cmd", return_value=""), pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--nope", "hi"])


class TestDeliveryPaneResolution:
    """Delivery-pane resolution (incident).

    Window state is a pane aggregation (PERMIT > BUSY > IDLE >
    SHELL), but `send-keys -t <window>` lands in the ACTIVE pane.
    A split window with Claude idle in a side pane and an active
    bare zsh aggregated to IDLE, so `ccm send --start` skipped the
    launch and typed the whole message into zsh as shell commands.
    The fix targets the claude-hosting pane id directly."""

    # Two-pane window: %72 is the active zsh, %51 hosts claude
    # (pane shell pid 12077 with a claude child). A pane whose
    # foreground process is claude reports `claude` as
    # pane_current_command — `zsh` there would read as a Ctrl-Z'd
    # claude (SHELL) to detect_pane_state.
    _PANES_CLAUDE_INACTIVE = (
        "%72\t81413\t1\tzsh\n"
        "%51\t12077\t0\tclaude"
    )
    _PS_WITH_CLAUDE = (
        "61814 12077 61814 claude\n"
        "81718 81413 81718 zsh"
    )
    _PS_NO_CLAUDE = "81718 81413 81718 zsh"

    def _make_project(self, state="IDLE"):
        return ccm_core.Project(
            win_target="0:5", win_idx="5", name="demo",
            directory="/tmp/demo", state=state,
        )

    def _patch_resolution(self, monkeypatch, project, ps_text):
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: project.win_idx if name == project.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [project],
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: ps_text)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    def _tmux_stub(self, panes_output, capture_output="❯ "):
        calls = []

        def stub(*args):
            calls.append(tuple(args))
            if args and args[0] == "list-panes":
                return panes_output
            if args and args[0] == "capture-pane":
                return capture_output
            return ""
        return stub, calls

    def test_idle_send_targets_claude_pane_not_active_zsh(self, monkeypatch):
        """The reported accident: window IDLE (from the side claude
        pane) while the active pane is a bare zsh. The literal body
        must go to the claude pane id, never to the window target
        (= active zsh pane)."""
        project = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project, self._PS_WITH_CLAUDE)
        stub, calls = self._tmux_stub(self._PANES_CLAUDE_INACTIVE)
        with patch("ccm_core.tmux_cmd", side_effect=stub):
            ccm_send.cmd_send(["demo", "hello"])
        assert ("send-keys", "-t", "%51", "-l", "--", "hello") in calls
        assert ("send-keys", "-t", "%51", "Enter") in calls
        # Nothing typed at the window target (active zsh pane).
        assert not any(
            c[0] == "send-keys" and c[2] == "0:5" for c in calls
        ), "keystrokes leaked to the window target / active shell pane"

    def test_shell_start_launches_in_active_shell_pane(self, monkeypatch):
        """SHELL window (no claude anywhere) + --start: the launch
        command goes to the ACTIVE pane — the pane the user is
        looking at — once it is confirmed to be a shell."""
        initial = self._make_project(state="SHELL")
        idle = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, initial, self._PS_NO_CLAUDE)

        call_count = [0]
        def build(fast=False):
            call_count[0] += 1
            return [initial if call_count[0] == 1 else idle]
        monkeypatch.setattr(ccm_core, "build_project_list", build)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        stub, calls = self._tmux_stub(self._PANES_CLAUDE_INACTIVE)
        with patch("ccm_core.tmux_cmd", side_effect=stub):
            ccm_send.cmd_send(["demo", "--start", "hi"])
        assert ("send-keys", "-t", "%72",
                ccm_constants.CLAUDE_CMD, "Enter") in calls
        assert ("send-keys", "-t", "%72", "-l", "--", "hi") in calls

    def test_shell_start_refuses_when_active_pane_is_editor(self, monkeypatch):
        """SHELL window whose active pane runs vim (a claude-less
        pane reads SHELL regardless of its foreground): typing the
        launch command would edit text, not start Claude. Refuse."""
        project = self._make_project(state="SHELL")
        self._patch_resolution(monkeypatch, project, self._PS_NO_CLAUDE)
        panes = "%72\t81413\t1\tvim\n%51\t12077\t0\tzsh"
        stub, calls = self._tmux_stub(panes)
        with patch("ccm_core.tmux_cmd", side_effect=stub), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--start", "hi"])
        assert not any(
            c[0] == "send-keys" and ccm_constants.CLAUDE_CMD in c
            for c in calls
        ), "launch command was typed into a non-shell pane"

    def test_multiple_claude_panes_active_wins(self, monkeypatch):
        """Agent Teams split: two claude panes, active one hosts
        claude → deliver to the active pane."""
        project = self._make_project(state="IDLE")
        ps = (
            "61814 12077 61814 claude\n"
            "61999 81413 61999 claude"
        )
        self._patch_resolution(monkeypatch, project, ps)
        panes = "%72\t81413\t1\tclaude\n%51\t12077\t0\tzsh"
        stub, calls = self._tmux_stub(panes)
        with patch("ccm_core.tmux_cmd", side_effect=stub):
            ccm_send.cmd_send(["demo", "hello"])
        assert ("send-keys", "-t", "%72", "-l", "--", "hello") in calls

    def test_multiple_claude_panes_ambiguous_refused(self, monkeypatch):
        """Two claude panes and the active pane hosts neither: the
        delivery target is ambiguous — refuse instead of guessing
        (a wrong guess would inject a prompt into the wrong
        teammate's conversation)."""
        project = self._make_project(state="IDLE")
        ps = (
            "61814 12077 61814 claude\n"
            "61999 81413 61999 claude"
        )
        self._patch_resolution(monkeypatch, project, ps)
        panes = (
            "%70\t99999\t1\tzsh\n"
            "%72\t81413\t0\tzsh\n"
            "%51\t12077\t0\tzsh"
        )
        stub, calls = self._tmux_stub(panes)
        with patch("ccm_core.tmux_cmd", side_effect=stub), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "hello"])
        assert not any(
            c[0] == "send-keys" and c[3:] == ("-l", "--", "hello")
            for c in calls
        ), "literal payload leaked despite ambiguity refusal"

    def test_ambiguity_refusal_offers_the_sidekick_escape_hatch(
        self, monkeypatch, capsys
    ):
        """The refusal names CCM_IGNORE / the dashboard's `i` key.

        This is the ONE place ccm volunteers hiding a pane, chosen
        over a standing "this window has two claudes" hint precisely
        because a standing hint also reaches Agent Teams users, for
        whom hiding a teammate means losing its PERMIT. Deleting the
        line would leave a reader who hits the ambiguity with only
        "switch focus", which does not resolve it for good."""
        project = self._make_project(state="IDLE")
        ps = (
            "61814 12077 61814 claude\n"
            "61999 81413 61999 claude"
        )
        self._patch_resolution(monkeypatch, project, ps)
        stub, _calls = self._tmux_stub(
            "%70\t99999\t1\tzsh\n"
            "%72\t81413\t0\tzsh\n"
            "%51\t12077\t0\tzsh"
        )
        with patch("ccm_core.tmux_cmd", side_effect=stub), \
                pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "hello"])
        msg = capsys.readouterr().err
        assert "CCM_IGNORE" in msg
        assert "sidekick" in msg

    def test_ignoring_one_of_two_claude_panes_resolves_ambiguity(
        self, monkeypatch
    ):
        """The escape hatch the refusal advertises actually works.

        Same window as the refusal case, except %72 carries
        `@ccm_ignore`. It drops out of `live`, so a single claude
        pane remains and delivery resolves to it. Without this the
        advice above would be a promise the code does not keep."""
        project = self._make_project(state="IDLE")
        ps = (
            "61814 12077 61814 claude\n"
            "61999 81413 61999 claude"
        )
        self._patch_resolution(monkeypatch, project, ps)
        stub, calls = self._tmux_stub(
            "%70\t99999\t1\tzsh\t\n"
            "%72\t81413\t0\tclaude\t1\n"
            "%51\t12077\t0\tclaude\t"
        )
        with patch("ccm_core.tmux_cmd", side_effect=stub):
            ccm_send.cmd_send(["demo", "hello"])
        assert ("send-keys", "-t", "%51", "-l", "--", "hello") in calls
        assert not any(
            c[0] == "send-keys" and c[2] == "%72" for c in calls
        ), "delivered to the ignored sidekick pane"

    def test_pane_enumeration_failure_falls_back_to_window(self, monkeypatch):
        """list-panes returning nothing (tmux hiccup) must not break
        the send — fall back to the window target, matching the
        pre-resolution behaviour."""
        project = self._make_project(state="IDLE")
        self._patch_resolution(monkeypatch, project, self._PS_NO_CLAUDE)
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "hello"])
        calls = [tuple(c.args) for c in mock_tmux.call_args_list]
        assert ("send-keys", "-t", "0:5", "-l", "--", "hello") in calls


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
                win_target="0:5", win_idx="5", name="demo",
                directory="/tmp/demo", state="IDLE",
            )
        monkeypatch.setattr(ccm_core, "get_session", lambda: session)
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: project.win_idx if name == project.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [project],
        )
        # See TestCmdSend._patch_resolution — _resolve_delivery_pane
        # must not shell out to the real ps.
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
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
        with patch("ccm_core.tmux_cmd", return_value=""):
            ccm_send.cmd_send(["demo", "hello\nworld"])
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
        with patch("ccm_core.tmux_cmd", return_value=""):
            ccm_send.cmd_send(["demo", "line1\nline2"])
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
        with patch("ccm_core.tmux_cmd", return_value=""):
            ccm_send.cmd_send(["demo", payload])
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
        with patch("ccm_core.tmux_cmd", return_value=""):
            ccm_send.cmd_send(["demo", "hi"])
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
        with patch("ccm_core.tmux_cmd", return_value="") as mock_tmux:
            ccm_send.cmd_send(["demo", "hi"])
        # Specifically: the literal send-keys still fired despite
        # the trace write failures.
        calls = [tuple(c.args) for c in mock_tmux.call_args_list]
        assert ("send-keys", "-t", "0:5", "-l", "--", "hi") in calls


class TestSendSelfDeliveryGuard:
    """`ccm send` resolves delivery to the Claude-hosting pane, so a
    Claude session addressing its OWN project resolves to the pane it
    is running in. Two things go wrong there: the body would be typed
    into the caller's own composer, and the state gate would consult a
    state the caller itself is producing — a session is BUSY *because*
    it is running the command, so the gate reports BUSY and appears to
    blame the target. That self-reference was reported as
    "sending to the other agent says BUSY when it isn't". The guard
    refuses with an explanation instead of a state verdict."""

    def _patch(self, monkeypatch, delivery_pane, caller_pane):
        project = ccm_core.Project(
            win_target="0:5", win_idx="5", name="demo",
            directory="/tmp/demo", state="BUSY",
        )
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(ccm_core, "find_window",
                            lambda s, n: "5" if n == "demo" else None)
        monkeypatch.setattr(ccm_core, "build_project_list",
                            lambda fast=False: [project])
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr(ccm_send, "_resolve_delivery_pane",
                            lambda w: (delivery_pane, "claude"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        if caller_pane is None:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        else:
            monkeypatch.setenv("TMUX_PANE", caller_pane)
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *a: calls.append(a) or "")
        return calls

    def test_sending_to_own_pane_is_refused(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch, delivery_pane="%1", caller_pane="%1")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "hello"])
        err = capsys.readouterr().err
        assert "IS this pane" in err
        assert not any("-l" in c for c in calls), \
            "the body must never be typed into the caller's own composer"

    def test_refusal_explains_the_busy_self_reference(self, monkeypatch,
                                                     capsys):
        """The point of the message: a caller that sees BUSY here is
        seeing itself, not the target. Saying so is the whole value —
        otherwise the state verdict misdirects."""
        self._patch(monkeypatch, delivery_pane="%1", caller_pane="%1")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "hello"])
        err = capsys.readouterr().err
        assert "your own" in err and "BUSY" in err
        # And it points at the route that does work for a second agent.
        assert "ccm capture demo" in err

    def test_different_pane_in_same_window_still_sends(self, monkeypatch):
        """Only the caller's OWN pane is refused. A sidekick pane
        invoking `ccm send` for the Claude beside it is the supported
        path and must keep working."""
        calls = self._patch(monkeypatch, delivery_pane="%1",
                            caller_pane="%41")
        ccm_send.cmd_send(["demo", "--force", "hello"])
        assert any(c[:3] == ("send-keys", "-t", "%1") and "-l" in c
                   for c in calls)

    def test_guard_inactive_outside_tmux(self, monkeypatch):
        """No `$TMUX_PANE` (invoked outside tmux, e.g. from a script
        or MCP hook) → the guard cannot identify a caller and must not
        block anything."""
        calls = self._patch(monkeypatch, delivery_pane="%1",
                            caller_pane=None)
        ccm_send.cmd_send(["demo", "--force", "hello"])
        assert any(c[:3] == ("send-keys", "-t", "%1") and "-l" in c
                   for c in calls)


class TestSendPreTypeRecheck:
    """TOCTOU guard: the initial state gate runs on a
    `build_project_list` snapshot, and the interactive confirmation
    prompt can block for any length of time. If the target
    transitions to PERMIT while the operator reads the preview, the
    stale verdict would type the message into the permission dialog
    — breaking the "PERMIT never receives keystrokes" safety story.
    `_recheck_delivery_state` re-detects the delivery pane's raw
    state immediately before typing and aborts on danger."""

    def _pane(self, pane_id="%51", pid="12077", command="claude",
              claude=True):
        from ccm_pane_state import PaneInfo
        return PaneInfo(pane_id, pid, True, command, False,
                        int(pid) + 1 if claude else None)

    def _patch(self, monkeypatch, recheck_state, project_state="IDLE",
               interactive=False, pane_command="claude", pane_claude=True):
        """Stub resolution so the initial gate sees `project_state`,
        and the pre-type re-check sees `recheck_state`. Returns the
        list of tmux_cmd call args."""
        project = ccm_core.Project(
            win_target="0:5", win_idx="5", name="demo",
            directory="/tmp/demo", state=project_state,
        )
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: project.win_idx if name == project.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [project],
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)
        monkeypatch.setattr("sys.stdout.isatty", lambda: interactive)
        panes = ([] if recheck_state is None
                 else [self._pane(command=pane_command, claude=pane_claude)])
        monkeypatch.setattr(ccm_send, "enumerate_window_panes",
                            lambda win, ps: panes)
        monkeypatch.setattr(ccm_send, "detect_pane_state",
                            lambda *a, **k: recheck_state)
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *a: calls.append(a) or "")
        return calls

    def _literal_sent(self, calls, message="hello"):
        return any(
            c[:3] == ("send-keys", "-t", "%51") and "-l" in c
            and message in c
            for c in calls
        )

    def test_recheck_permit_after_confirmation_refused(
        self, monkeypatch, capsys,
    ):
        """The reported bug: gate sees IDLE, the operator confirms
        ("y") after the target has transitioned to PERMIT. Without
        the re-check the body would land in the dialog."""
        calls = self._patch(monkeypatch, "PERMIT", interactive=True)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])
        assert not self._literal_sent(calls), \
            "body typed into a PERMIT dialog after confirmation"
        assert "PERMIT" in capsys.readouterr().err

    def test_recheck_permit_refused_even_with_force(self, monkeypatch):
        """--force overrides BUSY, never PERMIT — also on re-check."""
        calls = self._patch(monkeypatch, "PERMIT")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--force", "--now", "hello"])
        assert not self._literal_sent(calls)

    def test_recheck_shell_refused(self, monkeypatch):
        """Claude exited between the gate and the send: the body
        would be typed into a bare shell (incident class)."""
        calls = self._patch(monkeypatch, "SHELL")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])
        assert not self._literal_sent(calls)

    def test_recheck_shell_after_start_launch_allowed(self, monkeypatch):
        """The --start exemption (did_launch): we just launched Claude
        and the wait loop confirmed IDLE a moment ago, so a raw=SHELL
        re-check reading is detection lag (MCP loading can hide the
        `❯` prompt), not a dead session — the send must proceed.
        Pairs with test_recheck_shell_refused (no launch → refuse)."""
        calls = self._patch(monkeypatch, "SHELL", project_state="SHELL",
                            pane_command="zsh", pane_claude=False)
        monkeypatch.setattr(ccm_send, "_wait_for_target_idle",
                            lambda *a, **k: "IDLE")
        ccm_send.cmd_send(["demo", "--start", "hi"])
        assert self._literal_sent(calls, "hi")

    def test_recheck_busy_refused_without_force(self, monkeypatch):
        """A target that became BUSY during confirmation gets the
        same policy as the initial gate: refuse without --force."""
        calls = self._patch(monkeypatch, "BUSY")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])
        assert not self._literal_sent(calls)

    def test_recheck_busy_sends_with_force(self, monkeypatch):
        calls = self._patch(monkeypatch, "BUSY")
        ccm_send.cmd_send(["demo", "--force", "hello"])
        assert self._literal_sent(calls)

    def test_recheck_idle_proceeds(self, monkeypatch):
        """Sanity: when the re-check agrees with the gate, the send
        goes through unchanged."""
        calls = self._patch(monkeypatch, "IDLE")
        ccm_send.cmd_send(["demo", "hello"])
        assert self._literal_sent(calls)

    def test_recheck_unresolvable_fails_open(self, monkeypatch):
        """Pane enumeration failing at re-check time (tmux hiccup)
        must not break sends that worked before the guard existed —
        matching the delivery-pane resolution fallback."""
        calls = self._patch(monkeypatch, None)
        ccm_send.cmd_send(["demo", "hello"])
        # Delivery resolution also fell back to the window target.
        assert any(
            c[:3] == ("send-keys", "-t", "0:5") and "-l" in c
            for c in calls
        )


class TestComposerDraftGuard:
    """The composer-draft guard: state detection cannot see a
    half-typed draft (raw IDLE matches `^❯\\s`, which a composer
    holding text also satisfies), so `cmd_send` reads the composer
    line itself immediately before typing and refuses while a draft
    is present. Without it, the message merges into the user's
    in-progress text and the committing Enter submits the mix.

    These tests drive the refusal through `--now`; with the default
    spool behaviour the same draft queues the message instead
    (covered in tests/test_spool.py)."""

    def _patch(self, monkeypatch, capture, project_state="IDLE"):
        """Stub the gate to `project_state` and every capture-pane
        read to `capture` (a string, or a callable taking the tmux
        args for alt-screen variants). Returns the tmux call list."""
        project = ccm_core.Project(
            win_target="0:5", win_idx="5", name="demo",
            directory="/tmp/demo", state=project_state,
        )
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: project.win_idx if name == project.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [project],
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        # The pre-type re-check agrees with the gate; the guard under
        # test is the composer read, not the state re-check.
        monkeypatch.setattr(ccm_send, "_recheck_delivery_state",
                            lambda *a: project_state)
        calls = []

        def tmux(*args):
            calls.append(args)
            if args[0] == "capture-pane":
                return capture(args) if callable(capture) else capture
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", tmux)
        return calls

    @staticmethod
    def _literal_sent(calls):
        return any("-l" in c for c in calls if c[0] == "send-keys")

    def test_doubled_glyph_mode_line_is_not_a_draft(self):
        """Older builds render the accept-edits mode as `❯❯ …` at line
        start. The prompt glyph doubled is a mode line, not a draft —
        the composer always puts a space after its single glyph."""
        import ccm_constants
        assert not ccm_constants.PATTERN_COMPOSER_DRAFT.match(
            "❯❯ accept edits on")
        assert ccm_constants.PATTERN_COMPOSER_DRAFT.match("❯ a draft")

    def test_draft_refuses_and_never_types(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch, "❯ half-typed thought\n")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])
        assert not self._literal_sent(calls), \
            "body typed over a half-typed draft"
        err = capsys.readouterr().err
        # The refusal names what is sitting in the composer, so the
        # operator can tell whose draft it is.
        assert "half-typed thought" in err

    def test_bare_composer_proceeds(self, monkeypatch):
        """The normal IDLE screen: bare `❯` prompt, status line below.
        No draft → the send proceeds."""
        calls = self._patch(
            monkeypatch,
            "  ⎿  Tip: example tip line\n"
            "❯ \n"
            "  /tmp/demo  main  ·  ctx 42%\n",
        )
        ccm_send.cmd_send(["demo", "--now", "hello"])
        assert self._literal_sent(calls)

    def test_unreadable_composer_fails_open(self, monkeypatch):
        """An empty capture (tmux hiccup) must not break sends that
        worked before this guard existed — the same fail-open call
        `_recheck_delivery_state` makes. Pinned so a future
        tightening is a deliberate choice, not a side effect."""
        calls = self._patch(monkeypatch, "")
        ccm_send.cmd_send(["demo", "--now", "hello"])
        assert self._literal_sent(calls)

    def test_draft_found_via_alternate_screen(self, monkeypatch):
        """When the normal capture comes back empty the guard retries
        against the alternate screen before giving up — a draft
        visible only there must still refuse."""
        def capture(args):
            if "-a" in args:
                return "❯ draft on the alt screen\n"
            return ""
        calls = self._patch(monkeypatch, capture)
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])
        assert not self._literal_sent(calls)

    def test_multiline_draft_detected_from_first_row(self, monkeypatch,
                                                     capsys):
        """A multi-line draft carries the `❯` prompt on its first
        row; that row alone must trigger the refusal."""
        calls = self._patch(monkeypatch,
                            "❯ first line of a draft\n  continuation\n")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])
        assert not self._literal_sent(calls)
        assert "first line of a draft" in capsys.readouterr().err

    def test_draft_refused_even_with_force_on_busy(self, monkeypatch,
                                                   capsys):
        """`--force` licenses queueing into a BUSY turn — it does not
        license merging into the user's draft. Uniform refusal."""
        calls = self._patch(monkeypatch, "❯ do not touch this\n",
                            project_state="BUSY")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--force", "--now", "hello"])
        assert not self._literal_sent(calls)
        # The guard read the composer AFTER the defensive mode-cancel,
        # so the capture reflects the live composer, not copy-mode
        # scrollback.
        cancel_i = next(i for i, c in enumerate(calls)
                        if c == ("send-keys", "-t", "0:5", "-X", "cancel"))
        capture_i = next(i for i, c in enumerate(calls)
                         if c[0] == "capture-pane")
        assert cancel_i < capture_i

    def test_fragment_is_capped_in_the_refusal(self, monkeypatch, capsys):
        """A long draft is quoted capped, so the refusal stays a
        readable one-liner instead of flooding the terminal."""
        long_draft = "❯ " + "x" * 120
        calls = self._patch(monkeypatch, long_draft + "\n")
        with pytest.raises(SystemExit):
            ccm_send.cmd_send(["demo", "--now", "hello"])
        assert not self._literal_sent(calls)
        err = capsys.readouterr().err
        # The cap is 60 chars of the whole stripped line, so the
        # `❯ ` prefix leaves room for 58 x's plus the ellipsis.
        assert "x" * 58 in err and "x" * 59 not in err
        assert "..." in err
