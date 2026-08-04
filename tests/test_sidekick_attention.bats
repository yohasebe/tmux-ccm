#!/usr/bin/env bats
# Writer half of the sidekick attention-marker contract:
# hooks/sidekick-attention.sh, invoked by a sidekick CLI's OWN hook
# system with the event JSON on stdin. The reader/GC half is pinned
# in tests/test_sidekick_attention.py.

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
SCRIPT="${CCM_ROOT}/hooks/sidekick-attention.sh"

setup() {
    SANDBOX="$(mktemp -d)"
    export TMPDIR="${SANDBOX}/tmp"
    mkdir -p "$TMPDIR"
    ATTENTION_DIR="${TMPDIR}/ccm-${UID}/attention"
    export TMUX_PANE="%40"

    # Stub the externals the script may reach for: tmux (toggle read)
    # and terminal-notifier (desktop notification). The tmux stub's
    # toggle value comes from $STUB_TOGGLE; the notifier stub records
    # its argv so tests can assert on firing without a real
    # notification. PATH is prepended, so the stubs always win.
    STUB_BIN="${SANDBOX}/bin"
    mkdir -p "$STUB_BIN"
    cat > "${STUB_BIN}/tmux" <<'EOS'
#!/usr/bin/env bash
if [[ "$1" == "show-option" ]]; then printf '%s' "${STUB_TOGGLE:-}"; fi
exit 0
EOS
    cat > "${STUB_BIN}/terminal-notifier" <<EOS
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "${SANDBOX}/notified.log"
EOS
    chmod +x "${STUB_BIN}/tmux" "${STUB_BIN}/terminal-notifier"
    export PATH="${STUB_BIN}:${PATH}"
}

teardown() {
    [[ -n "$SANDBOX" && -d "$SANDBOX" ]] && rm -rf "$SANDBOX"
}

_request_payload() {
    printf '%s' '{"hook_event_name":"PermissionRequest","session_id":"sess-1","cwd":"/x/proj","tool_name":"Bash","tool_input":{"command":"npm test"}}'
}

@test "PermissionRequest writes a waiting marker keyed by TMUX_PANE" {
    _request_payload | bash "$SCRIPT" kimi
    MARKER="${ATTENTION_DIR}/%40.json"
    [[ -f "$MARKER" ]]
    run jq -r '.state' "$MARKER";   [[ "$output" == "waiting" ]]
    run jq -r '.agent' "$MARKER";   [[ "$output" == "kimi" ]]
    run jq -r '.summary' "$MARKER"; [[ "$output" == "Bash: npm test" ]]
    run jq -r '.pane' "$MARKER";    [[ "$output" == "%40" ]]
    run jq -r '.ts' "$MARKER"
    [[ "$output" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ]]
}

@test "PermissionResult overwrites to resolved, preserving id" {
    _request_payload | bash "$SCRIPT" kimi
    MARKER="${ATTENTION_DIR}/%40.json"
    ORIG_ID=$(jq -r '.id' "$MARKER")
    printf '%s' '{"hook_event_name":"PermissionResult","session_id":"sess-1"}' \
        | bash "$SCRIPT" kimi
    # Overwrite, never delete: a consumer that sees `resolved` KNOWS
    # the wait ended; a vanished file could just be a stale reap.
    [[ -f "$MARKER" ]]
    run jq -r '.state' "$MARKER"; [[ "$output" == "resolved" ]]
    run jq -r '.id' "$MARKER";    [[ "$output" == "$ORIG_ID" ]]
    run jq -r '.resolved_ts' "$MARKER"
    [[ "$output" =~ ^[0-9]{4}- ]]
}

@test "Interrupt and Stop also resolve a pending wait" {
    for ev in Interrupt Stop; do
        _request_payload | bash "$SCRIPT" kimi
        printf '%s' "{\"hook_event_name\":\"${ev}\"}" | bash "$SCRIPT" kimi
        run jq -r '.state' "${ATTENTION_DIR}/%40.json"
        [[ "$output" == "resolved" ]]
    done
}

@test "resolution events without a pending wait write nothing" {
    printf '%s' '{"hook_event_name":"Stop"}' | bash "$SCRIPT" kimi
    [[ ! -e "${ATTENTION_DIR}/%40.json" ]]
}

@test "no TMUX_PANE means no marker and exit 0" {
    unset TMUX_PANE
    run bash -c "printf '%s' '$(_request_payload)' | bash '$SCRIPT' kimi"
    [[ "$status" -eq 0 ]]
    [[ ! -d "$ATTENTION_DIR" || -z "$(ls -A "$ATTENTION_DIR")" ]]
}

@test "notification fires on request and carries the summary" {
    _request_payload | bash "$SCRIPT" kimi
    [[ -f "${SANDBOX}/notified.log" ]]
    run cat "${SANDBOX}/notified.log"
    [[ "$output" == *"kimi needs a decision"* ]]
    [[ "$output" == *"npm test"* ]]
    # No -sender: bundle-identity swapping makes notifications
    # silently droppable (feedback_terminal_notifier_sender).
    [[ "$output" != *"-sender"* ]]
}

@test "notification is suppressed when the toggle is off, marker still written" {
    _request_payload | STUB_TOGGLE=off bash "$SCRIPT" kimi
    [[ ! -f "${SANDBOX}/notified.log" ]]
    # ringi may consume markers independently of ccm's display, so
    # the toggle silences ccm without starving other consumers.
    [[ -f "${ATTENTION_DIR}/%40.json" ]]
}

@test "malformed stdin writes nothing and exits 0" {
    run bash -c "printf 'not json' | bash '$SCRIPT' kimi"
    [[ "$status" -eq 0 ]]
    [[ ! -e "${ATTENTION_DIR}/%40.json" ]]
}
