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


