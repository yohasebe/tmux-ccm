"""Raw pane / window state from process tree + capture-pane.

This module owns the "what does the terminal look like right now?"
half of detection — the half that runs without any hook signals,
JSONL session log, or event log. The output is a four-valued raw
state (`SHELL` / `BUSY` / `IDLE` / `PERMIT`) inferred from:

  1. The process tree under the pane (`find_claude_pid`,
     `has_children`) — distinguishes "no claude here" (SHELL) from
     "claude is running tools" (children present) from "claude is
     idle waiting for input" (no children).
  2. The bottom rows of the pane via `tmux capture-pane`
     (`capture_pane_bottom`) — recognises Claude's `❯` input
     prompt (IDLE) and modal footers like `Esc to cancel · Tab to
     amend` (PERMIT).

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

from collections import namedtuple

from ccm_constants import (
    CLAUDE_PROCESS_NAME,
    IGNORED_CHILDREN,
    PATTERN_ACCEPT_EDITS,
    PATTERN_ACTIVE_SPINNER,
    PATTERN_INPUT_PROMPT,
    PATTERN_PERMIT_FOOTER,
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

    The second shape was missed until 2026-08-08: the walk only ever
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
      7. No children → IDLE.
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
        # Scan the whole visible pane for the input prompt. The `❯`
        # row marks the top of the input area — when the user is
        # composing a multi-line message, the `❯` may be many rows
        # above the pane bottom while the user keeps typing.
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
