"""Tests for ccm_notify.

Auto-split from test_ccm_core.py. Shared fixtures + helpers
(write_jsonl, make_ps_lines, real_activity_record, system_record,
iso_ts) live in conftest.py; import them here when used.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

import pytest

import ccm_core
import ccm_activity
import ccm_canaries
import ccm_commands
import ccm_detection
import ccm_jsonl
import ccm_notify
import ccm_pane_state
import ccm_render
import ccm_rules
import ccm_runtime
import ccm_signals

from conftest import (
    iso_ts,
    make_ctx,
    make_ps_lines,
    real_activity_record,
    system_record,
    write_jsonl,
)

# Backward-compat alias used by some tests.
_iso_ts = iso_ts

class TestClearNotificationsScope:
    def test_returns_minus_one_when_terminal_notifier_missing(self, monkeypatch):
        monkeypatch.setattr(ccm_notify, "_terminal_notifier_path", lambda: None)
        assert ccm_notify.clear_notifications() == -1

    def test_removes_only_ccm_prefixed_groups(self, monkeypatch):
        listing_stdout = (
            "GroupID\tTitle\tSubtitle\tMessage\tDelivered At\n"
            "ccm-alpha\tccm ⚠ alpha\t\tPermission required\t2024-01-01 10:00:00 +0000\n"
            "ccm-beta\tccm ⚠ beta\t\tPermission required\t2024-01-01 10:01:00 +0000\n"
            "deploy-alert\tDeploy succeeded\t\tprod\t2024-01-01 10:02:00 +0000\n"
            "monitoring-cpu\tHigh CPU\t\t\t2024-01-01 10:03:00 +0000\n"
        ).encode("utf-8")
        monkeypatch.setattr(ccm_notify, "_terminal_notifier_path", lambda: "/fake/tn")

        calls = []

        class _Result:
            def __init__(self, stdout=b""):
                self.stdout = stdout

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[1:3] == ["-list", "ALL"]:
                return _Result(stdout=listing_stdout)
            return _Result(stdout=b"")

        monkeypatch.setattr(ccm_core.subprocess, "run", fake_run)

        rc = ccm_notify.clear_notifications()
        assert rc == 2  # only 2 ccm-prefixed groups
        # First call enumerates
        assert calls[0][1:3] == ["-list", "ALL"]
        # Subsequent removes target only ccm- ids — `[tn_path, "-remove", group_id]`
        remove_targets = [c[2] for c in calls[1:]]
        assert remove_targets == ["ccm-alpha", "ccm-beta"]
        assert "deploy-alert" not in remove_targets
        assert "monitoring-cpu" not in remove_targets

    def test_returns_zero_when_no_ccm_notifications(self, monkeypatch):
        listing_stdout = (
            "GroupID\tTitle\tSubtitle\tMessage\tDelivered At\n"
            "deploy-alert\tDeploy succeeded\t\tprod\t2024-01-01 10:00:00 +0000\n"
        ).encode("utf-8")
        monkeypatch.setattr(ccm_notify, "_terminal_notifier_path", lambda: "/fake/tn")

        class _Result:
            def __init__(self, stdout=b""):
                self.stdout = stdout

        def fake_run(args, **kwargs):
            if args[1:3] == ["-list", "ALL"]:
                return _Result(stdout=listing_stdout)
            return _Result(stdout=b"")

        monkeypatch.setattr(ccm_core.subprocess, "run", fake_run)
        assert ccm_notify.clear_notifications() == 0

    def test_returns_minus_one_when_listing_fails(self, monkeypatch):
        monkeypatch.setattr(ccm_notify, "_terminal_notifier_path", lambda: "/fake/tn")

        def fake_run(args, **kwargs):
            raise OSError("simulated")

        monkeypatch.setattr(ccm_core.subprocess, "run", fake_run)
        assert ccm_notify.clear_notifications() == -1


# ─── notify() dispatch + escaping ───
# The three delivery branches (terminal-notifier / osascript /
# notify-send) each hand a PERMIT `detail` — arbitrary tool text like
# `Bash: rm -rf "x"` — to a different quoting regime. The osascript
# branch embeds title/body into an AppleScript string literal, where
# an unescaped `"` or `\` silently corrupts the script (the
# notification simply never appears, and nothing in the suite would
# notice). These tests stub subprocess.Popen and pin the exact argv
# each branch receives.


class _PopenRecorder:
    """subprocess.Popen stand-in: records argv, optionally raises
    FileNotFoundError for chosen commands to simulate a missing
    binary."""

    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if os.path.basename(str(args[0])) in self.fail_for:
            raise FileNotFoundError(str(args[0]))
        return MagicMock()

    def commands(self):
        return [os.path.basename(str(c[0])) for c in self.calls]


def _stub_notify_env(monkeypatch, tn_path=None, **settings):
    """Mute tmux settings + the terminal-notifier probe, and install
    a Popen recorder. Returns the recorder."""
    fail_for = settings.pop("_fail_for", ())
    opts = {
        "@ccm-notify": "permit,completed",
        "@ccm-notify-sound": "off",
        "@ccm-notify-sound-name": "Glass",
    }
    opts.update(settings)
    monkeypatch.setattr(ccm_core, "tmux_cmd",
                        lambda *a, **k: opts.get(a[-1], ""))
    monkeypatch.setattr(ccm_notify, "_terminal_notifier_path",
                        lambda: tn_path)
    recorder = _PopenRecorder(fail_for=fail_for)
    monkeypatch.setattr(ccm_notify.subprocess, "Popen", recorder)
    return recorder


class TestNotifyDispatch:
    def test_terminal_notifier_branch_passes_argv_verbatim(self, monkeypatch):
        """terminal-notifier takes argv (no shell), so the detail must
        arrive byte-for-byte — no escaping applied, none needed."""
        rec = _stub_notify_env(monkeypatch, tn_path="/fake/tn")
        ccm_notify.notify("PERMIT", "proj", 'Bash: rm -rf "quoted dir"')
        assert rec.commands() == ["tn"]
        args = rec.calls[0]
        assert args[0] == "/fake/tn"
        assert args[1:3] == ["-message",
                             'Permission required: Bash: rm -rf "quoted dir"']
        assert args[3:5] == ["-title", "ccm ⚠ proj"]
        assert args[5:7] == ["-group", "ccm-proj"]

    def test_terminal_notifier_sound_flag_appended(self, monkeypatch):
        rec = _stub_notify_env(monkeypatch, tn_path="/fake/tn",
                               **{"@ccm-notify-sound": "on"})
        ccm_notify.notify("PERMIT", "proj", "")
        args = rec.calls[0]
        assert args[-2:] == ["-sound", "Glass"]

    def test_terminal_notifier_oserror_falls_through_to_osascript(self, monkeypatch):
        rec = _stub_notify_env(monkeypatch, tn_path="/fake/tn",
                               _fail_for=("tn",))
        ccm_notify.notify("PERMIT", "proj", "")
        assert rec.commands() == ["tn", "osascript"]

    def test_osascript_escapes_quotes_and_backslashes(self, monkeypatch):
        r"""The AppleScript branch embeds body/title in double-quoted
        string literals. A `"` or `\` in the tool detail must be
        escaped or the generated script is syntactically broken and
        the notification silently never fires."""
        rec = _stub_notify_env(monkeypatch, tn_path=None)
        ccm_notify.notify("PERMIT", "proj",
                          'Bash: rm -rf "report.txt" from C:\\tmp')
        assert rec.commands() == ["osascript"]
        script = rec.calls[0][2]
        assert script == (
            'display notification "Permission required: '
            'Bash: rm -rf \\"report.txt\\" from C:\\\\tmp" '
            'with title "ccm ⚠ proj"'
        )
        # Once the escaped sequences are removed, only the 4
        # structural quotes (body open/close, title open/close)
        # remain — every quote from the detail was escaped.
        stripped = script.replace('\\"', "").replace("\\\\", "")
        assert stripped.count('"') == 4

    def test_osascript_sound_option(self, monkeypatch):
        rec = _stub_notify_env(monkeypatch, tn_path=None,
                               **{"@ccm-notify-sound": "on"})
        ccm_notify.notify("PERMIT", "proj", "")
        script = rec.calls[0][2]
        assert script.endswith(' sound name "Glass"')

    def test_notify_send_fallback_when_osascript_missing(self, monkeypatch):
        """Linux / minimal installs: no terminal-notifier, osascript
        raises FileNotFoundError → notify-send gets title and body as
        separate argv items (verbatim, no escaping)."""
        rec = _stub_notify_env(monkeypatch, tn_path=None,
                               _fail_for=("osascript",))
        ccm_notify.notify("PERMIT", "proj", 'Bash: rm -rf "x"')
        assert rec.commands() == ["osascript", "notify-send"]
        assert rec.calls[1] == [
            "notify-send", "ccm ⚠ proj",
            'Permission required: Bash: rm -rf "x"',
        ]

    def test_all_backends_missing_is_silent(self, monkeypatch):
        rec = _stub_notify_env(monkeypatch, tn_path=None,
                               _fail_for=("osascript", "notify-send"))
        ccm_notify.notify("PERMIT", "proj", "")  # must not raise
        assert rec.commands() == ["osascript", "notify-send"]

    def test_unknown_state_dispatches_nothing(self, monkeypatch):
        rec = _stub_notify_env(monkeypatch, tn_path="/fake/tn",
                               **{"@ccm-notify": "all"})
        ccm_notify.notify("NOPE", "proj", "")
        assert rec.calls == []


