#!/usr/bin/env bats
# Tests for hook setup/removal (lib/common.sh)

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

setup() {
    MOCK_DIR="$(mktemp -d)"
    export MOCK_SETTINGS="${MOCK_DIR}/settings.json"
    export HOME="$MOCK_DIR"
    mkdir -p "${MOCK_DIR}/.claude"

    # Hermetic claude stub: ccm_setup_hooks requires `claude --version`
    # on PATH, so these tests previously depended on the host's real
    # claude binary (green locally, red on CI runners without one).
    # Version-gate tests prepend their own bin to PATH, which overrides
    # this stub.
    mkdir -p "${MOCK_DIR}/bin"
    printf '#!/bin/bash\necho "2.1.218 (Claude Code)"\n' > "${MOCK_DIR}/bin/claude"
    chmod +x "${MOCK_DIR}/bin/claude"
    # Hermetic tmux stub: ccm_write_signal runs `tmux list-windows` etc.
    # for instant status updates, and under `set -euo pipefail` a missing
    # tmux binary kills the hook with 127 (2>/dev/null hides the message,
    # not the exit status). CI runners have no tmux. A no-op stub yields
    # an empty window list — the "no matching window" path — so the hook
    # proceeds normally.
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_DIR}/bin/tmux"
    chmod +x "${MOCK_DIR}/bin/tmux"
    export PATH="${MOCK_DIR}/bin:${PATH}"

    source "${CCM_ROOT}/lib/common.sh"

    # Override ccm_init_dirs to avoid creating real directories
    ccm_init_dirs() { :; }
    # Override die to not exit
    ccm_die() { echo "ERROR: $1" >&2; return 1; }
}

teardown() {
    [[ -n "$MOCK_DIR" && -d "$MOCK_DIR" ]] && rm -rf "$MOCK_DIR"
}

# ============================================================
# ccm_setup_hooks tests
# ============================================================

@test "setup-hooks: creates settings.json when none exists" {
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ -f "${MOCK_DIR}/.claude/settings.json" ]]
    # Verify hooks are present
    local hook_count
    hook_count=$(jq '.hooks.UserPromptSubmit | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$hook_count" -eq 1 ]]
    hook_count=$(jq '.hooks.Stop | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$hook_count" -eq 1 ]]
}

@test "setup-hooks: preserves existing settings" {
    echo '{"permissions":{"allow":["Read"]}}' > "${MOCK_DIR}/.claude/settings.json"
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    # Verify existing settings preserved
    local allow
    allow=$(jq -r '.permissions.allow[0]' "${MOCK_DIR}/.claude/settings.json")
    [[ "$allow" == "Read" ]]
    # Verify hooks added
    local hook_count
    hook_count=$(jq '.hooks.UserPromptSubmit | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$hook_count" -eq 1 ]]
}

@test "setup-hooks: idempotent (no duplicates on re-run)" {
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    # Should still be exactly 1 hook each
    local hook_count
    hook_count=$(jq '.hooks.UserPromptSubmit | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$hook_count" -eq 1 ]]
    hook_count=$(jq '.hooks.Stop | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$hook_count" -eq 1 ]]
}

@test "setup-hooks: creates backup file" {
    echo '{"existing":true}' > "${MOCK_DIR}/.claude/settings.json"
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ -f "${MOCK_DIR}/.claude/settings.json.bak" ]]
}

@test "setup-hooks: hook commands point to correct scripts" {
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    local prompt_cmd stop_cmd session_end_cmd
    prompt_cmd=$(jq -r '.hooks.UserPromptSubmit[0].hooks[0].command' "${MOCK_DIR}/.claude/settings.json")
    stop_cmd=$(jq -r '.hooks.Stop[0].hooks[0].command' "${MOCK_DIR}/.claude/settings.json")
    session_end_cmd=$(jq -r '.hooks.SessionEnd[0].hooks[0].command' "${MOCK_DIR}/.claude/settings.json")
    [[ "$prompt_cmd" == *"/hooks/on-prompt-submit.sh" ]]
    [[ "$stop_cmd" == *"/hooks/on-stop.sh" ]]
    [[ "$session_end_cmd" == *"/hooks/on-session-end.sh" ]]
}

# Re-source common.sh with CCM_HOOK_CMD_TIMEOUT as given, restoring the
# test overrides that sourcing replaces.
_resource_with_timeout() {
    if [[ "$1" == "unset" ]]; then unset CCM_HOOK_CMD_TIMEOUT; else export CCM_HOOK_CMD_TIMEOUT="$1"; fi
    source "${CCM_ROOT}/lib/common.sh"
    ccm_init_dirs() { :; }
    ccm_die() { echo "ERROR: $1" >&2; return 1; }
}

_timeout_of() {
    jq ".hooks.$1[0].hooks[0].timeout" "${MOCK_DIR}/.claude/settings.json"
}

# Another tool's hooks, added the ways they really appear: one inside
# ccm's own Stop matcher entry, and one under its own matcher whose
# script shares a ccm script's name in a different directory. Plus an
# unrelated setting.
_add_peers() {
    local f="${MOCK_DIR}/.claude/settings.json"
    jq '.hooks.Stop[0].hooks += [{"type":"command","command":"/peer/independent.sh","timeout":600}]
        | .hooks.Stop += [{"matcher":"x","hooks":[{"type":"command","command":"/peer/hooks/on-stop.sh","timeout":600}]}]
        | .model = "keep"' "$f" > "$f.new" && mv "$f.new" "$f"
}

# Every /peer/ hook with its entry's matcher, plus the unrelated setting.
_peers() {
    jq -S '{model, peers: ([.hooks[]?[]? | . as $e | .hooks[]? | select(.command | startswith("/peer/"))
             | {matcher: $e.matcher, hook: .}] | sort_by(.hook.command))}' "${MOCK_DIR}/.claude/settings.json"
}

# "<count> <distinct timeouts>" of the hooks whose command starts with $1.
_hooks_under() {
    jq -r --arg d "$1" '[.hooks[]?[]?.hooks[]? | select(.command | startswith($d)) | .timeout]
        | "\(length) \(unique | map(tostring) | join(","))"' "${MOCK_DIR}/.claude/settings.json"
}

# Rewrite ccm command paths: every one ($3 = all) or only the first Stop entry's ($3 = stop).
_respell_ccm_paths() {
    local f="${MOCK_DIR}/.claude/settings.json"
    if [[ "$3" == "stop" ]]; then
        jq --arg from "$1" --arg to "$2" '.hooks.Stop[0].hooks[0].command |= sub("^" + $from; $to)' "$f" > "$f.new"
    else
        jq --arg from "$1" --arg to "$2" '(.hooks[]?[]?.hooks[]? | .command) |= (if startswith($from) then $to + ltrimstr($from) else . end)' "$f" > "$f.new"
    fi
    mv "$f.new" "$f"
}

@test "setup-hooks: the default timeout is 5 seconds" {
    # The field is in seconds; the default is pinned here with the
    # variable unset, so it cannot pass by reading back whatever the
    # variable happens to hold.
    _resource_with_timeout unset
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$(_timeout_of UserPromptSubmit)" == "5" ]]
    [[ "$(_timeout_of SessionEnd)" == "5" ]]
    [[ "$(_timeout_of Stop)" == "5" ]]
}

@test "setup-hooks: an explicit CCM_HOOK_CMD_TIMEOUT is written as given" {
    _resource_with_timeout 7
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$(_timeout_of UserPromptSubmit)" == "7" ]]
}

@test "setup-hooks: on an existing install, updates ccm's timeouts and nothing else" {
    # An install written with the old millisecond value, then another
    # tool's hooks added the ways they really appear: one inside ccm's
    # own Stop matcher entry, one under its own matcher with a script
    # that shares a ccm basename in a different directory. Plus an
    # unrelated setting.
    _resource_with_timeout 5000
    run ccm_setup_hooks
    local f="${MOCK_DIR}/.claude/settings.json"
    jq '.hooks.Stop[0].hooks += [{"type":"command","command":"/peer/independent.sh","timeout":600}]
        | .hooks.Stop += [{"matcher":"x","hooks":[{"type":"command","command":"/peer/hooks/on-stop.sh","timeout":600}]}]
        | .model = "keep"' "$f" > "$f.new" && mv "$f.new" "$f"
    local before; before=$(jq -S . "$f")

    _resource_with_timeout unset
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]

    # every one of ccm's 16 hook commands carries the current default ...
    [[ "$(_hooks_under "${CCM_ROOT}/hooks/")" == "16 5" ]]

    # ... and putting the old value back on ccm's commands alone gives
    # the file as it was: nothing else changed, peers included.
    local restored; restored=$(jq -S --arg d "${CCM_ROOT}/hooks/" '
        .hooks |= with_entries(.value |= map(
            if (.hooks | type) == "array" then
                .hooks |= map(if (.command | type) == "string" and (.command | startswith($d))
                              then .timeout = 5000 else . end)
            else . end))' "$f")
    [[ "$restored" == "$before" ]]
}

@test "setup-hooks: on an install already current, the file is left alone" {
    _resource_with_timeout unset
    run ccm_setup_hooks
    local f="${MOCK_DIR}/.claude/settings.json"
    local before; before=$(cat "$f")
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"already installed"* ]]
    [[ "$(cat "$f")" == "$before" ]]
}

@test "remove-hooks: removes ccm hooks" {
    ccm_setup_hooks >/dev/null 2>&1
    run ccm_remove_hooks
    [[ "$status" -eq 0 ]]
    # Hooks should be gone
    local has_hooks
    has_hooks=$(jq 'has("hooks")' "${MOCK_DIR}/.claude/settings.json")
    [[ "$has_hooks" == "false" ]]
}

@test "remove-hooks: preserves non-ccm hooks" {
    # Setup with ccm hooks + a custom hook
    ccm_setup_hooks >/dev/null 2>&1
    # Add a non-ccm hook to UserPromptSubmit
    local settings
    settings=$(jq '.hooks.UserPromptSubmit += [{"hooks":[{"type":"command","command":"/usr/local/bin/my-hook.sh"}]}]' "${MOCK_DIR}/.claude/settings.json")
    echo "$settings" > "${MOCK_DIR}/.claude/settings.json"

    run ccm_remove_hooks
    [[ "$status" -eq 0 ]]
    # Custom hook should survive
    local remaining
    remaining=$(jq '.hooks.UserPromptSubmit | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$remaining" -eq 1 ]]
    local cmd
    cmd=$(jq -r '.hooks.UserPromptSubmit[0].hooks[0].command' "${MOCK_DIR}/.claude/settings.json")
    [[ "$cmd" == "/usr/local/bin/my-hook.sh" ]]
}

@test "remove-hooks: no error when no settings file" {
    run ccm_remove_hooks
    [[ "$status" -eq 0 ]]
}

@test "remove-hooks: creates backup" {
    ccm_setup_hooks >/dev/null 2>&1
    run ccm_remove_hooks
    [[ "$status" -eq 0 ]]
    [[ -f "${MOCK_DIR}/.claude/settings.json.bak" ]]
}

# ============================================================
# _ccm_strip_hooks tests
# ============================================================

# ============================================================
# ccm_hooks_configured tests
# ============================================================

@test "hooks-configured: returns true when hooks are installed" {
    ccm_setup_hooks >/dev/null 2>&1
    run ccm_hooks_configured
    [[ "$status" -eq 0 ]]
}

@test "hooks-configured: returns false when no settings file" {
    run ccm_hooks_configured
    [[ "$status" -ne 0 ]]
}

@test "hooks-configured: returns false after remove-hooks" {
    ccm_setup_hooks >/dev/null 2>&1
    ccm_remove_hooks >/dev/null 2>&1
    run ccm_hooks_configured
    [[ "$status" -ne 0 ]]
}

@test "hooks-configured: returns false when PostToolUseFailure missing" {
    # All script paths present but PostToolUseFailure event is not
    # registered — the configured-check must catch this and force a
    # reinstall.
    ccm_setup_hooks >/dev/null 2>&1
    jq 'del(.hooks.PostToolUseFailure)' "${MOCK_DIR}/.claude/settings.json" \
        > "${MOCK_DIR}/.claude/settings.json.tmp"
    mv "${MOCK_DIR}/.claude/settings.json.tmp" "${MOCK_DIR}/.claude/settings.json"
    run ccm_hooks_configured
    [[ "$status" -ne 0 ]]
}

@test "setup-hooks: reinstalls when PostToolUseFailure missing" {
    # Same scenario: event-level gap should trigger update path.
    ccm_setup_hooks >/dev/null 2>&1
    jq 'del(.hooks.PostToolUseFailure)' "${MOCK_DIR}/.claude/settings.json" \
        > "${MOCK_DIR}/.claude/settings.json.tmp"
    mv "${MOCK_DIR}/.claude/settings.json.tmp" "${MOCK_DIR}/.claude/settings.json"
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$output" != *"already installed"* ]]
    local hook_count
    hook_count=$(jq '.hooks.PostToolUseFailure | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$hook_count" -eq 1 ]]
}

@test "setup-hooks: registers SubagentStop / PreCompact / PostCompact" {
    ccm_setup_hooks >/dev/null 2>&1
    local n
    for ev in SubagentStop PreCompact PostCompact; do
        n=$(jq ".hooks.${ev} | length" "${MOCK_DIR}/.claude/settings.json")
        [[ "$n" -eq 1 ]] || { echo "missing event: $ev"; return 1; }
    done
}

@test "hooks-configured: returns false when SubagentStop missing" {
    ccm_setup_hooks >/dev/null 2>&1
    jq 'del(.hooks.SubagentStop)' "${MOCK_DIR}/.claude/settings.json" \
        > "${MOCK_DIR}/.claude/settings.json.tmp"
    mv "${MOCK_DIR}/.claude/settings.json.tmp" "${MOCK_DIR}/.claude/settings.json"
    run ccm_hooks_configured
    [[ "$status" -ne 0 ]]
}

@test "hooks-configured: returns false when PreCompact missing" {
    ccm_setup_hooks >/dev/null 2>&1
    jq 'del(.hooks.PreCompact)' "${MOCK_DIR}/.claude/settings.json" \
        > "${MOCK_DIR}/.claude/settings.json.tmp"
    mv "${MOCK_DIR}/.claude/settings.json.tmp" "${MOCK_DIR}/.claude/settings.json"
    run ccm_hooks_configured
    [[ "$status" -ne 0 ]]
}

@test "remove-hooks: strips SubagentStop / PreCompact / PostCompact" {
    ccm_setup_hooks >/dev/null 2>&1
    ccm_remove_hooks >/dev/null 2>&1
    # All ccm-added events should be gone
    for ev in SubagentStop PreCompact PostCompact PostToolUseFailure; do
        local present
        present=$(jq -r "has(\"hooks\") and (.hooks | has(\"${ev}\"))" \
            "${MOCK_DIR}/.claude/settings.json")
        [[ "$present" == "false" ]] || { echo "leftover: $ev"; return 1; }
    done
}

@test "remove-hooks: strips all 3 Notification matchers (permission_prompt/idle_prompt/elicitation_dialog)" {
    ccm_setup_hooks >/dev/null 2>&1
    ccm_remove_hooks >/dev/null 2>&1
    # Notification section itself should be gone (or contain no ccm matchers)
    local has_notification
    has_notification=$(jq -r '.hooks.Notification // "absent" | if . == "absent" then "absent" else "present" end' \
        "${MOCK_DIR}/.claude/settings.json")
    [[ "$has_notification" == "absent" ]] || {
        # If still present, ensure no ccm-pointing matcher remains
        local n_ccm
        n_ccm=$(jq '[.hooks.Notification[]? | select(.hooks | any(.command | test("on-notification\\.sh")))] | length' \
            "${MOCK_DIR}/.claude/settings.json")
        [[ "$n_ccm" -eq 0 ]] || { echo "leftover Notification matcher pointing to on-notification.sh"; return 1; }
    }
}

@test "remove-hooks: preserves non-ccm Notification matchers when removing" {
    ccm_setup_hooks >/dev/null 2>&1
    # Inject a foreign Notification matcher that points elsewhere
    jq '.hooks.Notification += [{"matcher": "permission_prompt", "hooks": [{"type": "command", "command": "/usr/local/bin/my-foreign-notify.sh"}]}]' \
        "${MOCK_DIR}/.claude/settings.json" > "${MOCK_DIR}/.claude/settings.json.tmp"
    mv "${MOCK_DIR}/.claude/settings.json.tmp" "${MOCK_DIR}/.claude/settings.json"
    ccm_remove_hooks >/dev/null 2>&1
    # Foreign matcher must survive
    local n_foreign
    n_foreign=$(jq '[.hooks.Notification[]? | select(.hooks | any(.command | test("my-foreign-notify\\.sh")))] | length' \
        "${MOCK_DIR}/.claude/settings.json")
    [[ "$n_foreign" -eq 1 ]] || { echo "expected 1 foreign matcher, got $n_foreign"; return 1; }
}

@test "setup-hooks: registers Notification elicitation_dialog matcher" {
    ccm_setup_hooks >/dev/null 2>&1
    local n
    n=$(jq '[.hooks.Notification[] | select(.matcher == "elicitation_dialog")] | length' \
        "${MOCK_DIR}/.claude/settings.json")
    [[ "$n" -eq 1 ]]
}

@test "hooks-configured: returns false when elicitation_dialog matcher missing" {
    ccm_setup_hooks >/dev/null 2>&1
    jq '.hooks.Notification = [.hooks.Notification[] | select(.matcher != "elicitation_dialog")]' \
        "${MOCK_DIR}/.claude/settings.json" > "${MOCK_DIR}/.claude/settings.json.tmp"
    mv "${MOCK_DIR}/.claude/settings.json.tmp" "${MOCK_DIR}/.claude/settings.json"
    run ccm_hooks_configured
    [[ "$status" -ne 0 ]]
}

@test "version: _ccm_version_ge semver comparison" {
    _ccm_version_ge "2.1.107" "2.1.107"
    _ccm_version_ge "2.1.108" "2.1.107"
    _ccm_version_ge "2.2.0"   "2.1.107"
    _ccm_version_ge "3.0.0"   "2.1.107"
    ! _ccm_version_ge "2.1.106" "2.1.107"
    ! _ccm_version_ge "2.1.99"  "2.1.107"
    ! _ccm_version_ge "2.0.200" "2.1.107"
    # Empty version = unknown = too old (safer default)
    ! _ccm_version_ge "" "2.1.107"
}

@test "setup-hooks: hard-fails when claude is too old" {
    # Stub claude --version to a release below CCM_MIN_CLAUDE_VERSION
    local bin="${MOCK_DIR}/bin"
    mkdir -p "$bin"
    cat > "$bin/claude" <<'STUB'
#!/bin/bash
echo "2.1.103 (Claude Code)"
STUB
    chmod +x "$bin/claude"
    run env PATH="${bin}:${PATH}" bash -c "source '${CCM_ROOT}/lib/common.sh' && ccm_setup_hooks"
    [[ "$status" -ne 0 ]] || { echo "expected setup-hooks to fail on old claude"; return 1; }
    [[ "$output" == *"too old"* ]] || { echo "expected 'too old' in error; got: $output"; return 1; }

    # Settings file must NOT have been written (or at least not contain the new matcher)
    if [[ -f "${MOCK_DIR}/.claude/settings.json" ]]; then
        local n_elicit
        n_elicit=$(jq '[.hooks.Notification[]? | select(.matcher == "elicitation_dialog")] | length' \
            "${MOCK_DIR}/.claude/settings.json" 2>/dev/null || echo 0)
        [[ "$n_elicit" -eq 0 ]] || { echo "settings should not contain elicitation_dialog after hard-fail"; return 1; }
    fi
}

@test "setup-hooks: installs elicitation_dialog matcher when claude is v2.1.107" {
    local bin="${MOCK_DIR}/bin"
    mkdir -p "$bin"
    cat > "$bin/claude" <<'STUB'
#!/bin/bash
echo "2.1.107 (Claude Code)"
STUB
    chmod +x "$bin/claude"
    PATH="${bin}:${PATH}" ccm_setup_hooks >/dev/null 2>&1

    local n
    n=$(jq '[.hooks.Notification[] | select(.matcher == "elicitation_dialog")] | length' \
        "${MOCK_DIR}/.claude/settings.json")
    [[ "$n" -eq 1 ]] || { echo "expected 1 elicitation_dialog matcher, got $n"; return 1; }
}

@test "on-notification.sh: elicitation_dialog writes PERMIT signal" {
    # End-to-end: feed the actual hook script the JSON Claude Code would
    # send for an elicitation event, and verify the signal file content.
    # Hook signal files are now keyed on the session_id from the payload,
    # not md5(cwd) — so we pass an explicit session_id and check that
    # specific path.
    local hook_dir="${MOCK_DIR}/hooks-tap"
    local sid="11111111-1111-1111-1111-111111111111"
    mkdir -p "$hook_dir"
    TMPDIR="${MOCK_DIR}" \
        bash -c "cd '${CCM_ROOT}' && \
                 export TMPDIR='${MOCK_DIR}' && \
                 mkdir -p \"\$TMPDIR/ccm-\$UID/hooks\" && \
                 echo '{\"cwd\":\"/tmp/test-proj\",\"session_id\":\"${sid}\",\"notification_type\":\"elicitation_dialog\"}' \
                     | hooks/on-notification.sh"

    local signal_file="${MOCK_DIR}/ccm-${UID}/hooks/${sid}"
    [[ -f "$signal_file" ]] || { echo "signal file not written: $signal_file"; return 1; }
    grep -q ' PERMIT' "$signal_file" || { echo "expected PERMIT in signal file"; cat "$signal_file"; return 1; }
}

@test "on-notification.sh: permission_prompt writes PERMIT signal" {
    # Regression: ensure the existing matcher path still works after
    # the elicitation_dialog addition.
    local hook_dir="${MOCK_DIR}/hooks-tap2"
    local sid="22222222-2222-2222-2222-222222222222"
    mkdir -p "$hook_dir"
    TMPDIR="${MOCK_DIR}" \
        bash -c "cd '${CCM_ROOT}' && \
                 export TMPDIR='${MOCK_DIR}' && \
                 mkdir -p \"\$TMPDIR/ccm-\$UID/hooks\" && \
                 echo '{\"cwd\":\"/tmp/test-proj2\",\"session_id\":\"${sid}\",\"notification_type\":\"permission_prompt\"}' \
                     | hooks/on-notification.sh"

    local signal_file="${MOCK_DIR}/ccm-${UID}/hooks/${sid}"
    [[ -f "$signal_file" ]] || { echo "signal file not written"; return 1; }
    grep -q ' PERMIT' "$signal_file" || { echo "expected PERMIT"; return 1; }
}

@test "on-notification.sh: idle_prompt clears signal but never notifies" {
    # Claude Code's idle_prompt notification carries a documented
    # 10-60s+ delay (anthropics/claude-code#5186), so any notification
    # fired from this branch arrives long after the response actually
    # finished and reads as a phantom "very late" alert. The hook
    # therefore clears the signal file (driving the IDLE transition)
    # without firing _ccm_instant_notify; the authoritative completion
    # ping comes from on-stop.sh's grace-scheduled notification.
    #
    # We observe the per-project notify *marker* that
    # `_ccm_instant_notify` writes SYNCHRONOUSLY before any fork. If
    # the marker exists after the hook runs, the function was called
    # — the contract we're guarding.
    local cwd="/tmp/test-idle-${RANDOM}"
    local sid="33333333-3333-3333-3333-333333333333"

    # Pre-populate the signal file as if a prior BUSY hook wrote it,
    # so the test exercises the file-clear path. Use a stale ts so
    # the "skip if BUSY-and-future" guard doesn't short-circuit.
    mkdir -p "${MOCK_DIR}/ccm-${UID}/hooks"
    printf '%s BUSY' "$(($(date +%s) - 60))" > "${MOCK_DIR}/ccm-${UID}/hooks/${sid}"

    TMPDIR="${MOCK_DIR}" \
        bash -c "cd '${CCM_ROOT}' && \
                 export TMPDIR='${MOCK_DIR}' && \
                 echo '{\"cwd\":\"${cwd}\",\"session_id\":\"${sid}\",\"notification_type\":\"idle_prompt\"}' \
                     | hooks/on-notification.sh"

    # Brief wait to let any backgrounded `_ccm_instant_notify ... &`
    # write its marker (synchronous within the function).
    sleep 0.2

    local signal_file="${MOCK_DIR}/ccm-${UID}/hooks/${sid}"
    [[ ! -f "$signal_file" ]] || { echo "signal file should be deleted: $signal_file"; cat "$signal_file"; return 1; }

    local marker_file="${MOCK_DIR}/ccm-${UID}/notified/${key}"
    [[ ! -f "$marker_file" ]] || {
        echo "regression: idle_prompt path called _ccm_instant_notify"
        echo "marker contents:"
        cat "$marker_file"
        return 1
    }
}

@test "setup-hooks: skips when already installed" {
    ccm_setup_hooks >/dev/null 2>&1
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"already installed"* ]]
}

@test "setup-hooks: after a move, installs from the new place and names the old hooks without editing them" {
    ccm_setup_hooks >/dev/null 2>&1
    local f="${MOCK_DIR}/.claude/settings.json"
    local old_root="$CCM_ROOT"
    local old_hooks; old_hooks=$(jq -c --arg d "${old_root}/hooks/" '[.hooks[]?[]?.hooks[]? | select(.command | startswith($d))]' "$f")
    [[ "$(echo "$old_hooks" | jq length)" -eq 16 ]]

    # Simulate the plugin moving: the old checkout is still on disk, and
    # nothing in the settings can tell it apart from another tool.
    CCM_ROOT="${MOCK_DIR}/new-location"
    mkdir -p "${CCM_ROOT}/hooks"
    cp "${old_root}/hooks/"*.sh "${CCM_ROOT}/hooks/"

    run ccm_setup_hooks
    CCM_ROOT="$old_root"
    [[ "$status" -eq 0 ]]
    [[ "$(_hooks_under "${MOCK_DIR}/new-location/hooks/")" == "16 5" ]]
    [[ "$(jq -c --arg d "${old_root}/hooks/" '[.hooks[]?[]?.hooks[]? | select(.command | startswith($d))]' "$f")" == "$old_hooks" ]]
    [[ "$output" == *"Left in place"* ]]
    [[ "$output" == *"${old_root}/hooks/on-stop.sh"* ]]
}

# ============================================================
# _ccm_strip_hooks tests
# ============================================================

@test "_ccm_strip_hooks: strips ccm hooks from JSON" {
    local input
    input=$(jq -nc --arg h "${CCM_ROOT}/hooks" '{hooks:{UserPromptSubmit:[{hooks:[{type:"command",command:($h+"/on-prompt-submit.sh")}]}],Stop:[{hooks:[{type:"command",command:($h+"/on-stop.sh")}]}]}}')
    local result
    result=$(echo "$input" | _ccm_strip_hooks)
    local has_hooks
    has_hooks=$(echo "$result" | jq 'has("hooks")')
    [[ "$has_hooks" == "false" ]]
}

@test "_ccm_strip_hooks: preserves non-ccm hooks" {
    local input
    input=$(jq -nc --arg h "${CCM_ROOT}/hooks" '{hooks:{UserPromptSubmit:[{hooks:[{type:"command",command:($h+"/on-prompt-submit.sh")}]},{hooks:[{type:"command",command:"/other/hook.sh"}]}]}}')
    local result
    result=$(echo "$input" | _ccm_strip_hooks)
    local count
    count=$(echo "$result" | jq '.hooks.UserPromptSubmit | length')
    [[ "$count" -eq 1 ]]
}

@test "_ccm_strip_hooks: removes ccm's hook alone, keeping the tool beside it and a look-alike" {
    # A script named like ccm's is not ccm's for that alone: here /path
    # does not exist and is tied to a single ccm script, so it is kept.
    local input result
    input=$(jq -nc --arg h "${CCM_ROOT}/hooks" '{hooks:{Stop:[
        {matcher:"m", hooks:[{type:"command",command:($h+"/on-stop.sh")},{type:"command",command:"/peer/x.sh"}]},
        {hooks:[{type:"command",command:"/path/on-stop.sh"}]}]}}')
    result=$(echo "$input" | _ccm_strip_hooks)
    [[ "$(echo "$result" | jq -c .hooks.Stop)" == '[{"matcher":"m","hooks":[{"type":"command","command":"/peer/x.sh"}]},{"hooks":[{"type":"command","command":"/path/on-stop.sh"}]}]' ]]
}

@test "setup-hooks: an install missing an event is rebuilt without losing another tool's hooks" {
    _resource_with_timeout 5000
    run ccm_setup_hooks
    _add_peers
    local f="${MOCK_DIR}/.claude/settings.json"
    jq 'del(.hooks.PostCompact)' "$f" > "$f.new" && mv "$f.new" "$f"
    local peers_before; peers_before=$(_peers)
    _resource_with_timeout unset
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$(_peers)" == "$peers_before" ]]
    [[ "$(_hooks_under "${CCM_ROOT}/hooks/")" == "16 5" ]]
}

@test "setup-hooks: an install written through a symlinked path is rebuilt without losing another tool's hooks" {
    _resource_with_timeout 5000
    run ccm_setup_hooks
    _add_peers
    ln -s "${CCM_ROOT}/hooks" "${MOCK_DIR}/alias-hooks"
    _respell_ccm_paths "${CCM_ROOT}/hooks/" "${MOCK_DIR}/alias-hooks/" all
    local peers_before; peers_before=$(_peers)
    _resource_with_timeout unset
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$(_peers)" == "$peers_before" ]]
    [[ "$(_hooks_under "${CCM_ROOT}/hooks/")" == "16 5" ]]
    [[ "$(_hooks_under "${MOCK_DIR}/alias-hooks/")" == "0 " ]]
}

@test "setup-hooks: a single command written through a symlink is updated with the rest" {
    _resource_with_timeout 5000
    run ccm_setup_hooks
    _add_peers
    ln -s "${CCM_ROOT}/hooks" "${MOCK_DIR}/alias-hooks"
    _respell_ccm_paths "${CCM_ROOT}/hooks/" "${MOCK_DIR}/alias-hooks/" stop
    local peers_before; peers_before=$(_peers)
    _resource_with_timeout unset
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$(_peers)" == "$peers_before" ]]
    [[ "$(_hooks_under "${CCM_ROOT}/hooks/")" == "15 5" ]]
    [[ "$(_hooks_under "${MOCK_DIR}/alias-hooks/")" == "1 5" ]]
}

@test "setup-hooks: hooks in a directory that no longer exists are named, not removed" {
    # They may be a deleted ccm's, or another tool's whose scripts share
    # the names; the settings alone cannot say which.
    _resource_with_timeout unset
    run ccm_setup_hooks
    _respell_ccm_paths "${CCM_ROOT}/hooks/" "${MOCK_DIR}/gone/hooks/" all
    local f="${MOCK_DIR}/.claude/settings.json"
    local before; before=$(jq -S . "$f")
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$(_hooks_under "${MOCK_DIR}/gone/hooks/")" == "16 5" ]]
    [[ "$(_hooks_under "${CCM_ROOT}/hooks/")" == "16 5" ]]
    [[ "$(jq -S --arg d "${CCM_ROOT}/hooks/" 'del(.hooks[][] | select(.hooks[0].command | startswith($d)))' "$f")" == "$before" ]]
    [[ "$output" == *"${MOCK_DIR}/gone/hooks/on-stop.sh"* ]]
}

@test "setup-hooks: names the look-alike hooks it leaves in place" {
    _resource_with_timeout unset
    run ccm_setup_hooks
    _add_peers
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"Left in place"* ]]
    [[ "$output" == *"/peer/hooks/on-stop.sh"* ]]
    [[ "$output" != *"/peer/independent.sh"* ]]
}

@test "setup-hooks and remove-hooks: a settings file that does not parse is left alone, and so is the backup" {
    # An overlapping writer can leave the file cut off mid-object. The
    # backup is what recovers from that, so copying the broken file
    # over it first would destroy the one thing worth having.
    local f="${MOCK_DIR}/.claude/settings.json"
    mkdir -p "$(dirname "$f")"
    printf '{"good": true}\n' > "$f.bak"
    printf '{"hooks": {"Stop": [' > "$f"
    local broken; broken=$(cat "$f")
    run ccm_setup_hooks
    [[ "$status" -ne 0 ]]
    [[ "$(cat "$f")" == "$broken" ]]
    [[ "$(cat "$f.bak")" == '{"good": true}' ]]
    run ccm_remove_hooks
    [[ "$status" -ne 0 ]]
    [[ "$(cat "$f")" == "$broken" ]]
    [[ "$(cat "$f.bak")" == '{"good": true}' ]]
}

_assert_untouched() {   # $1 = content expected in settings.json
    local f="${MOCK_DIR}/.claude/settings.json"
    [[ "$(cat "$f")" == "$1" ]] || { echo "settings changed: $(cat "$f")"; return 1; }
    [[ "$(cat "$f.bak")" == '{"good": true}' ]] || { echo "backup changed: $(cat "$f.bak")"; return 1; }
}

@test "setup-hooks and remove-hooks: JSON that is not an object is not settings, and is left alone with its backup" {
    # `[]`, `123` and `null` parse, and jq builds an object out of
    # `null` without complaint — which is how a good backup got
    # replaced by `null` and hooks written over it.
    local f="${MOCK_DIR}/.claude/settings.json" content
    mkdir -p "$(dirname "$f")"
    for content in '[]' '123' 'null' '"text"'; do
        printf '{"good": true}' > "$f.bak"
        printf '%s' "$content" > "$f"
        run ccm_setup_hooks
        [[ "$status" -ne 0 ]] || { echo "setup accepted $content"; return 1; }
        _assert_untouched "$content"
        run ccm_remove_hooks
        [[ "$status" -ne 0 ]] || { echo "remove accepted $content"; return 1; }
        _assert_untouched "$content"
    done
}

@test "setup-hooks and remove-hooks: a settings path that is a broken symlink is not replaced" {
    local f="${MOCK_DIR}/.claude/settings.json"
    mkdir -p "$(dirname "$f")"
    printf '{"good": true}' > "$f.bak"
    ln -s "${MOCK_DIR}/missing-target" "$f"
    run ccm_setup_hooks
    [[ "$status" -ne 0 ]]
    [[ -L "$f" && ! -e "$f" ]]
    run ccm_remove_hooks
    [[ "$status" -ne 0 ]]
    [[ -L "$f" && ! -e "$f" ]]
    [[ "$(cat "$f.bak")" == '{"good": true}' ]]
}

@test "setup-hooks: the backup is the file as it was before this write" {
    local f="${MOCK_DIR}/.claude/settings.json"
    mkdir -p "$(dirname "$f")"
    printf '{"model": "keep"}\n' > "$f"
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$(jq -c . "$f.bak")" == '{"model":"keep"}' ]]
}

@test "remove-hooks: keeps another tool's hooks, even in ccm's matcher entry" {
    ccm_setup_hooks >/dev/null 2>&1
    _add_peers
    local peers_before; peers_before=$(_peers)
    run ccm_remove_hooks
    [[ "$status" -eq 0 ]]
    [[ "$(_peers)" == "$peers_before" ]]
    [[ "$(_hooks_under "${CCM_ROOT}/hooks/")" == "0 " ]]
}

# ============================================================
# Multi-turn Stop delayed-notify (pending sentinel flow)
# ============================================================
#
# These hooks cooperate to avoid firing a COMPLETED notification at
# every Stop, which Claude Code raises at each tool-call boundary:
#   - on-stop.sh writes `<key>.pending` and schedules a delayed check
#   - on-pre-tool-use.sh / on-prompt-submit.sh delete the sentinel
#     when new work begins, so the scheduled notify finds nothing
#
# The async timing (sleep 3) isn't exercised here — we only verify
# the synchronous sentinel I/O that the two sides depend on.

@test "on-stop.sh: writes .pending sentinel instead of immediate notify" {
    # on-stop.sh queries tmux to resolve the project name; shim a
    # minimal tmux so that list-windows returns a line for our
    # synthetic cwd. Without this, on-stop.sh finds no project and
    # skips the schedule step entirely.
    local tmp_home="${MOCK_DIR}/stop-home"
    local shim_dir="${MOCK_DIR}/stop-shim"
    mkdir -p "$tmp_home" "$shim_dir"
    local cwd="/tmp/stop-proj-a"
    local sid="44444444-4444-4444-4444-444444444444"
    cat > "${shim_dir}/tmux" << EOF
#!/usr/bin/env bash
if [[ "\$1" == "list-windows" ]]; then
    printf '%s\t%s\t%s\n' 'main:1' '$cwd' 'stop-proj-a'
fi
exit 0
EOF
    chmod +x "${shim_dir}/tmux"

    # CCM_COMPLETION_GRACE_SEC=99 keeps the detached subshell's
    # delayed-check from firing during the test window — we only
    # care that the sentinel was written synchronously.
    PATH="${shim_dir}:$PATH" TMPDIR="$tmp_home" HOME="$tmp_home" \
        CCM_COMPLETION_GRACE_SEC=99 \
        bash -c "cd '${CCM_ROOT}' && \
                 mkdir -p \"\$TMPDIR/ccm-\$UID/hooks\" && \
                 echo '{\"cwd\":\"$cwd\",\"session_id\":\"${sid}\"}' | hooks/on-stop.sh"

    local pending="${tmp_home}/ccm-${UID}/hooks/${sid}.pending"
    [[ -f "$pending" ]] || { echo "pending sentinel not written: $pending"; return 1; }
}

# Helper for the bg-pending-suppression tests: runs on-stop.sh with a
# minimal tmux shim that lets project resolution succeed, and returns
# whether the .pending sentinel was written. Caller passes the JSON
# payload fragment that follows `cwd`/`session_id` (i.e. extra fields).
_run_stop_with_payload_extras() {
    local extras="$1" tmp_home="$2" cwd="$3" sid="$4"
    local shim_dir="${MOCK_DIR}/stop-bg-shim-${RANDOM}"
    mkdir -p "$tmp_home" "$shim_dir"
    cat > "${shim_dir}/tmux" << EOF
#!/usr/bin/env bash
if [[ "\$1" == "list-windows" ]]; then
    printf '%s\t%s\t%s\n' 'main:1' '$cwd' 'proj-bg'
fi
exit 0
EOF
    chmod +x "${shim_dir}/tmux"

    local payload="{\"cwd\":\"$cwd\",\"session_id\":\"${sid}\"${extras:+,$extras}}"
    PATH="${shim_dir}:$PATH" TMPDIR="$tmp_home" HOME="$tmp_home" \
        CCM_COMPLETION_GRACE_SEC=99 \
        bash -c "cd '${CCM_ROOT}' && \
                 mkdir -p \"\$TMPDIR/ccm-\$UID/hooks\" && \
                 echo '$payload' | hooks/on-stop.sh"
}

@test "on-stop.sh: skips .pending when background_tasks is non-empty" {
    # Claude Code 2.1.145+ Stop payload includes background_tasks.
    # When the array is non-empty, the user has work still in flight
    # (e.g. /bg dispatch, async tool) and a COMPLETED notification at
    # this Stop would be premature — the grace period catches some
    # but not all (a /loop sleeping past 3 s would fire false alerts).
    # The guard is "skip when length > 0", which keeps legacy
    # payloads (field absent) on the original notify path.
    local tmp_home="${MOCK_DIR}/stop-bg-tasks"
    local sid="66666666-6666-6666-6666-666666666666"
    _run_stop_with_payload_extras \
        '"background_tasks":[{"id":"task1"}]' \
        "$tmp_home" "/tmp/stop-bg-tasks" "$sid"

    local pending="${tmp_home}/ccm-${UID}/hooks/${sid}.pending"
    [[ ! -f "$pending" ]] || {
        echo "pending sentinel should NOT have been written when background_tasks is non-empty: $pending"; return 1;
    }
}

@test "on-stop.sh: skips .pending when session_crons is non-empty" {
    # Same rationale as background_tasks but for /loop / /schedule
    # style scheduled wakeups — Claude is technically done with the
    # current turn but will resume autonomously, so the user's intent
    # is not yet "done".
    local tmp_home="${MOCK_DIR}/stop-bg-crons"
    local sid="77777777-7777-7777-7777-777777777777"
    _run_stop_with_payload_extras \
        '"session_crons":[{"name":"loop1"}]' \
        "$tmp_home" "/tmp/stop-bg-crons" "$sid"

    local pending="${tmp_home}/ccm-${UID}/hooks/${sid}.pending"
    [[ ! -f "$pending" ]] || {
        echo "pending sentinel should NOT have been written when session_crons is non-empty: $pending"; return 1;
    }
}

@test "on-stop.sh: writes .pending when bg-task fields are present-but-empty" {
    # Forward-compat: when Claude Code emits the fields but both are
    # empty arrays, the user genuinely has no outstanding work and we
    # should fire the COMPLETED notification as on legacy payloads.
    local tmp_home="${MOCK_DIR}/stop-bg-empty"
    local sid="88888888-8888-8888-8888-888888888888"
    _run_stop_with_payload_extras \
        '"background_tasks":[],"session_crons":[]' \
        "$tmp_home" "/tmp/stop-bg-empty" "$sid"

    local pending="${tmp_home}/ccm-${UID}/hooks/${sid}.pending"
    [[ -f "$pending" ]] || {
        echo "pending sentinel should have been written when both arrays are empty: $pending"; return 1;
    }
}

@test "on-pre-tool-use.sh: cancels pending sentinel from prior Stop" {
    local tmp_home="${MOCK_DIR}/pretool-home"
    local cwd="/tmp/pretool-proj"
    local sid="55555555-5555-5555-5555-555555555555"
    local hook_dir="${tmp_home}/ccm-${UID}/hooks"
    mkdir -p "$hook_dir"
    # Pre-seed the pending sentinel as if a Stop just fired
    printf '%s' "$(date +%s)" > "${hook_dir}/${sid}.pending"
    [[ -f "${hook_dir}/${sid}.pending" ]] || { echo "setup: pending missing"; return 1; }

    TMPDIR="$tmp_home" HOME="$tmp_home" \
        bash -c "cd '${CCM_ROOT}' && \
                 echo '{\"cwd\":\"$cwd\",\"session_id\":\"${sid}\"}' | hooks/on-pre-tool-use.sh"

    [[ ! -f "${hook_dir}/${sid}.pending" ]] || {
        echo "pending should have been cancelled"; return 1;
    }
}

@test "state_meta: ccm_state_icon agrees with Python STATE_ICONS" {
    # Single-source guard: bash lib/state_meta.sh and Python
    # lib/ccm_core.py::STATE_ICONS must stay aligned. We pull the
    # Python table at test time and compare every entry against
    # ccm_state_icon's output so drift is caught automatically.
    # COMPLETED is deliberately bash-only (notification-display
    # marker, not a detection state) and excluded from this check.
    source "${CCM_ROOT}/lib/state_meta.sh"

    local mismatches=""
    while IFS=$'\t' read -r state expected; do
        [[ -z "$state" ]] && continue
        local actual
        actual=$(ccm_state_icon "$state")
        if [[ "$actual" != "$expected" ]]; then
            mismatches+="${state}: bash=${actual} python=${expected}"$'\n'
        fi
    done < <(python3 -c "
import sys; sys.path.insert(0, '${CCM_ROOT}/lib')
from ccm_core import STATE_ICONS
for k, v in STATE_ICONS.items():
    print(f'{k}\t{v}')
")

    if [[ -n "$mismatches" ]]; then
        echo "STATE_ICONS drift between bash and Python:"
        echo "$mismatches"
        return 1
    fi

    # Unknown states fall back to IDLE's icon rather than erroring —
    # hook scripts must never fail a tmux rename because a new
    # upstream state name appeared.
    [[ "$(ccm_state_icon WHATEVER)" == "●" ]] || {
        echo "fallback mismatch: $(ccm_state_icon WHATEVER)"; return 1;
    }
    # Notification-only marker must also resolve.
    [[ "$(ccm_state_icon COMPLETED)" == "✔" ]] || {
        echo "COMPLETED missing: $(ccm_state_icon COMPLETED)"; return 1;
    }
}

@test "on-prompt-submit.sh: cancels pending sentinel on new turn" {
    local tmp_home="${MOCK_DIR}/prompt-home"
    local cwd="/tmp/prompt-proj"
    local sid="66666666-6666-6666-6666-666666666666"
    local hook_dir="${tmp_home}/ccm-${UID}/hooks"
    mkdir -p "$hook_dir"
    printf '%s' "$(date +%s)" > "${hook_dir}/${sid}.pending"

    TMPDIR="$tmp_home" HOME="$tmp_home" \
        bash -c "cd '${CCM_ROOT}' && \
                 echo '{\"cwd\":\"$cwd\",\"session_id\":\"${sid}\"}' | hooks/on-prompt-submit.sh"

    [[ ! -f "${hook_dir}/${sid}.pending" ]] || {
        echo "pending should have been cancelled by UserPromptSubmit"; return 1;
    }
}
