#!/usr/bin/env bash
# Record ccm demo GIF using asciinema + agg.
# Usage: ./scripts/record-demo.sh
#
# This script:
# 1. Creates a fresh tmux session "demo"
# 2. Adds demo projects via ccm
# 3. Runs a scripted demo with tmux send-keys
# 4. Records the session with asciinema
# 5. Converts to GIF with agg
#
# Prerequisites: asciinema, agg, tmux, ccm
# Run from a terminal OUTSIDE of tmux.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CCM_ROOT="$(dirname "$SCRIPT_DIR")"
CCM_BIN="${CCM_ROOT}/ccm"
DEMO_DIR="$(dirname "$CCM_ROOT")/ccm-demo"
CAST_FILE="/tmp/ccm-demo.cast"
GIF_FILE="${CCM_ROOT}/assets/demo.gif"
SESSION="demo"

# Check prerequisites
for cmd in asciinema agg tmux; do
    command -v "$cmd" >/dev/null || { echo "Missing: $cmd"; exit 1; }
done

if [[ ! -d "$DEMO_DIR" ]]; then
    echo "Demo projects not found. Run: ./scripts/setup-demo.sh"
    exit 1
fi

# Clean up any existing demo session
tmux kill-session -t "$SESSION" 2>/dev/null || true
sleep 0.5

echo "=== ccm Demo Recorder ==="
echo ""
echo "Output: $GIF_FILE"
echo ""

# Create orchestration script (runs in background during recording)
ORCH_SCRIPT="/tmp/ccm-demo-orchestrate.sh"
cat > "$ORCH_SCRIPT" << ORCHESTRATE
#!/usr/bin/env bash
SESSION="$SESSION"
CCM_BIN="$CCM_BIN"
DEMO_DIR="$DEMO_DIR"

sleep 2

# Add projects
"\$CCM_BIN" add "\$DEMO_DIR/auth-service" auth-service 2>/dev/null
sleep 0.5
"\$CCM_BIN" add "\$DEMO_DIR/dashboard-ui" dashboard-ui 2>/dev/null
sleep 0.5
"\$CCM_BIN" add "\$DEMO_DIR/data-pipeline" data-pipeline 2>/dev/null
sleep 0.5
"\$CCM_BIN" add "\$DEMO_DIR/sdk-python" sdk-python 2>/dev/null
sleep 1

# Show status
tmux send-keys -t "\$SESSION:1" "\$CCM_BIN status" Enter
sleep 3

# Open dashboard
tmux send-keys -t "\$SESSION:1" "\$CCM_BIN dashboard" Enter
sleep 3

# Navigate down
tmux send-keys -t "\$SESSION:1" "j"
sleep 1
tmux send-keys -t "\$SESSION:1" "j"
sleep 1
tmux send-keys -t "\$SESSION:1" "j"
sleep 1

# Navigate up
tmux send-keys -t "\$SESSION:1" "k"
sleep 1
tmux send-keys -t "\$SESSION:1" "k"
sleep 1

# Close dashboard
tmux send-keys -t "\$SESSION:1" "q"
sleep 2

# Exit
tmux send-keys -t "\$SESSION:1" "exit" Enter
sleep 1
ORCHESTRATE
chmod +x "$ORCH_SCRIPT"

echo "Starting recording... (Ctrl+C to abort)"
echo ""

# Create tmux session and start recording
tmux new-session -d -s "$SESSION" -x 120 -y 30
bash "$ORCH_SCRIPT" &
ORCH_PID=$!

asciinema rec "$CAST_FILE" --overwrite --cols 120 --rows 30 -c "tmux attach -t $SESSION"

wait $ORCH_PID 2>/dev/null || true
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Convert to GIF
echo ""
echo "Converting to GIF..."
agg --cols 120 --rows 30 --speed 1.5 --theme monokai "$CAST_FILE" "$GIF_FILE"

echo ""
echo "Done! Output: $GIF_FILE"
echo "Size: $(du -h "$GIF_FILE" | cut -f1)"
