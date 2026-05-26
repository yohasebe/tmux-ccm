"""`ccm send` — cross-pane prompt delivery to another project's
Claude Code session.

This is the most state-aware of the ccm commands and the only one
that mutates a *different* project's pane via `send-keys`. The
state-based gating policy is the safety story:

  - **IDLE / DONE**: send immediately
  - **BUSY**: refuse without `--force`. With `--force`, the message
    queues into Claude's input buffer (mixes with the current turn,
    user must want this)
  - **PERMIT**: ALWAYS refuse — typing into a permission dialog
    could accidentally approve or deny a tool call. The classifier
    in `ccm_constants.classify_permit_modal` distinguishes
    permission dialogs from safer modals (session-resume, /model
    picker) but the refusal is still unconditional; the
    classification only shapes the guidance message
  - **SHELL**: refuse without `--start`. With `--start`, launch
    Claude (`claude --continue ...`) and poll until the target
    reaches IDLE before sending. A fixed wait would mis-deliver
    the message when `claude --continue` triggers a follow-on
    action — most commonly an auto `/compact` on resume of a
    long session, or a session-resume picker (PERMIT modal) —
    because the keystrokes would land mid-modal and be eaten or
    queued into a dialog. Polling refuses with the captured pane
    tail so the operator can see exactly what happened and finish
    by hand

Multi-line messages (`\\n` in body) are converted to `M-Enter`
between lines + a final `Enter`, matching Claude Code's "newline
without submit" key convention so a multi-line prompt arrives as
one turn rather than several.
"""

import os
import sys
import time

import ccm_core  # late-bound for tmux_cmd / build_project_list / die / etc.
from ccm_constants import CLAUDE_CMD


# Maximum seconds to wait for a `--start`-launched target to
# reach IDLE before refusing. Tuned for two real scenarios:
#
#   - Normal resume (no auto-action): 1-5 s typical to IDLE.
#   - Resume into auto-`/compact` (long session): 10-60+ s in
#     BUSY. No reasonable wait gets the message delivered, so we
#     refuse early and let the operator verify by hand.
#
# 10 s comfortably covers the first case while refusing the
# second within a useful response time. Override with
# CCM_START_WAIT_SEC if your environment routinely needs more.
START_WAIT_SEC = int(os.environ.get("CCM_START_WAIT_SEC", "10"))

# How often the wait loop reports progress. The loop polls more
# frequently (so IDLE is caught quickly) but only prints when the
# tick interval has elapsed, so the operator gets a sign-of-life
# every second without log spam.
_WAIT_PROGRESS_TICK_SEC = 1.0


def _wait_for_target_idle(project_name, timeout_sec=None,
                          progress=False):
    """Poll the named project until its detected state is IDLE,
    or return the last observed non-IDLE state at timeout.

    Used by the `--start` path so the message-send only proceeds
    once the target is genuinely at the input prompt — not still
    initialising MCP servers, not in an auto-`/compact`, not on a
    session-resume picker. Polls every 0.5 s up to `timeout_sec`
    (default `START_WAIT_SEC`).

    Returns either the string `"IDLE"` (success), or the last
    non-IDLE state seen (one of `BUSY` / `PERMIT` / `SHELL` /
    `DOWN`). PERMIT and DOWN short-circuit the wait — they will
    not transition to IDLE without operator action, so further
    polling is wasted time and delays the refusal message that
    the operator needs.

    With `progress=True` the function prints one short status line
    per second while waiting, so an operator running an
    interactive `ccm send --start` knows something is happening
    rather than staring at a frozen terminal until the timeout.
    """
    if timeout_sec is None:
        timeout_sec = START_WAIT_SEC
    started = time.time()
    deadline = started + timeout_sec
    last_state = None
    last_progress = started
    while time.time() < deadline:
        time.sleep(0.5)
        projects = ccm_core.build_project_list(fast=False)
        target = next((p for p in projects if p.name == project_name), None)
        if target is None:
            return "DOWN"
        last_state = target.state
        if last_state in ("IDLE", "PERMIT", "DOWN"):
            return last_state
        now = time.time()
        if progress and now - last_progress >= _WAIT_PROGRESS_TICK_SEC:
            elapsed = now - started
            ccm_core.ccm_info(
                f"  [waiting {elapsed:.1f}s] state={last_state}"
            )
            last_progress = now
    return last_state or "BUSY"


_SEND_USAGE = (
    "Usage: ccm send <name|#idx> <message> "
    "[--file path] [--stdin] [--force] [--start] [--no-enter] [-y]"
)


def cmd_send(args):
    """Send a prompt to a project's Claude Code session.

    Usage:
      ccm send <name|#idx> <message>       Send literal message + Enter
      ccm send <name> --file <path>        Read message from file
      ccm send <name> --stdin              Read message from stdin
      ccm send <name> --no-enter <msg>     Send without submitting
      ccm send <name> --force <msg>        Send to a BUSY project (queued)
      ccm send <name> --start <msg>        Auto-launch Claude if SHELL
      ccm send -y <name> <msg>             Skip confirmation prompt
      ccm send <name> -- "--literal"       `--` ends flag parsing
    """
    if any(a in ("-h", "--help") for a in args):
        print(_SEND_USAGE)
        return
    target = None
    positional_parts = []
    message_file = None
    use_stdin = False
    no_enter = False
    force = False
    auto_start = False
    skip_confirm = False

    stop_flags = False
    i = 0
    while i < len(args):
        arg = args[i]
        if not stop_flags and arg == "--":
            stop_flags = True
            i += 1
            continue
        if not stop_flags and arg.startswith("-") and arg != "-":
            if arg == "--file":
                i += 1
                if i >= len(args):
                    ccm_core.ccm_die("--file requires a path argument")
                message_file = args[i]
            elif arg == "--stdin":
                use_stdin = True
            elif arg == "--no-enter":
                no_enter = True
            elif arg == "--force":
                force = True
            elif arg == "--start":
                auto_start = True
            elif arg in ("-y", "--yes"):
                skip_confirm = True
            else:
                ccm_core.ccm_die(f"Unknown flag: {arg}\n{_SEND_USAGE}")
        else:
            if arg == "-":  # conventional stdin alias
                use_stdin = True
            elif target is None:
                target = arg
            else:
                positional_parts.append(arg)
        i += 1

    if not target:
        ccm_core.ccm_die(_SEND_USAGE)

    # Resolve message source (exactly one of the three)
    positional_message = " ".join(positional_parts) if positional_parts else None
    source_count = sum(x is not None and x is not False for x in
                       (positional_message, message_file, use_stdin or None))
    if source_count == 0:
        ccm_core.ccm_die("No message provided (positional, --file, or --stdin)")
    if source_count > 1:
        ccm_core.ccm_die(
            "Provide exactly one of: positional message, --file, or --stdin"
        )

    if message_file:
        try:
            with open(message_file, encoding="utf-8") as f:
                message = f.read()
        except OSError as e:
            ccm_core.ccm_die(f"Failed to read message file: {e}")
    elif use_stdin:
        message = sys.stdin.read()
        # Once we have consumed stdin, the interactive confirmation
        # prompt can no longer read from it (EOFError). Force-skip
        # confirmation so a TTY user running `ccm send blog --stdin`
        # and typing a body terminated by Ctrl-D is not silently
        # cancelled.
        skip_confirm = True
    else:
        message = positional_message

    if not message.strip() and not no_enter:
        ccm_core.ccm_die(
            "Empty message (use --no-enter to send only Enter suppression)"
        )

    # Resolve target window
    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die(
            "Not inside a tmux session — start one with `tmux new-session` first"
        )

    if target.startswith("#"):
        idx = target[1:]
    elif target.isdigit():
        idx = target
    else:
        idx = ccm_core.find_window(session, target)
        if idx is None:
            ccm_core.ccm_die(f"Project not found: {target}")

    win_target = f"{session}:{idx}"

    # Look up project state from the current ccm scan
    projects = ccm_core.build_project_list(fast=False)
    matched = next((p for p in projects if p.win_target == win_target), None)
    if matched is None:
        ccm_core.ccm_die(f"Window is not a registered ccm project: {win_target}")

    project_name = matched.name
    state = matched.state

    # State-based gating
    if state == "PERMIT":
        # Give the caller (human or another Claude) enough information
        # to understand what the target pane is blocked on. The refusal
        # itself is unconditional — PERMIT is never auto-dismissed from
        # another pane even when the modal is safe, because
        # misclassification of a real permission dialog could
        # accidentally approve a tool call.
        raw_tail = ccm_core.tmux_cmd(
            "capture-pane", "-t", win_target, "-p", "-S", "-10"
        ) or ""
        if not raw_tail.strip():
            raw_tail = ccm_core.tmux_cmd(
                "capture-pane", "-a", "-t", win_target, "-p", "-S", "-10"
            ) or ""
        tail_lines = [l for l in raw_tail.split("\n") if l.strip()][-8:]
        category, guidance = ccm_core.classify_permit_modal(raw_tail)
        lines = [
            f"{project_name} is in PERMIT state — send refused.",
            f"  Classification: {category}",
            "  Guidance:",
        ]
        lines.extend(f"    {g}" for g in guidance.split("\n"))
        if tail_lines:
            lines.append("  Pane tail:")
            lines.extend(f"    {l}" for l in tail_lines)
        ccm_core.ccm_die("\n".join(lines))

    if state == "SHELL":
        if not auto_start:
            ccm_core.ccm_die(
                f"{project_name} is in SHELL state (Claude not running). "
                "Use --start to auto-launch Claude before sending."
            )
        ccm_core.ccm_info(f"Starting Claude in {project_name}...")
        ccm_core.tmux_cmd("send-keys", "-t", win_target, "-X", "cancel")
        ccm_core.tmux_cmd("send-keys", "-t", win_target, CLAUDE_CMD, "Enter")
        # Wait for the target to reach the input prompt. A fixed
        # sleep would mis-deliver the message when `claude
        # --continue` triggers a follow-on action — most commonly
        # an auto-`/compact` on resume of a long session, or a
        # session-resume picker (PERMIT modal). Refuse with the
        # captured pane tail so the operator sees exactly what
        # the target is doing and finishes by hand. Progress is
        # printed when run interactively so the operator does not
        # see a frozen terminal during the wait.
        interactive_wait = sys.stdout.isatty()
        ready_state = _wait_for_target_idle(
            project_name, progress=interactive_wait,
        )
        if ready_state != "IDLE":
            tail = ccm_core.tmux_cmd(
                "capture-pane", "-t", win_target, "-p", "-S", "-15",
            )
            tail_lines = [l for l in tail.split("\n") if l.strip()]
            lines = [
                f"{project_name} did not reach IDLE within "
                f"{START_WAIT_SEC}s after `claude --continue`.",
                f"  Last observed state: {ready_state}",
                "  Likely cause: Claude resumed into an auto-action",
                "    (e.g. `/compact` on a long session, or a",
                "    session-resume picker). The message would land",
                "    in the middle of that action and be eaten,",
                "    so the send is refused.",
                "  Switch to the target window, let the action",
                "    finish (or dismiss the modal), then retry.",
            ]
            if tail_lines:
                lines.append("  Pane tail:")
                lines.extend(f"    {l}" for l in tail_lines[-15:])
            ccm_core.ccm_die("\n".join(lines))

    if state == "BUSY" and not force:
        ccm_core.ccm_die(
            f"{project_name} is BUSY. The message would queue in the "
            "input buffer and mix with Claude's current turn. Use --force "
            "if that is what you want."
        )

    # `claude agents` TUI guard. The TUI shows an `❯` input prompt that
    # ccm reads as IDLE, but typing into it dispatches a BRAND-NEW
    # agent-view session rather than landing in an existing
    # conversation — a `ccm send` would silently spawn a session the
    # operator didn't ask for. Refused unconditionally (even with
    # `--force`): the BUSY-style "queue it anyway" semantic does not
    # map onto "dispatch a new agent". Check is gated on `state ==
    # "IDLE"` because the PERMIT / BUSY / SHELL branches above already
    # refuse or handle their own paths, so we don't pay the
    # capture-pane cost twice.
    if state == "IDLE":
        raw_tail = ccm_core.tmux_cmd(
            "capture-pane", "-t", win_target, "-p", "-S", "-10"
        ) or ""
        if not raw_tail.strip():
            raw_tail = ccm_core.tmux_cmd(
                "capture-pane", "-a", "-t", win_target, "-p", "-S", "-10"
            ) or ""
        if ccm_core.is_agents_tui(raw_tail):
            tail_lines = [l for l in raw_tail.split("\n") if l.strip()][-8:]
            lines = [
                f"{project_name} is showing the `claude agents` TUI — send refused.",
                "  Reason: keystrokes typed into the agents TUI dispatch a NEW",
                "    agent-view session rather than landing in an existing",
                "    Claude conversation.",
                "  User action: switch to the target pane and use the TUI's",
                "    own input (Enter to open a session, ? for shortcuts), or",
                "    detach from `claude agents` before retrying.",
            ]
            if tail_lines:
                lines.append("  Pane tail:")
                lines.extend(f"    {l}" for l in tail_lines)
            ccm_core.ccm_die("\n".join(lines))

    # Confirmation prompt (skip when piping or --yes)
    interactive = (
        not skip_confirm
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if interactive:
        preview = message.strip().replace("\n", " ")[:80]
        if len(message.strip()) > 80:
            preview += "..."
        tag = " (force)" if state == "BUSY" else ""
        print(f"Send to {project_name} ({state}{tag}): {preview}")
        try:
            ans = input("Proceed? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ccm_core.ccm_info("Cancelled")
            return
        if ans not in ("y", "yes"):
            ccm_core.ccm_info("Cancelled")
            return

    # Defensively exit any tmux mode on the target pane. Without this,
    # a pane stuck in copy-mode would interpret the message characters
    # as copy-mode bindings rather than typed input.
    ccm_core.tmux_cmd("send-keys", "-t", win_target, "-X", "cancel")

    # Literal send, converting `\n` into M-Enter (Claude Code's
    # "newline without submit" key) so the body is delivered as a
    # single multi-line prompt rather than multiple submitted turns.
    lines = message.split("\n")
    for line_i, line in enumerate(lines):
        if line:
            ccm_core.tmux_cmd("send-keys", "-t", win_target, "-l", line)
        if line_i < len(lines) - 1:
            ccm_core.tmux_cmd("send-keys", "-t", win_target, "M-Enter")

    # Final submit (unless --no-enter)
    if not no_enter:
        ccm_core.tmux_cmd("send-keys", "-t", win_target, "Enter")

    ccm_core.ccm_info(f"Sent to {project_name}")
