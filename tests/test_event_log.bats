#!/usr/bin/env bats
# Tests for the event log writer. Each hook script appends one JSONL
# record to $HOOK_DIR/<session_id>.events.jsonl in addition to
# writing the signal file. This suite verifies both the event-log
# append path and the signal-file write.

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
    # Same file-existence convention for list-sessions: the hooks bail
    # out early when no tmux server answers, so the harness must model
    # a live one.
    : > "${MOCK_STATE_DIR}/sessions"

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

@test "append_event: includes mode field when PERMISSION_MODE set" {
    PERMISSION_MODE="acceptEdits"
    ccm_append_event "$HOOK_DIR" "$KEY" "pretool"
    run jq -r '.mode' "$EVENTS_FILE"
    [[ "$output" == "acceptEdits" ]]
}

@test "append_event: omits mode field when PERMISSION_MODE empty" {
    PERMISSION_MODE=""
    ccm_append_event "$HOOK_DIR" "$KEY" "pretool"
    run jq -r 'has("mode")' "$EVENTS_FILE"
    [[ "$output" == "false" ]]
}

@test "hook_init: extracts and sanitizes permission_mode" {
    ccm_hook_init <<< '{"session_id":"'"$TEST_SESSION_ID"'","cwd":"/x","permission_mode":"bypassPermissions"}'
    [[ "$PERMISSION_MODE" == "bypassPermissions" ]]
    # Hostile value: everything outside [A-Za-z0-9_-] is stripped so
    # the value can be embedded into the JSONL record verbatim.
    ccm_hook_init <<< '{"session_id":"'"$TEST_SESSION_ID"'","cwd":"/x","permission_mode":"we\\ird{mo:de"}'
    [[ "$PERMISSION_MODE" == "weirdmode" ]]
}

@test "hook_init: permission_mode empty when field absent" {
    ccm_hook_init <<< '{"session_id":"'"$TEST_SESSION_ID"'","cwd":"/x"}'
    [[ -z "$PERMISSION_MODE" ]]
}

# ─── CCM_IGNORE: hide a session from ccm ───

@test "hook_init: CCM_IGNORE early-exits and writes the session marker" {
    export TMUX_PANE="%5"
    run env CCM_IGNORE=1 bash -c '
        source "'"${CCM_ROOT}"'/hooks/lib.sh"
        ccm_hook_init <<< "{\"session_id\":\"'"$TEST_SESSION_ID"'\",\"cwd\":\"/x\"}"
        echo "rc=$?"
    '
    [[ "$output" == *"rc=1"* ]]                       # suppressed
    [[ -f "${HOOK_DIR}/${TEST_SESSION_ID}.ignore" ]]  # marker written
}

@test "hook_init: CCM_IGNORE stamps the @ccm_ignore pane option" {
    export TMUX_PANE="%5"
    CCM_IGNORE=1 ccm_hook_init \
        <<< '{"session_id":"'"$TEST_SESSION_ID"'","cwd":"/x"}' || true
    # mock_tmux records `set-option -p -t %5 @ccm_ignore 1` under the
    # pane's options dir.
    [[ "$(cat "${MOCK_STATE_DIR}/options/%5/@ccm_ignore" 2>/dev/null)" == "1" ]]
}

@test "hook_init: pre-existing session marker suppresses the hook" {
    : > "${HOOK_DIR}/${TEST_SESSION_ID}.ignore"
    run ccm_hook_init <<< '{"session_id":"'"$TEST_SESSION_ID"'","cwd":"/x"}'
    [[ "$status" -eq 1 ]]   # runtime-ignored → suppressed
}

@test "hook_init: normal session (no ignore) returns 0" {
    run ccm_hook_init <<< '{"session_id":"'"$TEST_SESSION_ID"'","cwd":"/x"}'
    [[ "$status" -eq 0 ]]
}

# ─── Foreign-harness gate (Grok Build reads ~/.claude/settings.json) ───

@test "hook_init: Grok Build payload is rejected (workspaceRoot discriminator)" {
    # Verbatim field shape from grok-build user-guide/10-hooks.md: the
    # camelCase sessionId would pass the dual-case extraction below,
    # so without the gate this payload would key signals and events
    # under a Grok session id.
    run ccm_hook_init <<< '{"hookEventName":"pre_tool_use","sessionId":"abc-123","cwd":"/x","workspaceRoot":"/x","permissionMode":"default","timestamp":"2026-08-05T00:00:00Z"}'
    [[ "$status" -eq 1 ]]
}

@test "on-pre-tool-use.sh: Grok payload writes no signal and no event log" {
    printf '%s' '{"hookEventName":"pre_tool_use","sessionId":"abc-123","cwd":"/x/test-project","workspaceRoot":"/x/test-project"}' \
        | bash "${CCM_ROOT}/hooks/on-pre-tool-use.sh"
    [[ ! -f "${HOOK_DIR}/abc-123" ]]
    [[ ! -f "${HOOK_DIR}/abc-123.events.jsonl" ]]
}

@test "hook_init: workspaceRoot gate does not reject Claude payloads" {
    # A Claude payload never carries workspaceRoot; assert the gate is
    # keyed on that field and not on any coincidental shape, by running
    # the richest Claude-form payload we consume.
    run ccm_hook_init <<< '{"session_id":"'"$TEST_SESSION_ID"'","cwd":"/x","permission_mode":"acceptEdits","hook_event_name":"PreToolUse"}'
    [[ "$status" -eq 0 ]]
}

@test "on-pre-tool-use.sh: CCM_IGNORE'd session writes no event log" {
    export TMUX_PANE="%5"
    printf '%s' "$(_with_session '{"hook_event_name":"PreToolUse","cwd":"/x/test-project"}')" \
        | env CCM_IGNORE=1 bash "${CCM_ROOT}/hooks/on-pre-tool-use.sh"
    [[ ! -f "$EVENTS_FILE" ]]   # no event recorded for an ignored session
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

# ─── ccm_write_signal() status-bar push ───
# On a real state TRANSITION, ccm_write_signal spawns
# `ccm inject-status --fast` (backgrounded) so mode-0/2 status
# surfaces re-render from the just-written @ccm_prev_state instead
# of lagging one status-interval. Gated on transition so BUSY→BUSY
# bursts (PreToolUse/PostToolUse pairs) spawn nothing. CCM_BIN
# points the spawn at a recording stub here.

_setup_push_stub() {
    CCM_STUB_LOG="${MOCK_STATE_DIR}/push-stub.log"
    CCM_BIN="${MOCK_DIR}/bin/ccm-push-stub"
    printf '#!/usr/bin/env bash\necho "$@" >> "%s"\n' "$CCM_STUB_LOG" > "$CCM_BIN"
    chmod +x "$CCM_BIN"
    export CCM_BIN CCM_STUB_LOG
}

_wait_for_stub_log() {
    local i
    for i in $(seq 1 20); do
        [[ -s "$CCM_STUB_LOG" ]] && return 0
        sleep 0.1
    done
    return 1
}

@test "write_signal: state transition spawns inject-status push" {
    _setup_push_stub
    printf 'sess:1\t/x/test-project\ttest-project\tIDLE\n' \
        > "${MOCK_STATE_DIR}/windows"
    ccm_write_signal "$HOOK_DIR" "$KEY" "BUSY" "/x/test-project"
    _wait_for_stub_log
    grep -q -- "inject-status --fast" "$CCM_STUB_LOG"
}

@test "write_signal: same-state repeat does not spawn push" {
    _setup_push_stub
    printf 'sess:1\t/x/test-project\ttest-project\tBUSY\n' \
        > "${MOCK_STATE_DIR}/windows"
    ccm_write_signal "$HOOK_DIR" "$KEY" "BUSY" "/x/test-project"
    sleep 0.5
    [[ ! -s "$CCM_STUB_LOG" ]]
}

@test "write_signal: no matching window spawns nothing" {
    _setup_push_stub
    : > "${MOCK_STATE_DIR}/windows"
    ccm_write_signal "$HOOK_DIR" "$KEY" "BUSY" "/x/test-project"
    sleep 0.5
    [[ ! -s "$CCM_STUB_LOG" ]]
}

@test "write_signal: foreign session id leaves the window untouched" {
    # Hooks are user-scope: a Claude Desktop / VS Code session opened
    # on the same directory fires them too. The window's cached
    # session id disagreeing with the firing session means the prompt
    # is not in this window — no state write, no push, no
    # notification. (2.1.233 made Notification hooks fire under
    # Desktop/VS Code, turning this from theoretical into routine.)
    _setup_push_stub
    printf 'sess:1\t/x/test-project\ttest-project\tIDLE\tother-session-uuid\n' \
        > "${MOCK_STATE_DIR}/windows"
    ccm_write_signal "$HOOK_DIR" "$KEY" "PERMIT" "/x/test-project"
    sleep 0.5
    [[ ! -s "$CCM_STUB_LOG" ]]
    # The signal file itself IS written — keyed by the firing session,
    # which no tmux reader resolves to.
    [[ -f "${HOOK_DIR}/${KEY}" ]]
}

@test "write_signal: matching session id still updates the window" {
    _setup_push_stub
    printf 'sess:1\t/x/test-project\ttest-project\tIDLE\t%s\n' "$KEY" \
        > "${MOCK_STATE_DIR}/windows"
    ccm_write_signal "$HOOK_DIR" "$KEY" "BUSY" "/x/test-project"
    _wait_for_stub_log
    grep -q -- "inject-status --fast" "$CCM_STUB_LOG"
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

@test "on-pre-tool-use.sh: payload permission_mode lands on event record" {
    _run_hook on-pre-tool-use.sh \
        '{"hook_event_name":"PreToolUse","cwd":"/x/test-project","permission_mode":"acceptEdits"}'
    run jq -rs '.[-1].mode' "$EVENTS_FILE"
    [[ "$output" == "acceptEdits" ]]
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

# ─── idle_prompt BUSY-signal age guard ───
# idle_prompt is delivered 10-60s+ late (anthropics/claude-code#5186),
# so a BUSY signal written a few seconds ago (PreToolUse of a session
# that is actively working) must survive. Only a BUSY signal OLDER
# than CCM_IDLE_PROMPT_GUARD_SEC (default 60) may be cleared — an
# older one necessarily predates the point where Claude decided the
# session was idle. The pre-fix guard (`ts >= NOW`) only protected
# signals from the same second, so a 5-second-old legitimate BUSY
# signal was deleted and a working session falsely dropped to IDLE
# (which feeds auto-exit's kill path).

_run_idle_prompt() {
    printf '%s' "$(_with_session \
        '{"hook_event_name":"Notification","notification_type":"idle_prompt","cwd":"/x/test-project"}')" \
        | env "$@" bash "${CCM_ROOT}/hooks/on-notification.sh"
}

@test "on-notification.sh: idle_prompt keeps a fresh BUSY signal" {
    # BUSY written ~now (e.g. a PreToolUse from ongoing work that
    # started after Claude queued the delayed idle_prompt).
    printf '%s BUSY' "$(date +%s)" > "${HOOK_DIR}/${KEY}"
    _run_idle_prompt
    [[ -f "${HOOK_DIR}/${KEY}" ]]
    run cat "${HOOK_DIR}/${KEY}"
    [[ "$output" == *"BUSY"* ]]
}

@test "on-notification.sh: idle_prompt clears a stale BUSY signal" {
    # BUSY written 120s ago — older than the maximum documented
    # idle_prompt delay, so it genuinely belongs to the idle period.
    printf '%s BUSY' "$(( $(date +%s) - 120 ))" > "${HOOK_DIR}/${KEY}"
    _run_idle_prompt
    [[ ! -f "${HOOK_DIR}/${KEY}" ]]
}

@test "on-notification.sh: CCM_IDLE_PROMPT_GUARD_SEC=0 restores clear-on-arrival" {
    # Operators who run a Claude Code build without the idle_prompt
    # delay can opt out of the guard entirely.
    printf '%s BUSY' "$(date +%s)" > "${HOOK_DIR}/${KEY}"
    _run_idle_prompt CCM_IDLE_PROMPT_GUARD_SEC=0
    [[ ! -f "${HOOK_DIR}/${KEY}" ]]
}

@test "on-notification.sh: idle_prompt clears a corrupt BUSY signal" {
    # A non-numeric timestamp must fail OPEN (delete) rather than
    # silently disable the guard forever.
    printf 'garbage BUSY' > "${HOOK_DIR}/${KEY}"
    _run_idle_prompt
    [[ ! -f "${HOOK_DIR}/${KEY}" ]]
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
