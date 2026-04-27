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
    PERMIT_GAP_TOLERANCE,
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


def detect_pane_state(pane_pid, pane_target, ps_lines, own_pgid,
                      current_command=""):
    claude_pid = find_claude_pid(pane_pid, ps_lines)
    if not claude_pid:
        return "SHELL"

    # Foreground-process override (2026-04-27, after observed personal
    # pane stuck-BUSY): claude may exist somewhere in the process tree
    # while the pane's foreground is actually a shell — e.g. user did
    # Ctrl-Z on claude and dropped to a fresh zsh, or the claude pid
    # was inherited from a parent shell but is no longer the active
    # process. tmux's pane_current_command reports the actual
    # foreground; if it is a shell command, the user is at a shell
    # prompt regardless of the leftover claude pid. Auto-start can
    # then trigger normally on the SHELL state. Editor / pager
    # foregrounds (vim, less, etc.) are NOT in this set — those mean
    # the user is actively doing something else and ccm should not
    # auto-start over them.
    if current_command in ccm_core.SHELL_FOREGROUND_COMMANDS:
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
    # panes_cache entries can be 3-tuples (legacy) or 4-tuples
    # (with pane_current_command, added 2026-04-27). Normalise to
    # 4-tuple here so the rest of the logic doesn't branch.
    panes = [
        (pid, pane_id, pc[3] if len(pc) >= 4 else "")
        for pc in panes_cache
        for wt, pid, pane_id in [pc[:3]]
        if wt == win_target
    ]
    if not panes:
        return "DOWN"

    best = "SHELL"
    for pid, pane_id, current_command in panes:
        state = detect_pane_state(pid, pane_id, ps_lines, own_pgid,
                                   current_command=current_command)
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

# ─── Phase taxonomy (Step 1 of a phased move toward a phase-machine
# architecture — see project_phase_machine_roadmap memo) ───
#
# Each rule is annotated with the session-lifecycle "phase" in which
# it is designed to fire. This is metadata only at Step 1: the rule
# engine still evaluates rules in priority order without consulting
# `phase`. The annotation exists so:
#   * `ccm debug trace` and `CCM_DEBUG_TRACE` log the phase alongside
#     the matched rule, making "why did this fire?" investigations
#     one step clearer.
#   * New rules must pick a phase (via the Rule constructor), which
#     forces authors to think about scope instead of quietly slotting
#     a rule into an arbitrary priority.
#   * A drift-guard test asserts every rule's phase is in PHASES or
#     explicitly None (for genuine catch-all passthroughs).
# Step 2 (future) would make the evaluator phase-scoped — only rules
# whose `phase` matches the current session phase would be evaluated,
# closing the rule-shadowing class of bugs structurally. That
# requires an authoritative "what phase are we in?" signal, which is
# why we're collecting phase data first.
PHASES = (
    "shell",          # no Claude process (DOWN / SHELL)
    "startup",        # claude pid young, MCP loading, no hooks yet
    "midturn",        # user prompt in flight; Claude generating / tool-calling
    "between_tools",  # Stop fired mid-response, next tool pending
    "idle",           # prompt visible, waiting for user input
    "permit",         # permission dialog open
)


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
    # Session-lifecycle phase this rule belongs to. Must be one of
    # `PHASES` above, or None for genuine catch-all passthroughs
    # whose phase depends on `ctx.raw` (e.g., `default`).
    # Metadata only — not consulted by `matches()`.
    # Surface in debug traces; enforced by a drift-guard test.
    phase: Optional[str] = None

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


# stop_reason values that mean "Claude's response truly ended"
# (versus "tool_use" which means "paused mid-response for a tool").
# Single source of truth for both the legacy DETECTION_RULES table
# (which uses the tuple form) and the event-log derive path (which
# uses the frozenset form for fast `in` checks). Defined before
# DETECTION_RULES so the rule definitions can reference the tuple.
TERMINAL_STOP_REASONS = frozenset({"end_turn", "max_tokens", "stop_sequence"})
TERMINAL_STOP_REASONS_TUPLE = tuple(sorted(TERMINAL_STOP_REASONS))


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
    Rule(name="process_down", raw_in=("DOWN",), result="DOWN", phase="shell"),
    Rule(name="process_shell", raw_in=("SHELL",), result="SHELL", phase="shell"),
    Rule(
        # Fast path: very fresh BUSY hook is trusted over any raw state.
        # The recap discriminator (hook_after_real_activity_lt) rejects
        # phantom hooks from v2.1.108+ recap events.
        name="hook_fresh_busy",
        hook_in=("BUSY",),
        hook_age_lt=HOOK_FRESH_THRESHOLD,
        hook_after_real_activity_lt=JSONL_HOOK_GAP_TOLERANCE,
        result="BUSY",
        phase="midturn",
    ),
    Rule(
        # PERMIT-axis mirror of hook_busy_jsonl_terminal_release.
        # When the user accepts a permission dialog (or the dialog
        # disappears for any reason — Esc dismissal, auto-approval,
        # parent process resolution) Claude Code does not always fire
        # a "permission resolved" hook to clear the PERMIT signal.
        # The stale PERMIT then locks `hook_permit_blocking` (which
        # accepts raw in (BUSY, PERMIT)) — and `accept edits on` mode
        # keeps raw=BUSY via PATTERN_ACCEPT_EDITS, so even after
        # claude completes the response the dashboard stays stuck on
        # ⚠ PERMIT. Observed live on monadic-chat 2026-04-26 evening
        # after permission approval in accept-edits mode: hook stayed
        # PERMIT for 5 minutes despite a fresh JSONL end_turn.
        #
        # Discriminator and threshold mirror the BUSY-axis rule.
        # hook_after_real_activity_lt=0 keeps a freshly-fired permit_req
        # (which is newer than any prior turn's JSONL terminal) on
        # PERMIT — only stale PERMIT signals overtaken by a newer
        # JSONL terminal release to IDLE. Falls through to the
        # `hook_permit_blocking` rule below otherwise.
        name="hook_permit_jsonl_terminal_release",
        hook_in=("PERMIT",),
        raw_in=("BUSY", "IDLE"),
        jsonl_missing=False,
        jsonl_age_lt=JSONL_HOOK_GAP_TOLERANCE,
        jsonl_last_stop_reason_in=TERMINAL_STOP_REASONS_TUPLE,
        hook_after_real_activity_lt=0,
        result="IDLE",
        phase="permit",
    ),
    Rule(
        # PERMIT signal lingers but the modal is NOT on screen (raw
        # is BUSY or IDLE, not PERMIT) AND the JSONL shows the
        # response is actively running a tool (`stop_reason=tool_use`
        # within the 10-minute long-tool window). This is the auto-
        # approved permit case: Claude Code fired permit_req for a
        # tool that the user has pre-approved (or accepted in
        # accept-edits mode), the dialog flashed and dismissed
        # without firing a "permission resolved" hook, and the tool
        # is now executing. From the user's perspective they are
        # being attended to (claude is responding) — show BUSY, not
        # PERMIT, because PERMIT semantically means "waiting on user
        # input" and there is nothing for the user to do.
        #
        # Discriminator: raw==PERMIT (modal physically visible) is
        # still the authoritative PERMIT signal — that case falls
        # through to `hook_permit_blocking` below. Only when the
        # capture-pane confirms no modal AND JSONL says a tool is
        # in flight do we re-classify as BUSY.
        name="hook_permit_tool_use_active",
        hook_in=("PERMIT",),
        # raw is BUSY or IDLE — modal NOT physically on screen
        # (raw=PERMIT is handled by hook_permit_blocking below
        # which is the authoritative on-screen-modal signal). The
        # `⏵⏵ accept edits on` line is on a SEPARATE line from
        # the `❯` input prompt, so detect_pane_state can return
        # raw=IDLE even when accept-edits is active — covering
        # both raw values catches both rendering layouts.
        raw_in=("BUSY", "IDLE"),
        jsonl_missing=False,
        jsonl_age_lt=BUSY_HOOK_JSONL_WINDOW,
        jsonl_last_stop_reason_in=("tool_use",),
        # The discriminator against "user dismissed dialog with
        # Esc, claude is genuinely idle" (the 2026-04-24 case):
        # in a real dismiss scenario the PermissionRequest hook
        # fired AFTER the last assistant tool_use write to JSONL,
        # so hook_age < jsonl_age (hook is fresher). Auto-approved
        # case is the opposite — tool runs AFTER permit, JSONL
        # gets updated with new records, so jsonl_age < hook_age
        # (JSONL fresher than hook). hook_after_real_activity_lt=0
        # encodes "JSONL strictly fresher than hook".
        hook_after_real_activity_lt=0,
        result="BUSY",
        phase="permit",
    ),
    Rule(
        # PERMIT dialog visible (raw != IDLE means input prompt is not
        # showing, so the permission UI is still active).
        name="hook_permit_blocking",
        hook_in=("PERMIT",),
        hook_age_lt=PERMIT_MAX_TIMEOUT,
        raw_in=("BUSY", "PERMIT"),
        result="PERMIT",
        phase="permit",
    ),
    Rule(
        # Symmetric to derive_state_from_events's Esc-interrupt fallback:
        # when the BUSY hook signal is stale (the Stop hook never fired
        # to clear it because the user pressed Esc, or the hook pipeline
        # went silent under #16047) and the JSONL tail shows the response
        # actually completed (terminal stop_reason fresher than the hook
        # itself), release BUSY → IDLE. The 60 s `BUSY_HOOK_JSONL_WINDOW`
        # cap on `hook_busy_idle` would otherwise leave the dashboard
        # stuck for up to 9.5 minutes after an Esc interrupt — observed
        # gap on 2026-04-26.
        #
        # The discriminator against "fresh prompt right after a previous
        # turn ended" (which would have a fresh hook=BUSY but stale
        # JSONL terminal from the prior turn) is `hook_after_real_activity_lt=0`:
        # this rule matches only when `jsonl_age < hook_age`, i.e. the
        # JSONL terminal is strictly fresher than the BUSY hook. A new
        # prompt's hook=BUSY is fresher than the prior turn's terminal,
        # so this rule will not fire for it; the existing `hook_busy_idle`
        # rule handles it normally.
        name="hook_busy_jsonl_terminal_release",
        hook_in=("BUSY",),
        raw_in=("IDLE",),
        jsonl_missing=False,
        jsonl_age_lt=JSONL_HOOK_GAP_TOLERANCE,
        jsonl_last_stop_reason_in=TERMINAL_STOP_REASONS_TUPLE,
        hook_after_real_activity_lt=0,
        result="IDLE",
        phase="midturn",
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
        phase="midturn",
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
        phase="midturn",
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
        # Genuine between-tools gap: Claude Code's Stop hook has
        # cleared the signal file and the next PreToolUse has not
        # yet fired, but the JSONL tail still shows
        # `stop_reason="tool_use"` so the response is mid-flight.
        # Hold BUSY until the JSONL flips to a terminal stop_reason
        # (`end_turn` / `max_tokens` / `stop_sequence`).
        #
        # CRITICAL: hook_missing=True is what gates this safely.
        # When the hook signal IS present:
        #   - hook=BUSY → `hook_busy_idle` (earlier in the chain)
        #     handles it, with the same gap-tolerance guard against
        #     recap-style phantom hooks.
        #   - hook=PERMIT → must NOT trigger this rule. Allowing it
        #     would re-create the 600 s BUSY-stuck symptom on the
        #     mirror axis of the (just-fixed) PERMIT-stuck case:
        #     after a user dismisses a permission dialog with Esc,
        #     the PERMIT signal stays stale AND the JSONL tail still
        #     carries the old tool_use stop_reason from the assistant
        #     turn that triggered the dismissed permission. With
        #     hook_missing=True the rule correctly falls through to
        #     fallback_busy_to_idle in that scenario. Verified
        #     empirically 2026-04-24 (live monadic-chat trace).
        name="jsonl_tool_use_pending",
        raw_in=("IDLE",),
        prev_in=("BUSY",),
        hook_missing=True,
        jsonl_missing=False,
        jsonl_last_stop_reason_in=("tool_use",),
        jsonl_age_lt=BUSY_HOOK_JSONL_WINDOW,
        result="BUSY",
        phase="between_tools",
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
        phase="midturn",
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
        phase="midturn",
    ),
    Rule(
        # Fallback: BUSY → IDLE direct transition (no DONE intermediate).
        # Fires when all BUSY evidence has aged out.
        name="fallback_busy_to_idle",
        raw_in=("IDLE",),
        prev_in=("BUSY",),
        result="IDLE",
        phase="idle",
    ),
    Rule(
        # Fallback: keep PERMIT for a brief grace window after the modal
        # footer disappears (raw transitioned to IDLE). Covers the
        # normal approve→PreToolUse handoff (sub-second, sometimes a
        # few seconds on slow machines).
        #
        # Critically NOT bounded by PERMIT_MAX_TIMEOUT (600 s): if the
        # user dismisses a permission dialog with Esc / No / Tab to
        # amend, Claude Code does NOT fire any follow-up hook to clear
        # the PermissionRequest signal — the hook file stays PERMIT
        # for 10 minutes, and a 10-minute hold here would leave the
        # dashboard frozen on PERMIT for the full window even though
        # the modal is long gone. With PERMIT_GAP_TOLERANCE (60 s)
        # the dashboard recovers to IDLE within a minute, while still
        # bridging the genuine approve→PreToolUse gap. The longer
        # PERMIT_MAX_TIMEOUT remains in effect for hook_permit_blocking,
        # which only fires while raw is still BUSY/PERMIT (modal still
        # visible — trust the hook signal in that case).
        #
        # HOLD_NO_WRITE: do not touch tmux state — preserve prior PERMIT.
        name="fallback_permit_hold",
        raw_in=("IDLE",),
        prev_in=("PERMIT",),
        hook_in=("PERMIT",),
        hook_age_lt=PERMIT_GAP_TOLERANCE,
        result="PERMIT",
        action=Action.HOLD_NO_WRITE,
        phase="permit",
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
        # BUSY via `raw_busy_passthrough`, which is the right outcome.
        name="startup_transient_raw_busy",
        raw_in=("BUSY",),
        hook_missing=True,
        claude_pid_age_lt=STARTUP_GRACE_SEC,
        result="IDLE",
        action=Action.HOLD_NO_WRITE,
        phase="startup",
    ),
    Rule(
        # raw=BUSY passthrough. Reached when none of the more
        # specific BUSY-promoting / IDLE-demoting rules above
        # matched — typically the no-hooks fallback path where the
        # process tree shows BUSY (claude has children, no `❯`
        # prompt) but neither the JSONL nor any hook signal can
        # be consulted. Phase is `midturn` because that is what
        # raw=BUSY actually represents at this point in the
        # priority chain (the post-grace startup case is split off
        # by `startup_transient_raw_busy` above; once we reach
        # here the BUSY is a real mid-turn signal).
        name="raw_busy_passthrough",
        raw_in=("BUSY",),
        result="BUSY",
        phase="midturn",
    ),
    Rule(
        # raw=PERMIT passthrough. Reached when none of the more
        # specific PERMIT rules above matched — typically the no-
        # hooks fallback where the capture-pane footer detected
        # a permission modal but no PermissionRequest hook fired
        # (or fired but was already cleared). Phase is `permit`.
        name="raw_permit_passthrough",
        raw_in=("PERMIT",),
        result="PERMIT",
        phase="permit",
    ),
    Rule(
        # Default: trust raw state. Always matches (terminal rule).
        # No phase — this fires in any unmatched case, typically
        # for raw=IDLE from prev=IDLE/SHELL/"" where no promoting
        # evidence exists. The surrounding context determines
        # semantic phase.
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


# ─── Event-log state derivation (detection redesign phase 2+) ───
# Phase 2 of the event-log redesign: state is a pure function of the
# event-log tail plus JSONL stop_reason and process lifecycle signals.
# The time-window heuristics that gate the legacy DETECTION_RULES
# (JSONL_FRESH_THRESHOLD, JSONL_ACTIVE_THRESHOLD, JSONL_HOOK_GAP_TOLERANCE,
# BUSY_HOOK_JSONL_WINDOW, and the implicit fallback_busy_to_idle timing)
# all collapse down to a single STARTUP_GRACE_SEC pid-age check here.
#
# Activated by `CCM_USE_EVENT_LOG` env var:
#   unset    — legacy DETECTION_RULES path only (no event-log read)
#   observe  — compute both, log to trace, still use legacy (diff study)
#   1 / primary — compute both, log, use event-log state as authoritative
#
# Event type → state class mapping. Keep in sync with the normalized
# vocabulary emitted by `hooks/lib.sh::ccm_append_event`. Adding a
# new upstream hook event means: pick its normalized name at the
# writer, add it here, and add a parametrized test.
EVENT_CLASS_START = "start"  # → BUSY
EVENT_CLASS_PERMIT = "permit"  # → PERMIT
EVENT_CLASS_PAUSE = "pause"  # → IDLE (terminal) or BUSY (tool_use) depending on JSONL
EVENT_CLASS_IDLE = "idle"  # → IDLE (explicit)
EVENT_CLASS_END = "end"  # → SHELL

EVENT_CLASSES = {
    "prompt": EVENT_CLASS_START,
    "pretool": EVENT_CLASS_START,
    "posttool": EVENT_CLASS_START,
    "subagent": EVENT_CLASS_START,
    "compact": EVENT_CLASS_START,
    "stop": EVENT_CLASS_PAUSE,
    "permit_req": EVENT_CLASS_PERMIT,
    "notify_permit": EVENT_CLASS_PERMIT,
    "notify_idle": EVENT_CLASS_IDLE,
    "session_end": EVENT_CLASS_END,
}

def _jsonl_fresher_than_event(latest, jsonl_age, now):
    """Return True iff JSONL was updated AFTER the latest event log
    record. This is the bare time-comparison primitive — no stop_reason
    or window checks. Callers add their own conditions on top.

    Used by the Esc / hook-silence release paths to reject the
    "fresh prompt right after a previous turn ended" false-positive
    (the JSONL terminal in that case predates the new event).

    Returns False when:
      - jsonl_age is unavailable (-1)
      - the latest event has no parsable timestamp
      - `now` is unavailable (0)
      - the latest event is fresher than (or equal to) JSONL
    """
    if jsonl_age < 0:
        return False
    event_ts = latest.get("ts", 0) if isinstance(latest, dict) else 0
    if not (now > 0 and event_ts > 0):
        return False
    event_age = now - event_ts
    return event_age > jsonl_age


def _jsonl_terminal_fresher_than_event(latest, jsonl_stop_reason,
                                       jsonl_age, now):
    """Combines `_jsonl_fresher_than_event` with the terminal
    stop_reason check + the JSONL_HOOK_GAP_TOLERANCE window. Used by
    the start-class and permit-class Esc-fallback paths in derive."""
    if jsonl_stop_reason not in TERMINAL_STOP_REASONS:
        return False
    if jsonl_age > JSONL_HOOK_GAP_TOLERANCE:
        return False
    return _jsonl_fresher_than_event(latest, jsonl_age, now)


def derive_state_from_events(events, jsonl_stop_reason,
                             pid_present, claude_pid_age, raw=None,
                             jsonl_age=-1, now=0):
    """Pure function: resolve state from event log tail + JSONL + pid.

    Arguments:
        events: iterable of {"ts": int, "type": str} dicts, newest last.
            Empty iterable means "no event log for this project yet".
        jsonl_stop_reason: string or None. The stop_reason of the most
            recent assistant record in the JSONL tail. Used only when
            the latest event is a "stop" — IDLE (terminal) vs BUSY
            (tool_use mid-turn) discriminator.
        pid_present: bool. True iff a `claude` process currently runs
            for this project's window. SHELL when False, regardless of
            any stale events in the log.
        claude_pid_age: int. Seconds since the `claude` process started,
            or -1 if unknown. Used to suppress the false-IDLE that
            would otherwise apply during MCP-loading startup with no
            events recorded yet.
        raw: optional capture-pane classification ("IDLE" / "BUSY" /
            "PERMIT" / "SHELL" / "DOWN"). When `raw=="PERMIT"` the
            function returns "PERMIT" regardless of event-log content
            — the pane footer match is the authoritative signal for
            modal-blocked panes (the event log can lag behind the
            PermissionRequest hook firing or miss it entirely under
            anthropics/claude-code#16047 class regressions).
        jsonl_age: optional. Seconds since the most recent JSONL
            real-activity record, or -1 if unavailable. Used together
            with `jsonl_stop_reason` to detect Esc-interrupt and hook
            silence: a fresh JSONL terminal stop_reason combined with
            a start-class latest event proves the response actually
            ended even though the Stop hook never wrote a `stop`
            event. In that case the function defers to legacy by
            returning None.

    Returns one of: "SHELL", "PERMIT", "BUSY", "IDLE", or None.
    None means "no authoritative answer" — the caller should commit
    the legacy state instead. Returned when (a) the event log is
    empty (no hook events recorded yet), (b) the latest record is
    malformed or carries an unknown type, or (c) pid is present but
    the latest event is `session_end` (claude has been restarted and
    the new session has not yet emitted any event).

    Semantics (see project_event_log_redesign memo for the decision log):
      - pid absent → SHELL (authoritative from process tree)
      - latest event permit-class → PERMIT (no time limit; ONLY an
        event clears PERMIT, not a timer)
      - latest event start-class → BUSY
      - latest event notify_idle → IDLE (Claude itself signalled idle)
      - latest event stop → IDLE if JSONL stop_reason is terminal,
        BUSY if tool_use or unknown (tool still in flight)
      - raw=="PERMIT" → PERMIT (overrides any non-PERMIT candidate)
      - no events / unknown type / session_end with pid present →
        None (fall back to legacy detection)
    """
    if not pid_present:
        return "SHELL"

    # Tuple/list/iterator — defensive normalization. derive must not
    # depend on random-access indexing so any iterable works.
    events_seq = tuple(events) if not isinstance(events, tuple) else events

    if not events_seq:
        # No hook activity recorded yet. Could be (a) fresh Claude
        # session before first UserPromptSubmit, (b) hooks never
        # installed on this project, (c) event log file was cleaned
        # up out from under us. The previous behaviour was to return
        # IDLE here, which masked a 2.7-hour real outage observed
        # 2026-04-25 where the file went missing and `ccm send` would
        # have happily injected into a pane that was actually showing
        # a PERMIT modal. Returning None forces caller fallback to
        # legacy detection (capture-pane footer + JSONL heartbeat),
        # which keeps the modal visible to the dashboard.
        return None

    latest = events_seq[-1]
    t = latest.get("type") if isinstance(latest, dict) else None
    if not t:
        # Malformed latest record — let legacy decide rather than
        # silently committing IDLE.
        return None

    klass = EVENT_CLASSES.get(t)
    if klass is None:
        # Unknown event type (upstream schema drift) — defer to legacy.
        return None
    if klass == EVENT_CLASS_END:
        # session_end with pid present: claude has restarted but the
        # new session has not yet emitted any event. Returning SHELL
        # would falsely show "claude not running" on the dashboard for
        # the brief window between SessionEnd and the next prompt.
        # Defer to legacy (which sees the live pid via raw).
        return None

    if klass == EVENT_CLASS_PERMIT:
        candidate = "PERMIT"
        # raw=="PERMIT" precedence: if a modal is physically on
        # screen (capture-pane footer match) we trust it over any
        # JSONL state — that is the user-blocking case PERMIT was
        # designed to flag.
        if raw == "PERMIT":
            return "PERMIT"
        # No modal on screen. The latest permit event was either
        # silently resolved (Claude Code does not fire a permission-
        # resolved hook) or dismissed by the user. Look at JSONL to
        # disambiguate the underlying activity:
        #
        #   1. Terminal stop_reason fresher than the permit event →
        #      response truly ended → IDLE (Esc-dismiss / silent-
        #      resolve mirror of the start-class fallback below).
        #   2. JSONL shows a tool is in flight (`tool_use` within
        #      the long-tool window) → claude is actively running
        #      tools (auto-approved permit). The user is being
        #      attended to, not waiting — show BUSY, not PERMIT.
        #   3. Otherwise → PERMIT remains (cosmetic stuck state,
        #      visible via the (Nm) suffix).
        if _jsonl_terminal_fresher_than_event(
                latest, jsonl_stop_reason, jsonl_age, now):
            return "IDLE"
        # raw is not PERMIT (handled above). If JSONL shows a tool
        # is in flight AND JSONL is fresher than the permit event
        # (the same fresher-than-event discriminator we use for
        # the terminal-release path above), claude is actively
        # running tools after the permit was silently resolved →
        # BUSY. The fresher-than-event guard rejects the dismiss
        # case where JSONL last update predates the permit event.
        if (jsonl_stop_reason == "tool_use"
                and 0 <= jsonl_age <= BUSY_HOOK_JSONL_WINDOW
                and _jsonl_fresher_than_event(latest, jsonl_age, now)):
            return "BUSY"
    elif klass == EVENT_CLASS_START:
        # Phantom-subagent shortcut (2026-04-27). The Claude Code
        # upstream fires occasional spurious `SubagentStart` /
        # `SubagentStop` hooks during otherwise-idle periods (status
        # line refresh? auto-memory? — root cause filed in memory
        # `project_phantom_subagent`). Pattern: `... stop, notify_idle,
        # subagent` with no follow-up activity. Legitimate subagent
        # events always come mid-conversation (after a `prompt` or
        # tool event); only the phantom case appears AFTER a
        # `notify_idle` with no intervening start-class event. Walk
        # back through any stacked subagent events; if we land on a
        # `notify_idle` without crossing a `prompt` or tool event,
        # the latest is phantom — defer to legacy.
        if t == "subagent" and raw != "PERMIT":
            for i in range(len(events_seq) - 2, -1, -1):
                prev_event = events_seq[i]
                prev_t = (prev_event.get("type")
                          if isinstance(prev_event, dict) else None)
                if prev_t == "subagent":
                    continue  # walk past stacked phantoms
                if prev_t == "notify_idle":
                    return None  # phantom chain after idle
                break  # crossed a real event; legitimate context
        # Esc-interrupt / hook-silence fallback (2026-04-26).
        # Latest event is start-class (prompt / pretool / posttool /
        # subagent / compact) but JSONL shows the assistant just
        # completed with a terminal stop_reason. The response really
        # ended, but the Stop hook failed to fire — either the user
        # pressed Esc to interrupt mid-stream (Esc bypasses Stop in
        # current Claude Code), or the hook pipeline went silent
        # (anthropics/claude-code#16047 class). The naive "defer to
        # legacy" doesn't help here: legacy's hook_busy_idle rule
        # holds BUSY off the stale BUSY signal that the Stop hook
        # would have cleared, so deferring would still report BUSY.
        # Commit IDLE directly — JSONL is the authoritative
        # completion signal.
        # raw=="PERMIT" is the one exception — A' (capture-pane
        # footer match) wins because a modal literally on screen is
        # more authoritative than a pre-modal JSONL completion.
        # The fresher-than-event check is the regression guard
        # against "fresh prompt right after a previous turn ended"
        # false-positives; centralised in
        # `_jsonl_terminal_fresher_than_event`.
        if (raw != "PERMIT" and _jsonl_terminal_fresher_than_event(
                latest, jsonl_stop_reason, jsonl_age, now)):
            return "IDLE"
        # Combined-stale fallback (2026-04-27): a start-class event
        # with no follow-up for >BUSY_HOOK_JSONL_WINDOW (10 min)
        # AND a similarly stale JSONL is the long-tail signature of
        # any other spurious upstream firing (not just subagent).
        # Defer to legacy.
        if raw != "PERMIT":
            event_ts = latest.get("ts", 0) if isinstance(latest, dict) else 0
            if now > 0 and event_ts > 0:
                event_age = now - event_ts
                if (event_age > BUSY_HOOK_JSONL_WINDOW
                        and 0 <= jsonl_age
                        and jsonl_age > BUSY_HOOK_JSONL_WINDOW):
                    return None
        candidate = "BUSY"
    elif klass == EVENT_CLASS_IDLE:
        candidate = "IDLE"
    elif klass == EVENT_CLASS_PAUSE:
        # stop event: BUSY if still in tool_use mid-turn (claude
        # paused waiting on a tool result), IDLE if the response
        # completed with a terminal stop_reason. Missing stop_reason
        # (older schema or no assistant record in tail) is
        # conservative → BUSY so that long-running tools without
        # clear evidence do not flip to false IDLE.
        # Pre-v0.3.0 this branch returned "CONT" for the tool-use
        # case to give the dashboard a visual hint. Removed for
        # state-model purity: per the user-centered principle
        # (docs/state-machine.md), state captures "does the user
        # need to act" — and CONT and BUSY were both "wait", so
        # they collapse into BUSY. The diagnostic distinction (was
        # claude streaming or paused?) lives in the pane itself
        # (look at the spinner / capture-pane) rather than in the
        # state label.
        if jsonl_stop_reason in TERMINAL_STOP_REASONS:
            candidate = "IDLE"
        else:
            candidate = "BUSY"
    else:
        # Defensive — every defined class above is handled, but a new
        # EVENT_CLASS_* added without updating this branch should
        # surface as a legacy fallback rather than a hard error.
        return None

    # Capture-pane PERMIT footer overrides any non-PERMIT derivation.
    # The PermissionRequest hook can fire after the modal is already
    # rendered, or fail to fire at all under #16047-class regressions,
    # leaving the latest event still a start-class hook (pretool /
    # posttool / prompt) when a permission dialog is actually waiting
    # on user input. The capture-pane footer match (PATTERN_PERMIT_FOOTER)
    # is reliable in those cases — a modal cannot render its footer
    # without actually being on screen.
    if raw == "PERMIT" and candidate != "PERMIT":
        return "PERMIT"

    return candidate


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
    for entry in panes_cache:
        # 3-tuple (legacy) or 4-tuple (with current_command, 2026-04-27)
        wt = entry[0]
        pane_pid = entry[1]
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
                  state: str, event_log_state=None) -> str:
    """Execute the side effects declared by a matched rule.

    Returns the resolved state string.

    `event_log_state` is the result of `derive_state_from_events`
    when the event-log path is active (observe or primary); it is
    forwarded to `_trace_scan` so the debug trace records both
    states on every scan regardless of which one is committed.
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
    _trace_scan(win_target, ctx, rule, state, event_log_state=event_log_state)

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

    # Background-activity affordance (option C, 2026-04-27).
    # Conceptually: state captures "whose turn it is" (BUSY = claude
    # is responding, IDLE = user has the ball). But process-tree
    # detection sets raw=BUSY whenever a tool grandchild exists —
    # including leftover dev servers / background scripts that
    # claude started but no longer owns. The event-log path now
    # correctly overrides those to IDLE (via the rule chain that
    # trusts JSONL stop_reason terminal), but the background
    # processes are still running. Surface the discrepancy as a
    # `(bg)` UI suffix so the user knows "claude is at rest, but
    # something it spawned is still alive". Written when the
    # committed state is IDLE but raw says BUSY (process tree has
    # active children/grandchildren). Cleared whenever the two
    # agree, so a stale flag cannot survive a state change.
    if state == "IDLE" and ctx.raw == "BUSY":
        ccm_core.tmux_cmd("set-option", "-wt", win_target, "@ccm_bg_active", "1")
    elif state != "IDLE" or ctx.raw != "BUSY":
        # Clear when no longer applicable. -u unsets the option.
        ccm_core.tmux_cmd("set-option", "-wut", win_target, "@ccm_bg_active")

    return state


def _event_log_mode():
    """Read CCM_USE_EVENT_LOG env var and normalize to one of:
      "off"      — disabled (legacy path only, no event-log read).
                   Must be set explicitly (`0` / `off` / `no` / `false`).
      "observe"  — compute both, log to trace, use legacy as authoritative
      "auto"     — commit event-log state when derive returns non-None;
                   otherwise fall back to legacy. **This is the default
                   when the env var is unset** (P3b, 2026-04-25). Pre-P3b
                   the unset default was "off"; the flip is justified by
                   the observe-mode rollout finding zero false-IDLE diffs
                   that the safety nets in `derive_state_from_events`
                   (raw=PERMIT override, None-on-empty fallback) did not
                   already catch
      "primary"  — same dispatch as auto today; kept as a distinct token
                   so a future diagnostic flag can bring back the
                   "commit even when events are empty" behaviour without
                   reusing this name

    Accepted aliases: "1" / "true" / "yes" / "primary" / "on" → "primary".
    Falsy values (`""` / `"0"` / `"off"` / `"no"` / `"false"`) → "off".
    Unset → "auto" (the new default).
    Unknown values → "auto" (conservative: opt-in remains the safe
    side; users who explicitly want legacy-only must say so).
    """
    raw_env = os.environ.get("CCM_USE_EVENT_LOG")
    if raw_env is None:
        return "auto"  # P3b default
    raw = raw_env.strip().lower()
    if raw in ("", "0", "off", "no", "false"):
        return "off"
    if raw == "observe":
        return "observe"
    if raw == "auto":
        return "auto"
    if raw in ("1", "true", "yes", "primary", "on"):
        return "primary"
    return "auto"


def detect_window_state(win_target, project_dir, prev_state,
                        panes_cache, ps_lines, own_pgid):
    """Full detection pipeline. Returns the resolved state string.

    Thin orchestration layer:
      1. build_detection_context — gather inputs
      2. evaluate_rules          — pure rule-table match
      3. (phase 2+) derive_state_from_events — parallel event-log path
      4. apply_actions           — execute tmux/file side effects

    All state transitions are declared in DETECTION_RULES above. To add
    or change a case, edit the rule table rather than this function.

    The event-log path is activated by CCM_USE_EVENT_LOG:
      - observe: compute both paths, log the diff to CCM_DEBUG_TRACE,
        still commit the legacy state (risk-free observation).
      - auto: commit the event-log state when `derive_state_from_events`
        returns a non-None answer; otherwise fall back to legacy. The
        derive function returns None for empty event logs, malformed
        records, unknown event types, and `session_end` with a live
        pid — situations where the event log lacks an authoritative
        signal and the legacy path's capture-pane / process-tree /
        JSONL heuristics are more reliable.
      - primary: same dispatch as auto today (the old "commit IDLE
        even when events are empty" behaviour is unsafe — see the
        2026-04-25 observation where a 2.7-hour event-log outage
        would have produced false IDLE for a pane actually showing a
        PERMIT modal). Kept as a separate mode so a future diagnostic
        flag can bring back unconditional event-log commits if needed.
    """
    ctx = build_detection_context(
        win_target, project_dir, prev_state,
        panes_cache, ps_lines, own_pgid,
    )
    rule, legacy_state = evaluate_rules(ctx)

    mode = _event_log_mode()
    event_log_state = None
    if mode != "off":
        events = ccm_core.read_events_tail(project_dir)
        # pid_present: the legacy raw detection already resolved SHELL
        # when no claude process is present, so raw != "SHELL" is a
        # reliable "pid present" proxy without a second ps scan.
        pid_present = ctx.raw not in ("SHELL", "DOWN")
        event_log_state = derive_state_from_events(
            events=events,
            jsonl_stop_reason=ctx.jsonl_last_stop_reason,
            pid_present=pid_present,
            claude_pid_age=ctx.claude_pid_age,
            raw=ctx.raw,
            jsonl_age=ctx.jsonl_age,
            now=ctx.now,
        )

    # Commit which state?
    #   observe → legacy (event_log_state is logged for diff only)
    #   auto    → event_log_state when not None, else legacy. The
    #             None sentinel covers all cases where the event log
    #             cannot speak authoritatively (empty, malformed,
    #             unknown type, post-session_end transient).
    #   primary → same dispatch as auto for now; see docstring.
    if mode in ("primary", "auto") and event_log_state is not None:
        resolved = event_log_state
    else:
        resolved = legacy_state

    return apply_actions(win_target, project_dir, ctx, rule, resolved,
                         event_log_state=event_log_state)


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


def _trace_scan(win_target, ctx, rule, state, event_log_state=None):
    """Append one JSON record per detection cycle when CCM_DEBUG_TRACE
    is set. Records include every DetectionContext field plus the
    rule name and resolved state. Failure to open/write the trace
    file is silently ignored — this path must never break detection.

    When the event-log path is active (CCM_USE_EVENT_LOG), the
    derived state is included as `event_log_state` alongside the
    legacy rule result. A `diff=true` field flags rows where the
    two disagree, so `jq -c 'select(.diff)'` isolates the interesting
    scans during observation.

    `CCM_TRACE_ONLY_DIFF=1` (any truthy value) restricts writes to
    rows where the legacy and event-log derivations disagree. Lets
    observe-mode run for days on a busy multi-project tmux without
    hitting the size cap — the file only grows when a diff actually
    shows up. Has no effect when the event-log path is disabled
    (nothing to diff against, so the file would stay empty).

    Above `TRACE_MAX_BYTES` the writer emits a single sentinel line
    (so the reason for the gap is visible in the log) and stops
    appending. The stat + writes stay best-effort; a missing file
    or permission error is swallowed.
    """
    path = os.environ.get("CCM_DEBUG_TRACE")
    if not path:
        return
    only_diff = os.environ.get(
        "CCM_TRACE_ONLY_DIFF", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if only_diff and (event_log_state is None or event_log_state == state):
        # No disagreement to record. Skip before any stat/write I/O
        # so the hot path stays cheap on quiet scans.
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
            "phase": rule.phase,
            "state": state,
            "action": rule.action.value,
        }
        if event_log_state is not None:
            record["event_log_state"] = event_log_state
            if event_log_state != state:
                record["diff"] = True
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
