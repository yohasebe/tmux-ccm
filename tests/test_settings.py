"""Settings readers share failure semantics and cannot block on pipes."""

import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

import ccm_canaries
import ccm_core
import ccm_settings


@pytest.mark.parametrize("content", [b"{", b"[]", b"null", b"false", b"1", b'"text"', b"\xff"])
def test_unreadable_settings_are_unknown(tmp_path, monkeypatch, content):
    path = tmp_path / "settings.json"
    path.write_bytes(content)
    monkeypatch.setattr("os.path.expanduser", lambda p: str(path))
    monkeypatch.setattr(ccm_canaries, "_settings_sources",
                        lambda projects: [("user settings", str(path))])
    assert ccm_settings.read_settings(path) is None
    assert ccm_core.own_hook_entry_count() is None
    assert ccm_core.hooks_configured() is None
    assert ccm_canaries.unreadable_settings() == ["user settings"]


def test_regular_symlink_and_empty_object(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    assert ccm_settings.read_settings(link) == {}
    path.write_text('{"disableAllHooks": true}')
    assert ccm_settings.read_settings(link) == {"disableAllHooks": True}


def test_missing_directory_and_device_are_unreadable(tmp_path):
    assert ccm_settings.read_settings(tmp_path / "absent") is None
    assert ccm_settings.read_settings(tmp_path) is None
    assert ccm_settings.read_settings(os.devnull) is None


@pytest.mark.parametrize("replace_on_open", [False, True])
def test_fifo_never_waits_for_a_writer(tmp_path, replace_on_open):
    # A real child with a deadline also makes a regression to blocking
    # open fail instead of hanging pytest. The replacement case rejects
    # a path-stat check followed by an ordinary blocking open.
    path = tmp_path / "settings.json"
    path.write_text("{}")
    marker = tmp_path / "replaced"
    script = r'''import os, sys, builtins
sys.path.insert(0, sys.argv[1])
from ccm_settings import read_settings
path, marker = sys.argv[2:4]
original_open = os.open
original_builtin = builtins.open
replaced = False
def replace(target):
    global replaced
    if target == path and not replaced:
        replaced = True
        os.unlink(path)
        os.mkfifo(path)
        with original_builtin(marker, "w") as stream:
            stream.write("replaced")
def low_open(target, *args, **kwargs):
    replace(target)
    return original_open(target, *args, **kwargs)
def high_open(target, *args, **kwargs):
    replace(target)
    return original_builtin(target, *args, **kwargs)
if sys.argv[4] == "True":
    os.open = low_open
    builtins.open = high_open
else:
    os.unlink(path)
    os.mkfifo(path)
assert read_settings(path) is None
'''
    try:
        subprocess.run([sys.executable, "-c", script,
                        os.path.dirname(ccm_settings.__file__), str(path),
                        str(marker), str(replace_on_open)], check=True, timeout=5)
    finally:
        # Check even if the child times out: a timeout without the swap
        # would not demonstrate replacement-at-open protection.
        if replace_on_open:
            assert marker.read_text() == "replaced"



@pytest.mark.parametrize("content", ["{}", "{", "[]"])
def test_descriptor_closed_after_read(tmp_path, monkeypatch, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    original_open = os.open
    descriptors = []

    def track(*args):
        fd = original_open(*args)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(os, "open", track)
    ccm_settings.read_settings(path)
    assert descriptors, "the reader must have opened a descriptor"
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_hook_names_must_appear_in_hook_commands(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr("os.path.expanduser", lambda p: str(path))
    path.write_text(json.dumps({"notes": list(ccm_core.HOOK_SCRIPTS)}))
    assert not ccm_core.hooks_configured()
    complete = json.loads((Path(__file__).parent / "fixtures/hooks-complete.json").read_text())
    path.write_text(json.dumps(complete))
    assert ccm_core.hooks_configured()


@pytest.mark.parametrize("kind", ["fifo", "device", "fstat_failure"])
def test_rejected_descriptor_is_closed(tmp_path, kind):
    # Bound every FIFO read, including the close check: replacing the
    # reader with blocking open must fail, not hang the test runner.
    script = r'''import errno, os, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from ccm_settings import read_settings
path, kind = sys.argv[2:4]
if kind == "fifo":
    os.mkfifo(path)
elif kind == "device":
    path = os.devnull
else:
    with open(path, "w") as stream:
        stream.write("{}")
original_open, original_fstat = os.open, os.fstat
descriptors = []
def track(*args, **kwargs):
    fd = original_open(*args, **kwargs)
    descriptors.append(fd)
    return fd
def fail(fd):
    raise OSError(errno.EIO, "simulated fstat failure")
try:
    with patch("os.open", track), patch("os.fstat", fail if kind == "fstat_failure" else original_fstat):
        assert read_settings(path) is None
    assert descriptors, "the rejection must follow an actual open"
    for fd in descriptors:
        try:
            original_fstat(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            raise AssertionError("rejected descriptor is still open")
finally:
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            pass
'''
    subprocess.run([sys.executable, "-c", script,
                    os.path.dirname(ccm_settings.__file__),
                    str(tmp_path / "settings.json"), kind], check=True, timeout=5)
