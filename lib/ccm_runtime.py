"""ccm runtime maintenance helpers.

Side-effecting utilities run by the polling loop (`inject_status`)
once per refresh cycle, after the per-window detection pipeline has
finished:

  - `update_window_names` rewrites tmux window names so the icon
    matches the freshly-resolved state. Cheap; runs every cycle.
  - `auto_exit_idle` walks all ccm windows, finds projects that
    have been IDLE longer than `@ccm-idle-timeout` (or the default
    `IDLE_EXIT_TIMEOUT`), and sends `/exit` to free the Claude
    process. Skipped for the currently focused window so the user
    is never logged out from under their cursor.
  - `_force_autosave` / `periodic_autosave` write the `_autosave`
    snapshot. The first is called synchronously after auto-exit so
    a vanished project is preserved for the next `ccm start`; the
    second runs every cycle but rate-limits itself to one save per
    2 minutes via the `autosave-time` marker.

Helpers late-bind to `ccm_core` for shared utilities (`tmux_cmd`,
`log_caught_exception`, `STATE_ICONS`, `IDLE_EXIT_TIMEOUT`,
`CCM_TMP_DIR`) so test mocks routed via `ccm_core.X` reach this
module unchanged. `ccm_detection._set_win_state` and
`ccm_snapshot.cmd_snapshot_save` are imported from their owning
modules directly.
"""

import os
import time

import ccm_constants
import ccm_core
import ccm_detection
import ccm_notify
import ccm_pane_state
import ccm_snapshot


# ─── Window name update ───

def update_window_names(projects):
    """Rewrite tmux window names so each carries the current state
    icon. No-op for any window whose name already matches."""
    all_windows = ccm_core.tmux_cmd(
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
        icon = ccm_core.STATE_ICONS.get(state, "●")
        new_name = f"{icon} {project}"
        if current_name != new_name:
            ccm_core.tmux_cmd("rename-window", "-t", win_target, new_name)


# ─── Auto-exit idle sessions ───

def _window_has_background_work(panes_raw, ps_lines):
    """True when any pane in the window shows live background work,
    meaning auto-exit must leave the window alone.

    Two disjoint signals, either sufficient:

      1. A NON-Claude pane whose foreground command is not a shell
         and not a parked interactive tool — a batch job, dev
         server, `tail -f`, anything autonomous the user left
         running in a split. The window IS the project; work running
         anywhere in it means the project is active even if the
         Claude conversation has been idle for hours.

         Editors and pagers (`PARKED_FOREGROUND_COMMANDS`) are
         exempt: a parked nvim is ambient tooling, not evidence of
         activity. Active use of an editor refreshes
         `window_activity` (screen output) and resets the idle timer
         on its own, and exiting Claude leaves the sibling pane
         untouched — so the exemption cannot interrupt anything.
         Without it, a split-editor workflow silently disables
         auto-exit for every window (observed 2026-07-17: three
         sessions idle for 3-4 days with `@ccm-idle-timeout 10` set,
         each with a parked nvim in the second pane).

      2. The Claude pane's process has a live SHELL child. Claude
         spawns a shell per Bash tool invocation, and that shell
         survives exactly as long as the job it runs — foreground OR
         `run_in_background`. MCP servers / LSP workers are direct
         children too but are never bare shells, so this does not
         false-positive on the standing worker set. A live shell
         child therefore means a tool job (possibly a background
         task whose completion will re-invoke Claude) is still
         running, and exiting Claude would orphan it.

    Deliberately conservative: false-keep costs one idle Claude
    process, false-exit interrupts real work — so anything that
    LOOKS like work counts as work. A window with a permanently
    running dev server or a parked editor will effectively never
    auto-exit; that is the intended trade-off, not a leak.

    Pane detection notes: `pane_current_command` cannot identify the
    Claude pane (the versioned-install symlink makes it report e.g.
    "2.1.207"), so Claude panes are identified by the same
    `find_claude_pid` process-tree walk detection uses, and the
    foreground-command check applies only to the remaining panes.
    Login-shell names carry a leading dash ("-zsh"); strip it before
    the shell-set membership test.
    """
    for line in (panes_raw or "").split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # CCM_IGNORE'd panes are invisible to ccm — a hidden sidekick's
        # editor or Bash job must neither block auto-exit nor count as
        # the window's work.
        if len(parts) >= 4 and parts[3] and parts[3] != "0":
            continue
        pane_pid = parts[1]
        pane_cmd = parts[2] if len(parts) >= 3 else ""
        claude_pid = ccm_pane_state.find_claude_pid(pane_pid, ps_lines)
        if claude_pid:
            for pl in ps_lines:
                p = pl.split()
                if (len(p) >= 4 and p[1] == str(claude_pid)
                        and p[3].lstrip("-")
                        in ccm_constants.SHELL_FOREGROUND_COMMANDS):
                    return True
        else:
            cmd = pane_cmd.lstrip("-") if pane_cmd else ""
            if (cmd
                    and cmd not in ccm_constants.SHELL_FOREGROUND_COMMANDS
                    and cmd not in ccm_constants.PARKED_FOREGROUND_COMMANDS):
                return True
    return False


def auto_exit_idle(projects):
    """Send `/exit` to ccm windows that have been IDLE longer than the
    configured timeout (`@ccm-idle-timeout` minutes; default
    `IDLE_EXIT_TIMEOUT` seconds). The currently focused window is
    always skipped — exiting the user's active pane would be
    surprising and destructive.

    Idle age is the maximum of `@ccm_completed_at` (set when the
    window most recently transitioned to IDLE) and `window_activity`
    (tmux's own touch on send-keys / resize / focus). The wider
    signal protects against false reset on a window the user
    interacted with but which never re-entered BUSY."""
    idle_timeout_str = ccm_core.tmux_cmd(
        "show-option", "-gqv", "@ccm-idle-timeout"
    )
    if idle_timeout_str:
        try:
            idle_timeout = int(idle_timeout_str) * 60
        except ValueError:
            idle_timeout = ccm_core.IDLE_EXIT_TIMEOUT
    else:
        idle_timeout = ccm_core.IDLE_EXIT_TIMEOUT

    if idle_timeout <= 0:
        return

    now = int(time.time())

    current_session = ccm_core.tmux_cmd("display-message", "-p", "#{session_name}")
    current_win = ccm_core.tmux_cmd("display-message", "-p", "#{window_index}")
    # The focused window is never auto-exited (logging the user out
    # from under their cursor would be hostile). That protection hinges
    # on identifying the focused window — if either query came back
    # empty (display-message failed / ran without a client context),
    # `current_target` would be ":" and match no real window, silently
    # removing the protection and letting the focused Claude be exited.
    # Bail this cycle instead; the next poll retries with a real target.
    if not current_session or not current_win:
        return
    current_target = f"{current_session}:{current_win}"

    activity_raw = ccm_core.tmux_cmd(
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
            ccm_core.tmux_cmd(
                "set-option", "-wt", win_target, "@ccm_completed_at", str(now)
            )
            continue

        idle_duration = now - idle_since
        if idle_duration >= idle_timeout:
            # Target the Claude pane specifically, NOT `win_target`
            # (which routes send-keys to the window's currently
            # active pane). If the user split off a shell pane and
            # left focus on it, send-keys to the window would land
            # the Escape + `/exit` + Enter sequence in the shell:
            # in emacs mode Esc becomes the meta prefix, Esc + `/`
            # is the no-op `complete` widget on an empty prompt, the
            # literal `exit` characters then fill the buffer, and
            # the trailing Enter submits `exit` to the shell —
            # silently killing the user's pane some indeterminate
            # time after they last touched it.
            #
            # The Claude pane is identified by walking each pane's
            # process tree (the same `find_claude_pid` lookup the
            # rest of state detection uses), NOT by string-matching
            # `#{pane_current_command}`. On a standard claude.ai
            # install the binary lives at .../versions/<X.Y.Z>/
            # with `claude` as a symlink; macOS's `proc_pidinfo`
            # then reports `pane_current_command` as the version
            # string (e.g. "2.1.167") instead of "claude". `ps`'s
            # `comm` field is "claude" though (Claude Code sets
            # `p_comm` explicitly), so the process-tree path stays
            # correct across installs while string-matching the
            # foreground command would silently mis-fire.
            panes_raw = ccm_core.tmux_cmd(
                "list-panes", "-t", win_target,
                "-F", "#{pane_index}\t#{pane_pid}\t#{pane_current_command}"
                "\t#{@ccm_ignore}"
            )
            # `ps_snapshot()` returns the raw stdout string; the
            # process-tree helpers iterate over lines, so split here.
            # Without this `find_claude_pid` walks one character at a
            # time and never matches anything, which is what made the
            # first cut of this fix silently no-op forever.
            ps_lines = ccm_core.ps_snapshot().strip().split("\n")

            # Background-work guard: a window that still hosts live
            # work must not have its Claude exited, no matter how
            # long the CONVERSATION has been idle. The cost asymmetry
            # decides this: wrongly exiting interrupts running work
            # and surprises the user (2026-07-11 monadic-chat
            # incident — a sibling-pane batch job's window went
            # quiet, the 10-min timer fired, and Claude was exited
            # out from under a project the user considered active),
            # while wrongly keeping costs one idle Claude process.
            # Checked only here — inside the already-rare timeout
            # branch — so the steady-state poll cycle pays nothing.
            if _window_has_background_work(panes_raw, ps_lines):
                continue

            # Multi-Claude windows (Agent Teams split panes) resolve to
            # the FIRST pane hosting a Claude, in pane-index order. Only
            # that one is exited; a teammate in another pane keeps
            # running. This is acceptable: auto-exit's whole reason to
            # exist is reclaiming idle resources, and a window idle long
            # enough to trip the timeout has all its teammates idle too.
            # The shell-foreground gate below means the window is only
            # marked SHELL once the targeted Claude has actually exited.
            claude_pane = None
            for line in (panes_raw or "").split("\n"):
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                # Never target a CCM_IGNORE'd pane for exit — the whole
                # point of ignore is that ccm keeps its hands off it.
                if len(parts) >= 4 and parts[3] and parts[3] != "0":
                    continue
                if ccm_pane_state.find_claude_pid(parts[1], ps_lines):
                    claude_pane = f"{win_target}.{parts[0]}"
                    break
            if not claude_pane:
                # No pane currently hosts a Claude process — the
                # window may be transitioning, Claude was already
                # backgrounded, or the user manually exited.
                # Defensive skip; the next polling cycle re-evaluates.
                continue

            # Cancel any partial input, then cleanly exit Claude Code.
            ccm_core.tmux_cmd("send-keys", "-t", claude_pane, "Escape")
            time.sleep(0.1)
            ccm_core.tmux_cmd("send-keys", "-t", claude_pane, "/exit", "Enter")
            time.sleep(0.5)
            # Confirm `/exit` actually completed before committing any
            # side effect. `/exit` can take longer than the 0.5 s wait
            # on sessions with heavy conversation history (Claude's
            # shutdown is context-size sensitive), or be swallowed
            # entirely if Claude is parked at a confirmation modal. A
            # ccm-launched Claude always runs as a child of the pane's
            # shell, so a SUCCESSFUL exit returns the pane's foreground
            # to that shell; anything else (still `claude`, the version-
            # string masking, or "" from a failed/timed-out query) means
            # the exit has not landed. Gate all three side effects on
            # that positive shell evidence:
            #   - `clear`: otherwise it lands as literal text in Claude's
            #     input box and submits as a stray user prompt.
            #   - SHELL state write: otherwise we declare SHELL while
            #     Claude is still alive (a one-cycle state lie that the
            #     next detection pass would have to undo) and fire a
            #     spurious autosave against it.
            # When the exit hasn't completed, do nothing this cycle; the
            # window keeps its real state and the next poll re-evaluates.
            current_cmd = ccm_core.tmux_cmd(
                "display-message", "-t", claude_pane, "-p", "#{pane_current_command}"
            )
            if current_cmd in ccm_constants.SHELL_FOREGROUND_COMMANDS:
                ccm_core.tmux_cmd("send-keys", "-t", claude_pane, "clear", "Enter")
                ccm_detection._set_win_state(win_target, "SHELL")
                # Force autosave after auto-exit to preserve project in snapshot.
                _force_autosave()
                # Tell the user WHAT happened and THAT nothing is
                # lost. An unannounced exit reads as a crash or a
                # mystery timeout and sends the user hunting for a
                # cause (2026-07-11 monadic-chat report); one
                # notification removes the entire investigation.
                # Fired only here — after the shell-foreground gate
                # confirmed the exit actually landed — so it can
                # never announce an exit that didn't happen.
                ccm_notify.notify(
                    "AUTOEXIT", project,
                    detail=f"{idle_timeout // 60}m",
                )


# ─── Autosave ───

def _force_autosave():
    """Force an immediate autosave. Silent on failure to keep the
    auto-exit cleanup path non-blocking, but logged so a recurrent
    autosave outage (disk full, snapshot dir permission lost) is
    visible via `ccm errors` rather than a black hole."""
    try:
        ccm_snapshot.cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        ccm_core.log_caught_exception("_force_autosave")


def periodic_autosave():
    """Save the `_autosave` snapshot if at least 2 minutes have
    passed since the last save. No-op when no ccm projects exist
    (so a stale snapshot from a previous session is preserved)."""
    marker = os.path.join(ccm_core.CCM_TMP_DIR, "autosave-time")
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

    check = ccm_core.tmux_cmd("list-windows", "-a", "-F", "#{@ccm_project}")
    has_projects = any(line.strip() for line in check.split("\n") if line.strip())
    if not has_projects:
        return

    try:
        ccm_snapshot.cmd_snapshot_save("_autosave", quiet=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(now))
    except Exception:
        # Periodic autosave is best-effort but a recurring failure
        # (e.g. disk full) is exactly the kind of silent issue that
        # `ccm errors` was designed to surface.
        ccm_core.log_caught_exception("periodic_autosave")
