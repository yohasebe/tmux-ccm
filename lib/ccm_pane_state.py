"""Raw pane / window state from process tree + capture-pane.

This module owns the "what does the terminal look like right now?"
half of detection — the half that runs without any hook signals,
JSONL session log, or event log. The output is a four-valued raw
state (`SHELL` / `BUSY` / `IDLE` / `PERMIT`) inferred from:

  1. The process tree under the pane (`find_claude_pid`,
     `has_children`) — distinguishes "no claude here" (SHELL) from
     "claude is running tools" (children present) from "claude has
     nothing spawned" (thinking, generating, or at rest — told
     apart by the pane's work clock, below).
  2. The pane text via `tmux capture-pane` (`capture_pane_bottom`,
     `capture_pane_visible`) — recognises Claude's `❯` input prompt
     (IDLE), modal footers like `Esc to cancel · Tab to amend`
     (PERMIT), and the work clock: the spinner's elapsed-time
     footer, believed only while it ticks (see `_clock_is_ticking`).

`detect_pane_state` combines both per pane; `detect_window_raw`
aggregates across panes (PERMIT > BUSY > IDLE > SHELL) with sliver
exclusion so that a hidden 1-row pane cannot infect the window
state with a false BUSY (capture-pane returns nothing for sliver
panes — has_child fires alone — pane reads BUSY indefinitely).

This is the **fallback** path. When event-log + JSONL signals are
healthy, the higher-fidelity classifier in `ccm_activity` overrides
the raw state — but raw remains as the last-resort signal when
hooks are uninstalled or upstream goes silent.

Late-bound `ccm_core` access: `tmux_cmd`, `SHELL_FOREGROUND_COMMANDS`,
and `SLIVER_HEIGHT_THRESHOLD` are accessed via `ccm_core.X` so test
mocks routed through `ccm_core` reach this module unchanged.
"""

import time
from collections import OrderedDict, namedtuple
from typing import Optional

from ccm_constants import (
    CLAUDE_PROCESS_NAME,
    IGNORED_CHILDREN,
    PATTERN_ACCEPT_EDITS,
    PATTERN_ACTIVE_SPINNER,
    PATTERN_INPUT_PROMPT,
    PATTERN_PERMIT_FOOTER,
    SPINNER_STALE_RELEASE_SEC,
)
# `import ccm_core` lives at the BOTTOM of this module (after the
# function definitions) so that when `ccm_pane_state` is the entry
# point of an import chain, our defs finish executing before
# ccm_core's bottom-of-file `import ccm_detection` triggers
# `from ccm_pane_state import detect_window_raw, find_claude_pid`.
# Functions still call `ccm_core.X` at runtime — by then, the
# bottom import has populated the module reference.


def find_claude_pid(parent_pid, ps_lines):
    """Return the pid of the `claude` process this pane hosts, or None
    when the pane is a bare shell.

    Two shapes count, because tmux produces both:

    * `claude` running as a child of the pane's shell — what every ccm
      flow creates, since `ccm add` / `ccm open` type the launch
      command into a shell.
    * the pane process *being* `claude` — what `tmux new-window
      "claude …"` creates, with no shell in between.

    The second shape was missed at first: the walk only ever
    looked for a child, so a pane whose own process is claude resolved
    to None and read as SHELL forever. `ccm debug trace` on a probe
    session hit this immediately (probes launch claude as the pane
    command), and a window registered with `ccm register` after being
    created that way would have been just as invisible.

    A child match still wins, so the common shape keeps its exact
    previous result and only the previously-blind case changes.
    """
    want = str(parent_pid)
    self_match = None
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[3] == CLAUDE_PROCESS_NAME:
            if parts[1] == want:
                return parts[0]
            if parts[0] == want:
                self_match = parts[0]
    return self_match


# One structured record per pane, produced by `enumerate_window_panes`.
#   claude_pid: the pane's claude pid, or None (also the has-claude flag)
#   ignored:    the pane's `@ccm_ignore` option is set
#   active:     the pane is the window's focused pane
PaneInfo = namedtuple(
    "PaneInfo",
    ["pane_id", "pane_pid", "active", "current_command", "ignored", "claude_pid"],
)


def enumerate_window_panes(win_target, ps_lines):
    """Enumerate a window's panes into `PaneInfo` records — the single
    source for the per-window `list-panes` format string, the
    `@ccm_ignore` parse, and the per-pane claude resolution.

    Several callers need "which pane in this window hosts claude":
    `ccm send`'s delivery pane (ccm_send), the dashboard
    preview (dashboard), and idle auto-exit (ccm_runtime). They differ
    only in POLICY — whether ignored panes are eligible, whether the
    active pane wins, whether ambiguity refuses or picks the first —
    so only the enumeration is shared here; each caller filters/selects
    on the returned list. (The `⊘` count in `build_project_list` is the
    exception: it reads the bulk `list-panes -a` cache to avoid a
    per-window query, so it does not use this helper.)

    Robust to short rows: fields past pane_pid degrade to
    active=False / current_command="" / ignored=False."""
    raw = ccm_core.tmux_cmd(
        "list-panes", "-t", win_target, "-F",
        "#{pane_id}\t#{pane_pid}\t#{pane_active}\t"
        "#{pane_current_command}\t#{@ccm_ignore}",
    )
    out = []
    for line in (raw or "").split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        out.append(PaneInfo(
            pane_id=parts[0],
            pane_pid=parts[1],
            active=len(parts) >= 3 and parts[2] == "1",
            current_command=parts[3] if len(parts) >= 4 else "",
            ignored=bool(len(parts) >= 5 and parts[4] and parts[4] != "0"),
            claude_pid=find_claude_pid(parts[1], ps_lines),
        ))
    return out


def has_children(pid, ps_lines, own_pgid):
    """Return True iff `pid` has at least one child process that is
    neither in our own process group (avoid counting the python
    detector itself if it spawned under claude) nor on the
    `IGNORED_CHILDREN` allowlist (caffeinate, MCP servers, etc. are
    permanent fixtures and don't indicate active tool use)."""
    for line in ps_lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == str(pid):
            if parts[2] == str(own_pgid):
                continue
            if parts[3] in IGNORED_CHILDREN:
                continue
            return True
    return False


def capture_pane_bottom(pane_target, lines=8):
    """Capture the bottom `lines` non-empty lines of a pane.

    Used for footer matching (PERMIT modal detection) where the
    relevant content is always the last ~3 rows.

    Handles alternate-screen mode (`CLAUDE_CODE_NO_FLICKER=1`) by
    trying the normal screen first and falling back to alternate
    capture if the normal read is empty. Returns a list (possibly
    empty) of stripped lines, oldest first."""
    raw = ccm_core.tmux_cmd("capture-pane", "-t", pane_target, "-p", "-S", "-10")
    if not raw or not raw.strip():
        raw = ccm_core.tmux_cmd("capture-pane", "-a", "-t", pane_target, "-p", "-S", "-10")
    if not raw:
        return []
    non_empty = [l for l in raw.split("\n") if l.strip()]
    return non_empty[-lines:]


def capture_pane_visible(pane_target):
    """Capture the entire visible area of a pane (no scrollback).

    Used for input-prompt (`❯`) detection where the prompt may sit
    well above the bottom rows when the user is composing a long
    multi-line message — the input area grows upward as text wraps,
    so a narrow tail-only capture loses the `❯` line. The visible
    area is bounded by the pane's current height, so this remains a
    fixed-cost capture regardless of scrollback length.

    Returns a list of stripped non-empty lines, oldest first."""
    raw = ccm_core.tmux_cmd("capture-pane", "-t", pane_target, "-p")
    if not raw or not raw.strip():
        raw = ccm_core.tmux_cmd("capture-pane", "-a", "-t", pane_target, "-p")
    if not raw:
        return []
    return [l for l in raw.split("\n") if l.strip()]


# ─── Work-clock staleness ───
#
# The no-children question — "is claude working or at rest?" — is
# answered by the pane's clock: the spinner footer carries elapsed
# seconds that tick once a second while a turn runs. But raw=BUSY
# has no timeout anywhere in the pipeline (every stale-release path
# requires raw=IDLE), so a STATIC clock must not be believed past a
# window: a frozen frame (claude hung after rendering the footer)
# and a transcript line merely quoting a footer are both static,
# and either would pin the window busy forever — a worse failure
# than the false idle this reading exists to prevent.
#
# So the claim is made only while the clock ticks. `_work_clock_cache`
# maps pane_target → {clock string → when that value was first seen}.
# A value seen for the first time is believed (fail-open — a one-shot
# process has no history, and BUSY is the safe direction); a value
# unchanged for longer than SPINNER_STALE_RELEASE_SEC is not. Kept
# per (pane, clock) rather than per pane because one screen can show
# several footers (a frozen one above a live one, two quotations) —
# with a single slot they would alternate and each read as "changed".
# In-process only, like the JSONL caches: detection stays read-only
# and long-lived pollers (inject_status, dashboard) accumulate the
# history the check needs.
_WORK_CLOCK_CACHE_MAX = 128   # panes
_PER_PANE_CLOCKS_MAX = 4      # distinct clock strings remembered per pane
_work_clock_cache: "OrderedDict[str, OrderedDict[str, float]]" = OrderedDict()

#: Indirection so tests can drive the staleness window deterministically.
_now = time.time


def _work_clock(line) -> Optional[str]:
    """Return the on-screen clock string marking an active turn on
    this line, or None.

    The matched parenthesised segment always contains the elapsed
    seconds (PATTERN_ACTIVE_SPINNER requires them), so the string
    itself serves as the clock: identical across passes means static.
    The spinning glyph and verb stay OUTSIDE the matched segment on
    purpose — they change even in a frame grabbed mid-animation, and
    including them would make a frozen frame look alive."""
    m = PATTERN_ACTIVE_SPINNER.search(line)
    if m:
        return m.group(0)
    return None


def _clock_is_ticking(pane_target, clock) -> bool:
    now = _now()
    clocks = _work_clock_cache.get(pane_target)
    if clocks is None:
        clocks = OrderedDict()
        _work_clock_cache[pane_target] = clocks
        if len(_work_clock_cache) > _WORK_CLOCK_CACHE_MAX:
            _work_clock_cache.popitem(last=False)
    else:
        _work_clock_cache.move_to_end(pane_target)
    first_seen = clocks.get(clock)
    if first_seen is None:
        if len(clocks) >= _PER_PANE_CLOCKS_MAX:
            clocks.popitem(last=False)
        clocks[clock] = now
        return True
    return now - first_seen <= SPINNER_STALE_RELEASE_SEC


def detect_pane_state(pane_pid, pane_target, ps_lines, own_pgid,
                      current_command=""):
    """Per-pane raw state. Returns SHELL / BUSY / IDLE / PERMIT.

    Resolution order (highest priority first):
      1. No claude under the pane → SHELL.
      2. Pane's foreground is a shell command (Ctrl-Z'd claude or
         inherited pid) → SHELL. Editor / pager foregrounds are NOT
         in this set — they mean the user is doing something else
         and ccm should not auto-start over them.
      3. Permit-footer matched at the bottom → PERMIT (matched even
         when claude has no children — permission dialogs appear
         before the tool subprocess spawns).
      4. Has live children + no input prompt anywhere visible →
         BUSY.
      5. Has live children + input prompt visible + an active-work
         spinner footer also visible → BUSY. In accept-edits mode
         the `❯` composer stays on screen WHILE a tool runs, so a
         visible prompt alone is not proof of idleness; the spinner
         (`… (elapsed · arrow Nk tokens)`) is rendered only during
         active generation / tool execution and disambiguates.
      6. Has live children + input prompt visible + no spinner →
         IDLE. The prompt is searched across the whole visible area
         (not just the bottom) because a long multi-line user input
         pushes the `❯` row well above the bottom 8 lines while the
         user is still composing.
      7. No children + a ticking work clock visible → BUSY. A turn
         spends its thinking and its generation with nothing spawned,
         and the spinner's elapsed-time footer is on screen the whole
         while — believed only while it ticks (`_clock_is_ticking`),
         so a frozen frame or a quoted footer cannot hold the window
         busy past SPINNER_STALE_RELEASE_SEC.
      8. No children + no ticking clock → IDLE.
    """
    claude_pid = find_claude_pid(pane_pid, ps_lines)
    if not claude_pid:
        return "SHELL"

    if current_command in ccm_core.SHELL_FOREGROUND_COMMANDS:
        return "SHELL"

    has_child = has_children(claude_pid, ps_lines, own_pgid)

    # PERMIT footer is always rendered at the very bottom of the
    # pane — a tail-only capture is sufficient and avoids
    # false-positives from "permit footer"-shaped strings appearing
    # in conversation content above.
    bottom = capture_pane_bottom(pane_target)
    for line in bottom:
        if PATTERN_PERMIT_FOOTER.match(line):
            return "PERMIT"

    if has_child:
        # Whether Claude's own UI is on screen at all, rather than
        # where its input box is. The composer is drawn continuously,
        # so this is true almost whenever Claude is running; what is
        # actually being asked is "has something covered the UI"
        # (a dialog, a flood of output).
        #
        # Do NOT read the match as locating the input area. A
        # submitted prompt is drawn into the transcript with the same
        # glyph, so the first `❯` on screen is usually an old message
        # — a belief that cost the send path a real bug. Nothing here
        # depends on which row matched, and the BUSY/IDLE call below
        # is made by the spinner, not by this.
        visible = capture_pane_visible(pane_target)
        prompt_visible = any(
            PATTERN_INPUT_PROMPT.match(line) and not PATTERN_ACCEPT_EDITS.match(line)
            for line in visible
        )
        if prompt_visible:
            # A visible `❯` normally means IDLE (queued-input box).
            # But accept-edits mode keeps that box on screen while a
            # tool runs, so check for the active-work spinner first:
            # its incrementing `(elapsed · arrow Nk tokens)` footer
            # appears only during generation / tool execution, never
            # at a true idle prompt or during a menu / permission
            # wait (Claude has stopped generating to ask). Present →
            # the pane is BUSY despite the visible prompt. This is
            # what un-sticks the false PERMIT when an approved
            # permission is the latest hook event and a long tool is
            # still running (see PATTERN_ACTIVE_SPINNER docstring).
            for line in visible:
                if PATTERN_ACTIVE_SPINNER.search(line):
                    return "BUSY"
            return "IDLE"
        return "BUSY"

    # No child process. That used to end the question, on the
    # reasoning that work means a tool is running — but a turn spends
    # its thinking and its generation entirely inside claude, with
    # nothing spawned, and the pane says so all the while: the
    # spinner's elapsed-time footer is on screen, ticking. Reading
    # only the process table there calls a working session idle, and
    # idle is the reading auto-exit acts on.
    #
    # The claim is gated on the clock ticking because raw=BUSY has no
    # release path: a static footer (frozen frame, quoted text) must
    # age out on its own — see `_clock_is_ticking`.
    #
    # Every visible clock is registered each pass, not just the
    # first: a screen can show several (a frozen footer above a
    # quotation), and each must age on its own first-seen time —
    # evaluating only the first would let the rest start their
    # windows whenever they happen to be reached.
    ticking = False
    for line in capture_pane_visible(pane_target):
        clock = _work_clock(line)
        if clock is not None and _clock_is_ticking(pane_target, clock):
            ticking = True
    if ticking:
        return "BUSY"

    return "IDLE"


def detect_window_raw(win_target, panes_cache, ps_lines, own_pgid):
    """Window-level raw state = aggregation across panes that are
    tall enough to render Claude's UI. Priority: PERMIT > BUSY >
    IDLE > SHELL.

    A tmux window can host multiple panes (single-pane projects are
    the dominant case, but Agent Teams splits a window into one
    pane per teammate and casual `prefix " ` / `prefix %` splits
    also occur). The user attends to all visible panes
    simultaneously, so the window state aggregates across panes —
    picking the most attention-needing one ensures that a single
    teammate waiting for permission surfaces in the dashboard even
    when focus is on another teammate.

    Sliver exclusion: a pane shorter than `SLIVER_HEIGHT_THRESHOLD`
    (default 4 rows) cannot render the `❯` prompt + accept-edits
    indicator + footer that pane-state detection relies on.
    capture-pane returns nothing useful, has_children fires alone,
    and the pane reads BUSY indefinitely — even when claude is
    long-idle. Excluding short panes prevents an invisible sliver
    from infecting the whole window. If every pane is below the
    threshold (impossible in practice), all panes are used as a
    last resort.

    `panes_cache` entries are 7-tuples:
        (win_target, pid, pane_id, current_command, pane_active,
         pane_height, ignore)

    CCM_IGNORE'd panes (index 6 set) are excluded from aggregation, so
    a hidden sidekick session (a second Claude pane launched with
    `CCM_IGNORE=1`) never contributes its PERMIT/BUSY to the window's
    state.
    """
    panes = []
    for pc in panes_cache:
        if pc[0] != win_target:
            continue
        if ccm_core._pane_is_ignored(pc):
            continue
        try:
            height = int(pc[5]) if pc[5] else None
        except (ValueError, IndexError):
            height = None
        # pane_active (pc[4]) intentionally unused here — only the
        # auto-focus helper consults it.
        panes.append((pc[1], pc[2], pc[3], height))

    if not panes:
        return "DOWN"

    eligible = [p for p in panes
                if p[3] is None or p[3] >= ccm_core.SLIVER_HEIGHT_THRESHOLD]
    if not eligible:
        eligible = panes

    best = "SHELL"
    for pid, pane_id, current_command, _height in eligible:
        state = detect_pane_state(pid, pane_id, ps_lines, own_pgid,
                                  current_command=current_command)
        if state == "PERMIT":
            return "PERMIT"
        if state == "BUSY":
            best = "BUSY"
        elif state == "IDLE" and best != "BUSY":
            best = "IDLE"
    return best


# See top-of-file note for why this lives below the function defs.
import ccm_core  # noqa: E402
