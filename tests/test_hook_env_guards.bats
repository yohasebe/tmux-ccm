#!/usr/bin/env bats
# Environment guards in the hook scripts' shared state path
# (hooks/lib.sh). Hook scripts run under `set -euo pipefail` inside
# Claude Code, which reports ANY non-zero exit as a visible
# "hook error" in the TUI — so tmux being absent in some dimension
# (no attached client, no server at all) must read as "nothing to
# do", never as failure.
#
# Observation approach mirrors test_notify_parity.bats: a stub tmux
# on PATH decides per-subcommand whether to succeed, emit fixture
# rows, or fail — no production code is modified. Stub writers live
# at file level (not inside @test bodies, where bats' preprocessor
# and heredocs interact badly).

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

# A server with no attached client: reads succeed, the window row
# matches $CCM_TEST_MATCH_DIR, and `refresh-client -S` exits 1 —
# exactly what real tmux does when no client is attached (network
# drop, scripted use).
write_detached_server_stub() {
    cat > "${MOCK_BIN}/tmux" <<'EOF'
#!/bin/bash
case "$1" in
    list-windows)   printf 'test:0\t%s\tguardproj\t\t\n' "$CCM_TEST_MATCH_DIR" ;;
    refresh-client) exit 1 ;;
esac
exit 0
EOF
    chmod +x "${MOCK_BIN}/tmux"
}

# No tmux server at all: every tmux invocation fails.
write_no_server_stub() {
    printf '#!/bin/bash\nexit 1\n' > "${MOCK_BIN}/tmux"
    chmod +x "${MOCK_BIN}/tmux"
}

# Healthy server that logs every invocation to $CCM_TEST_TMUX_LOG and
# serves one window row matching $CCM_TEST_MATCH_DIR whose cached
# session id is $CCM_TEST_ROW_SID (empty = unresolved window). The two
# list-windows callers ask for different field sets, so the row shape
# follows the requested format.
write_logging_server_stub() {
    cat > "${MOCK_BIN}/tmux" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$CCM_TEST_TMUX_LOG"
case "$1" in
    list-windows)
        if [[ "$*" == *"@ccm_prev_state"* ]]; then
            printf 'test:0\t%s\tguardproj\t\t%s\n' \
                "$CCM_TEST_MATCH_DIR" "$CCM_TEST_ROW_SID"
        else
            printf 'test:0\t%s\tguardproj\t%s\n' \
                "$CCM_TEST_MATCH_DIR" "$CCM_TEST_ROW_SID"
        fi ;;
esac
exit 0
EOF
    chmod +x "${MOCK_BIN}/tmux"
}

setup() {
    SANDBOX="$(mktemp -d)"
    # The hooks canonicalize the payload cwd (macOS: /var →
    # /private/var), so fixtures must carry the physical path or the
    # window match silently misses and the tests go vacuous.
    SANDBOX="$(cd "$SANDBOX" && pwd -P)"
    export TMPDIR="${SANDBOX}/tmp"
    mkdir -p "$TMPDIR"
    MOCK_BIN="${SANDBOX}/bin"
    mkdir -p "$MOCK_BIN"
    export PATH="${MOCK_BIN}:${PATH}"
    export CCM_TEST_MATCH_DIR="$SANDBOX"
    export CCM_TEST_TMUX_LOG="${SANDBOX}/tmux.log"
    export CCM_TEST_ROW_SID=""
    # Hooks resolve the pane from the environment tmux provides.
    export TMUX_PANE="%1"
    # Keep the backgrounded `ccm inject-status --fast` spawn inert.
    export CCM_BIN=/usr/bin/true
    # Keep desktop notifications inert (mirrors test_notify_parity).
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/osascript"
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/notify-send"
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/terminal-notifier"
    chmod +x "${MOCK_BIN}/osascript" "${MOCK_BIN}/notify-send" \
        "${MOCK_BIN}/terminal-notifier"
    HOOKS_DIR="${TMPDIR}/ccm-$(id -u)/hooks"
}

teardown() {
    [[ -n "$SANDBOX" && -d "$SANDBOX" ]] && rm -rf "$SANDBOX"
}

payload() {
    printf '{"session_id":"guard-test","hook_event_name":"%s","cwd":"%s"}' \
        "$1" "$SANDBOX"
}

run_hook() {
    local script="$1" event="$2"
    run bash -c "printf '%s' '$(payload "$event")' \
        | bash '${CCM_ROOT}/hooks/${script}'"
}

@test "state write survives refresh-client failing (server without attached client)" {
    write_detached_server_stub
    run_hook on-prompt-submit.sh UserPromptSubmit
    [ "$status" -eq 0 ]
    # The BUSY signal was written before the repaint attempt and must
    # survive it.
    [ -f "${HOOKS_DIR}/guard-test" ]
    grep -q BUSY "${HOOKS_DIR}/guard-test"
}

@test "on-prompt-submit exits 0 and writes nothing when no tmux server runs" {
    write_no_server_stub
    run_hook on-prompt-submit.sh UserPromptSubmit
    [ "$status" -eq 0 ]
    # With no server there is nothing to manage: no signal, no events.
    [ ! -e "${HOOKS_DIR}/guard-test" ]
    [ ! -e "${HOOKS_DIR}/guard-test.events.jsonl" ]
}

@test "on-stop exits 0 and writes nothing when no tmux server runs" {
    write_no_server_stub
    run_hook on-stop.sh Stop
    [ "$status" -eq 0 ]
    [ ! -e "${HOOKS_DIR}/guard-test.events.jsonl" ]
}

@test "a same-cwd session outside tmux cannot paint an unresolved window" {
    write_logging_server_stub
    unset TMUX_PANE
    run_hook on-prompt-submit.sh UserPromptSubmit
    [ "$status" -eq 0 ]
    # Its own session-keyed signal is still written — that part of the
    # contract is unchanged for user-scope hooks.
    [ -f "${HOOKS_DIR}/guard-test" ]
    # …but the window it merely shares a directory with is untouched.
    ! grep -q "rename-window" "$CCM_TEST_TMUX_LOG"
    ! grep -q "set-option" "$CCM_TEST_TMUX_LOG"
}

@test "a same-cwd session outside tmux cannot schedule the window's COMPLETED notification" {
    write_logging_server_stub
    unset TMUX_PANE
    run_hook on-stop.sh Stop
    [ "$status" -eq 0 ]
    [ ! -e "${HOOKS_DIR}/guard-test.pending" ]
}

@test "a tmux-resident session still paints and notifies an unresolved window" {
    # The legitimate cold start: the window's own claude fires its
    # first events before any session id is cached.
    write_logging_server_stub
    run_hook on-prompt-submit.sh UserPromptSubmit
    [ "$status" -eq 0 ]
    grep -q "rename-window" "$CCM_TEST_TMUX_LOG"
    run_hook on-stop.sh Stop
    [ "$status" -eq 0 ]
    [ -e "${HOOKS_DIR}/guard-test.pending" ]
}
