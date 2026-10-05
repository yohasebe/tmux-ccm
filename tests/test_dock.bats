#!/usr/bin/env bats
load helpers/tmux_guard.bash
# The docked dashboard on a real, isolated tmux server: it opens as a
# full-width pane, follows a window switch through the installed hook,
# and every window it leaves gets its exact layout back.

setup() {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_DATA_DIR="$BATS_TEST_TMPDIR/data" CCM_TMP_DIR="$BATS_TEST_TMPDIR/runtime"
    export CCM_DOCK_COMMAND="sleep 300"
    mkdir -p "$CCM_DATA_DIR" "$CCM_TMP_DIR" "$BATS_TEST_TMPDIR/bin"
    export CCM_TEST_SOCKET
    CCM_TEST_SOCKET=$(ccm_test_new_socket)
    # The server's own children (hooks) call plain tmux: the shim comes
    # first on PATH and reaches the guard by path.
    cat > "$BATS_TEST_TMPDIR/bin/tmux" <<SHIM
#!/usr/bin/env bash
exec "$CCM_TEST_GUARD_DIR/bin/tmux" -L "\$CCM_TEST_SOCKET" -f /dev/null "\$@"
SHIM
    chmod +x "$BATS_TEST_TMPDIR/bin/tmux"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    tmux new-session -d -s w -x 120 -y 40
    tmux split-window -h -t w:0
    tmux new-window -t w
    tmux split-window -v -t w:1
    tmux set -g @ccm-dashboard-dock top
    W0=$(tmux display -p -t w:0 '#{window_id}')
    W1=$(tmux display -p -t w:1 '#{window_id}')
    L0=$(tmux display -p -t w:0 '#{window_layout}')
    L1=$(tmux display -p -t w:1 '#{window_layout}')
}

teardown() {
    tmux kill-server 2>/dev/null || true
}

dock_window() {
    tmux list-panes -a -F '#{window_id} #{@ccm_dock}' | awk '$2 == "1" {print $1}'
}

@test "toggle opens a full-width top pane and toggling again restores the layout" {
    python3 "$CCM_ROOT/lib/ccm_dock.py" toggle "$W0"
    [[ "$(dock_window)" == "$W0" ]]
    run tmux list-panes -t "$W0" -F '#{@ccm_dock} #{pane_top} #{pane_width} #{pane_height}'
    [[ "$output" == *"1 0 120 16"* ]]
    python3 "$CCM_ROOT/lib/ccm_dock.py" toggle "$W0"
    [[ -z "$(dock_window)" ]]
    [[ "$(tmux display -p -t w:0 '#{window_layout}')" == "$L0" ]]
}

@test "the installed hook moves the dock with a window switch and restores each window" {
    bash "$CCM_ROOT/ccm.tmux" >/dev/null 2>&1
    tmux select-window -t w:0
    python3 "$CCM_ROOT/lib/ccm_dock.py" toggle "$W0"
    # The pane moves first and the window it left is re-laid just after,
    # so wait for both.
    tmux select-window -t w:1
    for _ in $(seq 1 50); do
        [[ "$(dock_window)" == "$W1" && "$(tmux display -p -t w:0 '#{window_layout}')" == "$L0" ]] && break
        sleep 0.1
    done
    [[ "$(dock_window)" == "$W1" ]]
    [[ "$(tmux display -p -t w:0 '#{window_layout}')" == "$L0" ]]
    tmux select-window -t w:0
    for _ in $(seq 1 50); do
        [[ "$(dock_window)" == "$W0" && "$(tmux display -p -t w:1 '#{window_layout}')" == "$L1" ]] && break
        sleep 0.1
    done
    [[ "$(dock_window)" == "$W0" ]]
    [[ "$(tmux display -p -t w:1 '#{window_layout}')" == "$L1" ]]
}

@test "with docking off the hook leaves windows alone" {
    bash "$CCM_ROOT/ccm.tmux" >/dev/null 2>&1
    python3 "$CCM_ROOT/lib/ccm_dock.py" toggle "$W0"
    tmux set -g @ccm-dashboard-dock off
    tmux select-window -t w:1
    sleep 1
    [[ "$(dock_window)" == "$W0" ]]
}

@test "an uneven split comes back exactly after the dock opens and closes" {
    tmux new-window -t w
    tmux split-window -v -l 10 -t w:2
    W2=$(tmux display -p -t w:2 '#{window_id}')
    L2=$(tmux display -p -t w:2 '#{window_layout}')
    python3 "$CCM_ROOT/lib/ccm_dock.py" toggle "$W2"
    python3 "$CCM_ROOT/lib/ccm_dock.py" toggle "$W2"
    [[ "$(tmux display -p -t w:2 '#{window_layout}')" == "$L2" ]]
}

@test "concurrent toggles are serialized and never leave two docks" {
    local i
    for i in 1 2 3 4 5; do
        python3 "$CCM_ROOT/lib/ccm_dock.py" toggle "$W0" &
    done
    wait
    # Five toggles in turn end open: exactly one dock.
    [[ "$(tmux list-panes -a -F '#{@ccm_dock}' | grep -c '^1$')" -eq 1 ]]
}

@test "a dock marked as closing stays put on a window switch, so the new window is laid out once" {
    bash "$CCM_ROOT/ccm.tmux" >/dev/null 2>&1
    tmux select-window -t w:0
    python3 "$CCM_ROOT/lib/ccm_dock.py" toggle "$W0"
    pane=$(tmux list-panes -a -F '#{pane_id} #{@ccm_dock}' | awk '$2 == "1" {print $1}')
    python3 -c "import sys; sys.path.insert(0, '$CCM_ROOT/lib'); import ccm_dock; ccm_dock.mark_leaving('$pane')"
    tmux select-window -t w:1
    sleep 1
    [[ "$(dock_window)" == "$W0" ]]
    [[ "$(tmux display -p -t w:1 '#{window_layout}')" == "$L1" ]]
    python3 "$CCM_ROOT/lib/ccm_dock.py" close
    [[ -z "$(dock_window)" ]]
    [[ "$(tmux display -p -t w:0 '#{window_layout}')" == "$L0" ]]
}
