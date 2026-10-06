# All Bats files load this before defining setup/teardown. Mocks may
# precede the guard; any fallback to a real executable must pass it.
setup_file() {
    export CCM_TEST_REAL_TMUX="${CCM_TEST_REAL_TMUX-$(type -P tmux || true)}"
    export CCM_TEST_GUARD_DIR="${BATS_FILE_TMPDIR}/tmux-guard"
    # A real server's children (run-shell, hooks) run with the server's
    # environment; ccm.tmux starts `ccm setup-hooks` that way, which edits
    # $HOME/.claude/settings.json. The spy starts servers away from it.
    export CCM_TEST_REAL_HOME="$HOME"
    # The user's own settings must come out of a test file as they went
    # in; a test that writes them fails the file by name (see
    # teardown_file). Checksums only: nothing is read beyond that.
    mkdir -p "$CCM_TEST_GUARD_DIR/bin" "$CCM_TEST_GUARD_DIR/sockets" "$CCM_TEST_GUARD_DIR/home"
    ccm_test_real_settings_sums > "$CCM_TEST_GUARD_DIR/settings-before"
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

ccm_test_real_settings_sums() {
    local f hooks keys
    for f in .claude/settings.json .claude/settings.local.json .tmux.conf \
             .codex/hooks.json .codex/config.toml; do
        # Claude Code rewrites other keys of its settings while it runs.
        # Compared: the hooks (what ccm writes there), and in the user
        # settings also permissions and env (which only the user edits;
        # the local file's permissions change with each approval).
        # Anything but an object (or no jq) is compared whole.
        keys='{hooks}'
        [[ "$f" == .claude/settings.json ]] && keys='{hooks, permissions, env}'
        if [[ "$f" == .claude/* && -e "$CCM_TEST_REAL_HOME/$f" ]] && command -v jq >/dev/null \
                && hooks=$(jq -S "if type == \"object\" then $keys else error(\"not an object\") end" \
                               "$CCM_TEST_REAL_HOME/$f" 2>/dev/null); then
            printf '%s hooks %s\n' "$f" "$(printf '%s' "$hooks" | cksum)"
        elif [[ -e "$CCM_TEST_REAL_HOME/$f" ]]; then
            printf '%s %s\n' "$f" "$(cksum < "$CCM_TEST_REAL_HOME/$f")"
        else
            printf '%s absent\n' "$f"
        fi
    done
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
    if ! ccm_test_real_settings_sums | cmp -s - "$CCM_TEST_GUARD_DIR/settings-before"; then
        echo "The user's own settings (\$HOME) changed while this test file ran (a test wrote them, or they were edited meanwhile):"
        ccm_test_real_settings_sums | diff "$CCM_TEST_GUARD_DIR/settings-before" - | grep '^[<>]' || true
        return 1
    fi
    if [[ -s "$CCM_TEST_GUARD_DIR/denied" ]]; then
        echo "Non-isolated tmux calls were blocked (exit 99):"
        cat "$CCM_TEST_GUARD_DIR/denied"
        return 1
    fi
}
