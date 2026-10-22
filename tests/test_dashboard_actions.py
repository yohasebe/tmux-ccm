"""The dashboard's save and preview actions, through the names their own
module looks up."""

import json
import subprocess
from types import SimpleNamespace

import pytest

import dashboard_actions
from dashboard import Dashboard


@pytest.fixture
def board(monkeypatch):
    d = object.__new__(Dashboard)
    d.messages, d.drawn = [], []
    monkeypatch.setattr(Dashboard, "_show_message",
                        lambda self, s, msg, duration=1: self.messages.append(msg))
    monkeypatch.setattr(Dashboard, "_addstr",
                        lambda self, s, y, x, text, attr=0:
                        self.drawn.append((y, text)))
    return d


# ─── save ───

@pytest.fixture
def saving(board, monkeypatch, tmp_path):
    saved = []

    def save(name, quiet=False):
        saved.append((name, quiet))
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"projects": [{}, {}, {}]}), encoding="utf-8")
    monkeypatch.setattr(dashboard_actions, "cmd_snapshot_save", save)
    monkeypatch.setattr(dashboard_actions, "CCM_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(dashboard_actions.time, "strftime",
                        lambda fmt: fmt.replace("%Y%m%d", "20261023")
                                       .replace("%H%M", "0930"))
    return board, saved


def _answer(board, monkeypatch, answer):
    monkeypatch.setattr(Dashboard, "_prompt",
                        lambda self, s, text: answer)


def test_an_empty_name_saves_under_the_dated_default(saving, monkeypatch):
    board, saved = saving
    _answer(board, monkeypatch, "")
    board._do_save(None)
    assert saved == [("save-20261023-0930", True)]
    assert board.messages == ["Saved: save-20261023-0930 (3 projects)"]


def test_a_cancelled_prompt_saves_nothing(saving, monkeypatch):
    board, saved = saving
    _answer(board, monkeypatch, None)
    board._do_save(None)
    assert saved == []
    assert board.messages == []


def test_a_snapshot_it_cannot_read_back_is_still_reported_saved(
        saving, monkeypatch):
    board, saved = saving
    monkeypatch.setattr(dashboard_actions, "cmd_snapshot_save",
                        lambda name, quiet=False: None)
    _answer(board, monkeypatch, "mine")
    board._do_save(None)
    assert board.messages == ["Saved: mine"]


@pytest.mark.parametrize("failure, message", [
    (SystemExit(1), "Save failed: no active projects"),
    (OSError("x" * 80), "Save failed: " + "x" * 50),
])
def test_a_failed_save_says_why(saving, monkeypatch, failure, message):
    board, _ = saving

    def fail(name, quiet=False):
        raise failure
    monkeypatch.setattr(dashboard_actions, "cmd_snapshot_save", fail)
    _answer(board, monkeypatch, "mine")
    board._do_save(None)
    assert board.messages == [message]


# ─── preview ───

class _Screen:
    def __init__(self, key, height=5):
        self.key, self.size, self.timeouts = key, (height, 40), []

    def erase(self):
        pass

    def refresh(self):
        pass

    def getmaxyx(self):
        return self.size

    def timeout(self, ms):
        self.timeouts.append(ms)

    def getch(self):
        return self.key


@pytest.fixture
def previewing(board, monkeypatch):
    board.projects = [SimpleNamespace(win_target="0:1", name="alpha")]
    board.selected = 0
    monkeypatch.setattr(Dashboard, "_resolve_preview_pane",
                        lambda self, target: "%7")
    return board


def _captures(monkeypatch, *outputs):
    calls, replies = [], iter(outputs)
    monkeypatch.setattr(dashboard_actions, "tmux_cmd",
                        lambda *a, **k: calls.append(a) or next(replies))
    return calls


def test_the_preview_shows_the_pane_and_waits_for_a_key(previewing, monkeypatch):
    calls = _captures(monkeypatch, "one\ntwo\nthree\nfour\nfive")
    screen = _Screen(ord("x"))
    previewing._do_preview(screen)
    assert calls == [("capture-pane", "-t", "%7", "-p", "-S", "-30")]
    assert previewing.drawn == [
        (0, "=== alpha (press any key, 'c' to copy) ==="),
        (1, "one"), (2, "two"), (3, "three")]     # height 5 keeps 3 rows
    assert screen.timeouts == [-1, 50]


def test_a_blank_pane_is_captured_again_from_the_alternate_screen(
        previewing, monkeypatch):
    calls = _captures(monkeypatch, "  \n", "full-screen app")
    previewing._do_preview(_Screen(ord("x")))
    assert calls == [("capture-pane", "-t", "%7", "-p", "-S", "-30"),
                     ("capture-pane", "-a", "-t", "%7", "-p", "-S", "-30")]
    assert (1, "full-screen app") in previewing.drawn


def test_nothing_captured_draws_nothing(previewing, monkeypatch):
    _captures(monkeypatch, "", "")
    screen = _Screen(ord("x"))
    previewing._do_preview(screen)
    assert previewing.drawn == []
    assert screen.timeouts == []


def test_c_copies_with_the_first_clipboard_tool_present(previewing, monkeypatch):
    _captures(monkeypatch, "copied text")
    tried = []

    def run(cmd, input=None, timeout=None):
        tried.append((cmd, input, timeout))
        if cmd[0] in ("pbcopy", "clip.exe"):
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(dashboard_actions.subprocess, "run", run)
    previewing._do_preview(_Screen(ord("c")))
    assert tried == [
        (["pbcopy"], b"copied text", 3),
        (["clip.exe"], b"copied text", 3),
        (["xclip", "-selection", "clipboard"], b"copied text", 3),
    ]


def test_another_key_copies_nothing(previewing, monkeypatch):
    _captures(monkeypatch, "text")
    ran = []
    monkeypatch.setattr(dashboard_actions.subprocess, "run",
                        lambda *a, **k: ran.append(a))
    previewing._do_preview(_Screen(ord("q")))
    assert ran == []
