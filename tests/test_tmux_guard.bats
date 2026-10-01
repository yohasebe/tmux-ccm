#!/usr/bin/env bats
load helpers/tmux_guard.bash

setup() {
    # Exercise the same spy with a recording executable, never a live server.
    CASE_DIR="$BATS_TEST_TMPDIR/case"
    mkdir -p "$CASE_DIR/sockets/ccm-test.allowed"
    : > "$CASE_DIR/denied"
    cat > "$CASE_DIR/native" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$CASE_DIR/forwarded"
MOCK
    chmod +x "$CASE_DIR/native"
    export CASE_DIR
}

_spy() {
    CCM_TEST_GUARD_DIR="$CASE_DIR" CCM_TEST_REAL_TMUX="$CASE_DIR/native" \
        bash "$BATS_TEST_DIRNAME/helpers/tmux_spy.bash" "$@"
}

@test "tmux guard rejects default, unregistered and overriding selectors" {
    local args
    for args in 'list-sessions' '-L default list-sessions' '-S /tmp/socket list-sessions' \
                '-L ccm-test.allowed -L default list-sessions'; do
        run _spy $args
        [[ "$status" -eq 99 ]]
    done
    [[ "$(wc -l < "$CASE_DIR/denied")" -eq 4 ]]
    [[ ! -e "$CASE_DIR/forwarded" ]]
}

@test "tmux guard forwards only an allocated socket with empty config" {
    _spy -L ccm-test.allowed -f /dev/null list-sessions
    [[ "$(cat "$CASE_DIR/forwarded")" == $'-L\nccm-test.allowed\n-f\n/dev/null\nlist-sessions' ]]
    [[ ! -s "$CASE_DIR/denied" ]]
}

@test "tmux startup failure preserves stderr and status without dumping the environment" {
    cat > "$CASE_DIR/native" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == -V ]]; then echo 'tmux test-version'; exit 0; fi
echo 'synthetic startup failure' >&2
exit 42
MOCK
    export CCM_TEST_SECRET=do-not-print-this-value
    export ENV=do-not-print-this-rc-path
    unset TERM
    run _spy -L ccm-test.allowed -f /dev/null new-session -d
    [[ "$status" -eq 42 ]]
    [[ "$output" == *'synthetic startup failure'* ]]
    [[ "$output" == *'tmux new-session failed: exit=42'* ]]
    [[ "$output" == *'socket_bytes='* && "$output" == *'resolved_socket_bytes='* ]]
    [[ "$output" == *'config=/dev/null'* && "$output" == *'tmux test-version'* ]]
    [[ "$output" == *'TERM: set=no bytes=0'* ]]
    [[ "$output" != *'do-not-print'* && "$output" != *'CCM_TEST_SECRET'* ]]
}

_fixture() {
    mkdir -p "$CASE_DIR/fixture"
    ln -s "$BATS_TEST_DIRNAME/helpers" "$CASE_DIR/fixture/helpers"
    cp "$BATS_TEST_DIRNAME/fixtures/tmux_guard/$1.fixture" "$CASE_DIR/fixture/probe.bats"
}

@test "a swallowed tmux refusal still fails the file at teardown" {
    _fixture swallow
    run "$BATS_ROOT/bin/bats" "$CASE_DIR/fixture/probe.bats"
    [[ "$status" -ne 0 ]]
    [[ "$output" == *"Non-isolated tmux calls were blocked"* ]]
    [[ "$output" == *"list-sessions"* ]]
}

@test "a failed test still removes its dedicated tmux server" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    _fixture failed_server
    run "$BATS_ROOT/bin/bats" "$CASE_DIR/fixture/probe.bats"
    [[ "$status" -ne 0 ]]
    [[ -s "$CASE_DIR/server-pid" ]]
    local pid i
    pid=$(cat "$CASE_DIR/server-pid")
    for i in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.05; done
    ! kill -0 "$pid" 2>/dev/null
    [[ ! -d "$(cat "$CASE_DIR/socket-dir")" ]]
    [[ "$output" != *"Non-isolated tmux calls were blocked"* ]]
}
