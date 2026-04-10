#!/usr/bin/env bash
# ccm - common functions and constants
# This file provides bash-only helpers for: init wizard, hook setup/removal,
# dependency checks, and output formatting.
# All session management, snapshots, and state detection are in Python (lib/ccm_core.py).

# Runtime data directory
CCM_DATA_DIR="${HOME}/.local/share/ccm"
CCM_SNAPSHOT_DIR="${CCM_DATA_DIR}/snapshots"
CCM_STATE_DIR="${CCM_DATA_DIR}/state"

# Temp directory (user-scoped to avoid multi-user collisions)
CCM_TMP_DIR="${TMPDIR:-/tmp}/ccm-${UID}"

# Hook signal directory
CCM_HOOK_DIR="${CCM_TMP_DIR}/hooks"
# Timeout for hook commands in Claude Code settings (milliseconds)
CCM_HOOK_CMD_TIMEOUT=5000

# Commands to start Claude Code
CCM_CLAUDE_CMD="claude --continue 2>/dev/null || claude"

# Colors for terminal output (using $'...' for real escape characters)
COLOR_RED=$'\033[0;31m'
COLOR_GREEN=$'\033[0;32m'
COLOR_YELLOW=$'\033[1;33m'
COLOR_BLUE=$'\033[0;34m'
COLOR_CYAN=$'\033[0;36m'
COLOR_BOLD=$'\033[1m'
COLOR_DIM=$'\033[2m'
COLOR_RESET=$'\033[0m'

# Ensure runtime directories exist
ccm_init_dirs() {
    mkdir -p "$CCM_SNAPSHOT_DIR" "$CCM_STATE_DIR" "$CCM_TMP_DIR" "$CCM_HOOK_DIR" \
             "${CCM_TMP_DIR}/port-cache" "${CCM_TMP_DIR}/git-cache" 2>/dev/null

    # Clean up stale cache/lock files (older than 1 hour)
    find "$CCM_TMP_DIR" -maxdepth 2 -type f -mmin +60 -delete 2>/dev/null || true
    find "$CCM_TMP_DIR" -maxdepth 2 -type d -empty -mmin +60 -delete 2>/dev/null || true
}

# Print an error message and exit
ccm_die() {
    printf '%s\n' "${COLOR_RED}Error: $1${COLOR_RESET}" >&2
    exit 1
}

# Print a warning message
ccm_warn() {
    printf '%s\n' "${COLOR_YELLOW}Warning: $1${COLOR_RESET}" >&2
}

# Print an info message
ccm_info() {
    printf '%s\n' "${COLOR_GREEN}$1${COLOR_RESET}"
}

# Check dependencies
ccm_check_deps() {
    local missing=()
    for cmd in tmux python3 jq fzf claude; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        ccm_die "Missing dependencies: ${missing[*]}"
    fi
}

# Interactive initial setup wizard
# Works both inside and outside tmux (tmux-dependent steps show manual instructions)
ccm_init() {
    local in_tmux=0
    [[ -n "${TMUX:-}" ]] && in_tmux=1

    echo ""
    echo "  ${COLOR_BOLD}ccm — Claude Code Manager Setup${COLOR_RESET}"
    echo ""

    if [[ $in_tmux -eq 0 ]]; then
        echo "  ${COLOR_YELLOW}Note:${COLOR_RESET} Not inside tmux. Some settings (auto-restore, status bar)"
        echo "  can only be applied interactively inside a tmux session."
        echo "  ${COLOR_DIM}For the full setup experience: tmux → ccm init${COLOR_RESET}"
        echo ""
        echo -n "  Continue anyway? [Y/n] "
        local cont
        read -r cont
        if [[ -n "$cont" && ! "$cont" =~ ^[Yy] ]]; then
            return 0
        fi
        echo ""
    fi

    # Step 1: Dependency check
    echo "  ${COLOR_BOLD}[1/4] Checking dependencies${COLOR_RESET}"
    local all_ok=1
    for cmd in tmux python3 jq fzf claude; do
        if command -v "$cmd" &>/dev/null; then
            echo "    ${COLOR_GREEN}✔${COLOR_RESET} $cmd"
        else
            echo "    ${COLOR_RED}✘${COLOR_RESET} $cmd — not found"
            all_ok=0
        fi
    done
    if [[ $all_ok -eq 0 ]]; then
        echo ""
        echo "  ${COLOR_RED}Please install missing dependencies and re-run 'ccm init'.${COLOR_RESET}"
        return 1
    fi
    echo ""

    # Step 2: Claude Code hooks
    echo "  ${COLOR_BOLD}[2/4] Claude Code hooks${COLOR_RESET}"
    echo "    Hooks improve state detection accuracy (BUSY/DONE)."
    if ccm_hooks_configured; then
        echo "    ${COLOR_GREEN}✔${COLOR_RESET} Already installed"
    else
        echo -n "    Install hooks? [Y/n] "
        local answer
        read -r answer
        if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
            ccm_setup_hooks 2>/dev/null
            echo "    ${COLOR_GREEN}✔${COLOR_RESET} Installed"
        else
            echo "    ${COLOR_DIM}Skipped (you can run 'ccm setup-hooks' later)${COLOR_RESET}"
        fi
    fi
    echo ""

    # Helper: write a setting to ~/.tmux.conf BEFORE ccm plugin loads
    _ccm_save_tmux_conf() {
        local setting="$1"
        local key
        key=$(echo "$setting" | awk '{print $3}')
        sed -i.bak "/${key}/d" ~/.tmux.conf 2>/dev/null || true
        local insert_line=""
        local ccm_source
        ccm_source=$(grep -n 'source-file.*ccm' ~/.tmux.conf 2>/dev/null | head -1 | cut -d: -f1)
        local tpm_run
        tpm_run=$(grep -n '^run.*tpm' ~/.tmux.conf 2>/dev/null | head -1 | cut -d: -f1)
        if [[ -n "$ccm_source" && -n "$tpm_run" ]]; then
            insert_line=$(( ccm_source < tpm_run ? ccm_source : tpm_run ))
        elif [[ -n "$ccm_source" ]]; then
            insert_line="$ccm_source"
        elif [[ -n "$tpm_run" ]]; then
            insert_line="$tpm_run"
        fi
        if [[ -n "$insert_line" ]]; then
            sed -i.bak "${insert_line}i\\
${setting}
" ~/.tmux.conf 2>/dev/null
        else
            echo "$setting" >> ~/.tmux.conf
        fi
        rm -f ~/.tmux.conf.bak 2>/dev/null
    }

    # Step 3: Auto-restore
    echo "  ${COLOR_BOLD}[3/4] Auto-restore on tmux start${COLOR_RESET}"
    echo "    Automatically load the last workspace when tmux starts."
    if grep -q '@ccm-auto-restore on' ~/.tmux.conf 2>/dev/null; then
        echo "    ${COLOR_GREEN}✔${COLOR_RESET} Already enabled in ~/.tmux.conf"
    else
        echo -n "    Enable auto-restore? [Y/n] "
        local answer
        read -r answer
        if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
            [[ $in_tmux -eq 1 ]] && tmux set -g @ccm-auto-restore on 2>/dev/null
            _ccm_save_tmux_conf "set -g @ccm-auto-restore on"
            echo "    ${COLOR_GREEN}✔${COLOR_RESET} Saved to ~/.tmux.conf"
        else
            echo "    ${COLOR_DIM}Skipped${COLOR_RESET}"
        fi
    fi
    echo ""

    # Step 4: Status bar mode
    echo "  ${COLOR_BOLD}[4/4] Status bar mode${COLOR_RESET}"
    echo "    0 = Icon in status-right (default, minimal)"
    echo "    1 = Replace window list with ccm entries"
    echo "    2 = Dedicated status line with full details"
    local current_mode="0"
    if [[ $in_tmux -eq 1 ]]; then
        current_mode=$(tmux show-option -gqv @ccm-status-line 2>/dev/null)
        current_mode="${current_mode:-0}"
    elif grep -q '@ccm-status-line' ~/.tmux.conf 2>/dev/null; then
        current_mode=$(grep '@ccm-status-line' ~/.tmux.conf | awk '{print $NF}')
    fi
    echo "    Current: mode ${current_mode}"
    echo -n "    Choose mode [0/1/2] (Enter to keep current): "
    local mode_answer
    read -r mode_answer
    if [[ "$mode_answer" =~ ^[012]$ && "$mode_answer" != "$current_mode" ]]; then
        [[ $in_tmux -eq 1 ]] && tmux set -g @ccm-status-line "$mode_answer" 2>/dev/null
        _ccm_save_tmux_conf "set -g @ccm-status-line ${mode_answer}"
        echo "    ${COLOR_GREEN}✔${COLOR_RESET} Mode ${mode_answer} saved to ~/.tmux.conf"
    else
        echo "    ${COLOR_DIM}Keeping mode ${current_mode}${COLOR_RESET}"
    fi
    echo ""

    # Summary
    echo "  ${COLOR_BOLD}Setup complete!${COLOR_RESET}"
    echo ""
    if [[ $in_tmux -eq 1 ]]; then
        echo "  ${COLOR_DIM}Quick start:${COLOR_RESET}"
        echo "    ccm add ~/your-project    Add a project"
        echo "    prefix + Tab              Open dashboard"
    else
        echo "  ${COLOR_DIM}Next steps:${COLOR_RESET}"
        echo "    tmux                         Start tmux"
        echo "    ccm add ~/your-project       Add a project"
        echo "    prefix + Tab                 Open dashboard"
    fi
    echo ""
}

# Remove ccm hook entries from a settings JSON string (helper)
_ccm_strip_hooks() {
    jq '
        if .hooks then
            .hooks.UserPromptSubmit = [
                .hooks.UserPromptSubmit[]? |
                select(.hooks | any(.command | test("on-prompt-submit\\.sh")) | not)
            ] |
            .hooks.Stop = [
                .hooks.Stop[]? |
                select(.hooks | any(.command | test("on-stop\\.sh")) | not)
            ] |
            .hooks.StopFailure = [
                .hooks.StopFailure[]? |
                select(.hooks | any(.command | test("on-stop\\.sh")) | not)
            ] |
            .hooks.PreToolUse = [
                .hooks.PreToolUse[]? |
                select(.hooks | any(.command | test("on-pre-tool-use\\.sh")) | not)
            ] |
            .hooks.SubagentStart = [
                .hooks.SubagentStart[]? |
                select(.hooks | any(.command | test("on-pre-tool-use\\.sh")) | not)
            ] |
            .hooks.PermissionRequest = [
                .hooks.PermissionRequest[]? |
                select(.hooks | any(.command | test("on-permission-request\\.sh")) | not)
            ] |
            .hooks.PermissionDenied = [
                .hooks.PermissionDenied[]? |
                select(.hooks | any(.command | test("on-permission-denied\\.sh")) | not)
            ] |
            .hooks.Notification = [
                .hooks.Notification[]? |
                select(.hooks | any(.command | test("on-notification\\.sh")) | not)
            ] |
            .hooks.SessionEnd = [
                .hooks.SessionEnd[]? |
                select(.hooks | any(.command | test("on-session-end\\.sh")) | not)
            ] |
            if (.hooks.UserPromptSubmit | length) == 0 then del(.hooks.UserPromptSubmit) else . end |
            if (.hooks.Stop | length) == 0 then del(.hooks.Stop) else . end |
            if (.hooks.StopFailure | length) == 0 then del(.hooks.StopFailure) else . end |
            if (.hooks.PreToolUse | length) == 0 then del(.hooks.PreToolUse) else . end |
            if (.hooks.SubagentStart | length) == 0 then del(.hooks.SubagentStart) else . end |
            if (.hooks.PermissionRequest | length) == 0 then del(.hooks.PermissionRequest) else . end |
            if (.hooks.PermissionDenied | length) == 0 then del(.hooks.PermissionDenied) else . end |
            if (.hooks.Notification | length) == 0 then del(.hooks.Notification) else . end |
            if (.hooks.SessionEnd | length) == 0 then del(.hooks.SessionEnd) else . end |
            if (.hooks | length) == 0 then del(.hooks) else . end
        else . end
    '
}

# Write JSON to settings file atomically
_ccm_write_settings() {
    local settings_file="$1"
    local content="$2"
    local tmp_file
    tmp_file=$(mktemp "${settings_file}.tmp.XXXXXX") || ccm_die "Failed to create temp file"
    printf '%s\n' "$content" > "$tmp_file" || { rm -f "$tmp_file"; ccm_die "Failed to write temp file"; }
    mv -f "$tmp_file" "$settings_file" || { rm -f "$tmp_file"; ccm_die "Failed to replace settings file"; }
}

# Check if ccm hooks are installed in Claude Code settings
ccm_hooks_configured() {
    local settings_file="${HOME}/.claude/settings.json"
    [[ ! -f "$settings_file" ]] && return 1
    grep -q 'on-prompt-submit\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-stop\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-pre-tool-use\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-notification\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-permission-request\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-permission-denied\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-session-end\.sh' "$settings_file" 2>/dev/null || return 1
}

# Install Claude Code hooks for improved state detection
ccm_setup_hooks() {
    local settings_file="${HOME}/.claude/settings.json"
    local hooks_dir="${CCM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/hooks"

    if [[ ! -f "${hooks_dir}/on-prompt-submit.sh" ]]; then
        ccm_die "Hook scripts not found at ${hooks_dir}/"
    fi

    if ccm_hooks_configured; then
        local prompt_hook="${hooks_dir}/on-prompt-submit.sh"
        local stop_hook="${hooks_dir}/on-stop.sh"
        if grep -q "$prompt_hook" "$settings_file" 2>/dev/null && \
           grep -q "$stop_hook" "$settings_file" 2>/dev/null; then
            ccm_info "Claude Code hooks are already installed."
            echo "  To reinstall: ccm remove-hooks && ccm setup-hooks"
            return 0
        fi
        ccm_warn "Hook paths changed, updating..."
    fi

    mkdir -p "$(dirname "$settings_file")" 2>/dev/null

    local existing="{}"
    if [[ -f "$settings_file" ]]; then
        cp "$settings_file" "${settings_file}.bak" 2>/dev/null || true
        existing=$(cat "$settings_file")
    fi

    local prompt_hook="${hooks_dir}/on-prompt-submit.sh"
    local stop_hook="${hooks_dir}/on-stop.sh"
    local pre_tool_hook="${hooks_dir}/on-pre-tool-use.sh"
    local notify_hook="${hooks_dir}/on-notification.sh"
    local perm_hook="${hooks_dir}/on-permission-request.sh"
    local perm_denied_hook="${hooks_dir}/on-permission-denied.sh"
    local session_end_hook="${hooks_dir}/on-session-end.sh"
    local timeout="${CCM_HOOK_CMD_TIMEOUT:-5000}"

    local new_settings
    new_settings=$(echo "$existing" | _ccm_strip_hooks | jq \
        --arg prompt_cmd "$prompt_hook" \
        --arg stop_cmd "$stop_hook" \
        --arg pre_tool_cmd "$pre_tool_hook" \
        --arg notify_cmd "$notify_hook" \
        --arg perm_cmd "$perm_hook" \
        --arg perm_denied_cmd "$perm_denied_hook" \
        --arg session_end_cmd "$session_end_hook" \
        --argjson timeout "$timeout" '
        .hooks //= {} |
        .hooks.UserPromptSubmit //= [] |
        .hooks.Stop //= [] |
        .hooks.StopFailure //= [] |
        .hooks.PreToolUse //= [] |
        .hooks.PostToolUse //= [] |
        .hooks.SubagentStart //= [] |
        .hooks.PermissionRequest //= [] |
        .hooks.PermissionDenied //= [] |
        .hooks.Notification //= [] |
        .hooks.SessionEnd //= [] |
        .hooks.UserPromptSubmit += [{"hooks": [{"type": "command", "command": $prompt_cmd, "timeout": $timeout}]}] |
        .hooks.Stop += [{"hooks": [{"type": "command", "command": $stop_cmd, "timeout": $timeout}]}] |
        .hooks.StopFailure += [{"hooks": [{"type": "command", "command": $stop_cmd, "timeout": $timeout}]}] |
        .hooks.PreToolUse += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.PostToolUse += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.SubagentStart += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.PermissionRequest += [{"hooks": [{"type": "command", "command": $perm_cmd, "timeout": $timeout}]}] |
        .hooks.PermissionDenied += [{"hooks": [{"type": "command", "command": $perm_denied_cmd, "timeout": $timeout}]}] |
        .hooks.Notification += [{"matcher": "permission_prompt", "hooks": [{"type": "command", "command": $notify_cmd, "timeout": $timeout}]},
                                {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": $notify_cmd, "timeout": $timeout}]}] |
        .hooks.SessionEnd += [{"hooks": [{"type": "command", "command": $session_end_cmd, "timeout": $timeout}]}]
    ') || ccm_die "Failed to update settings JSON"

    _ccm_write_settings "$settings_file" "$new_settings"
    ccm_info "Claude Code hooks installed successfully."
    echo "  Settings: ${settings_file}"
    echo "  Hooks: UserPromptSubmit → BUSY, PreToolUse → BUSY, PostToolUse → BUSY"
    echo "         SubagentStart → BUSY, Stop/StopFailure → DONE"
    echo "         Notification → PERMIT/IDLE, PermissionDenied → PERMIT"
    echo "         SessionEnd → SHELL"
    echo ""
    echo "  Restart Claude Code to activate the hooks."
    echo "  To remove: ccm remove-hooks"
}

# Remove ccm hooks from Claude Code settings
ccm_remove_hooks() {
    local settings_file="${HOME}/.claude/settings.json"

    if [[ ! -f "$settings_file" ]]; then
        ccm_warn "No settings file found at ${settings_file}"
        return 0
    fi

    cp "$settings_file" "${settings_file}.bak" 2>/dev/null || true

    local new_settings
    new_settings=$(cat "$settings_file" | _ccm_strip_hooks) || ccm_die "Failed to update settings JSON"

    _ccm_write_settings "$settings_file" "$new_settings"
    ccm_info "ccm hooks removed from Claude Code settings."
    echo "  Restart Claude Code to apply changes."
}

# ─── CLAUDE.md integration ───

# Marker comments used to identify the ccm-managed section
_CCM_MD_BEGIN="<!-- ccm:begin -->"
_CCM_MD_END="<!-- ccm:end -->"

_ccm_claude_md_section() {
    cat <<'SECTION'
<!-- ccm:begin -->
## Multi-Project Environment

This user manages multiple projects with ccm (Claude Code Manager for tmux).
Use the following commands to discover and inspect other projects:

- `ccm list` — List all managed projects (names and directories)
- `ccm status` — Show all project states (branch, port, Claude status)
- `ccm capture <name>` — Capture visible terminal output from another project
<!-- ccm:end -->
SECTION
}

ccm_setup_claude_md() {
    local claude_md="${HOME}/.claude/CLAUDE.md"

    # Ensure directory exists
    mkdir -p "${HOME}/.claude"

    # Check if section already present
    if [[ -f "$claude_md" ]] && grep -qF "$_CCM_MD_BEGIN" "$claude_md"; then
        ccm_info "ccm section already exists in ${claude_md}"
        return 0
    fi

    # Show what will be added
    echo "The following will be appended to ${claude_md}:"
    echo ""
    _ccm_claude_md_section | sed 's/^/  /'
    echo ""

    # Ask for confirmation
    printf "Proceed? [y/N]: "
    read -r ans
    case "$ans" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Cancelled."; return 0 ;;
    esac

    # Append with a blank line separator
    if [[ -f "$claude_md" ]] && [[ -s "$claude_md" ]]; then
        # Ensure file ends with a newline before appending
        [[ "$(tail -c 1 "$claude_md")" != "" ]] && echo "" >> "$claude_md"
        echo "" >> "$claude_md"
    fi
    _ccm_claude_md_section >> "$claude_md"
    ccm_info "ccm section added to ${claude_md}"
}

ccm_remove_claude_md() {
    local claude_md="${HOME}/.claude/CLAUDE.md"

    if [[ ! -f "$claude_md" ]]; then
        ccm_warn "No file found at ${claude_md}"
        return 0
    fi

    if ! grep -qF "$_CCM_MD_BEGIN" "$claude_md"; then
        ccm_info "No ccm section found in ${claude_md}"
        return 0
    fi

    # Remove the ccm section (begin marker through end marker, plus surrounding blank lines)
    local tmp
    tmp=$(mktemp)
    awk -v begin="$_CCM_MD_BEGIN" -v end="$_CCM_MD_END" '
        $0 == begin { skip = 1; next }
        $0 == end   { skip = 0; next }
        skip { next }
        { print }
    ' "$claude_md" > "$tmp"

    # Clean up trailing blank lines left by removal
    sed -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$tmp" > "$claude_md"
    rm -f "$tmp"
    ccm_info "ccm section removed from ${claude_md}"
}
