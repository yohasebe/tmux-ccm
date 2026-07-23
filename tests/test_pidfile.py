"""Tests for dashboard.acquire_pidfile PID-identity verification.

A pidfile left behind by a crashed dashboard holds a stale PID, and
the OS may have recycled that PID for an unrelated process. The old
code SIGTERM→SIGKILL'd it blind; `acquire_pidfile` now verifies via
`_pid_is_dashboard` (ps command-line probe) before signalling.

The conftest `block_live_subprocess` guard forbids real `ps` runs,
so every test stubs `_pid_is_dashboard` (or `subprocess.run` for the
probe's own unit tests) — never touches the real process table.
`os.kill` is likewise stubbed; no real signal is ever sent.
"""

import os
import signal
import sys
import types

import pytest

import dashboard


class TestAcquirePidfile:
    def _setup(self, tmp_path, monkeypatch, pidfile_content=None,
               is_dashboard=True):
        """Redirect the pidfile into tmp_path, stub os.kill / sleep /
        the identity probe, and return the recorded kill calls."""
        monkeypatch.setattr(dashboard, "CCM_TMP_DIR", str(tmp_path))
        if pidfile_content is not None:
            (tmp_path / "dashboard.pid").write_text(pidfile_content)
        kills = []
        monkeypatch.setattr(dashboard.os, "kill",
                            lambda pid, sig: kills.append((pid, sig)))
        monkeypatch.setattr(dashboard.time, "sleep", lambda _s: None)
        monkeypatch.setattr(dashboard, "_pid_is_dashboard",
                            lambda pid: is_dashboard)
        return kills

    def test_stale_dashboard_pid_is_killed(self, tmp_path, monkeypatch):
        kills = self._setup(tmp_path, monkeypatch, "424242",
                            is_dashboard=True)
        dashboard.acquire_pidfile()
        sigs = [s for _p, s in kills]
        assert kills[0] == (424242, signal.SIGTERM)
        assert signal.SIGKILL in sigs

    def test_recycled_pid_is_not_killed(self, tmp_path, monkeypatch):
        """PID reuse: the stale pidfile's PID now belongs to an
        unrelated process — no signal may be sent to it."""
        kills = self._setup(tmp_path, monkeypatch, "424242",
                            is_dashboard=False)
        dashboard.acquire_pidfile()
        assert kills == []

    def test_own_pid_is_never_killed(self, tmp_path, monkeypatch):
        kills = self._setup(tmp_path, monkeypatch, str(os.getpid()),
                            is_dashboard=True)
        dashboard.acquire_pidfile()
        assert kills == []

    def test_malformed_pidfile_is_ignored(self, tmp_path, monkeypatch):
        kills = self._setup(tmp_path, monkeypatch, "not-a-pid",
                            is_dashboard=True)
        pidfile = dashboard.acquire_pidfile()
        assert kills == []
        assert (tmp_path / "dashboard.pid").read_text() == str(os.getpid())
        assert pidfile == os.path.join(str(tmp_path), "dashboard.pid")

    def test_no_pidfile_just_writes(self, tmp_path, monkeypatch):
        kills = self._setup(tmp_path, monkeypatch, None)
        dashboard.acquire_pidfile()
        assert kills == []
        assert (tmp_path / "dashboard.pid").read_text() == str(os.getpid())

    def test_pidfile_always_rewritten_with_own_pid(
            self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, "424242", is_dashboard=False)
        dashboard.acquire_pidfile()
        assert (tmp_path / "dashboard.pid").read_text() == str(os.getpid())


class TestPidIsDashboard:
    """The ps-based identity probe. `subprocess.run` is stubbed
    (conftest blocks real `ps`); the probe must fail SAFE — any error
    means "not verified", i.e. False, so nothing gets killed."""

    def _stub_ps(self, monkeypatch, stdout="", raises=None):
        def fake_run(argv, *a, **kw):
            if raises is not None:
                raise raises
            assert argv == ["ps", "-p", "1234", "-o", "command="]
            return types.SimpleNamespace(stdout=stdout)
        monkeypatch.setattr(dashboard.subprocess, "run", fake_run)

    def test_dashboard_cmdline_matches(self, monkeypatch):
        self._stub_ps(monkeypatch,
                      stdout="python3 /home/u/ccm/lib/dashboard.py\n")
        assert dashboard._pid_is_dashboard(1234) is True

    def test_unrelated_process_does_not_match(self, monkeypatch):
        self._stub_ps(monkeypatch, stdout="/usr/sbin/sshd -D\n")
        assert dashboard._pid_is_dashboard(1234) is False

    def test_dead_pid_empty_output(self, monkeypatch):
        self._stub_ps(monkeypatch, stdout="")
        assert dashboard._pid_is_dashboard(1234) is False

    def test_ps_failure_fails_safe(self, monkeypatch):
        self._stub_ps(monkeypatch, raises=OSError("ps missing"))
        assert dashboard._pid_is_dashboard(1234) is False

    def test_ps_timeout_fails_safe(self, monkeypatch):
        import subprocess as sp
        self._stub_ps(monkeypatch,
                      raises=sp.TimeoutExpired(cmd="ps", timeout=5))
        assert dashboard._pid_is_dashboard(1234) is False
