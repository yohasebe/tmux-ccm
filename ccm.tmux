#!/usr/bin/env bash
# ccm - tmux plugin entry point (loaded by TPM)
# This file is executed by TPM when the plugin is loaded.

CCM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CCM_BIN="${CCM_ROOT}/ccm"

# Store plugin root for hook scripts to find ccm binary
tmux set -g @ccm-plugin-root "$CCM_BIN" 2>/dev/null

# User-configurable keybindings (with defaults)
CCM_KEY_DASHBOARD=$(tmux show-option -gqv @ccm-key-dashboard 2>/dev/null)
CCM_KEY_MENU=$(tmux show-option -gqv @ccm-key-menu 2>/dev/null)
CCM_KEY_TREE=$(tmux show-option -gqv @ccm-key-tree 2>/dev/null)

CCM_KEY_DASHBOARD="${CCM_KEY_DASHBOARD:-Tab}"

# Temp dir setup command (used in run-shell for session detection)
_session_cmd='mkdir -p "${TMPDIR:-/tmp}/ccm-$(id -u)" && printf "#{session_name}" > "${TMPDIR:-/tmp}/ccm-$(id -u)/popup-session"'

# Keybindings — only dashboard is bound by default (Tab rarely conflicts)
# Menu and tree are opt-in via @ccm-key-menu / @ccm-key-tree to avoid
# conflicts with other plugins (e.g., tmux-sessionist binds C).
tmux bind-key "$CCM_KEY_DASHBOARD" \
    run-shell "$_session_cmd" \\\; \
    display-popup -E -w 80% -h 60% -T " ccm Dashboard " "$CCM_BIN dashboard"

if [[ -n "$CCM_KEY_MENU" ]]; then
    tmux bind-key "$CCM_KEY_MENU" \
        run-shell "$_session_cmd" \\\; \
        display-popup -E -w 80% -h 60% -T " ccm Menu " "$CCM_BIN menu"
fi

if [[ -n "$CCM_KEY_TREE" ]]; then
    tmux bind-key "$CCM_KEY_TREE" \
        run-shell "$_session_cmd" \\\; \
        display-popup -E -w 80% -h 60% -T " ccm Tree " "$CCM_BIN tree-interactive"
fi

# Pane title display: show Claude Code's session description in pane borders
# Controlled by @ccm-pane-title: "on" or "off" (default)
CCM_PANE_TITLE=$(tmux show-option -gqv @ccm-pane-title 2>/dev/null)
CCM_PANE_TITLE="${CCM_PANE_TITLE:-off}"
if [[ "$CCM_PANE_TITLE" == "on" ]]; then
    tmux set-option pane-border-status top 2>/dev/null
    tmux set-option pane-border-format "#{pane_title}" 2>/dev/null
fi

# Mouse click on ccm status icon → open dashboard
# Falls back to default behavior (select-window) for non-ccm clicks
tmux bind-key -n MouseDown1Status \
    if-shell -F '#{==:#{mouse_status_range},ccm}' \
    "run-shell '$_session_cmd' ; display-popup -E -w 80% -h 60% -T ' ccm Dashboard ' '$CCM_BIN dashboard'" \
    "switch-client -t ="

# Reduce Claude Code UI flicker in tmux (alt-screen rendering)
# ccm's capture-pane handles both normal and alternate screen modes
tmux set-environment -g CLAUDE_CODE_NO_FLICKER 1

# Ensure status-interval is not too aggressive (inject-status runs each cycle)
# Minimum 3 seconds to avoid excessive CPU usage from Python process spawning
_current_interval=$(tmux show-option -gv status-interval 2>/dev/null || echo 5)
if [[ "$_current_interval" -lt 3 ]] 2>/dev/null; then
    tmux set -g status-interval 3
fi

# Restore prefix + w to default (choose-tree)
tmux bind-key w choose-tree -Zs

# Auto-update hooks on plugin load (idempotent — skips if already up to date)
# This ensures new hook types added in updates are registered automatically.
tmux run-shell -b "$CCM_BIN setup-hooks >/dev/null 2>&1 || true"

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
