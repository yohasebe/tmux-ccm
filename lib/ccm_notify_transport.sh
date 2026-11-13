# shellcheck shell=bash
# Whether a desktop notification goes through terminal-notifier: it is
# installed and `@ccm-notify-transport` is not `osascript`. One
# definition for the shell senders (hooks/lib.sh and
# hooks/sidekick-attention.sh); `_desktop` in ccm_notify.py applies the
# same rule in Python.
ccm_use_terminal_notifier() {
    command -v terminal-notifier >/dev/null 2>&1 || return 1
    local transport
    transport=$(tmux show-option -gqv @ccm-notify-transport 2>/dev/null) || transport=""
    [[ "$transport" != "osascript" ]]
}
