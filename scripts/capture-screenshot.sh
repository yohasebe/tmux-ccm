#!/usr/bin/env bash
# Capture ccm dashboard and status command as SVG/PNG screenshots.
#
# Prerequisites:
#   - ./scripts/setup-screenshot.sh has been run
#   - freeze is installed: brew install charmbracelet/tap/freeze
#
# Usage: ./scripts/capture-screenshot.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CCM_ROOT="$(dirname "$SCRIPT_DIR")"
CCM_BIN="${CCM_ROOT}/ccm"
SOCKET="ccm-ss"
SESSION="work"
OUT_DIR="${CCM_ROOT}/assets"

T() { tmux -L "$SOCKET" "$@"; }

if ! T has-session -t "$SESSION" 2>/dev/null; then
    echo "Error: Run ./scripts/setup-screenshot.sh first."
    exit 1
fi

mkdir -p "$OUT_DIR"

HAS_FREEZE=false
command -v freeze >/dev/null 2>&1 && HAS_FREEZE=true

save_capture() {
    local name="$1" ansi_file="$2"
    if $HAS_FREEZE; then
        freeze --language ansi --theme dracula \
            --font.family "HackGen Console NF" \
            --window --border.radius 8 \
            --margin 0 --padding 20 \
            --output "${OUT_DIR}/${name}.svg" "$ansi_file"
        echo "  Saved: ${OUT_DIR}/${name}.svg"
        freeze --language ansi --theme dracula \
            --font.family "HackGen Console NF" \
            --font.file "$HOME/Library/Fonts/HackGenConsoleNF-Regular.ttf" \
            --window --border.radius 8 \
            --margin 0 --padding 20 \
            --output "${OUT_DIR}/${name}.png" "$ansi_file"
        echo "  Saved: ${OUT_DIR}/${name}.png"
    else
        cp "$ansi_file" "${OUT_DIR}/${name}.ansi"
        echo "  Saved: ${OUT_DIR}/${name}.ansi (install freeze for SVG/PNG)"
    fi
}

new_capture_win() {
    local win_idx
    win_idx=$(T new-window -P -F "#{window_index}" -t "${SESSION}:" -n "capture")
    local target="${SESSION}:${win_idx}"
    T send-keys -t "$target" "export CCM_MOCK_STATE=1" Enter
    sleep 0.3
    echo "$target"
}

# ── 1. Dashboard ──

echo "Capturing dashboard..."
DASH=$(new_capture_win)
T send-keys -t "$DASH" "ccm dashboard" Enter
sleep 3

T capture-pane -e -p -t "$DASH" > ${TMPDIR:-/tmp}/ccm-ss-dashboard.ansi
T send-keys -t "$DASH" "q"
sleep 0.5
T kill-window -t "$DASH" 2>/dev/null || true
sleep 0.3

save_capture "dashboard" ${TMPDIR:-/tmp}/ccm-ss-dashboard.ansi

# ── 2. ccm status ──

echo "Capturing 'ccm status'..."
STATUS=$(new_capture_win)
T send-keys -t "$STATUS" "clear" Enter
sleep 0.3
T send-keys -t "$STATUS" "ccm status" Enter
sleep 2

T capture-pane -e -p -t "$STATUS" > ${TMPDIR:-/tmp}/ccm-ss-status.ansi
T kill-window -t "$STATUS" 2>/dev/null || true

save_capture "status-cmd" ${TMPDIR:-/tmp}/ccm-ss-status.ansi

echo ""
echo "Screenshots saved to: $OUT_DIR/"
