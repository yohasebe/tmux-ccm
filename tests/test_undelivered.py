"""Delivery acceptance and explicit decisions about undelivered records."""
from pathlib import Path
from unittest.mock import Mock

import pytest

import ccm_constants
import ccm_core
import ccm_send
import ccm_spool
import dashboard
from test_send import TestCmdSend as SendHelpers, composer_screen
from test_dashboard import _stub_dashboard_environment, _make_mock_stdscr


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(ccm_send.time, "time", lambda: now[0])
    monkeypatch.setattr(ccm_send.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def test_idle_without_input_box_is_not_start_ready(monkeypatch, clock):
    p = SendHelpers()._make_project()
    monkeypatch.setattr(ccm_core, "build_project_list", lambda **k: [p])
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: ("Loading…", None))
    assert ccm_send._wait_for_target_idle("demo", timeout_sec=2) != "IDLE"
    assert clock[0] == 2


def test_start_wait_requires_stable_empty_box_on_pinned_pane(monkeypatch, clock):
    p = SendHelpers()._make_project()
    monkeypatch.setattr(ccm_core, "build_project_list", lambda **k: [p])
    seen = []
    def capture(pane):
        seen.append(pane)
        text = "❯ draft" if clock[0] < 1 else "❯ "
        return composer_screen(text), None
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", capture)
    assert ccm_send._wait_for_target_idle("demo", timeout_sec=3, pane_target="%42") == "IDLE"
    assert clock[0] == 2
    assert set(seen) == {"%42"}


def test_cmd_send_reports_unchanged_body_unsent(monkeypatch, clock, capsys):
    helper = SendHelpers()
    helper._patch_resolution(monkeypatch)
    submitted = [False]
    def tmux(*args):
        if args[0] == "send-keys" and args[-1] == "Enter":
            submitted[0] = True
        return ""
    monkeypatch.setattr(ccm_core, "tmux_cmd", tmux)
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: (
        composer_screen("❯ Please reply only OK." if submitted[0] else "❯ "), None))
    with pytest.raises(SystemExit) as error:
        ccm_send.cmd_send(["demo", "--now", "Please reply only OK."])
    assert error.value.code == 1
    out = capsys.readouterr()
    assert "Sent to" not in out.out
    assert "unsent" in out.err


@pytest.mark.parametrize("draft,expected", [
    ("Please reply only OK.", True),
    ("reply only", False),
    ("Please write another answer", False),
    ("", False),
])
def test_prefix_is_anchored(monkeypatch, clock, draft, expected):
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: (composer_screen("❯ " + draft), None))
    assert bool(ccm_send.held_after_submit("%1", message="Please reply only OK.")) == expected


def test_transient_body_clears_after_enter(monkeypatch, clock):
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: (
        composer_screen("❯ hello" if clock[0] < .8 else "❯ "), None))
    assert ccm_send.held_after_submit("%1", message="hello") is None
    assert clock[0] == .8


def test_notice_still_catches_arbitrary_removal(monkeypatch, clock):
    screen = "Removed 99 invisible characters · review and press Enter to send\n" + composer_screen("❯ changed", scrollback=False)
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: (screen, None))
    assert "Removed 99" in ccm_send.held_after_submit("%1", message="original body")


def record(kind="expired", ident="1000-sender", body="Please reply only OK."):
    path = Path(ccm_spool.SPOOL_ROOT) / "demo" / kind / (ident + ".msg")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_spool_unchanged_submission_is_held(monkeypatch, clock):
    msg_id, _ = ccm_spool.enqueue("demo", "sender", "Please reply only OK.")
    monkeypatch.setattr(ccm_spool, "_deliverable_pane", lambda w: ("%1", None))
    body = []
    monkeypatch.setattr(ccm_send, "_type_body", lambda p, lines: body.extend(lines))
    monkeypatch.setattr(ccm_send, "_send_keys", lambda *a, **k: None)
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: (composer_screen("❯ " + "\n".join(body)), None))
    pdir = Path(ccm_spool.SPOOL_ROOT) / "demo"
    assert ccm_spool._deliver_one(SendHelpers()._make_project(), str(pdir), msg_id + ".msg") is False
    assert (pdir / "held" / (msg_id + ".msg")).exists()
    assert not (pdir / "delivered").exists()


def test_discard_one_and_confirmation(monkeypatch):
    one = record()
    two = record(ident="2000-sender")
    held = record("held")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit):
        ccm_spool.cmd_spool(["discard", "expired", one.stem, "demo"])
    assert one.exists()
    ccm_spool.cmd_spool(["discard", "expired", one.stem, "demo", "--yes"])
    assert not one.exists() and two.exists() and held.exists()


@pytest.mark.parametrize("success", [True, False])
def test_resend_removes_only_after_success(monkeypatch, success):
    path = record()
    def send(args):
        assert args == ["--now", "--yes", "--file", str(path), "--start", "--", "demo"]
        assert path.exists()
        if not success:
            ccm_core.ccm_die("unsent")
    monkeypatch.setattr(ccm_send, "cmd_send", send)
    args = ["resend", "expired", path.stem, "demo", "--yes", "--start"]
    if success:
        ccm_spool.cmd_spool(args)
    else:
        with pytest.raises(SystemExit):
            ccm_spool.cmd_spool(args)
    assert path.exists() != success
    assert not Path(str(path.parent.parent) + ".lock").exists()


def test_held_cannot_be_resent(monkeypatch):
    path = record("held")
    send = Mock()
    monkeypatch.setattr(ccm_send, "cmd_send", send)
    with pytest.raises(SystemExit):
        ccm_spool.cmd_spool(["resend", "held", path.stem, "demo", "--yes"])
    send.assert_not_called()
    assert path.exists()


def test_record_path_cannot_escape(monkeypatch, tmp_path):
    outside = tmp_path / "1000-sender.msg"
    outside.write_text("keep")
    with pytest.raises(SystemExit):
        ccm_spool.cmd_spool(["discard", "expired", "../../1000-sender", "demo", "--yes"])
    assert outside.read_text() == "keep"


def make_dashboard(monkeypatch, keys):
    _stub_dashboard_environment(monkeypatch)
    d = dashboard.Dashboard()
    screen = _make_mock_stdscr()
    screen.getch.side_effect = keys
    return d, screen


def test_dashboard_u_opens_list_and_preserves_project_selection(monkeypatch):
    record()
    d, screen = make_dashboard(monkeypatch, [ord("q")])
    d.selected = 4
    assert d._handle_key(ord("u"), screen) == ""
    assert d.selected == 4
    text = " ".join(str(c.args) for c in screen.addstr.call_args_list)
    assert "Undelivered messages" in text and "sender → demo" in text
    assert "Please reply only" in text


@pytest.mark.parametrize("answer", [None, "n", "y"])
def test_dashboard_discard_confirmation(monkeypatch, answer):
    path = record()
    d, screen = make_dashboard(monkeypatch, [ord("d"), ord("q")])
    monkeypatch.setattr(d, "_prompt", lambda *a: answer)
    d._do_spool(screen)
    assert path.exists() == (answer != "y")


def test_dashboard_held_has_no_resend(monkeypatch):
    record("held")
    d, screen = make_dashboard(monkeypatch, [ord("r"), ord("q")])
    action = Mock()
    monkeypatch.setattr(d, "_prompt", action)
    d._do_spool(screen)
    action.assert_not_called()
    assert "r resend" not in " ".join(str(c.args) for c in screen.addstr.call_args_list)


def test_dashboard_resend_start_and_failure_keeps_record(monkeypatch):
    path = record()
    d, screen = make_dashboard(monkeypatch, [ord("r"), ord("q")])
    monkeypatch.setattr(dashboard, "build_project_list", lambda **k: [SendHelpers()._make_project(state="SHELL")])
    prompts = []
    monkeypatch.setattr(d, "_prompt", lambda s, text: prompts.append(text) or "y")
    def send(args):
        assert "--start" in args and "--now" in args
        ccm_core.ccm_die("unsent; inspect input box")
    monkeypatch.setattr(ccm_send, "cmd_send", send)
    d._do_spool(screen)
    assert "Start Claude" in prompts[0]
    assert path.exists()
    assert "unsent" in d._msg_text


def test_dashboard_open_sends_no_keys(monkeypatch):
    record("held")
    d, screen = make_dashboard(monkeypatch, [ord("o")])
    monkeypatch.setattr(dashboard, "build_project_list", lambda **k: [SendHelpers()._make_project()])
    calls = Mock(return_value="")
    monkeypatch.setattr(dashboard, "tmux_cmd", calls)
    assert d._do_spool(screen) == "attached"
    assert calls.call_args_list[0].args == ("select-window", "-t", "0:5")
    assert calls.call_count == 1


def test_dashboard_full_text_scrolls_long_lines(monkeypatch):
    d, screen = make_dashboard(monkeypatch, [dashboard.curses.KEY_NPAGE, ord("q")])
    screen.getmaxyx.return_value = (6, 20)
    d._spool_text(screen, "message", "x" * 100 + "THE END")
    assert "THE END" in "".join(c.args[2] for c in screen.addstr.call_args_list if c.args[0] > 0)
    screen.timeout.assert_called_with(50)


def test_start_timeout_does_not_type_body(monkeypatch, clock, capsys):
    helper = SendHelpers()
    initial = helper._make_project(state="SHELL")
    idle = helper._make_project()
    helper._patch_resolution(monkeypatch, initial)
    states = iter([initial])
    monkeypatch.setattr(ccm_core, "build_project_list", lambda **k: [next(states, idle)])
    monkeypatch.setattr(ccm_send, "START_WAIT_SEC", 2)
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: ("Loading…", None))
    calls = Mock(return_value="")
    monkeypatch.setattr(ccm_core, "tmux_cmd", calls)
    with pytest.raises(SystemExit) as error:
        ccm_send.cmd_send(["demo", "--start", "--yes", "hello"])
    assert error.value.code == 1
    assert not any("-l" in c.args for c in calls.call_args_list)
    assert "Sent to" not in capsys.readouterr().out


def test_new_draft_after_clear_is_not_ours(monkeypatch, clock):
    def capture(pane):
        return composer_screen("❯ other draft" if clock[0] < .4 else "❯ Please reply only OK."), None
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", capture)
    assert ccm_send.held_after_submit("%1", message="Please reply only OK.") is None


def test_prefix_matches_wrapped_multiline_body(monkeypatch, clock):
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: (
        composer_screen("❯ Please investigate this", "  specific failure in detail", "  and report back"), None))
    assert ccm_send.held_after_submit("%1", message="Please investigate this specific failure in detail\nand report back")


def test_dashboard_read_full_body(monkeypatch):
    record(body="first\nsecond\nlast")
    d, screen = make_dashboard(monkeypatch, [10, ord("q")])
    viewer = Mock()
    monkeypatch.setattr(d, "_spool_text", viewer)
    d._do_spool(screen)
    assert viewer.call_args.args[2] == "first\nsecond\nlast"


def test_dashboard_resend_cancel_does_not_start(monkeypatch):
    path = record()
    d, screen = make_dashboard(monkeypatch, [ord("r"), ord("q")])
    monkeypatch.setattr(dashboard, "build_project_list", lambda **k: [SendHelpers()._make_project(state="SHELL")])
    monkeypatch.setattr(d, "_prompt", lambda *a: "n")
    send = Mock()
    monkeypatch.setattr(ccm_send, "cmd_send", send)
    d._do_spool(screen)
    send.assert_not_called()
    assert path.exists()


def test_dashboard_preserves_record_order_on_refresh(monkeypatch):
    record(ident="1000-first")
    second = record(ident="2000-second")
    d, screen = make_dashboard(monkeypatch, [ord("j"), ord("d"), 10, ord("q")])
    monkeypatch.setattr(d, "_prompt", lambda *a: "y")
    original = ccm_spool.attention_records
    count = [0]
    def refresh():
        count[0] += 1
        if count[0] > 1:
            record(ident="0500-new", body="new message")
        return original()
    monkeypatch.setattr(ccm_spool, "attention_records", refresh)
    reader = Mock(wraps=ccm_spool.read_record)
    monkeypatch.setattr(ccm_spool, "read_record", reader)
    viewer = Mock()
    monkeypatch.setattr(d, "_spool_text", viewer)
    d._do_spool(screen)
    assert not second.exists()
    # New older record is appended, not inserted before the selection.
    assert reader.call_args.args == ("expired", "0500-new", "demo")
    assert viewer.call_args.args[2] == "new message"


def test_cli_resend_keeps_record_if_target_became_shell(monkeypatch):
    path = record()
    SendHelpers()._patch_resolution(monkeypatch, SendHelpers()._make_project(state="SHELL"))
    monkeypatch.setattr(ccm_core, "tmux_cmd", lambda *a: "")
    with pytest.raises(SystemExit):
        ccm_spool.cmd_spool(["resend", "expired", path.stem, "demo", "--yes"])
    assert path.exists()
    assert ccm_spool.pending_counts() == {}


@pytest.mark.parametrize("kind", ["expired", "held"])
def test_discard_input_wording_only_for_held(monkeypatch, kind):
    path = record(kind)
    d, screen = make_dashboard(monkeypatch, [ord("d"), ord("q")])
    prompts = []
    monkeypatch.setattr(d, "_prompt", lambda s, text: prompts.append(text) or "y")
    d._do_spool(screen)
    assert not path.exists()
    assert prompts == [
        "Discard selected record (input unchanged)? [y/N] " if kind == "held"
        else "Discard selected record? [y/N] "
    ]
    shown = [c.args[2] for c in screen.addstr.call_args_list]
    expected = ("Record discarded; input box unchanged." if kind == "held"
                else "Record discarded.")
    assert expected in shown
    if kind == "expired":
        assert not any("input box unchanged" in text for text in shown)


def test_start_returns_to_shell_without_typing_body_or_submit(monkeypatch, clock, capsys):
    """A failed resume leaves a shell prompt, even if IDLE lingers once."""
    helper = SendHelpers()
    shell = helper._make_project(state="SHELL")
    idle = helper._make_project()
    helper._patch_resolution(monkeypatch, shell)
    states = iter([shell, idle])
    monkeypatch.setattr(ccm_core, "build_project_list", lambda **k: [next(states, shell)])
    monkeypatch.setattr(ccm_send, "START_WAIT_SEC", 2)
    screen = "No conversation found to continue\n❯ \n"
    monkeypatch.setattr(ccm_send, "capture_composer_snapshot", lambda p: (screen, None))
    monkeypatch.setattr(ccm_send, "_recheck_delivery_state", lambda *a: "SHELL")
    calls = []
    def tmux(*args):
        calls.append(args)
        return screen if args[0] == "capture-pane" else ""
    monkeypatch.setattr(ccm_core, "tmux_cmd", tmux)
    exit_code = None
    try:
        ccm_send.cmd_send(["demo", "--start", "--yes", "hello"])
    except SystemExit as error:
        exit_code = error.code
    keys = [args for args in calls if args[0] == "send-keys"]
    launch = [args for args in keys if ccm_constants.CLAUDE_CMD in args]
    assert len(launch) == 1, keys
    assert not any("-l" in args for args in keys), "message body was typed into the shell"
    assert keys[keys.index(launch[0]) + 1:] == [], "keys reached the shell after launch exited"
    assert [args for args in keys if "Enter" in args] == launch
    assert exit_code == 1
    output = capsys.readouterr()
    assert "Sent to" not in output.out
    assert "did not reach IDLE" in output.err
