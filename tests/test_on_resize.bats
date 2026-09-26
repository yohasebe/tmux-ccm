#!/usr/bin/env bats
load helpers/tmux_guard.bash
# Tests for lib/on-resize.sh — the settle window that turns one resize
# gesture into one status-bar render.
#
# tmux fires `client-resized` for every step of a drag. Rendering per
# step would start a python process per step, which is the shape of
# per-tick subprocess spawning the poll path was rate-limited to avoid.

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

setup() {
    MOCK_DIR="$(mktemp -d)"
    export CCM_TMP_DIR="${MOCK_DIR}/tmp"
    export CCM_RESIZE_SETTLE=0.2
    export RENDERS="${MOCK_DIR}/renders"
    RESIZE_JOBS=()
    RESIZE_IDS=()

    # Stand-in for `ccm`, recording one line per render.
    export CCM_BIN="${MOCK_DIR}/ccm"
    cat > "$CCM_BIN" <<EOF
#!/bin/sh
echo "\$@" >> "${RENDERS}"
EOF
    chmod +x "$CCM_BIN"
}

teardown() {
    local id pid failed=0
    # Release every gate even when an assertion stopped the test early.
    for id in "${RESIZE_IDS[@]}"; do
        touch "$RESIZE_GATE_ROOT/$id.release"
    done
    for pid in "${RESIZE_JOBS[@]}"; do
        if ! wait "$pid"; then
            echo "resize child $pid failed during cleanup"
            failed=1
        fi
    done
    if [[ -n "${RESIZE_GATE_ROOT:-}" ]]; then
        for id in "${RESIZE_IDS[@]}"; do
            if [[ -e "$RESIZE_GATE_ROOT/$id.timed-out" ]]; then
                echo "resize gate $id timed out"
                failed=1
            fi
        done
    fi
    rm -rf "$MOCK_DIR"
    return "$failed"
}

_use_resize_gates() {
    export RESIZE_REAL_SLEEP
    RESIZE_REAL_SLEEP=$(type -P sleep)
    export RESIZE_GATE_ROOT="$MOCK_DIR/gates"
    mkdir -p "$RESIZE_GATE_ROOT" "$MOCK_DIR/bin"
    cp "$BATS_TEST_DIRNAME/helpers/resize_sleep.bash" "$MOCK_DIR/bin/sleep"
    chmod +x "$MOCK_DIR/bin/sleep"
    export PATH="$MOCK_DIR/bin:$PATH"
    cat > "$CCM_BIN" <<'MOCK'
#!/usr/bin/env bash
printf '%s %s\n' "$RESIZE_EVENT_ID" "$*" >> "$RENDERS"
MOCK
}

_start_resize() {
    local id="$1" attempt
    RESIZE_EVENT_ID="$id" "${CCM_ROOT}/lib/on-resize.sh" &
    RESIZE_JOBS+=("$!")
    RESIZE_IDS+=("$id")
    # Arrival is after the stamp write. Wait before launching the next
    # event so launch order and stamp order cannot diverge.
    for ((attempt=0; attempt<500; attempt++)); do
        if [[ -f "$RESIZE_GATE_ROOT/$id.ready" ]]; then
            [[ "$(cat "$RESIZE_GATE_ROOT/$id.request")" == "$CCM_RESIZE_SETTLE" ]]
            return
        fi
        "$RESIZE_REAL_SLEEP" 0.02
    done
    echo "resize event $id did not reach ready gate"
    return 1
}

_release_resize() {
    local index="$1" id pid
    id="${RESIZE_IDS[$index]}"
    pid="${RESIZE_JOBS[$index]}"
    touch "$RESIZE_GATE_ROOT/$id.release"
    if ! wait "$pid"; then
        echo "resize event $id exited unsuccessfully"
        return 1
    fi
    # The product does not check sleep's exit code, so a gate timeout
    # must be checked independently of the handler's final exit code.
    [[ ! -e "$RESIZE_GATE_ROOT/$id.timed-out" ]] || {
        echo "resize gate $id timed out"; return 1;
    }
}

_assert_no_early_render() {
    local count
    count=$(renders)
    if [[ "$count" -ne 0 ]]; then
        if [[ "$count" -eq 1 ]]; then
            echo "render event mismatch: only event 15 may render; got $(cat "$RENDERS")"
        else
            echo "multiple early renders: expected 0, got $count"
        fi
        return 1
    fi
}

renders() {
    [ -f "$RENDERS" ] && wc -l < "$RENDERS" | tr -d ' ' || echo 0
}

@test "one resize renders once" {
    "${CCM_ROOT}/lib/on-resize.sh"
    [[ "$(renders)" -eq 1 ]] || { echo "got $(renders)"; return 1; }
}

@test "the render is the fast path" {
    "${CCM_ROOT}/lib/on-resize.sh"
    grep -q -- "inject-status --fast" "$RENDERS" \
        || { echo "rendered as: $(cat "$RENDERS")"; return 1; }
}

@test "a burst of resizes renders once, not once per event" {
    _use_resize_gates
    local id index
    for id in $(seq 1 15); do
        _start_resize "$id"
    done
    _assert_no_early_render
    for index in $(seq 0 13); do
        _release_resize "$index"
    done
    _assert_no_early_render
    _release_resize 14
    [[ "$(renders)" -eq 1 ]] || {
        echo "expected one final render, got $(renders)"; return 1;
    }
    [[ "$(cat "$RENDERS")" == "15 inject-status --fast" ]] || {
        echo "render event mismatch: expected 15 inject-status --fast, got $(cat "$RENDERS")"
        return 1
    }
}

@test "resizes after the previous settle completes each render" {
    _use_resize_gates
    _start_resize 1
    _release_resize 0
    [[ "$(cat "$RENDERS")" == "1 inject-status --fast" ]]
    _start_resize 2
    [[ "$(renders)" -eq 1 ]]
    _release_resize 1
    [[ "$(cat "$RENDERS")" == $'1 inject-status --fast\n2 inject-status --fast' ]]
}

@test "the render waits for the resizing to settle" {
    # The window is what makes it a trailing render: the surviving
    # invocation is the last one, so the bar is laid out for the size
    # the drag ended on rather than a size it passed through.
    #
    # Asserted as elapsed time, not by watching for the render to be
    # absent early on: that reading depends on how fast a process
    # starts, which is a property of the machine and not of the code.
    # Timed with a real clock rather than bash's `SECONDS`, which
    # counts second boundaries crossed: assigned at .95 past, it reads
    # 1 a few milliseconds later, so `>= 1` is true even with no wait
    # at all. That is how the first version of this test passed
    # against a build with the wait removed.
    export CCM_RESIZE_SETTLE=1
    local start end
    start=$(python3 -c 'import time; print(time.monotonic())')
    "${CCM_ROOT}/lib/on-resize.sh"
    end=$(python3 -c 'import time; print(time.monotonic())')
    python3 -c "import sys; sys.exit(0 if $end - $start >= 0.5 else 1)" \
        || { echo "returned in $(python3 -c "print(round($end-$start,3))")s,"\
                  "without waiting"; return 1; }
    [[ "$(renders)" -eq 1 ]] || { echo "got $(renders)"; return 1; }
}

@test "an unwritable state directory is not a crash" {
    export CCM_TMP_DIR=/proc/nonexistent/ccm
    run "${CCM_ROOT}/lib/on-resize.sh"
    [[ "$status" -eq 0 ]] || { echo "exited $status"; return 1; }
}

# The script falls back to the `ccm` beside it when CCM_BIN is not
# usable. Run from the repository that is the real ccm, which goes on
# to redraw the status bar of whatever tmux server the person running
# the tests is in. So both cases run a copy of the script from a
# directory of their own, where what sits beside it is theirs to say.
_isolated_script() {
    mkdir -p "${MOCK_DIR}/plugin/lib"
    cp "${CCM_ROOT}/lib/on-resize.sh" "${MOCK_DIR}/plugin/lib/on-resize.sh"
    echo "${MOCK_DIR}/plugin/lib/on-resize.sh"
}

@test "a missing ccm binary is not a crash" {
    local script; script=$(_isolated_script)
    export CCM_BIN="${MOCK_DIR}/absent"          # and nothing beside the script either
    run "$script"
    [[ "$status" -eq 0 ]] || { echo "exited $status"; return 1; }
    [[ "$(renders)" -eq 0 ]]
}

@test "without a usable CCM_BIN, the ccm beside the script renders" {
    local script; script=$(_isolated_script)
    printf '#!/usr/bin/env bash\necho "beside $@" >> "%s"\n' "$RENDERS" > "${MOCK_DIR}/plugin/ccm"
    chmod +x "${MOCK_DIR}/plugin/ccm"
    export CCM_BIN="${MOCK_DIR}/absent"
    run "$script"
    [[ "$status" -eq 0 ]]
    grep -q "^beside inject-status --fast$" "$RENDERS"
}
