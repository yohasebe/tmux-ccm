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
     footer or a retry countdown, believed only while it ticks (see
     `_clock_is_ticking`).

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
from collections import namedtuple
from typing import Optional, Tuple

from ccm_constants import (
    CLAUDE_PROCESS_NAME,
    IGNORED_CHILDREN,
    PATTERN_ACCEPT_EDITS,
    PATTERN_ACTIVE_SPINNER,
    PATTERN_INPUT_PROMPT,
    PATTERN_PERMIT_FOOTER,
    PATTERN_RETRY_BACKOFF,
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
# So a clock is believed when it MOVED since the last read — a live
# one changes every second — and an unmoved one only inside
# SPINNER_STALE_RELEASE_SEC. Believing movement rather than novelty
# is what keeps recycled strings honest: a retry countdown cycles
# through its few values every attempt, and a thinking footer
# repeats verbatim across turns, so aging by first-ever-sight would
# call a live retry storm idle — the dangerous direction, the one
# auto-exit acts on. The corner that loses: a screen showing
# several static footers at once alternates them and reads as
# movement, holding BUSY — accepted, because that direction only
# delays auto-exit.
#
# The history this comparison needs CANNOT live in-process: tmux
# spawns `ccm inject-status` fresh every status-interval, so the
# periodic path — the one auto-exit runs on — always starts empty,
# and an in-process cache only ever worked while the dashboard
# happened to be open. It therefore lives on the window itself, in
# `@ccm_work_clock` / `@ccm_work_clock_ts`, written by
# `apply_actions` on CHANGE only: a static frame must never refresh
# the timestamp, because its age is the verdict. Detection stays
# read-only; a process that finds nothing stored sees every clock
# as moved, i.e. fails open toward BUSY — the safe direction.
#
# The slot is per WINDOW, not per pane: single-pane projects (the
# dominant case) get exact semantics; a window with several
# clock-showing panes compares each pane's clock against the one
# stored value, so an unfamiliar clock reads as moved — the safe
# direction — and a frozen pane's clock ages out once it is the one
# being stored.

#: Indirection so tests can drive the staleness window deterministically.
_now = time.time


def _work_clock(line) -> Optional[str]:
    """Return the on-screen clock string marking an active turn on
    this line, or None.

    The matched segment always contains a ticking value — the
    spinner pattern requires the elapsed seconds, the retry pattern
    the countdown — so the string itself serves as the clock:
    identical across passes means static. The spinning glyph and
    verb stay OUTSIDE the matched segment on purpose — they change
    even in a frame grabbed mid-animation, and including them would
    make a frozen frame look alive. Tabs are stripped so the value
    is safe to persist through a tab-separated tmux format."""
    m = PATTERN_ACTIVE_SPINNER.search(line)
    if m:
        return m.group(0).replace("\t", " ")
    m = PATTERN_RETRY_BACKOFF.search(line)
    if m:
        return m.group(0).replace("\t", " ")
    return None


def _clock_is_ticking(clock, stored, now) -> bool:
    """Believe `clock` when it moved, or has not stood still for long.

    `stored` is the window's persisted `(clock_string, unix_ts)`
    from the previous detection pass, or None when nothing is
    stored. A different (or absent) stored value means the clock
    moved — believed at once. The same value is believed only
    inside SPINNER_STALE_RELEASE_SEC; past that it is a frozen
    frame or a quotation, not a running turn. Pure: the caller
    supplies the history and the time."""
    if stored is None or stored[0] != clock:
        return True
    return now - stored[1] <= SPINNER_STALE_RELEASE_SEC


def _scan_work_clock(lines, stored_clock, now, clock_out) -> bool:
    """True when any work clock in `lines` is ticking.

    The single implementation of the clock scan — the childless
    branch and the accept-edits disambiguation ask the same
    question ("is claude working?"), so they must not answer it
    separately. The first clock seen is reported to `clock_out`
    (when a list is passed) for the persistence hand-off. Every
    visible clock is evaluated, not just the first, so each starts
    being judged from the pass it appears on."""
    ticking = False
    found = None
    for line in lines:
        clock = _work_clock(line)
        if clock is None:
            continue
        if found is None:
            found = clock
        if _clock_is_ticking(clock, stored_clock, now):
            ticking = True
    if found is not None and clock_out is not None:
        clock_out.append(found)
    return ticking


def detect_pane_state(pane_pid, pane_target, ps_lines, own_pgid,
                      current_command="", stored_clock=None,
                      clock_out=None):
    """Per-pane raw state. Returns SHELL / BUSY / IDLE / PERMIT.

    `stored_clock` is the window's persisted `(clock, ts)` tuple the
    tick check compares against (see `_clock_is_ticking`); None means
    no history, so every clock reads as moved. `clock_out`, when a
    list is passed, collects the first work clock this pane shows
    (if any) so the caller can persist it — the history cannot live
    in-process (see the "Work-clock staleness" note above).

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
      5. Has live children + input prompt visible + a ticking work
         clock also visible → BUSY. In accept-edits mode the `❯`
         composer stays on screen WHILE a tool runs, so a visible
         prompt alone is not proof of idleness; the ticking footer
         is rendered only during active generation / tool execution
         and disambiguates. The clock is gated the same way as in 7,
         so a leftover child plus a static footer-shaped string
         cannot pin BUSY.
      6. Has live children + input prompt visible + no ticking clock
         → IDLE. The prompt is searched across the whole visible area
         (not just the bottom) because a long multi-line user input
         pushes the `❯` row well above the bottom 8 lines while the
         user is still composing.
      7. No children + a ticking work clock visible → BUSY. A turn
         spends its thinking and its generation with nothing spawned,
         and a retry backoff waits the same way — the spinner's
         elapsed-time footer or the `Retrying in Ns` countdown is on
         screen the whole while. Believed only while it ticks
         (`_clock_is_ticking`), so a frozen frame or a quoted footer
         cannot hold the window busy past SPINNER_STALE_RELEASE_SEC.
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
            # tool runs, so check for the work clock first: its
            # ticking footer appears only during generation / tool
            # execution, never at a true idle prompt or during a
            # menu / permission wait (Claude has stopped generating
            # to ask). Ticking → the pane is BUSY despite the visible
            # prompt. This is what un-sticks the false PERMIT when an
            # approved permission is the latest hook event and a long
            # tool is still running (see PATTERN_ACTIVE_SPINNER
            # docstring).
            # The gate is the same one the childless branch uses —
            # literally the same scan — because the question is the
            # same: "is claude working?", not "is anything alive?".
            # A leftover long-lived child (a dev server claude no
            # longer owns — the `(bg)` case) keeps this branch
            # reachable after the turn ends, and a static
            # spinner-shaped string on screen would otherwise pin
            # raw=BUSY, which has no release path. The leftover
            # process itself is the bg-active mechanism's concern,
            # not a reason to skip the gate.
            if _scan_work_clock(visible, stored_clock, _now(), clock_out):
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
    if _scan_work_clock(capture_pane_visible(pane_target),
                        stored_clock, _now(), clock_out):
        return "BUSY"

    return "IDLE"


def detect_window_raw(win_target, panes_cache, ps_lines, own_pgid,
                      stored_clock=None, clock_out=None):
    """Window-level raw state = aggregation across panes that are
    tall enough to render Claude's UI. Priority: PERMIT > BUSY >
    IDLE > SHELL.

    `stored_clock` / `clock_out` are passed through to
    `detect_pane_state`: the persisted work-clock history the tick
    check compares against, and the collector for the clock this
    window shows now (so `apply_actions` can persist it).

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
                                  current_command=current_command,
                                  stored_clock=stored_clock,
                                  clock_out=clock_out)
        if state == "PERMIT":
            return "PERMIT"
        if state == "BUSY":
            best = "BUSY"
        elif state == "IDLE" and best != "BUSY":
            best = "IDLE"
    return best


# See top-of-file note for why this lives below the function defs.
import ccm_core  # noqa: E402
