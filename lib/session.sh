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
    win_idx=$(tmux new-window -P -F '#{window_index}' -t "$session:" -n "$name" -c "$dir")

    # Tag the window with ccm metadata and save original name for restoration
    local orig_name
    orig_name=$(tmux display-message -t "${session}:${win_idx}" -p '#{window_name}' 2>/dev/null)
    tmux set-option -wt "${session}:${win_idx}" @ccm_orig_name "$orig_name" 2>/dev/null
    tmux set-option -wt "${session}:${win_idx}" @ccm_project "$name"
    tmux set-option -wt "${session}:${win_idx}" @ccm_dir "$dir"
    tmux set-option -wt "${session}:${win_idx}" automatic-rename off 2>/dev/null

    if [[ "$start_claude" == "true" ]]; then
        tmux send-keys -t "${session}:${win_idx}" "$CCM_CLAUDE_CMD" Enter
    fi

    ccm_info "Added project: $name ($dir)"

    # Warn if hooks not installed (PERMIT detection requires hooks)
    if ! ccm_hooks_configured; then
        ccm_warn "Hooks not installed. Run 'ccm setup-hooks' for accurate state detection."
    fi

    # Trigger immediate autosave so the new project is captured
    # Skip during snapshot load to avoid overwriting the source snapshot
    if [[ -z "${_CCM_LOADING_SNAPSHOT:-}" ]]; then
        (ccm_snapshot_save "_autosave") &>/dev/null || true
    fi
}

# Unregister a window from ccm (keep window alive, restore original name)
ccm_unregister() {
    local name="$1"
    [[ -z "$name" ]] && ccm_die "Project name is required"

    local session idx
    session=$(_ccm_session)
    idx=$(ccm_find_window "$name")

    if [[ -z "$idx" ]]; then
        ccm_die "Project window not found: $name"
    fi

    local win_target="${session}:${idx}"

    # Restore original window name if saved
    local orig_name
    orig_name=$(tmux show-option -wt "$win_target" -qv @ccm_orig_name 2>/dev/null)
    if [[ -n "$orig_name" ]]; then
        tmux rename-window -t "$win_target" "$orig_name" 2>/dev/null
    fi

    # Restore automatic-rename
    tmux set-option -wt "$win_target" -u automatic-rename 2>/dev/null

    # Remove all ccm tags
    tmux set-option -wt "$win_target" -u @ccm_project 2>/dev/null
    tmux set-option -wt "$win_target" -u @ccm_dir 2>/dev/null
    tmux set-option -wt "$win_target" -u @ccm_orig_name 2>/dev/null
    tmux set-option -wt "$win_target" -u @ccm_prev_state 2>/dev/null
    tmux set-option -wt "$win_target" -u @ccm_done 2>/dev/null
    tmux set-option -wt "$win_target" -u @ccm_last_done 2>/dev/null
    tmux set-option -wt "$win_target" -u @ccm_state_icon 2>/dev/null
    tmux set-option -wt "$win_target" -u @ccm_state_color 2>/dev/null

    ccm_info "Unregistered: $name (window kept)"

    # Trigger immediate autosave
    (ccm_snapshot_save "_autosave") &>/dev/null || true
}

# Rename a ccm project
ccm_rename() {
    local old_name="$1"
    local new_name="$2"
    [[ -z "$old_name" ]] && ccm_die "Usage: ccm rename <current_name> <new_name>"
    [[ -z "$new_name" ]] && ccm_die "New name is required"

    new_name=$(ccm_validate_name "$new_name") || ccm_die "Invalid project name"

    local session idx
    session=$(_ccm_session)
    idx=$(ccm_find_window "$old_name")
    [[ -z "$idx" ]] && ccm_die "Project not found: $old_name"

    if ccm_project_exists "$new_name"; then
        ccm_die "Project name already in use: $new_name"
    fi

    local win_target="${session}:${idx}"
    tmux set-option -wt "$win_target" @ccm_project "$new_name" 2>/dev/null
    tmux rename-window -t "$win_target" "$new_name" 2>/dev/null

    ccm_info "Renamed: $old_name → $new_name"

    # Trigger immediate autosave
    (ccm_snapshot_save "_autosave") &>/dev/null || true
}

# Remove a ccm project window (kill window)
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

    # If target is a number, treat as tmux window index
    if [[ "$target" =~ ^[0-9]+$ ]]; then
        local windows
        windows=$(ccm_list_windows)
        local line
        line=$(echo "$windows" | awk -F'\t' -v idx="$target" '$1 == idx')
        [[ -z "$line" ]] && ccm_die "No ccm project at window index: $target"
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
    state=$(ccm_detect_window_state "$pane_target")
    [[ "$state" == "SHELL" ]] && ccm_auto_start_claude "$pane_target"

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

    # If target starts with # or is numeric, treat as tmux window index
    if [[ "$target" == \#* ]]; then
        local num="${target#\#}"
        local windows
        windows=$(ccm_list_windows)
        local line
        line=$(echo "$windows" | awk -F'\t' -v idx="$num" '$1 == idx')
        [[ -z "$line" ]] && ccm_die "No ccm project at window index: $num"
        idx=$(echo "$line" | cut -f1)
        name=$(echo "$line" | cut -f3)
    elif [[ "$target" =~ ^[0-9]+$ ]]; then
        local windows
        windows=$(ccm_list_windows)
        local line
        line=$(echo "$windows" | awk -F'\t' -v idx="$target" '$1 == idx')
        [[ -z "$line" ]] && ccm_die "No ccm project at window index: $target"
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
        if echo "$output" | ccm_clipboard_copy 2>/dev/null; then
            ccm_info "Captured ${name} → clipboard"
        else
            ccm_warn "No clipboard tool available (install pbcopy, xclip, or xsel)"
        fi
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

    # Tag the window and save original name for restoration
    tmux set-option -wt "${session}:${win_idx}" @ccm_orig_name "$win_name" 2>/dev/null
    tmux set-option -wt "${session}:${win_idx}" @ccm_project "$name"
    tmux set-option -wt "${session}:${win_idx}" @ccm_dir "$dir"
    tmux set-option -wt "${session}:${win_idx}" automatic-rename off 2>/dev/null

    # Rename window to project name
    tmux rename-window -t "${session}:${win_idx}" "$name"

    ccm_info "Registered: $win_name → $name"

    # Trigger immediate autosave
    (ccm_snapshot_save "_autosave") &>/dev/null || true
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

# Control pane title display (pane-border-status)
# Claude Code sets pane titles via terminal escape sequences to describe the current session.
# This function toggles tmux's pane-border-status to show/hide those titles.
ccm_pane_title() {
    local action="${1:-toggle}"
    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && ccm_die "Not inside a tmux session"

    case "$action" in
        on)
            tmux set-option -t "$session" pane-border-status top 2>/dev/null
            tmux set-option -t "$session" pane-border-format "#{pane_title}" 2>/dev/null
            tmux set-option -g @ccm-pane-title on 2>/dev/null
            tmux display-message "ccm: pane title ON" 2>/dev/null
            ;;
        off)
            tmux set-option -t "$session" -u pane-border-status 2>/dev/null
            tmux set-option -t "$session" -u pane-border-format 2>/dev/null
            tmux set-option -g @ccm-pane-title off 2>/dev/null
            tmux display-message "ccm: pane title OFF" 2>/dev/null
            ;;
        toggle)
            local current
            current=$(tmux show-option -t "$session" -qv pane-border-status 2>/dev/null)
            if [[ "$current" == "top" ]]; then
                ccm_pane_title off
            else
                ccm_pane_title on
            fi
            ;;
        status)
            local current
            current=$(tmux show-option -t "$session" -qv pane-border-status 2>/dev/null)
            if [[ "$current" == "top" ]]; then
                echo "pane-title: on"
            else
                echo "pane-title: off"
            fi
            ;;
        *)
            ccm_die "Usage: ccm pane-title [on|off|toggle|status]"
            ;;
    esac
}
