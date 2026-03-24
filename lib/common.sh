#!/usr/bin/env bash
# ccm - common functions and constants

# Runtime data directory
CCM_DATA_DIR="${HOME}/.local/share/ccm"
CCM_SNAPSHOT_DIR="${CCM_DATA_DIR}/snapshots"
CCM_STATE_DIR="${CCM_DATA_DIR}/state"

# Temp directory (user-scoped to avoid multi-user collisions)
CCM_TMP_DIR="${TMPDIR:-/tmp}/ccm-${UID}"

# Dashboard refresh interval (seconds)
CCM_DASHBOARD_INTERVAL=2

# DONE state auto-clear timeout (seconds)
CCM_DONE_TIMEOUT=30

# Hook signal directory and timeout
CCM_HOOK_DIR="${CCM_TMP_DIR}/hooks"
# Safety timeout for BUSY hook signal (seconds) — if Claude crashes,
# the BUSY signal expires after this period
CCM_HOOK_TIMEOUT=300
# Timeout for hook commands in Claude Code settings (milliseconds)
CCM_HOOK_CMD_TIMEOUT=5000

# Popup size (percentage)
CCM_POPUP_WIDTH="80%"
CCM_POPUP_HEIGHT="60%"

# Claude Code detection patterns
# Centralized here so they can be easily updated when Claude Code UI changes.
# These are the ONLY place where Claude Code output text is matched.
# Process name used to find claude in the process tree.
CCM_CLAUDE_PROCESS_NAME="claude"
# Screen text pattern for permission prompts (grep -Ei compatible)
CCM_PATTERN_PERMIT='(Do you want|Allow|yes.*no|y\/n|approve|Would you like|Esc to cancel)'
# Claude Code input prompt pattern (visible when IDLE, absent during PERMIT)
# Note: Claude Code uses ❯ (U+276F) followed by non-breaking space (U+00A0)
CCM_PATTERN_INPUT_PROMPT='^[>❯›]'
# Commands to start Claude Code
CCM_CLAUDE_CMD="claude --resume 2>/dev/null || claude"
CCM_CLAUDE_CMD_RESUME="claude --continue 2>/dev/null || claude"

# Desktop notification (macOS / Linux)
# Controlled by @ccm-notify option: "off" (default), "permit", "done",
# "permit,done", "all"
_ccm_notify() {
    local title="$1" body="$2" sound="${3:-}"
    if command -v osascript &>/dev/null; then
        local sound_opt=""
        [[ -n "$sound" ]] && sound_opt=" sound name \"$sound\""
        osascript -e "display notification \"$body\" with title \"$title\"${sound_opt}" 2>/dev/null &
    elif command -v notify-send &>/dev/null; then
        notify-send "$title" "$body" 2>/dev/null &
    fi
}

ccm_notify() {
    local state="$1" project="$2"
    local notify_setting
    notify_setting=$(tmux show-option -gqv @ccm-notify 2>/dev/null)
    notify_setting="${notify_setting:-off}"

    [[ "$notify_setting" == "off" ]] && return

    local state_lower
    state_lower=$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')

    # Check if this state should trigger a notification
    case "$notify_setting" in
        all) ;;
        *"$state_lower"*) ;;
        *) return ;;
    esac

    # Sound option: @ccm-notify-sound (default: on)
    local sound_setting
    sound_setting=$(tmux show-option -gqv @ccm-notify-sound 2>/dev/null)
    sound_setting="${sound_setting:-on}"
    local permit_sound=""
    [[ "$sound_setting" == "on" ]] && permit_sound="Basso"

    case "$state" in
        PERMIT) _ccm_notify "ccm ⚠ $project" "Action required — switch to this project and respond to the permission prompt" "$permit_sound" "critical" ;;
        DONE)   _ccm_notify "ccm ✔ $project" "Claude has finished responding — review the output when ready" "" "normal" ;;
        BUSY)   _ccm_notify "ccm ◉ $project" "Claude is now processing your request" "" "low" ;;
        IDLE)   _ccm_notify "ccm $project" "Waiting for your input" "" "low" ;;
    esac
}

# Auto-start Claude Code when switching to SHELL-state windows
# Controlled by @ccm-auto-start: "on" (default) or "off"
ccm_auto_start_claude() {
    local win_target="$1"
    local auto_start
    auto_start=$(tmux show-option -gqv @ccm-auto-start 2>/dev/null)
    auto_start="${auto_start:-on}"
    [[ "$auto_start" != "on" ]] && return

    tmux send-keys -t "$win_target" "$CCM_CLAUDE_CMD_RESUME" Enter 2>/dev/null
}

# Colors for terminal output (using $'...' for real escape characters)
COLOR_RED=$'\033[0;31m'
COLOR_GREEN=$'\033[0;32m'
COLOR_YELLOW=$'\033[1;33m'
COLOR_BLUE=$'\033[0;34m'
COLOR_CYAN=$'\033[0;36m'
COLOR_BOLD=$'\033[1m'
COLOR_DIM=$'\033[2m'
COLOR_RESET=$'\033[0m'

# Status indicators
STATUS_PERMIT="⚠ PERMIT"
STATUS_IDLE="● IDLE"
STATUS_BUSY="◉ BUSY"
STATUS_DONE="✔ DONE"
STATUS_SHELL="■ SHELL"
STATUS_DOWN="○ DOWN"

# Ensure runtime directories exist
ccm_init_dirs() {
    mkdir -p "$CCM_SNAPSHOT_DIR" "$CCM_STATE_DIR" "$CCM_TMP_DIR" "$CCM_HOOK_DIR" \
             "$CCM_PORT_CACHE_DIR" "$CCM_GIT_CACHE_DIR" 2>/dev/null

    # Clean up stale cache/lock files (older than 1 hour)
    find "$CCM_TMP_DIR" -maxdepth 2 -type f -mmin +60 -delete 2>/dev/null || true
    find "$CCM_TMP_DIR" -maxdepth 2 -type d -empty -mmin +60 -delete 2>/dev/null || true
}

# Auto-detect current tmux session
ccm_current_session() {
    # 1. Check popup session file (written by keybinding via run-shell)
    #    Fresh file (< 60s) takes priority because tmux display-message
    #    may return wrong session inside popups
    if [[ -f ${CCM_TMP_DIR}/popup-session ]]; then
        local file_age
        file_age=$(( $(date +%s) - $(stat -f %m ${CCM_TMP_DIR}/popup-session 2>/dev/null || stat -c %Y ${CCM_TMP_DIR}/popup-session 2>/dev/null || echo 0) ))
        if [[ $file_age -lt 60 ]]; then
            cat ${CCM_TMP_DIR}/popup-session
            return
        fi
    fi

    # 2. Try tmux display-message (works in normal panes)
    local session
    session=$(tmux display-message -p '#{session_name}' 2>/dev/null)
    if [[ -n "$session" ]]; then
        echo "$session"
        return
    fi

    # 3. Last resort: first attached client's session
    tmux list-clients -F '#{session_name}' 2>/dev/null | head -1
}

# Get session name (defaults to current session)
CCM_SESSION="${CCM_SESSION:-}"
_ccm_session() {
    if [[ -n "$CCM_SESSION" ]]; then
        echo "$CCM_SESSION"
    else
        ccm_current_session
    fi
}

# List all ccm-managed windows in the current session
# Output: window_index \t window_name \t @ccm_project \t @ccm_dir
ccm_list_windows() {
    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && return

    tmux list-windows -t "$session" -F '#{window_index}	#{window_name}	#{@ccm_project}	#{@ccm_dir}' 2>/dev/null \
        | awk -F'\t' '$3 != ""' || true
}

# Get the window name for a project (window_name == project name)
ccm_window_name() {
    local name="$1"
    echo "$name"
}

# Get the project name from a window's @ccm_project tag
ccm_project_name_from_window() {
    local win_target="$1"
    tmux show-option -wt "$win_target" -qv @ccm_project 2>/dev/null
}

# Find window index by project name (searches @ccm_project tag)
# Returns window index or empty string
ccm_find_window() {
    local name="$1"
    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && return

    tmux list-windows -t "$session" -F '#{window_index}	#{@ccm_project}' 2>/dev/null \
        | awk -F'\t' -v n="$name" '$2 == n {print $1; exit}' || true
}

# Check if a ccm project window exists
ccm_project_exists() {
    local name="$1"
    local idx
    idx=$(ccm_find_window "$name")
    [[ -n "$idx" ]]
}

# Get the working directory for a ccm project window
ccm_project_dir() {
    local name="$1"
    local session idx
    session=$(_ccm_session)
    idx=$(ccm_find_window "$name")
    [[ -z "$idx" ]] && return
    tmux show-option -wt "${session}:${idx}" -qv @ccm_dir 2>/dev/null
}

# Validate and sanitize a project name
# Returns sanitized name or exits with error if invalid
ccm_validate_name() {
    local name="$1"
    # Remove leading/trailing whitespace
    name=$(printf '%s' "$name" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    # Replace tabs, newlines, spaces with hyphens
    name=$(printf '%s' "$name" | tr '[:space:]' '-' | tr -s '-')
    # Remove characters that break tmux or shell parsing
    name=$(printf '%s' "$name" | tr -d "'\"\`\$\\;&|<>()")
    # Remove leading/trailing hyphens
    name=$(printf '%s' "$name" | sed 's/^-*//;s/-*$//')
    # Must not be empty after sanitization
    [[ -z "$name" ]] && return 1
    printf '%s' "$name"
}

# Print an error message and exit
ccm_die() {
    echo -e "${COLOR_RED}Error: $1${COLOR_RESET}" >&2
    exit 1
}

# Print a warning message
ccm_warn() {
    echo -e "${COLOR_YELLOW}Warning: $1${COLOR_RESET}" >&2
}

# Print an info message
ccm_info() {
    echo -e "${COLOR_GREEN}$1${COLOR_RESET}"
}

# Check dependencies
ccm_check_deps() {
    local missing=()
    for cmd in tmux jq fzf; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        ccm_die "Missing dependencies: ${missing[*]}"
    fi
}

# Remove ccm hook entries from a settings JSON string (helper)
# Reads JSON from stdin, outputs cleaned JSON to stdout
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
            if (.hooks.UserPromptSubmit | length) == 0 then del(.hooks.UserPromptSubmit) else . end |
            if (.hooks.Stop | length) == 0 then del(.hooks.Stop) else . end |
            if (.hooks | length) == 0 then del(.hooks) else . end
        else . end
    '
}

# Write JSON to settings file atomically (temp file + mv)
_ccm_write_settings() {
    local settings_file="$1"
    local content="$2"
    local tmp_file
    tmp_file=$(mktemp "${settings_file}.tmp.XXXXXX") || ccm_die "Failed to create temp file"
    printf '%s\n' "$content" > "$tmp_file" || { rm -f "$tmp_file"; ccm_die "Failed to write temp file"; }
    mv -f "$tmp_file" "$settings_file" || { rm -f "$tmp_file"; ccm_die "Failed to replace settings file"; }
}

# Install Claude Code hooks for improved state detection
# Adds UserPromptSubmit and Stop hooks to ~/.claude/settings.json
ccm_setup_hooks() {
    local settings_file="${HOME}/.claude/settings.json"
    local hooks_dir="${CCM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/hooks"

    if [[ ! -f "${hooks_dir}/on-prompt-submit.sh" ]]; then
        ccm_die "Hook scripts not found at ${hooks_dir}/"
    fi

    # Ensure settings directory exists
    mkdir -p "$(dirname "$settings_file")" 2>/dev/null

    # Read existing settings or start with empty object
    local existing="{}"
    if [[ -f "$settings_file" ]]; then
        # Create backup
        cp "$settings_file" "${settings_file}.bak" 2>/dev/null || true
        existing=$(cat "$settings_file")
    fi

    local prompt_hook="${hooks_dir}/on-prompt-submit.sh"
    local stop_hook="${hooks_dir}/on-stop.sh"
    local timeout="${CCM_HOOK_CMD_TIMEOUT:-5000}"

    # Strip any existing ccm hooks first (handles path changes cleanly)
    # then add fresh entries with current paths
    local new_settings
    new_settings=$(echo "$existing" | _ccm_strip_hooks | jq \
        --arg prompt_cmd "$prompt_hook" \
        --arg stop_cmd "$stop_hook" \
        --argjson timeout "$timeout" '
        .hooks //= {} |
        .hooks.UserPromptSubmit //= [] |
        .hooks.Stop //= [] |
        .hooks.UserPromptSubmit += [{"hooks": [{"type": "command", "command": $prompt_cmd, "timeout": $timeout}]}] |
        .hooks.Stop += [{"hooks": [{"type": "command", "command": $stop_cmd, "timeout": $timeout}]}]
    ') || ccm_die "Failed to update settings JSON"

    _ccm_write_settings "$settings_file" "$new_settings"
    ccm_info "Claude Code hooks installed successfully."
    echo "  Settings: ${settings_file}"
    echo "  Hooks: UserPromptSubmit → BUSY, Stop → DONE"
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

    # Create backup
    cp "$settings_file" "${settings_file}.bak" 2>/dev/null || true

    local new_settings
    new_settings=$(cat "$settings_file" | _ccm_strip_hooks) || ccm_die "Failed to update settings JSON"

    _ccm_write_settings "$settings_file" "$new_settings"
    ccm_info "ccm hooks removed from Claude Code settings."
    echo "  Restart Claude Code to apply changes."
}

# Detect listening TCP ports for processes whose cwd matches a directory
# Returns comma-separated port list (e.g., "3000,8080") or empty string
# Uses a short-lived cache (10 seconds) to avoid repeated lsof calls in dashboard
CCM_PORT_CACHE_DIR="${CCM_TMP_DIR}/port-cache"

ccm_detect_ports() {
    local dir="$1"
    local expanded
    expanded=$(ccm_expand_path "$dir")
    [[ ! -d "$expanded" ]] && return

    # Check cache (valid for 10 seconds)
    local cache_key
    cache_key=$(_ccm_md5 "$expanded")
    local cache_file="${CCM_PORT_CACHE_DIR}/${cache_key}"
    if [[ -f "$cache_file" ]]; then
        local cache_age
        cache_age=$(( $(date +%s) - $(stat -f %m "$cache_file" 2>/dev/null || stat -c %Y "$cache_file" 2>/dev/null || echo 0) ))
        if [[ $cache_age -lt 30 ]]; then
            cat "$cache_file"
            return
        fi
    fi

    # Collect PIDs of processes whose cwd starts with the directory
    local pids
    pids=$(lsof -d cwd 2>/dev/null \
        | awk -v d="$expanded" '$0 ~ d {print $2}' \
        | sort -un)

    local result=""
    if [[ -n "$pids" ]]; then
        # Build a comma-separated PID list for lsof
        local pid_list
        pid_list=$(echo "$pids" | paste -sd, -)

        # Find listening TCP ports for those PIDs
        result=$(lsof -nP -iTCP -sTCP:LISTEN -a -p "$pid_list" -F n 2>/dev/null \
            | awk -F: '/^n/ {print $NF}' \
            | sort -un \
            | paste -sd, -)
    fi

    # Write cache
    echo -n "$result" > "$cache_file" 2>/dev/null
    echo -n "$result"
}

# Get git branch name for a directory (empty string if not a git repo)
# Uses a short-lived cache (30 seconds) to avoid repeated git calls in dashboard
CCM_GIT_CACHE_DIR="${CCM_TMP_DIR}/git-cache"

ccm_git_branch() {
    local dir="$1"
    local expanded
    expanded=$(ccm_expand_path "$dir")

    # Check cache
    local cache_key
    cache_key=$(_ccm_md5 "$expanded")
    local cache_file="${CCM_GIT_CACHE_DIR}/${cache_key}"
    if [[ -f "$cache_file" ]]; then
        local cache_age
        cache_age=$(( $(date +%s) - $(stat -f %m "$cache_file" 2>/dev/null || stat -c %Y "$cache_file" 2>/dev/null || echo 0) ))
        if [[ $cache_age -lt 30 ]]; then
            cat "$cache_file"
            return
        fi
    fi

    local branch
    branch=$(git -C "$expanded" branch --show-current 2>/dev/null || echo "")
    if [[ -n "$branch" ]]; then
        # Append * if there are uncommitted changes (staged or unstaged)
        if ! git -C "$expanded" diff --quiet HEAD 2>/dev/null; then
            branch="${branch}*"
        fi
    fi
    echo -n "$branch" > "$cache_file" 2>/dev/null
    echo -n "$branch"
}

# Expand ~ to $HOME and resolve symlinks to canonical path
ccm_expand_path() {
    local path="$1"
    path="${path/#\~/$HOME}"
    # Resolve symlinks to canonical path
    if [[ -e "$path" ]]; then
        realpath "$path" 2>/dev/null || echo "$path"
    else
        # Path doesn't exist yet — resolve parent if possible
        local parent
        parent=$(dirname "$path")
        local base
        base=$(basename "$path")
        if [[ -e "$parent" ]]; then
            echo "$(realpath "$parent" 2>/dev/null || echo "$parent")/$base"
        else
            echo "$path"
        fi
    fi
}

# Compute MD5 hash of a string (cross-platform)
_ccm_md5() {
    if command -v md5 &>/dev/null; then
        printf '%s' "$1" | md5
    elif command -v md5sum &>/dev/null; then
        printf '%s' "$1" | md5sum | cut -d' ' -f1
    else
        return 1
    fi
}

# Read hook signal for a directory
# Returns: "<timestamp> <state>" or empty string
_ccm_read_hook_signal() {
    local dir="$1"
    local expanded
    expanded=$(ccm_expand_path "$dir")

    local cache_key
    cache_key=$(_ccm_md5 "$expanded") || return
    local hook_file="${CCM_HOOK_DIR}/${cache_key}"
    [[ -f "$hook_file" ]] && cat "$hook_file"
}
