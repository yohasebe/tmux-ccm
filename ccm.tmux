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
_ccm_session_cmd='mkdir -p "${TMPDIR:-/tmp}/ccm-$(id -u)" && printf "#{session_name}" > "${TMPDIR:-/tmp}/ccm-$(id -u)/popup-session"'

# Keybindings
tmux bind-key "$CCM_KEY_DASHBOARD" run-shell "$_ccm_session_cmd" \; \
    display-popup -E -w 80% -h 60% -T " ccm Dashboard " "$CCM_BIN dashboard"

tmux bind-key "$CCM_KEY_MENU" run-shell "$_ccm_session_cmd" \; \
    display-popup -E -w 60% -h 40% -T " ccm Menu " "$CCM_BIN menu"

tmux bind-key "$CCM_KEY_TREE" run-shell "$_ccm_session_cmd" \; \
    display-popup -E -w 80% -h 60% -T " ccm Tree " "$CCM_BIN tree-interactive"

# Click on status-right to open dashboard
tmux bind-key -T root MouseDown1StatusRight run-shell "$_ccm_session_cmd" \; \
    display-popup -E -w 80% -h 60% -T " ccm Dashboard " "$CCM_BIN dashboard"

# Restore prefix + w to default (choose-tree)
tmux bind-key w choose-tree -Zs

# Initialize status injection
tmux run-shell "$CCM_BIN inject-status 2>/dev/null || true"
