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
    display-popup -E -w 60% -h 40% -T " ccm Menu " "$CCM_BIN menu"

tmux bind-key "$CCM_KEY_TREE" \
    run-shell "$_session_cmd" \\\; \
    display-popup -E -w 80% -h 60% -T " ccm Tree " "$CCM_BIN tree-interactive"

# Click on status-right to open dashboard
tmux bind-key -T root MouseDown1StatusRight \
    run-shell "$_session_cmd" \\\; \
    display-popup -E -w 80% -h 60% -T " ccm Dashboard " "$CCM_BIN dashboard"

# Restore prefix + w to default (choose-tree)
tmux bind-key w choose-tree -Zs

# Save clean status-right before ccm modifies it
# Use tmux option to avoid file save/restore issues
_ccm_orig_sr=$(tmux show-option -gqv @ccm-orig-status-right 2>/dev/null)
if [[ -n "$_ccm_orig_sr" ]]; then
    # Re-source: restore original first
    tmux set -g status-right "$_ccm_orig_sr" 2>/dev/null
fi
tmux set -g @ccm-orig-status-right "$(tmux show-option -gv status-right 2>/dev/null)" 2>/dev/null
unset _ccm_orig_sr

# Initialize status injection
tmux run-shell "$CCM_BIN inject-status 2>/dev/null || true"
