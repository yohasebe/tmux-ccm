#!/usr/bin/env bash
# Start an isolated demo tmux server with 4 projects for GIF recording.
# Uses a separate tmux server (-L ccm-demo) so existing projects don't leak in.
#
# Usage: ./scripts/start-demo-session.sh
# Attach: tmux -L ccm-demo attach
# Cleanup: tmux -L ccm-demo kill-server

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CCM_ROOT="$(dirname "$SCRIPT_DIR")"
CCM_BIN="${CCM_ROOT}/ccm"
DEMO_DIR="$(dirname "$CCM_ROOT")/ccm-demo"
SERVER="ccm-demo"
SESSION="work"

if [[ ! -d "$DEMO_DIR" ]]; then
    echo "Demo projects not found. Run: ./scripts/setup-demo.sh"
    exit 1
fi

# Clean up existing demo server
tmux -L "$SERVER" kill-server 2>/dev/null || true
sleep 0.5

# Clear Claude Code session history for demo projects (fresh start)
rm -rf "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/"*ccm-demo* 2>/dev/null

# Temporarily hide _autosave to prevent auto-restore
AUTOSAVE="$HOME/.local/share/ccm/snapshots/_autosave.json"
AUTOSAVE_BAK="${AUTOSAVE}.demo-bak"
if [[ -f "$AUTOSAVE" ]]; then
    mv "$AUTOSAVE" "$AUTOSAVE_BAK"
fi

# Create isolated server + session
tmux -L "$SERVER" new-session -d -s "$SESSION" -x 120 -y 32

# Load ccm plugin (now safe — no _autosave to restore)
tmux -L "$SERVER" run-shell "$CCM_ROOT/ccm.tmux"
sleep 3

# Restore _autosave
if [[ -f "$AUTOSAVE_BAK" ]]; then
    mv "$AUTOSAVE_BAK" "$AUTOSAVE"
fi

# Add demo projects via the demo server
export TMUX="$(tmux -L "$SERVER" display -p '#{socket_path}'),$(tmux -L "$SERVER" display -p '#{pid}'),0"
"$CCM_BIN" add "$DEMO_DIR/auth-service" auth-service
sleep 0.5
"$CCM_BIN" add "$DEMO_DIR/dashboard-ui" dashboard-ui
sleep 0.5
"$CCM_BIN" add "$DEMO_DIR/data-pipeline" data-pipeline
sleep 0.5
"$CCM_BIN" add "$DEMO_DIR/sdk-python" sdk-python
unset TMUX
sleep 1

echo ""
echo "=== Demo server ready ==="
echo ""
echo "Attach with:"
echo "  tmux -L $SERVER attach"
echo ""
echo "Projects: auth-service, dashboard-ui, data-pipeline, sdk-python"
echo ""
echo "Cleanup after recording:"
echo "  tmux -L $SERVER kill-server"
