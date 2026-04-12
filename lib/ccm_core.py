#!/usr/bin/env python3
"""ccm core — shared constants, helpers, state detection, and project list building."""

import contextlib
import glob as _glob_mod
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

# ─── Constants ───

CCM_ROOT = os.environ.get(
    "CCM_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CCM_TMP_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"ccm-{os.getuid()}")
CCM_HOOK_DIR = os.path.join(CCM_TMP_DIR, "hooks")
CCM_SNAPSHOT_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "ccm",
    "snapshots",
)
CCM_GIT_CACHE_DIR = os.path.join(CCM_TMP_DIR, "git-cache")
CCM_PORT_CACHE_DIR = os.path.join(CCM_TMP_DIR, "port-cache")

DONE_TIMEOUT = int(os.environ.get("CCM_DONE_TIMEOUT", "30"))
# Hook signal age (seconds) below which a BUSY signal is treated as "fresh"
# and trusted unconditionally — bypasses the slower pipeline when multiple
# projects contend for evaluation time.
HOOK_FRESH_THRESHOLD = 2
# Minimum seconds before showing DONE after Stop hook fires.
# Suppresses false DONE at multi-turn boundaries where Stop fires
# between tool executions and the next turn starts within seconds.
DONE_SETTLE_TIME = int(os.environ.get("CCM_DONE_SETTLE_TIME", "3"))
PERMIT_MAX_TIMEOUT = int(os.environ.get("CCM_PERMIT_MAX_TIMEOUT", "600"))  # 10 min safety net
IDLE_EXIT_TIMEOUT = int(os.environ.get("CCM_IDLE_EXIT_TIMEOUT", "600"))  # 10 minutes default
CACHE_TTL = int(os.environ.get("CCM_CACHE_TTL", "30"))  # git/port cache seconds

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
# Permission dialog footer markers (v2.1.101+). "Tab to amend" and
# "ctrl+e to explain" are unique to the permission prompt — other
# menus (slash commands, /hooks) only show "Esc to cancel". Used as a
# hook-independent fallback when Claude Code stops firing hooks
# mid-session (see anthropics/claude-code#16047, #13193).
#
# Anchored to the start of the line (after optional whitespace and the
# "Esc to cancel · " prefix) so that the same words appearing in the
# body of a Claude response — e.g. "use ctrl+e to explain" inside an
# answer — do not falsely trigger PERMIT.
PATTERN_PERMIT_FOOTER = re.compile(
    r"^\s*Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
)

# Claude Code process name in `ps` output
CLAUDE_PROCESS_NAME = "claude"
# Processes that are always children of Claude Code and should be ignored
# when checking for meaningful child processes (tool execution).
IGNORED_CHILDREN = {"caffeinate"}

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

STATE_PRIORITY = {"PERMIT": 0, "DONE": 1, "BUSY": 2, "IDLE": 3, "SHELL": 4, "DOWN": 5}
STATE_ICONS = {
    "PERMIT": "⚠", "BUSY": "◉", "DONE": "✔", "IDLE": "●", "SHELL": "■", "DOWN": "○",
}


# ─── Subprocess helpers ───

def tmux_cmd(*args, timeout=5):
    """Run tmux command, return stdout."""
    try:
        r = subprocess.run(
            ["tmux"] + list(args), capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip() if r.returncode == 0 else ""
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
    """Single ps call for scan cycle."""
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,ppid,pgid,comm"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def md5_hash(s):
    return hashlib.md5(s.encode()).hexdigest()


# ─── Session detection ───

def get_session():
    popup_file = os.path.join(CCM_TMP_DIR, "popup-session")
    try:
        if os.path.exists(popup_file):
            age = time.time() - os.path.getmtime(popup_file)
            if age < 60:
                with open(popup_file) as f:
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
# - STATE: one of BUSY, DONE, PERMIT
# - Extra fields are reserved for future use and ignored by current code
# Written by: hooks/on-prompt-submit.sh, hooks/on-pre-tool-use.sh,
#             hooks/on-stop.sh, hooks/on-notification.sh

VALID_HOOK_STATES = {"BUSY", "DONE", "PERMIT", "SHELL"}


def _resolve_project_dir(project_dir):
    """Expand and resolve a project directory path."""
    expanded = os.path.expanduser(project_dir)
    try:
        expanded = os.path.realpath(expanded)
    except OSError:
        pass
    return expanded


def _hook_signal_path(project_dir):
    """Get the hook signal file path for a project directory."""
    expanded = _resolve_project_dir(project_dir)
    return os.path.join(CCM_HOOK_DIR, md5_hash(expanded))


def read_hook_signal(project_dir):
    """Read hook signal file. Returns (timestamp, state, detail) or None.
    Detail is optional extra info (e.g., tool name for PERMIT).
    """
    hook_file = _hook_signal_path(project_dir)
    try:
        with open(hook_file) as f:
            content = f.read().strip()
        parts = content.split(None, 2)  # split into at most 3 parts
        if len(parts) >= 2 and parts[1] in VALID_HOOK_STATES:
            detail = parts[2] if len(parts) >= 3 else ""
            return int(parts[0]), parts[1], detail
    except (OSError, ValueError):
        pass
    return None


# ─── State detection ───

def find_claude_pid(parent_pid, ps_lines):
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == str(parent_pid) and parts[3] == CLAUDE_PROCESS_NAME:
            return parts[0]
    return None


def has_children(pid, ps_lines, own_pgid):
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == str(pid):
            if parts[2] == str(own_pgid):
                continue
            if parts[3] in IGNORED_CHILDREN:
                continue
            return True
    return False


def has_grandchildren(pid, ps_lines, own_pgid):
    """True if any descendant two levels below `pid` exists.

    Claude Code's Bash tool runs commands as `claude → bash → cmd`,
    so a grandchild process is unambiguous evidence that a foreground
    tool is executing. MCP servers and language servers are direct
    children of claude only — they do not spawn nested workers in
    normal operation, so this check does not false-trigger on them.

    This is the hook-independent fallback for the case where Claude
    Code's UI shows the empty `❯ ` prompt above a still-running tool
    (the v2.1+ "ctrl+b ctrl+b to background" affordance), which would
    otherwise let the input-prompt heuristic resolve to IDLE.
    """
    children = set()
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == str(pid):
            if parts[2] == str(own_pgid):
                continue
            if parts[3] in IGNORED_CHILDREN:
                continue
            children.add(parts[0])
    if not children:
        return False
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] in children:
            if parts[3] in IGNORED_CHILDREN:
                continue
            return True
    return False


def capture_pane_bottom(pane_target, lines=8):
    """Capture bottom non-empty lines of a pane.
    Handles alternate screen mode (CLAUDE_CODE_NO_FLICKER=1) by trying
    normal capture first, then falling back to alternate screen capture.
    """
    raw = tmux_cmd("capture-pane", "-t", pane_target, "-p", "-S", "-10")
    if not raw or not raw.strip():
        # Try alternate screen (used when CLAUDE_CODE_NO_FLICKER=1)
        raw = tmux_cmd("capture-pane", "-a", "-t", pane_target, "-p", "-S", "-10")
    if not raw:
        return []
    non_empty = [l for l in raw.split("\n") if l.strip()]
    return non_empty[-lines:]


def detect_pane_state(pane_pid, pane_target, ps_lines, own_pgid):
    claude_pid = find_claude_pid(pane_pid, ps_lines)
    if not claude_pid:
        return "SHELL"

    has_child = has_children(claude_pid, ps_lines, own_pgid)

    # Capture once and share between the permit-footer check and the
    # input-prompt check below. We check permit before has_children
    # because permission dialogs can appear with no Claude child
    # process (the tool subprocess has not been spawned yet — Claude
    # is waiting for user approval).
    bottom = capture_pane_bottom(pane_target)
    for line in bottom:
        if PATTERN_PERMIT_FOOTER.match(line):
            return "PERMIT"

    if has_child:
        # Foreground tool execution (claude → bash → cmd) leaves a
        # grandchild in the process tree. When that is the case, the
        # input-prompt IDLE heuristic does not apply — newer Claude
        # Code UIs render the empty `❯ ` line above a still-running
        # tool to advertise ctrl+b ctrl+b backgrounding.
        if has_grandchildren(claude_pid, ps_lines, own_pgid):
            return "BUSY"
        # Otherwise: only direct children → MCP / language servers.
        # Visible input prompt means user is being asked for input.
        for line in bottom:
            if PATTERN_INPUT_PROMPT.match(line) and not PATTERN_ACCEPT_EDITS.match(line):
                return "IDLE"
        return "BUSY"

    return "IDLE"


def detect_window_raw(win_target, panes_cache, ps_lines, own_pgid):
    panes = [
        (pid, pane_id)
        for wt, pid, pane_id in panes_cache
        if wt == win_target
    ]
    if not panes:
        return "DOWN"

    best = "SHELL"
    for pid, pane_id in panes:
        state = detect_pane_state(pid, pane_id, ps_lines, own_pgid)
        if state == "PERMIT":
            return "PERMIT"
        elif state == "BUSY":
            best = "BUSY"
        elif state == "IDLE" and best != "BUSY":
            best = "IDLE"
    return best


def _set_win_state(win_target, state, done=None, last_done=None, unset_done=False):
    """Batch-write window state options in a single tmux call."""
    cmds = [("set-option", "-wt", win_target, "@ccm_prev_state", state)]
    if unset_done:
        cmds.append(("set-option", "-wt", win_target, "-u", "@ccm_done"))
    elif done is not None:
        cmds.append(("set-option", "-wt", win_target, "@ccm_done", str(done)))
    if last_done is not None:
        cmds.append(("set-option", "-wt", win_target, "@ccm_last_done", str(last_done)))
    tmux_batch(*cmds)


# ─── Declarative state detection ───
# detect_window_state is decomposed into:
#   1. build_detection_context — gather raw, hook, busy, done inputs
#   2. evaluate_rules          — pure priority-ordered rule match
#   3. apply_actions           — execute tmux / busy-file side effects
#
# To add or change a detection rule, edit DETECTION_RULES below.
# The rule table is the single source of truth for state transitions.


@dataclass(frozen=True)
class DetectionContext:
    """Immutable snapshot of all inputs to state detection.

    All fields are derived before any rule is evaluated, so rule matching
    is a pure function of this context.
    """
    raw: str              # detect_window_raw result: DOWN/SHELL/BUSY/IDLE
    hook_state: str       # hook signal state: BUSY/PERMIT/DONE/SHELL/""
    hook_ts: int          # hook signal timestamp (0 if no signal)
    hook_age: int         # now - hook_ts (-1 if no signal)
    prev_state: str       # previous detected state
    done_flag: str        # @ccm_done raw value ("" if unset)
    done_age: int         # now - int(done_flag) (-1 if missing/invalid)
    last_done_ts: int     # @ccm_last_done value
    last_busy_age: int    # now - .busy file mtime (-1 if missing)
    now: int              # current unix timestamp


class Action(Enum):
    """Side effect to execute when a rule matches.

    DEFAULT         — set @ccm_prev_state to resolved state
    CLEAR_DONE      — set state + unset @ccm_done
    SET_DONE_NOW    — set state=DONE, @ccm_done=now, @ccm_last_done=now
    SET_DONE_HOOK   — set state=DONE, @ccm_done=hook_ts, @ccm_last_done=hook_ts
    WRITE_BUSY_FILE — write .busy file with now, then set state
    HOLD_NO_WRITE   — do not touch tmux state (preserve prior state)
    """
    DEFAULT = "default"
    CLEAR_DONE = "clear_done"
    SET_DONE_NOW = "set_done_now"
    SET_DONE_HOOK = "set_done_hook"
    WRITE_BUSY_FILE = "write_busy_file"
    HOLD_NO_WRITE = "hold_no_write"


# Sentinel: result=USE_RAW means "use ctx.raw as the resolved state".
# A unique object ensures it cannot collide with a real state name.
class _UseRawSentinel:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<USE_RAW>"


USE_RAW = _UseRawSentinel()


@dataclass(frozen=True)
class Rule:
    """Declarative detection rule.

    Condition fields: None = wildcard (not checked).
    - raw_in         — ctx.raw must be in this tuple
    - hook_in        — ctx.hook_state must be in this tuple (implies signal present)
    - prev_in        — ctx.prev_state must be in this tuple
    - hook_age_lt    — ctx.hook_age must satisfy 0 <= age < value
    - busy_age_lt    — ctx.last_busy_age must satisfy 0 <= age < value
    - done_valid     — True: done_flag present AND 0 <= done_age < DONE_TIMEOUT
                       False: done_flag present AND NOT valid (expired/invalid)
    """
    name: str
    # str for concrete state (e.g. "BUSY"), or the USE_RAW sentinel
    result: object = USE_RAW
    action: Action = Action.DEFAULT
    raw_in: Optional[Tuple[str, ...]] = None
    hook_in: Optional[Tuple[str, ...]] = None
    prev_in: Optional[Tuple[str, ...]] = None
    hook_age_lt: Optional[int] = None
    busy_age_lt: Optional[int] = None
    done_valid: Optional[bool] = None

    def matches(self, ctx: "DetectionContext") -> bool:
        if self.raw_in is not None and ctx.raw not in self.raw_in:
            return False
        if self.hook_in is not None:
            if ctx.hook_state == "" or ctx.hook_state not in self.hook_in:
                return False
        if self.prev_in is not None and ctx.prev_state not in self.prev_in:
            return False
        if self.hook_age_lt is not None:
            if ctx.hook_age < 0 or ctx.hook_age >= self.hook_age_lt:
                return False
        if self.busy_age_lt is not None:
            if ctx.last_busy_age < 0 or ctx.last_busy_age >= self.busy_age_lt:
                return False
        if self.done_valid is True:
            if not ctx.done_flag or ctx.done_age < 0 or ctx.done_age >= DONE_TIMEOUT:
                return False
        elif self.done_valid is False:
            # "Invalid" means: flag present but NOT within valid window
            if not ctx.done_flag:
                return False
            if 0 <= ctx.done_age < DONE_TIMEOUT:
                return False
        return True


# Priority-ordered rule table. First match wins.
#
# Priority rationale:
#   1-2  process tree authoritative for SHELL/DOWN
#   3    fresh BUSY hook beats stale pipeline (multi-project race)
#   4    PERMIT blocks BUSY when dialog actually visible
#   5-7  DONE hook variants: post-permit, multi-turn boundary, genuine
#   8    BUSY hook overrides idle pipeline (text generation)
#   9-12 fallback transitions when hooks are absent or irrelevant
#   13   clear stale @ccm_done when leaving IDLE
#   14   default: trust raw state
DETECTION_RULES: Tuple[Rule, ...] = (
    Rule(
        name="process_down",
        raw_in=("DOWN",),
        result="DOWN",
        action=Action.CLEAR_DONE,
    ),
    Rule(
        name="process_shell",
        raw_in=("SHELL",),
        result="SHELL",
        action=Action.CLEAR_DONE,
    ),
    Rule(
        # Fast path: very fresh BUSY hook is trusted over any raw state.
        # Resolves races where the pipeline lags behind rapid BUSY
        # transitions across multiple projects.
        name="hook_fresh_busy",
        hook_in=("BUSY",),
        hook_age_lt=HOOK_FRESH_THRESHOLD,
        result="BUSY",
    ),
    Rule(
        # PERMIT dialog visible (raw != IDLE means input prompt is not
        # showing, so the permission UI is still active).
        name="hook_permit_blocking",
        hook_in=("PERMIT",),
        hook_age_lt=PERMIT_MAX_TIMEOUT,
        raw_in=("BUSY", "PERMIT"),
        result="PERMIT",
    ),
    Rule(
        # Post-PERMIT tool execution: after user grants permission the
        # tool runs but PreToolUse does NOT re-fire. Stop fires DONE
        # almost immediately. Within settle time, show BUSY and write
        # the .busy file so subsequent cycles keep the BUSY lock.
        name="hook_post_permit_tool",
        hook_in=("DONE",),
        hook_age_lt=DONE_SETTLE_TIME,
        prev_in=("PERMIT",),
        raw_in=("IDLE",),
        result="BUSY",
        action=Action.WRITE_BUSY_FILE,
    ),
    Rule(
        # Multi-turn boundary: Stop fired DONE between tool turns.
        # .busy file was touched within settle time by the previous
        # PreToolUse, so this DONE is a false positive.
        name="hook_multiturn_boundary",
        hook_in=("DONE",),
        hook_age_lt=DONE_TIMEOUT,
        prev_in=("BUSY",),
        raw_in=("IDLE",),
        busy_age_lt=DONE_SETTLE_TIME,
        result="BUSY",
    ),
    Rule(
        # Genuine DONE: Stop fired, not a multi-turn/post-permit case.
        name="hook_done_genuine",
        hook_in=("DONE",),
        hook_age_lt=DONE_TIMEOUT,
        raw_in=("IDLE",),
        result="DONE",
        action=Action.SET_DONE_HOOK,
    ),
    Rule(
        # Slow path: trust any BUSY hook signal while raw=IDLE.
        # No age limit — long-running tool executions and text generation
        # phases can legitimately exceed 5+ minutes without any intervening
        # hook refresh. If Claude Code crashes, rule 1/2 (raw=DOWN/SHELL)
        # clears state authoritatively. If Stop hook fails to fire
        # (Claude Code bug), we prefer showing stale BUSY over false IDLE —
        # BUSY is closer to the truth and prompts user investigation.
        name="hook_busy_idle",
        hook_in=("BUSY",),
        raw_in=("IDLE",),
        result="BUSY",
    ),
    Rule(
        # Fallback (no hooks): BUSY → IDLE transition means DONE.
        name="fallback_busy_to_done",
        raw_in=("IDLE",),
        prev_in=("BUSY",),
        result="DONE",
        action=Action.SET_DONE_NOW,
    ),
    Rule(
        # Fallback: keep PERMIT until a hook signal (BUSY/DONE) arrives.
        # After user responds, there's a brief IDLE gap before the tool
        # starts; don't let the fallback turn that into DONE.
        # HOLD_NO_WRITE: do not touch tmux state — preserve prior PERMIT.
        #
        # Safety net: require the PERMIT hook signal to still be present
        # and within PERMIT_MAX_TIMEOUT. Without this, a crashed Claude
        # during a permission dialog would leave PERMIT stuck forever
        # (no hook signal would ever clear prev_state=PERMIT).
        name="fallback_permit_hold",
        raw_in=("IDLE",),
        prev_in=("PERMIT",),
        hook_in=("PERMIT",),
        hook_age_lt=PERMIT_MAX_TIMEOUT,
        result="PERMIT",
        action=Action.HOLD_NO_WRITE,
    ),
    Rule(
        # Fallback: done_flag still within DONE_TIMEOUT → keep showing DONE.
        name="fallback_done_active",
        raw_in=("IDLE",),
        done_valid=True,
        result="DONE",
    ),
    Rule(
        # Fallback: done_flag expired or invalid → clear and trust raw.
        name="fallback_done_expired",
        raw_in=("IDLE",),
        done_valid=False,
        result=USE_RAW,
        action=Action.CLEAR_DONE,
    ),
    Rule(
        # Leaving IDLE: clear any stale @ccm_done.
        name="raw_not_idle_clear",
        raw_in=("BUSY", "PERMIT"),
        result=USE_RAW,
        action=Action.CLEAR_DONE,
    ),
    Rule(
        # Default: trust raw state. Always matches (terminal rule).
        name="default",
        result=USE_RAW,
    ),
)


def evaluate_rules(ctx: DetectionContext,
                   rules: Tuple[Rule, ...] = DETECTION_RULES) -> Tuple[Rule, str]:
    """Pure: return (matched_rule, resolved_state) for the first matching rule.

    No I/O, no tmux calls — this function is the testable core of detection.
    """
    for rule in rules:
        if rule.matches(ctx):
            state = ctx.raw if rule.result is USE_RAW else rule.result
            return rule, state
    # The terminal "default" rule guarantees a match. If we get here the
    # rule table is broken.
    raise RuntimeError("DETECTION_RULES has no terminal default rule")


def _read_busy_ts(project_dir: str) -> int:
    """Read the .busy file timestamp. Returns 0 if missing/invalid."""
    if not project_dir:
        return 0
    busy_file = _hook_signal_path(project_dir) + ".busy"
    try:
        with open(busy_file) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


# prev_state → synthetic raw mapping for the fast path.
# The fast path (statusline) skips the ps/capture-pane pipeline, so it
# has no real `raw` value. It derives one from prev_state under the
# assumption that Claude is still in the same lifecycle phase as the
# last authoritative slow-path evaluation. DONE is not a raw value —
# it's a derived state, so we map it back to IDLE and let rule 11
# (fallback_done_active) re-derive DONE from done_flag.
_FAST_PREV_TO_RAW = {
    "DOWN": "DOWN",
    "SHELL": "SHELL",
    "BUSY": "BUSY",
    "PERMIT": "PERMIT",
    "IDLE": "IDLE",
    "DONE": "IDLE",
    "": "IDLE",
}


def build_fast_context(prev_state, done_flag, project_dir,
                       now=None) -> DetectionContext:
    """Build a DetectionContext for the read-only statusline path.

    Does not call ps/capture-pane/tmux queries for process tree info.
    Derives `raw` from prev_state, reads hook signal + done_flag only.
    Skips the .busy file read — multi-turn boundary suppression is a
    slow-path concern; the fast path may briefly flash DONE which the
    next slow-path cycle will correct.
    """
    if now is None:
        now = int(time.time())

    raw = _FAST_PREV_TO_RAW.get(prev_state, "IDLE")

    hook_state = ""
    hook_ts = 0
    hook_age = -1
    if project_dir:
        sig = read_hook_signal(project_dir)
        if sig is not None:
            hook_ts, hook_state, _detail = sig
            if hook_state == "SHELL":
                hook_state = ""
                hook_ts = 0
            else:
                hook_age = now - hook_ts

    done_age = -1
    if done_flag:
        try:
            done_age = now - int(done_flag)
        except ValueError:
            done_age = -1

    return DetectionContext(
        raw=raw,
        hook_state=hook_state,
        hook_ts=hook_ts,
        hook_age=hook_age,
        prev_state=prev_state,
        done_flag=done_flag,
        done_age=done_age,
        last_done_ts=0,
        last_busy_age=-1,
        now=now,
    )


def evaluate_fast(prev_state, done_flag, project_dir, now=None) -> str:
    """Read-only state evaluation for statusline-speed contexts.

    Runs the same DETECTION_RULES table as the slow path, so there is
    one source of truth for state transitions. Does not write to tmux
    or touch .busy files — the slow-path run next cycle is authoritative
    for persisting state.
    """
    ctx = build_fast_context(prev_state, done_flag, project_dir, now)
    _rule, state = evaluate_rules(ctx)
    return state


def build_detection_context(win_target, project_dir, prev_state, done_flag,
                            last_done_ts, panes_cache, ps_lines, own_pgid
                            ) -> DetectionContext:
    """Gather all inputs needed for rule evaluation.

    Read-only side effects only (tmux query, ps snapshot, file reads).
    The returned context is an immutable snapshot.
    """
    now = int(time.time())
    raw = detect_window_raw(win_target, panes_cache, ps_lines, own_pgid)

    hook_state = ""
    hook_ts = 0
    hook_age = -1
    if project_dir:
        sig = read_hook_signal(project_dir)
        if sig is not None:
            hook_ts, hook_state, _detail = sig
            # SHELL hook signal is ignored (see comment in old detect_window_state):
            # process tree is authoritative for SHELL; trusting a stale SHELL
            # signal while raw=IDLE causes false SHELL after Claude restarts.
            if hook_state == "SHELL":
                hook_state = ""
                hook_ts = 0
            else:
                hook_age = now - hook_ts

    # .busy file is only consulted by the hook_multiturn_boundary rule,
    # which requires hook=DONE and prev=BUSY. Skip the filesystem read
    # in all other cases to keep per-project overhead minimal.
    last_busy_age = -1
    if hook_state == "DONE" and prev_state == "BUSY":
        last_busy_ts = _read_busy_ts(project_dir)
        if last_busy_ts:
            last_busy_age = now - last_busy_ts

    done_age = -1
    if done_flag:
        try:
            done_age = now - int(done_flag)
        except ValueError:
            done_age = -1

    return DetectionContext(
        raw=raw,
        hook_state=hook_state,
        hook_ts=hook_ts,
        hook_age=hook_age,
        prev_state=prev_state,
        done_flag=done_flag,
        done_age=done_age,
        last_done_ts=last_done_ts,
        last_busy_age=last_busy_age,
        now=now,
    )


def apply_actions(win_target, project_dir, ctx: DetectionContext, rule: Rule,
                  state: str) -> Tuple[str, str, int]:
    """Execute the side effects declared by a matched rule.

    Returns (state, done_flag, last_done_ts) tuple in the same format as
    detect_window_state's contract.
    """
    action = rule.action

    if action == Action.HOLD_NO_WRITE:
        # Preserve prior tmux state and prior done fields.
        return state, ctx.done_flag, ctx.last_done_ts

    if action == Action.CLEAR_DONE:
        _set_win_state(win_target, state, unset_done=True)
        return state, "", ctx.last_done_ts

    if action == Action.SET_DONE_NOW:
        _set_win_state(win_target, "DONE", done=ctx.now, last_done=ctx.now)
        return "DONE", str(ctx.now), ctx.now

    if action == Action.SET_DONE_HOOK:
        _set_win_state(win_target, "DONE", done=ctx.hook_ts, last_done=ctx.hook_ts)
        return "DONE", str(ctx.hook_ts), ctx.hook_ts

    if action == Action.WRITE_BUSY_FILE:
        if project_dir:
            busy_file = _hook_signal_path(project_dir) + ".busy"
            try:
                with open(busy_file, "w") as f:
                    f.write(str(ctx.now))
            except OSError:
                pass
        _set_win_state(win_target, state)
        return state, ctx.done_flag, ctx.last_done_ts

    # Action.DEFAULT
    _set_win_state(win_target, state)
    return state, ctx.done_flag, ctx.last_done_ts


def detect_window_state(win_target, project_dir, prev_state, done_flag, last_done_ts,
                        panes_cache, ps_lines, own_pgid):
    """Full detection pipeline. Returns (state, new_done_flag, new_last_done).

    Thin orchestration layer:
      1. build_detection_context — gather inputs
      2. evaluate_rules          — pure rule-table match
      3. apply_actions           — execute tmux/file side effects

    All state transitions are declared in DETECTION_RULES above. To add
    or change a case, edit the rule table rather than this function.
    """
    ctx = build_detection_context(
        win_target, project_dir, prev_state, done_flag, last_done_ts,
        panes_cache, ps_lines, own_pgid,
    )
    rule, state = evaluate_rules(ctx)
    return apply_actions(win_target, project_dir, ctx, rule, state)


# ─── Project data ───

class Project:
    __slots__ = (
        "win_target", "win_idx", "name", "dir", "state",
        "branch", "ports", "last_done_ts", "sort_key",
    )

    def __init__(self, win_target, win_idx, name, directory, state,
                 branch="", ports="", last_done_ts=0):
        self.win_target = win_target
        self.win_idx = win_idx
        self.name = name
        self.dir = directory
        self.state = state
        self.branch = branch
        self.ports = ports
        self.last_done_ts = last_done_ts
        self.sort_key = (STATE_PRIORITY.get(state, 5), -(last_done_ts or 0))


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
                with open(path) as f:
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
        "#{@ccm_prev_state}\t#{@ccm_done}\t#{@ccm_last_done}\t#{window_activity}"
    )
    if not raw:
        return []

    ps_lines = ps_snapshot().strip().split("\n") if not fast else []
    panes_cache = []
    if not fast:
        panes_raw = tmux_cmd("list-panes", "-a", "-F",
                             "#{session_name}:#{window_index}\t#{pane_pid}\t#{pane_id}")
        for line in panes_raw.split("\n"):
            parts = line.split("\t")
            if len(parts) == 3:
                panes_cache.append((parts[0], parts[1], parts[2]))

    own_pgid = str(os.getpgrp())
    seen_dirs = set()
    projects = []

    for line in raw.split("\n"):
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        win_target, project, proj_dir = parts[0], parts[1], parts[2]
        prev_state, done_flag, last_done_str, win_activity_str = (
            parts[3], parts[4], parts[5], parts[6]
        )

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

        last_done_ts = 0
        if last_done_str and last_done_str != "0":
            try:
                last_done_ts = int(last_done_str)
            except ValueError:
                pass

        win_activity = 0
        if win_activity_str:
            try:
                win_activity = int(win_activity_str)
            except ValueError:
                pass

        if fast:
            # Unified with slow path via DETECTION_RULES. Read-only:
            # does not touch tmux state or .busy files.
            state = evaluate_fast(prev_state, done_flag, proj_dir)
        else:
            state, done_flag, last_done_ts_new = detect_window_state(
                win_target, proj_dir, prev_state, done_flag, last_done_ts,
                panes_cache, ps_lines, own_pgid
            )
            if last_done_ts_new:
                last_done_ts = last_done_ts_new if isinstance(last_done_ts_new, int) else last_done_ts

        sort_ts = max(last_done_ts, win_activity) if win_activity else last_done_ts

        branch = read_cache_file(CCM_GIT_CACHE_DIR, proj_dir) if proj_dir else ""
        ports = read_cache_file(CCM_PORT_CACHE_DIR, proj_dir) if proj_dir else ""

        projects.append(Project(
            win_target=win_target, win_idx=win_idx, name=project,
            directory=proj_dir, state=state, branch=branch, ports=ports,
            last_done_ts=sort_ts,
        ))

    projects.sort(key=lambda p: p.sort_key)
    return projects


# ─── Formatting helpers ───

def format_elapsed(ts):
    if not ts or ts == 0:
        return ""
    elapsed = int(time.time()) - ts
    if elapsed < 0:
        return ""
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m"
    if elapsed < 86400:
        return f"{elapsed // 3600}h"
    return f"{elapsed // 86400}d"


def format_dir(directory, prefix_len, cols):
    d = directory.replace(os.path.expanduser("~"), "~")
    avail = cols - prefix_len - 4
    if avail < 10:
        return ""
    if len(d) <= avail:
        return d
    base = os.path.basename(d)
    parent = os.path.basename(os.path.dirname(d))
    short = f"…/{parent}/{base}"
    if len(short) <= avail:
        return short
    if len(base) <= avail:
        return base
    return ""


def hooks_configured():
    settings_file = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings_file) as f:
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
            with open(conf) as f:
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

        with open(conf, "w") as f:
            f.writelines(lines)
    except OSError:
        pass


# ─── Desktop notifications ───

def notify(state, project, detail=""):
    """Send desktop notification for state changes.
    Controlled by @ccm-notify tmux option: off, permit, done, permit,done, all.
    detail: optional context (e.g., "Bash: rm -rf ..." for PERMIT).
    """
    setting = tmux_cmd("show-option", "-gqv", "@ccm-notify") or "permit,done"
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
        "DONE":   (f"ccm ✔ {project}",
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
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_prev_state}\t#{@ccm_last_done}\t#{window_activity}"
    )
    if not activity_raw:
        return

    for line in activity_raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 5:
            parts.append("")
        win_target, project, prev_state, last_done_str, win_activity_str = parts[:5]

        if not project or prev_state != "IDLE":
            continue
        if win_target == current_target:
            continue

        # Parse timestamps
        last_done = 0
        if last_done_str and last_done_str != "0":
            try:
                last_done = int(last_done_str)
            except ValueError:
                pass

        win_activity = 0
        if win_activity_str and win_activity_str != "0":
            try:
                win_activity = int(win_activity_str)
            except ValueError:
                pass

        idle_since = max(last_done, win_activity)

        if idle_since == 0:
            tmux_cmd("set-option", "-wt", win_target, "@ccm_last_done", str(now))
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
            _set_win_state(win_target, "SHELL", unset_done=True)
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
            with open(marker) as f:
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
        with open(marker, "w") as f:
            f.write(str(now))
    except Exception:
        pass


# ─── CLI output helpers ───

# ANSI color codes for terminal output
_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_STATE = {
    "PERMIT": "\033[1;33m",      # bold yellow
    "BUSY": "\033[38;5;209m",    # salmon
    "DONE": "\033[0;32m",        # green
    "IDLE": "\033[0;34m",        # blue
    "SHELL": "\033[38;5;245m",   # gray
    "DOWN": "\033[2m",           # dim
}


def print_status():
    """Print status of all ccm projects (for `ccm status` CLI command)."""
    projects = build_project_list(fast=False)

    if not projects:
        print("No active projects.")
        return

    # Hooks status
    if hooks_configured():
        print(f"{_C_DIM}Hooks: ON{_C_RESET}")
    else:
        print(f"{_C_DIM}Hooks: OFF (run 'ccm setup-hooks' for improved detection){_C_RESET}")
    print()

    # Header
    print(f"{_C_BOLD}{'STATUS':<12} {'PROJECT':<20} {'BRANCH':<16} {'PORTS':<12} {'DIRECTORY'}{_C_RESET}")
    print(f"{'------':<12} {'-------':<20} {'------':<16} {'-----':<12} {'---------'}")

    for p in projects:
        color = _C_STATE.get(p.state, _C_DIM)
        icon = STATE_ICONS.get(p.state, "?")
        status = f"{color}{icon} {p.state}{_C_RESET}"
        branch = p.branch or "-"
        ports = p.ports or "-"
        d = p.dir.replace(os.path.expanduser("~"), "~") if p.dir else ""
        # Status field with ANSI codes is wider than visible, compensate
        print(f"{status:<22} {p.name:<20} {branch:<16} {ports:<12} {d}")


def print_ports():
    """Print listening ports per project (for `ccm ports` CLI command)."""
    projects = build_project_list(fast=True)
    if not projects:
        print("No active projects.")
        return

    print(f"{_C_BOLD}{'PROJECT':<20} {'PORTS':<16} {'DIRECTORY'}{_C_RESET}")
    print(f"{'-------':<20} {'-----':<16} {'---------'}")

    for p in projects:
        ports = p.ports or "-"
        d = p.dir.replace(os.path.expanduser("~"), "~") if p.dir else ""
        print(f"{p.name:<20} {ports:<16} {d}")


def print_tree():
    """Print hierarchical tree of all sessions/windows/panes (for `ccm tree`)."""
    sessions_raw = tmux_cmd("list-sessions", "-F", "#{session_name}")
    if not sessions_raw:
        print("No tmux sessions.")
        return

    sessions = sorted(sessions_raw.split("\n"))
    current_session = get_session()

    # Build project state lookup
    projects = build_project_list(fast=True)
    project_map = {p.win_target: p for p in projects}

    for si, sess in enumerate(sessions):
        is_last_s = si == len(sessions) - 1
        s_pre = "└── " if is_last_s else "├── "
        s_cont = "    " if is_last_s else "│   "
        marker = " ◀" if sess == current_session else ""
        print(f"{s_pre}{_C_BOLD}{sess}{_C_RESET}{marker}")

        windows_raw = tmux_cmd("list-windows", "-t", sess, "-F",
                               "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}")
        if not windows_raw:
            continue
        windows = windows_raw.split("\n")

        for wi, wline in enumerate(windows):
            parts = wline.split("\t")
            while len(parts) < 4:
                parts.append("")
            win_idx, win_name, project, wdir = parts[:4]
            win_target = f"{sess}:{win_idx}"
            is_last_w = wi == len(windows) - 1
            w_pre = f"{s_cont}└── " if is_last_w else f"{s_cont}├── "

            proj = project_map.get(win_target)
            if proj:
                color = _C_STATE.get(proj.state, _C_DIM)
                icon = STATE_ICONS.get(proj.state, "?")
                name = proj.name
                extra = ""
                if proj.branch:
                    extra += f" ({proj.branch})"
                if proj.ports:
                    extra += f" [:{proj.ports}]"
            else:
                color = _C_DIM
                icon = ""
                name = win_name
                extra = ""

            d = ""
            if wdir:
                d = f" {wdir.replace(os.path.expanduser('~'), '~')}"
            elif not project:
                pane_path = tmux_cmd("display-message", "-t", win_target, "-p", "#{pane_current_path}")
                if pane_path:
                    d = f" {pane_path.replace(os.path.expanduser('~'), '~')}"

            icon_str = f"{color}{icon}{_C_RESET} " if icon else ""
            print(f"{w_pre}{icon_str}{name}{extra}{_C_DIM}{d}{_C_RESET}")


def print_statusline():
    """Print one-line status for tmux status bar (for `ccm statusline`)."""
    projects = build_project_list(fast=True)
    active = [p for p in projects if p.state in ("BUSY", "PERMIT", "DONE")]
    if not active:
        return

    parts = []
    for p in active:
        icon = STATE_ICONS.get(p.state, "?")
        parts.append(f"{p.name}:{icon}")

    print(f"| {' '.join(parts)} |")


# ─── CLI helpers ───

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


def clear_done(win_target):
    """Clear DONE hook signal for a window."""
    proj_dir = tmux_cmd("show-option", "-wqv", "-t", win_target, "@ccm_dir")
    if not proj_dir:
        return
    resolved = _resolve_project_dir(proj_dir)
    hook_file = os.path.join(CCM_HOOK_DIR, md5_hash(resolved))
    try:
        if os.path.exists(hook_file):
            with open(hook_file) as f:
                if "DONE" in f.read():
                    os.unlink(hook_file)
    except OSError:
        pass
    tmux_cmd("set-option", "-wq", "-t", win_target, "@ccm_prev_state", "")


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


# ─── Snapshot commands ───


def _sanitize_snapshot_name(name):
    """Sanitize snapshot name to prevent path traversal."""
    # Strip path components — only keep the basename
    name = os.path.basename(name)
    # Remove any remaining dots that could cause issues (e.g., ".." left over)
    name = name.strip(".")
    if not name:
        ccm_die("Invalid snapshot name")
    return name


def cmd_snapshot_save(name="", quiet=False):
    """Save current projects as a snapshot."""
    if not name:
        try:
            name = input("Snapshot name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not name:
        ccm_die("Snapshot name is required")
    name = _sanitize_snapshot_name(name)

    init_dirs()

    # Scan ALL sessions for ccm-tagged windows
    raw = tmux_cmd("list-windows", "-a", "-F",
                   "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}")
    if not raw:
        if not quiet:
            ccm_die("No active projects to save")
        return

    projects_list = []
    for line in raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        project, proj_dir = parts[2], parts[3]
        if not project or not proj_dir:
            continue
        # Replace $HOME with ~ for portability
        proj_dir = proj_dir.replace(os.path.expanduser("~"), "~")
        projects_list.append({
            "name": project,
            "dir": proj_dir,
            "auto_start_claude": True,
        })

    if not projects_list:
        if not quiet:
            ccm_die("No active projects to save")
        return

    snapshot = {
        "version": 1,
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "projects": projects_list,
    }

    file_path = os.path.join(CCM_SNAPSHOT_DIR, f"{name}.json")
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, file_path)

    if not quiet:
        ccm_info(f"Snapshot saved: {name} ({file_path})")


def cmd_snapshot_load(name=""):
    """Load and restore a snapshot."""
    init_dirs()
    if not name:
        files = sorted(_glob_mod.glob(os.path.join(CCM_SNAPSHOT_DIR, "*.json")))
        if not files:
            ccm_die("No snapshots found")
        items = [os.path.splitext(os.path.basename(f))[0] for f in files]
        name = fzf_select(items, "Select snapshot: ")
        if not name:
            return

    name = _sanitize_snapshot_name(name)
    file_path = os.path.join(CCM_SNAPSHOT_DIR, f"{name}.json")
    if not os.path.exists(file_path):
        ccm_die(f"Snapshot not found: {name}")

    with open(file_path) as f:
        data = json.load(f)

    snap_projects = data.get("projects", [])
    print(f"Loading snapshot: {name} ({len(snap_projects)} projects)")

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    for proj in snap_projects:
        proj_name = proj.get("name", "")
        proj_dir = proj.get("dir", "")
        if not proj_name or proj_name == "null":
            continue
        if not proj_dir or proj_dir == "null":
            continue

        proj_dir = os.path.expanduser(proj_dir)
        try:
            proj_dir = os.path.realpath(proj_dir)
        except OSError:
            pass

        if project_exists(session, proj_name):
            ccm_warn(f"Project window already exists, skipping: {proj_name}")
            continue
        if not os.path.isdir(proj_dir):
            ccm_warn(f"Directory not found, skipping: {proj_name} ({proj_dir})")
            continue

        # Don't auto-start Claude on restore — saves resources
        cmd_add(proj_dir, proj_name, start_claude=False, _loading=True)

    # Save autosave after all projects loaded
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        ccm_warn("Failed to save autosave snapshot after load")

    ccm_info(f"Snapshot loaded: {name}")


def cmd_snapshot_list():
    """List available snapshots."""
    init_dirs()
    files = sorted(_glob_mod.glob(os.path.join(CCM_SNAPSHOT_DIR, "*.json")))
    if not files:
        print("No snapshots.")
        return

    print(f"{_C_BOLD}{'NAME':<20} {'CREATED':<24} {'PROJECTS'}{_C_RESET}")
    print(f"{'----':<20} {'-------':<24} {'--------'}")

    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            name = data.get("name", os.path.splitext(os.path.basename(fp))[0])
            created = data.get("created", "-")
            count = len(data.get("projects", []))
            print(f"{name:<20} {created:<24} {count}")
        except (json.JSONDecodeError, OSError):
            pass


def cmd_snapshot_delete(name=""):
    """Delete a snapshot."""
    init_dirs()
    if not name:
        files = sorted(_glob_mod.glob(os.path.join(CCM_SNAPSHOT_DIR, "*.json")))
        if not files:
            ccm_die("No snapshots found")
        items = [os.path.splitext(os.path.basename(f))[0] for f in files]
        name = fzf_select(items, "Delete snapshot: ")
        if not name:
            return

    name = _sanitize_snapshot_name(name)
    file_path = os.path.join(CCM_SNAPSHOT_DIR, f"{name}.json")
    if not os.path.exists(file_path):
        ccm_die(f"Snapshot not found: {name}")

    os.unlink(file_path)
    ccm_info(f"Snapshot deleted: {name}")


# ─── Session commands ───


def _autosave_trigger():
    """Trigger autosave in background (non-blocking)."""
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        pass


def cmd_add(directory, name="", start_claude=True, _loading=False):
    """Add a new ccm project window."""
    if not directory:
        ccm_die("Directory is required")

    directory = os.path.expanduser(directory)
    try:
        directory = os.path.realpath(directory)
    except OSError:
        pass

    if not os.path.isdir(directory):
        ccm_die(f"Directory does not exist: {directory}")

    if not name:
        name = os.path.basename(directory)
    name = validate_name(name)
    if not name:
        ccm_die("Invalid project name")

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    if project_exists(session, name):
        ccm_die(f"Project window already exists: {name}")

    # Check for duplicate directory
    for _idx, _wn, _proj, existing_dir in list_windows_raw(session):
        try:
            real_existing = os.path.realpath(os.path.expanduser(existing_dir))
        except OSError:
            real_existing = existing_dir
        if directory == real_existing:
            ccm_die(f"Directory already registered as project '{_proj}': {existing_dir}")

    # Create new window
    win_idx = tmux_cmd("new-window", "-P", "-F", "#{window_index}",
                       "-t", f"{session}:", "-n", name, "-c", directory)
    if not win_idx:
        ccm_die("Failed to create window")

    win_target = f"{session}:{win_idx}"

    # Tag the window with ccm metadata
    orig_name = tmux_cmd("display-message", "-t", win_target, "-p", "#{window_name}") or name
    tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_orig_name", orig_name),
        ("set-option", "-wt", win_target, "@ccm_project", name),
        ("set-option", "-wt", win_target, "@ccm_dir", directory),
        ("set-option", "-wt", win_target, "automatic-rename", "off"),
    )

    if start_claude:
        tmux_cmd("send-keys", "-t", win_target, CLAUDE_CMD, "Enter")

    ccm_info(f"Added project: {name} ({directory})")

    if not hooks_configured():
        ccm_warn("Hooks not installed. Run 'ccm setup-hooks' for accurate state detection.")

    if not _loading:
        _autosave_trigger()


def cmd_open(directory, name=""):
    """Start Claude in the current pane (for split-pane use)."""
    if not directory:
        ccm_die("Directory is required")

    directory = os.path.expanduser(directory)
    try:
        directory = os.path.realpath(directory)
    except OSError:
        pass

    if not os.path.isdir(directory):
        ccm_die(f"Directory does not exist: {directory}")

    if not name:
        name = os.path.basename(directory)

    # shlex.quote for safety
    tmux_cmd("send-keys",
             f"cd {shlex.quote(directory)} && (claude --continue 2>/dev/null || claude)",
             "Enter")


def cmd_register(source_target, new_name=""):
    """Register an existing tmux window as a ccm project."""
    if not source_target:
        ccm_die("Usage: ccm register <window_name|window_index> [name]")

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    # Find window by index or name
    if source_target.isdigit():
        win_idx = source_target
        win_name = tmux_cmd("display-message", "-t", f"{session}:{win_idx}",
                            "-p", "#{window_name}")
        if not win_name:
            ccm_die(f"Window not found at index: {source_target}")
    else:
        raw = tmux_cmd("list-windows", "-t", session, "-F",
                       "#{window_index}\t#{window_name}")
        win_idx = None
        win_name = source_target
        if raw:
            for line in raw.split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1] == source_target:
                    win_idx = parts[0]
                    break
        if win_idx is None:
            ccm_die(f"Window not found: {source_target}")

    win_target = f"{session}:{win_idx}"

    # Check if already tagged
    existing = tmux_cmd("show-option", "-wt", win_target, "-qv", "@ccm_project")
    if existing:
        ccm_die(f"Already a ccm project: {existing}")

    name = new_name or win_name
    name = validate_name(name)
    if not name:
        ccm_die("Invalid project name")

    if project_exists(session, name):
        ccm_die(f"Project name already in use: {name}")

    # Get directory from pane
    pane_dir = tmux_cmd("display-message", "-t", win_target, "-p", "#{pane_current_path}")

    tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_orig_name", win_name),
        ("set-option", "-wt", win_target, "@ccm_project", name),
        ("set-option", "-wt", win_target, "@ccm_dir", pane_dir or ""),
        ("set-option", "-wt", win_target, "automatic-rename", "off"),
        ("rename-window", "-t", win_target, name),
    )

    ccm_info(f"Registered: {win_name} → {name}")
    _autosave_trigger()


def cmd_unregister(name):
    """Unregister window from ccm (keep window alive)."""
    if not name:
        ccm_die("Project name is required")

    session = get_session()
    idx = find_window(session, name)
    if idx is None:
        ccm_die(f"Project window not found: {name}")

    win_target = f"{session}:{idx}"

    # Restore original name
    orig_name = tmux_cmd("show-option", "-wt", win_target, "-qv", "@ccm_orig_name")
    if orig_name:
        tmux_cmd("rename-window", "-t", win_target, orig_name)

    # Remove all ccm tags
    tags = ["automatic-rename", "@ccm_project", "@ccm_dir", "@ccm_orig_name",
            "@ccm_prev_state", "@ccm_done", "@ccm_last_done",
            "@ccm_state_icon", "@ccm_state_color"]
    cmds = [("set-option", "-wt", win_target, "-u", tag) for tag in tags]
    tmux_batch(*cmds)

    ccm_info(f"Unregistered: {name} (window kept)")
    _autosave_trigger()


def cmd_rename(old_name, new_name):
    """Rename a ccm project."""
    if not old_name:
        ccm_die("Usage: ccm rename <current_name> <new_name>")
    if not new_name:
        ccm_die("New name is required")

    new_name = validate_name(new_name)
    if not new_name:
        ccm_die("Invalid project name")

    session = get_session()
    idx = find_window(session, old_name)
    if idx is None:
        ccm_die(f"Project not found: {old_name}")

    if project_exists(session, new_name):
        ccm_die(f"Project name already in use: {new_name}")

    win_target = f"{session}:{idx}"
    tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_project", new_name),
        ("rename-window", "-t", win_target, new_name),
    )

    ccm_info(f"Renamed: {old_name} → {new_name}")
    _autosave_trigger()


def cmd_remove(name):
    """Remove a ccm project window (kill window)."""
    if not name:
        ccm_die("Project name is required")

    session = get_session()
    idx = find_window(session, name)
    if idx is None:
        ccm_die(f"Project window not found: {name}")

    tmux_cmd("kill-window", "-t", f"{session}:{idx}")
    ccm_info(f"Removed project: {name}")
    _autosave_trigger()


def cmd_list():
    """List all ccm-managed project windows."""
    session = get_session()
    if not session:
        print("No active projects.")
        return

    windows = list_windows_raw(session)
    if not windows:
        print("No active projects.")
        return

    print(f"{_C_BOLD}{'PROJECT':<20} {'DIRECTORY'}{_C_RESET}")
    print(f"{'-------':<20} {'---------'}")

    for _idx, _wn, project, proj_dir in windows:
        print(f"{project:<20} {proj_dir}")


def cmd_attach(target):
    """Switch to a ccm project window."""
    if not target:
        ccm_die("Project name or number is required")

    session = get_session()
    if not session:
        ccm_die("Not inside a tmux session")

    idx = None
    if target.isdigit():
        # By window index
        windows = list_windows_raw(session)
        for w_idx, _, _, _ in windows:
            if w_idx == target:
                idx = w_idx
                break
        if idx is None:
            ccm_die(f"No ccm project at window index: {target}")
    else:
        idx = find_window(session, target)
        if idx is None:
            # Try by window name
            raw = tmux_cmd("list-windows", "-t", session, "-F",
                           "#{window_index}\t#{window_name}")
            if raw:
                for line in raw.split("\n"):
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[1] == target:
                        idx = parts[0]
                        break
            if idx is None:
                ccm_die(f"Project not found: {target}")

    # Check if already on this window
    current_idx = tmux_cmd("display-message", "-t", session, "-p", "#{window_index}")
    if current_idx == idx:
        ccm_info("Already in this window")
        return

    win_target = f"{session}:{idx}"

    # Auto-start Claude if SHELL state
    pane_pid = tmux_cmd("list-panes", "-t", win_target, "-F", "#{pane_pid}")
    if pane_pid:
        pane_pid = pane_pid.split("\n")[0]
        # Check if claude is running as child
        try:
            ps_out = subprocess.run(["ps", "-eo", "ppid,comm"],
                                    capture_output=True, text=True, timeout=5)
            has_claude = False
            for line in ps_out.stdout.strip().split("\n"):
                fields = line.split()
                if len(fields) >= 2 and fields[0] == pane_pid and fields[1] == "claude":
                    has_claude = True
                    break
        except (subprocess.TimeoutExpired, OSError):
            has_claude = True  # Assume running on error

        if not has_claude:
            auto_start_claude(win_target)

    clear_done(win_target)
    tmux_cmd("select-window", "-t", f"{session}:{idx}")


def cmd_capture(args):
    """Capture visible content of a project window."""
    copy_mode = False
    target = ""
    for arg in args:
        if arg in ("--copy", "-c"):
            copy_mode = True
        else:
            target = arg

    if not target:
        ccm_die("Usage: ccm capture [--copy] <name|#id>")

    session = get_session()

    # Resolve target to window index
    if target.startswith("#"):
        num = target[1:]
    elif target.isdigit():
        num = target
    else:
        num = None

    if num is not None:
        windows = list_windows_raw(session)
        idx = None
        proj_name = None
        for w_idx, _, proj, _ in windows:
            if w_idx == num:
                idx = w_idx
                proj_name = proj
                break
        if idx is None:
            ccm_die(f"No ccm project at window index: {num}")
    else:
        proj_name = target
        idx = find_window(session, target)
        if idx is None:
            ccm_die(f"Project not found: {target}")

    output = tmux_cmd("capture-pane", "-t", f"{session}:{idx}", "-p", "-S", "-50")

    if copy_mode:
        if clipboard_copy(output):
            ccm_info(f"Captured {proj_name} → clipboard")
        else:
            ccm_warn("No clipboard tool available (install pbcopy, xclip, or xsel)")
    else:
        print(f"=== ccm capture: {proj_name} ===")
        print(output)
        print("=== end ===")


def cmd_stop(target):
    """Stop project window(s)."""
    if target == "--all":
        session = get_session()
        windows = list_windows_raw(session)
        if not windows:
            print("No active projects.")
            return

        # Auto-save before stopping
        init_dirs()
        try:
            cmd_snapshot_save("_autosave", quiet=True)
            ccm_info("Auto-saved snapshot: _autosave")
        except Exception:
            pass

        for w_idx, _, project, _ in windows:
            tmux_cmd("kill-window", "-t", f"{session}:{w_idx}")
            ccm_info(f"Stopped: {project}")
    elif target:
        cmd_remove(target)
    else:
        ccm_die("Usage: ccm stop [--all|<name>]")



def cmd_clear_done():
    """Clear DONE flag for the current window."""
    session_name = tmux_cmd("display-message", "-p", "#{session_name}")
    win_idx = tmux_cmd("display-message", "-p", "#{window_index}")
    if session_name and win_idx:
        clear_done(f"{session_name}:{win_idx}")


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
    elif cmd == "snapshot-save":
        cmd_snapshot_save(args[0] if args else "")
    elif cmd == "snapshot-load":
        cmd_snapshot_load(args[0] if args else "")
    elif cmd == "snapshot-list":
        cmd_snapshot_list()
    elif cmd == "snapshot-delete":
        cmd_snapshot_delete(args[0] if args else "")
    elif cmd == "clear-done":
        cmd_clear_done()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
