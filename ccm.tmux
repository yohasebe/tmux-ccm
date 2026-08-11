#!/usr/bin/env bash
# ccm - tmux plugin entry point (loaded by TPM)
# This file is executed by TPM when the plugin is loaded.

CCM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CCM_BIN="${CCM_ROOT}/ccm"

# User-configurable keybindings (with defaults)
CCM_KEY_DASHBOARD=$(tmux show-option -gqv @ccm-key-dashboard 2>/dev/null)
CCM_KEY_MENU=$(tmux show-option -gqv @ccm-key-menu 2>/dev/null)
CCM_KEY_TREE=$(tmux show-option -gqv @ccm-key-tree 2>/dev/null)
CCM_KEY_SEARCH=$(tmux show-option -gqv @ccm-key-search 2>/dev/null)

CCM_KEY_DASHBOARD="${CCM_KEY_DASHBOARD:-Tab}"

# Temp dir setup command (used in run-shell for session detection)
_session_cmd='mkdir -p "${TMPDIR:-/tmp}/ccm-$(id -u)" && printf "#{session_name}" > "${TMPDIR:-/tmp}/ccm-$(id -u)/popup-session"'

# Coloured "ccm" badge for popup titles. Three pill cells in a
# muted traffic-light palette (rose / amber / sage) so each
# letter reads as a distinct mark while staying gentle on the
# eye. Each cell is 3 columns wide (" c " / " c " / " m ") with
# a black bold glyph centred on a coloured background.
# `#[default]` resets to the popup's normal title style for the
# suffix word. Truecolor terminals render exact hex; on 256-
# colour terminals tmux falls back to the nearest palette entry.
_logo='#[bg=#E89B9B,fg=#000000,bold] c #[bg=#E8C76A,fg=#000000,bold] c #[bg=#86C99B,fg=#000000,bold] m #[default]'

# Keybindings — only dashboard is bound by default (Tab rarely conflicts)
# Menu and tree are opt-in via @ccm-key-menu / @ccm-key-tree to avoid
# conflicts with other plugins (e.g., tmux-sessionist binds C).
tmux bind-key "$CCM_KEY_DASHBOARD" \
    run-shell "$_session_cmd" \\\; \
    display-popup -E -w 80% -h 60% -T " ${_logo} Dashboard " "$CCM_BIN dashboard"

if [[ -n "$CCM_KEY_MENU" ]]; then
    tmux bind-key "$CCM_KEY_MENU" \
        run-shell "$_session_cmd" \\\; \
        display-popup -E -w 80% -h 60% -T " ${_logo} Menu " "$CCM_BIN menu"
fi

if [[ -n "$CCM_KEY_TREE" ]]; then
    tmux bind-key "$CCM_KEY_TREE" \
        run-shell "$_session_cmd" \\\; \
        display-popup -E -w 80% -h 60% -T " ${_logo} Tree " "$CCM_BIN tree-interactive"
fi

if [[ -n "$CCM_KEY_SEARCH" ]]; then
    tmux bind-key "$CCM_KEY_SEARCH" \
        run-shell "$_session_cmd" \\\; \
        display-popup -E -w 80% -h 60% -T " ${_logo} Filter " "$CCM_BIN search"
fi

# Optional prefix-less dashboard hotkey. Set
# `@ccm-key-dashboard-noprefix "F1"` (or any tmux key) in
# ~/.tmux.conf to bind the dashboard popup directly, without the
# tmux prefix. Useful for users who want a top-row function key
# to toggle ccm. Goes through the same display-popup invocation
# as the prefix binding so the coloured logo title is preserved.
CCM_KEY_DASHBOARD_NOPREFIX=$(tmux show-option -gqv @ccm-key-dashboard-noprefix 2>/dev/null)
if [[ -n "$CCM_KEY_DASHBOARD_NOPREFIX" ]]; then
    tmux bind-key -n "$CCM_KEY_DASHBOARD_NOPREFIX" \
        run-shell "$_session_cmd" \\\; \
        display-popup -E -w 80% -h 60% -T " ${_logo} Dashboard " "$CCM_BIN dashboard"
fi

# Mouse click on ccm status icon → open dashboard
# Falls back to default behavior (select-window) for non-ccm clicks
tmux bind-key -n MouseDown1Status \
    if-shell -F '#{==:#{mouse_status_range},ccm}' \
    "run-shell '$_session_cmd' ; display-popup -E -w 80% -h 60% -T ' ${_logo} Dashboard ' '$CCM_BIN dashboard'" \
    "switch-client -t ="

# Reduce Claude Code UI flicker in tmux (alt-screen rendering)
# ccm's capture-pane handles both normal and alternate screen modes
tmux set-environment -g CLAUDE_CODE_NO_FLICKER 1

# Status-interval drives how often tmux invokes `ccm inject-status`.
# 5 seconds keeps the steady-state CPU cost negligible (each
# invocation is ~150-200ms, so 5 s polling is ~3-4% of one core).
# The PERMIT-axis instant path bypasses polling entirely: hooks
# write @ccm-permit-pending which inject-status promotes on the
# next tick, so the actionable "user must approve a tool" signal
# surfaces within ~hook latency, not within status-interval.
# Override CCM_STATUS_INTERVAL in your tmux env to tune (e.g.
# `tmux set-environment -g CCM_STATUS_INTERVAL 10`).
_target_interval=$(tmux show-environment -g CCM_STATUS_INTERVAL 2>/dev/null | sed -n 's/^CCM_STATUS_INTERVAL=//p')
[[ -z "$_target_interval" ]] && _target_interval=5
_current_interval=$(tmux show-option -gv status-interval 2>/dev/null || echo 15)
if [[ "$_current_interval" -gt "$_target_interval" ]] 2>/dev/null; then
    tmux set -g status-interval "$_target_interval"
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

# Re-inject status on client attach — theme plugins may overwrite status-right
# on reattach, removing the #(ccm inject-status) periodic trigger.
# The delay lets the theme finish re-rendering before ccm re-injects.
tmux set-hook -g client-attached "run-shell -b 'sleep 1 && $CCM_BIN inject-status 2>/dev/null || true'"

# Reflect the focused project in the status bar immediately on a
# window switch, instead of waiting up to status-interval for the
# next tick. The mode-1/2 status bakes the "current window"
# highlight into a static status string when inject-status runs, so
# the highlight only moves when inject-status re-runs; a bare
# `refresh-client -S` redraws the cached `#(...)` output and does
# not move it. `session-window-changed` fires whenever the active
# window changes; re-running inject-status there rewrites the
# highlight. `--fast` is the point: the per-project states are
# already cached in `@ccm_prev_state` and a window switch changes
# only WHICH window is current, so the focus refresh skips the full
# detection pass (~250 ms) and re-renders from the cache (~10 ms),
# making the highlight feel instant — exactly the "the info is
# already known, reflect it now" case. The next regular poll tick
# re-detects. `-ga` appends so a theme/user hook on the same event
# is preserved; `-b` keeps the switch itself snappy and the inject
# lockfile prevents pile-up during rapid switching.
#
# Append-once guard: re-sourcing .tmux.conf (or reloading TPM) re-runs
# this script, and a blind `-ga` stacks an identical hook per reload —
# observed live with two copies firing a double --fast
# render on every switch. Match on the distinctive command substring
# rather than the full string so an install whose path changed (e.g.
# a symlinked plugin dir) still counts as "already registered" instead
# of accumulating a second variant.
if ! tmux show-hooks -g 2>/dev/null | grep "session-window-changed" | grep -q "inject-status --fast"; then
    tmux set-hook -ga session-window-changed "run-shell -b '$CCM_BIN inject-status --fast 2>/dev/null || true'"
fi

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
