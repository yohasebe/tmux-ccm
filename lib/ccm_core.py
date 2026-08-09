#!/usr/bin/env python3
"""ccm core — subprocess helpers, observability, project model, CLI dispatch.

Pure constants, regex patterns, and the PERMIT-modal classifier
live in `ccm_constants` so submodules can import the values they
need without triggering ccm_core's body. Everything in this file
either has I/O side effects (`tmux_cmd`, `ps_snapshot`,
`log_caught_exception`), business logic (`build_project_list`,
`Project`), or is the entry-point dispatcher.
"""

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

# Single-instance guarantee. When invoked as `python3 lib/ccm_core.py
# <subcmd>`, Python loads this file as `__main__`. Submodules
# (ccm_commands, ccm_detection, …) imported at the bottom of this
# file then `import ccm_core` and find no entry in sys.modules under
# that name — Python re-executes ccm_core.py a second time as
# "ccm_core", giving us two instances of every module-level state.
# Aliasing __main__ to "ccm_core" turns that second load into a
# no-op so module-level dicts (e.g. log-file path, error counters)
# stay singletons. Harmless when imported normally.
sys.modules.setdefault("ccm_core", sys.modules[__name__])

# Pure constants (paths, thresholds, regex patterns, state metadata,
# PERMIT-modal classifier) live in ccm_constants. Pulled in as bare
# locals so this module's own body can use unqualified names; other
# modules import directly from ccm_constants.
from ccm_constants import (  # noqa: F401 (used as module-local names)
    ATTENTION_RESOLVED_GC_SEC,
    ATTENTION_WAITING_TTL_SEC,
    BUSY_HOOK_JSONL_WINDOW,
    CACHE_TTL,
    CCM_ATTENTION_DIR,
    CCM_DATA_DIR,
    CCM_GIT_CACHE_DIR,
    CCM_HOOK_DIR,
    CCM_NOTIFY_MARKER_DIR,
    CCM_PORT_CACHE_DIR,
    CCM_ROOT,
    CCM_SNAPSHOT_DIR,
    CCM_TMP_DIR,
    CLAUDE_CMD,
    CLAUDE_PROCESS_NAME,
    COMPLETED_AT_TIMEOUT,
    EXTERNAL_AGENT_COMMANDS,
    external_agent_name,
    HOOK_FRESH_THRESHOLD,
    HOOK_SCRIPTS,
    IDLE_EXIT_TIMEOUT,
    IGNORED_CHILDREN,
    PATTERN_ACCEPT_EDITS,
    PATTERN_AGENTS_FOOTER,
    PATTERN_INPUT_PROMPT,
    PATTERN_MODEL_PICKER,
    PATTERN_PERMISSION_DIALOG,
    PATTERN_PERMIT_FOOTER,
    PATTERN_RESUME_MODAL,
    PERMIT_MAX_TIMEOUT,
    SHELL_FOREGROUND_COMMANDS,
    SLIVER_HEIGHT_THRESHOLD,
    STARTUP_GRACE_SEC,
    STATE_ICONS,
    STATE_PRIORITY,
    classify_permit_modal,
    is_agents_tui,
)


# ─── Subprocess helpers ───

def tmux_cmd(*args, timeout=5):
    """Run tmux command, return stdout.

    Decodes output with `errors="replace"` because `capture-pane`
    can emit byte sequences that are not valid UTF-8 — terminal
    escape codes, half-rendered multi-byte chars, or random binary
    that another program wrote to the pane. A decode error here
    would propagate up and silently kill the entire detection
    cycle (inject_status / dashboard refresh), leaving every
    project's `@ccm_prev_state` frozen.
    """
    try:
        r = subprocess.run(
            ["tmux"] + list(args), capture_output=True, timeout=timeout
        )
        if r.returncode != 0:
            return ""
        return r.stdout.decode("utf-8", errors="replace").strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def tmux_batch(*commands):
    """Run multiple tmux commands in a single subprocess call.
    Each command is a tuple of args. Commands are joined with ';' separator.

    Failure visibility: a non-zero exit is recorded in the
    silent-exception log. This matters more for batches than for
    single commands — when ANY command in a `;`-chain is invalid,
    tmux aborts the REST of the chain too, so one bad value silently
    drops every subsequent write. That is exactly how the 2026-07-11
    frozen-status-bar incident stayed invisible for days: `set -g
    status 6` (above tmux's max of 5) was rejected and took all the
    status-format writes down with it, every second, with stderr
    swallowed here. Logging turns the next such bug into an
    `errors.log` burst the canaries surface within minutes.
    """
    if not commands:
        return
    args = ["tmux"]
    for i, cmd in enumerate(commands):
        if i > 0:
            args.append(";")
        args.extend(cmd)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            try:
                raise RuntimeError(
                    f"tmux batch failed rc={r.returncode}: "
                    f"{(r.stderr or '').strip()[:200]!r} "
                    f"first_cmds={[tuple(c[:4]) for c in commands[:3]]}"
                )
            except RuntimeError:
                log_caught_exception("tmux_batch")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def ps_snapshot():
    """Single ps call for scan cycle.

    Output columns: pid ppid pgid comm etime. `etime` is appended at
    the end so the existing `parts[0..3]` positions for pid / ppid /
    pgid / comm are unchanged — all process-tree helpers
    (find_claude_pid, has_children) parse by index without
    modification. `etime` is consumed only by `find_process_age`
    below, which distinguishes Claude's startup window from
    steady-state operation.

    macOS `ps` truncates the `comm` column at a fixed byte width,
    which can slice a multi-byte UTF-8 character in half (e.g. an
    app named `⌘英かな`). Decoding the raw bytes with
    `errors="replace"` keeps the line intact (truncated names are
    only used for prefix matching, not displayed) instead of
    raising and crashing the entire detection cycle.
    """
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,ppid,pgid,comm,etime"],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return ""
        return r.stdout.decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _parse_etime(etime: str) -> int:
    """Parse a BSD/GNU `ps etime` field to seconds.

    Supported formats (as emitted by macOS / Linux ps):
      - "SS"              single field, seconds only
      - "MM:SS"
      - "HH:MM:SS"
      - "DD-HH:MM:SS"     days separated by '-'
    Returns -1 on any parse error so detection rules using
    claude_pid_age_lt cleanly skip the match when the runtime
    couldn't be determined.
    """
    if not etime:
        return -1
    try:
        days = 0
        if "-" in etime:
            ds, etime = etime.split("-", 1)
            days = int(ds)
        parts = [int(p) for p in etime.split(":")]
        if len(parts) == 1:
            return days * 86400 + parts[0]
        if len(parts) == 2:
            return days * 86400 + parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (ValueError, TypeError):
        pass
    return -1


def find_process_age(pid, ps_lines):
    """Return seconds since the process `pid` started, or -1 if the
    pid is not in the ps snapshot or its etime column is absent /
    unparseable. Reads the `etime` column from the ps output
    produced by `ps_snapshot` (position `parts[4]`)."""
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 5 and parts[0] == str(pid):
            return _parse_etime(parts[4])
    return -1


def md5_hash(s):
    return hashlib.md5(s.encode()).hexdigest()


def shorten_home(path):
    """Replace a leading `$HOME` path component with `~` for display
    and snapshot portability.

    Prefix-only: a naive `path.replace(home, "~")` rewrites EVERY
    occurrence, so with HOME=/Users/x the path `/Users/x2/work` would
    become `~2/work` — which no longer expands back to a real
    directory (snapshot load then fails with "Directory not found")
    and mid-path occurrences render wrongly
    (`/Users/alice/code/Users/alice-backup` → `~/code~-backup`).
    Only an exact match or a `home + os.sep` prefix is shortened."""
    if not path:
        return path
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


# ─── Silent-exception observability ───
# `inject_status` and `dashboard._refresh_loop` both wrap their main
# body in `except Exception: pass` because crashing the tmux status
# refresh or the dashboard render loop is worse than skipping a tick.
# But silent swallowing kept the recent UTF-8 decode regression
# invisible until users noticed states freezing across every project.
# `log_caught_exception` records each silent catch so the next
# regression of this class is debuggable without having to enable
# `CCM_DEBUG_TRACE` in advance. The log is size-capped so a runaway
# loop cannot exhaust disk.

CCM_ERRORS_LOG = os.path.join(CCM_TMP_DIR, "errors.log")
CCM_ERRORS_LOG_PREV = CCM_ERRORS_LOG + ".1"
ERRORS_LOG_MAX_BYTES = int(
    os.environ.get("CCM_ERRORS_LOG_MAX_BYTES", str(1 * 1024 * 1024))
)


def log_caught_exception(scope: str) -> None:
    """Append one JSON line describing a silently-caught exception.

    Must be called from inside an `except` block — reads
    `sys.exc_info()`. `scope` identifies the call site so the log
    distinguishes "every project breaks" from "one project breaks"
    (e.g. `"inject_status"`, `"build_project_list[my-project]"`).

    Rotation: when the active log reaches `ERRORS_LOG_MAX_BYTES`
    it is renamed to `<log>.1` and a fresh log starts. Total disk
    use is bounded at 2 × cap (~2 MB by default). The previous
    epoch is preserved so a regression that fills the log with
    fast-firing repeats does not erase the original cause.
    Tunable via `CCM_ERRORS_LOG_MAX_BYTES`.

    Best-effort: a failure to write the log is itself swallowed,
    because turning a survivable detection error into a fatal one
    would defeat the purpose of the silent-catch barriers.
    """
    try:
        exc_type, exc, tb = sys.exc_info()
        if exc_type is None:
            return
        os.makedirs(os.path.dirname(CCM_ERRORS_LOG), exist_ok=True)
        try:
            size = os.path.getsize(CCM_ERRORS_LOG)
        except OSError:
            size = 0
        if size >= ERRORS_LOG_MAX_BYTES:
            try:
                os.replace(CCM_ERRORS_LOG, CCM_ERRORS_LOG_PREV)
            except OSError:
                pass
        record = {
            "ts": int(time.time()),
            "scope": scope,
            "type": exc_type.__name__,
            "msg": str(exc),
            "traceback": "".join(traceback.format_tb(tb)),
        }
        with open(CCM_ERRORS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


# ─── Session detection ───

def get_session():
    popup_file = os.path.join(CCM_TMP_DIR, "popup-session")
    try:
        if os.path.exists(popup_file):
            age = time.time() - os.path.getmtime(popup_file)
            if age < 60:
                with open(popup_file, encoding="utf-8") as f:
                    return f.read().strip()
    except OSError:
        pass
    session = tmux_cmd("display-message", "-p", "#{session_name}")
    if session:
        return session
    out = tmux_cmd("list-clients", "-F", "#{session_name}")
    return out.split("\n")[0] if out else ""


def touch_popup_session():
    path = os.path.join(CCM_TMP_DIR, "popup-session")
    try:
        os.utime(path, None)
    except OSError:
        pass


# ─── Project data ───

class Project:
    __slots__ = (
        "win_target", "win_idx", "name", "dir", "state",
        "branch", "ports", "completed_at", "bg_active", "pane_count",
        "ignored_panes", "permission_mode", "sort_key",
        "cached_session_id", "external_agents", "attention_agents",
    )

    def __init__(self, win_target, win_idx, name, directory, state,
                 branch="", ports="", completed_at=0, bg_active=False,
                 pane_count=1, permission_mode="", ignored_panes=0,
                 cached_session_id=None, external_agents=(),
                 attention_agents=()):
        self.win_target = win_target
        self.win_idx = win_idx
        self.name = name
        self.dir = directory
        self.state = state
        self.branch = branch
        self.ports = ports
        self.completed_at = completed_at
        self.bg_active = bg_active
        # Number of tmux panes in this window. Surfaces a `[N]`
        # marker (brackets dim, digit cyan) on multi-pane windows
        # so users notice split-pane windows (Agent Teams, casual
        # splits, orphan panes). Always >= 1; populated by
        # build_project_list from panes_cache.
        self.pane_count = pane_count
        # Number of panes in this window hidden via CCM_IGNORE /
        # `ccm ignore`. > 0 → a `⊘` marker on the row (a sidekick
        # session is running but untracked). 0 on the fast path.
        self.ignored_panes = ignored_panes
        # Raw `permission_mode` payload value from the newest
        # mode-bearing hook event ("" when unknown). Display-only —
        # rendered as a badge by `ccm status` / the dashboard via
        # `permission_mode_label`; never consulted by detection.
        self.permission_mode = permission_mode
        # `@ccm_session_id` value bulk-fetched by build_project_list's
        # single `list-windows` query. Carried on the Project so
        # display-layer signal readers (signal_age_suffix et al.) can
        # key hook files without a per-project tmux subprocess (N+1
        # avoidance). Semantics mirror `_parse_window_line`:
        # non-empty → use, "" → authoritative "no session", None →
        # not fetched (constructed outside build_project_list).
        self.cached_session_id = cached_session_id
        # Tuple of foreground command names (one per pane) matching
        # EXTERNAL_AGENT_COMMANDS — external agent CLI sessions
        # running in this window's panes. Display-only presence
        # signal (dim `⚙<name>` badge / `(name)` note on SHELL
        # rows); detection never reads it. Populated from the bulk
        # panes_cache (zero extra subprocesses); () on the fast
        # path, mirroring pane_count=1 / ignored_panes=0 there.
        self.external_agents = external_agents
        # Agent names (subset of external_agents) whose pane currently
        # holds a `waiting` attention marker — the sidekick is blocked
        # on a decision. Drives the ⚙ badge's attention colour and
        # nothing else: NOT part of the 4-state model, never consulted
        # by detection, () on the fast path.
        self.attention_agents = attention_agents
        self.sort_key = (STATE_PRIORITY.get(state, 4), -(completed_at or 0))


def canonical_dir(path):
    """Canonical form of a project directory for equality checks
    (expanduser + realpath, falling back to the raw path on error).

    Single source for the identity `build_project_list`'s same-dir
    dedup (`seen_dirs`) uses; the same-dir fallback lookups in
    `ccm_runtime.update_window_names` and `ccm_send.cmd_send` must
    canonicalize identically or they would never match the dedup
    key."""
    expanded = os.path.expanduser(path)
    try:
        return os.path.realpath(expanded)
    except OSError:
        return expanded


def read_cache_file(cache_dir, directory):
    expanded = canonical_dir(directory)
    key = md5_hash(expanded)
    path = os.path.join(cache_dir, key)
    try:
        if os.path.exists(path):
            age = time.time() - os.path.getmtime(path)
            if age < CACHE_TTL:
                with open(path, encoding="utf-8") as f:
                    return f.read().strip()
    except OSError:
        pass
    return ""


# ─── build_project_list (decomposed pipeline) ───
# The list-windows tmux output is parsed once and walked record-by-
# record. Each phase is a small named helper so the orchestrator
# (`build_project_list`) reads as a sequence of intent-named steps
# rather than a 130-line scroll. The helpers are private and only
# called from `build_project_list` itself.

# Tab-separated list-windows format. Update both this string and
# `_WINDOW_FIELDS` together — the helper that parses the output
# indexes by position into both.
_WINDOW_FORMAT = (
    "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_dir}\t"
    "#{@ccm_prev_state}\t#{@ccm_completed_at}\t#{window_activity}\t"
    "#{@ccm_bg_active}\t#{@ccm_session_id}\t#{@ccm-sidekick-attention}\t"
    "#{@ccm_pane_order}"
)
_WINDOW_FIELDS_MIN = 6  # win_target, project, dir, prev_state, completed_at, win_activity


def _force_mock_state() -> bool:
    """Honour the screenshots/demo override that pins state to
    `@ccm_prev_state` without invoking the detection pipeline."""
    return (os.environ.get("CCM_MOCK_STATE") == "1"
            or tmux_cmd("show-option", "-gqv", "@ccm-mock-state") == "1")


def _build_panes_cache():
    """One bulk `list-panes -a` query returning 7-tuples
    `(target, pid, pane_id, current_command, pane_active, pane_height,
    ignore)` for every pane across every session. Used by detection
    and pane-count phases. `ignore` is the `@ccm_ignore` pane option
    ("1" when the pane hosts a CCM_IGNORE'd session, "" otherwise) —
    carried here so every detection consumer can drop the pane without
    an extra tmux call. Empty tuple in fast mode (caller skips ps
    too)."""
    panes_raw = tmux_cmd(
        "list-panes", "-a", "-F",
        "#{session_name}:#{window_index}\t#{pane_pid}\t#{pane_id}\t"
        "#{pane_current_command}\t#{pane_active}\t#{pane_height}\t"
        "#{@ccm_ignore}"
    )
    cache = []
    for line in panes_raw.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 6:
            # Pad the ignore field so older tmux output (6 fields)
            # stays valid — treated as "not ignored".
            while len(parts) < 7:
                parts.append("")
            cache.append(tuple(parts[:7]))
    return cache


def _pane_is_ignored(pc) -> bool:
    """True when a panes_cache entry carries the `@ccm_ignore` marker.
    Single source of truth for the ignore test so every consumer reads
    the same field position (index 6)."""
    return len(pc) >= 7 and bool(pc[6]) and pc[6] != "0"


def _parse_window_line(line):
    """Parse one row of the list-windows output. Returns a dict of
    field strings, or None if the line is malformed / missing the
    `@ccm_project` tag (bare windows that ccm does not own)."""
    parts = line.split("\t")
    if len(parts) < _WINDOW_FIELDS_MIN:
        return None
    project = parts[1]
    if not project:
        return None
    # @ccm_bg_active and @ccm_session_id are recent additions; older
    # tmux windows may not have them. Treat absent as "no
    # background activity" / "no session known".
    bg_active_str = parts[6] if len(parts) >= 7 else ""
    # `cached_session_id` semantics:
    #   - empty string ""  → fetched, but no session_id cached
    #     (slow path resolves via pid chain; build_detection_context
    #     skips its own show-option since the bulk value is authoritative)
    #   - None             → format string was missing the field
    #     (legacy tmux window, fallback to per-call show-option)
    cached_session_id = parts[7] if len(parts) >= 8 else None
    # Global @ccm-sidekick-attention toggle, piggybacked on the bulk
    # query (global user options resolve in window-format context, so
    # every row carries the same value — one subprocess saved over a
    # dedicated show-option).
    attention_toggle = parts[8] if len(parts) >= 9 else ""
    # Last pane order this window was seen in, as a comma-joined pane
    # id list. Rides the bulk query for the same reason as the two
    # fields above: a per-project show-option would reintroduce the
    # N+1 the subprocess-count tests pin down.
    pane_order = parts[9] if len(parts) >= 10 else ""
    return {
        "win_target": parts[0],
        "project": project,
        "proj_dir": parts[2],
        "prev_state": parts[3],
        "completed_at_str": parts[4],
        "win_activity_str": parts[5],
        "bg_active_str": bg_active_str,
        "cached_session_id": cached_session_id,
        "attention_toggle": attention_toggle,
        "pane_order": pane_order,
    }


def _safe_int(value, default=0):
    """Parse a tmux option value as int, falling back to `default` on
    empty / unset / malformed values."""
    if not value or value == "0":
        return default if not value else 0
    try:
        return int(value)
    except ValueError:
        return default


def _resolve_window_state(row, fast, panes_cache, ps_lines, own_pgid):
    """Run the per-window detection. On failure, carry forward
    `@ccm_prev_state` so a single buggy project does not freeze
    every other project's state."""
    try:
        if fast:
            return ccm_rules.evaluate_fast(
                row["prev_state"], row["proj_dir"],
                session_id=row["cached_session_id"],
            )
        return ccm_detection.detect_window_state(
            row["win_target"], row["proj_dir"], row["prev_state"],
            panes_cache, ps_lines, own_pgid,
            prev_bg_active=bool(row["bg_active_str"]),
            cached_session_id=row["cached_session_id"],
        )
    except Exception:
        log_caught_exception(f"build_project_list[{row['project']}]")
        return row["prev_state"] or "IDLE"


def _pane_ids_in_order(panes_cache, win_target):
    """Pane ids for `win_target` in the order tmux listed them, which
    is pane-index order. Read straight from the bulk cache so the
    reorder observer costs no subprocess of its own."""
    return tuple(pc[2] for pc in panes_cache if pc[0] == win_target)


def _observe_pane_order(row, panes_cache):
    """Feed the reorder observer from a build cycle. Never allowed to
    break a build: this is a bystander recording what it sees, and a
    failure here must not cost the caller its project list."""
    try:
        current = _pane_ids_in_order(panes_cache, row["win_target"])
        if not current:
            return
        ccm_canaries.observe_pane_order(
            row["win_target"], row["project"], row["proj_dir"],
            row.get("pane_order", ""), current, time.time(),
        )
    except Exception:
        log_caught_exception(f"observe_pane_order[{row['project']}]")


def _count_panes(panes_cache, win_target):
    """Count tmux panes belonging to `win_target` in the bulk panes
    cache. Always returns >= 1 (no panes is impossible for a real
    window — defensive default). Counts ALL panes including ignored
    ones (physical truth); the live-ignored subset is counted
    separately by `_resolve_ignored_panes`."""
    return sum(1 for pc in panes_cache if pc[0] == win_target) or 1


def _resolve_ignored_panes(panes_cache, win_target, ps_lines):
    """Number of LIVE ignored panes (`@ccm_ignore` set AND currently
    hosting a claude process) for the window's `⊘` marker — and
    self-heal stale markers.

    `@ccm_ignore` lives on the tmux PANE, but the sidekick it hides is
    a SESSION. When that claude exits, the pane keeps the option (the
    ignored session's own SessionEnd hook early-exits, so nothing
    clears it, and the pane survives as a shell). Left as-is, the `⊘`
    would linger after the sidekick is gone, and a NEW claude later
    launched in that same pane would be silently ignored. So a pane
    marked ignored but no longer hosting claude is stale: its
    `@ccm_ignore` is unset here and it is not counted. A genuinely
    live env-based sidekick (`CCM_IGNORE=1`) re-stamps the marker on
    its next hook fire, so a rare spurious clear self-corrects."""
    count = 0
    for pc in panes_cache:
        if pc[0] != win_target or not _pane_is_ignored(pc):
            continue
        if ccm_detection.find_claude_pid(pc[1], ps_lines):
            count += 1
        else:
            tmux_cmd("set-option", "-p", "-t", pc[2], "-u", "@ccm_ignore")
    return count


def _read_attention_markers(panes_cache, ps_lines):
    """Read every sidekick attention marker once per build and GC the
    dead ones. Returns {pane_id: marker_dict} for markers that are
    live `waiting` — the only state any display surface acts on.

    ccm is the single reader of the attention directory, so it is
    also the garbage collector (the writers are per-CLI hook scripts
    ccm does not control). A marker is unlinked when:
      - state == "resolved" and older than ATTENTION_RESOLVED_GC_SEC
        (kept that long so a slower consumer can still see the
        resolution — the contract is overwrite-then-reap, never
        silent deletion by the writer);
      - its pane no longer exists, or no longer runs an external
        agent (the sidekick exited; same self-heal shape as
        `_resolve_ignored_panes`);
      - `expires` (RFC3339, set by adapters for CLIs without a
        resolution event) has passed, or the hard
        ATTENTION_WAITING_TTL_SEC has — the safety net for an agent
        that died mid-wait without firing anything.
    Unparseable files are treated as dead. All failures are silent:
    attention is a display garnish and must never break a build."""
    try:
        entries = os.listdir(CCM_ATTENTION_DIR)
    except OSError:
        return {}
    import json as _json
    from datetime import datetime, timezone
    now = time.time()
    by_pane = {pc[2]: pc for pc in panes_cache}

    def _pane_hosts_agent(pane_id):
        """Does this pane still host the sidekick its marker came from?

        A pane running a known external agent CLI answers from
        `pane_current_command` alone. A Claude sidekick does NOT:
        the versioned-install symlink makes tmux report the launcher
        name (`2.1.221`, measured), never `claude`, so comparing the
        command to CLAUDE_PROCESS_NAME is false in every real
        environment — `ccm_runtime.auto_exit_idle` documents the same
        quirk and resolves it the same way. Left as a name compare,
        every Claude-sidekick marker was reaped by the next slow build
        and the whole Claude path of the contract silently did
        nothing.

        The process walk runs only for panes that actually have a
        marker (usually none), so it costs nothing on the common
        build."""
        pc = by_pane.get(pane_id)
        if pc is None:
            return False
        if external_agent_name(pc[3]):
            return True
        return bool(ccm_detection.find_claude_pid(pc[1], ps_lines))

    def _epoch(iso):
        try:
            return datetime.fromisoformat(
                iso.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError, TypeError):
            return None

    live = {}
    for entry in entries:
        if not entry.endswith(".json"):
            # Writers stage through `<marker>.json.tmp` before an
            # atomic rename; one killed mid-write leaves that behind,
            # and the `.json` filter would let it accumulate forever.
            # Reaped on age alone — a live stage file is milliseconds
            # old, so anything past the resolved-GC window is debris.
            if entry.endswith(".json.tmp"):
                tmp_path = os.path.join(CCM_ATTENTION_DIR, entry)
                try:
                    if (now - os.path.getmtime(tmp_path)
                            > ATTENTION_RESOLVED_GC_SEC):
                        os.unlink(tmp_path)
                except OSError:
                    pass
            continue
        path = os.path.join(CCM_ATTENTION_DIR, entry)
        pane_id = entry[:-5]
        marker = None
        try:
            with open(path, encoding="utf-8") as f:
                marker = _json.load(f)
        except (OSError, ValueError):
            pass
        reap = False
        if not isinstance(marker, dict):
            reap = True
        elif marker.get("state") == "resolved":
            # A timestamp in the future (clock skew, or a marker
            # copied between machines) would make `now - rts`
            # negative and keep the file forever. Age it from the
            # file's own mtime in that case — the writer's clock
            # cannot lie about when the local filesystem saw it.
            rts = _epoch(marker.get("resolved_ts")) or 0
            if rts > now:
                try:
                    rts = os.path.getmtime(path)
                except OSError:
                    rts = 0
            reap = (now - rts) > ATTENTION_RESOLVED_GC_SEC
        elif marker.get("state") == "waiting":
            ts = _epoch(marker.get("ts"))
            exp = _epoch(marker.get("expires"))
            if not _pane_hosts_agent(pane_id):
                reap = True
            elif exp is not None and now > exp:
                reap = True
            elif ts is None or (now - ts) > ATTENTION_WAITING_TTL_SEC:
                reap = True
            else:
                live[pane_id] = marker
        else:
            reap = True
        if reap:
            try:
                os.unlink(path)
            except OSError:
                pass
    return live


def _resolve_external_agent_panes(panes_cache, win_target):
    """Foreground command names of this window's panes that match
    EXTERNAL_AGENT_COMMANDS — one tuple entry per matching pane.

    Pure read of the bulk panes_cache (field index 3,
    `pane_current_command`): zero extra tmux subprocesses. Unlike
    `_resolve_ignored_panes` there is no live-process check and no
    self-heal — `pane_current_command` is tmux's own live value, so
    a stale entry is impossible. Display-only: the result feeds the
    `⚙<name>` presence badge and the `(name)` note on SHELL rows;
    detection and the state machine never see it."""
    # Canonical short names, not the raw command: a launcher symlink
    # resolves to a platform-suffixed binary, and `⚙grok-macos-aarc`
    # is neither readable nor stable across machines.
    return tuple(
        external_agent_name(pc[3]) for pc in panes_cache
        if pc[0] == win_target and external_agent_name(pc[3])
    )


def build_project_list(fast=False):
    """Build the active project list from tmux state.

    Two paths:
      - fast=True  → statusline path. Uses `@ccm_prev_state` plus a
                     hook-signal read; skips ps / capture-pane /
                     git/port cache I/O. Forced when CCM_MOCK_STATE=1
                     or `@ccm-mock-state` tmux option is set (used by
                     screenshots so visual state is reproducible).
      - fast=False → slow path. Runs the full detection pipeline
                     (process tree + capture-pane + JSONL + event log)
                     and refreshes git/port caches.
    """
    if _force_mock_state():
        fast = True

    raw = tmux_cmd("list-windows", "-a", "-F", _WINDOW_FORMAT)
    if not raw:
        return []

    ps_lines = [] if fast else ps_snapshot().strip().split("\n")
    panes_cache = [] if fast else _build_panes_cache()
    own_pgid = str(os.getpgrp())
    # Sidekick attention markers: read once per build (a single
    # readdir), lazily on the first row because the global
    # @ccm-sidekick-attention toggle rides the bulk window query
    # rather than costing its own show-option. {} on the fast path
    # and when toggled off. Never allowed to break a build.
    attention_markers = None

    seen_dirs = set()
    projects = []

    for line in raw.split("\n"):
        row = _parse_window_line(line)
        if row is None:
            continue

        resolved = canonical_dir(row["proj_dir"])
        if resolved in seen_dirs:
            continue
        seen_dirs.add(resolved)

        completed_at = _safe_int(row["completed_at_str"])
        win_activity = _safe_int(row["win_activity_str"])
        sort_ts = max(completed_at, win_activity) if win_activity else completed_at

        state = _resolve_window_state(
            row, fast, panes_cache, ps_lines, own_pgid,
        )

        proj_dir = row["proj_dir"]
        branch = read_cache_file(CCM_GIT_CACHE_DIR, proj_dir) if proj_dir else ""
        ports = read_cache_file(CCM_PORT_CACHE_DIR, proj_dir) if proj_dir else ""
        if not fast:
            _observe_pane_order(row, panes_cache)
        pane_count = 1 if fast else _count_panes(panes_cache, row["win_target"])
        ignored_panes = (0 if fast else _resolve_ignored_panes(
            panes_cache, row["win_target"], ps_lines))
        external_agents = (() if fast else _resolve_external_agent_panes(
            panes_cache, row["win_target"]))
        if attention_markers is None:
            attention_markers = {}
            # The reader runs even when the display is toggled off,
            # because it is also the only garbage collector: writers
            # are per-CLI hook scripts that keep writing regardless of
            # a ccm-side switch, so skipping the read would let the
            # directory grow without bound. Only the RESULT is
            # discarded when off.
            if not fast:
                try:
                    markers = _read_attention_markers(panes_cache, ps_lines)
                    if row["attention_toggle"] != "off":
                        attention_markers = markers
                except Exception:
                    log_caught_exception("attention_markers")
        # Names among this window's agent panes whose marker says
        # `waiting` — join by pane id against the bulk cache, so a
        # window with two kimi panes colours only the waiting one's
        # name into the set.
        attention_agents = tuple(sorted({
            m.get("agent") or pc[3]
            for pc in panes_cache
            if pc[0] == row["win_target"]
            for m in (attention_markers.get(pc[2]),) if m
        })) if attention_markers else ()

        # Permission-mode badge (slow path, live sessions only). The
        # events tail was just read by detection for this same cycle,
        # so this is a cache hit, not extra I/O. `or ""` collapses the
        # None-vs-"" session_id semantics to the authoritative "no
        # session" form — a legacy window without the bulk field must
        # not trigger a per-project tmux lookup here (display-only
        # data is not worth a subprocess; it self-heals next cycle).
        permission_mode = ""
        if not fast and state in ("BUSY", "IDLE", "PERMIT"):
            try:
                permission_mode = ccm_signals.read_latest_permission_mode(
                    proj_dir, session_id=row["cached_session_id"] or "")
            except Exception:
                log_caught_exception(
                    f"permission_mode[{row['project']}]")

        projects.append(Project(
            win_target=row["win_target"],
            win_idx=row["win_target"].split(":")[-1],
            name=row["project"], directory=proj_dir, state=state,
            branch=branch, ports=ports, completed_at=sort_ts,
            bg_active=bool(row["bg_active_str"]
                           and row["bg_active_str"] != "0"),
            pane_count=pane_count,
            permission_mode=permission_mode,
            ignored_panes=ignored_panes,
            cached_session_id=row["cached_session_id"],
            external_agents=external_agents,
            attention_agents=attention_agents,
        ))

    projects.sort(key=lambda p: p.sort_key)
    return projects


# ─── Setup-state predicates and config-file mutation ───
# Helpers that read or write configuration files outside ccm itself
# (`~/.claude/settings.json`, `~/.tmux.conf`). Used by the dashboard
# settings menu, the init wizard, and the rendering layer to decide
# whether to surface "hooks not installed" warnings.

def hooks_configured():
    """Return True iff all 7 ccm hook scripts are referenced in the
    user's `~/.claude/settings.json`. Used as a "hooks installed?"
    probe — does not validate the script paths or contents."""
    settings_file = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings_file, encoding="utf-8") as f:
            content = f.read()
        return all(script in content for script in HOOK_SCRIPTS)
    except OSError:
        return False


def save_tmux_conf_setting(setting):
    """Persist a tmux setting to ~/.tmux.conf (before ccm/TPM load lines).
    setting: e.g., 'set -g @ccm-auto-restore on'
    """
    conf = os.path.expanduser("~/.tmux.conf")
    parts = setting.split()
    if len(parts) < 3:
        return
    key = parts[2]  # e.g., "@ccm-auto-restore"

    try:
        lines = []
        if os.path.exists(conf):
            with open(conf, encoding="utf-8") as f:
                lines = f.readlines()

        # Remove existing lines with this key
        lines = [l for l in lines if key not in l]

        # Find earliest ccm/TPM load line
        insert_at = len(lines)
        for i, l in enumerate(lines):
            if "source-file" in l and "ccm" in l:
                insert_at = min(insert_at, i)
            if l.strip().startswith("run") and "tpm" in l.lower():
                insert_at = min(insert_at, i)

        lines.insert(insert_at, setting + "\n")

        with open(conf, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError:
        pass


# ─── CLI helpers ───

# Minimal ANSI palette for ccm_core's own die/warn/info output and
# for `ccm_commands` (`_C_BOLD` / `_C_RESET` for table headers).
# `ccm_render` carries its own copy for the print_* helpers; the
# duplication is intentional to keep ccm_render's import direction
# strictly downstream of ccm_core (no circular dep).
_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_RED = "\033[0;31m"
_C_GREEN = "\033[0;32m"
_C_YELLOW = "\033[1;33m"


class CCMError(Exception):
    """Raised by ccm_die when raise_on_die() context is active.

    Allows TUI callers (e.g. dashboard) to receive errors as exceptions
    instead of stderr output + sys.exit, which would corrupt curses display.
    """
    pass


_die_mode = threading.local()


@contextlib.contextmanager
def raise_on_die():
    """Context manager: make ccm_die raise CCMError instead of exit.

    Thread-local, so CLI callers on other threads are unaffected.
    """
    prev = getattr(_die_mode, "raise_errors", False)
    _die_mode.raise_errors = True
    try:
        yield
    finally:
        _die_mode.raise_errors = prev


def ccm_die(msg):
    """Print error message and exit.

    If called inside a raise_on_die() context on the same thread, raises
    CCMError(msg) instead.
    """
    if getattr(_die_mode, "raise_errors", False):
        raise CCMError(msg)
    print(f"{_C_RED}Error: {msg}{_C_RESET}", file=sys.stderr)
    sys.exit(1)


def ccm_warn(msg):
    """Print warning message."""
    print(f"{_C_YELLOW}Warning: {msg}{_C_RESET}", file=sys.stderr)


def ccm_info(msg):
    """Print info message."""
    print(f"{_C_GREEN}{msg}{_C_RESET}")


def validate_name(name):
    """Sanitize project name. Returns cleaned name or empty string."""
    if not name:
        return ""
    # Strip whitespace
    name = name.strip()
    # Replace whitespace with hyphens
    name = re.sub(r'\s+', '-', name)
    # Remove shell-dangerous characters
    name = re.sub(r"['\"`$\\;&|<>()]", '', name)
    # Collapse and strip hyphens
    name = re.sub(r'-+', '-', name).strip('-')
    # Reject digit-only names: `ccm send 123 ...` would otherwise be
    # parsed as window INDEX 123 instead of the project named "123",
    # silently targeting an unrelated window. Name resolution does
    # prefer an exact name match over index interpretation (so legacy
    # digit-named projects stay reachable), but banning the ambiguous
    # form at creation keeps new name/index collisions impossible.
    if name.isdigit():
        return ""
    return name


def find_window(session, name):
    """Find window index by project name. Returns index string or None."""
    raw = tmux_cmd("list-windows", "-t", session, "-F",
                   "#{window_index}\t#{@ccm_project}")
    if not raw:
        return None
    for line in raw.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    return None


def project_exists(session, name):
    """Check if project name already exists in session."""
    return find_window(session, name) is not None


def list_windows_raw(session):
    """List ccm-managed windows. Returns list of (idx, name, project, dir)."""
    raw = tmux_cmd("list-windows", "-t", session, "-F",
                   "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}")
    if not raw:
        return []
    result = []
    for line in raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        if parts[2]:  # has @ccm_project tag
            result.append(tuple(parts[:4]))
    return result


def clipboard_copy(text):
    """Copy text to system clipboard. Returns True on success."""
    for cmd in [["pbcopy"], ["clip.exe"], ["xclip", "-selection", "clipboard"], ["xsel", "-b"]]:
        try:
            subprocess.run(cmd, input=text, text=True, timeout=5,
                           capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False


def init_dirs():
    """Create runtime directories."""
    for d in [CCM_SNAPSHOT_DIR, CCM_TMP_DIR, CCM_HOOK_DIR,
              CCM_GIT_CACHE_DIR, CCM_PORT_CACHE_DIR,
              os.path.join(os.path.expanduser("~/.local/share/ccm"), "state")]:
        os.makedirs(d, exist_ok=True)


def fzf_select(items, prompt="Select: "):
    """Run fzf for interactive selection. Returns selected item or None."""
    try:
        r = subprocess.run(["fzf", "--prompt", prompt, "--height=10"],
                           input="\n".join(items), capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    except FileNotFoundError:
        ccm_die("fzf not found (install with: brew install fzf)")




# ─── Submodule imports for circular-dep-safe access ───
# Loaded at the bottom because each of these modules imports
# ccm_core for its own constants / helpers; importing them at the
# top would deadlock on circular references. Loaded as bare
# `import` (not `from … import`) so callers reach symbols via
# `ccm_<module>.X` — there is no re-export hub.

import ccm_canaries  # noqa: E402, F401
import ccm_commands  # noqa: E402, F401
import ccm_detection  # noqa: E402, F401
import ccm_jsonl  # noqa: E402, F401
import ccm_notify  # noqa: E402, F401
import ccm_render  # noqa: E402, F401
import ccm_rules  # noqa: E402, F401
import ccm_runtime  # noqa: E402, F401
import ccm_send  # noqa: E402, F401
import ccm_signals  # noqa: E402, F401
import ccm_snapshot  # noqa: E402, F401
import ccm_window  # noqa: E402, F401


# ─── CLI entry point ───
# argparse-based dispatch for the Python-side subcommands. The bash
# `ccm` wrapper forwards `ccm <cmd> [args...]` to
# `python3 lib/ccm_core.py <cmd> [args...]`; this block is what
# resolves <cmd>. Bash-side commands (init, setup-hooks, hooks
# cleanup, claude-md install, init wizard, dashboard) stay in the
# bash wrapper because they touch interactive prompts or run a
# different Python entry point (lib/dashboard.py).
#
# Every subcommand is registered as a `(name, handler, configurer)`
# triple. The configurer adds parser-specific arguments; the handler
# receives parsed args and is the side-effecting body. Adding a new
# subcommand means appending one entry to `_SUBCOMMANDS` — no
# elif-chain editing.

def _clear_notifications_handler(_args):
    rc = ccm_notify.clear_notifications()
    if rc < 0:
        ccm_warn("terminal-notifier is not installed. "
                 "Install with: brew install terminal-notifier")
        sys.exit(1)
    if rc == 0:
        ccm_info("No ccm notifications were in Notification Center")
    else:
        ccm_info(f"Cleared {rc} ccm notification(s) from Notification Center")


def _debug_handler(args):
    if args.subcommand != "trace":
        print(f"Unknown debug subcommand: {args.subcommand!r}", file=sys.stderr)
        print("Available: trace", file=sys.stderr)
        sys.exit(2)
    ccm_commands.cmd_debug_trace(args.target, interval=args.interval)


def _add_debug_args(p):
    p.add_argument("subcommand", choices=["trace"],
                   help="Debug action (currently only 'trace')")
    p.add_argument("target",
                   help="Project name/substring, or a tmux target "
                        "(pane %%42, window @7, session:index) for a "
                        "session ccm does not manage")
    p.add_argument("interval", nargs="?", type=float, default=0.3,
                   help="Polling interval in seconds (default: 0.3)")


def _add_name_arg(p, *, optional=True):
    p.add_argument("name", nargs="?" if optional else None, default="")


def _add_name_pair(p):
    p.add_argument("name", nargs="?", default="")
    p.add_argument("new_name", nargs="?", default="")


def _add_dir_and_name(p):
    p.add_argument("dir", nargs="?", default="")
    p.add_argument("name", nargs="?", default="")


def _handle_add(args):
    """`ccm add` handler — prompts for directory creation when the
    path is missing and stdin is a tty.

    Why the tty gate: existing scripts / snapshot-restore paths
    that call `ccm add <missing>` expect deterministic "die" today.
    Adding a prompt unconditionally would (a) hang non-tty
    callers waiting on stdin or (b) auto-create dirs the script
    didn't intend. The `sys.stdin.isatty()` gate preserves the
    contract for automated callers while giving interactive
    users a one-step "create + add" affordance.

    Parent-must-exist (no recursive `mkdir -p`): typo'd paths
    are far more common than legitimate deep tree creation
    requests. Refusing when the parent is missing forces the
    caller to spell intent explicitly; `cmd_add` enforces the
    same rule even if `create_dir=True` is supplied.
    """
    create_dir = False
    if args.dir and sys.stdin.isatty():
        expanded = os.path.expanduser(args.dir)
        if not os.path.exists(expanded):
            parent = os.path.dirname(os.path.abspath(expanded)) or "/"
            if os.path.isdir(parent):
                try:
                    ans = input(f"Create '{expanded}'? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return
                if ans in ("y", "yes"):
                    create_dir = True
                else:
                    return  # user declined; quietly exit
    ccm_commands.cmd_add(args.dir, args.name, create_dir=create_dir)


def _handle_stop(args):
    """`ccm stop` handler — passthrough so `--all` reaches cmd_stop.

    With a plain positional configurer, argparse rejects `--all` as
    an unknown option (exit 2) before the handler ever runs, which
    made the documented `ccm stop --all` — and its `_autosave`
    snapshot — unreachable from the CLI. Raw-argv passthrough (the
    same mechanism as capture / send / errors) lets cmd_stop see
    the flag itself."""
    if len(args.rest) > 1:
        ccm_die("Usage: ccm stop [--all|<name>]")
    ccm_commands.cmd_stop(args.rest[0] if args.rest else "")


def _passthrough_argparse_config(p):
    """Marker configurer: the subcommand bypasses argparse and
    receives raw `argv[1:]` as `rest`. The handler does its own
    flag parsing — necessary for commands that allow flags
    intermixed with positionals (`ccm capture --copy blog` is as
    valid as `ccm capture blog --copy`), which `nargs="*"` and
    `nargs=REMAINDER` both reject. The dispatcher detects this
    configurer by identity and skips `parse_args`."""
    p.add_argument("rest", nargs="*")


# (subcommand, configurer, handler). Configurers receive the
# subparser; handlers receive the namespace and dispatch.
# Use `_passthrough_argparse_config` as the configurer to opt a
# subcommand into raw-argv passthrough (handler parses its own flags).
_SUBCOMMANDS = (
    ("status", lambda p: None,
     lambda a: ccm_render.print_status()),
    ("ports", lambda p: None,
     lambda a: ccm_render.print_ports()),
    ("tree", lambda p: None,
     lambda a: ccm_render.print_tree()),
    ("statusline", lambda p: None,
     lambda a: ccm_render.print_statusline()),
    ("add", _add_dir_and_name, _handle_add),
    ("open", _add_dir_and_name,
     lambda a: ccm_commands.cmd_open(a.dir, a.name)),
    ("register", _add_name_pair,
     lambda a: ccm_commands.cmd_register(a.name, a.new_name)),
    ("unregister", _add_name_arg,
     lambda a: ccm_commands.cmd_unregister(a.name)),
    ("rename", _add_name_pair,
     lambda a: ccm_commands.cmd_rename(a.name, a.new_name)),
    ("remove", _add_name_arg,
     lambda a: ccm_commands.cmd_remove(a.name)),
    ("attach", _add_name_arg,
     lambda a: ccm_commands.cmd_attach(a.name)),
    ("list", lambda p: None,
     lambda a: ccm_commands.cmd_list()),
    ("capture",
     _passthrough_argparse_config,
     lambda a: ccm_commands.cmd_capture(a.rest)),
    ("stop",
     _passthrough_argparse_config,
     _handle_stop),
    ("send",
     _passthrough_argparse_config,
     lambda a: ccm_send.cmd_send(a.rest)),
    ("snapshot-save", _add_name_arg,
     lambda a: ccm_snapshot.cmd_snapshot_save(a.name)),
    ("snapshot-load", _add_name_arg,
     lambda a: ccm_snapshot.cmd_snapshot_load(a.name)),
    ("snapshot-list", lambda p: None,
     lambda a: ccm_snapshot.cmd_snapshot_list()),
    ("snapshot-delete", _add_name_arg,
     lambda a: ccm_snapshot.cmd_snapshot_delete(a.name)),
    ("reset-window", lambda p: None,
     lambda a: ccm_commands.cmd_reset_window()),
    ("reset", _add_name_arg,
     lambda a: ccm_commands.cmd_reset(a.name)),
    ("ignore", _add_name_arg,
     lambda a: ccm_commands.cmd_ignore(a.name)),
    ("unignore", _add_name_arg,
     lambda a: ccm_commands.cmd_unignore(a.name)),
    ("errors",
     _passthrough_argparse_config,
     lambda a: ccm_commands.cmd_errors(a.rest)),
    ("doctor", lambda p: None,
     lambda a: ccm_commands.cmd_doctor()),
    ("setup-sidekick-hooks", _add_name_arg,
     lambda a: ccm_commands.cmd_setup_sidekick_hooks(a.name)),
    ("remove-sidekick-hooks", _add_name_arg,
     lambda a: ccm_commands.cmd_remove_sidekick_hooks(a.name)),
    ("clear-notifications", lambda p: None, _clear_notifications_handler),
    ("debug", _add_debug_args, _debug_handler),
    ("bg-list", lambda p: None,
     lambda a: ccm_render.print_bg_sessions()),
)


# Derived from the subcommand table: any command whose configurer
# is `_passthrough_argparse_config` opts into raw-argv passthrough.
# Adding a new flag-intermixing command requires no change here.
_PASSTHROUGH_COMMANDS = frozenset(
    name for name, configure, _ in _SUBCOMMANDS
    if configure is _passthrough_argparse_config
)


def _build_parser():
    import argparse
    parser = argparse.ArgumentParser(
        prog="ccm_core.py",
        description="ccm core dispatcher (called by the bash `ccm` wrapper).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    handlers = {}
    for name, configure, handler in _SUBCOMMANDS:
        sp = sub.add_parser(name)
        configure(sp)
        handlers[name] = handler
    return parser, handlers


def dispatch(argv):
    """Run a ccm subcommand. `argv` is the args after the program
    name (e.g. `["send", "blog", "--file", "msg.txt"]`)."""
    import argparse
    if argv and argv[0] in _PASSTHROUGH_COMMANDS:
        cmd = argv[0]
        rest = argv[1:]
        for name, _configure, handler in _SUBCOMMANDS:
            if name == cmd:
                handler(argparse.Namespace(cmd=cmd, rest=rest))
                return
    parser, handlers = _build_parser()
    ns = parser.parse_args(argv)
    handlers[ns.cmd](ns)


if __name__ == "__main__":
    dispatch(sys.argv[1:])
