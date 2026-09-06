"""Clock-history parity across one-off pane-state consumers."""

import pytest

import ccm_core
import ccm_pane_state
import ccm_send
import ccm_spool
import ccm_window

from conftest import make_ps_lines


@pytest.mark.parametrize("consumer", ["send", "spool", "window"])
@pytest.mark.parametrize("stored,stamp,expected", [
    ("(7s · thinking)", "100", "IDLE"),
    ("(7s · thinking)", "995", "BUSY"),
    ("(6s · thinking)", "100", "BUSY"),
    ("", "", "BUSY"),
    ("(7s · thinking)", "bad timestamp", "BUSY"),
], ids=["stale", "fresh", "advanced", "missing", "malformed"])
def test_consumers_use_saved_clock(monkeypatch, consumer, stored, stamp, expected):
    screen = "quoted (7s · thinking)\n❯ \n"
    ps = make_ps_lines((100, 1, 100, "bash"), (200, 100, 200, "claude"),
                       (300, 1, 300, "bash"))
    calls = []

    def tmux(*args):
        calls.append(args)
        if args[0] == "show-option":
            assert args[-2] == "0:5"
            return {"@ccm_work_clock": stored, "@ccm_work_clock_ts": stamp,
                    "@ccm_dir": "/tmp/demo"}.get(args[-1], "")
        if args[0] == "list-panes":
            if consumer == "window":
                return "300\t%52\tbash\t1\t48\n100\t%51\tclaude\t0\t48"
            return "%51\t100\t1\tclaude\t0"
        if args[0] == "capture-pane":
            return screen
        raise AssertionError(f"Unexpected tmux mutation: {args}")

    monkeypatch.setattr(ccm_core, "tmux_cmd", tmux)
    monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "\n".join(ps))
    monkeypatch.setattr(ccm_pane_state, "_now", lambda: 1000)
    observed = []
    detect = ccm_pane_state.detect_pane_state

    def record(*args, **kwargs):
        state = detect(*args, **kwargs)
        if args[1] == "%51":
            observed.append(state)
        return state

    module = {"send": ccm_send, "spool": ccm_spool, "window": ccm_window}[consumer]
    monkeypatch.setattr(module, "detect_pane_state", record)
    for _ in range(2):
        if consumer == "send":
            assert ccm_send._recheck_delivery_state("0:5", "%51") == expected
        elif consumer == "spool":
            result = ccm_spool._deliverable_pane("0:5")
            assert result == (("%51", None) if expected == "IDLE"
                              else (None, "raw state BUSY"))
        else:
            ccm_window.auto_focus_attention_pane("0:5")
    assert observed == [expected, expected]
    assert any(c[-1] == "@ccm_work_clock" for c in calls)


@pytest.mark.parametrize("cached,expected", [
    (("(7s · thinking)", "100"), ("(7s · thinking)", 100)),
    (("", ""), None),
    (("(7s · thinking)", "invalid"), None),
])
def test_cached_clock_does_not_query_tmux(monkeypatch, cached, expected):
    def unexpected(*args):
        raise AssertionError("Cached clock must not query tmux")

    monkeypatch.setattr(ccm_core, "tmux_cmd", unexpected)
    assert ccm_pane_state.read_work_clock("0:5", cached=cached) == expected
