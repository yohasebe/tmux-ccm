#!/usr/bin/env bats
load helpers/tmux_guard.bash

setup() {
    export HOME="$BATS_TEST_TMPDIR/home"
    export CODEX_HOME="$HOME/codex"
    export CCM_DATA_DIR="$BATS_TEST_TMPDIR/data"
    export CCM_TMP_DIR="$BATS_TEST_TMPDIR/ccm-tmp"
    export TMPDIR="$BATS_TEST_TMPDIR/tmp"
    mkdir -p "$HOME" "$TMPDIR" "$BATS_TEST_TMPDIR/bin"
    SCRIPT="$BATS_TEST_DIRNAME/../hooks/codex-notify.sh"
    # The hook must ignore a stale ambient pane and an unregistered cwd.
    export TMUX_PANE='%999'
    cat > "$BATS_TEST_TMPDIR/bin/tmux" <<'SH'
#!/usr/bin/env bash
exit 0
SH
    chmod +x "$BATS_TEST_TMPDIR/bin/tmux"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
}

@test "Codex malformed hook input exits zero without output" {
    run bash -c 'printf "%s" "{invalid" | "$1"' bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "Codex unregistered payload cwd writes no spool and returns no approval decision" {
    run bash -c 'printf "{\"hook_event_name\":\"PermissionRequest\",\"cwd\":\"%s\",\"session_id\":\"demo-session\",\"turn_id\":\"demo-turn\",\"tool_name\":\"Bash\"}" "$HOME" | "$1"' bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    [ ! -d "$CCM_DATA_DIR/spool" ]
}

@test "Codex hook helper failure never blocks or answers approval" {
    cat > "$BATS_TEST_TMPDIR/bin/python3" <<'SH'
#!/usr/bin/env bash
printf 'unexpected output\n'
exit 3
SH
    chmod +x "$BATS_TEST_TMPDIR/bin/python3"
    run bash -c 'printf "%s" "{}" | "$1"' bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}
