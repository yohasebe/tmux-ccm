#!/usr/bin/env python3
"""ccm core — shared constants, helpers, state detection, and project list building."""

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
from collections import OrderedDict
from datetime import datetime
from typing import Optional, Tuple

# When invoked as `python3 lib/ccm_core.py <subcmd>` (what the `ccm`
# bash wrapper does), Python loads this file as `__main__`. Later, the
# re-exports at the bottom pull in `ccm_detection` / `ccm_commands`,
# which both `import ccm_core`. Without the alias below, Python would
# not find `ccm_core` in `sys.modules` (only `__main__`), so it would
# re-execute this file as a second module — which then re-runs the
# `from ccm_detection import ...` block while `ccm_detection` is still
# initializing, raising "partially initialized module" on DETECTION_RULES.
# Registering __main__ under the `ccm_core` name short-circuits the
# second import. For normal `import ccm_core` (tests, dashboard, etc.)
# this is a harmless no-op because the entry is already present.
sys.modules.setdefault("ccm_core", sys.modules[__name__])

# ─── Constants ───

CCM_ROOT = os.environ.get(
    "CCM_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CCM_TMP_DIR = os.environ.get(
    "CCM_TMP_DIR",
    os.path.join(os.environ.get("TMPDIR", "/tmp"), f"ccm-{os.getuid()}"),
)
CCM_HOOK_DIR = os.environ.get("CCM_HOOK_DIR", os.path.join(CCM_TMP_DIR, "hooks"))
_default_data_dir = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "ccm",
)
CCM_DATA_DIR = os.environ.get("CCM_DATA_DIR", _default_data_dir)
CCM_SNAPSHOT_DIR = os.environ.get(
    "CCM_SNAPSHOT_DIR", os.path.join(CCM_DATA_DIR, "snapshots")
)
CCM_GIT_CACHE_DIR = os.path.join(CCM_TMP_DIR, "git-cache")
CCM_PORT_CACHE_DIR = os.path.join(CCM_TMP_DIR, "port-cache")

# Display-layer "recently completed" marker timeout.
COMPLETED_AT_TIMEOUT = int(os.environ.get("CCM_COMPLETED_AT_TIMEOUT", "30"))
# Hook signal age (seconds) below which a BUSY signal is treated as "fresh"
# and trusted unconditionally — bypasses the slower pipeline when multiple
# projects contend for evaluation time.
HOOK_FRESH_THRESHOLD = 2
# JSONL session-log freshness threshold. If the project's newest .jsonl
# Window for trusting a BUSY hook signal without JSONL corroboration.
# A BUSY hook older than this AND a JSONL that has been silent for
# the same duration is almost certainly left over from a turn that
# completed without a Stop hook firing (anthropics/claude-code#25655
# class). Past this window, the rule table stops trusting the hook
# and falls through to the raw=IDLE fallback path so the state can
# eventually drop out of BUSY. Default 10 minutes — long enough to
# cover real thinking phases that legitimately lack tool activity,
# short enough that a missed Stop does not strand the project in
# BUSY indefinitely.
BUSY_HOOK_JSONL_WINDOW = int(os.environ.get("CCM_BUSY_HOOK_JSONL_WINDOW", "600"))
# JSONL real-activity filter — whitelist. Only records whose
# top-level `type` is in this set count as conversation activity
# in `_parse_jsonl_tail`. Whitelist is preferable to a blacklist
# because Claude Code adds housekeeping record types over time
# (recap / `away_summary`, `permission-mode`, `file-history-snapshot`,
# `last-prompt`, …): a blacklist needs a code change for each new
# type, while the whitelist absorbs them automatically. New
# activity types are the only case that needs explicit addition.
JSONL_ACTIVITY_TYPES = frozenset({"user", "assistant"})
# Tail size (bytes) read from each JSONL when looking for the most
# recent real activity record. Needs to accommodate a single large
# tool_result record (Read of 2000 lines, long shell output, ...)
# plus several trailing system records — any tool-result record alone
# can easily exceed 8 KB, which at the previous cap could leave the
# tail without a decodable real-activity line and force the mtime
# fallback. 32 KB covers that comfortably while remaining trivially
# cheap per detection cycle.
JSONL_TAIL_BYTES = 32768
# Safety cap on how many lines from the tail we will JSON-parse.
JSONL_TAIL_MAX_LINES = 200
# Hook-vs-real-activity gap discriminator. A BUSY hook fired more
# than this many seconds AFTER the last real conversation activity
# is treated as a phantom hook (no surrounding real work) — the
# upstream `away_summary` recap fires a BUSY-class hook with no
# corresponding Stop, and this guard rejects it. In real long-
# thinking, hook_age and real_activity_age grow together (gap ~0);
# in recap, the hook is brand new while real_activity is minutes
# old (gap >> threshold).
JSONL_HOOK_GAP_TOLERANCE = int(os.environ.get("CCM_JSONL_HOOK_GAP_TOLERANCE", "60"))
# Cluster-SHELL-transition detection: surface a warning when a project
# drops back to SHELL too many times in a short window. The canonical
# trigger is anthropics/claude-code#48069 (macOS silent-exit), where
# Claude Code dies every 1-5 minutes and ccm observes SHELL → (user
# re-attaches) → BUSY → IDLE → SHELL loops. Defaults: 3 transitions
# in 10 minutes.
SHELL_CLUSTER_WINDOW = int(os.environ.get("CCM_SHELL_CLUSTER_WINDOW", "600"))
SHELL_CLUSTER_COUNT = int(os.environ.get("CCM_SHELL_CLUSTER_COUNT", "3"))
# Upstream issue tag surfaced in the cluster-SHELL warning. Centralised
# so future re-classification (different issue, root-cause PR, etc.)
# only needs one edit. Update both halves when the upstream story
# changes.
SHELL_CLUSTER_ISSUE = "anthropics/claude-code#48069"
SHELL_CLUSTER_ISSUE_NOTE = "macOS silent-exit"
PERMIT_MAX_TIMEOUT = int(os.environ.get("CCM_PERMIT_MAX_TIMEOUT", "600"))  # 10 min safety net
IDLE_EXIT_TIMEOUT = int(os.environ.get("CCM_IDLE_EXIT_TIMEOUT", "600"))  # 10 minutes default
CACHE_TTL = int(os.environ.get("CCM_CACHE_TTL", "30"))  # git/port cache seconds
# How long after the `claude` process starts a `raw=BUSY` reading is
# treated as MCP-loading startup rather than real work, when no hook
# signal has been written yet. MCP server initialization typically
# finishes within 10–30 s, so 60 s is a conservative cap. After the
# grace expires the startup_transient rule stops firing and detection
# falls back to the `raw_busy_passthrough` rule — so a
# Claude that actually hangs during startup will surface as BUSY.
STARTUP_GRACE_SEC = int(os.environ.get("CCM_STARTUP_GRACE_SEC", "60"))

# Minimum pane height (in tmux rows) for a pane to contribute to the
# window's aggregated state. Panes shorter than this cannot reliably
# render Claude's `❯` prompt + accept-edits indicator + footer, so
# capture-pane–based prompt detection silently fails and the pane
# falsely reads BUSY (has children, no prompt visible). Excluding
# them from aggregation prevents an invisible sliver from
# infecting the whole window. If every pane is below the
# threshold (impossible in practice), the filter is bypassed so
# detection still produces an answer.
SLIVER_HEIGHT_THRESHOLD = int(os.environ.get("CCM_SLIVER_HEIGHT_THRESHOLD", "4"))

# ─── Claude Code UI patterns (update when Claude Code UI changes) ───
# These are the ONLY place where Claude Code's terminal output is matched.
# If detection breaks after a Claude Code update, check these first.
# See: https://github.com/anthropics/claude-code

# Input prompt characters (single character followed by space = idle prompt)
_PROMPT_CHARS = "❯"
# Accept-edits prompt characters (doubled = accept-edits mode)
_ACCEPT_CHARS = "❯⏵"
PATTERN_INPUT_PROMPT = re.compile(rf"^[{_PROMPT_CHARS}]\s")
PATTERN_ACCEPT_EDITS = re.compile(rf"^\s*[{_ACCEPT_CHARS}]{{2}}")
# Modal-dialog footer markers. Matches any Claude Code UI that is
# blocked awaiting a user keypress response. Observed forms:
#
#   - "Esc to cancel · Tab to amend"          (permission dialog)
#   - "Esc to cancel · ctrl+e to explain"     (permission dialog alt)
#   - "Enter to confirm · Esc to cancel"      (session-resume modal)
#   - "Enter to confirm · Esc to exit"        (/model picker)
#
# All map to the PERMIT state because semantically Claude is blocked
# pending a single user action — the UX is the same as a permission
# prompt (user sees the ⚠ icon, knows to return to the pane). A
# fifth "MODAL" state would split hairs without benefit.
#
# The Esc-verb after "Enter to confirm" varies per modal author
# (cancel / exit observed so far; Claude Code upstream is not
# consistent). `Esc to \w+` is intentionally permissive for the
# confirm-modal branch — the `Enter to confirm` prefix is strong
# enough that false-positive risk is negligible, and this future-
# proofs against new modals that pick yet another verb (close,
# quit, dismiss, ...).
#
# Anchored at line start (after optional whitespace) so the same
# words inside a Claude response — e.g. "use ctrl+e to explain" in
# answer text, or a code example containing "Enter to confirm" —
# do not falsely trigger PERMIT. The bare "Esc to cancel" line used
# by slash menus (/hooks, /config, /skills, ...) deliberately does
# NOT match: those menus are free navigation, not a blocked decision.
PATTERN_PERMIT_FOOTER = re.compile(
    r"^\s*(?:"
    r"Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
    r"|Enter to confirm\s*(?:·|\|)\s*Esc to \w+"
    r")"
)

# ─── PERMIT modal classification patterns ───
# Content-level signatures (not just the footer) used by
# `classify_permit_modal()` to distinguish safe modals (session-resume,
# /model picker, /exit confirmation) from dangerous permission
# dialogs. Order inside `classify_permit_modal()` matters — check the
# most specific signatures first.
#
# We match these against the full captured tail, not just the footer
# line, because the footer alone is ambiguous: both session-resume
# and /model use `Enter to confirm · Esc to …`.
PATTERN_RESUME_MODAL = re.compile(
    r"This session is \d+h \d+m old"
    r"|Resume from summary \(recommended\)"
)
PATTERN_PERMISSION_DIALOG = re.compile(
    r"Do you want to proceed\?"
    r"|Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
)
PATTERN_MODEL_PICKER = re.compile(
    r"Switch between Claude models"
    r"|Select (?:a )?model"
)

# Claude Code process name in `ps` output
CLAUDE_PROCESS_NAME = "claude"
# Processes that are always children of Claude Code and should be ignored
# when checking for meaningful child processes (tool execution).
IGNORED_CHILDREN = {"caffeinate"}
# Foreground commands (`tmux #{pane_current_command}`) that
# indicate the pane is at a shell prompt — claude may exist
# somewhere in the process tree but is not the active foreground
# process (e.g. user did Ctrl-Z + new shell). Used to override
# the process-tree heuristic (which would otherwise return BUSY
# for the lingering claude pid). Editors / pagers (vim, less,
# etc.) are intentionally NOT in this set — those mean the user
# is actively doing something, even if not in claude, and ccm's
# auto-start should not fire over them.
SHELL_FOREGROUND_COMMANDS = frozenset({
    "zsh", "bash", "sh", "fish", "ksh", "csh", "tcsh", "dash", "ash",
})

CLAUDE_CMD = "claude --continue 2>/dev/null || claude"

# Hook script filenames (single source of truth for hooks_configured checks)
HOOK_SCRIPTS = [
    "on-prompt-submit.sh",
    "on-stop.sh",
    "on-pre-tool-use.sh",
    "on-notification.sh",
    "on-permission-request.sh",
    "on-permission-denied.sh",
    "on-session-end.sh",
]

STATE_PRIORITY = {"PERMIT": 0, "BUSY": 1, "IDLE": 2, "SHELL": 3, "DOWN": 4}
# Detection state icons. Keep in sync with `lib/state_meta.sh` —
# bash hooks pay a ~50ms cost per Python cold start, so we cannot
# just shell out to Python to resolve icons; the bash side has its
# own copy in `ccm_state_icon`. Update BOTH when adding / changing
# a state icon. The extra "COMPLETED" key used by notification
# paths is bash-only (not a detection state).
STATE_ICONS = {
    "PERMIT": "⚠", "BUSY": "◉", "IDLE": "●", "SHELL": "■", "DOWN": "○",
}


# ─── PERMIT modal classification ───

# Guidance strings are kept close to the classifier so they evolve
# together when Claude Code adds a new modal kind. Multi-line so
# `cmd_send` can print them verbatim under the refusal header.
_PERMIT_GUIDANCE = {
    "session-resume": (
        "claude --continue resume picker (safe — no side effects).\n"
        "User action required: switch to the target pane, then:\n"
        "  - Enter         → Resume from summary (recommended)\n"
        "  - ↓, Enter      → Resume full session as-is\n"
        "  - ↓×2, Enter    → Don't ask me again\n"
        "  - Esc           → Cancel resume (session won't start)"
    ),
    "permission-request": (
        "Permission dialog for a tool invocation (DANGEROUS —\n"
        "do NOT attempt to dismiss from another pane).\n"
        "User action required: switch to the target pane and\n"
        "respond to the prompt yourself. ccm refuses to send\n"
        "keystrokes here because they could accidentally approve\n"
        "or deny a tool call."
    ),
    "confirmation-modal": (
        "Confirmation modal (e.g., /model picker, /exit).\n"
        "Safe to dismiss but requires a user decision.\n"
        "User action required: switch to the target pane and\n"
        "press Enter to confirm or Esc to cancel."
    ),
    "unknown-permit": (
        "Unrecognized PERMIT modal. Treat as dangerous by default.\n"
        "User action required: switch to the target pane and\n"
        "inspect the dialog before responding. If this is a new\n"
        "Claude Code modal, the classifier in ccm_core.py needs\n"
        "an additional signature pattern."
    ),
}


def classify_permit_modal(pane_text: str):
    """Classify a PERMIT-state pane by content signature.

    Returns (category, guidance) where category is one of:
      - "session-resume"     — claude --continue resume picker
      - "permission-request" — tool permission dialog (dangerous)
      - "confirmation-modal" — /model picker, /exit, ... (safe)
      - "unknown-permit"     — none of the above matched

    `pane_text` is the full captured tail (lines joined with newlines).
    Order matters: the more specific the signature, the earlier we
    check. Permission dialog is checked before the generic confirm
    footer so that a permission dialog never falls through as a safe
    confirmation-modal.
    """
    if PATTERN_PERMISSION_DIALOG.search(pane_text):
        cat = "permission-request"
    elif PATTERN_RESUME_MODAL.search(pane_text):
        cat = "session-resume"
    elif PATTERN_MODEL_PICKER.search(pane_text):
        cat = "confirmation-modal"
    elif PATTERN_PERMIT_FOOTER.search(pane_text):
        # Footer says "Enter to confirm · Esc to ..." but no
        # content-level signature matched — treat as a generic safe
        # confirmation rather than unknown. Permission dialogs are
        # caught above, so what remains is almost always a /<slash>
        # confirm or a not-yet-cataloged confirm modal.
        cat = "confirmation-modal"
    else:
        cat = "unknown-permit"
    return cat, _PERMIT_GUIDANCE[cat]


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
    """
    if not commands:
        return
    args = ["tmux"]
    for i, cmd in enumerate(commands):
        if i > 0:
            args.append(";")
        args.extend(cmd)
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=10)
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


# ─── Hook signal ───
# Signal file format: "<unix_timestamp> <STATE> [extra_fields...]"
# - Fields are space-separated; first two are required
# - STATE: one of BUSY, PERMIT (Stop hook deletes the file instead of writing)
# - Extra fields are reserved for future use and ignored by current code
# Written by: hooks/on-prompt-submit.sh, hooks/on-pre-tool-use.sh,
#             hooks/on-notification.sh (PERMIT only)
# Deleted by: hooks/on-stop.sh, hooks/on-notification.sh (idle_prompt)

VALID_HOOK_STATES = {"BUSY", "PERMIT", "SHELL"}


def _resolve_project_dir(project_dir):
    """Expand and resolve a project directory path."""
    expanded = os.path.expanduser(project_dir)
    try:
        expanded = os.path.realpath(expanded)
    except OSError:
        pass
    return expanded


# ─── session_id resolution ───
# Hook signal / events files are keyed on the Claude Code session_id
# (UUID per session, present in every hook payload). This is the
# natural primary key:
#   - stable for the lifetime of one Claude session
#   - distinct across sessions, so a fresh `claude --continue` cannot
#     read state left by a prior session in the same cwd
#   - unaffected by `cd` mid-session
# Earlier `md5(cwd)` keying needed sidecar files and a pid-age filter
# to patch around cwd drift and cross-session contamination; both
# vanish under session_id keying.
#
# ccm resolves session_id for a tmux project window via the chain:
#   @ccm_dir → tmux pane → claude pid → ~/.claude/sessions/<pid>.json
#                                       → sessionId
# The slow detection path computes this once and caches it on the
# tmux window option `@ccm_session_id`; the fast path (statusline)
# reads the cached value without a process-tree walk.

_CCM_SESSION_ID_OPTION = "@ccm_session_id"


def _session_id_from_tmux(project_dir: str) -> Optional[str]:
    """Look up the cached session_id for a project window via the
    `@ccm_session_id` tmux option. Returns None when no window
    matches (project removed) or no slow-path scan has populated
    the cache yet."""
    if not project_dir:
        return None
    expanded = _resolve_project_dir(project_dir)
    raw = tmux_cmd(
        "list-windows", "-a", "-F",
        "#{@ccm_dir}\t#{" + _CCM_SESSION_ID_OPTION + "}",
    )
    if not raw:
        return None
    for line in raw.split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        d, sid = parts[0], parts[1]
        if not d:
            continue
        if _resolve_project_dir(d) == expanded:
            return sid or None
    return None


def _hook_signal_path(project_dir, session_id: Optional[str] = None):
    """Get the hook signal file path for a project window.

    `session_id` semantics:
      - non-empty string → use directly (caller resolved it).
      - ``""``           → caller authoritatively reports "no session";
                            return None without any tmux lookup.
      - ``None``         → caller did not resolve; fall back to the
                            cached `@ccm_session_id` tmux option.
    Returns None whenever no session_id is known — callers must treat
    that as "no signal available" rather than reading a default path.
    """
    if session_id is None:
        session_id = _session_id_from_tmux(project_dir)
    if not session_id:
        return None
    return os.path.join(CCM_HOOK_DIR, session_id)


# Directory used for per-project "instant notification already
# fired" markers. Each marker filename is the same md5-of-cwd key
# used by the hook signal file, so ccm_core and the bash hook share
# one naming scheme. Must stay in sync with the `marker_dir` resolver
# in `hooks/lib.sh::_ccm_instant_notify`.
CCM_NOTIFY_MARKER_DIR = os.path.join(CCM_TMP_DIR, "notified")


def read_project_notify_marker(project_dir):
    """Read the per-project instant-notify marker. Returns (ts, state)
    or None if missing/unparseable.

    Written by `hooks/lib.sh::_ccm_instant_notify` when the hook path
    fires a desktop notification. Used by inject_status polling to
    avoid firing a duplicate notification for a project whose hook
    already notified — per-project scoping is required so that
    project A's recent completion does not suppress project B's
    notification (the symptom users see as "COMPLETED notifications
    randomly delayed / missing when running multiple projects").
    """
    if not project_dir:
        return None
    expanded = _resolve_project_dir(project_dir)
    marker_path = os.path.join(CCM_NOTIFY_MARKER_DIR, md5_hash(expanded))
    try:
        with open(marker_path, encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return None
    parts = content.split(None, 1)
    if len(parts) < 2:
        return None
    try:
        return (int(parts[0]), parts[1])
    except (ValueError, TypeError):
        return None


def cleanup_project_runtime_files(project_dir):
    """Remove all runtime files for a project window.

    Called from `cmd_unregister` and `cmd_remove`. Targets two
    distinct keying schemes:

    1. Hook artefacts keyed on Claude Code session_id
       (`$HOOK_DIR/<sessionId>`, `.events.jsonl`, `.busy`, `.pending`).
       Discovered via the cached `@ccm_session_id` tmux option for
       this project's window plus a sweep of any session_id files
       whose owning session has exited (no companion files remain
       elsewhere). For an active project being unregistered, the
       cached session_id catches its current session; the sweep
       picks up prior sessions whose hooks accumulated.

    2. Caches keyed on `md5(project_dir)` (git-cache, port-cache,
       notify-marker). These were already cwd-keyed on the ccm
       side and stay that way — they identify the project window,
       not the session.

    Each removal is independent and guarded against OSError so a
    missing file (normal case for inactive projects) is a silent
    no-op; one failure does not block the rest.
    """
    if not project_dir:
        return
    expanded = _resolve_project_dir(project_dir)
    cwd_key = md5_hash(expanded)

    # Hook-side: find session_ids that belong to this project.
    # Active session: the cached @ccm_session_id (if the window is
    # still tagged). Past sessions accumulate in $HOOK_DIR but are
    # not project-attributed by themselves; we cannot cleanly sweep
    # them without a per-session cwd record (which we deliberately
    # removed), so we only clear the active session's files. Stale
    # past-session files persist until macOS $TMPDIR auto-cleanup
    # or a manual `ccm errors --clear`-style action in the future.
    session_ids = set()
    cached_sid = _session_id_from_tmux(project_dir)
    if cached_sid:
        session_ids.add(cached_sid)

    for sid in session_ids:
        for suffix in ("", ".busy", ".pending", ".events.jsonl"):
            path = os.path.join(CCM_HOOK_DIR, sid + suffix)
            try:
                os.unlink(path)
            except OSError:
                pass

    # Caches keyed on cwd: notification marker, git branch, ports.
    for directory in (
        CCM_NOTIFY_MARKER_DIR,
        CCM_GIT_CACHE_DIR,
        CCM_PORT_CACHE_DIR,
    ):
        try:
            os.unlink(os.path.join(directory, cwd_key))
        except OSError:
            pass


def read_hook_signal(project_dir, session_id: Optional[str] = None):
    """Read hook signal file. Returns (timestamp, state, detail) or None.
    Detail is optional extra info (e.g., tool name for PERMIT).

    Caller may pass `session_id` directly (slow path, after resolving
    via pid chain). Otherwise resolved via the `@ccm_session_id`
    tmux option set by the most recent slow path. Returns None when
    no session_id is yet known for this project — fresh sessions
    before the first hook fire fall through to legacy detection.
    """
    hook_file = _hook_signal_path(project_dir, session_id=session_id)
    if not hook_file:
        return None
    try:
        with open(hook_file, encoding="utf-8") as f:
            content = f.read().strip()
        parts = content.split(None, 2)  # split into at most 3 parts
        if len(parts) >= 2 and parts[1] in VALID_HOOK_STATES:
            detail = parts[2] if len(parts) >= 3 else ""
            return int(parts[0]), parts[1], detail
    except (OSError, ValueError):
        pass
    return None


# ─── Claude Code session log (JSONL) ───
# Independent activity heartbeat that survives hook outages.
# Claude Code writes one JSONL file per session under
# `~/.claude/projects/<slug>/<sessionId>.jsonl`. Records are appended
# at conversation turn boundaries (user prompt, assistant message,
# tool_use, tool_result). The file mtime is therefore a reliable
# "session activity" signal — when fresh, Claude is alive and
# exchanging records, regardless of whether hooks are firing.
#
# Limitation: pure thinking / token streaming phases do NOT update
# the file (records are written at message completion, not during
# generation). So a stale mtime does not imply IDLE — only fresh
# mtime is actionable. We use this as a positive BUSY signal only.
#
# Slug rule (verified empirically against ~/.claude/projects/):
#   `/Users/alice/code/myproject` → `-Users-alice-code-myproject`
# Claude Code uses the *literal* cwd at session start (no realpath
# resolution), so we must NOT resolve symlinks here.

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CLAUDE_SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
JSONL_CACHE_TTL = int(os.environ.get("CCM_JSONL_CACHE_TTL", "60"))

# In-process cache: project_dir → (newest_jsonl_path, expiry_unixtime).
# Path is re-discovered on cache expiry or when the cached file is gone.
_jsonl_path_cache: dict = {}


def read_session_info(claude_pid):
    """Read the Claude Code runtime session file for a pid.

    Claude Code writes `~/.claude/sessions/{pid}.json` at session start
    with fields: pid, sessionId, cwd, startedAt, kind, entrypoint.
    This is the authoritative source for mapping a running Claude
    process to its session id and recorded cwd — no slug guessing,
    no symlink / worktree edge cases.

    Returns a dict on success, or None if the file is missing or
    malformed. Callers should gracefully fall back to slug-based
    discovery when this returns None (older Claude Code versions,
    sandboxed execution, etc.).
    """
    if not claude_pid:
        return None
    path = os.path.join(CLAUDE_SESSIONS_DIR, f"{claude_pid}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _project_slug(project_dir: str) -> str:
    """Convert a project directory to its Claude Code JSONL slug.

    Uses the literal path with `/` → `-`. Tilde is expanded but
    symlinks are NOT resolved (Claude Code records the cwd as-given).
    """
    expanded = os.path.expanduser(project_dir)
    return expanded.replace("/", "-")


def _jsonl_from_session_info(claude_pid):
    """Resolve the exact JSONL path via ~/.claude/sessions/{pid}.json.

    Returns the path to the session's JSONL file, or None if the
    runtime session file is missing or does not point at an existing
    JSONL. Skips non-interactive sessions (e.g. `claude -p` headless
    runs) — they are not user-facing and ccm should ignore them.
    """
    info = read_session_info(claude_pid)
    if not info:
        return None
    if info.get("kind") != "interactive":
        return None
    session_id = info.get("sessionId")
    cwd = info.get("cwd")
    if not session_id or not cwd:
        return None
    slug = cwd.replace("/", "-")
    path = os.path.join(CLAUDE_PROJECTS_DIR, slug, f"{session_id}.jsonl")
    return path if os.path.exists(path) else None


def _find_newest_jsonl(project_dir: str, claude_pid=None):
    """Return the path to the newest *.jsonl file for this project,
    or None if there is none.

    Prefers the authoritative mapping via `~/.claude/sessions/{pid}.json`
    when claude_pid is provided and the runtime session file exists.
    Falls back to slug-based directory scanning for older Claude Code
    versions or when the pid mapping cannot be resolved.

    Result is cached for JSONL_CACHE_TTL seconds; the file's mtime is
    read live each call.
    """
    now = time.time()

    # Fast path: runtime session file gives us the exact JSONL.
    if claude_pid:
        exact = _jsonl_from_session_info(claude_pid)
        if exact:
            _jsonl_path_cache[project_dir] = (exact, now + JSONL_CACHE_TTL)
            return exact

    cached = _jsonl_path_cache.get(project_dir)
    if cached and now < cached[1]:
        path = cached[0]
        if path is None:
            return None
        if os.path.exists(path):
            return path
        # cached path vanished — fall through to re-scan

    slug = _project_slug(project_dir)
    session_dir = os.path.join(CLAUDE_PROJECTS_DIR, slug)
    try:
        entries = os.listdir(session_dir)
    except OSError:
        _jsonl_path_cache[project_dir] = (None, now + JSONL_CACHE_TTL)
        return None

    newest = None
    newest_mtime = -1.0
    for entry in entries:
        if not entry.endswith(".jsonl"):
            continue
        full = os.path.join(session_dir, entry)
        try:
            mt = os.path.getmtime(full)
        except OSError:
            continue
        if mt > newest_mtime:
            newest_mtime = mt
            newest = full

    _jsonl_path_cache[project_dir] = (newest, now + JSONL_CACHE_TTL)
    return newest


# Cache for _parse_jsonl_tail. Key: jsonl path. Value:
# ((mtime_int, size_int), (real_activity_ts_or_None, last_stop_reason_or_None)).
# The cache hits on every detection cycle as long as the JSONL file hasn't
# been written, so the cost of tail-reading + JSON parsing is paid only
# when the file actually changes.
#
# Why (mtime, size) and not just mtime: int(mtime) has 1-second
# precision, so two writes within the same wall-clock second would
# share the same int(mtime) and collide. JSONL files are append-only
# during a session, so the size is monotonic and any new write
# changes it — adding size to the key catches sub-second writes that
# bare mtime would miss.
#
# OrderedDict + bounded eviction: a new JSONL file is created on every
# `claude --continue` or `/compact`, so the cache would otherwise grow
# without bound in long-running tmux sessions. On each lookup we move
# the entry to the end (MRU); on insertion, we pop the oldest entry
# if the cache exceeds JSONL_ACTIVITY_CACHE_MAX.
JSONL_ACTIVITY_CACHE_MAX = 128
_jsonl_activity_cache: "OrderedDict[str, Tuple[Tuple[int, int], Tuple[Optional[int], Optional[str]]]]" = OrderedDict()


_TERMINAL_STOP_REASONS = frozenset({"end_turn", "max_tokens", "stop_sequence"})

# Synthesized stop_reason value: emitted when the latest real-activity
# record is a `user` entry that landed AFTER a terminal assistant
# record. This is the signature of "user submitted a new prompt;
# claude is processing it (extended-thinking phase, no new assistant
# record yet)". Distinct from any stop_reason Claude Code emits, so
# detection can branch on it explicitly.
JSONL_USER_PENDING = "user_pending"


def _parse_jsonl_tail(
    path: str, mtime: int, size: int
) -> Tuple[Optional[int], Optional[str]]:
    """Tail-read a JSONL file and return:
      - unix timestamp of the most recent real-conversation-activity
        record, or None if no such record was found in the tail window
      - active stop-state at the JSONL tail:
          * `stop_reason` of the most recent assistant record (any of
            `tool_use` / `end_turn` / `max_tokens` / `stop_sequence`,
            etc.), OR
          * the synthetic value `JSONL_USER_PENDING` when the latest
            real-activity record is a `user` entry whose timestamp is
            newer than the latest assistant record AND that assistant
            had a terminal stop_reason. This indicates a fresh user
            prompt is in flight (extended-thinking case where claude
            has not written any new assistant record yet).
        None when the tail contained neither.

    Only records whose `type` is in `JSONL_ACTIVITY_TYPES`
    (whitelist) are considered for both fields; everything else
    is skipped as system / housekeeping metadata.

    Cached by (path, mtime, size): the second call with an unchanged
    mtime AND size returns the cached tuple without re-reading the
    file. A new write changes the size (JSONL is append-only during
    a session), so cache invalidation is reliable even within the
    same wall-clock second.
    """
    key = (mtime, size)
    cached = _jsonl_activity_cache.get(path)
    if cached is not None and cached[0] == key:
        _jsonl_activity_cache.move_to_end(path)
        return cached[1]

    real_ts: Optional[int] = None
    latest_user_ts: Optional[int] = None
    latest_assistant_ts: Optional[int] = None
    last_stop_reason: Optional[str] = None

    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            actual_size = f.tell()
            f.seek(max(0, actual_size - JSONL_TAIL_BYTES))
            tail_bytes = f.read()
    except OSError:
        _cache_jsonl_activity(path, key, (None, None))
        return (None, None)

    tail = tail_bytes.decode("utf-8", errors="ignore")
    lines = tail.split("\n")
    # When we read mid-file, the first chunk line is potentially partial.
    # Drop it unless we read the entire file in one shot.
    if size > JSONL_TAIL_BYTES and len(lines) > 1:
        lines = lines[1:]

    parsed = 0
    for line in reversed(lines):
        if parsed >= JSONL_TAIL_MAX_LINES:
            break
        line = line.strip()
        if not line:
            continue
        parsed += 1
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        rec_type = rec.get("type")
        if not rec_type or rec_type not in JSONL_ACTIVITY_TYPES:
            continue
        # Parse timestamp — defence-in-depth in case Claude Code adds
        # a whitelisted type that omits the field.
        rec_ts: Optional[int] = None
        ts_str = rec.get("timestamp")
        if ts_str and isinstance(ts_str, str):
            try:
                iso = ts_str.replace("Z", "+00:00")
                rec_ts = int(datetime.fromisoformat(iso).timestamp())
            except (ValueError, TypeError):
                pass
        if rec_ts is None:
            continue
        if real_ts is None:
            real_ts = rec_ts
        if rec_type == "user" and latest_user_ts is None:
            latest_user_ts = rec_ts
        elif rec_type == "assistant" and latest_assistant_ts is None:
            latest_assistant_ts = rec_ts
            msg = rec.get("message") or {}
            sr = msg.get("stop_reason") if isinstance(msg, dict) else None
            if isinstance(sr, str) and sr:
                last_stop_reason = sr
        # Stop scanning once we have everything we need.
        if (real_ts is not None and latest_user_ts is not None
                and latest_assistant_ts is not None):
            break

    # Promote to JSONL_USER_PENDING when a user record is newer than
    # the latest terminal assistant — i.e. the user just submitted a
    # new prompt and claude has not written any response yet.
    if (latest_user_ts is not None and latest_assistant_ts is not None
            and latest_user_ts > latest_assistant_ts
            and last_stop_reason in _TERMINAL_STOP_REASONS):
        last_stop_reason = JSONL_USER_PENDING

    result = (real_ts, last_stop_reason)
    _cache_jsonl_activity(path, key, result)
    return result


def _cache_jsonl_activity(path: str, key: Tuple[int, int],
                          value: Tuple[Optional[int], Optional[str]]) -> None:
    """Insert into _jsonl_activity_cache with LRU eviction."""
    _jsonl_activity_cache[path] = (key, value)
    _jsonl_activity_cache.move_to_end(path)
    while len(_jsonl_activity_cache) > JSONL_ACTIVITY_CACHE_MAX:
        _jsonl_activity_cache.popitem(last=False)


def read_jsonl_tail_info(project_dir: str, claude_pid=None) -> Tuple[int, Optional[str]]:
    """Return `(age_seconds, last_assistant_stop_reason)` for the project's
    newest JSONL file.

      - age_seconds: seconds since the most recent real-activity record,
        or -1 if no JSONL exists or no real activity is present in the
        tail.
      - last_assistant_stop_reason: `stop_reason` string from the most
        recent `assistant` record in the tail (e.g. `"tool_use"`,
        `"end_turn"`, `"max_tokens"`), or None if none was found.

    System / housekeeping records (anything outside the
    `JSONL_ACTIVITY_TYPES` whitelist) are filtered out of both
    fields so they do NOT register as fresh activity and do NOT
    clobber the last-assistant stop_reason signal.

    `stop_reason` is the upstream signal that distinguishes "tool
    pending mid-turn" (`tool_use`) from "response complete"
    (`end_turn` / `max_tokens` / `stop_sequence`). The event-log
    detection path uses it to hold BUSY across tool-turn
    boundaries.

    When claude_pid is provided, the exact session file is resolved
    via `~/.claude/sessions/{pid}.json` (authoritative, no slug guess).
    """
    if not project_dir:
        return -1, None
    newest = _find_newest_jsonl(project_dir, claude_pid=claude_pid)
    if newest is None:
        return -1, None
    try:
        st = os.stat(newest)
    except OSError:
        return -1, None
    real_ts, stop_reason = _parse_jsonl_tail(newest, int(st.st_mtime), st.st_size)
    if real_ts is None:
        return -1, stop_reason
    return int(time.time() - real_ts), stop_reason


def read_jsonl_age(project_dir: str, claude_pid=None) -> int:
    """Thin wrapper around `read_jsonl_tail_info` that returns only the age.

    Kept for callers that do not need the stop_reason and for backward
    compatibility with the pytest suite that mocks this function
    directly. The combined accessor is preferred in new detection code
    because it shares the underlying tail-parse cache entry.
    """
    age, _ = read_jsonl_tail_info(project_dir, claude_pid=claude_pid)
    return age


# ─── Event log reader ───
# The per-project event log is written by
# `hooks/lib.sh::ccm_append_event` as append-only JSONL at
# `$HOOK_DIR/<md5>.events.jsonl`. Each record is
# `{"ts": unix_seconds, "type": <normalized_type>}`, one per hook
# invocation. State is derived as a pure function of the event
# tail by `derive_state_from_events`; this reader is the input
# side.
#
# The reader shares the tail-read + bounded-cache pattern used by
# `_parse_jsonl_tail` so per-cycle overhead is ~0 on cache hit and a
# single 8 KB seek + JSON parse on miss. The cache key is
# (mtime_int, size) which is guaranteed to invalidate on any append
# because the file is monotonically growing.

EVENTS_TAIL_BYTES = 8192       # ~200 events at typical line length
EVENTS_TAIL_MAX_LINES = 200    # parse cap per cycle
EVENTS_CACHE_MAX = 128
_events_cache: "OrderedDict[str, Tuple[Tuple[int, int], Tuple[dict, ...]]]" = OrderedDict()


def _events_log_path(project_dir: str,
                     session_id: Optional[str] = None) -> Optional[str]:
    """Return the absolute path of a project's event log file.

    Keyed on Claude Code's session_id (UUID per session, stable for
    the session's lifetime). `session_id` semantics match
    `_hook_signal_path`: non-empty → use, ``""`` → "no session"
    (skip tmux lookup), ``None`` → fall back to cached tmux option.
    Returns None whenever no session_id is known.
    """
    if session_id is None:
        session_id = _session_id_from_tmux(project_dir)
    if not session_id:
        return None
    return os.path.join(CCM_HOOK_DIR, session_id + ".events.jsonl")


def _cache_events(path: str, key: Tuple[int, int],
                  value: Tuple[dict, ...]) -> None:
    _events_cache[path] = (key, value)
    _events_cache.move_to_end(path)
    while len(_events_cache) > EVENTS_CACHE_MAX:
        _events_cache.popitem(last=False)


def read_events_tail(project_dir: str, limit: int = 20,
                     session_id: Optional[str] = None) -> Tuple[dict, ...]:
    """Return the last `limit` events from a project's event log.

    Each event is a dict `{"ts": int, "type": str}`. Malformed lines
    are silently skipped. Returns an empty tuple when no log exists
    for the project (hook not installed, no event yet written, or
    session_id not yet resolved).

    Caller may pass `session_id` directly (slow path) or rely on the
    `@ccm_session_id` tmux option set by the most recent slow path.

    Why a tuple rather than a list: the result is cached and must be
    immutable against accidental caller mutation.

    Tail strategy: read the last EVENTS_TAIL_BYTES (8 KB) of the file
    and parse forward, then slice the most recent `limit`. An
    append-only log grows monotonically so the tail always contains
    the most recent events even if the file exceeds the tail window.
    """
    if not project_dir:
        return ()
    path = _events_log_path(project_dir, session_id=session_id)
    if not path:
        return ()
    try:
        st = os.stat(path)
    except OSError:
        return ()
    size = st.st_size
    mtime = int(st.st_mtime)
    key = (mtime, size)
    cached = _events_cache.get(path)
    if cached is not None and cached[0] == key:
        _events_cache.move_to_end(path)
        events = cached[1]
        return events[-limit:] if limit and len(events) > limit else events

    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            actual_size = f.tell()
            f.seek(max(0, actual_size - EVENTS_TAIL_BYTES))
            tail_bytes = f.read()
    except OSError:
        _cache_events(path, key, ())
        return ()

    tail = tail_bytes.decode("utf-8", errors="ignore")
    lines = tail.split("\n")
    # Drop the leading partial line if we read mid-file.
    if size > EVENTS_TAIL_BYTES and len(lines) > 1:
        lines = lines[1:]

    # Walk newest-first so the EVENTS_TAIL_MAX_LINES parse cap keeps
    # the most recent records rather than dropping them. Final list
    # is reversed back to chronological order for callers.
    events_newest_first: list = []
    for line in reversed(lines):
        if len(events_newest_first) >= EVENTS_TAIL_MAX_LINES:
            break
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        t = rec.get("type")
        ts = rec.get("ts")
        if not isinstance(t, str) or not t:
            continue
        if not isinstance(ts, (int, float)):
            continue
        events_newest_first.append({"ts": int(ts), "type": t})

    events_newest_first.reverse()
    result = tuple(events_newest_first)
    _cache_events(path, key, result)
    return result[-limit:] if limit and len(result) > limit else result


# ─── Claude Code hooks.log canary ───
# Per anthropics/claude-code#16047, an unrotated `~/.claude/hooks.log`
# can grow to many GB and silently disable all hook firing (every
# hook write fails). Claude Code does not rotate or cap this file.
# We surface a warning in `ccm status` and the dashboard footer when
# the size crosses a threshold so the user can `: > ~/.claude/hooks.log`
# and recover hook delivery.

CLAUDE_HOOKS_LOG = os.path.expanduser("~/.claude/hooks.log")
HOOKS_LOG_WARN_BYTES = int(
    os.environ.get("CCM_HOOKS_LOG_WARN_BYTES", str(100 * 1024 * 1024))  # 100 MB
)


def hooks_log_size() -> int:
    """Return the byte size of `~/.claude/hooks.log`, or -1 if absent."""
    try:
        return os.path.getsize(CLAUDE_HOOKS_LOG)
    except OSError:
        return -1


def hooks_log_warning() -> str:
    """Return a one-line warning string when hooks.log is bloated past
    the threshold, or "" if everything is fine. The message tells the
    user the exact remediation command — this is a self-service fix.
    """
    size = hooks_log_size()
    if size < HOOKS_LOG_WARN_BYTES:
        return ""
    mb = size / (1024 * 1024)
    return (
        f"Claude hooks.log is {mb:.0f} MB — hooks may be silently failing. "
        f"Run `: > ~/.claude/hooks.log` to restore hook delivery (#16047)."
    )


# ─── Claude Code global settings canary ───
# Detect configuration flags in ~/.claude/settings.json that silently
# disable ccm's fast-path signals. If the user sets these, state
# detection degrades without any obvious error — we surface a warning
# so the cause is discoverable.

CLAUDE_SETTINGS_FILE = os.path.expanduser("~/.claude/settings.json")


def _read_claude_settings():
    try:
        with open(CLAUDE_SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def disable_all_hooks_warning() -> str:
    """Return a warning string if `disableAllHooks: true` is set in
    ~/.claude/settings.json, otherwise "".

    Per Claude Code's docs, this setting disables ALL hooks AND any
    custom statusLine — ccm's entire fast-path signal goes dark with
    no error. Same class of silent failure as the hooks.log bloat
    canary.

    Scope: only the user-level file `~/.claude/settings.json` is
    checked. Project-scope settings (`<project>/.claude/settings.json`)
    and enterprise managed settings (e.g.
    `/Library/Application Support/ClaudeCode/managed-settings.json`
    on macOS) are NOT inspected. The setting is also valid in those
    locations, so a managed-policy or per-project disable will not
    surface a warning here. Adding cross-scope checks would require
    walking Claude Code's full settings precedence chain, which is
    out of scope for this canary.
    """
    data = _read_claude_settings()
    if not data:
        return ""
    if data.get("disableAllHooks") is True:
        return (
            "Claude Code `disableAllHooks: true` is set in "
            "~/.claude/settings.json — this disables ALL hooks AND any "
            "custom `statusLine` command. ccm state detection falls "
            "back to JSONL polling and process tree only, and any "
            "embedded statusLine you configured will stop rendering. "
            "Remove the setting to restore real-time hook signals."
        )
    return ""


def managed_hooks_only_warning() -> str:
    """Return a warning string if `allowManagedHooksOnly: true` is set
    in ~/.claude/settings.json, otherwise "".

    Per Claude Code's docs, when this is set in *managed* settings,
    every user-scope hook (which is exactly where ccm installs all
    14 of its hooks) is silently blocked with no error.
    The result looks identical to a broken Claude Code install from
    ccm's perspective: no hooks fire, ever.

    Scope (important caveat): only the user-level file
    `~/.claude/settings.json` is checked. The setting is most
    commonly placed in an enterprise-managed settings file (e.g.
    `/Library/Application Support/ClaudeCode/managed-settings.json`
    on macOS), which is the actual deployment scenario this flag
    targets. ccm does NOT walk Claude Code's settings precedence
    chain — that path varies by OS and is not stably documented.

    This canary therefore catches:
      - a user who set the flag in their own file by mistake or test
      - a managed file symlinked to the user-scope path
    But it does NOT catch the typical enterprise deployment where
    the flag lives in a separate managed file. Users in managed
    enterprise environments should not expect a warning here even
    when ccm hooks are silently disabled.
    """
    data = _read_claude_settings()
    if not data:
        return ""
    if data.get("allowManagedHooksOnly") is True:
        return (
            "Claude Code `allowManagedHooksOnly: true` is set — all "
            "user-scope hooks (including every ccm hook) are blocked. "
            "Remove the setting or move ccm hooks to managed scope to "
            "restore real-time signals."
        )
    return ""


# ─── Cluster-SHELL-transition detection ───
# When Claude Code dies repeatedly (most commonly the macOS silent-
# exit regression, anthropics/claude-code#48069), ccm observes a
# rapid SHELL → (user or ccm re-attach) → BUSY → IDLE → SHELL loop.
# We record each SHELL transition timestamp in a per-window tmux
# option `@ccm_shell_history` and surface a warning if the count in
# the last SHELL_CLUSTER_WINDOW exceeds SHELL_CLUSTER_COUNT.

_SHELL_HISTORY_OPT = "@ccm_shell_history"


def _parse_shell_history_raw(raw: str) -> list:
    """Parse a raw `@ccm_shell_history` value (comma-separated unix
    timestamps) into a list of ints, newest first, with entries
    older than SHELL_CLUSTER_WINDOW filtered out. Shared by the
    per-window read and the batch read.
    """
    if not raw:
        return []
    horizon = int(time.time()) - SHELL_CLUSTER_WINDOW
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ts = int(item)
        except ValueError:
            continue
        if ts >= horizon:
            out.append(ts)
    return out


def _read_shell_history(win_target: str) -> list:
    """Read the SHELL transition timestamp history for a single window.

    Used by the rare write path (`_push_shell_transition`). The hot
    read path (`shell_cluster_warnings`) goes through
    `_read_all_shell_histories` to amortise the subprocess cost
    across windows.
    """
    raw = tmux_cmd("show-option", "-wqv", "-t", win_target, _SHELL_HISTORY_OPT)
    return _parse_shell_history_raw(raw)


def _read_all_shell_histories() -> dict:
    """Return `{win_target: history_list}` for every window in one
    `tmux list-windows -a` subprocess instead of one show-option
    per window. Keeps `shell_cluster_warnings` O(1) subprocess
    regardless of project count.
    """
    raw = tmux_cmd(
        "list-windows", "-a",
        "-F", "#{session_name}:#{window_index}\t#{" + _SHELL_HISTORY_OPT + "}",
    )
    histories = {}
    if not raw:
        return histories
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        win_target, _, history_raw = line.partition("\t")
        histories[win_target] = _parse_shell_history_raw(history_raw)
    return histories


def _push_shell_transition(win_target: str) -> None:
    """Record a new SHELL transition for a window.

    Prepends the current timestamp to `@ccm_shell_history`, trims
    entries older than SHELL_CLUSTER_WINDOW via `_read_shell_history`,
    and caps the stored size at 2 × SHELL_CLUSTER_COUNT entries (or
    a floor of 6) to prevent unbounded growth of the tmux option.
    Every push writes back a capped, trimmed history so that even
    pre-existing oversized values converge on the cap.
    """
    existing = _read_shell_history(win_target)
    now = int(time.time())
    max_entries = max(SHELL_CLUSTER_COUNT * 2, 6)

    # Deduplicate same-second pushes so two code paths hitting
    # apply_actions for the same scan cycle don't double-count one
    # transition. We still write back the capped history though,
    # so pre-existing oversized values are normalised.
    if existing and existing[0] == now:
        updated = existing
    else:
        updated = [now] + existing

    updated = updated[:max_entries]
    tmux_cmd(
        "set-option", "-wt", win_target, _SHELL_HISTORY_OPT,
        ",".join(str(t) for t in updated),
    )


def _format_cluster_warning(history_len: int, project_name: str) -> str:
    label = f"{project_name}: " if project_name else ""
    return (
        f"{label}Claude Code exited {history_len}+ times in "
        f"{SHELL_CLUSTER_WINDOW // 60} min — likely "
        f"{SHELL_CLUSTER_ISSUE} ({SHELL_CLUSTER_ISSUE_NOTE}). "
        f"The conversation auto-restores via `claude --continue`."
    )


def shell_cluster_warning(win_target: str, project_name: str = "") -> str:
    """Return a one-line warning if this window has hit the SHELL
    cluster threshold, otherwise "". Per-window read; for bulk
    use prefer `shell_cluster_warnings` which batches.
    """
    history = _read_shell_history(win_target)
    if len(history) < SHELL_CLUSTER_COUNT:
        return ""
    return _format_cluster_warning(len(history), project_name)


def shell_cluster_warnings(projects) -> list:
    """Return a list of warning strings, one per project that has
    crossed the SHELL cluster threshold. Empty list if all quiet.

    Reads every window's `@ccm_shell_history` in a single
    `tmux list-windows` subprocess instead of one per project, so
    the cost is O(1) subprocess regardless of project count.
    """
    histories = _read_all_shell_histories()
    out = []
    for p in projects:
        history = histories.get(p.win_target, [])
        if len(history) < SHELL_CLUSTER_COUNT:
            continue
        out.append(_format_cluster_warning(len(history), p.name))
    return out


# ─── State detection ───
# Moved to lib/ccm_detection.py. Re-exported at the bottom of this
# file so `from ccm_core import DETECTION_RULES / detect_window_state / …`
# continues to work for dashboard, inject_status, and pytest.


# ─── Project data ───

class Project:
    __slots__ = (
        "win_target", "win_idx", "name", "dir", "state",
        "branch", "ports", "completed_at", "bg_active", "pane_count",
        "sort_key",
    )

    def __init__(self, win_target, win_idx, name, directory, state,
                 branch="", ports="", completed_at=0, bg_active=False,
                 pane_count=1):
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
        self.sort_key = (STATE_PRIORITY.get(state, 4), -(completed_at or 0))


def read_cache_file(cache_dir, directory):
    expanded = os.path.expanduser(directory)
    try:
        expanded = os.path.realpath(expanded)
    except OSError:
        pass
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


def build_project_list(fast=False):
    """Build project list from tmux. If fast, skip git/port refresh.
    Set CCM_MOCK_STATE=1 env var or @ccm-mock-state tmux option to force
    fast mode (uses @ccm_prev_state only — for screenshots).
    """
    if (os.environ.get("CCM_MOCK_STATE") == "1"
            or tmux_cmd("show-option", "-gqv", "@ccm-mock-state") == "1"):
        fast = True
    raw = tmux_cmd(
        "list-windows", "-a", "-F",
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_dir}\t"
        "#{@ccm_prev_state}\t#{@ccm_completed_at}\t#{window_activity}\t"
        "#{@ccm_bg_active}\t#{@ccm_session_id}"
    )
    if not raw:
        return []

    ps_lines = ps_snapshot().strip().split("\n") if not fast else []
    panes_cache = []
    if not fast:
        panes_raw = tmux_cmd("list-panes", "-a", "-F",
                             "#{session_name}:#{window_index}\t#{pane_pid}\t#{pane_id}\t#{pane_current_command}\t#{pane_active}\t#{pane_height}")
        for line in panes_raw.split("\n"):
            parts = line.split("\t")
            # 6-tuple format: (target, pid, pane_id, current_command,
            # pane_active, pane_height). All production callers
            # (here + cmd_debug_trace) use this shape.
            if len(parts) >= 6:
                panes_cache.append(tuple(parts[:6]))

    own_pgid = str(os.getpgrp())
    seen_dirs = set()
    projects = []

    for line in raw.split("\n"):
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        win_target, project, proj_dir = parts[0], parts[1], parts[2]
        prev_state, completed_at_str, win_activity_str = (
            parts[3], parts[4], parts[5]
        )
        # @ccm_bg_active and @ccm_session_id are recent additions;
        # older tmux windows may not have them — treat absent as
        # "no background activity" / "no session known".
        bg_active_str = parts[6] if len(parts) >= 7 else ""
        # cached_session_id semantics:
        #   - empty string ""  → fetched, but no session_id cached
        #     (slow path will resolve via pid chain; build_detection_context
        #     skips its own show-option since the bulk value is authoritative)
        #   - None             → format string was missing the field
        #     (legacy tmux window, fallback to per-call show-option)
        # Both forms are distinct so build_detection_context can trust
        # an empty cached value as the authoritative answer.
        cached_session_id = parts[7] if len(parts) >= 8 else None

        if not project:
            continue

        try:
            resolved = os.path.realpath(os.path.expanduser(proj_dir))
        except OSError:
            resolved = proj_dir
        if resolved in seen_dirs:
            continue
        seen_dirs.add(resolved)

        win_idx = win_target.split(":")[-1]

        completed_at = 0
        if completed_at_str and completed_at_str != "0":
            try:
                completed_at = int(completed_at_str)
            except ValueError:
                pass

        win_activity = 0
        if win_activity_str:
            try:
                win_activity = int(win_activity_str)
            except ValueError:
                pass

        # Per-project exception barrier. A bug in detection for one
        # project must not freeze every other project's state. On
        # failure, carry forward `@ccm_prev_state` (worst case: this
        # project's state stays stale for a tick, instead of the
        # whole loop dying and ALL projects freezing — the failure
        # mode that motivated this barrier).
        try:
            if fast:
                state = evaluate_fast(
                    prev_state, proj_dir,
                    session_id=cached_session_id,
                )
            else:
                state = detect_window_state(
                    win_target, proj_dir, prev_state,
                    panes_cache, ps_lines, own_pgid,
                    prev_bg_active=bool(bg_active_str),
                    cached_session_id=cached_session_id,
                )
        except Exception:
            log_caught_exception(f"build_project_list[{project}]")
            state = prev_state or "IDLE"

        sort_ts = max(completed_at, win_activity) if win_activity else completed_at

        branch = read_cache_file(CCM_GIT_CACHE_DIR, proj_dir) if proj_dir else ""
        ports = read_cache_file(CCM_PORT_CACHE_DIR, proj_dir) if proj_dir else ""

        # Count panes in this window (slow path only — fast path
        # has no panes_cache and pane_count would always show 1).
        if fast:
            pane_count = 1
        else:
            pane_count = sum(1 for pc in panes_cache if pc[0] == win_target) or 1

        projects.append(Project(
            win_target=win_target, win_idx=win_idx, name=project,
            directory=proj_dir, state=state, branch=branch, ports=ports,
            completed_at=sort_ts,
            bg_active=bool(bg_active_str and bg_active_str != "0"),
            pane_count=pane_count,
        ))

    projects.sort(key=lambda p: p.sort_key)
    return projects


def hooks_configured():
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


# ─── Desktop notifications ───

_TERMINAL_NOTIFIER_PATH = None
_TERMINAL_NOTIFIER_CHECKED = False


def _terminal_notifier_path():
    """Return the path to `terminal-notifier` if installed, else None.

    Result is cached for the process lifetime — `which` is fast but
    we hit this on every notification.
    """
    global _TERMINAL_NOTIFIER_PATH, _TERMINAL_NOTIFIER_CHECKED
    if _TERMINAL_NOTIFIER_CHECKED:
        return _TERMINAL_NOTIFIER_PATH
    _TERMINAL_NOTIFIER_CHECKED = True
    try:
        r = subprocess.run(["which", "terminal-notifier"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            _TERMINAL_NOTIFIER_PATH = r.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return _TERMINAL_NOTIFIER_PATH


def notify(state, project, detail=""):
    """Send desktop notification for state changes.

    Controlled by @ccm-notify tmux option: off, permit, completed,
    permit,completed, all. `detail` is optional context (e.g.
    "Bash: rm -rf ..." for PERMIT).

    macOS notifications accumulate in Notification Center — a
    user who runs ccm continuously across many projects can build
    up hundreds of stale entries that drive WindowServer /
    NotificationCenter to high CPU. Two mitigations:

      1. If `terminal-notifier` is installed, use it with
         `-group ccm-<project>` so each project shows at most
         one notification (new replaces old). Recommended.
      2. Otherwise fall back to `osascript display notification`,
         which has no group / replace primitive — users who hit
         the NC-accumulation problem should either install
         terminal-notifier (`brew install terminal-notifier`),
         set `@ccm-notify permit` to drop the more-frequent
         COMPLETED notifications, or run `ccm clear-notifications`
         periodically.
    """
    setting = tmux_cmd("show-option", "-gqv", "@ccm-notify") or "permit,completed"
    if setting == "off":
        return

    state_lower = state.lower()
    if setting != "all" and state_lower not in setting:
        return

    sound_setting = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound") or "off"
    sound_name = (tmux_cmd("show-option", "-gqv", "@ccm-notify-sound-name") or "Glass") if sound_setting == "on" else ""

    permit_body = f"Permission required: {detail}" if detail else \
                  "Action required — respond to the permission prompt"
    messages = {
        "PERMIT": (f"ccm ⚠ {project}",
                   permit_body,
                   sound_name),
        "COMPLETED": (f"ccm ✔ {project}",
                      "Claude has finished responding — review the output when ready",
                      sound_name),
        "BUSY":   (f"ccm ◉ {project}",
                   "Claude is now processing your request",
                   ""),
        "IDLE":   (f"ccm {project}",
                   "Waiting for your input",
                   ""),
    }

    if state not in messages:
        return

    title, body, sound = messages[state]
    # Group ID per-project so a fresh notification for the same
    # project replaces (rather than accumulates with) the previous
    # one. terminal-notifier respects -group; osascript does not.
    group_id = f"ccm-{project}"

    tn_path = _terminal_notifier_path()
    if tn_path:
        # Intentionally NO `-sender com.apple.Terminal`. Specifying
        # a sender bundle id makes macOS deliver the notification
        # under that app's identity, which silently drops it for
        # every user not actually running Terminal.app (iTerm2,
        # WezTerm, kitty, ghostty, …). Letting terminal-notifier
        # use its own bundle id means the user grants notification
        # permission once and it works regardless of terminal.
        cmd_args = [tn_path,
                    "-message", body,
                    "-title", title,
                    "-group", group_id]
        if sound:
            cmd_args.extend(["-sound", sound])
        try:
            subprocess.Popen(cmd_args,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass  # fall through to osascript

    try:
        # Escape double quotes and backslashes for AppleScript string literals
        esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
        esc_body = body.replace("\\", "\\\\").replace('"', '\\"')
        sound_opt = ""
        if sound:
            esc_sound = sound.replace("\\", "\\\\").replace('"', '\\"')
            sound_opt = f' sound name "{esc_sound}"'
        cmd = f'display notification "{esc_body}" with title "{esc_title}"{sound_opt}'
        subprocess.Popen(["osascript", "-e", cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        try:
            subprocess.Popen(["notify-send", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass


def clear_notifications():
    """Remove ccm-sent notifications from the macOS Notification
    Center. Requires `terminal-notifier` (the only command-line
    way to enumerate / remove macOS notifications).

    Scopes the removal to ccm by enumerating `-list ALL` and
    deleting only group ids prefixed with `ccm-` (the convention
    `notify()` uses). `-remove ALL` was tempting but would also
    delete notifications a user has sent via terminal-notifier
    from unrelated scripts (deploy alerts, monitoring, …). The
    enumerate-then-filter approach pays one extra subprocess but
    keeps the user's other notifications intact.

    Notifications that pre-date the terminal-notifier integration
    (delivered via `osascript`) have no programmatic remove path
    and remain in Notification Center until the user dismisses
    them manually — `osascript display notification` does not
    expose an identifier the system will accept for later removal.

    Returns the count of removed notifications on success, -1 if
    terminal-notifier is not installed or the enumeration failed.
    """
    tn_path = _terminal_notifier_path()
    if not tn_path:
        return -1
    try:
        listing = subprocess.run(
            [tn_path, "-list", "ALL"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return -1

    # Other apps' notification titles may contain bytes that are not
    # valid UTF-8; decode permissively so a single bad title does not
    # abort the entire scrub.
    listing_text = (listing.stdout or b"").decode("utf-8", errors="replace")
    removed = 0
    for line in listing_text.splitlines()[1:]:  # skip header
        # `-list ALL` emits a TSV: GroupID<TAB>Title<TAB>...
        group_id = line.split("\t", 1)[0].strip()
        if not group_id.startswith("ccm-"):
            continue
        try:
            subprocess.run(
                [tn_path, "-remove", group_id],
                capture_output=True, text=True, timeout=5,
            )
            removed += 1
        except (subprocess.TimeoutExpired, OSError):
            # Best-effort: log nothing, continue with remaining ids
            continue
    return removed


# ─── Window name update ───

def update_window_names(projects):
    """Update tmux window names with state icons."""
    all_windows = tmux_cmd(
        "list-windows", "-a", "-F",
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{window_name}"
    )
    if not all_windows:
        return

    project_states = {p.win_target: p.state for p in projects}

    for line in all_windows.split("\n"):
        parts = line.split("\t")
        if len(parts) < 3 or not parts[1]:
            continue
        win_target, project, current_name = parts[0], parts[1], parts[2]
        state = project_states.get(win_target, "IDLE")
        icon = STATE_ICONS.get(state, "●")
        new_name = f"{icon} {project}"
        if current_name != new_name:
            tmux_cmd("rename-window", "-t", win_target, new_name)


# ─── Auto-exit idle sessions ───

def auto_exit_idle(projects):
    """Exit idle Claude Code sessions to free resources."""
    idle_timeout_str = tmux_cmd("show-option", "-gqv", "@ccm-idle-timeout")
    if idle_timeout_str:
        try:
            idle_timeout = int(idle_timeout_str) * 60
        except ValueError:
            idle_timeout = IDLE_EXIT_TIMEOUT
    else:
        idle_timeout = IDLE_EXIT_TIMEOUT

    if idle_timeout <= 0:
        return

    now = int(time.time())

    # Get current window to exclude
    current_session = tmux_cmd("display-message", "-p", "#{session_name}")
    current_win = tmux_cmd("display-message", "-p", "#{window_index}")
    current_target = f"{current_session}:{current_win}"

    # Get window activity for all windows
    activity_raw = tmux_cmd(
        "list-windows", "-a", "-F",
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_prev_state}\t#{@ccm_completed_at}\t#{window_activity}"
    )
    if not activity_raw:
        return

    for line in activity_raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 5:
            parts.append("")
        win_target, project, prev_state, completed_at_str, win_activity_str = parts[:5]

        if not project or prev_state != "IDLE":
            continue
        if win_target == current_target:
            continue

        # Parse timestamps
        completed_at = 0
        if completed_at_str and completed_at_str != "0":
            try:
                completed_at = int(completed_at_str)
            except ValueError:
                pass

        win_activity = 0
        if win_activity_str and win_activity_str != "0":
            try:
                win_activity = int(win_activity_str)
            except ValueError:
                pass

        idle_since = max(completed_at, win_activity)

        if idle_since == 0:
            tmux_cmd("set-option", "-wt", win_target, "@ccm_completed_at", str(now))
            continue

        idle_duration = now - idle_since
        if idle_duration >= idle_timeout:
            # Cancel any partial input, then cleanly exit Claude Code
            tmux_cmd("send-keys", "-t", win_target, "Escape")
            time.sleep(0.1)
            tmux_cmd("send-keys", "-t", win_target, "/exit", "Enter")
            time.sleep(0.5)
            # Clear the pane so auto-restart shows a clean screen
            tmux_cmd("send-keys", "-t", win_target, "clear", "Enter")
            _set_win_state(win_target, "SHELL")
            # Force autosave after auto-exit to preserve project in snapshot
            _force_autosave()


# ─── Autosave ───

def _force_autosave():
    """Force an immediate autosave."""
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        pass


def periodic_autosave():
    """Save autosave snapshot if 2 minutes have passed."""
    marker = os.path.join(CCM_TMP_DIR, "autosave-time")
    now = int(time.time())
    last_save = 0
    try:
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as f:
                last_save = int(f.read().strip())
    except (OSError, ValueError):
        pass

    if now - last_save < 120:
        return

    # Check if any ccm projects exist
    check = tmux_cmd("list-windows", "-a", "-F", "#{@ccm_project}")
    has_projects = any(line.strip() for line in check.split("\n") if line.strip())
    if not has_projects:
        return

    try:
        cmd_snapshot_save("_autosave", quiet=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(now))
    except Exception:
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


def auto_start_claude(win_target):
    """Auto-start Claude Code if @ccm-auto-start is on."""
    setting = tmux_cmd("show-option", "-gqv", "@ccm-auto-start") or "on"
    if setting != "on":
        return
    tmux_cmd("send-keys", "-t", win_target, CLAUDE_CMD, "Enter")


def reset_window_after_attach(win_target):
    """Run the post-attach reset bundle for a project window.

    Called whenever the user attaches to a project (CLI `cmd_attach`,
    dashboard `_do_attach`, dashboard tree-mode attach). All side
    effects are keyed off `@ccm_dir`; on a non-ccm window this is a
    no-op:

    1. Unset `@ccm_completed_at` so the `* elapsed` completion
       marker disappears (stale completion markers from before
       the user attached should not appear to follow the attach).
    2. Unset `@ccm_shell_history` so the cluster-SHELL canary
       (#48069) is acknowledged. The warning will reappear only if
       NEW transitions cluster after the attach.

    `@ccm_prev_state` is intentionally NOT wiped: the
    `startup_transient_raw_busy` rule uses pid age as the
    monotonic discriminator and prev_state as a corroborating
    signal. Wiping it would conflate startup transients with
    real in-flight responses (both raw=BUSY + prev="") and
    produce ~10 s of false BUSY on every attach. The other wipes
    are cosmetic (completed_at) or per-canary (shell_history);
    they do not participate in rule evaluation.

    Symmetric across all attach paths — do not duplicate these
    set-option calls inline elsewhere.
    """
    proj_dir = tmux_cmd("show-option", "-wqv", "-t", win_target, "@ccm_dir")
    if not proj_dir:
        return
    tmux_cmd("set-option", "-wt", win_target, "-u", "@ccm_completed_at")
    tmux_cmd("set-option", "-wt", win_target, "-u", "@ccm_shell_history")
    auto_focus_attention_pane(win_target)


def auto_focus_attention_pane(win_target):
    """If the window has a pane in PERMIT state and that pane is
    not currently the active one, switch focus to it.

    Rationale: `detect_window_raw` aggregates state across panes
    and surfaces `⚠ PERMIT` for the whole window when any pane
    has a permission modal up. The user attaches to that window
    expecting to deal with the modal — but tmux drops them on
    whichever pane was last active, which may not be the one
    actually waiting. Auto-focusing on attach saves a manual
    `prefix + arrow` step and prevents the user from typing into
    the wrong pane.

    Scope is intentionally narrow: PERMIT only. BUSY panes are
    interesting to monitor but do not require user input, so
    auto-stealing focus from where the user wanted to be would be
    surprising. Only fires from `reset_window_after_attach` (i.e.
    ccm-mediated attach: `cmd_attach`, dashboard `_do_attach`).
    Manual `prefix + N` window-switch is not hooked.

    No-op on single-pane windows. No-op if no pane is PERMIT. No-op
    if the active pane already is PERMIT. The 2-row format we ask
    tmux for has all the data needed; `detect_pane_state` is run
    for each pane to determine state.
    """
    proj_dir = tmux_cmd("show-option", "-wqv", "-t", win_target, "@ccm_dir")
    if not proj_dir:
        return
    panes_raw = tmux_cmd(
        "list-panes", "-t", win_target, "-F",
        "#{pane_pid}\t#{pane_id}\t#{pane_current_command}\t#{pane_active}\t#{pane_height}",
    )
    if not panes_raw:
        return
    rows = []
    for line in panes_raw.split("\n"):
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        pid, pane_id, cmd, active, height_str = parts[:5]
        try:
            height = int(height_str)
        except ValueError:
            height = 0
        rows.append((pid, pane_id, cmd, active == "1", height))

    if len(rows) < 2:
        return

    # Re-import here to avoid the import cycle at module load.
    from ccm_detection import detect_pane_state

    ps_lines = ps_snapshot().strip().split("\n")
    own_pgid = str(os.getpgrp())

    permit_pane = None
    active_is_permit = False
    for pid, pane_id, cmd, is_active, height in rows:
        # Apply the same sliver filter as detect_window_raw — a
        # short pane cannot reliably report PERMIT either, so we
        # do not auto-focus it.
        if height and height < SLIVER_HEIGHT_THRESHOLD:
            continue
        state = detect_pane_state(pid, pane_id, ps_lines, own_pgid,
                                   current_command=cmd)
        if state != "PERMIT":
            continue
        if is_active:
            active_is_permit = True
            break
        if permit_pane is None:
            permit_pane = pane_id

    if active_is_permit:
        return  # User is already on the right pane.
    if permit_pane is None:
        return  # No PERMIT pane to focus.
    tmux_cmd("select-pane", "-t", permit_pane)


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




# ─── Re-exported detection API ───
# Import at the bottom of the file so ccm_detection.py can pull in
# the constants / tmux helpers / hook-signal / JSONL readers defined
# above without hitting a circular import.
from ccm_detection import (  # noqa: E402
    DETECTION_RULES,
    DetectionContext,
    Action,
    Rule,
    USE_RAW,
    EVENT_CLASSES,
    TERMINAL_STOP_REASONS,
    apply_actions,
    build_detection_context,
    build_fast_context,
    capture_pane_bottom,
    derive_state_from_events,
    detect_pane_state,
    detect_window_raw,
    detect_window_state,
    evaluate_fast,
    evaluate_rules,
    find_claude_pid,
    has_children,
    _event_log_enabled,
    _set_win_state,
    _FAST_PREV_TO_RAW,
)


# ─── Re-exported command handlers ───
# Same pattern as the detection re-export above: import after the
# module body so `ccm_commands` can freely reach back into `ccm_core`
# for tmux helpers, constants, and logging without a circular import.
from ccm_commands import (  # noqa: E402
    _autosave_trigger,
    _sanitize_snapshot_name,
    cmd_add,
    cmd_attach,
    cmd_capture,
    cmd_debug_trace,
    cmd_doctor,
    cmd_errors,
    cmd_list,
    cmd_open,
    cmd_register,
    cmd_remove,
    cmd_rename,
    cmd_reset_window,
    cmd_send,
    cmd_snapshot_delete,
    cmd_snapshot_list,
    cmd_snapshot_load,
    cmd_snapshot_save,
    cmd_stop,
    cmd_unregister,
)


# ─── Re-exported render API ───
# Same pattern: ccm_render imports from ccm_core, then we re-export
# its public surface so existing callers can keep using
# `ccm_core.print_status` / `ccm_core.signal_age_suffix` etc.
from ccm_render import (  # noqa: E402
    SIGNAL_STALE_DISPLAY_THRESHOLD,
    display_width,
    format_dir,
    format_elapsed,
    pad_to_width,
    print_ports,
    print_status,
    print_statusline,
    print_tree,
    signal_age_suffix,
    truncate_to_width,
)


# ─── CLI entry point ───

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]

    if cmd == "status":
        print_status()
    elif cmd == "ports":
        print_ports()
    elif cmd == "tree":
        print_tree()
    elif cmd == "statusline":
        print_statusline()
    elif cmd == "add":
        cmd_add(args[0] if args else "", args[1] if len(args) > 1 else "")
    elif cmd == "open":
        cmd_open(args[0] if args else "", args[1] if len(args) > 1 else "")
    elif cmd == "register":
        cmd_register(args[0] if args else "", args[1] if len(args) > 1 else "")
    elif cmd == "unregister":
        cmd_unregister(args[0] if args else "")
    elif cmd == "rename":
        cmd_rename(args[0] if args else "", args[1] if len(args) > 1 else "")
    elif cmd == "remove":
        cmd_remove(args[0] if args else "")
    elif cmd == "attach":
        cmd_attach(args[0] if args else "")
    elif cmd == "list":
        cmd_list()
    elif cmd == "capture":
        cmd_capture(args)
    elif cmd == "stop":
        cmd_stop(args[0] if args else "")
    elif cmd == "send":
        cmd_send(args)
    elif cmd == "snapshot-save":
        cmd_snapshot_save(args[0] if args else "")
    elif cmd == "snapshot-load":
        cmd_snapshot_load(args[0] if args else "")
    elif cmd == "snapshot-list":
        cmd_snapshot_list()
    elif cmd == "snapshot-delete":
        cmd_snapshot_delete(args[0] if args else "")
    elif cmd == "reset-window":
        cmd_reset_window()
    elif cmd == "errors":
        # `ccm errors [--clear]` — view the silent-exception log.
        # See `log_caught_exception` for what this records.
        cmd_errors(args)
    elif cmd == "doctor":
        # `ccm doctor` — single self-check command. Aggregates
        # canaries, hook state, project inventory, and recent
        # errors into one diagnostic snapshot.
        cmd_doctor()
    elif cmd == "clear-notifications":
        # `ccm clear-notifications` — remove only ccm-prefixed
        # notifications from macOS Notification Center. Other
        # terminal-notifier notifications (from unrelated scripts)
        # are left alone. Returns -1 if terminal-notifier is not
        # installed; otherwise the count of removed notifications.
        rc = clear_notifications()
        if rc < 0:
            ccm_warn(
                "terminal-notifier is not installed. "
                "Install with: brew install terminal-notifier"
            )
            sys.exit(1)
        if rc == 0:
            ccm_info("No ccm notifications were in Notification Center")
        else:
            ccm_info(f"Cleared {rc} ccm notification(s) from Notification Center")
    elif cmd == "debug":
        # `ccm debug trace <project> [interval]`
        sub = args[0] if args else ""
        if sub == "trace":
            proj = args[1] if len(args) > 1 else ""
            if not proj:
                print("Usage: ccm debug trace <project-name-or-substring> [interval-seconds]",
                      file=sys.stderr)
                sys.exit(2)
            try:
                interval = float(args[2]) if len(args) > 2 else 0.3
            except ValueError:
                print(f"Invalid interval: {args[2]!r}", file=sys.stderr)
                sys.exit(2)
            cmd_debug_trace(proj, interval=interval)
        else:
            print(f"Unknown debug subcommand: {sub!r}", file=sys.stderr)
            print("Available: trace", file=sys.stderr)
            sys.exit(2)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
