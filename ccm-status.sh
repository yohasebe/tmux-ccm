#!/usr/bin/env bash
# ccm - status bar setup (run after TPM completes)
# Window-based architecture

CCM="$HOME/Dropbox/code/ccm/ccm"

# Inject ccm status into status-right (suppress errors)
"$CCM" inject-status 2>/dev/null || true

# Hooks for immediate refresh on window events (all errors suppressed)
tmux set-hook -g window-linked "run-shell -b '$CCM inject-status 2>/dev/null || true'"
tmux set-hook -g window-unlinked "run-shell -b '$CCM inject-status 2>/dev/null || true'"
tmux set-hook -g window-renamed "run-shell -b '$CCM inject-status 2>/dev/null || true'"
tmux set-hook -g client-session-changed "run-shell -b '$CCM clear-done 2>/dev/null; $CCM inject-status 2>/dev/null || true'"
tmux set-hook -g session-window-changed "run-shell -b '$CCM clear-done 2>/dev/null; $CCM inject-status 2>/dev/null || true'"

tmux set -g status-interval 2
tmux set -g status-right-length 200

# Hide window name/status
tmux set -g window-status-format ''
tmux set -g window-status-current-format ''
