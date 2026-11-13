#!/usr/bin/env bats
load helpers/tmux_guard.bash
# Dispatch-level tests for `ccm status --json`: the bash wrapper must
# forward the arguments, and a run answers with one JSON object even
# when it is refused. The records themselves are covered by
# tests/test_status_json.py (pytest).

setup() {
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_TMP_DIR="$(mktemp -d)"
}

teardown() {
    [[ -n "$CCM_TMP_DIR" && -d "$CCM_TMP_DIR" ]] && rm -rf "$CCM_TMP_DIR"
}

@test "status --json --help answers with the usage as JSON" {
    run bash "$CCM_ROOT/ccm" status --json --help
    [ "$status" -eq 0 ]
    [[ "$output" == '{"schema": 1, "help": "Usage: ccm status'* ]]
}

@test "status --json with two names is a usage error as JSON" {
    run bash "$CCM_ROOT/ccm" status --json one two
    [ "$status" -eq 1 ]
    [[ "$output" == '{"schema": 1, "error": {"code": "usage"'* ]]
}

@test "status --help prints the usage" {
    run bash "$CCM_ROOT/ccm" status --help
    [ "$status" -eq 0 ]
    [[ "$output" == "Usage: ccm status"* ]]
}

@test "status --json is listed in ccm help" {
    run bash "$CCM_ROOT/ccm" help
    [ "$status" -eq 0 ]
    [[ "$output" == *"ccm status --json"* ]]
}
