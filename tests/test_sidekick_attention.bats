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

# ─── Claude-as-sidekick (routed via lib.sh's ignore branch) ───

@test "claude: Notification permission_prompt opens a wait" {
    printf '%s' '{"hook_event_name":"Notification","notification_type":"permission_prompt","session_id":"c-1","cwd":"/x"}' \
        | bash "$SCRIPT" claude
    run jq -r '.state + "/" + .agent' "${ATTENTION_DIR}/%40.json"
    [[ "$output" == "waiting/claude" ]]
}

@test "claude: double-fire (PermissionRequest + Notification) keeps one marker, one notification" {
    # One Claude dialog raises BOTH events; the second write must be
    # a no-op or the user gets two desktop notifications per dialog.
    printf '%s' '{"hook_event_name":"PermissionRequest","session_id":"c-1","cwd":"/x","tool_name":"Bash","tool_input":{"command":"ls"}}' \
        | bash "$SCRIPT" claude
    ORIG_ID=$(jq -r '.id' "${ATTENTION_DIR}/%40.json")
    printf '%s' '{"hook_event_name":"Notification","notification_type":"permission_prompt","session_id":"c-1","cwd":"/x"}' \
        | bash "$SCRIPT" claude
    run jq -r '.id' "${ATTENTION_DIR}/%40.json"
    [[ "$output" == "$ORIG_ID" ]]
    run wc -l < "${SANDBOX}/notified.log"
    [[ "$output" -eq 1 ]]
}

@test "claude: any activity event resolves (no PermissionResult upstream)" {
    for ev in PostToolUse UserPromptSubmit PreToolUse; do
        printf '%s' '{"hook_event_name":"PermissionRequest","session_id":"c-1","cwd":"/x"}' \
            | bash "$SCRIPT" claude
        printf '%s' "{\"hook_event_name\":\"${ev}\",\"session_id\":\"c-1\"}" \
            | bash "$SCRIPT" claude
        run jq -r '.state' "${ATTENTION_DIR}/%40.json"
        [[ "$output" == "resolved" ]]
        rm -f "${ATTENTION_DIR}/%40.json"
    done
}

@test "claude: idle_prompt notification resolves, other types are inert" {
    printf '%s' '{"hook_event_name":"PermissionRequest","session_id":"c-1","cwd":"/x"}' \
        | bash "$SCRIPT" claude
    printf '%s' '{"hook_event_name":"Notification","notification_type":"something_else","session_id":"c-1"}' \
        | bash "$SCRIPT" claude
    run jq -r '.state' "${ATTENTION_DIR}/%40.json"
    [[ "$output" == "waiting" ]]
    printf '%s' '{"hook_event_name":"Notification","notification_type":"idle_prompt","session_id":"c-1"}' \
        | bash "$SCRIPT" claude
    run jq -r '.state' "${ATTENTION_DIR}/%40.json"
    [[ "$output" == "resolved" ]]
}

@test "lib.sh routes an IGNORED session's permission events to the adapter" {
    # End-to-end through the real hook script: the session marker file
    # makes ccm_hook_init take the ignore branch, which must forward
    # to the adapter and still suppress every state artefact.
    HOOK_DIR="${TMPDIR}/ccm-${UID}/hooks"
    mkdir -p "$HOOK_DIR"
    : > "${HOOK_DIR}/ig-sess.ignore"
    printf '%s' '{"hook_event_name":"PermissionRequest","session_id":"ig-sess","cwd":"/x","tool_name":"Bash","tool_input":{"command":"rm -rf build"}}' \
        | bash "${CCM_ROOT}/hooks/on-permission-request.sh"
    run jq -r '.agent + "/" + .state' "${ATTENTION_DIR}/%40.json"
    [[ "$output" == "claude/waiting" ]]
    # The ignore contract still holds: no signal, no event log.
    [[ ! -f "${HOOK_DIR}/ig-sess" ]]
    [[ ! -f "${HOOK_DIR}/ig-sess.events.jsonl" ]]
}

@test "lib.sh routes the CCM_IGNORE env branch too (first fire of a fresh sidekick)" {
    # The env branch handles a `CCM_IGNORE=1 claude` session's FIRST
    # hook fire, before the session marker file exists. It must route
    # like the marker-file branch — otherwise a fresh Claude sidekick's
    # very first permission dialog (a plausible first event) vanishes.
    HOOK_DIR="${TMPDIR}/ccm-${UID}/hooks"
    mkdir -p "$HOOK_DIR"
    printf '%s' '{"hook_event_name":"PermissionRequest","session_id":"env-sess","cwd":"/x"}' \
        | env CCM_IGNORE=1 bash "${CCM_ROOT}/hooks/on-permission-request.sh"
    run jq -r '.agent + "/" + .state' "${ATTENTION_DIR}/%40.json"
    [[ "$output" == "claude/waiting" ]]
    # And the branch's own duty still done: the session marker exists.
    [[ -f "${HOOK_DIR}/env-sess.ignore" ]]
}

@test "lib.sh does NOT route a tracked session to the adapter" {
    # A tracked claude's waits surface as real PERMIT; writing an
    # attention marker too would double-report the same dialog.
    HOOK_DIR="${TMPDIR}/ccm-${UID}/hooks"
    mkdir -p "$HOOK_DIR"
    printf '%s' '{"hook_event_name":"PermissionRequest","session_id":"tr-sess","cwd":"/x"}' \
        | bash "${CCM_ROOT}/hooks/on-permission-request.sh"
    [[ ! -e "${ATTENTION_DIR}/%40.json" ]]
}
