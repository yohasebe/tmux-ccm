"""Tests for `ccm sidekick-send` — delivery to the external-agent
sidekick pane of the caller's own window.

The command's safety story is identity, not state: ccm tracks no
state for a non-Claude pane, so the target is found from tmux
metadata (foreground command + working directory), every ambiguity
refuses, and delivery is confirmed by capture."""

import pytest

import ccm_core
import ccm_send

_MSG = "review the draft please"  # long enough for a verify signature
_CALLER = "@1\tclaude\t/tmp/proj"  # window_id, command, cwd
_KIMI = ("%2", "kimi", "/tmp/proj")


class TestSidekickSend:
    def _stub(self, monkeypatch, caller_info=_CALLER, panes=(_KIMI,),
              ccm_dir="/tmp/proj", capture=""):
        """Install tmux_cmd / TMUX_PANE / time.sleep stubs and return
        the shared call list (time.sleep is recorded into it as
        ("sleep", seconds) so key/pause ordering is assertable).

        `panes` may be a list of (pane_id, command, cwd) tuples — or
        a list of such lists to make successive list-panes calls
        change (TOCTOU re-resolution tests). `capture` is a string or
        a callable taking the tmux args."""
        monkeypatch.setenv("TMUX_PANE", "%1")
        panes_seq = panes if panes and isinstance(panes[0], list) else [panes]
        pane_calls = [0]
        calls = []

        def tmux(*args):
            calls.append(args)
            cmd = args[0]
            if cmd == "display-message":
                return caller_info
            if cmd == "list-panes":
                i = min(pane_calls[0], len(panes_seq) - 1)
                pane_calls[0] += 1
                return "\n".join("\t".join(p) for p in panes_seq[i])
            if cmd == "show-option":
                return ccm_dir
            if cmd == "capture-pane":
                return capture(args) if callable(capture) else capture
            return ""

        monkeypatch.setattr(ccm_core, "tmux_cmd", tmux)
        monkeypatch.setattr(ccm_send.time, "sleep",
                            lambda s: calls.append(("sleep", s)))
        return calls

    @staticmethod
    def _keys(calls, *tail):
        return [c for c in calls
                if c[0] == "send-keys" and c[3:] == tail]

    # --- happy path ---

    def test_happy_path_sequence(self, monkeypatch, capsys):
        """cancel → literal body → 0.3 s settle → Enter → capture to
        confirm, in that order, all against the sidekick pane."""
        calls = self._stub(monkeypatch, capture=f"some output\n{_MSG}\n")
        ccm_send.cmd_sidekick_send([_MSG])
        seq = [c for c in calls
               if c[0] in ("send-keys", "sleep", "capture-pane")]
        cancel = ("send-keys", "-t", "%2", "-X", "cancel")
        body = ("send-keys", "-t", "%2", "-l", "--", _MSG)
        enter = ("send-keys", "-t", "%2", "Enter")
        assert cancel in seq and body in seq and enter in seq
        assert ("sleep", 0.3) in seq
        assert (seq.index(cancel) < seq.index(body)
                < seq.index(("sleep", 0.3)) < seq.index(enter))
        # The confirmation capture happens after the Enter.
        assert seq[-1][0] == "capture-pane"
        assert "Sent to sidekick kimi (%2)" in capsys.readouterr().out

    def test_multiline_uses_m_enter_between_lines(self, monkeypatch):
        body = "first line of the brief\nsecond line of the brief"
        calls = self._stub(monkeypatch, capture=body)
        ccm_send.cmd_sidekick_send([body])
        assert ("send-keys", "-t", "%2", "-l", "--",
                "first line of the brief") in calls
        assert ("send-keys", "-t", "%2", "M-Enter") in calls
        assert ("send-keys", "-t", "%2", "-l", "--",
                "second line of the brief") in calls

    def test_no_enter_skips_submit_and_settle(self, monkeypatch):
        """--no-enter types the body only: no settle pause, no Enter,
        and the confirmation still runs (the body sits in the
        composer, so its fragment is visible)."""
        calls = self._stub(monkeypatch, capture=f"❯ {_MSG}\n")
        ccm_send.cmd_sidekick_send(["--no-enter", _MSG])
        assert self._keys(calls, "-l", "--", _MSG)
        assert not self._keys(calls, "Enter")
        assert ("sleep", 0.3) not in calls

    def test_short_message_skips_verification(self, monkeypatch, capsys):
        """A message too short for a reliable signature is sent
        without the capture check, and the output says so."""
        calls = self._stub(monkeypatch)
        ccm_send.cmd_sidekick_send(["hi"])
        assert self._keys(calls, "Enter")
        assert not [c for c in calls if c[0] == "capture-pane"]
        assert "too short" in capsys.readouterr().out

    def test_dash_message_via_double_dash(self, monkeypatch):
        calls = self._stub(monkeypatch, capture="-dashy message here\n")
        ccm_send.cmd_sidekick_send(["--", "-dashy message here"])
        assert self._keys(calls, "-l", "--", "-dashy message here")

    # --- argument validation ---

    def test_no_message_refused(self, monkeypatch):
        self._stub(monkeypatch)
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([])

    def test_empty_message_refused(self, monkeypatch):
        self._stub(monkeypatch)
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send(["   "])

    def test_conflicting_sources_refused(self, monkeypatch):
        self._stub(monkeypatch)
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG, "--file", "/tmp/x.md"])

    def test_unknown_flag_refused(self, monkeypatch, capsys):
        self._stub(monkeypatch)
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send(["--bogus", _MSG])
        assert "Unknown flag" in capsys.readouterr().err

    def test_help_needs_no_tmux(self, monkeypatch, capsys):
        """--help must work outside tmux (no TMUX_PANE, no tmux at
        all) — it is where a user whose message was eaten as flags
        lands."""
        monkeypatch.delenv("TMUX_PANE", raising=False)
        ccm_send.cmd_sidekick_send(["--help"])
        assert "Usage: ccm sidekick-send" in capsys.readouterr().out

    # --- identity: caller ---

    def test_outside_tmux_refused(self, monkeypatch):
        """No $TMUX_PANE → the caller's window cannot be known, so
        there is nothing to scope the search to. Fail closed, and
        touch no tmux state before that verdict."""
        monkeypatch.delenv("TMUX_PANE", raising=False)
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *a: calls.append(a) or "")
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert not calls, "tmux touched before the caller is known"

    def test_reverse_lane_refused(self, monkeypatch, capsys):
        """The caller IS the sidekick: the reverse lane is `ccm send
        <project>`, not this command."""
        calls = self._stub(monkeypatch, caller_info="@1\tkimi\t/tmp/proj")
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert not self._keys(calls, "-l", "--", _MSG)
        err = capsys.readouterr().err
        assert "reverse lane" in err and "ccm send <project>" in err

    # --- identity: target pane ---

    def test_no_sidekick_refused(self, monkeypatch, capsys):
        self._stub(monkeypatch, panes=(("%2", "zsh", "/tmp/proj"),))
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert "No sidekick pane" in capsys.readouterr().err

    def test_two_sidekicks_refused_as_ambiguous(self, monkeypatch, capsys):
        calls = self._stub(monkeypatch,
                           panes=(_KIMI, ("%3", "grok", "/tmp/proj")))
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert not self._keys(calls, "-l", "--", _MSG)
        err = capsys.readouterr().err
        assert "ambiguous" in err and "%2" in err and "%3" in err

    def test_cwd_outside_project_refused(self, monkeypatch, capsys):
        """The wrong-window mis-send this command exists to prevent:
        the pane runs an agent, but its directory is not this
        project's."""
        calls = self._stub(monkeypatch,
                           panes=(("%2", "kimi", "/elsewhere"),))
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert not self._keys(calls, "-l", "--", _MSG)
        assert "outside" in capsys.readouterr().err

    def test_cwd_subdirectory_allowed(self, monkeypatch):
        """A sidekick started in a subdirectory of the project is
        still this project's."""
        calls = self._stub(monkeypatch,
                           panes=(("%2", "kimi", "/tmp/proj/sub"),),
                           capture=_MSG)
        ccm_send.cmd_sidekick_send([_MSG])
        assert self._keys(calls, "-l", "--", _MSG)

    def test_cwd_reference_falls_back_to_caller(self, monkeypatch):
        """Untagged window (no @ccm_dir): the caller pane's own cwd
        is the project reference."""
        calls = self._stub(monkeypatch, ccm_dir="", capture=_MSG)
        ccm_send.cmd_sidekick_send([_MSG])
        assert self._keys(calls, "-l", "--", _MSG)

    # --- TOCTOU ---

    def test_toctou_pane_changed_refused(self, monkeypatch, capsys):
        """Re-resolution immediately before typing finds a DIFFERENT
        pane (the original exited, a new one appeared) — refuse
        rather than type into the successor."""
        calls = self._stub(monkeypatch,
                           panes=[[_KIMI], [("%3", "kimi", "/tmp/proj")]])
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert not self._keys(calls, "-l", "--", _MSG)
        assert "changed" in capsys.readouterr().err

    def test_toctou_pane_vanished_refused(self, monkeypatch):
        calls = self._stub(monkeypatch, panes=[[_KIMI], []])
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert not self._keys(calls, "-l", "--", _MSG)

    # --- delivery confirmation ---

    def test_delivery_unconfirmed_dies(self, monkeypatch, capsys):
        """No fragment ever appears: the send may have been eaten, so
        the command must fail loudly instead of printing Sent."""
        monkeypatch.setattr(ccm_send, "_SIDEKICK_VERIFY_TIMEOUT_SEC", 0.2)
        monkeypatch.setattr(ccm_send, "_SIDEKICK_VERIFY_POLL_SEC", 0.05)
        self._stub(monkeypatch, capture="")
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        err = capsys.readouterr().err
        assert "could not be confirmed" in err and "ccm capture" in err

    def test_delivery_confirmation_polls_until_visible(self, monkeypatch):
        """The pane may need a moment to echo the submitted message;
        the confirmation polls instead of reading exactly once."""
        reads = [0]

        def capture(_args):
            reads[0] += 1
            return "" if reads[0] < 3 else _MSG + "\n"

        self._stub(monkeypatch, capture=capture)
        ccm_send.cmd_sidekick_send([_MSG])  # must NOT raise
        assert reads[0] >= 3
