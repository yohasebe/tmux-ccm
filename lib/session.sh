#!/usr/bin/env bash
# ccm - window-based session management
# All projects are windows in the user's current tmux session

# Create a new ccm project window and optionally start Claude Code
# Args: <dir> [name] [start_claude]
ccm_add() {
    local dir="$1"
    local name="$2"
    local start_claude="${3:-true}"

    [[ -z "$dir" ]] && ccm_die "Directory is required"

    dir=$(ccm_expand_path "$dir")
    [[ ! -d "$dir" ]] && ccm_die "Directory does not exist: $dir"

    # Default name from directory basename
    [[ -z "$name" ]] && name=$(basename "$dir")

    # Sanitize project name
    name=$(ccm_validate_name "$name") || ccm_die "Invalid project name"

    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && ccm_die "Not inside a tmux session"

    if ccm_project_exists "$name"; then
        ccm_die "Project window already exists: $name"
    fi

    # Check for duplicate directory (resolved path comparison)
    local real_dir
    real_dir=$(realpath "$dir" 2>/dev/null || echo "$dir")
    local windows
    windows=$(ccm_list_windows)
    if [[ -n "$windows" ]]; then
        while IFS=$'\t' read -r _idx _wname _proj existing_dir; do
            local real_existing
            real_existing=$(realpath "$existing_dir" 2>/dev/null || echo "$existing_dir")
            if [[ "$real_dir" == "$real_existing" ]]; then
                ccm_die "Directory already registered as project '$_proj': $existing_dir"
            fi
        done <<< "$windows"
    fi

    # Create new window in current session and capture its index
    local win_idx
    win_idx=$(tmux new-window -P -F '#{window_index}' -t "$session" -n "$name" -c "$dir")

    # Tag the window with ccm metadata
    tmux set-option -wt "${session}:${win_idx}" @ccm_project "$name"
    tmux set-option -wt "${session}:${win_idx}" @ccm_dir "$dir"

    if [[ "$start_claude" == "true" ]]; then
        tmux send-keys -t "${session}:${win_idx}" "$CCM_CLAUDE_CMD" Enter
    fi

    ccm_info "Added project: $name ($dir)"
}

# Remove a ccm project window
ccm_remove() {
    local name="$1"
    [[ -z "$name" ]] && ccm_die "Project name is required"

    local session idx
    session=$(_ccm_session)
    idx=$(ccm_find_window "$name")

    if [[ -z "$idx" ]]; then
        ccm_die "Project window not found: $name"
    fi

    tmux kill-window -t "${session}:${idx}"
    ccm_info "Removed project: $name"
}

# List all ccm-managed project windows
ccm_list() {
    local windows
    windows=$(ccm_list_windows)

    if [[ -z "$windows" ]]; then
        echo "No active projects."
        return
    fi

    printf "${COLOR_BOLD}%-20s %s${COLOR_RESET}\n" "PROJECT" "DIRECTORY"
    printf "%-20s %s\n" "-------" "---------"

    while IFS=$'\t' read -r win_idx win_name project dir; do
        printf "%-20s %s\n" "$project" "$dir"
    done <<< "$windows"
}

# Switch to a ccm project window
ccm_attach() {
    local target="$1"
    [[ -z "$target" ]] && ccm_die "Project name or number is required"

    local session idx
    session=$(_ccm_session)
    [[ -z "$session" ]] && ccm_die "Not inside a tmux session"

    # If target is a number, find by index in the project list
    if [[ "$target" =~ ^[0-9]+$ ]]; then
        local windows
        windows=$(ccm_list_windows)
        local line
        line=$(echo "$windows" | sed -n "${target}p")
        [[ -z "$line" ]] && ccm_die "No project at index: $target"
        idx=$(echo "$line" | cut -f1)
        local name
        name=$(echo "$line" | cut -f3)
    else
        idx=$(ccm_find_window "$target")
        if [[ -z "$idx" ]]; then
            # Try finding by window name directly
            idx=$(tmux list-windows -t "$session" -F '#{window_index}	#{window_name}' 2>/dev/null \
                | awk -F'\t' -v n="$target" '$2 == n {print $1; exit}')
            [[ -z "$idx" ]] && ccm_die "Project not found: $target"
        fi
    fi

    # Check current window
    local current_idx
    current_idx=$(tmux display-message -t "$session" -p '#{window_index}' 2>/dev/null)

    if [[ "$current_idx" == "$idx" ]]; then
        ccm_info "Already in this window"
        return
    fi

    # If Claude Code is not running (SHELL state), auto-start it
    local pane_target="${session}:${idx}"
    local state
    state=$(_detect_window_state "$pane_target")
    if [[ "$state" == "SHELL" ]]; then
        tmux send-keys -t "$pane_target" "$CCM_CLAUDE_CMD_RESUME" Enter
    fi

    # Clear DONE flag when switching to this window
    ccm_clear_done "$pane_target"

    tmux select-window -t "${session}:${idx}"
}

# Stop project windows
ccm_stop() {
    local target="$1"

    if [[ "$target" == "--all" ]]; then
        local windows
        windows=$(ccm_list_windows)
        if [[ -z "$windows" ]]; then
            echo "No active projects."
            return
        fi

        # Auto-save snapshot before stopping all
        ccm_init_dirs
        ccm_snapshot_save "_autosave" 2>/dev/null && \
            ccm_info "Auto-saved snapshot: _autosave"

        while IFS=$'\t' read -r win_idx win_name project dir; do
            local session
            session=$(_ccm_session)
            tmux kill-window -t "${session}:${win_idx}" 2>/dev/null
            ccm_info "Stopped: $project"
        done <<< "$windows"
    elif [[ -n "$target" ]]; then
        ccm_remove "$target"
    else
        ccm_die "Usage: ccm stop [--all|<name>]"
    fi
}

# Capture the visible content of a ccm project window's pane
# Usage: ccm capture [--copy] <name|#id>
ccm_capture() {
    local copy_mode=false
    local target=""

    # Parse flags
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --copy|-c) copy_mode=true; shift ;;
            *) target="$1"; shift ;;
        esac
    done

    [[ -z "$target" ]] && ccm_die "Usage: ccm capture [--copy] <name|#id>"

    local session idx name
    session=$(_ccm_session)

    # If target starts with # or is numeric, treat as index
    if [[ "$target" == \#* ]]; then
        local num="${target#\#}"
        local windows
        windows=$(ccm_list_windows)
        local line
        line=$(echo "$windows" | sed -n "${num}p")
        [[ -z "$line" ]] && ccm_die "No project at ID: $target"
        idx=$(echo "$line" | cut -f1)
        name=$(echo "$line" | cut -f3)
    elif [[ "$target" =~ ^[0-9]+$ ]]; then
        local windows
        windows=$(ccm_list_windows)
        local line
        line=$(echo "$windows" | sed -n "${target}p")
        [[ -z "$line" ]] && ccm_die "No project at ID: #$target"
        idx=$(echo "$line" | cut -f1)
        name=$(echo "$line" | cut -f3)
    else
        name="$target"
        idx=$(ccm_find_window "$target")
        [[ -z "$idx" ]] && ccm_die "Project not found: $target"
    fi

    local output
    output=$(tmux capture-pane -t "${session}:${idx}" -p -S -50)

    if [[ "$copy_mode" == "true" ]]; then
        echo "$output" | pbcopy 2>/dev/null
        ccm_info "Captured ${name} → clipboard"
    else
        echo "=== ccm capture: ${name} ==="
        echo "$output"
        echo "=== end ==="
    fi
}

# Register an existing tmux window as a ccm project
ccm_register() {
    local source_target="$1"
    local new_name="$2"

    [[ -z "$source_target" ]] && ccm_die "Usage: ccm register <window_name|window_index> [name]"

    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && ccm_die "Not inside a tmux session"

    # Find the window
    local win_idx win_name
    if [[ "$source_target" =~ ^[0-9]+$ ]]; then
        # By index
        win_idx="$source_target"
        win_name=$(tmux display-message -t "${session}:${win_idx}" -p '#{window_name}' 2>/dev/null)
        [[ -z "$win_name" ]] && ccm_die "Window not found at index: $source_target"
    else
        # By name
        win_idx=$(tmux list-windows -t "$session" -F '#{window_index}	#{window_name}' 2>/dev/null \
            | awk -F'\t' -v n="$source_target" '$2 == n {print $1; exit}')
        [[ -z "$win_idx" ]] && ccm_die "Window not found: $source_target"
        win_name="$source_target"
    fi

    # Check if already tagged
    local existing
    existing=$(tmux show-option -wt "${session}:${win_idx}" -qv @ccm_project 2>/dev/null)
    if [[ -n "$existing" ]]; then
        ccm_die "Already a ccm project: $existing"
    fi

    local name="${new_name:-$win_name}"
    name=$(ccm_validate_name "$name") || ccm_die "Invalid project name"

    # Check for duplicate project name
    if ccm_project_exists "$name"; then
        ccm_die "Project name already in use: $name"
    fi

    # Get directory from pane
    local dir
    dir=$(tmux display-message -t "${session}:${win_idx}" -p '#{pane_current_path}' 2>/dev/null)

    # Tag the window
    tmux set-option -wt "${session}:${win_idx}" @ccm_project "$name"
    tmux set-option -wt "${session}:${win_idx}" @ccm_dir "$dir"

    # Rename window to project name
    tmux rename-window -t "${session}:${win_idx}" "$name"

    ccm_info "Registered: $win_name → $name"
}

# Open claude in the CURRENT pane for a project (used for side-by-side)
# This is for split-pane usage from dashboard
ccm_open() {
    local dir="$1"
    local name="${2:-}"

    [[ -z "$dir" ]] && ccm_die "Directory is required"
    dir=$(ccm_expand_path "$dir")
    [[ ! -d "$dir" ]] && ccm_die "Directory does not exist: $dir"

    [[ -z "$name" ]] && name=$(basename "$dir")

    # Send commands to current pane
    # cd to directory, then start claude
    tmux send-keys "cd $(printf '%q' "$dir") && (claude --continue 2>/dev/null || claude)" Enter
}
