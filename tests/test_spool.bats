#!/usr/bin/env bats
# Dispatch-level and filesystem end-to-end tests for `ccm spool` —
# the CLI is tmux-free (queue inspection / withdrawal), so these run
# the real bash → python path against an isolated data dir. Delivery
# itself is covered by tests/test_spool.py (pytest).

setup() {
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_TMP_DIR="$(mktemp -d)"
    export CCM_DATA_DIR="$(mktemp -d)"
}

teardown() {
    rm -rf "$CCM_TMP_DIR" "$CCM_DATA_DIR"
}

@test "spool: --help prints usage" {
    run bash "$CCM_ROOT/ccm" spool --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage: ccm spool"* ]]
}

@test "spool: empty queue lists cleanly" {
    run bash "$CCM_ROOT/ccm" spool list
    [ "$status" -eq 0 ]
    [[ "$output" == *"No queued messages"* ]]
}

@test "spool: a queued file is listed with its preview" {
    mkdir -p "$CCM_DATA_DIR/spool/demo"
    printf 'review the draft please\n' > "$CCM_DATA_DIR/spool/demo/1700000000000-tester.msg"
    run bash "$CCM_ROOT/ccm" spool list
    [ "$status" -eq 0 ]
    [[ "$output" == *"demo:"* ]]
    [[ "$output" == *"1700000000000-tester"* ]]
    [[ "$output" == *"review the draft please"* ]]
}

@test "spool: cancel withdraws a queued message" {
    mkdir -p "$CCM_DATA_DIR/spool/demo"
    printf 'withdraw me\n' > "$CCM_DATA_DIR/spool/demo/1700000000000-tester.msg"
    run bash "$CCM_ROOT/ccm" spool cancel 1700000000000-tester demo
    [ "$status" -eq 0 ]
    [ ! -f "$CCM_DATA_DIR/spool/demo/1700000000000-tester.msg" ]
}
