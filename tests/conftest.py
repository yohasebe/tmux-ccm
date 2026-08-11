"""Shared test infrastructure: lib path setup, autouse fixtures,
and helpers used across the per-module test files."""

import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest

# Make lib/ importable so each test file can `import ccm_core` etc.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))


# ─── Live-subprocess guard ───
# ccm's whole reason to exist is driving a tmux server. A test that
# forgets to stub `tmux_cmd` / `tmux_batch` / `ps_snapshot` therefore
# does not just read the developer's LIVE tmux server — it can
# mutate it (this actually happened: `update_window_names` renamed
# real windows to `● IDLE` during a pytest run, and mode-2 render
# tests issued `set -g status 3` against the live server). CLAUDE.md
# mandates "tmux/ps は mock、subprocess 呼び出しなし"; this fixture
# makes that policy enforceable instead of aspirational.

#: External commands a test must never execute for real. Stub the ccm
#: helper that shells out instead (`ccm_core.tmux_cmd`, `tmux_batch`,
#: `ps_snapshot`, `ccm_notify.notify`, ...).
BLOCKED_SUBPROCESS_COMMANDS = frozenset({
    "tmux",
    "ps",
    "jq",
    "osascript",
    "terminal-notifier",
    "notify-send",
})


class LiveSubprocessCall(BaseException):
    """A test invoked a blocked external command for real.

    Inherits from BaseException (not Exception) on purpose: the call
    sites most likely to leak a live subprocess — `inject_status`,
    `Dashboard._refresh_loop`, `tmux_batch` — wrap their bodies in
    broad `except Exception:` / `log_caught_exception` barriers, and
    a normal exception would be silently swallowed there, turning the
    guard into a no-op exactly where it matters most.
    """


def _blocked_command(argv):
    """Return the blocked basename `argv` would exec, or None."""
    if isinstance(argv, str):
        # shell=True string form: take the first token.
        argv = argv.split()
    if not argv:
        return None
    base = os.path.basename(str(argv[0]))
    return base if base in BLOCKED_SUBPROCESS_COMMANDS else None


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_subprocess: allow this test to execute external "
        "commands for real (opt-out of the block_live_subprocess "
        "guard; last resort for tests that cannot be stubbed)",
    )


@pytest.fixture(autouse=True)
def block_live_subprocess(request, monkeypatch):
    """Fail fast when test code shells out to tmux/ps/jq/osascript/
    terminal-notifier/notify-send against the real environment.

    Patches `subprocess.run` and `subprocess.Popen` at the module
    level. Tests that stub those functions themselves patch AFTER
    this fixture (fixture setup runs before the test body), so their
    mocks take precedence and never see the guard; only genuinely
    unstubbed calls trip it. Non-blocked commands (e.g. `git` in a
    tmp repo) delegate to the real implementation.

    Opt out per-test with `@pytest.mark.live_subprocess`."""
    if request.node.get_closest_marker("live_subprocess"):
        yield
        return

    # `display_width` asks tmux once per process what the terminal
    # makes of ambiguous-width glyphs. Seed that answer so measuring a
    # string — which nearly every test does — is not a tmux round trip.
    # Tests about the resolution itself stub `tmux_cmd` and call
    # `_resolve_ambiguous_width` directly.
    import ccm_render
    monkeypatch.setattr(ccm_render, "_AMBIGUOUS_STATE", (1, False),
                        raising=False)

    import subprocess
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def _guard(argv, via):
        cmd = _blocked_command(argv)
        if cmd:
            raise LiveSubprocessCall(
                f"test invoked live `{cmd}` via {via}: "
                f"{list(argv) if not isinstance(argv, str) else argv!r}. "
                "Tests must not touch the real tmux server / process "
                "table / notification daemons (CLAUDE.md test policy). "
                "Stub the ccm helper that shells out (tmux_cmd, "
                "tmux_batch, ps_snapshot, ...); use "
                "@pytest.mark.live_subprocess only as a last resort."
            )

    def guarded_run(args, *a, **kw):
        _guard(args, "subprocess.run")
        return real_run(args, *a, **kw)

    class GuardedPopen(real_popen):
        def __init__(self, args, *a, **kw):
            _guard(args, "subprocess.Popen")
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

    # tmux exports `$TMUX_PANE` into every process started inside a
    # pane, so a developer running pytest from a ccm-managed window
    # leaks their real pane id into the code under test — `ccm send`'s
    # self-delivery guard compares against it. Unset it so behaviour
    # cannot depend on which pane the suite was launched from (it is
    # absent on CI, so leaving it would be exactly the local-green /
    # CI-red asymmetry this fixture exists to prevent). Tests that
    # exercise pane identity set it explicitly.
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield


@pytest.fixture(autouse=True)
def isolate_errors_log(tmp_path, monkeypatch):
    """Redirect the silent-exception log away from the user's real
    `$TMPDIR/ccm-$UID/errors.log` for every test.

    `CCM_ERRORS_LOG` is computed at import time from `CCM_TMP_DIR`,
    so tests that monkeypatch `CCM_TMP_DIR` do NOT redirect the
    error log — any test that exercises a silent-catch path (or has
    a bug in its scaffolding) would append garbage entries to the
    real log that `ccm errors` then shows the user as if they were
    production failures. This actually happened: a stale
    `Dashboard.__new__` scaffold in test_silent_exceptions.py was
    missing the `bg_visible` attribute, and the resulting
    AttributeError was logged to the real errors.log on every test
    run, masquerading as a production dashboard bug.
    """
    import ccm_core
    monkeypatch.setattr(ccm_core, "CCM_ERRORS_LOG",
                        str(tmp_path / "errors.log"))
    monkeypatch.setattr(ccm_core, "CCM_ERRORS_LOG_PREV",
                        str(tmp_path / "errors.log.1"))
    yield


@pytest.fixture(autouse=True)
def isolate_hook_silence_log(tmp_path, monkeypatch):
    """Redirect the hook-silence firing log (and its adjacent
    `.markers/` rate-limit dir) away from the user's real data dir
    for every test.

    The firing log is the *evidence dataset* for the canary's
    default-on promotion — a test-generated record would masquerade
    as a real-session firing and poison exactly the data the
    observe-first phase exists to collect. `hook_silence_log_path()`
    reads the env var at call time, so setenv is sufficient."""
    monkeypatch.setenv("CCM_HOOK_SILENCE_LOG",
                       str(tmp_path / "hook-silence.log"))
    yield


# ─── JSONL helpers ───
# Used by tests that simulate Claude Code's per-session JSONL log.

def iso_ts(unix_ts):
    """Format a unix timestamp as the ISO 8601 string Claude Code
    writes into JSONL records (UTC, milliseconds, trailing Z)."""
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def write_jsonl(path, records):
    """Write a list of dict records as one JSON-per-line to `path`."""
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def real_activity_record(unix_ts, role="assistant"):
    """Build a minimal user/assistant JSONL record at the given ts."""
    return {"type": role, "timestamp": iso_ts(unix_ts),
            "message": {"content": "x"}}


def system_record(unix_ts, subtype="away_summary"):
    """Build a minimal system metadata record (e.g. recap)."""
    return {"type": "system", "subtype": subtype, "timestamp": iso_ts(unix_ts)}


# ─── ps snapshot helpers ───

def make_ps_lines(*entries):
    """Build ps output lines. Each entry: (pid, ppid, pgid, comm)."""
    lines = ["  PID  PPID  PGID COMM"]
    for pid, ppid, pgid, comm in entries:
        lines.append(f"  {pid}   {ppid}   {pgid} {comm}")
    return lines


# ─── DetectionContext factory ───

# Set of states `evaluate_rules` / `derive_state_from_events` are
# allowed to commit. Any rule resolution outside this set means a
# bug in DETECTION_RULES (the dashboard renderer assumes it sees only
# these). Used by Pipeline / Derive invariant tests.
VALID_RESOLVED_STATES = frozenset({"SHELL", "DOWN", "BUSY", "IDLE", "PERMIT"})


def make_ctx(**overrides):
    """Build a DetectionContext with sensible defaults for rule
    testing. Optional fields (`jsonl_last_stop_reason`,
    `claude_pid_age`) are NOT in the defaults so tests can opt in
    via overrides; tests that omit them inherit dataclass defaults.

    Imported lazily because conftest is loaded before any test file
    has had a chance to `import ccm_rules` — taking the chain at
    conftest load time (`ccm_rules → ccm_core → ccm_commands →
    ccm_detection → ccm_rules`) hits the partial-module wall."""
    import ccm_rules  # deferred past the circular-load window
    now = int(time.time())
    defaults = dict(
        raw="IDLE",
        hook_state="",
        hook_ts=0,
        hook_age=-1,
        prev_state="IDLE",
        jsonl_age=-1,
        now=now,
    )
    defaults.update(overrides)
    return ccm_rules.DetectionContext(**defaults)
