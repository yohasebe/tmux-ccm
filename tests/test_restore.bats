#!/usr/bin/env bats
load helpers/tmux_guard.bash

setup() {
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_DATA_DIR="$BATS_TEST_TMPDIR/data"
    export CCM_SNAPSHOT_DIR="$BATS_TEST_TMPDIR/snapshots"
    export CCM_TMP_DIR="$BATS_TEST_TMPDIR/runtime"
    export HOME="$BATS_TEST_TMPDIR/home"
    export SHELL=/bin/sh
    export ZDOTDIR="$HOME/zsh"
    export ENV="$HOME/empty-rc" BASH_ENV="$HOME/empty-rc"
    export XDG_CONFIG_HOME="$HOME/config"
    unset ZSH ZSH_CUSTOM
    mkdir -p "$CCM_TMP_DIR" "$BATS_TEST_TMPDIR/bin" "$ZDOTDIR" "$XDG_CONFIG_HOME"
    : > "$ENV"
}

restore_probe() {
    local mode="$1" sock
    sock=$(ccm_test_new_socket)
    cat > "$BATS_TEST_TMPDIR/bin/tmux" <<SHIM
#!/usr/bin/env bash
PATH="$PATH" exec tmux -L "$sock" -f /dev/null "\$@"
SHIM
    chmod +x "$BATS_TEST_TMPDIR/bin/tmux"
    PATH="$BATS_TEST_TMPDIR/bin:$PATH" python3 "$CCM_ROOT/tests/fixtures/snapshot/restore-probe.py" "$mode" "$BATS_TEST_TMPDIR" > "$BATS_TEST_TMPDIR/probe.log" 2>&1 || {
        cat "$BATS_TEST_TMPDIR/probe.log"
        return 1
    }
    printf '# %s\n' "$(tail -n 1 "$BATS_TEST_TMPDIR/probe.log")" >&3
}

@test "restore v2: mixed splits, overlapping IDs, pane-base-index, zoom and roles" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe geometry
}

@test "restore v2: retry after a lost split reply creates no duplicates" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe retry
}

@test "restore v2: 46 synthetic windows remain inactive and start no agents" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe performance
}

@test "restore v2: a lost new-window reply is resumed by ownership marker" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe retry-new
}

@test "restore v2: retry after zoom verifies unzoomed geometry" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe retry-zoom
}

@test "restore v2: pane border labels do not change layout cell geometry" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe borders
}

@test "restore v2: configured login shell survives transient rc foreground work" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe login-shell
}

@test "restore v2: persistent rc work is preserved across failed retries" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe rc-work
}
