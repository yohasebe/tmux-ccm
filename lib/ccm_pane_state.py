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

import ccm_core  # late-bound for tmux_cmd / SHELL_FOREGROUND_COMMANDS / SLIVER_HEIGHT_THRESHOLD
from ccm_constants import (
    CLAUDE_PROCESS_NAME,
    IGNORED_CHILDREN,
    PATTERN_ACCEPT_EDITS,
    PATTERN_INPUT_PROMPT,
    PATTERN_PERMIT_FOOTER,
)


def find_claude_pid(parent_pid, ps_lines):
    """Return the pid of the `claude` process whose parent is
    `parent_pid` (i.e. the claude running directly under the pane's
    shell). Returns None when the user is at a bare shell."""
    for line in ps_lines:
        parts = line.split()
        if (len(parts) >= 4
                and parts[1] == str(parent_pid)
                and parts[3] == CLAUDE_PROCESS_NAME):
            return parts[0]
    return None


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
      4. Has live children + no input prompt → BUSY.
      5. Has live children + input prompt visible → IDLE
         (background workers like MCP / dev servers don't change
         that the user is at the prompt).
      6. No children → IDLE.
    """
    claude_pid = find_claude_pid(pane_pid, ps_lines)
    if not claude_pid:
        return "SHELL"

    if current_command in ccm_core.SHELL_FOREGROUND_COMMANDS:
        return "SHELL"

    has_child = has_children(claude_pid, ps_lines, own_pgid)

    # Single capture-pane read shared between the permit-footer
    # check and the input-prompt check below; permit takes priority
    # (a dialog can be shown while children are zero).
    bottom = capture_pane_bottom(pane_target)
    for line in bottom:
        if PATTERN_PERMIT_FOOTER.match(line):
            return "PERMIT"

    if has_child:
        for line in bottom:
            if PATTERN_INPUT_PROMPT.match(line) and not PATTERN_ACCEPT_EDITS.match(line):
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

    `panes_cache` entries are 6-tuples:
        (win_target, pid, pane_id, current_command, pane_active, pane_height)
    """
    panes = []
    for pc in panes_cache:
        if pc[0] != win_target:
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
