"""State detection engine — pure rules + thin tmux/fs orchestration.

This module is the extracted core of ccm's state machine. It owns:
  - The process-tree / pane-text "raw" state sniffer
    (`detect_pane_state`, `detect_window_raw`)
  - The declarative rule table (`DETECTION_RULES`) and its context /
    rule / action types
  - The slow- and fast-path context builders + evaluators
  - `apply_actions`, the detection-pipeline writer of
    `@ccm_prev_state` and `@ccm_completed_at`

Everything else (constants, tmux helpers, hook signal I/O, JSONL
parsing, SHELL cluster tracking) lives in `ccm_core`; this module
imports those dependencies at module load. `ccm_core` then
re-exports the detection API below at the bottom of its file so
existing callers (`build_project_list`, dashboard, inject_status,
pytest) continue to do `from ccm_core import ...`.

## `@ccm_prev_state` write sites (2, intentionally distributed)

The window option `@ccm_prev_state` has two writers in the
codebase. They look similar but serve different roles and are not
merged on purpose — see `project_r4_r5_decision` memo.

1. `apply_actions` → `_set_win_state` (this file)
   Detection-pipeline write. Runs every slow-path scan
   (inject_status poll, dashboard refresh) and records the
   resolved state so the next scan can key transition-based
   rules off `ctx.prev_state`.

2. `ccm_write_signal` (`hooks/lib.sh`)
   Hot-path write from Claude Code hook scripts. Bypasses the
   Python detector so the statusline reflects BUSY / PERMIT /
   SHELL with zero polling latency. Routing through Python
   would add 30–80 ms of interpreter startup per hook event,
   which defeats the purpose of instant status updates.

Historical note: `reset_window_after_attach` used to be a third
writer (wiping prev_state to "") but that made the startup-after-
attach signature indistinguishable from an in-flight response.
The wipe was removed so the new `startup_transient_raw_busy` rule
can key on `prev_state ∈ ("", "SHELL")` to identify MCP-loading
transients authoritatively. See the docstring of
`reset_window_after_attach` in `ccm_core.py` for the full
rationale.

When changing state-transition semantics, audit both sites.
"""

import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

# `ccm_core` is imported for its constants (immutable after startup) AND
# for its callable helpers (tmux_cmd, read_hook_signal, read_jsonl_age,
# _push_shell_transition). Constants we pull in as from-imports for
# readability; helpers stay accessible via the module object so that
# `unittest.mock.patch("ccm_core.tmux_cmd")` continues to affect this
# module's calls too. Direct from-imports would bind the helper name at
# import time, bypassing the mock.
import ccm_core  # noqa: F401 (used for late-bound attribute access)
from ccm_core import (
    # UI / process patterns
    CLAUDE_PROCESS_NAME,
    IGNORED_CHILDREN,
    PATTERN_ACCEPT_EDITS,
    PATTERN_INPUT_PROMPT,
    PATTERN_PERMIT_FOOTER,
    # Detection thresholds (int at module load)
    BUSY_HOOK_JSONL_WINDOW,
    HOOK_FRESH_THRESHOLD,
    JSONL_ACTIVE_THRESHOLD,
    JSONL_FRESH_THRESHOLD,
    JSONL_HOOK_GAP_TOLERANCE,
    PERMIT_MAX_TIMEOUT,
    STARTUP_GRACE_SEC,
)


# ─── Process tree helpers ───

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
    raw = ccm_core.tmux_cmd("capture-pane", "-t", pane_target, "-p", "-S", "-10")
    if not raw or not raw.strip():
        # Try alternate screen (used when CLAUDE_CODE_NO_FLICKER=1)
        raw = ccm_core.tmux_cmd("capture-pane", "-a", "-t", pane_target, "-p", "-S", "-10")
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
        # Input prompt visible → Claude is waiting for user input.
        # Even if grandchildren exist (e.g. a dev server started by
        # a previous Bash tool that is still running), Claude itself
        # is idle. The v2.1+ case where `❯ ` appears above a STILL-
        # ACTIVE tool is handled at the window level by hook_busy_idle
        # and jsonl_fresh_activity rules, not here.
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


def _set_win_state(win_target, state):
    """Write @ccm_prev_state to the window."""
    ccm_core.tmux_cmd("set-option", "-wt", win_target, "@ccm_prev_state", state)


# ─── Declarative state detection ───
# detect_window_state is decomposed into:
#   1. build_detection_context — gather raw, hook, jsonl inputs
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
    hook_state: str       # hook signal state: BUSY/PERMIT/SHELL/""
    hook_ts: int          # hook signal timestamp (0 if no signal)
    hook_age: int         # now - hook_ts (-1 if no signal)
    prev_state: str       # previous detected state
    jsonl_age: int        # now - newest JSONL real-activity ts (-1 if missing)
    now: int              # current unix timestamp
    # Seconds since the `claude` process in this window started, or -1
    # if no claude pid was found (SHELL state) or the ps snapshot had
    # no etime column. Used by `startup_transient_raw_busy` to
    # distinguish MCP-loading startup from steady-state operation.
    claude_pid_age: int = -1
    # stop_reason of the most recent `assistant` record in the JSONL
    # tail. "tool_use" is the authoritative signal that Claude has
    # paused mid-response to await a tool result; "end_turn" /
    # "max_tokens" / "stop_sequence" mean the response truly ended.
    # None when no assistant record was in the tail window. Defaulted
    # so existing rule-unit tests that construct a Context directly
    # without this field continue to match the wildcard behavior they
    # expected (rules that do not specify `jsonl_last_stop_reason_in`
    # ignore this field entirely).
    jsonl_last_stop_reason: Optional[str] = None


class Action(Enum):
    """Side effect to execute when a rule matches.

    DEFAULT         — set @ccm_prev_state to resolved state
    HOLD_NO_WRITE   — do not touch tmux state (preserve prior state)
    """
    DEFAULT = "default"
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
    """
    name: str
    # str for concrete state (e.g. "BUSY"), or the USE_RAW sentinel
    result: object = USE_RAW
    action: Action = Action.DEFAULT
    raw_in: Optional[Tuple[str, ...]] = None
    hook_in: Optional[Tuple[str, ...]] = None
    prev_in: Optional[Tuple[str, ...]] = None
    hook_age_lt: Optional[int] = None
    # Match only while the `claude` process started less than this
    # many seconds ago. Requires `0 <= ctx.claude_pid_age < value` —
    # an unknown age (-1) does NOT match, so rules using this axis
    # are inactive when the ps snapshot lacks an etime column.
    claude_pid_age_lt: Optional[int] = None
    jsonl_age_lt: Optional[int] = None
    # True:  ctx.hook_state must be "" (no hook signal present)
    # False: ctx.hook_state must be non-empty (some signal present)
    # None:  not checked
    # Distinct from hook_in, which requires a specific signal state.
    # hook_missing=True is the way to assert "no UserPromptSubmit /
    # PreToolUse / PermissionRequest has fired recently".
    hook_missing: Optional[bool] = None
    # True: ctx.jsonl_age must be < 0 (no JSONL file at all)
    # False: ctx.jsonl_age must be >= 0 (JSONL file exists)
    # None: not checked
    jsonl_missing: Optional[bool] = None
    # Authoritative BUSY-hold discriminator: the stop_reason of the most
    # recent assistant record in the JSONL tail must be in this tuple.
    # `"tool_use"` means Claude paused mid-response to await a tool
    # result, so raw=IDLE in that window is a between-tools gap rather
    # than a true completion. Other stop_reasons (`end_turn`,
    # `max_tokens`, `stop_sequence`) indicate the response truly ended.
    jsonl_last_stop_reason_in: Optional[Tuple[str, ...]] = None
    # Recap discriminator (Phase 2 of the v2.1.108 recap fix). When
    # set, the rule matches only if the BUSY hook signal was fired
    # within `value` seconds AFTER the last real conversation activity
    # (`ctx.jsonl_age - ctx.hook_age < value`). Both hook_age and
    # jsonl_age must be valid (>= 0); otherwise the rule does NOT
    # match.
    hook_after_real_activity_lt: Optional[int] = None

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
        if self.claude_pid_age_lt is not None:
            if ctx.claude_pid_age < 0 or ctx.claude_pid_age >= self.claude_pid_age_lt:
                return False
        if self.hook_missing is True:
            if ctx.hook_state != "":
                return False
        elif self.hook_missing is False:
            if ctx.hook_state == "":
                return False
        if self.jsonl_age_lt is not None:
            if ctx.jsonl_age < 0 or ctx.jsonl_age >= self.jsonl_age_lt:
                return False
        if self.jsonl_missing is True:
            if ctx.jsonl_age >= 0:
                return False
        elif self.jsonl_missing is False:
            if ctx.jsonl_age < 0:
                return False
        if self.jsonl_last_stop_reason_in is not None:
            if ctx.jsonl_last_stop_reason is None:
                return False
            if ctx.jsonl_last_stop_reason not in self.jsonl_last_stop_reason_in:
                return False
        if self.hook_after_real_activity_lt is not None:
            if ctx.hook_age < 0 or ctx.jsonl_age < 0:
                return False
            if (ctx.jsonl_age - ctx.hook_age) >= self.hook_after_real_activity_lt:
                return False
        return True


# Priority-ordered rule table. First match wins.
#
# Priority rationale (4-state model: PERMIT/BUSY/IDLE/SHELL):
#   1-2   process tree authoritative for SHELL/DOWN
#   3     fresh BUSY hook beats stale pipeline (multi-project race)
#   4     PERMIT blocks BUSY when dialog actually visible
#   5-6   BUSY hook overrides idle pipeline (text generation)
#   7     authoritative tool_use hold across tool-turn boundaries
#   8-9   JSONL freshness signals (5 s fresh, 15 s safety net)
#   10    BUSY → IDLE fallback (direct, no DONE intermediate)
#   11    PERMIT hold (brief IDLE gap after user approves)
#   12    startup transient: demote raw=BUSY during MCP loading
#   13    raw BUSY/PERMIT passthrough
#   14    default: trust raw state
DETECTION_RULES: Tuple[Rule, ...] = (
    Rule(name="process_down", raw_in=("DOWN",), result="DOWN"),
    Rule(name="process_shell", raw_in=("SHELL",), result="SHELL"),
    Rule(
        # Fast path: very fresh BUSY hook is trusted over any raw state.
        # The recap discriminator (hook_after_real_activity_lt) rejects
        # phantom hooks from v2.1.108+ recap events.
        name="hook_fresh_busy",
        hook_in=("BUSY",),
        hook_age_lt=HOOK_FRESH_THRESHOLD,
        hook_after_real_activity_lt=JSONL_HOOK_GAP_TOLERANCE,
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
        # Slow path: trust a BUSY hook signal while raw=IDLE, as long as
        # the session's JSONL has been written within BUSY_HOOK_JSONL_WINDOW
        # AND the BUSY hook fired within JSONL_HOOK_GAP_TOLERANCE of the
        # last real conversation activity. Stale BUSY hooks (no JSONL
        # corroboration) fall through to fallback_busy_to_idle.
        name="hook_busy_idle",
        hook_in=("BUSY",),
        raw_in=("IDLE",),
        jsonl_age_lt=BUSY_HOOK_JSONL_WINDOW,
        jsonl_missing=False,
        hook_after_real_activity_lt=JSONL_HOOK_GAP_TOLERANCE,
        result="BUSY",
    ),
    Rule(
        # Same BUSY-hook-trust path for the edge case where no JSONL
        # exists for this project at all. Trust the BUSY hook
        # unconditionally without JSONL corroboration.
        name="hook_busy_idle_no_jsonl",
        hook_in=("BUSY",),
        raw_in=("IDLE",),
        jsonl_missing=True,
        result="BUSY",
    ),
    Rule(
        # Authoritative BUSY hold across tool-turn boundaries. Claude
        # Code fires a Stop hook at every tool boundary (not just at
        # the true end of a response), which deletes the BUSY signal
        # file. For long tools (> JSONL_ACTIVE_THRESHOLD), both
        # jsonl_fresh_activity and jsonl_holds_busy expire before the
        # next PreToolUse refreshes the signal, leaving detection to
        # rely on the grandchild-process heuristic alone — which
        # flickers between tools and is worse under multi-project
        # concurrent load. The authoritative answer is in the JSONL
        # itself: the most recent assistant record carries a
        # stop_reason of "tool_use" whenever Claude is paused waiting
        # on a tool result mid-response, and switches to "end_turn"
        # (or "max_tokens" / "stop_sequence") only when the response
        # truly ended. Hold BUSY for as long as stop_reason=tool_use
        # is the latest signal, capped at BUSY_HOOK_JSONL_WINDOW so a
        # phantom abandoned session cannot hold BUSY forever.
        name="jsonl_tool_use_pending",
        raw_in=("IDLE",),
        prev_in=("BUSY",),
        jsonl_missing=False,
        jsonl_last_stop_reason_in=("tool_use",),
        jsonl_age_lt=BUSY_HOOK_JSONL_WINDOW,
        result="BUSY",
    ),
    Rule(
        # JSONL session log shows fresh activity (within 5s). Independent
        # of hooks — Claude Code writes records at conversation turn
        # boundaries. Also serves as the natural multi-turn bridge: when
        # Stop deletes the signal file, JSONL freshness keeps BUSY for
        # a few seconds until the next PreToolUse fires.
        name="jsonl_fresh_activity",
        raw_in=("IDLE",),
        jsonl_age_lt=JSONL_FRESH_THRESHOLD,
        result="BUSY",
    ),
    Rule(
        # Short safety net between the 5 s fresh window and the
        # authoritative tool_use hold above: holds BUSY for
        # JSONL_ACTIVE_THRESHOLD when the JSONL is still warm but
        # either (a) no assistant stop_reason has been written yet
        # (brand-new turn, user record only) or (b) the record is
        # older schema without stop_reason. Keeps the legacy
        # behavior as a defensive fallback for edge cases
        # jsonl_tool_use_pending does not cover.
        name="jsonl_holds_busy",
        raw_in=("IDLE",),
        prev_in=("BUSY",),
        jsonl_age_lt=JSONL_ACTIVE_THRESHOLD,
        result="BUSY",
    ),
    Rule(
        # Fallback: BUSY → IDLE direct transition (no DONE intermediate).
        # Fires when all BUSY evidence has aged out.
        name="fallback_busy_to_idle",
        raw_in=("IDLE",),
        prev_in=("BUSY",),
        result="IDLE",
    ),
    Rule(
        # Fallback: keep PERMIT until a hook signal (BUSY) arrives.
        # After user responds, there's a brief IDLE gap before the tool
        # starts; don't let the fallback turn that into IDLE.
        # HOLD_NO_WRITE: do not touch tmux state — preserve prior PERMIT.
        name="fallback_permit_hold",
        raw_in=("IDLE",),
        prev_in=("PERMIT",),
        hook_in=("PERMIT",),
        hook_age_lt=PERMIT_MAX_TIMEOUT,
        result="PERMIT",
        action=Action.HOLD_NO_WRITE,
    ),
    Rule(
        # Startup transient: `detect_pane_state` reports raw=BUSY when
        # the `claude` process has children (MCP servers, LSP, etc.)
        # and the `❯` prompt has not yet been rendered. During
        # Claude's 5–30 s startup window this signature looks
        # identical to an in-flight streaming response, so the pane-
        # tree heuristic returns BUSY either way.
        #
        # Authoritative discriminator: the `claude` process's own age
        # from the kernel. If the pid started less than
        # STARTUP_GRACE_SEC seconds ago and no hook signal has been
        # written yet (no UserPromptSubmit / PreToolUse for this
        # session), we are still in MCP-loading startup — real work
        # cannot have begun without a hook firing. An earlier version
        # of this rule keyed on prev_state, but during startup
        # prev_state briefly transitions SHELL → IDLE → BUSY as raw
        # flips (claude with no children → IDLE → MCP children
        # appear → BUSY), making prev_state an unstable
        # discriminator. The process age is monotonic.
        #
        # When the two conditions match, override the raw BUSY to
        # IDLE so the dashboard doesn't show 10 s of false BUSY after
        # every attach. Use HOLD_NO_WRITE so prev_state stays at
        # whatever the detection pipeline last wrote — rewriting to
        # IDLE here is not strictly required (the claude_pid_age
        # check doesn't depend on prev_state) but keeps the write
        # pattern consistent with other "this rule asserts the UI
        # state without committing it" cases.
        #
        # After STARTUP_GRACE_SEC the rule stops firing; if Claude is
        # genuinely hung during startup the state will fall back to
        # BUSY via `raw_not_idle`, which is the right outcome.
        name="startup_transient_raw_busy",
        raw_in=("BUSY",),
        hook_missing=True,
        claude_pid_age_lt=STARTUP_GRACE_SEC,
        result="IDLE",
        action=Action.HOLD_NO_WRITE,
    ),
    Rule(
        # raw BUSY/PERMIT passthrough: trust raw state.
        name="raw_not_idle",
        raw_in=("BUSY", "PERMIT"),
        result=USE_RAW,
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


# prev_state → synthetic raw mapping for the fast path.
# The fast path (statusline) skips the ps/capture-pane pipeline, so it
# has no real `raw` value. It derives one from prev_state under the
# assumption that Claude is still in the same lifecycle phase as the
# last authoritative slow-path evaluation.
_FAST_PREV_TO_RAW = {
    "DOWN": "DOWN",
    "SHELL": "SHELL",
    "BUSY": "BUSY",
    "PERMIT": "PERMIT",
    "IDLE": "IDLE",
    "": "IDLE",
}


def build_fast_context(prev_state, project_dir,
                       now=None) -> DetectionContext:
    """Build a DetectionContext for the read-only statusline path.

    Does not call ps/capture-pane/tmux queries for process tree info.
    Derives `raw` from prev_state, reads hook signal only.
    """
    if now is None:
        now = int(time.time())

    raw = _FAST_PREV_TO_RAW.get(prev_state, "IDLE")

    hook_state = ""
    hook_ts = 0
    hook_age = -1
    if project_dir:
        sig = ccm_core.read_hook_signal(project_dir)
        if sig is not None:
            hook_ts, hook_state, _detail = sig
            if hook_state == "SHELL":
                hook_state = ""
                hook_ts = 0
            else:
                hook_age = now - hook_ts

    if project_dir:
        jsonl_age, jsonl_last_stop_reason = ccm_core.read_jsonl_tail_info(project_dir)
    else:
        jsonl_age, jsonl_last_stop_reason = -1, None

    return DetectionContext(
        raw=raw,
        hook_state=hook_state,
        hook_ts=hook_ts,
        hook_age=hook_age,
        prev_state=prev_state,
        jsonl_age=jsonl_age,
        jsonl_last_stop_reason=jsonl_last_stop_reason,
        now=now,
    )


def evaluate_fast(prev_state, project_dir, now=None) -> str:
    """Read-only state evaluation for statusline-speed contexts.

    Runs the same DETECTION_RULES table as the slow path, so there is
    one source of truth for state transitions. Does not write to tmux
    — the slow-path run next cycle is authoritative for persisting state.
    """
    ctx = build_fast_context(prev_state, project_dir, now)
    _rule, state = evaluate_rules(ctx)
    return state


def build_detection_context(win_target, project_dir, prev_state,
                            panes_cache, ps_lines, own_pgid
                            ) -> DetectionContext:
    """Gather all inputs needed for rule evaluation.

    Read-only side effects only (tmux query, ps snapshot, file reads).
    The returned context is an immutable snapshot.
    """
    now = int(time.time())
    raw = detect_window_raw(win_target, panes_cache, ps_lines, own_pgid)

    # Find the window's primary claude_pid (first pane that hosts one).
    # Used to resolve the exact JSONL path via the runtime session file
    # at ~/.claude/sessions/{pid}.json AND to measure how long Claude
    # has been running (input to the startup-transient rule).
    claude_pid = None
    claude_pid_age = -1
    for wt, pane_pid, _pane_id in panes_cache:
        if wt != win_target:
            continue
        cp = find_claude_pid(pane_pid, ps_lines)
        if cp:
            claude_pid = cp
            claude_pid_age = ccm_core.find_process_age(cp, ps_lines)
            break

    hook_state = ""
    hook_ts = 0
    hook_age = -1
    if project_dir:
        sig = ccm_core.read_hook_signal(project_dir)
        if sig is not None:
            hook_ts, hook_state, _detail = sig
            # SHELL hook signal is ignored: process tree is authoritative
            # for SHELL; trusting a stale SHELL signal while raw=IDLE
            # causes false SHELL after Claude restarts.
            if hook_state == "SHELL":
                hook_state = ""
                hook_ts = 0
            else:
                hook_age = now - hook_ts

    if project_dir:
        jsonl_age, jsonl_last_stop_reason = ccm_core.read_jsonl_tail_info(
            project_dir, claude_pid=claude_pid
        )
    else:
        jsonl_age, jsonl_last_stop_reason = -1, None

    return DetectionContext(
        raw=raw,
        hook_state=hook_state,
        hook_ts=hook_ts,
        hook_age=hook_age,
        prev_state=prev_state,
        jsonl_age=jsonl_age,
        jsonl_last_stop_reason=jsonl_last_stop_reason,
        claude_pid_age=claude_pid_age,
        now=now,
    )


def apply_actions(win_target, project_dir, ctx: DetectionContext, rule: Rule,
                  state: str) -> str:
    """Execute the side effects declared by a matched rule.

    Returns the resolved state string.
    """
    action = rule.action

    # Optional trace log — useful for debugging false-BUSY / false-IDLE
    # reports without requiring a second ccm process. Activated by
    # setting CCM_DEBUG_TRACE to a file path in the environment of the
    # process that runs detection (inject_status, dashboard, etc.).
    # Each scan cycle appends one JSON-per-line record with every
    # DetectionContext input plus the matched rule + resolved state.
    # Writing happens here (after the rule is known, before side
    # effects) so a malformed trace never corrupts real state.
    _trace_scan(win_target, ctx, rule, state)

    # Record SHELL transitions for cluster-crash detection (#48069).
    # A transition into SHELL from a known *active* state (BUSY /
    # IDLE / PERMIT) is a real session exit and gets pushed.
    if state == "SHELL" and ctx.prev_state in (
        "BUSY", "IDLE", "PERMIT"
    ):
        ccm_core._push_shell_transition(win_target)

    if action == Action.HOLD_NO_WRITE:
        return state

    # Action.DEFAULT — set @ccm_prev_state.
    # Dispatched via the `ccm_core` module so that tests which patch
    # `ccm_core._set_win_state` observe the call site (direct local
    # reference would bypass the mock).
    ccm_core._set_win_state(win_target, state)

    # Set @ccm_completed_at when transitioning from BUSY/PERMIT to IDLE.
    # This is a display-layer marker — the ✔ icon shows for
    # COMPLETED_AT_TIMEOUT seconds after the transition.
    if state == "IDLE" and ctx.prev_state in ("BUSY", "PERMIT"):
        ccm_core.tmux_cmd("set-option", "-wt", win_target, "@ccm_completed_at", str(ctx.now))

    return state


def detect_window_state(win_target, project_dir, prev_state,
                        panes_cache, ps_lines, own_pgid):
    """Full detection pipeline. Returns the resolved state string.

    Thin orchestration layer:
      1. build_detection_context — gather inputs
      2. evaluate_rules          — pure rule-table match
      3. apply_actions           — execute tmux/file side effects

    All state transitions are declared in DETECTION_RULES above. To add
    or change a case, edit the rule table rather than this function.
    """
    ctx = build_detection_context(
        win_target, project_dir, prev_state,
        panes_cache, ps_lines, own_pgid,
    )
    rule, state = evaluate_rules(ctx)
    return apply_actions(win_target, project_dir, ctx, rule, state)


# ─── Debug trace (CCM_DEBUG_TRACE env var) ───
# When CCM_DEBUG_TRACE is set to a file path, every detection cycle
# emits a JSON line capturing the full DetectionContext, matched
# rule, and resolved state. This gives us one-shot visibility into
# why a given project is in a given state without needing to spin up
# `ccm debug trace` in a second pane — useful when the report is
# "it happened once last week" rather than "it's happening right
# now".
#
# Writing is best-effort (swallows I/O errors) so a broken trace
# file never blocks real detection. The writer uses line-buffered
# append so multi-project scans interleave correctly.
#
# Safety cap: at TRACE_MAX_BYTES the writer silently stops appending
# to prevent a forgotten CCM_DEBUG_TRACE from exhausting disk. One
# sentinel line is written at the threshold so a user returning to
# the log finds the reason writes stopped. The cap is resolved from
# CCM_TRACE_MAX_BYTES (default 100 MB) at module load; tuning it
# requires restarting the tmux server that launches inject-status.
TRACE_MAX_BYTES = int(
    os.environ.get("CCM_TRACE_MAX_BYTES", str(100 * 1024 * 1024))
)


def _trace_scan(win_target, ctx, rule, state):
    """Append one JSON record per detection cycle when CCM_DEBUG_TRACE
    is set. Records include every DetectionContext field plus the
    rule name and resolved state. Failure to open/write the trace
    file is silently ignored — this path must never break detection.

    Above `TRACE_MAX_BYTES` the writer emits a single sentinel line
    (so the reason for the gap is visible in the log) and stops
    appending. The stat + writes stay best-effort; a missing file
    or permission error is swallowed.
    """
    path = os.environ.get("CCM_DEBUG_TRACE")
    if not path:
        return
    try:
        size = -1
        try:
            size = os.path.getsize(path)
        except OSError:
            # File does not exist yet — treat as size 0 and proceed.
            size = 0
        if size >= TRACE_MAX_BYTES:
            # Write one sentinel line for the user then stop. Once the
            # sentinel itself is past the cap we no longer need to
            # write anything — the next append would just grow the
            # file past the cap again. Use a size delta check (any
            # bytes past the cap) so the sentinel is emitted exactly
            # once per process.
            if size < TRACE_MAX_BYTES + 200:
                try:
                    with open(path, "a") as f:
                        f.write(json.dumps({
                            "t": ctx.now,
                            "event": "trace_cap_reached",
                            "cap_bytes": TRACE_MAX_BYTES,
                            "note": "writes suspended; unset CCM_DEBUG_TRACE or set CCM_TRACE_MAX_BYTES higher",
                        }) + "\n")
                except OSError:
                    pass
            return
        record = {
            "t": ctx.now,
            "target": win_target,
            "raw": ctx.raw,
            "prev": ctx.prev_state,
            "hook_state": ctx.hook_state,
            "hook_age": ctx.hook_age,
            "jsonl_age": ctx.jsonl_age,
            "jsonl_stop": ctx.jsonl_last_stop_reason,
            "claude_pid_age": ctx.claude_pid_age,
            "rule": rule.name,
            "state": state,
            "action": rule.action.value,
        }
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
