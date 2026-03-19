#!/usr/bin/env bash
# ccm - common functions and constants

# Prefix for ccm-managed tmux sessions (kept for backward compat)
CCM_SESSION_PREFIX="ccm-"

# Runtime data directory
CCM_DATA_DIR="${HOME}/.local/share/ccm"
CCM_SNAPSHOT_DIR="${CCM_DATA_DIR}/snapshots"
CCM_STATE_DIR="${CCM_DATA_DIR}/state"

# Dashboard refresh interval (seconds)
CCM_DASHBOARD_INTERVAL=2

# Popup size (percentage)
CCM_POPUP_WIDTH="80%"
CCM_POPUP_HEIGHT="60%"

# Claude Code detection patterns
# These are centralized here so they can be easily updated when Claude Code UI changes
PATTERN_PERMIT='(Do you want|Allow|yes.*no|y,n|y\/n|approve|permit|Would you like to|Read|Edit|Bash|Write|Execute|WebFetch|WebSearch|Glob|Grep|ToolSearch|NotebookEdit|access|permission)'
PATTERN_IDLE='(❯|>)\s*$'
PATTERN_COMPACT='(compact|compress|summariz)'

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
STATUS_WORK="★ WORK"
STATUS_DOWN="○ DOWN"

# Ensure runtime directories exist
ccm_init_dirs() {
    mkdir -p "$CCM_SNAPSHOT_DIR" "$CCM_STATE_DIR"
}

# Auto-detect current tmux session
ccm_current_session() {
    tmux display-message -p '#{session_name}' 2>/dev/null
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

# Legacy compatibility: session name mapping
ccm_session_name() {
    local name="$1"
    echo "${CCM_SESSION_PREFIX}${name}"
}

ccm_project_name() {
    local session="$1"
    echo "${session#$CCM_SESSION_PREFIX}"
}

# List ccm sessions (legacy, for backward compat)
ccm_list_sessions() {
    tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${CCM_SESSION_PREFIX}" || true
}

# Check if a ccm session exists (legacy)
ccm_session_exists() {
    local name="$1"
    local session
    session=$(ccm_session_name "$name")
    tmux has-session -t "$session" 2>/dev/null
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

# Expand ~ to $HOME and resolve symlinks (e.g., ~/Dropbox → ~/Library/CloudStorage/Dropbox)
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
