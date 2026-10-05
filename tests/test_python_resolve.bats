#!/usr/bin/env bats
load helpers/tmux_guard.bash
# ccm resolves `python3` through PATH once and runs the real interpreter
# from then on, so a version manager's shim is not paid on every start.

setup() {
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_TMP_DIR="$BATS_TEST_TMPDIR/tmp" CCM_DATA_DIR="$BATS_TEST_TMPDIR/data"
    BIN="$BATS_TEST_TMPDIR/bin"
    mkdir -p "$BIN"
    REAL="$BATS_TEST_TMPDIR/real-python"
    printf '#!/bin/sh\necho real\n' > "$REAL"
    chmod +x "$REAL"
    # A shim: records that it ran, and reports the real interpreter.
    printf '#!/bin/sh\necho ran >> "%s/shim-runs"\necho "%s"\n' "$BATS_TEST_TMPDIR" "$REAL" > "$BIN/python3"
    chmod +x "$BIN/python3"
    export PATH="$BIN:$PATH"
}

resolve() {
    bash -c "source '$CCM_ROOT/lib/common.sh'; ccm_init_dirs; ccm_resolve_python; echo \"\$CCM_PYTHON\""
}

shim_runs() { wc -l < "$BATS_TEST_TMPDIR/shim-runs" 2>/dev/null | tr -d ' ' || echo 0; }

@test "the real interpreter is resolved once and then run directly" {
    run resolve
    [ "$output" = "$REAL" ]
    run resolve
    [ "$output" = "$REAL" ]
    [ "$(shim_runs)" = 1 ]
    [ "$(cat "$CCM_TMP_DIR/python")" = "$REAL" ]
}

@test "a cached interpreter that no longer runs is resolved again" {
    mkdir -p "$CCM_TMP_DIR"; chmod 700 "$CCM_TMP_DIR"
    echo "$BATS_TEST_TMPDIR/gone/python3" > "$CCM_TMP_DIR/python"
    run resolve
    [ "$output" = "$REAL" ]
    [ "$(shim_runs)" = 1 ]
}

@test "a symlinked cache is not read" {
    mkdir -p "$CCM_TMP_DIR"; chmod 700 "$CCM_TMP_DIR"
    printf '#!/bin/sh\necho planted\n' > "$BATS_TEST_TMPDIR/planted"; chmod +x "$BATS_TEST_TMPDIR/planted"
    echo "$BATS_TEST_TMPDIR/planted" > "$BATS_TEST_TMPDIR/list"
    ln -s "$BATS_TEST_TMPDIR/list" "$CCM_TMP_DIR/python"
    run resolve
    [ "$output" = "$REAL" ]
}

@test "an answer that is not an absolute path to a program falls back to python3" {
    printf '#!/bin/sh\necho not-a-path\n' > "$BIN/python3"
    run resolve
    [ "$output" = python3 ]
    [ ! -e "$CCM_TMP_DIR/python" ]
}

@test "the interpreter is never taken from the environment" {
    run env CCM_PYTHON=/bin/echo bash -c "source '$CCM_ROOT/lib/common.sh'; echo \"\$CCM_PYTHON\""
    [ "$output" = python3 ]
}

@test "the ccm command runs the resolved interpreter" {
    run "$CCM_ROOT/ccm" list
    [ "$output" = real ]
}

@test "a cache that cannot be kept does not stop the command" {
    run bash -c "set -euo pipefail; source '$CCM_ROOT/lib/common.sh'; ccm_init_dirs
        mv() { return 73; }
        ccm_resolve_python; echo \"reached \$CCM_PYTHON\""
    [ "$status" -eq 0 ]
    [ "$output" = "reached $REAL" ]
    [ -z "$(ls "$CCM_TMP_DIR" | grep '^python')" ]
}

@test "a cached directory is not taken for the interpreter" {
    mkdir -p "$CCM_TMP_DIR"; chmod 700 "$CCM_TMP_DIR"
    echo "$BATS_TEST_TMPDIR" > "$CCM_TMP_DIR/python"
    run resolve
    [ "$output" = "$REAL" ]
}
