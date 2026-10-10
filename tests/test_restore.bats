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
    local mode="$1" restore_sock
    export CCM_TEST_SOCKET
    CCM_TEST_SOCKET=$(ccm_test_new_socket)
    restore_sock=$(ccm_test_new_socket)
    cat > "$BATS_TEST_TMPDIR/bin/tmux" <<SHIM
#!/usr/bin/env bash
PATH="$PATH" exec tmux -L "\$CCM_TEST_SOCKET" -f /dev/null "\$@"
SHIM
    chmod +x "$BATS_TEST_TMPDIR/bin/tmux"
    PATH="$BATS_TEST_TMPDIR/bin:$PATH" python3 "$CCM_ROOT/tests/fixtures/snapshot/restore-probe.py" "$mode" "$BATS_TEST_TMPDIR" "$restore_sock" > "$BATS_TEST_TMPDIR/probe.log" 2>&1 || {
        cat "$BATS_TEST_TMPDIR/probe.log"
        return 1
    }
    printf '# %s\n' "$(tail -n 1 "$BATS_TEST_TMPDIR/probe.log")" >&3
}

@test "restore v2: mixed splits, overlapping IDs, pane-base-index, zoom and roles" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe geometry
}

@test "restore v2: newly split unreserved pane permits selection with and without a primary" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe added-pane
}

@test "restore v2: retry after a lost split reply creates no duplicates" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe retry
}

@test "restore v2: 46 synthetic windows remain inactive and start no agents" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe performance
}

@test "restore v2: a stop after creating every window resumes without duplicates" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe stop-after-create
}

@test "restore v2: a published window changed while others restore is left as the user set it" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe ready-changed
}

@test "restore v2: a window moved to another session after creation is held back; the rest are published" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe moved
}

@test "restore v2: a window linked into another session after creation is held back; the rest are published" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe linked
}

@test "restore v2: a window that lost its marker is held back and not re-marked; the rest are published" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe token-lost
}

@test "restore v2: a stop after clearing the first pending mark resumes publication" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe publish-lost
}

@test "restore v2: a failed progress cleanup is released by the retry it asks for" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe unlink-failed
}

@test "restore v2: a missing directory holds back one project; fixing it lets the same restore finish" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe missing-dir
}

@test "restore v2: a window that lost its marker part way through publication is held back, not adopted" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe started-unmarked
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

@test "restore v2: persistent rc work is preserved across retries and holds back only its window" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe rc-work
}

@test "restore v2: explicitly cleared panes stay manual after restore and autosave" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe manual
}

@test "restore v2: a second load during a restore waits and reports the first run's result" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    restore_probe concurrent
}

@test "auto-restore at tmux start keeps its output, including the failure reason" {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    export CCM_TEST_SOCKET
    CCM_TEST_SOCKET=$(ccm_test_new_socket)
    # The server's own children (run-shell, status jobs) call plain tmux, so
    # the shim must come first on the server's PATH and reach the guard by path.
    cat > "$BATS_TEST_TMPDIR/bin/tmux" <<SHIM
#!/usr/bin/env bash
exec "$CCM_TEST_GUARD_DIR/bin/tmux" -L "\$CCM_TEST_SOCKET" -f /dev/null "\$@"
SHIM
    chmod +x "$BATS_TEST_TMPDIR/bin/tmux"
    # The dependency check wants claude and fzf on PATH; neither may run
    # here beyond the version probe that startup's setup-hooks makes.
    local dep
    for dep in claude fzf; do
        cat > "$BATS_TEST_TMPDIR/bin/$dep" <<STUB
#!/bin/sh
[ "\$1" = --version ] && [ "$dep" = claude ] && { echo '9.9.9 (Claude Code)'; exit 0; }
touch "$BATS_TEST_TMPDIR/$dep-started"
exit 99
STUB
        chmod +x "$BATS_TEST_TMPDIR/bin/$dep"
    done
    mkdir -p "$CCM_SNAPSHOT_DIR"
    printf 'not json' > "$CCM_SNAPSHOT_DIR/_autosave.json"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    tmux new-session -d -s test -x 100 -y 30 /bin/sh
    tmux set-option -g @ccm-auto-restore on
    run bash "$CCM_ROOT/ccm.tmux"
    log="$CCM_DATA_DIR/state/auto-restore.log"
    for _ in $(seq 1 100); do
        [[ -s "$log" ]] && break
        sleep 0.1
    done
    tmux kill-server
    [[ -s "$log" ]]
    grep -q 'Snapshot unreadable' "$log" || { cat "$log"; return 1; }
    [ ! -e "$BATS_TEST_TMPDIR/claude-started" ]
    [ ! -e "$BATS_TEST_TMPDIR/fzf-started" ]
    mode=$(stat -c '%a' "$log" 2>/dev/null || stat -f '%Lp' "$log")
    [[ "$mode" == 600 ]]
}
