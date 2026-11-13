#!/usr/bin/env bats
load helpers/tmux_guard.bash

setup() {
    export CCM_DATA_DIR="$BATS_TEST_TMPDIR/data"
    export CCM_TMP_DIR="$BATS_TEST_TMPDIR/runtime"
    export CCM_HOOK_DIR="$CCM_TMP_DIR/hooks"
    export CCM_SNAPSHOT_DIR="$CCM_DATA_DIR/snapshots"
    export CCM_EXIT_ARGS="$BATS_TEST_TMPDIR/argv"
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    cat > "$BATS_TEST_TMPDIR/bin/python3" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$CCM_EXIT_ARGS"
exit "${CCM_EXIT_STATUS:-0}"
SH
    chmod +x "$BATS_TEST_TMPDIR/bin/python3"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
}

@test "exit forwards project and consent to the Python CLI" {
    run bash "$BATS_TEST_DIRNAME/../ccm" exit alpha -y
    [ "$status" -eq 0 ]
    [ "$(sed -n '2p' "$CCM_EXIT_ARGS")" = exit ]
    [ "$(sed -n '3p' "$CCM_EXIT_ARGS")" = alpha ]
    [ "$(sed -n '4p' "$CCM_EXIT_ARGS")" = -y ]
}

@test "exit preserves CLI failure status" {
    export CCM_EXIT_STATUS=1
    run bash "$BATS_TEST_DIRNAME/../ccm" exit alpha
    [ "$status" -eq 1 ]
}
