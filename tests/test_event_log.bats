#!/usr/bin/env bats
# Tests for the event log writer added in the detection-redesign P1
# and migrated to session_id keying in v0.3.0. Each hook script
# appends one JSONL record to $HOOK_DIR/<session_id>.events.jsonl
# in addition to writing the legacy signal file. The existing
# signal-file behavior is unchanged; this test suite verifies the
# event-log append path specifically.

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

load helpers/mock_tmux.bash

# Synthetic session_id used in every hook payload. Bats tests no
# longer derive KEY from cwd — session_id is the primary key, so we
# pin a fixed UUID here and inject it into each payload via the
# `_payload` helper.
TEST_SESSION_ID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

setup() {
    SANDBOX="$(mktemp -d)"
    export TMPDIR="${SANDBOX}/tmp"
    mkdir -p "$TMPDIR"

    setup_mocks
    # mock_tmux returns non-zero for list-windows when no windows file
    # exists; under `set -o pipefail` in hook scripts the pipeline then
    # aborts. An empty windows file makes the mock return 0 with empty
    # output (matches the real tmux behaviour in an empty session).
    : > "${MOCK_STATE_DIR}/windows"

    source "${CCM_ROOT}/hooks/lib.sh"

    KEY="$TEST_SESSION_ID"
    CWD="/x/test-project"
    HOOK_DIR="${TMPDIR}/ccm-${UID}/hooks"
    mkdir -p "$HOOK_DIR"
    EVENTS_FILE="${HOOK_DIR}/${KEY}.events.jsonl"
}

# Inject session_id into a JSON payload string. Used by every
# end-to-end hook test so payloads always carry the field hooks
# now require for keying.
_with_session() {
    local payload="$1"
    # Strip the trailing `}` and append the session_id field
    printf '%s,"session_id":"%s"}' "${payload%\}}" "$TEST_SESSION_ID"
}

teardown() {
    teardown_mocks
    [[ -n "$SANDBOX" && -d "$SANDBOX" ]] && rm -rf "$SANDBOX"
}

# ─── ccm_append_event() direct unit tests ───

@test "append_event: writes a single JSONL record" {
    ccm_append_event "$HOOK_DIR" "$KEY" "prompt"
    [[ -f "$EVENTS_FILE" ]]
    run wc -l < "$EVENTS_FILE"
    [[ "$output" -eq 1 ]]
}

@test "append_event: record contains ts and type fields" {
    ccm_append_event "$HOOK_DIR" "$KEY" "stop"
    run jq -r '.type' "$EVENTS_FILE"
    [[ "$output" == "stop" ]]
    # ts is a unix-seconds integer
    run jq -r '.ts | type' "$EVENTS_FILE"
    [[ "$output" == "number" ]]
}

@test "append_event: multiple calls append in order" {
    ccm_append_event "$HOOK_DIR" "$KEY" "prompt"
    ccm_append_event "$HOOK_DIR" "$KEY" "pretool"
    ccm_append_event "$HOOK_DIR" "$KEY" "stop"
    run jq -r '.type' "$EVENTS_FILE"
    [[ "${lines[0]}" == "prompt" ]]
    [[ "${lines[1]}" == "pretool" ]]
    [[ "${lines[2]}" == "stop" ]]
}

@test "append_event: empty args are a silent no-op" {
    run ccm_append_event "" "$KEY" "prompt"
    [[ "$status" -eq 0 ]]
    run ccm_append_event "$HOOK_DIR" "" "prompt"
    [[ "$status" -eq 0 ]]
    run ccm_append_event "$HOOK_DIR" "$KEY" ""
    [[ "$status" -eq 0 ]]
    [[ ! -f "$EVENTS_FILE" ]]
}

@test "append_event: write failure does not propagate error" {
    # Directory not writable → append must still return 0 so the
    # legacy signal-file path continues to run.
    chmod 000 "$HOOK_DIR"
    run ccm_append_event "$HOOK_DIR" "$KEY" "stop"
    chmod 755 "$HOOK_DIR"
    [[ "$status" -eq 0 ]]
}

# ─── hook_event_name() helper ───

@test "hook_event_name: extracts from JSON payload" {
    INPUT='{"hook_event_name":"PreToolUse","cwd":"/x"}'
    run ccm_hook_event_name
    [[ "$output" == "PreToolUse" ]]
}

@test "hook_event_name: empty when field absent" {
    INPUT='{"cwd":"/x"}'
    run ccm_hook_event_name
    [[ -z "$output" ]]
}

# ─── End-to-end: each hook script emits the right event type ───
#
# Invoke each script with a minimal Claude Code-shaped JSON payload
# and verify the resulting event log line.

_run_hook() {
    local script="$1" payload="$2"
    # Hook scripts resolve TMPDIR and write under $TMPDIR/ccm-$UID/hooks,
    # so the sandbox TMPDIR from setup() is already in effect.
    # All payloads are augmented with the test session_id since hooks
    # now key on it.
    printf '%s' "$(_with_session "$payload")" | bash "${CCM_ROOT}/hooks/${script}"
}

_latest_type() {
    jq -rs '.[-1].type' "$EVENTS_FILE"
}

@test "on-prompt-submit.sh: emits prompt event" {
    _run_hook on-prompt-submit.sh \
        '{"hook_event_name":"UserPromptSubmit","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "prompt" ]]
}

@test "on-pre-tool-use.sh: PreToolUse -> pretool" {
    _run_hook on-pre-tool-use.sh \
        '{"hook_event_name":"PreToolUse","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "pretool" ]]
}

@test "on-pre-tool-use.sh: PostToolUse -> posttool" {
    _run_hook on-pre-tool-use.sh \
        '{"hook_event_name":"PostToolUse","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "posttool" ]]
}

@test "on-pre-tool-use.sh: SubagentStart -> subagent" {
    _run_hook on-pre-tool-use.sh \
        '{"hook_event_name":"SubagentStart","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "subagent" ]]
}

@test "on-pre-tool-use.sh: PreCompact -> compact" {
    _run_hook on-pre-tool-use.sh \
        '{"hook_event_name":"PreCompact","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "compact" ]]
}

@test "on-stop.sh: emits stop event and removes signal file" {
    # Pre-populate the signal file so we can observe the stop-path
    # delete behaviour alongside the new event append.
    printf '0 BUSY' > "${HOOK_DIR}/${KEY}"
    _run_hook on-stop.sh \
        '{"hook_event_name":"Stop","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "stop" ]]
    [[ ! -f "${HOOK_DIR}/${KEY}" ]]
}

@test "on-notification.sh: permission_prompt -> notify_permit" {
    _run_hook on-notification.sh \
        '{"hook_event_name":"Notification","notification_type":"permission_prompt","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "notify_permit" ]]
}

@test "on-notification.sh: idle_prompt -> notify_idle" {
    _run_hook on-notification.sh \
        '{"hook_event_name":"Notification","notification_type":"idle_prompt","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "notify_idle" ]]
}

@test "on-permission-request.sh: emits permit_req event" {
    _run_hook on-permission-request.sh \
        '{"hook_event_name":"PermissionRequest","cwd":"/x/test-project","tool_name":"Bash","tool_input":{"command":"ls"}}'
    run _latest_type
    [[ "$output" == "permit_req" ]]
}

@test "on-permission-denied.sh: emits permit_req event" {
    _run_hook on-permission-denied.sh \
        '{"hook_event_name":"PermissionDenied","cwd":"/x/test-project","tool_name":"Bash","tool_input":{"command":"ls"}}'
    run _latest_type
    [[ "$output" == "permit_req" ]]
}

@test "on-session-end.sh: emits session_end event" {
    _run_hook on-session-end.sh \
        '{"hook_event_name":"SessionEnd","cwd":"/x/test-project"}'
    run _latest_type
    [[ "$output" == "session_end" ]]
}

# ─── Legacy signal-file path is still intact (P1 regression guard) ───

@test "on-prompt-submit.sh: still writes BUSY signal file" {
    _run_hook on-prompt-submit.sh \
        '{"hook_event_name":"UserPromptSubmit","cwd":"/x/test-project"}'
    [[ -f "${HOOK_DIR}/${KEY}" ]]
    run cat "${HOOK_DIR}/${KEY}"
    [[ "$output" == *"BUSY"* ]]
}

@test "on-permission-request.sh: still writes PERMIT signal file" {
    _run_hook on-permission-request.sh \
        '{"hook_event_name":"PermissionRequest","cwd":"/x/test-project","tool_name":"Bash","tool_input":{"command":"ls"}}'
    [[ -f "${HOOK_DIR}/${KEY}" ]]
    run cat "${HOOK_DIR}/${KEY}"
    [[ "$output" == *"PERMIT"* ]]
}
