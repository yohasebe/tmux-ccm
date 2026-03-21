#!/usr/bin/env bash
# ccm - Claude Code state detection
# Uses process tree inspection for reliability

# Cached ps output to avoid repeated ps calls within the same scan cycle
_PS_CACHE=""
_PS_CACHE_TIME=0

# Refresh ps cache (call once per scan cycle)
_refresh_ps_cache() {
    _PS_CACHE=$(ps -eo pid,ppid,comm 2>/dev/null)
    _PS_CACHE_TIME=$(date +%s)
}

# Ensure ps cache is fresh (within 2 seconds)
_ensure_ps_cache() {
    local now
    now=$(date +%s)
    if [[ -z "$_PS_CACHE" || $(( now - _PS_CACHE_TIME )) -ge 2 ]]; then
        _refresh_ps_cache
    fi
}

# Find a claude process among children of a given PID
_find_claude_pid() {
    local parent_pid="$1"
    _ensure_ps_cache
    echo "$_PS_CACHE" | awk -v p="$parent_pid" -v c="$CCM_CLAUDE_PROCESS_NAME" '$2==p && $3==c {print $1; exit}'
}

# Check if a process has any child processes
_has_children() {
    local pid="$1"
    _ensure_ps_cache
    echo "$_PS_CACHE" | awk -v p="$pid" '$2==p {found=1; exit} END {exit !found}'
}

# Detect state of a single pane by its PID and pane target
# Returns: PERMIT, IDLE, BUSY, SHELL
_detect_pane_state() {
    local pane_pid="$1"
    local pane_target="$2"

    local claude_pid
    claude_pid=$(_find_claude_pid "$pane_pid")

    if [[ -z "$claude_pid" ]]; then
        echo "SHELL"
        return
    fi

    # Check for permission prompt via screen capture
    local captured
    captured=$(tmux capture-pane -t "$pane_target" -p -S -10 2>/dev/null)
    local near_bottom
    near_bottom=$(echo "$captured" | sed '/^[[:space:]]*$/d' | tail -8)

    if echo "$near_bottom" | grep -qEi "$CCM_PATTERN_PERMIT"; then
        echo "PERMIT"
        return
    fi

    # Process-based BUSY detection:
    # Claude Code spawns child processes (caffeinate, gh, zsh) while actively working.
    # When idle (waiting for user input), claude has zero children.
    if _has_children "$claude_pid"; then
        echo "BUSY"
        return
    fi

    echo "IDLE"
}

# Detect the raw state of a window by scanning all its panes
# Args: window target (e.g., "session:window_index")
# Priority: PERMIT > BUSY > IDLE > SHELL > DOWN
_detect_window_state() {
    local win_target="$1"

    local pane_info
    pane_info=$(tmux list-panes -t "$win_target" -F '#{pane_pid} #{pane_id}' 2>/dev/null)
    if [[ -z "$pane_info" ]]; then
        echo "DOWN"
        return
    fi

    local best_state="SHELL"

    while read -r pid pane_id; do
        local state
        state=$(_detect_pane_state "$pid" "$pane_id")

        case "$state" in
            PERMIT) echo "PERMIT"; return ;;
            BUSY)   best_state="BUSY" ;;
            IDLE)   [[ "$best_state" != "BUSY" ]] && best_state="IDLE" ;;
        esac
    done <<< "$pane_info"

    echo "$best_state"
}

# Legacy: detect raw state of a session (scans all panes in all windows)
_detect_raw_state() {
    local session="$1"

    if ! tmux has-session -t "$session" 2>/dev/null; then
        echo "DOWN"
        return
    fi

    local pane_info
    pane_info=$(tmux list-panes -t "$session" -s -F '#{pane_pid} #{pane_id}' 2>/dev/null)
    if [[ -z "$pane_info" ]]; then
        echo "DOWN"
        return
    fi

    local best_state="SHELL"

    while read -r pid pane_id; do
        local state
        state=$(_detect_pane_state "$pid" "$pane_id")

        case "$state" in
            PERMIT) echo "PERMIT"; return ;;
            BUSY)   best_state="BUSY" ;;
            IDLE)   [[ "$best_state" != "BUSY" ]] && best_state="IDLE" ;;
        esac
    done <<< "$pane_info"

    echo "$best_state"
}

# Detect state with DONE tracking for a window
# Args: window target (e.g., "session:window_index")
# Returns: PERMIT, IDLE, BUSY, DONE, SHELL, or DOWN
ccm_detect_window_state() {
    local win_target="$1"
    local raw_state
    raw_state=$(_detect_window_state "$win_target")

    local prev_state
    prev_state=$(tmux show-option -wt "$win_target" -qv @ccm_prev_state 2>/dev/null) || true

    tmux set-option -wt "$win_target" @ccm_prev_state "$raw_state" 2>/dev/null

    # DONE detection: BUSY/PERMIT→IDLE transition = response completed
    if [[ "$raw_state" == "IDLE" ]]; then
        local done_flag
        done_flag=$(tmux show-option -wt "$win_target" -qv @ccm_done 2>/dev/null) || true

        if [[ "$prev_state" == "BUSY" || "$prev_state" == "PERMIT" ]]; then
            tmux set-option -wt "$win_target" @ccm_done "1" 2>/dev/null
            # Notify user of completion
            local project_name
            project_name=$(tmux show-option -wt "$win_target" -qv @ccm_project 2>/dev/null) || true
            [[ -z "$project_name" ]] && project_name=$(tmux display-message -t "$win_target" -p '#{window_name}' 2>/dev/null)
            tmux display-message "✔ ${project_name}: response complete" 2>/dev/null
            echo "DONE"
            return
        elif [[ "$done_flag" == "1" ]]; then
            echo "DONE"
            return
        fi
    else
        tmux set-option -wt "$win_target" -u @ccm_done 2>/dev/null || true
    fi

    echo "$raw_state"
}

# Legacy: detect state with DONE tracking for a session
ccm_detect_state() {
    local session="$1"
    local raw_state
    raw_state=$(_detect_raw_state "$session")

    local prev_state
    prev_state=$(tmux show-option -t "$session" -qv @ccm_prev_state 2>/dev/null) || true

    tmux set -t "$session" @ccm_prev_state "$raw_state" 2>/dev/null

    if [[ "$raw_state" == "IDLE" ]]; then
        local done_flag
        done_flag=$(tmux show-option -t "$session" -qv @ccm_done 2>/dev/null) || true

        if [[ "$prev_state" == "BUSY" || "$prev_state" == "PERMIT" ]]; then
            tmux set -t "$session" @ccm_done "1" 2>/dev/null
            echo "DONE"
            return
        elif [[ "$done_flag" == "1" ]]; then
            echo "DONE"
            return
        fi
    else
        tmux set -t "$session" -u @ccm_done 2>/dev/null || true
    fi

    echo "$raw_state"
}

# Clear DONE flag for a window target
ccm_clear_done() {
    local target="$1"
    tmux set-option -wt "$target" -u @ccm_done 2>/dev/null || true
    tmux set-option -wt "$target" -u @ccm_prev_state 2>/dev/null || true
}

# Update window names with status icons
# Called from inject-status to keep window names in sync
ccm_update_window_names() {
    local all_sessions
    all_sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | sort)
    [[ -z "$all_sessions" ]] && return

    while IFS= read -r sess; do
        local windows
        windows=$(tmux list-windows -t "$sess" -F '#{window_index}	#{@ccm_project}' 2>/dev/null)
        [[ -z "$windows" ]] && continue

        while IFS=$'\t' read -r win_idx project; do
            [[ -z "$project" ]] && continue

            local win_target="${sess}:${win_idx}"
            local state
            state=$(_detect_window_state "$win_target")

            # Check DONE flag
            local done_flag
            done_flag=$(tmux show-option -wt "$win_target" -qv @ccm_done 2>/dev/null) || true
            [[ "$done_flag" == "1" && "$state" == "IDLE" ]] && state="DONE"

            local icon state_color
            case "$state" in
                PERMIT) icon="⚠"; state_color="yellow" ;;
                BUSY)   icon="◉"; state_color="cyan" ;;
                DONE)   icon="✔"; state_color="green" ;;
                IDLE)   icon="●"; state_color="colour244" ;;
                SHELL)  icon="■"; state_color="colour240" ;;
                *)      icon="";  state_color="default" ;;
            esac

            # Window name = icon + project name
            local new_name="${icon} ${project}"
            local current_name
            current_name=$(tmux display-message -t "$win_target" -p '#{window_name}' 2>/dev/null)
            if [[ "$current_name" != "$new_name" ]]; then
                tmux rename-window -t "$win_target" "$new_name" 2>/dev/null
            fi
        done <<< "$windows"
    done <<< "$all_sessions"
}

# Legacy: clear DONE flag for a session
ccm_clear_done_session() {
    local session="$1"
    tmux set -t "$session" -u @ccm_done 2>/dev/null || true
    tmux set -t "$session" -u @ccm_prev_state 2>/dev/null || true
}

# Get a formatted status line for a window
ccm_format_window_status() {
    local win_target="$1"
    local state
    state=$(ccm_detect_window_state "$win_target")

    case "$state" in
        PERMIT) echo -e "${COLOR_YELLOW}${STATUS_PERMIT}${COLOR_RESET}" ;;
        IDLE)   echo -e "${COLOR_GREEN}${STATUS_IDLE}${COLOR_RESET}" ;;
        BUSY)   echo -e "${COLOR_CYAN}${STATUS_BUSY}${COLOR_RESET}" ;;
        DONE)   echo -e "${COLOR_GREEN}✔ DONE${COLOR_RESET}" ;;
        SHELL)  echo -e "${COLOR_BLUE}${STATUS_SHELL}${COLOR_RESET}" ;;
        DOWN)   echo -e "${COLOR_DIM}${STATUS_DOWN}${COLOR_RESET}" ;;
    esac
}

# Legacy format status for a session
ccm_format_status() {
    local session="$1"
    local state
    state=$(ccm_detect_state "$session")

    case "$state" in
        PERMIT) echo -e "${COLOR_YELLOW}${STATUS_PERMIT}${COLOR_RESET}" ;;
        IDLE)   echo -e "${COLOR_GREEN}${STATUS_IDLE}${COLOR_RESET}" ;;
        BUSY)   echo -e "${COLOR_CYAN}${STATUS_BUSY}${COLOR_RESET}" ;;
        DONE)   echo -e "${COLOR_GREEN}✔ DONE${COLOR_RESET}" ;;
        SHELL)  echo -e "${COLOR_BLUE}${STATUS_SHELL}${COLOR_RESET}" ;;
        DOWN)   echo -e "${COLOR_DIM}${STATUS_DOWN}${COLOR_RESET}" ;;
    esac
}

# Show status of all ccm project windows
ccm_status() {
    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && { echo "Not inside a tmux session."; return; }

    local windows
    windows=$(ccm_list_windows)

    if [[ -z "$windows" ]]; then
        echo "No active projects."
        return
    fi

    printf "${COLOR_BOLD}%-12s %-20s %-16s %-12s %s${COLOR_RESET}\n" "STATUS" "PROJECT" "BRANCH" "PORTS" "DIRECTORY"
    printf "%-12s %-20s %-16s %-12s %s\n" "------" "-------" "------" "-----" "---------"

    while IFS=$'\t' read -r win_idx win_name project dir; do
        local win_target="${session}:${win_idx}"
        local status
        status=$(ccm_format_window_status "$win_target")
        local branch
        branch=$(ccm_git_branch "$dir")
        [[ -z "$branch" ]] && branch="-"
        local ports
        ports=$(ccm_detect_ports "$dir")
        [[ -z "$ports" ]] && ports="-"
        printf "%-22s %-20s %-16s %-12s %s\n" "$status" "$project" "$branch" "$ports" "$dir"
    done <<< "$windows"
}

# Show listening ports per project
ccm_ports() {
    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && { echo "Not inside a tmux session."; return; }

    local windows
    windows=$(ccm_list_windows)

    if [[ -z "$windows" ]]; then
        echo "No active projects."
        return
    fi

    printf "${COLOR_BOLD}%-20s %-16s %s${COLOR_RESET}\n" "PROJECT" "PORTS" "DIRECTORY"
    printf "%-20s %-16s %s\n" "-------" "-----" "---------"

    while IFS=$'\t' read -r win_idx win_name project dir; do
        local ports
        ports=$(ccm_detect_ports "$dir")
        [[ -z "$ports" ]] && ports="-"
        printf "%-20s %-16s %s\n" "$project" "$ports" "$dir"
    done <<< "$windows"
}

# Show hierarchical tree of all sessions, windows, and panes
ccm_tree() {
    local all_sessions
    all_sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | sort)
    [[ -z "$all_sessions" ]] && { echo "No tmux sessions."; return; }

    local current_session
    current_session=$(_ccm_session)
    local current_win_idx
    current_win_idx=$(tmux display-message -p '#{window_index}' 2>/dev/null)
    local current_pane_id
    current_pane_id=$(tmux display-message -p '#{pane_id}' 2>/dev/null)

    local session_count
    session_count=$(echo "$all_sessions" | wc -l | tr -d ' ')
    local s_idx=0

    while IFS= read -r sess; do
        s_idx=$((s_idx + 1))
        local s_prefix="├── "
        local s_cont="│   "
        [[ $s_idx -eq $session_count ]] && { s_prefix="└── "; s_cont="    "; }

        # Session line
        local s_marker=""
        [[ "$sess" == "$current_session" ]] && s_marker=" ${COLOR_GREEN}◀${COLOR_RESET}"
        printf "${s_prefix}${COLOR_BOLD}%s${COLOR_RESET}%s\n" "$sess" "$s_marker"

        # Windows in this session
        local windows
        windows=$(tmux list-windows -t "$sess" -F '#{window_index}	#{window_name}	#{@ccm_project}	#{@ccm_dir}' 2>/dev/null)
        [[ -z "$windows" ]] && continue

        local win_count
        win_count=$(echo "$windows" | wc -l | tr -d ' ')
        local w_idx=0

        while IFS=$'\t' read -r win_idx win_name project dir; do
            w_idx=$((w_idx + 1))
            local w_prefix="${s_cont}├── "
            local w_cont="${s_cont}│   "
            [[ $w_idx -eq $win_count ]] && { w_prefix="${s_cont}└── "; w_cont="${s_cont}    "; }

            local win_target="${sess}:${win_idx}"

            # Window status
            local state icon=""
            if [[ -n "$project" ]]; then
                state=$(ccm_detect_window_state "$win_target")
            else
                state=$(_detect_window_state "$win_target")
            fi
            case "$state" in
                PERMIT) icon="${COLOR_YELLOW}⚠${COLOR_RESET} " ;;
                BUSY)   icon="${COLOR_CYAN}◉${COLOR_RESET} " ;;
                DONE)   icon="${COLOR_GREEN}✔${COLOR_RESET} " ;;
                IDLE)   icon="${COLOR_GREEN}●${COLOR_RESET} " ;;
                SHELL)  icon="${COLOR_BLUE}■${COLOR_RESET} " ;;
                DOWN)   icon="${COLOR_DIM}○${COLOR_RESET} " ;;
            esac

            # Git branch
            local branch_info=""
            local check_dir="${dir:-}"
            if [[ -n "$check_dir" ]]; then
                local branch
                branch=$(ccm_git_branch "$check_dir")
                [[ -n "$branch" ]] && branch_info=" ${COLOR_CYAN}(${branch})${COLOR_RESET}"
            fi

            # Port info
            local port_info=""
            if [[ -n "$check_dir" ]]; then
                local ports
                ports=$(ccm_detect_ports "$check_dir" 2>/dev/null)
                [[ -n "$ports" ]] && port_info=" ${COLOR_DIM}[${ports}]${COLOR_RESET}"
            fi

            # Current window marker
            local w_marker=""
            [[ "$sess" == "$current_session" && "$win_idx" == "$current_win_idx" ]] && w_marker=" ${COLOR_GREEN}◀${COLOR_RESET}"

            # Display name
            local display_name
            if [[ -n "$project" ]]; then
                display_name="${COLOR_BOLD}${project}${COLOR_RESET}"
            else
                display_name="${win_name}"
            fi

            local display_dir=""
            [[ -n "$dir" ]] && display_dir=" ${COLOR_DIM}${dir/#$HOME/\~}${COLOR_RESET}"

            printf "${w_prefix}${icon}${display_name}${branch_info}${port_info}${display_dir}${w_marker}\n"

            # Panes in this window
            local panes
            panes=$(tmux list-panes -t "$win_target" -F '#{pane_id}	#{pane_pid}	#{pane_current_path}	#{pane_width}x#{pane_height}' 2>/dev/null)
            local pane_count
            pane_count=$(echo "$panes" | wc -l | tr -d ' ')
            # Only show panes if more than 1
            if [[ $pane_count -gt 1 ]]; then
                local p_idx=0
                while IFS=$'\t' read -r pane_id pane_pid pane_path pane_size; do
                    p_idx=$((p_idx + 1))
                    local p_prefix="${w_cont}├── "
                    [[ $p_idx -eq $pane_count ]] && p_prefix="${w_cont}└── "

                    local pane_state
                    pane_state=$(_detect_pane_state "$pane_pid" "$pane_id")
                    local p_icon=""
                    case "$pane_state" in
                        PERMIT) p_icon="${COLOR_YELLOW}⚠${COLOR_RESET}" ;;
                        BUSY)   p_icon="${COLOR_CYAN}◉${COLOR_RESET}" ;;
                        IDLE)   p_icon="${COLOR_GREEN}●${COLOR_RESET}" ;;
                        SHELL)  p_icon="${COLOR_BLUE}■${COLOR_RESET}" ;;
                    esac

                    local p_marker=""
                    [[ "$pane_id" == "$current_pane_id" ]] && p_marker=" ${COLOR_GREEN}◀${COLOR_RESET}"

                    local pane_dir="${pane_path/#$HOME/\~}"
                    printf "${p_prefix}${p_icon} ${COLOR_DIM}${pane_id} (${pane_size}) ${pane_dir}${COLOR_RESET}${p_marker}\n"
                done <<< "$panes"
            fi
        done <<< "$windows"
    done <<< "$all_sessions"
}
