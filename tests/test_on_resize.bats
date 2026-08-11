#!/usr/bin/env bats
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
    RENDERS="${MOCK_DIR}/renders"

    # Stand-in for `ccm`, recording one line per render.
    export CCM_BIN="${MOCK_DIR}/ccm"
    cat > "$CCM_BIN" <<EOF
#!/bin/sh
echo "\$@" >> "${RENDERS}"
EOF
    chmod +x "$CCM_BIN"
}

teardown() {
    rm -rf "$MOCK_DIR"
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
    for _ in $(seq 1 15); do
        "${CCM_ROOT}/lib/on-resize.sh" &
        sleep 0.02
    done
    wait
    [[ "$(renders)" -eq 1 ]] \
        || { echo "a 15-step drag rendered $(renders) times"; return 1; }
}

@test "resizes further apart than the settle window each render" {
    "${CCM_ROOT}/lib/on-resize.sh"
    "${CCM_ROOT}/lib/on-resize.sh"
    [[ "$(renders)" -eq 2 ]] || { echo "got $(renders)"; return 1; }
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
    start=$(python3 -c 'import time; print(time.time())')
    "${CCM_ROOT}/lib/on-resize.sh"
    end=$(python3 -c 'import time; print(time.time())')
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

@test "a missing ccm binary is not a crash" {
    export CCM_BIN="${MOCK_DIR}/absent"
    run "${CCM_ROOT}/lib/on-resize.sh"
    [[ "$status" -eq 0 ]] || { echo "exited $status"; return 1; }
}
