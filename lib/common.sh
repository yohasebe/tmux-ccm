#!/usr/bin/env bash
# ccm - common functions and constants
# This file provides bash-only helpers for: init wizard, hook setup/removal,
# dependency checks, and output formatting.
# All session management, snapshots, and state detection are in Python (lib/ccm_core.py).

# Claude Code's own config dir override (mirrors Claude Code itself)
CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-${HOME}/.claude}"

# Runtime data directory (overridable for isolated test/demo environments)
CCM_DATA_DIR="${CCM_DATA_DIR:-${HOME}/.local/share/ccm}"
CCM_SNAPSHOT_DIR="${CCM_SNAPSHOT_DIR:-${CCM_DATA_DIR}/snapshots}"
CCM_STATE_DIR="${CCM_STATE_DIR:-${CCM_DATA_DIR}/state}"

# Temp directory (user-scoped; overridable for isolation)
CCM_TMP_DIR="${CCM_TMP_DIR:-${TMPDIR:-/tmp}/ccm-${UID}}"

# Hook signal directory
CCM_HOOK_DIR="${CCM_HOOK_DIR:-${CCM_TMP_DIR}/hooks}"
# Timeout for hook commands in Claude Code settings (milliseconds)
CCM_HOOK_CMD_TIMEOUT="${CCM_HOOK_CMD_TIMEOUT:-5000}"

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
# Seconds between full status reconciliations. The periodic
# `#(ccm inject-status)` in status-right fires once per
# `status-interval` — every second on a config that shows a seconds
# clock — but a full run costs ~174 ms and spawns ~24 processes
# (python3 plus 21 tmux clients plus this wrapper), which is how a
# status bar ends up holding a measurable share of the machine.
# State changes do not need that poll: the hooks already push
# `inject-status --fast` on every transition, so the periodic pass
# only has to catch what fires no hook — a git branch switch, a new
# listening port, a stale-BUSY release crossing its window.
#
# 20 s is chosen against BUSY_STALE_RELEASE_SEC (60 s): the release
# it must observe is a threshold crossing rather than an event, so
# nothing re-evaluates it until a reconciliation runs. Keeping the
# interval well under that window bounds the worst case for an
# Esc-interrupted session at roughly 80 s rather than leaving it to
# the next unrelated hook.
CCM_RECONCILE_INTERVAL="${CCM_RECONCILE_INTERVAL:-20}"

# True when the periodic (non-`--fast`) status pass should do a full
# run. Deliberately implemented in the shell wrapper rather than in
# `inject_status.py`: reaching Python already costs a python3 start,
# and the whole point is to not pay it on the seconds in between.
#
# Written to fork nothing on the common path — a gate that spawns
# `date` and `tmux` to decide not to spawn python3 gives most of the
# saving back. `EPOCHSECONDS` is a bash 5 builtin (macOS ships 3.2 as
# /bin/bash, hence the fallback), and the server-restart check reads
# the socket's mtime with `[[ -nt ]]`, which is a builtin test.
#
# The socket mtime is the restart signal: it equals the server's
# `#{start_time}` (verified), so a socket newer than our stamp means
# the server restarted since the last reconciliation. Without that
# check a fresh server would render from state nobody had computed
# for up to CCM_RECONCILE_INTERVAL seconds — the stamp lives in
# $TMPDIR and outlives the server that wrote it.
_ccm_should_reconcile() {
    local stamp="${CCM_TMP_DIR}/reconcile-stamp"
    local now last
    now="${EPOCHSECONDS:-}"
    [[ -z "$now" ]] && now=$(date +%s)
    if [[ -r "$stamp" ]]; then
        # Server restarted after our last run → reconcile now.
        local sock="${TMUX%%,*}"
        if [[ -z "$sock" || ! "$sock" -nt "$stamp" ]]; then
            read -r last < "$stamp" 2>/dev/null || true
            if [[ "$last" =~ ^[0-9]+$ ]] \
                && (( now - last < CCM_RECONCILE_INTERVAL )); then
                return 1
            fi
        fi
    fi
    printf '%s\n' "$now" > "$stamp" 2>/dev/null || true
    return 0
}


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
    echo "    Hooks improve state detection accuracy (BUSY/PERMIT/IDLE)."
    if ccm_hooks_configured; then
        echo "    ${COLOR_GREEN}✔${COLOR_RESET} Already installed"
    else
        echo -n "    Install hooks? [Y/n] "
        local answer
        read -r answer
        if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
            # Run setup-hooks in a subshell so its `ccm_die`-on-error
            # path does not kill the wizard. Show stderr live (the user
            # needs to see "Claude Code too old" / "claude not on PATH"
            # if it happens) and route stdout to /dev/null to keep the
            # wizard's progress output uncluttered.
            if ( ccm_setup_hooks >/dev/null ); then
                echo "    ${COLOR_GREEN}✔${COLOR_RESET} Installed"
            else
                echo "    ${COLOR_RED}✘${COLOR_RESET} Hook install failed — see message above. Continuing with the wizard; rerun 'ccm setup-hooks' once resolved."
            fi
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
    echo "    0 = Icon in status-right (minimal)"
    echo "    1 = Replace window list with ccm entries"
    echo "    2 = Dedicated status line with full details (default)"
    local current_mode="2"
    if [[ $in_tmux -eq 1 ]]; then
        current_mode=$(tmux show-option -gqv @ccm-status-line 2>/dev/null)
        current_mode="${current_mode:-2}"
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
            .hooks.PostToolUse = [
                .hooks.PostToolUse[]? |
                select(.hooks | any(.command | test("on-pre-tool-use\\.sh")) | not)
            ] |
            .hooks.PostToolUseFailure = [
                .hooks.PostToolUseFailure[]? |
                select(.hooks | any(.command | test("on-pre-tool-use\\.sh")) | not)
            ] |
            .hooks.SubagentStart = [
                .hooks.SubagentStart[]? |
                select(.hooks | any(.command | test("on-pre-tool-use\\.sh")) | not)
            ] |
            .hooks.SubagentStop = [
                .hooks.SubagentStop[]? |
                select(.hooks | any(.command | test("on-pre-tool-use\\.sh")) | not)
            ] |
            .hooks.PreCompact = [
                .hooks.PreCompact[]? |
                select(.hooks | any(.command | test("on-pre-tool-use\\.sh")) | not)
            ] |
            .hooks.PostCompact = [
                .hooks.PostCompact[]? |
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
            if (.hooks.PostToolUse | length) == 0 then del(.hooks.PostToolUse) else . end |
            if (.hooks.PostToolUseFailure | length) == 0 then del(.hooks.PostToolUseFailure) else . end |
            if (.hooks.SubagentStart | length) == 0 then del(.hooks.SubagentStart) else . end |
            if (.hooks.SubagentStop | length) == 0 then del(.hooks.SubagentStop) else . end |
            if (.hooks.PreCompact | length) == 0 then del(.hooks.PreCompact) else . end |
            if (.hooks.PostCompact | length) == 0 then del(.hooks.PostCompact) else . end |
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

# ─── Claude Code version detection ───
#
# ccm requires Claude Code v2.1.107+. The `elicitation_dialog`
# Notification matcher ccm registers is only accepted from that
# version onward.

CCM_MIN_CLAUDE_VERSION="2.1.107"

# Return the installed Claude Code version as an `X.Y.Z` string,
# or an empty string if `claude` is not on PATH or the output
# cannot be parsed. Format: `2.1.108 (Claude Code)`.
_ccm_claude_version() {
    local out
    out=$(claude --version 2>/dev/null) || return 0
    printf '%s' "$out" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

# Return 0 if version $1 >= $2 (only handles `MAJOR.MINOR.PATCH`).
_ccm_version_ge() {
    local a="$1" b="$2"
    [[ -z "$a" ]] && return 1
    [[ "$a" == "$b" ]] && return 0
    local smallest
    smallest=$(printf '%s\n%s\n' "$a" "$b" | sort -V | head -1)
    [[ "$smallest" == "$b" ]]
}

# Check if ccm hooks are installed in Claude Code settings
ccm_hooks_configured() {
    local settings_file="${CLAUDE_CONFIG_DIR}/settings.json"
    [[ ! -f "$settings_file" ]] && return 1
    grep -q 'on-prompt-submit\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-stop\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-pre-tool-use\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-notification\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-permission-request\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-permission-denied\.sh' "$settings_file" 2>/dev/null || return 1
    grep -q 'on-session-end\.sh' "$settings_file" 2>/dev/null || return 1
    # Required hook event names that ccm must always have
    # registered. Force a reinstall when settings.json is missing
    # any of them.
    grep -q 'PostToolUseFailure' "$settings_file" 2>/dev/null || return 1
    grep -q 'SubagentStop' "$settings_file" 2>/dev/null || return 1
    grep -q 'PreCompact' "$settings_file" 2>/dev/null || return 1
    grep -q 'PostCompact' "$settings_file" 2>/dev/null || return 1
    # The elicitation_dialog Notification matcher is required.
    # Scope the check to a `"matcher": "elicitation_dialog"` literal
    # rather than a bare string so we cannot false-positive on the
    # word appearing elsewhere in the file.
    grep -qE '"matcher"[[:space:]]*:[[:space:]]*"elicitation_dialog"' \
        "$settings_file" 2>/dev/null || return 1
}

# Install Claude Code hooks for improved state detection
ccm_setup_hooks() {
    local settings_file="${CLAUDE_CONFIG_DIR}/settings.json"
    local hooks_dir="${CCM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/hooks"

    if [[ ! -f "${hooks_dir}/on-prompt-submit.sh" ]]; then
        ccm_die "Hook scripts not found at ${hooks_dir}/"
    fi

    # Hard-fail if the running Claude Code is below the minimum
    # supported version (introduces the elicitation_dialog matcher
    # ccm requires). An empty version string means `claude` is not
    # on PATH — that is fatal too; ccm cannot manage hooks for a
    # client it cannot interrogate.
    local claude_ver
    claude_ver=$(_ccm_claude_version)
    if [[ -z "$claude_ver" ]]; then
        ccm_die "Claude Code is not on PATH. Install it before running setup-hooks."
    fi
    if ! _ccm_version_ge "$claude_ver" "$CCM_MIN_CLAUDE_VERSION"; then
        ccm_die "Claude Code ${claude_ver} is too old; ccm requires v${CCM_MIN_CLAUDE_VERSION}+. Run: claude update"
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

    local notification_matchers
    notification_matchers=$(jq -nc \
        --arg cmd "$notify_hook" --argjson timeout "$timeout" '[
            {"matcher": "permission_prompt", "hooks": [{"type": "command", "command": $cmd, "timeout": $timeout}]},
            {"matcher": "idle_prompt",       "hooks": [{"type": "command", "command": $cmd, "timeout": $timeout}]},
            {"matcher": "elicitation_dialog", "hooks": [{"type": "command", "command": $cmd, "timeout": $timeout}]}
        ]')

    local new_settings
    new_settings=$(echo "$existing" | _ccm_strip_hooks | jq \
        --arg prompt_cmd "$prompt_hook" \
        --arg stop_cmd "$stop_hook" \
        --arg pre_tool_cmd "$pre_tool_hook" \
        --arg notify_cmd "$notify_hook" \
        --arg perm_cmd "$perm_hook" \
        --arg perm_denied_cmd "$perm_denied_hook" \
        --arg session_end_cmd "$session_end_hook" \
        --argjson notification_matchers "$notification_matchers" \
        --argjson timeout "$timeout" '
        .hooks //= {} |
        .hooks.UserPromptSubmit //= [] |
        .hooks.Stop //= [] |
        .hooks.StopFailure //= [] |
        .hooks.PreToolUse //= [] |
        .hooks.PostToolUse //= [] |
        .hooks.PostToolUseFailure //= [] |
        .hooks.SubagentStart //= [] |
        .hooks.SubagentStop //= [] |
        .hooks.PreCompact //= [] |
        .hooks.PostCompact //= [] |
        .hooks.PermissionRequest //= [] |
        .hooks.PermissionDenied //= [] |
        .hooks.Notification //= [] |
        .hooks.SessionEnd //= [] |
        .hooks.UserPromptSubmit += [{"hooks": [{"type": "command", "command": $prompt_cmd, "timeout": $timeout}]}] |
        .hooks.Stop += [{"hooks": [{"type": "command", "command": $stop_cmd, "timeout": $timeout}]}] |
        .hooks.StopFailure += [{"hooks": [{"type": "command", "command": $stop_cmd, "timeout": $timeout}]}] |
        .hooks.PreToolUse += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.PostToolUse += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.PostToolUseFailure += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.SubagentStart += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.SubagentStop += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.PreCompact += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.PostCompact += [{"hooks": [{"type": "command", "command": $pre_tool_cmd, "timeout": $timeout}]}] |
        .hooks.PermissionRequest += [{"hooks": [{"type": "command", "command": $perm_cmd, "timeout": $timeout}]}] |
        .hooks.PermissionDenied += [{"hooks": [{"type": "command", "command": $perm_denied_cmd, "timeout": $timeout}]}] |
        .hooks.Notification += $notification_matchers |
        .hooks.SessionEnd += [{"hooks": [{"type": "command", "command": $session_end_cmd, "timeout": $timeout}]}]
    ') || ccm_die "Failed to update settings JSON"

    _ccm_write_settings "$settings_file" "$new_settings"
    ccm_info "Claude Code hooks installed successfully."
    echo "  Settings: ${settings_file}"
    if [[ -n "$claude_ver" ]]; then
        echo "  Claude Code: ${claude_ver}"
    else
        echo "  Claude Code: version unknown (claude CLI not found)"
    fi
    echo "  Hooks: UserPromptSubmit → BUSY, PreToolUse → BUSY, PostToolUse → BUSY"
    echo "         PostToolUseFailure → BUSY, SubagentStart/Stop → BUSY, Stop/StopFailure → clear signal"
    echo "         PreCompact/PostCompact → BUSY (compaction is busy work)"
    echo "         Notification → PERMIT (permission/elicitation) / clear signal (idle)"
    echo "         PermissionDenied → PERMIT, SessionEnd → SHELL"
    echo ""
    echo "  Restart Claude Code to activate the hooks."
    echo "  To remove: ccm remove-hooks"
}

# Remove ccm hooks from Claude Code settings
ccm_remove_hooks() {
    local settings_file="${CLAUDE_CONFIG_DIR}/settings.json"

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
Use the following commands to discover, inspect, and coordinate other projects:

- `ccm list` — List all managed projects (names and directories)
- `ccm status` — Show all project states (branch, port, Claude state)
- `ccm capture <name>` — Capture visible terminal output from another project.
  Split windows are captured pane by pane, each labelled with what runs in
  it. Passing THIS project's own name is how you read the pane beside you:
  when the user keeps a second agent CLI in a split pane of this window,
  that is how you see what it is doing.

  A project's STATE (from `ccm status`) describes its **Claude** pane, not
  any second agent sharing the window. So do not use it to judge whether
  that other agent is free — while you are the one asking, the state you
  read is your own, and a session running a command is BUSY by definition.
  Judge a sidekick pane only from its captured content.
- `ccm send <name> <message>` — Send a prompt to another project's Claude
  Code session. Accepts a positional message, `--file <path>`, `--stdin`,
  or `-` as a stdin alias. Multi-line messages are converted to `M-Enter`
  between lines so the body arrives as a single multi-line prompt.

  State policy (important — do not try to override PERMIT):
    - **IDLE** → send immediately
    - **BUSY** → refused unless `--force` (message queues into the input
      buffer and mixes with Claude's current turn)
    - **SHELL** → refused unless `--start` (launches Claude first, waits
      ~2s, then sends)
    - **PERMIT** → **always refused, even with `--force`**. Typing into
      a permission dialog could accidentally approve or deny a tool
      call. Ask the user to respond to the dialog in the target pane
      first, then retry the send.

  **Handing work back.** You cannot observe another agent's progress,
  and it cannot observe yours, so nobody should poll or wait. Announce
  instead: when you finish something another session asked you for,
  `ccm send` the result back yourself. When you ask *them* for
  something, say how to return the answer ("reply with `ccm send
  <project> …` when done") — otherwise the user ends up relaying it by
  hand, which means the relay is not actually working. For long
  results, write a file and send a one-line pointer to it rather than
  pasting the body.

  **Reaching a sidekick beside you — and only the one beside you.** A
  sidekick belongs to the Claude session sharing its window, and that
  session is the only one that should type into it. Want something from
  another project's sidekick? Ask that project's Claude (`ccm send
  <project> "…"`) instead of reaching into its window: it knows whether
  its sidekick is free and which keys that TUI takes, and it stays aware
  of what its own sidekick is doing. Two senders typing into one
  composer interleave into a single garbled prompt.

  `ccm send` only targets Claude panes, so the sidekick in YOUR window
  is reached with tmux directly — check it is at its prompt with
  `ccm capture <this project>` first, then:

      tmux send-keys -t <pane> -l -- "<message>"
      sleep 0.3
      tmux send-keys -t <pane> Enter

  `-l` sends the text literally; without it a word like `Enter` inside
  your message becomes that keystroke. The pause is not cosmetic: with
  no gap the peer's TUI can still be digesting the text when `Enter`
  lands and take it as a newline rather than a submit, leaving the body
  in its composer. Then `ccm capture` again and read its input box —
  **empty means sent; your text still sitting there means it was not.**
  Visible text is proof of failure, not proof of delivery.

  Other flags: `--no-enter` (type without submitting), `-y` / `--yes`
  (skip the interactive confirmation), `--` (end of flag parsing, for
  messages that start with `-`). Confirmation is auto-skipped when
  stdin or stdout is not a TTY, so `echo "..." | ccm send <name> --stdin`
  works from shell pipelines and MCP server hooks.
<!-- ccm:end -->
SECTION
}

ccm_setup_claude_md() {
    local claude_md="${CLAUDE_CONFIG_DIR}/CLAUDE.md"

    # Ensure directory exists
    mkdir -p "${CLAUDE_CONFIG_DIR}"

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
    local claude_md="${CLAUDE_CONFIG_DIR}/CLAUDE.md"

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
