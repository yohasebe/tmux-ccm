"""Window-manipulation actions for ccm projects.

The three helpers here all change tmux window state in response to
a user-driven event (project add, attach, ccm-mediated focus
change). They are deliberately separate from the detection layer:
detection only *reads* the window, this module *writes* to it.

  - `auto_start_claude` — typed `claude --continue ...` into a
    shell-foreground pane to launch Claude. Honours
    `@ccm-auto-start`; refuses to send when no shell pane can be
    resolved safely (split window with an editor focused).
  - `reset_window_after_attach` — the post-attach reset bundle.
    Clears the `* elapsed` completion marker and acknowledges the
    cluster-SHELL canary. Calls `auto_focus_attention_pane` for
    multi-pane PERMIT-focus stealing.
  - `auto_focus_attention_pane` — when a PERMIT pane exists in a
    multi-pane window and the user attaches, switch focus to it
    so they do not type into the wrong pane.

Late-bound `ccm_core` access for `tmux_cmd` / `ps_snapshot` keeps
test mocks working uniformly.
"""

import os

import ccm_agentview
import ccm_core  # late-bound for tmux_cmd / ps_snapshot
from ccm_constants import (
    CLAUDE_CMD,
    SHELL_FOREGROUND_COMMANDS,
    SLIVER_HEIGHT_THRESHOLD,
)
from ccm_pane_state import detect_pane_state, enumerate_window_panes, read_work_clock

#: How long the hand-off notice stays on the tmux message line. Long
#: enough to read after the eye has moved to the launching pane.
CONTINUE_NOTICE_MS = 12000


def _resolve_launch_pane(win_target):
    """Return the pane target that is safe to type the Claude launch
    command into, or None when no pane can be resolved safely.

    `send-keys -t <window>` delivers to the window's ACTIVE pane. In a
    split window that pane may be running an editor or pager, and
    typing `claude --continue ...` there would inject the command
    string into vim instead of starting Claude. The policy mirrors
    `ccm send`'s SHELL-foreground guard (`_resolve_delivery_pane`):

      - the active pane, if its foreground is a shell (the common
        case);
      - else the single non-ignored shell-foreground pane;
      - else None — refuse to send. Not auto-starting beats typing
        into an unknown foreground.

    When pane enumeration fails entirely (tmux error) the window
    target is returned, preserving the pre-resolution defensive
    fallback (`ccm send` skips its guard on the same condition)."""
    ps_lines = ccm_core.ps_snapshot().strip().split("\n")
    panes = enumerate_window_panes(win_target, ps_lines)
    if not panes:
        return win_target
    live = [p for p in panes if not p.ignored]
    active = next((p for p in live if p.active), None)
    if active and active.current_command in SHELL_FOREGROUND_COMMANDS:
        return active.pane_id
    shell_panes = [p for p in live
                   if p.current_command in SHELL_FOREGROUND_COMMANDS]
    if len(shell_panes) == 1:
        return shell_panes[0].pane_id
    return None


def auto_start_claude(win_target):
    """Auto-start Claude Code if `@ccm-auto-start` is on (default).

    The launch command is typed into a shell-foreground pane only
    (see `_resolve_launch_pane`); when no such pane can be resolved
    safely, nothing is sent."""
    setting = ccm_core.tmux_cmd("show-option", "-gqv", "@ccm-auto-start") or "on"
    if setting != "on":
        return
    # Judged before the launch is typed: once `claude` starts it
    # creates its own transcript, and a scan after that would see a
    # fresh session where there was a hand-off. Judged before the
    # pane is resolved, too, so the foreground check is the last
    # thing before the keys go out.
    notice = continue_blocker_notice(win_target)
    pane = _resolve_launch_pane(win_target)
    if pane is None:
        return None
    ccm_core.tmux_cmd("send-keys", "-t", pane, CLAUDE_CMD, "Enter")
    if notice:
        # `display-message` expands tmux formats in its argument:
        # `#{...}` substitutes and `#(...)` runs a shell command. The
        # notice carries a project name, which is not required to be
        # free of `#`, so every `#` is doubled — the format escape —
        # before it is shown.
        ccm_core.tmux_cmd("display-message", "-d", str(CONTINUE_NOTICE_MS),
                          "ccm: " + notice.replace("#", "##"))
    return notice


def continue_blocker_notice(win_target):
    """The hand-off notice for this window's project, or None: the
    line to show when `claude --continue` is about to start a fresh
    session because the newest transcript was handed to a background
    session the daemon still lists.

    Decided from the disk, roster and session registry as they are
    now — this runs at the moment the launch command is about to be
    typed. The fresh session itself prints the CLI's own notice, which names
    the session but not the project or the state it is in; this one
    is what the reader sees while the launch is still on screen.
    Callers with a stdout of their own (`ccm attach`) print it too."""
    proj_dir = ccm_core.tmux_cmd(
        "show-option", "-wqv", "-t", win_target, "@ccm_dir"
    )
    if not proj_dir:
        return None
    try:
        blocker = ccm_agentview.continue_blocker(proj_dir)
    except Exception:
        ccm_core.log_caught_exception("auto_start_claude.continue_blocker")
        return None
    if blocker is None:
        return None
    name = ccm_core.tmux_cmd(
        "show-option", "-wqv", "-t", win_target, "@ccm_project"
    ) or proj_dir
    return ccm_agentview.format_continue_blocker(name, blocker)


def reset_window_after_attach(win_target):
    """Run the post-attach reset bundle for a project window.

    Called whenever the user attaches to a project (CLI `cmd_attach`,
    dashboard `_do_attach`, dashboard tree-mode attach). All side
    effects are keyed off `@ccm_dir`; on a non-ccm window this is a
    no-op:

    1. Unset `@ccm_completed_at` so the `* elapsed` completion
       marker disappears (stale completion markers from before the
       user attached should not appear to follow the attach).
    2. Unset `@ccm_shell_history` so the cluster-SHELL canary
       (#48069) is acknowledged. The warning will reappear only if
       NEW transitions cluster after the attach.

    `@ccm_prev_state` is intentionally NOT wiped: the
    `startup_transient_raw_busy` rule uses pid age as the monotonic
    discriminator and prev_state as a corroborating signal. Wiping
    it would conflate startup transients with real in-flight
    responses (both raw=BUSY + prev="") and produce ~10 s of false
    BUSY on every attach. The other wipes are cosmetic
    (completed_at) or per-canary (shell_history); they do not
    participate in rule evaluation.

    Symmetric across all attach paths — do not duplicate these
    set-option calls inline elsewhere.
    """
    proj_dir = ccm_core.tmux_cmd(
        "show-option", "-wqv", "-t", win_target, "@ccm_dir"
    )
    if not proj_dir:
        return
    ccm_core.tmux_cmd("set-option", "-wt", win_target, "-u", "@ccm_completed_at")
    ccm_core.tmux_cmd("set-option", "-wt", win_target, "-u", "@ccm_shell_history")
    auto_focus_attention_pane(win_target)


def auto_focus_attention_pane(win_target):
    """If the window has a pane in PERMIT state and that pane is
    not currently active, switch focus to it.

    Rationale: `detect_window_raw` aggregates state across panes
    and surfaces `⚠ PERMIT` for the whole window when any pane has
    a permission modal up. The user attaches to that window
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
    if the active pane already is PERMIT.
    """
    proj_dir = ccm_core.tmux_cmd(
        "show-option", "-wqv", "-t", win_target, "@ccm_dir"
    )
    if not proj_dir:
        return
    panes_raw = ccm_core.tmux_cmd(
        "list-panes", "-t", win_target, "-F",
        "#{pane_pid}\t#{pane_id}\t#{pane_current_command}\t"
        "#{pane_active}\t#{pane_height}",
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

    ps_lines = ccm_core.ps_snapshot().strip().split("\n")
    own_pgid = str(os.getpgrp())

    stored_clock = read_work_clock(win_target)
    permit_pane = None
    active_is_permit = False
    for pid, pane_id, cmd, is_active, height in rows:
        # Apply the same sliver filter as detect_window_raw — a short
        # pane cannot reliably report PERMIT either, so we do not
        # auto-focus it.
        if height and height < SLIVER_HEIGHT_THRESHOLD:
            continue
        state = detect_pane_state(pid, pane_id, ps_lines, own_pgid,
                                  current_command=cmd,
                                  stored_clock=stored_clock)
        if state != "PERMIT":
            continue
        if is_active:
            active_is_permit = True
            break
        if permit_pane is None:
            permit_pane = pane_id

    if active_is_permit:
        return
    if permit_pane is None:
        return
    ccm_core.tmux_cmd("select-pane", "-t", permit_pane)
