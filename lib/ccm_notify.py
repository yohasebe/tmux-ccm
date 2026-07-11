"""Desktop notifications.

`notify(state, project, detail)` is the single entry point. It
respects the `@ccm-notify` tmux option (off / permit / completed /
permit,completed / all) and dispatches via the best available
backend:

  - macOS, terminal-notifier installed → `terminal-notifier
    -group ccm-<project>` so a fresh notification for a project
    replaces (rather than accumulates with) the previous one.
  - macOS, terminal-notifier missing → `osascript display
    notification` (no group / replace primitive).
  - Linux → `notify-send`.

`clear_notifications()` enumerates the macOS Notification Center
via `terminal-notifier -list ALL` and removes only `ccm-`-prefixed
groups, leaving notifications from other scripts intact.

`tmux_cmd` is accessed via `ccm_core.tmux_cmd` for late-bound
test-mock visibility.
"""

import subprocess

import ccm_core  # late-bound for tmux_cmd


_TERMINAL_NOTIFIER_PATH = None
_TERMINAL_NOTIFIER_CHECKED = False


def _terminal_notifier_path():
    """Return the path to `terminal-notifier` if installed, else None.

    Result is cached for the process lifetime — `which` is fast but
    we hit this on every notification.
    """
    global _TERMINAL_NOTIFIER_PATH, _TERMINAL_NOTIFIER_CHECKED
    if _TERMINAL_NOTIFIER_CHECKED:
        return _TERMINAL_NOTIFIER_PATH
    _TERMINAL_NOTIFIER_CHECKED = True
    try:
        r = subprocess.run(["which", "terminal-notifier"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            _TERMINAL_NOTIFIER_PATH = r.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return _TERMINAL_NOTIFIER_PATH


def notify(state, project, detail=""):
    """Send desktop notification for state changes.

    Controlled by @ccm-notify tmux option: off, permit, completed,
    permit,completed, all. `detail` is optional context (e.g.
    "Bash: rm -rf ..." for PERMIT).

    macOS notifications accumulate in Notification Center — a
    user who runs ccm continuously across many projects can build
    up hundreds of stale entries that drive WindowServer /
    NotificationCenter to high CPU. Two mitigations:

      1. If `terminal-notifier` is installed, use it with
         `-group ccm-<project>` so each project shows at most
         one notification (new replaces old). Recommended.
      2. Otherwise fall back to `osascript display notification`,
         which has no group / replace primitive — users who hit
         the NC-accumulation problem should either install
         terminal-notifier (`brew install terminal-notifier`),
         set `@ccm-notify permit` to drop the more-frequent
         COMPLETED notifications, or run `ccm clear-notifications`
         periodically.
    """
    setting = ccm_core.tmux_cmd("show-option", "-gqv", "@ccm-notify") or "permit,completed"
    if setting == "off":
        return

    state_lower = state.lower()
    # AUTOEXIT bypasses the per-state opt-in list (though not the
    # global "off" above): it announces an autonomous, destructive-
    # looking action ccm just took, and transparency about that must
    # not depend on the user having predicted the event and added it
    # to @ccm-notify. Without it an auto-exit reads as a crash or a
    # mystery timeout (2026-07-11 monadic-chat report).
    if state != "AUTOEXIT" and setting != "all" and state_lower not in setting:
        return

    sound_setting = ccm_core.tmux_cmd("show-option", "-gqv", "@ccm-notify-sound") or "off"
    sound_name = (ccm_core.tmux_cmd("show-option", "-gqv", "@ccm-notify-sound-name") or "Glass") if sound_setting == "on" else ""

    permit_body = f"Permission required: {detail}" if detail else \
                  "Action required — respond to the permission prompt"
    messages = {
        "PERMIT": (f"ccm ⚠ {project}",
                   permit_body,
                   sound_name),
        "COMPLETED": (f"ccm ✔ {project}",
                      "Claude has finished responding — review the output when ready",
                      sound_name),
        "BUSY":   (f"ccm ◉ {project}",
                   "Claude is now processing your request",
                   ""),
        "IDLE":   (f"ccm {project}",
                   "Waiting for your input",
                   ""),
        # detail carries the timeout, e.g. "10m".
        "AUTOEXIT": (f"ccm ■ {project}",
                     (f"Auto-exited after {detail} idle — "
                      "the conversation restores on next attach "
                      "(claude --continue)") if detail else
                     ("Auto-exited on idle timeout — the conversation "
                      "restores on next attach (claude --continue)"),
                     ""),
    }

    if state not in messages:
        return

    title, body, sound = messages[state]
    # Group ID per-project so a fresh notification for the same
    # project replaces (rather than accumulates with) the previous
    # one. terminal-notifier respects -group; osascript does not.
    group_id = f"ccm-{project}"

    tn_path = _terminal_notifier_path()
    if tn_path:
        # Intentionally NO `-sender com.apple.Terminal`. Specifying
        # a sender bundle id makes macOS deliver the notification
        # under that app's identity, which silently drops it for
        # every user not actually running Terminal.app (iTerm2,
        # WezTerm, kitty, ghostty, …). Letting terminal-notifier
        # use its own bundle id means the user grants notification
        # permission once and it works regardless of terminal.
        cmd_args = [tn_path,
                    "-message", body,
                    "-title", title,
                    "-group", group_id]
        if sound:
            cmd_args.extend(["-sound", sound])
        try:
            subprocess.Popen(cmd_args,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass  # fall through to osascript

    try:
        # Escape double quotes and backslashes for AppleScript string literals
        esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
        esc_body = body.replace("\\", "\\\\").replace('"', '\\"')
        sound_opt = ""
        if sound:
            esc_sound = sound.replace("\\", "\\\\").replace('"', '\\"')
            sound_opt = f' sound name "{esc_sound}"'
        cmd = f'display notification "{esc_body}" with title "{esc_title}"{sound_opt}'
        subprocess.Popen(["osascript", "-e", cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        try:
            subprocess.Popen(["notify-send", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass


def clear_notifications():
    """Remove ccm-sent notifications from the macOS Notification
    Center. Requires `terminal-notifier` (the only command-line
    way to enumerate / remove macOS notifications).

    Scopes the removal to ccm by enumerating `-list ALL` and
    deleting only group ids prefixed with `ccm-` (the convention
    `notify()` uses). `-remove ALL` was tempting but would also
    delete notifications a user has sent via terminal-notifier
    from unrelated scripts (deploy alerts, monitoring, …). The
    enumerate-then-filter approach pays one extra subprocess but
    keeps the user's other notifications intact.

    Notifications that pre-date the terminal-notifier integration
    (delivered via `osascript`) have no programmatic remove path
    and remain in Notification Center until the user dismisses
    them manually — `osascript display notification` does not
    expose an identifier the system will accept for later removal.

    Returns the count of removed notifications on success, -1 if
    terminal-notifier is not installed or the enumeration failed.
    """
    tn_path = _terminal_notifier_path()
    if not tn_path:
        return -1
    try:
        listing = subprocess.run(
            [tn_path, "-list", "ALL"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return -1

    # Other apps' notification titles may contain bytes that are not
    # valid UTF-8; decode permissively so a single bad title does not
    # abort the entire scrub.
    listing_text = (listing.stdout or b"").decode("utf-8", errors="replace")
    removed = 0
    for line in listing_text.splitlines()[1:]:  # skip header
        # `-list ALL` emits a TSV: GroupID<TAB>Title<TAB>...
        group_id = line.split("\t", 1)[0].strip()
        if not group_id.startswith("ccm-"):
            continue
        try:
            subprocess.run(
                [tn_path, "-remove", group_id],
                capture_output=True, text=True, timeout=5,
            )
            removed += 1
        except (subprocess.TimeoutExpired, OSError):
            # Best-effort: log nothing, continue with remaining ids
            continue
    return removed
