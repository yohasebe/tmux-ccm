#!/usr/bin/env bash
# ccm - fzf-based UI helpers (window-based)

# Interactive project selector
ccm_select_project() {
    local prompt="${1:-Select project: }"
    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && ccm_die "Not inside a tmux session"

    local windows
    windows=$(ccm_list_windows)

    [[ -z "$windows" ]] && ccm_die "No active projects"

    local items=""
    while IFS=$'\t' read -r win_idx win_name project dir; do
        local win_target="${session}:${win_idx}"
        local state
        state=$(ccm_detect_window_state "$win_target")
        local display_dir="${dir/#$HOME/~}"

        local icon
        case "$state" in
            PERMIT) icon="⚠" ;;
            IDLE)   icon="●" ;;
            BUSY)   icon="◉" ;;
            DONE)   icon="✔" ;;
            SHELL)  icon="■" ;;
            DOWN)   icon="○" ;;
        esac

        items+="${icon} ${project}  ${display_dir}"$'\n'
    done <<< "$windows"

    local selected
    selected=$(echo "$items" | sed '/^$/d' | fzf --prompt="$prompt" --height=10 --ansi)
    [[ -z "$selected" ]] && return 1

    # Extract project name (second field)
    echo "$selected" | awk '{print $2}'
}

# Interactive ccm menu (for tmux keybinding)
ccm_menu() {
    local choice
    choice=$(printf "Dashboard\nAdd Project\nLoad Snapshot\nSave Snapshot\nStop All" | \
        fzf --prompt="ccm> " --height=8)

    case "$choice" in
        "Dashboard")
            ccm_dashboard
            ;;
        "Add Project")
            echo -n "Directory: "
            read -r dir
            echo -n "Project name (Enter=basename): "
            read -r name
            [[ -n "$dir" ]] && ccm_add "$dir" "$name"
            ;;
        "Load Snapshot")
            ccm_snapshot_load
            ;;
        "Save Snapshot")
            ccm_snapshot_save
            ;;
        "Stop All")
            ccm_stop "--all"
            ;;
    esac
}
