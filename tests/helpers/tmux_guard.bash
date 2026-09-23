# All Bats files load this before defining setup/teardown. Mocks may
# precede the guard; any fallback to a real executable must pass it.
setup_file() {
    export CCM_TEST_REAL_TMUX="${CCM_TEST_REAL_TMUX-$(type -P tmux || true)}"
    export CCM_TEST_GUARD_DIR="${BATS_FILE_TMPDIR}/tmux-guard"
    mkdir -p "$CCM_TEST_GUARD_DIR/bin" "$CCM_TEST_GUARD_DIR/sockets"
    : > "$CCM_TEST_GUARD_DIR/denied"
    cp "${BATS_TEST_DIRNAME}/helpers/tmux_spy.bash" "$CCM_TEST_GUARD_DIR/bin/tmux"
    chmod +x "$CCM_TEST_GUARD_DIR/bin/tmux"
    export PATH="$CCM_TEST_GUARD_DIR/bin:$PATH"
    # Keep even explicitly isolated server sockets out of the user's
    # socket directory. mktemp's short path avoids Unix socket limits.
    export TMUX_TMPDIR
    TMUX_TMPDIR=$(mktemp -d /tmp/ccm-bats.XXXXXX)
    unset TMUX TMUX_PANE
}

ccm_test_new_socket() {
    local registration
    registration=$(mktemp -d "$CCM_TEST_GUARD_DIR/sockets/ccm-test.XXXXXX") || return
    basename "$registration"
}

teardown_file() {
    local registration
    # A failing test may not reach its normal server shutdown. Only
    # names allocated by ccm_test_new_socket can be used here.
    for registration in "$CCM_TEST_GUARD_DIR"/sockets/*; do
        [[ -d "$registration" ]] || continue
        "$CCM_TEST_GUARD_DIR/bin/tmux" -L "${registration##*/}" kill-server 2>/dev/null || true
    done
    rm -rf "$TMUX_TMPDIR"
    if [[ -s "$CCM_TEST_GUARD_DIR/denied" ]]; then
        echo "Non-isolated tmux calls were blocked (exit 99):"
        cat "$CCM_TEST_GUARD_DIR/denied"
        return 1
    fi
}
