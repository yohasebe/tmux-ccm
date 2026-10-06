#!/usr/bin/env bats
load helpers/tmux_guard.bash
# The hooks read every payload field with one jq; if that read fails
# they read each field the old way. Both paths run the real jq here, on
# the same payloads, and must agree.

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
load helpers/mock_tmux.bash

setup() {
    umask 077
    command -v jq >/dev/null || skip "jq not installed"
    SANDBOX="$(mktemp -d)"
    export TMPDIR="${SANDBOX}/tmp"
    mkdir -p "$TMPDIR"
    setup_mocks
    : > "${MOCK_STATE_DIR}/windows"
}

teardown() {
    rm -rf "$SANDBOX"
}

# Prints the fields ccm_hook_init sets, and its status. With `old`,
# the one-jq read fails, so each field is read separately.
read_fields() {
    local path="$1" payload="$2"
    bash -c '
        if [[ "$1" == old ]]; then
            jq() { [[ "$1" == -j ]] && return 1; command jq "$@"; }
        fi
        source "$2/hooks/lib.sh"
        ccm_hook_init <<< "$3"; rc=$?
        bg=""
        if [[ -n "${_CCM_FIELDS_READ:-}" ]]; then bg="$HOOK_BG_REMAINING"
        else bg=$(printf "%s" "$3" | command jq -r "((.background_tasks // []) | length) + ((.session_crons // []) | length)" 2>/dev/null) || bg=0
        fi
        printf "rc=%s sid=[%s] cwd=[%s] mode=[%s] event=[%s] bg=[%s]\n" \
            "$rc" "$SESSION_ID" "$CWD" "$PERMISSION_MODE" "$(ccm_hook_event_name)" "$bg"
        [[ "$1" == old ]] || printf "read-once=%s\n" "${_CCM_FIELDS_READ:-}"
    ' _ "$path" "$CCM_ROOT" "$payload"
}

@test "one jq read gives what reading each field gives" {
    local payloads=(
        '{"session_id":"s1","cwd":"/x/p","permission_mode":"default","hook_event_name":"PreToolUse","tool_name":"Bash"}'
        '{"sessionId":"s2","cwd":"/x/with space","hook_event_name":"Stop"}'
        '{"session_id":"s3"}'
        '{"session_id":"s4","cwd":"/x/日本語","permission_mode":"acceptEdits","hook_event_name":"Notification"}'
        '{"session_id":"s5","hook_event_name":"Stop","background_tasks":[1,2],"session_crons":["c"]}'
        '{"session_id":"s6","permission_mode":"we ird$;mode"}'
    )
    local p new old
    for p in "${payloads[@]}"; do
        new=$(read_fields new "$p")
        old=$(read_fields old "$p")
        [[ "$new" == *"read-once=1"* ]] || { echo "not read once: $new"; return 1; }
        [[ "${new%$'\n'read-once=*}" == "$old" ]] || { echo "new: $new"; echo "old: $old"; return 1; }
    done
}

@test "trailing newlines are dropped as reading each field drops them" {
    local p new old
    for p in '{"session_id":"sid\n","cwd":"/missing\n","tool_name":"Bash\n","hook_event_name":"Stop\n","permission_mode":"default\n"}'; do
        new=$(read_fields new "$p")
        old=$(read_fields old "$p")
        [[ "$new" == *"read-once=1"* ]]
        [[ "${new%$'\n'read-once=*}" == "$old" ]] || { echo "new: $new"; echo "old: $old"; return 1; }
    done
}

@test "an array or object field is read the old way, as jq -r prints it" {
    local p new old
    for p in '{"session_id":["a","b"]}' '{"session_id":"s","cwd":{"k":1}}' '{"session_id":"s","tool_name":["x"]}'; do
        new=$(read_fields new "$p")
        old=$(read_fields old "$p")
        [[ "$new" != *"read-once=1"* ]] || { echo "read once: $new"; return 1; }
        [[ "${new%$'\n'read-once=*}" == "$old" ]] || { echo "new: $new"; echo "old: $old"; return 1; }
    done
}

@test "a pending notice that cannot be removed does not stop the hook" {
    local hook_dir="${TMPDIR}/ccm-${UID}/hooks" bin="${SANDBOX}/bin"
    mkdir -p "$hook_dir" "$bin"
    : > "${hook_dir}/pend-sid.pending"
    printf '#!/bin/sh\nexit 1\n' > "$bin/rm"; chmod +x "$bin/rm"
    run env PATH="$bin:$PATH" bash "${CCM_ROOT}/hooks/on-pre-tool-use.sh" \
        <<< '{"session_id":"pend-sid","cwd":"/x","hook_event_name":"PreToolUse"}'
    [ "$status" -eq 0 ]
    grep -q '"type":"pretool"' "${hook_dir}/pend-sid.events.jsonl"
}

@test "another harness's payload is refused on both paths" {
    local p='{"session_id":"s1","workspaceRoot":"/w"}'
    [[ "$(read_fields new "$p")" == rc=1* ]]
    [[ "$(read_fields old "$p")" == rc=1* ]]
}

@test "a payload that is not an object falls back to reading each field" {
    run read_fields new '["not","an","object"]'
    [[ "$output" == *"read-once="$'\n'* || "$output" == *"read-once=" ]]
    [[ "$output" == rc=1* ]]
}

@test "a session id that is not a plain token writes nothing" {
    local hook_dir="${TMPDIR}/ccm-${UID}/hooks"
    run bash "${CCM_ROOT}/hooks/on-pre-tool-use.sh" \
        <<< '{"session_id":"../../escaped","cwd":"/x","hook_event_name":"PreToolUse"}'
    [ "$status" -eq 0 ]
    [ ! -e "${TMPDIR}/escaped" ]
    [ ! -e "${TMPDIR}/escaped.events.jsonl" ]
    [ -z "$(ls -A "$hook_dir" 2>/dev/null)" ]
}
