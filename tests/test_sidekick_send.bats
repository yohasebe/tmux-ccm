#!/usr/bin/env bats
# Dispatch-level tests for `ccm sidekick-send` — the bash wrapper must
# forward the subcommand to the Python side, and the help surfaces
# must list it. Identity/refusal behaviour itself is covered by
# tests/test_sidekick_send.py (pytest).

setup() {
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_TMP_DIR="$(mktemp -d)"
}

teardown() {
    [[ -n "$CCM_TMP_DIR" && -d "$CCM_TMP_DIR" ]] && rm -rf "$CCM_TMP_DIR"
}

@test "sidekick-send: --help prints usage without tmux" {
    run bash "$CCM_ROOT/ccm" sidekick-send --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage: ccm sidekick-send"* ]]
}

@test "sidekick-send: listed in ccm help" {
    run bash "$CCM_ROOT/ccm" help
    [ "$status" -eq 0 ]
    [[ "$output" == *"ccm sidekick-send"* ]]
}
