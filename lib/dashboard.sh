#!/usr/bin/env bash
# ccm - dashboard display (window-based architecture)

# Read a single keypress, returning arrow keys as special strings
# Output: "UP", "DOWN", "ESC", "ENTER", or the character itself
_read_key_ext() {
    local timeout="$1"
    local key
    if read -rsn1 -t "$timeout" key; then
        if [[ "$key" == $'\033' ]]; then
            local seq
            read -rsn2 -t 0.1 seq 2>/dev/null || true
            case "$seq" in
                '[A') echo "UP"; return 0 ;;
                '[B') echo "DOWN"; return 0 ;;
                *)    echo "ESC"; return 0 ;;
            esac
        elif [[ "$key" == "" ]]; then
            echo "ENTER"
            return 0
        else
            echo "$key"
            return 0
        fi
    fi
    return 1
}

# Read a line with Escape to cancel
# Result is stored in the global variable _INPUT_RESULT
_read_line_cancelable() {
    local prompt="$1"
    _INPUT_RESULT=""
    echo -n "$prompt"
    local line=""
    while true; do
        local ch
        IFS= read -rsn1 ch
        if [[ "$ch" == $'\033' ]]; then
            read -rsn2 -t 0.1 _ 2>/dev/null || true
            echo ""
            return 1
        elif [[ "$ch" == "" ]]; then
            echo ""
            _INPUT_RESULT="$line"
            return 0
        elif [[ "$ch" == $'\177' || "$ch" == $'\b' ]]; then
            if [[ -n "$line" ]]; then
                line="${line%?}"
                printf '\b \b'
            fi
        else
            line+="$ch"
            printf '%s' "$ch"
        fi
    done
}

# Shorten directory path based on available width
_format_dir() {
    local dir="$1"
    local cols
    cols=$(tput cols 2>/dev/null || echo 80)
    if [[ $cols -lt 50 ]]; then
        # Narrow: show only the last directory name
        basename "$dir"
    else
        echo "$dir"
    fi
}

# Format a status icon from state string
_state_icon() {
    local state="$1"
    case "$state" in
        PERMIT) echo "${COLOR_YELLOW}⚠ PERMIT${COLOR_RESET}" ;;
        IDLE)   echo "${COLOR_GREEN}● IDLE${COLOR_RESET}" ;;
        BUSY)   echo "${COLOR_CYAN}◉ BUSY${COLOR_RESET}" ;;
        DONE)   echo "${COLOR_GREEN}✔ DONE${COLOR_RESET}" ;;
        SHELL)  echo "${COLOR_BLUE}■ SHELL${COLOR_RESET}" ;;
        DOWN)   echo "${COLOR_DIM}○ DOWN${COLOR_RESET}" ;;
        WORK)   echo "${COLOR_GREEN}★ WORK${COLOR_RESET}" ;;
    esac
}

# Build project list into _SESSION_LINES array and _SESSION_COUNT
# Scans all sessions: tagged ccm windows + untagged panes running claude
# Deduplicates by resolved directory path
# Current (foreground) window is placed last for easy switching
_build_project_list() {
    _SESSION_LINES=()
    _SESSION_NAMES=()    # window target (session:win_idx)
    _SESSION_PROJECTS=() # project name
    _SESSION_DIRS=()     # project directory
    _SESSION_STATES=()
    _SESSION_COUNT=0

    # Track seen directories (resolved) for deduplication
    local -a _seen_dirs=()

    _is_seen_dir() {
        local resolved
        resolved=$(realpath "$1" 2>/dev/null || echo "$1")
        local d
        for d in "${_seen_dirs[@]}"; do
            [[ "$d" == "$resolved" ]] && return 0
        done
        _seen_dirs+=("$resolved")
        return 1
    }

    # Temporary arrays (before reordering)
    local -a _tmp_names=() _tmp_projects=() _tmp_dirs=() _tmp_states=() _tmp_tagged=()
    local _tmp_count=0

    # Detect current window to move it to the end
    local current_session current_win_idx
    current_session=$(_ccm_session)
    current_win_idx=$(tmux display-message -p '#{window_index}' 2>/dev/null)
    local current_target="${current_session}:${current_win_idx}"

    local all_sessions
    all_sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | sort)
    [[ -z "$all_sessions" ]] && return

    # Process current session first (priority), then others
    local ordered_sessions
    ordered_sessions=$(echo "$all_sessions" | awk -v cs="$current_session" 'BEGIN{print cs} $0!=cs{print $0}')

    while IFS= read -r sess; do
        local windows
        windows=$(tmux list-windows -t "$sess" -F '#{window_index}	#{window_name}	#{@ccm_project}	#{@ccm_dir}' 2>/dev/null)
        [[ -z "$windows" ]] && continue

        while IFS=$'\t' read -r win_idx win_name project dir; do
            local win_target="${sess}:${win_idx}"

            if [[ -n "$project" ]]; then
                _is_seen_dir "$dir" && continue

                local state
                state=$(ccm_detect_window_state "$win_target")

                _tmp_count=$((_tmp_count + 1))
                _tmp_names+=("$win_target")
                _tmp_projects+=("$project")
                _tmp_dirs+=("$dir")
                _tmp_states+=("$state")
                _tmp_tagged+=("1")
            else
                local state
                state=$(_detect_window_state "$win_target")

                case "$state" in
                    IDLE|BUSY|PERMIT)
                        local udir
                        udir=$(tmux display-message -t "$win_target" -p '#{pane_current_path}' 2>/dev/null)
                        _is_seen_dir "$udir" && continue

                        _tmp_count=$((_tmp_count + 1))
                        _tmp_names+=("$win_target")
                        _tmp_projects+=("$(basename "$udir")")
                        _tmp_dirs+=("$udir")
                        _tmp_states+=("$state")
                        _tmp_tagged+=("0")
                        ;;
                esac
            fi
        done <<< "$windows"
    done <<< "$all_sessions"

    # Reorder: current window goes last, others keep order
    local -a order=()
    local current_pos=-1
    for ((i=0; i<_tmp_count; i++)); do
        if [[ "${_tmp_names[$i]}" == "$current_target" ]]; then
            current_pos=$i
        else
            order+=("$i")
        fi
    done
    [[ $current_pos -ge 0 ]] && order+=("$current_pos")

    # Build final arrays with renumbered IDs
    for i in "${order[@]}"; do
        _SESSION_COUNT=$((_SESSION_COUNT + 1))
        local project="${_tmp_projects[$i]}"
        local dir="${_tmp_dirs[$i]}"
        local state="${_tmp_states[$i]}"
        local tagged="${_tmp_tagged[$i]}"
        local win_target="${_tmp_names[$i]}"

        local display_dir="${dir/#$HOME/~}"
        display_dir=$(_format_dir "$display_dir")
        local status_icon
        status_icon=$(_state_icon "$state")

        if [[ "$tagged" == "1" ]]; then
            _SESSION_LINES+=("${COLOR_DIM}#${_SESSION_COUNT}${COLOR_RESET} ${status_icon}  ${COLOR_BOLD}${project}${COLOR_RESET}  ${COLOR_DIM}${display_dir}${COLOR_RESET}")
        else
            _SESSION_LINES+=("${COLOR_DIM}#${_SESSION_COUNT}${COLOR_RESET} ${status_icon}  ${COLOR_DIM}${project}${COLOR_RESET}  ${COLOR_DIM}${display_dir}${COLOR_RESET}")
        fi
        _SESSION_NAMES+=("$win_target")
        _SESSION_PROJECTS+=("$project")
        _SESSION_DIRS+=("$dir")
        _SESSION_STATES+=("$state")
    done
}

# Render project list with selection highlight
# Args: selected_idx (1-based, 0 = no selection)
_render_list() {
    local selected="$1"
    local buf=""

    if [[ $_SESSION_COUNT -eq 0 ]]; then
        buf+="  No active projects."$'\n'
    else
        for ((i=1; i<=_SESSION_COUNT; i++)); do
            if [[ "$i" -eq "$selected" ]]; then
                buf+="  ${COLOR_BOLD}▶ ${_SESSION_LINES[$((i-1))]}${COLOR_RESET}"$'\n'
            else
                buf+="    ${_SESSION_LINES[$((i-1))]}"$'\n'
            fi
        done
    fi

    echo -n "$buf"
}

# Attach to selected window (select-window)
_do_attach() {
    local idx="$1"
    [[ "$idx" -lt 1 || "$idx" -gt "$_SESSION_COUNT" ]] && return 1

    local win_target="${_SESSION_NAMES[$((idx-1))]}"
    local project="${_SESSION_PROJECTS[$((idx-1))]}"

    # Auto-start Claude Code if in SHELL state (only for tagged windows)
    local project_tag
    project_tag=$(tmux show-option -wt "$win_target" -qv @ccm_project 2>/dev/null) || true

    if [[ -n "$project_tag" ]]; then
        local state
        state=$(_detect_window_state "$win_target")
        if [[ "$state" == "SHELL" ]]; then
            tmux send-keys -t "$win_target" "claude --continue 2>/dev/null || claude" Enter
        fi
    fi

    # Clear DONE flag
    ccm_clear_done "$win_target"

    # Extract session from win_target (session:win_idx)
    local target_session="${win_target%%:*}"
    local current_session
    current_session=$(_ccm_session)

    if [[ "$target_session" != "$current_session" ]]; then
        # Cross-session: switch client to target session first
        tmux switch-client -t "$target_session"
    fi

    # Select the window
    tmux select-window -t "$win_target"
    return 0
}

# Open selected project in a split pane (side-by-side)
_do_split() {
    local idx="$1"
    [[ "$idx" -lt 1 || "$idx" -gt "$_SESSION_COUNT" ]] && return 1

    local dir="${_SESSION_DIRS[$((idx-1))]}"
    local project="${_SESSION_PROJECTS[$((idx-1))]}"

    [[ -z "$dir" ]] && return 1

    local expanded_dir
    expanded_dir=$(ccm_expand_path "$dir")

    # Split the current pane horizontally and cd + start claude
    tmux split-window -h -c "$expanded_dir"
    tmux send-keys "claude --continue 2>/dev/null || claude" Enter
    return 0
}

# ─── Dashboard (modal popup) ───

ccm_dashboard() {
    local refresh_interval="${CCM_DASHBOARD_INTERVAL:-2}"
    local selected=1

    tput civis 2>/dev/null
    trap 'tput cnorm 2>/dev/null' EXIT

    while true; do
        _build_project_list

        # Clamp selection
        if [[ $_SESSION_COUNT -gt 0 ]]; then
            [[ $selected -lt 1 ]] && selected=$_SESSION_COUNT
            [[ $selected -gt $_SESSION_COUNT ]] && selected=1
        fi

        local buf=$'\n'
        buf+="$(_render_list "$selected")"
        buf+=$'\n'
        buf+="  ${COLOR_DIM}[↑↓] select  [Enter] attach  [s]plit  [a]dd  [g] register  [r]emove  [q/Esc] quit${COLOR_RESET}"$'\n'

        tput home 2>/dev/null
        tput ed 2>/dev/null
        printf '%s' "$buf"

        local key
        if key=$(_read_key_ext "$refresh_interval"); then
            case "$key" in
                UP)    selected=$((selected - 1)) ;;
                DOWN)  selected=$((selected + 1)) ;;
                ENTER)
                    if [[ $_SESSION_COUNT -gt 0 ]]; then
                        _do_attach "$selected" && break
                    fi
                    ;;
                s|S)
                    if [[ $_SESSION_COUNT -gt 0 ]]; then
                        _do_split "$selected" && break
                    fi
                    ;;
                ESC|q|Q) break ;;
                a|A)     _dashboard_add ;;
                g|G)     _dashboard_register "$selected" ;;
                r|R)     _dashboard_remove ;;
                [1-9])
                    if [[ "$key" -le "$_SESSION_COUNT" ]]; then
                        _do_attach "$key" && break
                    fi
                    ;;
            esac
        fi
    done
}

# Read a path with Tab completion
# Result is stored in _INPUT_RESULT
_read_path_with_completion() {
    local prompt="$1"
    _INPUT_RESULT=""
    echo -n "$prompt"
    local line=""
    local completion_msg=""

    while true; do
        local ch
        IFS= read -rsn1 ch

        # Clear previous completion message if any
        if [[ -n "$completion_msg" ]]; then
            # Move up and clear the completion line
            printf '\033[1A\033[2K'
            # Reprint prompt + current input
            printf '\r%s%s' "$prompt" "$line"
            completion_msg=""
        fi

        if [[ "$ch" == $'\033' ]]; then
            read -rsn2 -t 0.1 _ 2>/dev/null || true
            echo ""
            return 1
        elif [[ "$ch" == "" ]]; then
            # Enter
            echo ""
            _INPUT_RESULT="$line"
            return 0
        elif [[ "$ch" == $'\177' || "$ch" == $'\b' ]]; then
            # Backspace
            if [[ -n "$line" ]]; then
                line="${line%?}"
                printf '\b \b'
            fi
        elif [[ "$ch" == $'\t' ]]; then
            # Tab: directory completion
            # Expand ~ to $HOME only (do NOT resolve symlinks)
            local expanded="${line/#\~/$HOME}"

            # If current input is a complete directory, append / and list children
            if [[ -d "$expanded" && "$expanded" != */ ]]; then
                line+="/"
                expanded+="/"
                printf '/'
            fi

            local matches
            matches=$(compgen -d -- "$expanded" 2>/dev/null)
            [[ -z "$matches" ]] && continue

            local count
            count=$(echo "$matches" | wc -l | tr -d ' ')

            if [[ "$count" -eq 1 ]]; then
                # Single match — complete and add /
                local match="$matches"
                local to_append="${match#$expanded}"
                line="${match/#$HOME/\~}/"
                printf '%s/' "$to_append"
            else
                # Multiple matches — find common prefix
                local common_path
                common_path=$(echo "$matches" | awk '
                    NR==1 { p=$0; next }
                    { while(substr($0,1,length(p))!=p) p=substr(p,1,length(p)-1) }
                    END { print p }
                ')

                local to_append="${common_path#$expanded}"
                if [[ -n "$to_append" ]]; then
                    line="${common_path/#$HOME/\~}"
                    printf '%s' "$to_append"
                fi

                # Show candidates below
                echo ""
                echo "$matches" | while read -r m; do
                    echo "  $(basename "$m")"
                done | head -10
                local shown
                shown=$(echo "$matches" | wc -l | tr -d ' ')
                [[ "$shown" -gt 10 ]] && echo "  ... and $((shown - 10)) more"
                completion_msg="shown"
                printf '%s%s' "$prompt" "$line"
            fi
        else
            line+="$ch"
            printf '%s' "$ch"
        fi
    done
}

# Dashboard: add a project interactively
_dashboard_add() {
    tput cnorm 2>/dev/null
    echo ""

    # 1) Directory with tab completion
    _read_path_with_completion "  Directory (Tab=complete, Esc=cancel): "
    [[ $? -ne 0 ]] && { tput civis 2>/dev/null; return; }
    local dir="$_INPUT_RESULT"
    # Remove trailing slash for basename
    dir="${dir%/}"
    [[ -z "$dir" ]] && { tput civis 2>/dev/null; return; }

    # 2) Derive default name from last directory component
    local default_name
    default_name=$(basename "$(ccm_expand_path "$dir")")

    # 3) Let user edit the name (pre-filled with default)
    _read_line_cancelable "  Project name [${default_name}] (Esc=cancel): "
    [[ $? -ne 0 ]] && { tput civis 2>/dev/null; return; }
    local name="$_INPUT_RESULT"
    [[ -z "$name" ]] && name="$default_name"

    ccm_add "$dir" "$name" 2>&1
    sleep 1
    tput civis 2>/dev/null
}

# Dashboard: remove a project interactively
_dashboard_remove() {
    [[ $_SESSION_COUNT -eq 0 ]] && return

    tput cnorm 2>/dev/null
    echo ""
    echo -n "  Project number to remove (Esc=cancel): "

    local key
    if key=$(_read_key_ext 10); then
        case "$key" in
            ESC) ;;
            [1-9])
                echo "$key"
                if [[ "$key" -le "$_SESSION_COUNT" ]]; then
                    local project="${_SESSION_PROJECTS[$((key-1))]}"
                    if [[ -n "$project" ]]; then
                        ccm_remove "$project" 2>&1
                        sleep 1
                    fi
                fi
                ;;
        esac
    fi
    tput civis 2>/dev/null
}

# Dashboard: register selected window as ccm project
_dashboard_register() {
    local idx="$1"
    [[ "$idx" -lt 1 || "$idx" -gt "$_SESSION_COUNT" ]] && return

    local win_target="${_SESSION_NAMES[$((idx-1))]}"
    local project="${_SESSION_PROJECTS[$((idx-1))]}"

    # Check if already tagged
    local existing
    existing=$(tmux show-option -wt "$win_target" -qv @ccm_project 2>/dev/null) || true
    if [[ -n "$existing" ]]; then
        echo ""
        echo "  Already a ccm project: $existing"
        sleep 1
        return
    fi

    tput cnorm 2>/dev/null
    echo ""

    _read_line_cancelable "  Register '${project}' as (Esc=cancel, Enter=${project}): "
    [[ $? -ne 0 ]] && { tput civis 2>/dev/null; return; }
    local new_name="$_INPUT_RESULT"
    [[ -z "$new_name" ]] && new_name="$project"

    # Extract window index from win_target
    local win_idx="${win_target##*:}"
    local session="${win_target%%:*}"

    # Get directory
    local dir
    dir=$(tmux display-message -t "$win_target" -p '#{pane_current_path}' 2>/dev/null)

    # Tag the window
    tmux set-option -wt "$win_target" @ccm_project "$new_name"
    tmux set-option -wt "$win_target" @ccm_dir "$dir"
    tmux rename-window -t "$win_target" "$new_name"

    ccm_info "Registered: $project → $new_name"
    sleep 1
    tput civis 2>/dev/null
}

# ─── Shared: scan all windows for statusline/inject ───

# Scan all ccm windows and collect active (non-IDLE) ones
# Sets _SL_NAMES, _SL_STATES, _SL_COUNT
_scan_active_windows() {
    _SL_NAMES=()
    _SL_STATES=()
    _SL_COUNT=0

    local -a _sl_seen_dirs=()

    _sl_is_seen_dir() {
        local resolved
        resolved=$(realpath "$1" 2>/dev/null || echo "$1")
        local d
        for d in "${_sl_seen_dirs[@]}"; do
            [[ "$d" == "$resolved" ]] && return 0
        done
        _sl_seen_dirs+=("$resolved")
        return 1
    }

    local all_sessions
    all_sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | sort)
    [[ -z "$all_sessions" ]] && return

    while IFS= read -r sess; do
        local windows
        windows=$(tmux list-windows -t "$sess" -F '#{window_index}	#{window_name}	#{@ccm_project}	#{@ccm_dir}' 2>/dev/null)
        [[ -z "$windows" ]] && continue

        while IFS=$'\t' read -r win_idx win_name project tag_dir; do
            local win_target="${sess}:${win_idx}"

            # Get the actual directory
            local actual_dir
            if [[ -n "$tag_dir" ]]; then
                actual_dir="$tag_dir"
            else
                actual_dir=$(tmux display-message -t "$win_target" -p '#{pane_current_path}' 2>/dev/null)
            fi

            # Deduplicate by resolved directory
            _sl_is_seen_dir "$actual_dir" && continue

            local state
            if [[ -n "$project" ]]; then
                state=$(ccm_detect_window_state "$win_target")
            else
                state=$(_detect_window_state "$win_target")
            fi

            # Statusline: BUSY, PERMIT, and DONE
            case "$state" in
                BUSY|PERMIT|DONE) ;;
                *) continue ;;
            esac

            local display_name
            if [[ -n "$project" ]]; then
                display_name="$project"
            else
                display_name=$(basename "$actual_dir")
            fi
            _SL_COUNT=$((_SL_COUNT + 1))
            _SL_NAMES+=("$display_name")
            _SL_STATES+=("$state")
        done <<< "$windows"
    done <<< "$all_sessions"
}

# ─── Statusline (plain text, for #() usage) ───

ccm_statusline() {
    _scan_active_windows
    [[ $_SL_COUNT -eq 0 ]] && return

    local parts=()
    for ((i=0; i<_SL_COUNT; i++)); do
        local icon
        case "${_SL_STATES[$i]}" in
            PERMIT) icon="⚠" ;;
            BUSY)   icon="◉" ;;
            DONE)   icon="✔" ;;
        esac
        parts+=("${_SL_NAMES[$i]}:${icon}")
    done

    local IFS=" "
    echo "| ${parts[*]} |"
}

# ─── Inject status into tmux status-right (called via run-shell) ───

ccm_inject_status() {
    # Prevent concurrent execution (use mkdir for atomic lock)
    local lockdir="/tmp/ccm-inject.lock"
    if ! mkdir "$lockdir" 2>/dev/null; then
        # Check for stale lock
        local lock_age
        lock_age=$(( $(date +%s) - $(stat -f %m "$lockdir" 2>/dev/null || echo 0) ))
        if [[ $lock_age -gt 5 ]]; then
            rm -rf "$lockdir"
            mkdir "$lockdir" 2>/dev/null || return
        else
            return
        fi
    fi
    trap 'rm -rf "$lockdir"' EXIT RETURN

    _scan_active_windows

    # Update window names with status icons
    ccm_update_window_names 2>/dev/null

    local refresh='#(~/Dropbox/code/ccm/ccm inject-status 2>/dev/null)'
    local new_status

    local dashboard_btn="#[fg=#666666]≡#[fg=#9E9E9E]"

    if [[ $_SL_COUNT -eq 0 ]]; then
        new_status="#[fg=#9E9E9E,bg=#3a3a3a] ${dashboard_btn} ${refresh}"
    else
        local ccm_str=""
        for ((i=0; i<_SL_COUNT; i++)); do
            local color icon
            case "${_SL_STATES[$i]}" in
                PERMIT) color="yellow"; icon="⚠" ;;
                BUSY)   color="cyan";   icon="◉" ;;
                DONE)   color="green";  icon="✔" ;;
            esac
            [[ $i -gt 0 ]] && ccm_str+=" #[fg=#666666]│#[fg=#9E9E9E]"
            ccm_str+=" ${_SL_NAMES[$i]}:#[fg=${color}]${icon}#[fg=#9E9E9E]"
        done
        new_status="#[fg=#9E9E9E,bg=#3a3a3a]${ccm_str} #[fg=#666666]│ ${dashboard_btn} ${refresh}"
    fi

    # Only update tmux if status actually changed (prevents flicker)
    local cache_file="/tmp/ccm-status-cache"
    local prev_status=""
    [[ -f "$cache_file" ]] && prev_status=$(cat "$cache_file" 2>/dev/null)

    if [[ "$new_status" != "$prev_status" ]]; then
        echo "$new_status" > "$cache_file"
        tmux set -g status-right "$new_status" 2>/dev/null
    fi
}
