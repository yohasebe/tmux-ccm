#!/usr/bin/env bats
load helpers/tmux_guard.bash
# A project's name and branch go into the status bar's format as literal
# text: tmux, expanding the entry, shows them as they are and runs nothing.

setup() {
    [[ -n "$CCM_TEST_REAL_TMUX" ]] || skip "tmux not installed"
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_TEST_SOCKET
    CCM_TEST_SOCKET=$(ccm_test_new_socket)
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    cat > "$BATS_TEST_TMPDIR/bin/tmux" <<SHIM
#!/usr/bin/env bash
exec "$CCM_TEST_GUARD_DIR/bin/tmux" -L "\$CCM_TEST_SOCKET" -f /dev/null "\$@"
SHIM
    chmod +x "$BATS_TEST_TMPDIR/bin/tmux"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    tmux new-session -d -s w -x 120 -y 20 /bin/sh
}

teardown() {
    tmux kill-server 2>/dev/null || true
}

# tmux draws the bar, and so runs its `#(...)` jobs, only for an attached
# client: hold one on a pseudo-terminal for a few seconds.
attach_client() {
    python3 - <<'PY'
import os, pty, time
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execvp("tmux", ["tmux", "attach", "-t", "w"])
end = time.time() + 5
while time.time() < end:
    try:
        os.read(fd, 65536)
    except OSError:
        break
PY
}

# The entries for one project in both modes, current and not, one per line.
entries() {
    python3 - "$CCM_ROOT/lib" "$1" "$2" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import ccm_core, inject_status
p = ccm_core.Project(win_target="w:1", win_idx="1", name=sys.argv[2],
                     directory="/", state="IDLE", branch=sys.argv[3])
for extras in (False, True):
    for current in ("w:1", "w:9"):
        print(inject_status.build_detail_entries(
            [p], with_extras=extras, current_win_target=current)[0])
PY
}

@test "a name and branch holding tmux formats are shown as written and run nothing" {
    local marker="$BATS_TEST_TMPDIR/ran"
    local name='n#{session_name}x' branch="b#(touch${IFS:0:1}$marker)"
    local count=0 entry shown
    while IFS= read -r entry; do
        shown=$(tmux display-message -p "$entry")
        [[ "$shown" == *"$name"* ]]
        count=$((count + 1))
        # Mode 2 (the last two lines) also shows the branch.
        if (( count > 2 )); then
            [[ "$shown" == *"$branch"* ]]
        fi
    done < <(entries "$name" "$branch")
    [[ "$count" -eq 4 ]]
    # Drawn on the bar, too. A job of our own beside the entries shows that
    # tmux drew them; their jobs would run in the same pass.
    local drawn="$BATS_TEST_TMPDIR/drawn"
    tmux set-option -g status-interval 1
    tmux set-option -g status-right "$(entries "$name" "$branch" | tr '\n' ' ')#(touch '$drawn')"
    attach_client &
    for _ in $(seq 1 50); do
        [[ -e "$drawn" ]] && break
        sleep 0.1
    done
    sleep 1
    tmux kill-server
    wait
    [ -e "$drawn" ]
    [ ! -e "$marker" ]
}

@test "the bar's refresh runs ccm from an install path holding tmux formats" {
    local plugin="$BATS_TEST_TMPDIR/plug#{session_name}in"
    local marker="$BATS_TEST_TMPDIR/refreshed"
    mkdir -p "$plugin"
    printf '#!/bin/sh\n[ "$1" = inject-status ] && touch "%s"\nexit 0\n' "$marker" > "$plugin/ccm"
    chmod +x "$plugin/ccm"
    local refresh
    refresh=$(CCM_ROOT="$plugin" python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import inject_status; print(inject_status.refresh_command())' "$CCM_ROOT/lib")
    tmux set-option -g status-interval 1
    tmux set-option -g status-right "$refresh"
    attach_client &
    for _ in $(seq 1 50); do
        [[ -e "$marker" ]] && break
        sleep 0.1
    done
    tmux kill-server
    wait
    [ -e "$marker" ]
}
