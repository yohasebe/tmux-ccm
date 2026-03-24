#!/usr/bin/env bash
# ccm - tmux plugin entry point (loaded by TPM)
# This file is executed by TPM when the plugin is loaded.

CCM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CCM_BIN="${CCM_ROOT}/ccm"

# User-configurable keybindings (with defaults)
CCM_KEY_DASHBOARD=$(tmux show-option -gqv @ccm-key-dashboard 2>/dev/null)
CCM_KEY_MENU=$(tmux show-option -gqv @ccm-key-menu 2>/dev/null)
CCM_KEY_TREE=$(tmux show-option -gqv @ccm-key-tree 2>/dev/null)

CCM_KEY_DASHBOARD="${CCM_KEY_DASHBOARD:-Tab}"
CCM_KEY_MENU="${CCM_KEY_MENU:-C}"
CCM_KEY_TREE="${CCM_KEY_TREE:-T}"

# Temp dir setup command (used in run-shell for session detection)
_session_cmd='mkdir -p "${TMPDIR:-/tmp}/ccm-$(id -u)" && printf "#{session_name}" > "${TMPDIR:-/tmp}/ccm-$(id -u)/popup-session"'

# Keybindings
tmux bind-key "$CCM_KEY_DASHBOARD" \
    run-shell "$_session_cmd" \\\; \
    display-popup -E -w 80% -h 60% -T " ccm Dashboard " "$CCM_BIN dashboard"

tmux bind-key "$CCM_KEY_MENU" \
    run-shell "$_session_cmd" \\\; \
    display-popup -E -w 80% -h 60% -T " ccm Menu " "$CCM_BIN menu"

tmux bind-key "$CCM_KEY_TREE" \
    run-shell "$_session_cmd" \\\; \
    display-popup -E -w 80% -h 60% -T " ccm Tree " "$CCM_BIN tree-interactive"

# Pane title display: show Claude Code's session description in pane borders
# Controlled by @ccm-pane-title: "on" or "off" (default)
CCM_PANE_TITLE=$(tmux show-option -gqv @ccm-pane-title 2>/dev/null)
CCM_PANE_TITLE="${CCM_PANE_TITLE:-off}"
if [[ "$CCM_PANE_TITLE" == "on" ]]; then
    tmux set-option pane-border-status top 2>/dev/null
    tmux set-option pane-border-format "#{pane_title}" 2>/dev/null
fi

# Restore prefix + w to default (choose-tree)
tmux bind-key w choose-tree -Zs

# Initialize status injection (delayed to let theme plugins finish loading)
# inject-status auto-detects external status-right changes (by themes etc.)
# and saves the correct original value before injecting ccm's status.
tmux run-shell -b "sleep 1 && $CCM_BIN inject-status 2>/dev/null || true"

# Auto-restore: load _autosave snapshot on tmux start
# Controlled by @ccm-auto-restore: "on" or "off" (default)
CCM_AUTO_RESTORE=$(tmux show-option -gqv @ccm-auto-restore 2>/dev/null)
CCM_AUTO_RESTORE="${CCM_AUTO_RESTORE:-off}"
if [[ "$CCM_AUTO_RESTORE" == "on" ]]; then
    CCM_SNAPSHOT_FILE="${HOME}/.local/share/ccm/snapshots/_autosave.json"
    if [[ -f "$CCM_SNAPSHOT_FILE" ]]; then
        # Only restore if no ccm projects are already loaded
        # sleep 2 to run after inject-status (sleep 1)
        tmux run-shell -b "
            sleep 2
            existing=\$(tmux list-windows -a -F '#{window_id} #{@ccm_project}' 2>/dev/null | awk '\$2 != \"\" {print}')
            if [ -z \"\$existing\" ]; then
                $CCM_BIN start _autosave 2>/dev/null || true
            fi
        "
    fi
fi
