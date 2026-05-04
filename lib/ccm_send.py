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
    Claude (`claude --continue ...`) and wait 2 s for it to
    initialise before sending

Multi-line messages (`\\n` in body) are converted to `M-Enter`
between lines + a final `Enter`, matching Claude Code's "newline
without submit" key convention so a multi-line prompt arrives as
one turn rather than several.
"""

import sys
import time

import ccm_core  # late-bound for tmux_cmd / build_project_list / die / etc.
from ccm_constants import CLAUDE_CMD


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
                ccm_core.ccm_die(
                    f"Unknown flag: {arg}\n"
                    "Usage: ccm send <name> <message> "
                    "[--file path] [--stdin] [--force] [--start] "
                    "[--no-enter] [-y]"
                )
        else:
            if arg == "-":  # conventional stdin alias
                use_stdin = True
            elif target is None:
                target = arg
            else:
                positional_parts.append(arg)
        i += 1

    if not target:
        ccm_core.ccm_die(
            "Usage: ccm send <name> <message> "
            "[--file path] [--stdin] [--force] [--start] "
            "[--no-enter] [-y]"
        )

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
        # Crude wait for Claude to initialize. Longer would block ccm
        # pipelines; shorter risks sending before the input prompt is
        # ready. 2 seconds is a reasonable compromise on modern hardware.
        time.sleep(2)

    if state == "BUSY" and not force:
        ccm_core.ccm_die(
            f"{project_name} is BUSY. The message would queue in the "
            "input buffer and mix with Claude's current turn. Use --force "
            "if that is what you want."
        )

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
