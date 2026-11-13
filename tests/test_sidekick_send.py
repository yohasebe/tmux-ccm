"""Tests for `ccm sidekick-send` — delivery to the external-agent
sidekick pane of the caller's own window.

The command's safety story is identity first: the target is found
from tmux metadata (foreground command + working directory), every
ambiguity refuses, and delivery is confirmed by capture. Where ccm
reads a sidekick's screen (Codex, Antigravity), a dialog waiting for
a key refuses too."""

import pytest

import ccm_core
import ccm_send

_MSG = "review the draft please"  # long enough for a verify signature
_READ_MARKERS = ccm_core._read_attention_markers  # before any stub
_CALLER = "@1\tclaude\t/example/proj"  # window_id, command, cwd
_KIMI = ("%2", "kimi", "/example/proj")


class TestSidekickSend:
    def _stub(self, monkeypatch, caller_info=_CALLER, panes=(_KIMI,),
              ccm_dir="/example/proj", capture="", markers=None,
              identities=None, failing_keys=(), server="server-example"):
        """Install tmux_cmd / tmux_query / TMUX_PANE / time.sleep stubs
        and return the shared call list (time.sleep is recorded into it
        as ("sleep", seconds) so key/pause ordering is assertable).

        `panes` may be a list of (pane_id, command, cwd) tuples — or
        a list of such lists to make successive list-panes calls
        change (TOCTOU re-resolution tests). `capture` is a string or
        a callable taking the tmux args. `markers` is a list of marker
        dicts by look (the sidekick is looked at before the body and
        before Enter), `identities` the sidekick's process identity by
        look, and `failing_keys` the checked send-keys (by their last
        key) that tmux does not confirm. `server` is what tmux says
        when asked which server it is (None: it cannot be asked)."""
        monkeypatch.setenv("TMUX_PANE", "%1")
        monkeypatch.setattr(ccm_core, "caller_context", lambda **kw: ("demo", "%1"))
        panes_seq = panes if panes and isinstance(panes[0], list) else [panes]
        pane_calls = [0]
        calls = []
        looks = {"cache": 0, "identity": 0}

        def panes_cache():
            # What the latest list-panes of the caller's window showed.
            i = min(max(pane_calls[0] - 1, 0), len(panes_seq) - 1)
            looks["cache"] += 1
            return [("s:1", "1" + pid[1:], pid, cmd, "0", "40", "", "")
                    for pid, cmd, _ in panes_seq[i]]

        def read_markers(cache, ps_lines, strict=False):
            seq = markers or [{}]
            return seq[min(looks["cache"] - 1, len(seq) - 1)]

        def identity(pane_pid, agent, lines):
            seq = identities or [[["1", "birth"]]]
            looks["identity"] += 1
            return seq[min(looks["identity"] - 1, len(seq) - 1)]

        def query(*args, **kw):
            calls.append(args)
            if args[0] == "send-keys" and args[-1] in failing_keys:
                return None
            if args[0] == "display-message":
                return server
            return ""

        import ccm_sidekick_notify
        monkeypatch.setattr(ccm_core, "_build_panes_cache", panes_cache)
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr(ccm_core, "_read_attention_markers", read_markers)
        monkeypatch.setattr(ccm_sidekick_notify, "agent_process_identity", identity)
        monkeypatch.setattr(ccm_core, "tmux_query", query)

        def tmux(*args, **kw):
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
        clock = [ccm_send.time.time()]

        def sleep(seconds):
            calls.append(("sleep", seconds))
            clock[0] += seconds
        monkeypatch.setattr(ccm_send.time, "sleep", sleep)
        monkeypatch.setattr(ccm_send.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(ccm_send.time, "time", lambda: clock[0])
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
        assert seq.index(cancel) < seq.index(body) < seq.index(enter)
        settle = [c[1] for c in seq[seq.index(body):seq.index(enter)]
                  if c[0] == "sleep"]
        assert sum(settle) == pytest.approx(0.3)
        # The confirmation capture happens after the Enter.
        assert seq[-1][0] == "capture-pane"
        out = capsys.readouterr().out
        assert "seen on screen" in out and "receipt not confirmed" in out
        assert "Enter attempted" in out and "Sent to" not in out

    # --- a sidekick waiting on a dialog ---

    _CODEX_DIALOG = ("  Would you like to run the following command?\n"
                     "› 1. Yes, proceed (y)\n"
                     "  3. No, and tell Codex what to do differently (esc)\n"
                     "  Press enter to confirm or esc to cancel\n")
    _AGY_MENU = ("─" * 80 + "\nRun this command?\n> 1. Yes, run command\n"
                 "  2. No, cancel\n  ↑/↓ Navigate · tab Amend\n"
                 "esc to cancel        Example Model\n")
    _CODEX_IDLE = "› Ask Codex to do anything\n  ? for shortcuts\n"
    _CODEX_SELECTION = ("Working (5s • esc to interrupt)\n"
                        "› Ask Codex to do anything\n"
                        "  Example Model · ~/example\n"
                        "  New activity · ⌃c copy · enter copy & follow · esc clear\n")
    _CODEX_WORKING = ("Working (5s • esc to interrupt)\n"
                      "› Ask Codex to do anything\n  ? for shortcuts\n")

    def _screen_stub(self, monkeypatch, tmp_path, agent, first_screen):
        """A sidekick `agent` in a temporary project whose first capture
        is `first_screen` and later ones show the message."""
        proj = str(tmp_path)
        screens = iter([first_screen])

        def capture(args):
            screen = next(screens, _MSG)
            if isinstance(screen, Exception):
                raise screen
            return screen
        return self._stub(monkeypatch, caller_info=f"@1\tclaude\t{proj}",
                          panes=(("%2", agent, proj),), ccm_dir=proj,
                          capture=capture)

    @pytest.mark.parametrize("agent, screen", [
        ("codex", _CODEX_DIALOG), ("agy", _AGY_MENU)])
    @pytest.mark.parametrize("flags", [[], ["--no-enter"]])
    def test_waiting_sidekick_refused_before_any_key(
            self, monkeypatch, capsys, tmp_path, agent, screen, flags):
        calls = self._screen_stub(monkeypatch, tmp_path, agent, screen)
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send(flags + [_MSG])
        assert [c for c in calls if c[0] == "send-keys"] == []
        assert "waiting on a dialog" in capsys.readouterr().err

    @pytest.mark.parametrize("flags", [[], ["--no-enter"]])
    def test_view_taking_keys_refused_before_any_key(
            self, monkeypatch, capsys, tmp_path, flags):
        """A working Codex with text selected reads BUSY, but the keys
        typed would go to the selection, so nothing is typed."""
        calls = self._screen_stub(monkeypatch, tmp_path, "codex",
                                  self._CODEX_SELECTION)
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send(flags + [_MSG])
        assert [c for c in calls if c[0] == "send-keys"] == []
        err = capsys.readouterr().err
        assert "view that takes keys" in err
        assert "waiting on a dialog" not in err

    @pytest.mark.parametrize("agent, screen", [
        ("codex", _CODEX_IDLE),
        ("codex", _CODEX_WORKING),
        ("agy", "an overlay ccm cannot read\n"),
        ("codex", RuntimeError("capture failed")),
    ])
    def test_other_screens_are_sent_to(self, monkeypatch, tmp_path, agent,
                                       screen):
        calls = self._screen_stub(monkeypatch, tmp_path, agent, screen)
        ccm_send.cmd_sidekick_send([_MSG])
        assert ("send-keys", "-t", "%2", "Enter") in calls

    def test_multiline_uses_m_enter_between_lines(self, monkeypatch):
        body = "first line of the brief\nsecond line of the brief"
        calls = self._stub(monkeypatch, capture=body)
        ccm_send.cmd_sidekick_send([body])
        assert ("send-keys", "-t", "%2", "-l", "--",
                "first line of the brief") in calls
        assert ("send-keys", "-t", "%2", "M-Enter") in calls
        assert ("send-keys", "-t", "%2", "-l", "--",
                "second line of the brief") in calls

    def test_no_enter_skips_submit_and_settle(self, monkeypatch, capsys):
        """--no-enter types the body only: no settle pause, no Enter,
        and the confirmation still runs (the body sits in the
        composer, so its fragment is visible)."""
        calls = self._stub(monkeypatch, capture=f"❯ {_MSG}\n")
        ccm_send.cmd_sidekick_send(["--no-enter", _MSG])
        assert self._keys(calls, "-l", "--", _MSG)
        assert not self._keys(calls, "Enter")
        assert ("sleep", 0.3) not in calls
        out = capsys.readouterr().out
        assert "Enter not sent" in out and "receipt not confirmed" in out
        assert "Sent to" not in out

    @pytest.mark.parametrize("no_enter", [False, True])
    def test_short_message_skips_verification(self, monkeypatch, capsys, no_enter):
        """A message too short for a reliable signature is sent
        without the capture check, and the output says so."""
        calls = self._stub(monkeypatch)
        ccm_send.cmd_sidekick_send((["--no-enter"] if no_enter else []) + ["hi"])
        assert bool(self._keys(calls, "Enter")) is not no_enter
        assert not [c for c in calls if c[0] == "capture-pane"]
        out = capsys.readouterr().out
        assert "Delivery unconfirmed" in out and "ccm capture" in out
        assert ("Enter not sent" if no_enter else "Enter attempted") in out
        assert "Sent to" not in out

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
            ccm_send.cmd_sidekick_send([_MSG, "--file", "message.md"])

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
        calls = self._stub(monkeypatch, caller_info="@1\tkimi\t/example/proj")
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert not self._keys(calls, "-l", "--", _MSG)
        err = capsys.readouterr().err
        assert "reverse lane" in err and "ccm send <project>" in err

    # --- identity: target pane ---

    def test_no_sidekick_refused(self, monkeypatch, capsys):
        self._stub(monkeypatch, panes=(("%2", "zsh", "/example/proj"),))
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        assert "No sidekick pane" in capsys.readouterr().err

    def test_two_sidekicks_refused_as_ambiguous(self, monkeypatch, capsys):
        calls = self._stub(monkeypatch,
                           panes=(_KIMI, ("%3", "grok", "/example/proj")))
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
                           panes=(("%2", "kimi", "/example/proj/sub"),),
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
                           panes=[[_KIMI], [("%3", "kimi", "/example/proj")]])
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
        # Drive the deadline from a controlled clock: each read
        # advances it past one poll, so the wait ends after a few
        # iterations instead of spinning on the real clock.
        clock = [1000.0]

        def tick():
            clock[0] += 0.06
            return clock[0]
        monkeypatch.setattr(ccm_send.time, "time", tick)
        self._stub(monkeypatch, capture="")
        with pytest.raises(SystemExit):
            ccm_send.cmd_sidekick_send([_MSG])
        err = capsys.readouterr().err
        assert "could not be confirmed" in err and "ccm capture" in err
        assert "do not resend if received" in err and "leftover text" in err
        assert "then resend" not in err

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


class TestSendResult:
    """One result per run: refused before any key, or attempted_* with
    what is known after; `--json` prints it, the text output words it."""

    _WAITING = {"agent": "kimi", "state": "waiting"}

    @pytest.fixture
    def stub(self, monkeypatch):
        return lambda **kw: TestSidekickSend._stub(
            TestSidekickSend(), monkeypatch, **{"capture": f"{_MSG}\n", **kw})

    @staticmethod
    def run_json(args, capsys):
        """(the JSON object printed, the exit code, stderr); a run that
        ends without exactly one object fails the test."""
        import json
        try:
            ccm_send.cmd_sidekick_send(["--json"] + args)
            code = None
        except SystemExit as exit_:
            code = exit_.code
        except Exception as exc:  # the object is owed whatever happens
            code = repr(exc)
        captured = capsys.readouterr()
        assert captured.out.count("\n") == 1, (code, captured.out)
        return json.loads(captured.out), code, captured.err

    @staticmethod
    def keys(calls):
        return [c[3:] for c in calls if c[0] == "send-keys" and c[3:] != ("-X", "cancel")]

    @pytest.mark.parametrize("state, keys, outcome", [
        ("IDLE", "free", "attempted_seen"),
        ("BUSY", "free", "attempted_seen"),
        ("IDLE", None, "attempted_seen"),
        (None, None, "attempted_seen"),
        ("PERMIT", "held", "refused"),
        ("PERMIT", None, "refused"),
        ("BUSY", "held", "refused"),
        (None, "held", "refused"),
    ])
    def test_send_condition(self, stub, monkeypatch, capsys, state, keys, outcome):
        calls = stub()
        monkeypatch.setattr(ccm_send, "_observe_sidekick", lambda pane, agent: (
            ccm_core.SidekickObservation(agent, pane, state, None, keys), [["1", "b"]]))
        out, code, _ = self.run_json([_MSG], capsys)
        assert out["outcome"] == outcome
        assert (code == 0) is (outcome == "attempted_seen")
        if outcome == "refused":
            assert out["reason"] == ("dialog" if state == "PERMIT" else "view")
            assert self.keys(calls) == []

    def test_seen(self, stub, capsys):
        stub()
        out, code, _ = self.run_json([_MSG], capsys)
        assert code == 0
        assert out == {"schema": 1, "outcome": "attempted_seen", "reason": None,
                       "agent": "kimi", "pane": "%2", "enter_requested": True,
                       "enter_sent": True, "message": out["message"]}

    def test_marker_before_the_body_refuses(self, stub, capsys):
        calls = stub(markers=[{"%2": self._WAITING}])
        out, code, err = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], code) == ("refused", "dialog", 1)
        assert self.keys(calls) == []
        assert "waiting on a dialog" in err

    def test_codex_hook_wait_refuses_while_the_screen_is_unreadable(
            self, stub, monkeypatch, capsys):
        calls = stub(panes=(("%2", "codex", "/example/proj"),))
        monkeypatch.setattr(ccm_core, "_codex_hook_state", lambda *a: "PERMIT")
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"]) == ("refused", "dialog")
        assert self.keys(calls) == []

    def test_wait_seen_only_before_enter_stops_the_send(self, stub, capsys):
        calls = stub(markers=[{}, {"%2": self._WAITING}])
        out, code, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], code) == ("attempted_stopped", "dialog", 1)
        assert out["enter_sent"] is False
        assert self.keys(calls) == [("-l", "--", _MSG)]

    @pytest.mark.parametrize("identities", [
        [[["1", "birth-A"]], [["1", "birth-B"]]],   # restarted in the same pane
        [[["1", "birth-A"]], None],                 # cannot be told any more
    ])
    def test_another_process_before_enter_stops_the_send(self, stub, capsys, identities):
        calls = stub(identities=identities)
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"]) == ("attempted_stopped", "pane_changed")
        assert self.keys(calls) == [("-l", "--", _MSG)]

    def test_pane_gone_before_enter_stops_the_send(self, stub, monkeypatch, capsys):
        calls = stub()
        looks = []

        def observe(pane, agent):
            looks.append(1)
            if len(looks) == 1:
                return ccm_core.SidekickObservation(agent, pane, None, None, None), [["1", "b"]]
            return None, None
        monkeypatch.setattr(ccm_send, "_observe_sidekick", observe)
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"]) == ("attempted_stopped", "pane_changed")
        assert self.keys(calls) == [("-l", "--", _MSG)]

    def test_another_program_in_the_pane_before_enter_stops_the_send(
            self, stub, monkeypatch, capsys):
        calls = stub()
        listings = iter([[("s:1", "12", "%2", "kimi", "0", "40", "", "")],
                         [("s:1", "12", "%2", "zsh", "0", "40", "", "")]])
        monkeypatch.setattr(ccm_core, "_build_panes_cache", lambda: next(listings))
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"]) == ("attempted_stopped", "pane_changed")
        assert self.keys(calls) == [("-l", "--", _MSG)]

    @pytest.mark.parametrize("third", [
        [("%3", "kimi", "/example/proj")],                                # %2 moved away
        [("%2", "kimi", "/example/proj"), ("%3", "codex", "/example/proj")],  # a newcomer
        [],                                                               # gone
    ])
    def test_no_longer_the_windows_sidekick_before_enter_stops_the_send(
            self, stub, capsys, third):
        calls = stub(panes=[[_KIMI], [_KIMI], third])
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], out["enter_sent"]) == (
            "attempted_stopped", "pane_changed", False)
        assert self.keys(calls) == [("-l", "--", _MSG)]

    @pytest.mark.parametrize("source", ["file", "stdin"])
    def test_nul_is_refused_before_any_key(self, stub, monkeypatch, capsys, tmp_path, source):
        calls = stub()
        text = "first line\nsecond\x00line"
        if source == "file":
            path = tmp_path / "message.md"
            path.write_text(text)
            args = ["--file", str(path)]
        else:
            import io
            monkeypatch.setattr(ccm_send.sys, "stdin", io.StringIO(text))
            args = ["--stdin"]
        out, code, _ = self.run_json(args, capsys)
        assert (out["outcome"], out["reason"], code) == ("refused", "unsupported_text", 1)
        assert [c for c in calls if c[0] == "send-keys"] == []

    @pytest.mark.parametrize("char", ["\r", "\x1b", "\x03", "\x15"])
    def test_control_characters_are_refused_before_any_key(
            self, stub, monkeypatch, capsys, char):
        """Each would be typed as a key: `\\r` submits, ESC interrupts,
        `^C` quits, `^U` clears the composer."""
        import io
        calls = stub()
        monkeypatch.setattr(ccm_send.sys, "stdin",
                            io.StringIO(f"first line\nsecond{char}line"))
        out, code, _ = self.run_json(["--stdin"], capsys)
        assert (out["outcome"], out["reason"], code) == ("refused", "unsupported_text", 1)
        assert [c for c in calls if c[0] == "send-keys"] == []

    def test_a_lone_carriage_return_in_a_message_file_is_refused(
            self, stub, capsys, tmp_path):
        calls = stub()
        path = tmp_path / "body.txt"
        path.write_bytes(b"first\rsecond")
        out, code, _ = self.run_json(["--file", str(path)], capsys)
        assert (out["outcome"], out["reason"], code) == ("refused", "unsupported_text", 1)
        assert [c for c in calls if c[0] == "send-keys"] == []

    @pytest.mark.parametrize("breaks_on, outcome", [
        (1, "refused"),             # before the body: no key sent
        (2, "attempted_stopped"),   # before Enter: Enter not sent
    ])
    def test_a_look_that_breaks_stops_the_send(self, stub, monkeypatch, capsys,
                                               breaks_on, outcome):
        calls = stub()
        looks = []

        def markers(cache, ps_lines, strict=False):
            looks.append(1)
            if len(looks) == breaks_on:
                raise RuntimeError("marker directory unreadable")
            return {}
        monkeypatch.setattr(ccm_core, "_read_attention_markers", markers)
        out, code, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], out["enter_sent"], code) == (
            outcome, "observation_failed", False, 1)
        assert self.keys(calls) == ([] if breaks_on == 1 else [("-l", "--", _MSG)])

    @pytest.mark.parametrize("breaks_on, outcome", [
        (1, "refused"), (2, "attempted_stopped")])
    def test_an_unreadable_marker_directory_stops_the_send(
            self, stub, monkeypatch, capsys, tmp_path, breaks_on, outcome):
        import os
        if os.geteuid() == 0:
            pytest.skip("root reads any directory")
        calls = stub()
        markers = tmp_path / "attention"; markers.mkdir()
        monkeypatch.setattr(ccm_core, "CCM_ATTENTION_DIR", str(markers))
        looks = []

        def read(cache, ps_lines, strict=False):
            looks.append(1)
            if len(looks) == breaks_on:
                markers.chmod(0)
            return _READ_MARKERS(cache, ps_lines, strict=strict)
        monkeypatch.setattr(ccm_core, "_read_attention_markers", read)
        try:
            out, _, _ = self.run_json([_MSG], capsys)
        finally:
            markers.chmod(0o700)
        assert (out["outcome"], out["reason"], out["enter_sent"]) == (
            outcome, "observation_failed", False)
        assert self.keys(calls) == ([] if breaks_on == 1 else [("-l", "--", _MSG)])

    @pytest.mark.parametrize("breaks_on, outcome", [
        (1, "refused"), (2, "attempted_stopped")])
    def test_a_failing_codex_hook_read_stops_the_send(
            self, stub, monkeypatch, capsys, breaks_on, outcome):
        import ccm_sidekick_notify
        # A Codex screen with none of its marks: the hook record decides.
        calls = stub(panes=(("%2", "codex", "/example/proj"),))
        reads = []

        def activity(server, now=None, strict=False):
            reads.append(1)
            if len(reads) == breaks_on:
                raise RuntimeError("hook records unreadable")
            return {}
        monkeypatch.setattr(ccm_sidekick_notify, "pane_activity", activity)
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], out["enter_sent"]) == (
            outcome, "observation_failed", False)
        assert self.keys(calls) == ([] if breaks_on == 1 else [("-l", "--", _MSG)])

    @pytest.mark.parametrize("breaks_on, outcome", [
        (1, "refused"), (2, "attempted_stopped")])
    def test_an_unreadable_codex_hook_record_stops_the_send(
            self, stub, monkeypatch, capsys, tmp_path, breaks_on, outcome):
        import json
        import os
        import time as real_time
        import ccm_sidekick_notify
        import ccm_spool
        if os.geteuid() == 0:
            pytest.skip("root reads any file")
        # A Codex screen with none of its marks: the hook record decides.
        calls = stub(panes=(("%2", "codex", "/example/proj"),))
        monkeypatch.setattr(ccm_spool, "SPOOL_ROOT", str(tmp_path))
        records = tmp_path / ".sidekick"; records.mkdir()
        record = records / "window.json"
        record.write_text(json.dumps({
            "binding": {"server": "server-example", "source": "%2", "process": [["1", "birth"]]},
            "received": real_time.time(), "open_turn": True}))
        real = ccm_sidekick_notify.pane_activity
        reads = []

        def activity(server, now=None, strict=False):
            reads.append(1)
            if len(reads) == breaks_on:
                record.chmod(0)
            return real(server, now=now, strict=strict)
        monkeypatch.setattr(ccm_sidekick_notify, "pane_activity", activity)
        try:
            out, _, _ = self.run_json([_MSG], capsys)
        finally:
            record.chmod(0o600)
        assert (out["outcome"], out["reason"], out["enter_sent"]) == (
            outcome, "observation_failed", False)
        assert self.keys(calls) == ([] if breaks_on == 1 else [("-l", "--", _MSG)])

    def test_a_readable_codex_hook_wait_refuses(self, stub, monkeypatch, capsys, tmp_path):
        import json
        import time as real_time
        import ccm_spool
        calls = stub(panes=(("%2", "codex", "/example/proj"),))
        monkeypatch.setattr(ccm_spool, "SPOOL_ROOT", str(tmp_path))
        records = tmp_path / ".sidekick"; records.mkdir()
        (records / "window.json").write_text(json.dumps({
            "binding": {"server": "server-example", "source": "%2", "process": [["1", "birth"]]},
            "received": real_time.time(), "waits": {"call": {}}}))
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"]) == ("refused", "dialog")
        assert self.keys(calls) == []

    @staticmethod
    def codex_record(monkeypatch, tmp_path, **record):
        """A Codex hook record for %2 on the stub's tmux server."""
        import json
        import time as real_time
        import ccm_spool
        monkeypatch.setattr(ccm_spool, "SPOOL_ROOT", str(tmp_path))
        records = tmp_path / ".sidekick"; records.mkdir()
        (records / "window.json").write_text(json.dumps({
            "binding": {"server": "server-example", "source": "%2",
                        "process": [["1", "birth"]]},
            "received": real_time.time(), **record}))

    @pytest.mark.parametrize("breaks_on, outcome", [
        (1, "refused"), (2, "attempted_stopped")])
    def test_an_unknown_tmux_server_stops_the_send(
            self, stub, monkeypatch, capsys, tmp_path, breaks_on, outcome):
        calls = stub(panes=(("%2", "codex", "/example/proj"),))
        self.codex_record(monkeypatch, tmp_path, open_turn=True)
        query = ccm_core.tmux_query
        asked = []

        def failing(*args, **kw):
            if args[0] == "display-message":
                asked.append(1)
                if len(asked) == breaks_on:
                    return None
            return query(*args, **kw)
        monkeypatch.setattr(ccm_core, "tmux_query", failing)
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], out["enter_sent"]) == (
            outcome, "observation_failed", False)
        assert self.keys(calls) == ([] if breaks_on == 1 else [("-l", "--", _MSG)])

    @pytest.mark.parametrize("identities, outcome", [
        # each look asks twice: for the send, then to match the hook record
        ([[["1", "birth"]], None], "refused"),
        ([[["1", "birth"]], [["1", "birth"]], [["1", "birth"]], None], "attempted_stopped"),
    ])
    def test_a_process_the_hook_record_cannot_be_matched_to_stops_the_send(
            self, stub, monkeypatch, capsys, tmp_path, identities, outcome):
        calls = stub(panes=(("%2", "codex", "/example/proj"),), identities=identities)
        self.codex_record(monkeypatch, tmp_path, open_turn=True)
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], out["enter_sent"]) == (
            outcome, "observation_failed", False)

    def test_a_record_of_another_process_still_reads_as_no_record(
            self, stub, monkeypatch, capsys, tmp_path):
        stub(panes=(("%2", "codex", "/example/proj"),),
             identities=[[["1", "birth"]], [["1", "birth-B"]]] * 2)
        self.codex_record(monkeypatch, tmp_path, waits={"call": {}})
        out, _, _ = self.run_json([_MSG], capsys)
        assert out["outcome"] == "attempted_seen"

    def test_a_display_build_still_reads_failures_as_no_record(
            self, monkeypatch, tmp_path):
        import os
        if os.geteuid() == 0:
            pytest.skip("root reads any directory")
        markers = tmp_path / "attention"; markers.mkdir(); markers.chmod(0)
        monkeypatch.setattr(ccm_core, "CCM_ATTENTION_DIR", str(markers))
        try:
            assert _READ_MARKERS([], []) == {}
            with pytest.raises(PermissionError):
                _READ_MARKERS([], [], strict=True)
        finally:
            markers.chmod(0o700)

    def test_empty_file_name_is_refused(self, stub, capsys):
        calls = stub()
        out, code, _ = self.run_json(["--file", ""], capsys)
        assert (out["outcome"], out["reason"], code) == ("refused", "file_unreadable", 1)
        assert [c for c in calls if c[0] == "send-keys"] == []

    def test_an_unidentified_process_is_refused_before_the_body(self, stub, capsys):
        calls = stub(identities=[None])
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"]) == ("refused", "pane_unidentified")
        assert self.keys(calls) == []

    def test_typing_failure(self, stub, capsys):
        calls = stub(failing_keys=(_MSG,))
        out, code, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], out["enter_sent"], code) == (
            "attempted_failed", "typing_failed", False, 1)
        assert ("Enter",) not in self.keys(calls)

    def test_enter_failure_is_unknown(self, stub, capsys):
        stub(failing_keys=("Enter",))
        out, code, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], out["enter_sent"], code) == (
            "attempted_failed", "enter_failed", None, 1)

    def test_no_enter_skips_the_second_look(self, stub, monkeypatch, capsys):
        calls = stub()
        looks = []
        real = ccm_send._observe_sidekick
        monkeypatch.setattr(ccm_send, "_observe_sidekick",
                            lambda *a: looks.append(1) or real(*a))
        out, code, _ = self.run_json(["--no-enter", _MSG], capsys)
        assert (out["outcome"], out["enter_requested"], out["enter_sent"]) == (
            "attempted_seen", False, False)
        assert len(looks) == 1
        assert ("Enter",) not in self.keys(calls)

    def test_the_second_look_falls_inside_the_pause(self, stub, monkeypatch, capsys):
        calls = stub()
        times = []
        real = ccm_send._observe_sidekick
        monkeypatch.setattr(ccm_send, "_observe_sidekick", lambda *a: times.append(
            ccm_send.time.monotonic()) or real(*a))
        self.run_json([_MSG], capsys)
        body = next(i for i, c in enumerate(calls) if c[0] == "send-keys" and c[-1] == _MSG)
        enter = next(i for i, c in enumerate(calls) if c[0] == "send-keys" and c[-1] == "Enter")
        pause = [c[1] for c in calls[body:enter] if c[0] == "sleep"]
        assert sum(pause) == pytest.approx(0.3)
        assert times[1] - times[0] == pytest.approx(0.2)

    def test_a_slow_second_look_adds_no_wait(self, stub, monkeypatch, capsys):
        calls = stub()
        real = ccm_send._observe_sidekick
        looks = []

        def slow(*a):
            looks.append(1)
            if len(looks) == 2:
                ccm_send.time.sleep(0.5)  # the look itself takes 0.5 s
            return real(*a)
        monkeypatch.setattr(ccm_send, "_observe_sidekick", slow)
        self.run_json([_MSG], capsys)
        body = next(i for i, c in enumerate(calls) if c[0] == "send-keys" and c[-1] == _MSG)
        enter = next(i for i, c in enumerate(calls) if c[0] == "send-keys" and c[-1] == "Enter")
        assert [c[1] for c in calls[body:enter] if c[0] == "sleep"] == [
            pytest.approx(0.2), 0.5, 0.0]

    def test_unseen_and_unchecked(self, stub, capsys):
        stub(capture="")
        out, code, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], code) == ("attempted_unseen", 1)
        out, code, _ = self.run_json(["hi"], capsys)
        assert (out["outcome"], code) == ("attempted_unchecked", 0)

    @pytest.mark.parametrize("args, reason", [
        ([], "usage"),
        (["   "], "usage"),
        (["--bogus", _MSG], "usage"),
        (["--file"], "usage"),
    ])
    def test_usage_errors_are_json(self, stub, capsys, args, reason):
        calls = stub()
        out, code, _ = self.run_json(args, capsys)
        assert (out["outcome"], out["reason"], code) == ("refused", reason, 1)
        assert self.keys(calls) == []

    def test_unreadable_file_is_json(self, stub, capsys, tmp_path):
        bad = tmp_path / "message.md"
        bad.write_bytes(b"\xff\xfe not text")
        out, _, _ = self.run_json(["--file", str(bad)], capsys)
        assert (out["outcome"], out["reason"]) == ("refused", "file_unreadable")
        out, _, _ = self.run_json(["--file", str(tmp_path / "missing.md")], capsys)
        assert out["reason"] == "file_unreadable"

    def test_unreadable_stdin_is_json(self, stub, monkeypatch, capsys):
        class Broken:
            def read(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        monkeypatch.setattr(ccm_send.sys, "stdin", Broken())
        out, _, _ = self.run_json(["--stdin"], capsys)
        assert (out["outcome"], out["reason"]) == ("refused", "stdin_unreadable")

    def test_json_only_where_a_flag_stands(self, stub, capsys, tmp_path):
        calls = stub(capture="--json is the text\n" + _MSG)

        def text_output(args):
            try:
                ccm_send.cmd_sidekick_send(args)
            except SystemExit:
                pass
            return capsys.readouterr().out
        assert not text_output(["--", "--json", "is the text"]).startswith("{")
        assert ("-l", "--", "--json is the text") in self.keys(calls)
        named = tmp_path / "--json"
        named.write_text(_MSG)
        assert not text_output(["--file", str(named)]).startswith("{")
        assert ("-l", "--", _MSG) in self.keys(calls)

    def test_json_help(self, capsys):
        out, code, _ = self.run_json(["--help"], capsys)
        assert code == 0
        assert out["help"].startswith("Usage: ccm sidekick-send")

    def test_not_in_tmux_is_json(self, monkeypatch, capsys):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        out, _, _ = self.run_json([_MSG], capsys)
        assert (out["outcome"], out["reason"], out["agent"], out["pane"]) == (
            "refused", "not_in_tmux", None, None)
