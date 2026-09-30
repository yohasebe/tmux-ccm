#!/usr/bin/env bats
load helpers/tmux_guard.bash

setup() {
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_DATA_DIR="$BATS_TEST_TMPDIR/data"
    export CCM_SNAPSHOT_DIR="$BATS_TEST_TMPDIR/snapshots"
    export CCM_TMP_DIR="$BATS_TEST_TMPDIR/runtime"
    mkdir -p "$CCM_TMP_DIR"
}

@test "prepare-logout help and flags reach Python" {
    run bash "$CCM_ROOT/ccm" prepare-logout --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--cancel"* ]]
    [[ "$output" == *"--yes"* ]]
    run bash "$CCM_ROOT/ccm" prepare-logout --cancel -y
    [ "$status" -ne 0 ]
    [[ "$output" == *"not allowed"* ]]
}

@test "prepare-logout captures real isolated tmux splits and preserves its seal" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    local sock; sock=$(ccm_test_new_socket)
    tmux() { command tmux -L "$sock" -f /dev/null "$@"; }
    tmux new-session -d -x 160 -y 50 -s capture /bin/sh
    tmux set-option -w -t capture:0 @ccm_project alpha
    tmux set-option -w -t capture:0 @ccm_dir "$BATS_TEST_TMPDIR"
    tmux split-window -h -t capture:0 -c "$BATS_TEST_TMPDIR" /bin/sh
    tmux split-window -v -t capture:0 -c "$BATS_TEST_TMPDIR" /bin/sh
    tmux set-option -p -t capture:0.2 @ccm_ignore 1
    tmux resize-pane -Z -t capture:0.2
    local bin="$BATS_TEST_TMPDIR/bin"
    mkdir -p "$bin"
    # Python calls still pass the guard, with this test's allocated socket.
    cat > "$bin/tmux" <<SHIM
#!/usr/bin/env bash
PATH="$PATH" exec tmux -L "$sock" -f /dev/null "\$@"
SHIM
    chmod +x "$bin/tmux"
    run env PATH="$bin:$PATH" bash "$CCM_ROOT/ccm" prepare-logout
    [ "$status" -eq 0 ]
    [[ "$output" == *"saved and protected"* ]]
    run python3 "$CCM_ROOT/tests/fixtures/snapshot/check-capture.py" "$CCM_SNAPSHOT_DIR/_autosave.json"
    [ "$status" -eq 0 ]
    cp "$CCM_SNAPSHOT_DIR/_autosave.json" "$BATS_TEST_TMPDIR/sealed.json"
    tmux kill-pane -t capture:0.2
    run env PATH="$bin:$PATH" bash "$CCM_ROOT/ccm" snapshot save _autosave
    [ "$status" -eq 0 ]
    cmp "$BATS_TEST_TMPDIR/sealed.json" "$CCM_SNAPSHOT_DIR/_autosave.json"
    run env PATH="$bin:$PATH" bash "$CCM_ROOT/ccm" prepare-logout --cancel
    [ "$status" -eq 0 ]
    tmux kill-server
}
