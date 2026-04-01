#!/usr/bin/env bats
# Tests for hook setup/removal (lib/common.sh)

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

setup() {
    MOCK_DIR="$(mktemp -d)"
    export MOCK_SETTINGS="${MOCK_DIR}/settings.json"
    export HOME="$MOCK_DIR"
    mkdir -p "${MOCK_DIR}/.claude"

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

@test "setup-hooks: timeout uses CCM_HOOK_CMD_TIMEOUT" {
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    local timeout
    timeout=$(jq '.hooks.UserPromptSubmit[0].hooks[0].timeout' "${MOCK_DIR}/.claude/settings.json")
    [[ "$timeout" -eq "${CCM_HOOK_CMD_TIMEOUT:-5000}" ]]
}

# ============================================================
# ccm_remove_hooks tests
# ============================================================

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

@test "setup-hooks: skips when already installed" {
    ccm_setup_hooks >/dev/null 2>&1
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"already installed"* ]]
}

@test "setup-hooks: auto-updates when hook paths change" {
    # Install hooks with current CCM_ROOT
    ccm_setup_hooks >/dev/null 2>&1

    # Verify current paths are recorded
    local orig_prompt_cmd
    orig_prompt_cmd=$(jq -r '.hooks.UserPromptSubmit[0].hooks[0].command' "${MOCK_DIR}/.claude/settings.json")
    [[ "$orig_prompt_cmd" == *"$CCM_ROOT"* ]]

    # Change CCM_ROOT to simulate plugin relocation
    local old_root="$CCM_ROOT"
    CCM_ROOT="${MOCK_DIR}/new-location"
    mkdir -p "${CCM_ROOT}/hooks"
    cp "${old_root}/hooks/on-prompt-submit.sh" "${CCM_ROOT}/hooks/"
    cp "${old_root}/hooks/on-stop.sh" "${CCM_ROOT}/hooks/"

    # Re-run setup-hooks — should detect path change and update
    run ccm_setup_hooks
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"paths changed"* ]]

    # Verify paths were updated to new location
    local new_prompt_cmd
    new_prompt_cmd=$(jq -r '.hooks.UserPromptSubmit[0].hooks[0].command' "${MOCK_DIR}/.claude/settings.json")
    [[ "$new_prompt_cmd" == *"new-location"* ]]

    # Verify no duplicates (still exactly 1 hook each)
    local hook_count
    hook_count=$(jq '.hooks.UserPromptSubmit | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$hook_count" -eq 1 ]]
    hook_count=$(jq '.hooks.Stop | length' "${MOCK_DIR}/.claude/settings.json")
    [[ "$hook_count" -eq 1 ]]

    # Restore
    CCM_ROOT="$old_root"
}

# ============================================================
# _ccm_strip_hooks tests
# ============================================================

@test "_ccm_strip_hooks: strips ccm hooks from JSON" {
    local input='{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"/path/on-prompt-submit.sh"}]}],"Stop":[{"hooks":[{"type":"command","command":"/path/on-stop.sh"}]}]}}'
    local result
    result=$(echo "$input" | _ccm_strip_hooks)
    local has_hooks
    has_hooks=$(echo "$result" | jq 'has("hooks")')
    [[ "$has_hooks" == "false" ]]
}

@test "_ccm_strip_hooks: preserves non-ccm hooks" {
    local input='{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"/path/on-prompt-submit.sh"}]},{"hooks":[{"type":"command","command":"/other/hook.sh"}]}]}}'
    local result
    result=$(echo "$input" | _ccm_strip_hooks)
    local count
    count=$(echo "$result" | jq '.hooks.UserPromptSubmit | length')
    [[ "$count" -eq 1 ]]
}
