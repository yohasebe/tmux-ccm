"""CLI session errors must distinguish unavailable tmux from missing projects."""

import json
import subprocess

import pytest

import ccm_core
import ccm_commands
import ccm_send
import ccm_snapshot


@pytest.fixture
def isolated_session(tmp_path, monkeypatch):
    monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
    monkeypatch.setattr(ccm_core, "CCM_SNAPSHOT_DIR", str(tmp_path))
    (tmp_path / "saved.json").write_text(json.dumps({"projects": []}))
    return tmp_path


def invoke(command, root):
    if command == "send":
        ccm_send.cmd_send(["demo", "--no-enter", "-y", "probe"])
    elif command == "add":
        ccm_commands.cmd_add(str(root), "demo", start_claude=False)
    elif command == "register":
        ccm_commands.cmd_register("demo")
    elif command == "attach":
        ccm_commands.cmd_attach("demo")
    else:
        ccm_snapshot.cmd_snapshot_load("saved")


@pytest.mark.parametrize("command", ["send", "add", "register", "attach", "load"])
@pytest.mark.parametrize("popup", [False, True])
@pytest.mark.parametrize("inside", [False, True])
def test_blocked_connection_names_error_and_remedy(
        isolated_session, monkeypatch, capsys, command, popup, inside):
    root = isolated_session
    if popup:
        (root / "popup-session").write_text("main")
    if inside:
        monkeypatch.setenv("TMUX", "test-socket,123,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    detail = "error connecting to test-socket (Operation not permitted)"
    calls = []

    def blocked(args, **kwargs):
        calls.append(args)
        assert args[0] == "tmux"
        return subprocess.CompletedProcess(args, 1, b"", detail.encode())

    monkeypatch.setattr(ccm_core.subprocess, "run", blocked)
    code = None
    try:
        invoke(command, root)
    except SystemExit as exc:
        code = exc.code
    assert code == 1
    error = capsys.readouterr().err
    assert detail in error
    assert "sandbox" in error and "outside" in error
    assert "Not inside a tmux session" not in error
    assert "Project not found" not in error
    assert calls
    assert all(args[1] in {"display-message", "list-clients", "list-sessions", "list-windows"}
               for args in calls)


@pytest.mark.parametrize("inside", [False, True])
def test_no_server_distinguishes_tmux_environment(
        isolated_session, monkeypatch, capsys, inside):
    if inside:
        monkeypatch.setenv("TMUX", "test-socket,123,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    detail = b"no server running on test-socket"
    monkeypatch.setattr(ccm_core.subprocess, "run", lambda args, **kw:
                        subprocess.CompletedProcess(args, 1, b"", detail))
    with pytest.raises(SystemExit):
        invoke("send", isolated_session)
    error = capsys.readouterr().err
    if inside:
        assert detail.decode() in error and "sandbox" in error
        assert "Not inside" not in error
    else:
        assert "Not inside a tmux session" in error
        assert "tmux new-session" in error


def test_reachable_server_missing_project(isolated_session, monkeypatch, capsys):
    (isolated_session / "popup-session").write_text("main")

    def available(args, **kwargs):
        assert args[1] in {"list-sessions", "list-windows"}
        return subprocess.CompletedProcess(args, 0, b"main" if args[1] == "list-sessions" else b"", b"")

    monkeypatch.setattr(ccm_core.subprocess, "run", available)
    with pytest.raises(SystemExit):
        invoke("send", isolated_session)
    error = capsys.readouterr().err
    assert "Project not found: demo" in error
    assert "sandbox" not in error


@pytest.mark.parametrize("failure,fragment", [
    (PermissionError("permission denied"), "permission denied"),
    (FileNotFoundError("tmux executable missing"), "tmux executable missing"),
    (subprocess.TimeoutExpired(["tmux"], 5), "timed out"),
])
def test_query_keeps_failure_detail(monkeypatch, failure, fragment):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(ccm_core.subprocess, "run", fail)
    errors = []
    assert ccm_core.tmux_query("list-sessions", errors=errors) is None
    assert fragment in errors[0]
    assert ccm_core.tmux_query("list-sessions") is None


@pytest.mark.parametrize("returncode,stdout,stderr,expected,diagnostic", [
    (0, b"", b"", "", None),
    (0, b"main\n", b"", "main", None),
    (1, b"", b"bad \xff socket", None, "bad \ufffd socket"),
    (2, b"", b"", None, "tmux exited with status 2"),
])
def test_query_distinguishes_empty_from_failed(
        monkeypatch, returncode, stdout, stderr, expected, diagnostic):
    monkeypatch.setattr(ccm_core.subprocess, "run", lambda args, **kw:
                        subprocess.CompletedProcess(args, returncode, stdout, stderr))
    errors = []
    assert ccm_core.tmux_query("list-sessions", errors=errors) == expected
    assert errors == ([] if diagnostic is None else [diagnostic])


def test_reachable_server_without_current_session(isolated_session, monkeypatch, capsys):
    monkeypatch.delenv("TMUX", raising=False)
    def available(args, **kwargs):
        output = b"main" if args[1] == "list-sessions" else b""
        return subprocess.CompletedProcess(args, 0, output, b"")
    monkeypatch.setattr(ccm_core.subprocess, "run", available)
    with pytest.raises(SystemExit):
        invoke("send", isolated_session)
    error = capsys.readouterr().err
    assert "attach-session" in error
    assert "Not inside" not in error and "sandbox" not in error
