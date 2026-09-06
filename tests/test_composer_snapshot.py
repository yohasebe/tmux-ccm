"""Composer guards consume a single attributed screen snapshot."""

import pytest

import ccm_constants
import ccm_core
import ccm_pane_state
import ccm_send
import ccm_spool


def screen(row):
    rule = "─" * 40
    return f"{rule}\n{row}\n{rule}\n"


def stub_capture(monkeypatch, consumer, frames, alternate):
    calls = []
    frames = iter(frames)

    def tmux(*args):
        if args[0] != "capture-pane":
            return ""
        calls.append(args)
        if alternate and "-a" not in args:
            return ""
        return next(frames)

    monkeypatch.setattr(ccm_core, "tmux_cmd", tmux)
    if consumer == "spool":
        pane = ccm_pane_state.PaneInfo("%51", "100", True, "claude", False, "101")
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr(ccm_spool, "enumerate_window_panes", lambda *a: [pane])
        monkeypatch.setattr(ccm_spool, "detect_pane_state", lambda *a, **k: "IDLE")
    return calls


def verdict(consumer):
    if consumer == "send":
        return ccm_send._composer_draft_fragment("%51")
    return ccm_spool._deliverable_pane("0:5")


@pytest.mark.parametrize("consumer", ["send", "spool"])
@pytest.mark.parametrize("alternate", [False, True])
def test_draft_then_suggestion_never_merges_frames(monkeypatch, consumer, alternate):
    draft = screen("❯ a real half-typed draft")
    suggestion = screen("\x1b[39m❯ \x1b[2mnow try it with -I\x1b[0m")
    calls = stub_capture(monkeypatch, consumer, [draft, suggestion], alternate)
    result = verdict(consumer)
    assert result == ("❯ a real half-typed draft" if consumer == "send"
                      else (None, "composer holds a draft"))
    assert len(calls) == (2 if alternate else 1)
    assert all("-e" in args for args in calls)
    assert ("-a" in calls[-1]) == alternate


@pytest.mark.parametrize("consumer", ["send", "spool"])
@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("dim", [False, True])
def test_coloured_rules_and_text_share_snapshot(monkeypatch, consumer, alternate, dim):
    row = "❯ \x1b[2mtext\x1b[0m" if dim else "❯ \x1b[38;5;2mtext\x1b[0m"
    rule = "\x1b[90m" + "─" * 40 + "\x1b[0m"
    capture = f"{rule}\n{row}\n{rule}\n"
    calls = stub_capture(monkeypatch, consumer, [capture], alternate)
    result = verdict(consumer)
    expected = (None if dim else "❯ text") if consumer == "send" else (
        ("%51", None) if dim else (None, "composer holds a draft"))
    assert result == expected
    assert len(calls) == (2 if alternate else 1)
    assert all("-e" in args for args in calls)


def test_mismatched_attributed_line_cannot_clear_plain_draft():
    plain = screen("❯ a real half-typed draft")
    attributed = screen("\x1b[39m❯ \x1b[2mnow try it with -I\x1b[0m")
    assert ccm_constants.composer_draft_fragment(plain, attributed) == (
        "❯ a real half-typed draft")
