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
    # Refresh ps cache once for this entire scan cycle
    _refresh_ps_cache

    _SESSION_LINES=()
    _SESSION_NAMES=()    # window target (session:win_idx)
    _SESSION_PROJECTS=() # project name
    _SESSION_DIRS=()     # project directory
    _SESSION_STATES=()
    _SESSION_BRANCHES=() # git branch name
    _SESSION_PORTS=()    # listening ports
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
        local branch
        branch=$(ccm_git_branch "$dir")
        local branch_display=""
        [[ -n "$branch" ]] && branch_display="${COLOR_DIM}(${COLOR_RESET}${COLOR_CYAN}${branch}${COLOR_RESET}${COLOR_DIM})${COLOR_RESET}"
        local ports
        ports=$(ccm_detect_ports "$dir" 2>/dev/null)
        local port_display=""
        [[ -n "$ports" ]] && port_display=" ${COLOR_DIM}[${COLOR_RESET}${COLOR_YELLOW}:${ports}${COLOR_RESET}${COLOR_DIM}]${COLOR_RESET}"

        if [[ "$tagged" == "1" ]]; then
            _SESSION_LINES+=("${COLOR_DIM}#${_SESSION_COUNT}${COLOR_RESET} ${status_icon}  ${COLOR_BOLD}${project}${COLOR_RESET} ${branch_display}${port_display} ${COLOR_DIM}${display_dir}${COLOR_RESET}")
        else
            _SESSION_LINES+=("${COLOR_DIM}#${_SESSION_COUNT}${COLOR_RESET} ${status_icon}  ${COLOR_DIM}${project}${COLOR_RESET} ${branch_display}${port_display} ${COLOR_DIM}${display_dir}${COLOR_RESET}")
        fi
        _SESSION_NAMES+=("$win_target")
        _SESSION_PROJECTS+=("$project")
        _SESSION_DIRS+=("$dir")
        _SESSION_STATES+=("$state")
        _SESSION_BRANCHES+=("$branch")
        _SESSION_PORTS+=("$ports")
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
            tmux send-keys -t "$win_target" "$CCM_CLAUDE_CMD_RESUME" Enter
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
    tmux send-keys "$CCM_CLAUDE_CMD_RESUME" Enter
    return 0
}

# ─── Dashboard (modal popup) ───

ccm_dashboard() {
    local refresh_interval="${CCM_DASHBOARD_INTERVAL:-2}"
    local selected=1

    # Prevent concurrent dashboard instances (PID-based check)
    local pidfile="${CCM_TMP_DIR}/dashboard.pid"
    mkdir -p "$CCM_TMP_DIR" 2>/dev/null
    if [[ -f "$pidfile" ]]; then
        local old_pid
        old_pid=$(cat "$pidfile" 2>/dev/null)
        if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
            echo "Dashboard already running (pid $old_pid)."
            return
        fi
        # Stale PID file — process is dead
        rm -f "$pidfile"
    fi
    echo $$ > "$pidfile"

    tput civis 2>/dev/null
    trap 'tput cnorm 2>/dev/null; rm -f "$pidfile"' EXIT INT TERM HUP

    # Show loading indicator immediately
    tput home 2>/dev/null
    tput ed 2>/dev/null
    printf '\n  %s\n' "${COLOR_DIM}Loading...${COLOR_RESET}"

    local needs_rebuild=1

    while true; do
        # Only rebuild project list on timeout or after mutations (add/remove/register)
        if [[ $needs_rebuild -eq 1 ]]; then
            _build_project_list
            needs_rebuild=0
        fi

        # Clamp selection
        if [[ $_SESSION_COUNT -gt 0 ]]; then
            [[ $selected -lt 1 ]] && selected=$_SESSION_COUNT
            [[ $selected -gt $_SESSION_COUNT ]] && selected=1
        fi

        # Last autosave time
        local autosave_info=""
        local autosave_file="${CCM_SNAPSHOT_DIR}/_autosave.json"
        if [[ -f "$autosave_file" ]]; then
            local save_time
            save_time=$(stat -f %m "$autosave_file" 2>/dev/null || stat -c %Y "$autosave_file" 2>/dev/null || echo 0)
            local save_date
            save_date=$(date -r "$save_time" '+%H:%M:%S' 2>/dev/null || date -d "@$save_time" '+%H:%M:%S' 2>/dev/null || echo "")
            [[ -n "$save_date" ]] && autosave_info="  ${COLOR_DIM}Last saved: ${save_date}${COLOR_RESET}"
        fi

        local buf=$'\n'
        buf+="$(_render_list "$selected")"
        buf+=$'\n'
        buf+="  ${COLOR_DIM}[↑↓/jk] select  [Enter] attach  [s]plit  [p]review  [a]dd  [g] register  [r]emove  [S]ave  [/] search  [q/Esc] quit${COLOR_RESET}"$'\n'
        [[ -n "$autosave_info" ]] && buf+="${autosave_info}"$'\n'

        tput home 2>/dev/null
        tput ed 2>/dev/null
        printf '%s' "$buf"

        local key
        if key=$(_read_key_ext "$refresh_interval"); then
            case "$key" in
                UP|k)  selected=$((selected - 1)) ;;
                DOWN|j) selected=$((selected + 1)) ;;
                ENTER)
                    if [[ $_SESSION_COUNT -gt 0 ]]; then
                        _do_attach "$selected" && break
                    fi
                    ;;
                s)
                    if [[ $_SESSION_COUNT -gt 0 ]]; then
                        _do_split "$selected" && break
                    fi
                    ;;
                S)  _dashboard_save ;;
                ESC|q|Q) break ;;
                p|P)
                    if [[ $_SESSION_COUNT -gt 0 ]]; then
                        _dashboard_preview "$selected"
                        needs_rebuild=1
                    fi
                    ;;
                a|A)     _dashboard_add; needs_rebuild=1 ;;
                g|G)     _dashboard_register "$selected"; needs_rebuild=1 ;;
                r|R)     _dashboard_remove; needs_rebuild=1 ;;
                /)       _dashboard_search ;;
            esac
        else
            # Timeout — trigger rebuild on next iteration
            needs_rebuild=1
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
    local completion_showing=0

    while true; do
        local ch
        IFS= read -rsn1 ch

        # Clear previous completion candidates if any
        if [[ $completion_showing -eq 1 ]]; then
            # Restore cursor to saved position (end of prompt+line, before candidates)
            printf '\033[u'
            # Clear from cursor to end of screen (removes all candidate lines)
            printf '\033[J'
            completion_showing=0
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

                # Save cursor position (at end of prompt+line, before candidates)
                printf '\033[s'

                # Show candidates below
                echo ""
                echo "$matches" | while read -r m; do
                    echo "  $(basename "$m")"
                done | head -10
                local shown
                shown=$(echo "$matches" | wc -l | tr -d ' ')
                [[ "$shown" -gt 10 ]] && echo "  ... and $((shown - 10)) more"
                completion_showing=1
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

    # Expand and validate directory
    local expanded_dir
    expanded_dir=$(ccm_expand_path "$dir")
    if [[ ! -d "$expanded_dir" ]]; then
        echo "  ${COLOR_RED}Directory not found: $dir${COLOR_RESET}"
        sleep 1
        tput civis 2>/dev/null
        return
    fi

    # 2) Derive default name from last directory component
    local default_name
    default_name=$(basename "$expanded_dir")

    # 3) Let user edit the name (pre-filled with default)
    _read_line_cancelable "  Project name [${default_name}] (Esc=cancel): "
    [[ $? -ne 0 ]] && { tput civis 2>/dev/null; return; }
    local name="$_INPUT_RESULT"
    [[ -z "$name" ]] && name="$default_name"
    name=$(ccm_validate_name "$name")
    if [[ -z "$name" ]]; then
        echo "  ${COLOR_RED}Invalid project name${COLOR_RESET}"
        sleep 1
        tput civis 2>/dev/null
        return
    fi

    # Create window directly (not via ccm_add) to avoid subshell/session issues in popup
    local session
    session=$(_ccm_session)
    if [[ -z "$session" ]]; then
        echo "  ${COLOR_RED}Cannot detect tmux session${COLOR_RESET}"
        sleep 1
        tput civis 2>/dev/null
        return
    fi

    # Check if project name already exists
    if ccm_project_exists "$name"; then
        echo "  ${COLOR_RED}Project already exists: $name${COLOR_RESET}"
        sleep 1
        tput civis 2>/dev/null
        return
    fi

    # Check for duplicate directory (resolved path comparison)
    local real_dir
    real_dir=$(realpath "$expanded_dir" 2>/dev/null || echo "$expanded_dir")
    local existing_windows
    existing_windows=$(ccm_list_windows)
    if [[ -n "$existing_windows" ]]; then
        while IFS=$'\t' read -r _idx _wname _proj existing_dir; do
            local real_existing
            real_existing=$(realpath "$existing_dir" 2>/dev/null || echo "$existing_dir")
            if [[ "$real_dir" == "$real_existing" ]]; then
                echo "  ${COLOR_RED}Directory already registered as '$_proj'${COLOR_RESET}"
                sleep 1
                tput civis 2>/dev/null
                return
            fi
        done <<< "$existing_windows"
    fi

    # Create new window, tag it, and start Claude
    local win_idx
    if win_idx=$(tmux new-window -P -F '#{window_index}' -t "$session:" -n "$name" -c "$expanded_dir" 2>&1); then
        tmux set-option -wt "${session}:${win_idx}" @ccm_project "$name" 2>/dev/null
        tmux set-option -wt "${session}:${win_idx}" @ccm_dir "$expanded_dir" 2>/dev/null
        tmux send-keys -t "${session}:${win_idx}" "$CCM_CLAUDE_CMD" Enter 2>/dev/null
        echo "  ${COLOR_GREEN}Added: $name${COLOR_RESET}"
        echo "  ${COLOR_DIM}Select it and press Enter to switch, or q/Esc to close${COLOR_RESET}"
        sleep 2
    else
        echo ""
        echo "  ${COLOR_RED}Failed to create window: $win_idx${COLOR_RESET}"
        echo "  ${COLOR_DIM}Press any key to continue...${COLOR_RESET}"
        read -rsn1 -t 10
    fi
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
                        local session
                        session=$(_ccm_session)
                        (CCM_SESSION="$session" ccm_remove "$project") 2>&1
                        sleep 1
                    fi
                fi
                ;;
        esac
    fi
    tput civis 2>/dev/null
}

# Dashboard: register an unregistered window as ccm project
_dashboard_register() {
    local _unused="$1"  # selected idx (kept for backward compat signature)

    # Collect unregistered windows from current session
    local session
    session=$(_ccm_session)
    [[ -z "$session" ]] && return

    local -a unreg_indices=() unreg_names=() unreg_dirs=()
    local unreg_count=0

    local windows
    windows=$(tmux list-windows -t "$session" -F '#{window_index}	#{window_name}	#{@ccm_project}' 2>/dev/null)
    [[ -z "$windows" ]] && return

    while IFS=$'\t' read -r win_idx win_name project; do
        if [[ -z "$project" ]]; then
            local dir
            dir=$(tmux display-message -t "${session}:${win_idx}" -p '#{pane_current_path}' 2>/dev/null)
            unreg_count=$((unreg_count + 1))
            unreg_indices+=("$win_idx")
            unreg_names+=("$win_name")
            unreg_dirs+=("$dir")
        fi
    done <<< "$windows"

    if [[ $unreg_count -eq 0 ]]; then
        echo ""
        echo "  No unregistered windows found."
        sleep 1
        return
    fi

    # Show unregistered windows
    tput cnorm 2>/dev/null
    echo ""
    echo "  ${COLOR_BOLD}Unregistered windows:${COLOR_RESET}"
    for ((i=0; i<unreg_count; i++)); do
        local display_dir="${unreg_dirs[$i]/#$HOME/\~}"
        echo "  ${COLOR_DIM}$((i+1)))${COLOR_RESET} ${unreg_names[$i]}  ${COLOR_DIM}${display_dir}${COLOR_RESET}"
    done
    echo ""
    echo -n "  Select window number (Esc=cancel): "

    local key
    if key=$(_read_key_ext 10); then
        case "$key" in
            ESC) tput civis 2>/dev/null; return ;;
            [1-9])
                echo "$key"
                if [[ "$key" -le "$unreg_count" ]]; then
                    local sel_idx=$((key - 1))
                    local win_idx="${unreg_indices[$sel_idx]}"
                    local win_name="${unreg_names[$sel_idx]}"
                    local dir="${unreg_dirs[$sel_idx]}"
                    local default_name
                    default_name=$(basename "$dir")

                    _read_line_cancelable "  Project name [${default_name}] (Esc=cancel): "
                    if [[ $? -eq 0 ]]; then
                        local new_name="$_INPUT_RESULT"
                        [[ -z "$new_name" ]] && new_name="$default_name"
                        new_name=$(ccm_validate_name "$new_name")
                        if [[ -z "$new_name" ]]; then
                            echo "  ${COLOR_RED}Invalid project name${COLOR_RESET}"
                            sleep 1
                            tput civis 2>/dev/null
                            return
                        fi

                        local win_target="${session}:${win_idx}"
                        tmux set-option -wt "$win_target" @ccm_project "$new_name"
                        tmux set-option -wt "$win_target" @ccm_dir "$dir"
                        tmux rename-window -t "$win_target" "$new_name"
                        ccm_info "  Registered: ${win_name} → ${new_name}"
                        sleep 1
                    fi
                fi
                ;;
        esac
    fi
    tput civis 2>/dev/null
}

# Dashboard: preview a project's pane content
_dashboard_preview() {
    local idx="$1"
    [[ "$idx" -lt 1 || "$idx" -gt "$_SESSION_COUNT" ]] && return

    local win_target="${_SESSION_NAMES[$((idx-1))]}"
    local project="${_SESSION_PROJECTS[$((idx-1))]}"

    tput cnorm 2>/dev/null
    tput home 2>/dev/null
    tput ed 2>/dev/null

    echo ""
    echo "  ${COLOR_BOLD}Preview: ${project}${COLOR_RESET}  ${COLOR_DIM}(press any key to return, 'c' to copy)${COLOR_RESET}"
    echo "  ${COLOR_DIM}────────────────────────────────────${COLOR_RESET}"
    echo ""

    local captured
    captured=$(tmux capture-pane -t "$win_target" -p -S -30 2>/dev/null)
    echo "$captured" | sed 's/^/  /'

    echo ""

    local key
    if key=$(_read_key_ext 30); then
        if [[ "$key" == "c" || "$key" == "C" ]]; then
            echo "$captured" | pbcopy 2>/dev/null
            echo "  ${COLOR_GREEN}Copied to clipboard${COLOR_RESET}"
            sleep 1
        fi
    fi

    tput civis 2>/dev/null
}

# Dashboard: save snapshot
_dashboard_save() {
    [[ $_SESSION_COUNT -eq 0 ]] && return

    tput cnorm 2>/dev/null
    echo ""
    _read_line_cancelable "  Snapshot name [_autosave] (Esc=cancel): "
    if [[ $? -ne 0 ]]; then
        tput civis 2>/dev/null
        return
    fi

    local name="$_INPUT_RESULT"
    [[ -z "$name" ]] && name="_autosave"
    name=$(ccm_validate_name "$name")
    if [[ -z "$name" ]]; then
        echo "  ${COLOR_RED}Invalid name${COLOR_RESET}"
        sleep 1
        tput civis 2>/dev/null
        return
    fi

    ccm_init_dirs
    local save_output
    if save_output=$( (ccm_snapshot_save "$name") 2>&1 ); then
        echo "  ${COLOR_GREEN}Saved: $name${COLOR_RESET}"
    else
        echo "  ${COLOR_RED}Save failed${COLOR_RESET}"
    fi
    sleep 1
    tput civis 2>/dev/null
}

# Dashboard: search projects by name and jump to match
_dashboard_search() {
    [[ $_SESSION_COUNT -eq 0 ]] && return

    tput cnorm 2>/dev/null
    echo ""
    _read_line_cancelable "  Search: "
    if [[ $? -eq 0 && -n "$_INPUT_RESULT" ]]; then
        local query
        query=$(echo "$_INPUT_RESULT" | tr '[:upper:]' '[:lower:]')
        for ((i=0; i<_SESSION_COUNT; i++)); do
            local proj
            proj=$(echo "${_SESSION_PROJECTS[$i]}" | tr '[:upper:]' '[:lower:]')
            if [[ "$proj" == *"$query"* ]]; then
                selected=$((i + 1))
                break
            fi
        done
    fi
    tput civis 2>/dev/null
}

# ─── Shared: scan all windows for statusline/inject ───

# Scan ccm windows and collect their states
# Args: [--all]  include all states (default: BUSY/PERMIT/DONE only)
# Sets _SL_NAMES, _SL_STATES, _SL_COUNT
_scan_active_windows() {
    local include_all=0
    [[ "${1:-}" == "--all" ]] && include_all=1

    _SL_NAMES=()
    _SL_STATES=()
    _SL_DIRS=()
    _SL_WINIDS=()
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

            # Skip non-tagged windows in --all mode
            if [[ -z "$project" ]]; then
                if [[ $include_all -eq 1 ]]; then
                    continue
                fi
                # In active-only mode, check if claude is running
                local state
                state=$(_detect_window_state "$win_target")
                case "$state" in
                    BUSY|PERMIT|DONE) ;;
                    *) continue ;;
                esac
            else
                local state
                state=$(ccm_detect_window_state "$win_target")
                # In active-only mode, filter
                if [[ $include_all -eq 0 ]]; then
                    case "$state" in
                        BUSY|PERMIT|DONE) ;;
                        *) continue ;;
                    esac
                fi
            fi

            local display_name
            if [[ -n "$project" ]]; then
                display_name="$project"
            else
                display_name=$(basename "$actual_dir")
            fi
            _SL_COUNT=$((_SL_COUNT + 1))
            _SL_NAMES+=("$display_name")
            _SL_STATES+=("$state")
            _SL_DIRS+=("$actual_dir")
            _SL_WINIDS+=("$win_idx")
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

# ─── Inject status into tmux status bar (called via run-shell) ───
#
# Modes controlled by @ccm-status-line:
#   0: disabled (no status bar modification)
#   1: icon-only in status-right — ≡ colored by priority state (default)
#   2: dedicated status line(s) with full project details (auto-expands)

# Determine the highest-priority color from scanned states
# Priority: PERMIT (yellow) > BUSY (cyan) > DONE (green) > idle (gray)
_ccm_priority_color() {
    local has_permit=0 has_busy=0 has_done=0
    for ((i=0; i<_SL_COUNT; i++)); do
        case "${_SL_STATES[$i]}" in
            PERMIT) has_permit=1 ;;
            BUSY)   has_busy=1 ;;
            DONE)   has_done=1 ;;
        esac
    done

    if [[ $has_permit -eq 1 ]]; then echo "yellow"
    elif [[ $has_busy -eq 1 ]]; then echo "cyan"
    elif [[ $has_done -eq 1 ]]; then echo "green"
    else echo "#666666"
    fi
}

# Determine the highest-priority icon
_ccm_priority_icon() {
    local has_permit=0 has_busy=0 has_done=0
    for ((i=0; i<_SL_COUNT; i++)); do
        case "${_SL_STATES[$i]}" in
            PERMIT) has_permit=1 ;;
            BUSY)   has_busy=1 ;;
            DONE)   has_done=1 ;;
        esac
    done

    if [[ $has_permit -eq 1 ]]; then echo "⚠ PERMIT"
    elif [[ $has_busy -eq 1 ]]; then echo "◉ BUSY"
    elif [[ $has_done -eq 1 ]]; then echo "✔ DONE"
    else echo "≡"
    fi
}

# Build detailed status entries as an array of "name:icon" strings
_build_detail_entries() {
    local with_extras="${1:-}"  # pass "extras" to include window id, branch, port
    _DETAIL_ENTRIES=()
    for ((i=0; i<_SL_COUNT; i++)); do
        local color icon
        case "${_SL_STATES[$i]}" in
            PERMIT) color="yellow"; icon="⚠" ;;
            BUSY)   color="cyan";   icon="◉" ;;
            DONE)   color="green";  icon="✔" ;;
            IDLE)   color="#888888"; icon="●" ;;
            SHELL)  color="#666666"; icon="■" ;;
            *)      color="#666666"; icon="○" ;;
        esac

        if [[ "$with_extras" == "extras" ]]; then
            # Format: "0:name (branch)[:port]:icon"
            local winid="${_SL_WINIDS[$i]:-}"
            local entry="#[fg=#666666]${winid}:#[fg=#9E9E9E]${_SL_NAMES[$i]}"

            if [[ -n "${_SL_DIRS[$i]:-}" ]]; then
                local branch
                branch=$(ccm_git_branch "${_SL_DIRS[$i]}")
                [[ -n "$branch" ]] && entry+=" #[fg=#666666](#[fg=cyan]${branch}#[fg=#666666])#[fg=#9E9E9E]"
                local ports
                ports=$(ccm_detect_ports "${_SL_DIRS[$i]}" 2>/dev/null)
                [[ -n "$ports" ]] && entry+="#[fg=#666666][:${ports}]#[fg=#9E9E9E]"
            fi

            entry+=":#[fg=${color}]${icon}#[fg=#9E9E9E]"
            _DETAIL_ENTRIES+=("$entry")
        else
            _DETAIL_ENTRIES+=("${_SL_NAMES[$i]}:#[fg=${color}]${icon}#[fg=#9E9E9E]")
        fi
    done
}

ccm_inject_status() {
    # Prevent concurrent execution (PID-based check)
    local pidfile="${CCM_TMP_DIR}/inject.pid"
    mkdir -p "$CCM_TMP_DIR" 2>/dev/null
    if [[ -f "$pidfile" ]]; then
        local old_pid
        old_pid=$(cat "$pidfile" 2>/dev/null)
        if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
            return
        fi
        rm -f "$pidfile"
    fi
    echo $$ > "$pidfile"
    trap 'rm -f "$pidfile"' EXIT RETURN

    # Check mode: 0=window icons only, 1=icon in status-right, 2=dedicated line(s)
    local mode
    mode=$(tmux show-option -gqv @ccm-status-line 2>/dev/null)
    mode="${mode:-0}"

    # Always update window name icons
    ccm_update_window_names 2>/dev/null

    # Helper: clean up dedicated status lines only
    _cleanup_extra_lines() {
        tmux set -g status on 2>/dev/null
        for ((n=1; n<=5; n++)); do
            tmux set -g -u 'status-format['$n']' 2>/dev/null
        done
    }

    # Helper: full cleanup from mode 0/2 (restore window list + remove extra lines)
    _cleanup_mode02() {
        local marker="${CCM_TMP_DIR}/mode2-active"
        if [[ -f "$marker" ]]; then
            rm -f "$marker"
            tmux set -g -u window-status-format 2>/dev/null
            tmux set -g -u window-status-current-format 2>/dev/null
            _cleanup_extra_lines
        fi
    }

    if [[ "$mode" == "1" ]]; then
        # ── Mode 1: ccm-style window list in main bar (no branch/port) ──
        _cleanup_extra_lines
        _scan_active_windows --all

        # Hide standard window list and mark for restoration
        touch "${CCM_TMP_DIR}/mode2-active" 2>/dev/null
        tmux set -g window-status-format '' 2>/dev/null
        tmux set -g window-status-current-format '' 2>/dev/null

        _build_detail_entries  # no extras (no branch/port)

        local refresh="#(${CCM_ROOT}/ccm inject-status 2>/dev/null)"
        local cache_file="${CCM_TMP_DIR}/status-cache"
        local prev_status=""
        [[ -f "$cache_file" ]] && prev_status=$(cat "$cache_file" 2>/dev/null)

        local original
        original=$(tmux show-option -gqv @ccm-orig-status-right 2>/dev/null)

        local new_status
        if [[ $_SL_COUNT -eq 0 ]]; then
            new_status="#[fg=#666666]≡#[default] ${original}${refresh}"
        else
            local detail=""
            for ((i=0; i<${#_DETAIL_ENTRIES[@]}; i++)); do
                [[ $i -gt 0 ]] && detail+=" #[fg=#666666]│#[fg=#9E9E9E]"
                detail+=" ${_DETAIL_ENTRIES[$i]}"
            done
            new_status="#[fg=#9E9E9E,bg=#3a3a3a]${detail} #[fg=#666666]│#[default]${original}${refresh}"
        fi

        if [[ "$new_status" != "$prev_status" ]]; then
            echo "$new_status" > "$cache_file"
            tmux set -g status-right "$new_status" 2>/dev/null
            tmux set -g status-right-length 200 2>/dev/null
        fi
        return
    fi

    # Scan windows: mode 2 includes all projects, mode 0 only active
    if [[ "$mode" == "2" ]]; then
        _scan_active_windows --all
    else
        _scan_active_windows
    fi

    local refresh="#(${CCM_ROOT}/ccm inject-status 2>/dev/null)"
    local cache_file="${CCM_TMP_DIR}/status-cache"
    local prev_status=""
    [[ -f "$cache_file" ]] && prev_status=$(cat "$cache_file" 2>/dev/null)

    local original
    original=$(tmux show-option -gqv @ccm-orig-status-right 2>/dev/null)

    if [[ "$mode" == "2" ]]; then
        # ── Mode 2: dedicated status line(s) with branch/port details ──
        _apply_colored_window_format

        # Restore original status-right (no ccm icon) + refresh trigger
        local main_status="${original}${refresh}"
        tmux set -g status-right "$main_status" 2>/dev/null

        # Mark mode 2 as active and hide window list (dedicated line has this info)
        touch "${CCM_TMP_DIR}/mode2-active" 2>/dev/null
        tmux set -g window-status-format '' 2>/dev/null
        tmux set -g window-status-current-format '' 2>/dev/null

        _build_detail_entries extras

        if [[ $_SL_COUNT -eq 0 ]]; then
            # No ccm projects at all — show idle indicator
            tmux set -g status 2 2>/dev/null
            local fmt="#[align=right]#[fg=#666666,bg=#3a3a3a] ≡ ccm: no projects  "
            tmux set -g 'status-format[1]' "$fmt" 2>/dev/null
            local new_status="mode2:none"
            if [[ "$new_status" != "$prev_status" ]]; then
                echo "$new_status" > "$cache_file"
            fi
        else
            # Calculate how many lines we need based on terminal width
            local term_width
            term_width=$(tmux display-message -p '#{client_width}' 2>/dev/null || echo 120)
            local entries_per_line=$(( (term_width - 10) / 35 ))
            [[ $entries_per_line -lt 1 ]] && entries_per_line=1
            local num_lines=$(( (_SL_COUNT + entries_per_line - 1) / entries_per_line ))
            [[ $num_lines -lt 1 ]] && num_lines=1

            tmux set -g status $((num_lines + 1)) 2>/dev/null

            local line_idx=0
            local entry_idx=0
            local new_status="mode2:${_SL_COUNT}"

            for ((line_idx=0; line_idx<num_lines; line_idx++)); do
                local line_str=""
                local count=0
                while [[ $entry_idx -lt $_SL_COUNT && $count -lt $entries_per_line ]]; do
                    [[ $count -gt 0 ]] && line_str+=" #[fg=#666666]│#[fg=#9E9E9E]"
                    line_str+=" ${_DETAIL_ENTRIES[$entry_idx]}"
                    entry_idx=$((entry_idx + 1))
                    count=$((count + 1))
                done

                local fmt="#[align=right]#[fg=#9E9E9E,bg=#3a3a3a]${line_str}  "
                tmux set -g 'status-format['$((line_idx + 1))']' "$fmt" 2>/dev/null
                new_status+=":${line_idx}"
            done

            # Clear extra lines from previous state
            for ((extra=num_lines+1; extra<=5; extra++)); do
                tmux set -g -u 'status-format['$extra']' 2>/dev/null
            done

            if [[ "$new_status" != "$prev_status" ]]; then
                echo "$new_status" > "$cache_file"
            fi
        fi
    elif [[ "$mode" == "0" ]]; then
        # ── Mode 0: icon in status-right (default) ──
        _cleanup_mode02
        local new_status
        if [[ $_SL_COUNT -eq 0 ]]; then
            # All idle — dim hamburger icon
            new_status="${original}#[fg=#666666,bg=#3a3a3a] ≡ #[default]${refresh}"
        else
            local icon_color icon_char
            icon_color=$(_ccm_priority_color)
            icon_char=$(_ccm_priority_icon)
            new_status="${original}#[fg=${icon_color},bg=#3a3a3a,bold] ${icon_char}  #[default]${refresh}"
        fi

        if [[ "$new_status" != "$prev_status" ]]; then
            echo "$new_status" > "$cache_file"
            tmux set -g status-right "$new_status" 2>/dev/null
        fi
    fi

    # Periodic auto-save snapshot (every 5 minutes)
    local autosave_marker="${CCM_TMP_DIR}/autosave-time"
    local now
    now=$(date +%s)
    local last_save=0
    [[ -f "$autosave_marker" ]] && last_save=$(cat "$autosave_marker" 2>/dev/null)
    if [[ $(( now - last_save )) -ge 300 ]]; then
        # Only save if there are ccm projects
        local windows
        windows=$(ccm_list_windows 2>/dev/null)
        if [[ -n "$windows" ]]; then
            ccm_init_dirs
            (ccm_snapshot_save "_autosave") 2>/dev/null
            echo "$now" > "$autosave_marker"
        fi
    fi
}

# ─── Interactive Tree (popup) ───

# Build tree data into arrays for interactive display
# Sets _TREE_LINES (display), _TREE_TARGETS (win_target or ""), _TREE_COUNT, _TREE_SELECTABLE[]
_build_tree_data() {
    # Refresh ps cache once for this entire scan cycle
    _refresh_ps_cache

    _TREE_LINES=()
    _TREE_TARGETS=()    # window target for selectable lines, empty for non-selectable
    _TREE_COUNT=0
    _TREE_SELECTABLE=() # indices of selectable lines (0-based)

    local current_session
    current_session=$(_ccm_session)
    local current_win_idx
    current_win_idx=$(tmux display-message -p '#{window_index}' 2>/dev/null)

    local all_sessions
    all_sessions=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | sort)
    [[ -z "$all_sessions" ]] && return

    local session_count
    session_count=$(echo "$all_sessions" | wc -l | tr -d ' ')
    local s_idx=0

    while IFS= read -r sess; do
        s_idx=$((s_idx + 1))
        local s_prefix="├── "
        local s_cont="│   "
        [[ $s_idx -eq $session_count ]] && { s_prefix="└── "; s_cont="    "; }

        local s_marker=""
        [[ "$sess" == "$current_session" ]] && s_marker=" ${COLOR_GREEN}◀${COLOR_RESET}"

        _TREE_LINES+=("${s_prefix}${COLOR_BOLD}${sess}${COLOR_RESET}${s_marker}")
        _TREE_TARGETS+=("")
        _TREE_COUNT=$((_TREE_COUNT + 1))

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

            local branch_info=""
            if [[ -n "${dir:-}" ]]; then
                local branch
                branch=$(ccm_git_branch "$dir")
                [[ -n "$branch" ]] && branch_info=" ${COLOR_CYAN}(${branch})${COLOR_RESET}"
            fi

            local port_info=""
            if [[ -n "${dir:-}" ]]; then
                local ports
                ports=$(ccm_detect_ports "$dir" 2>/dev/null)
                [[ -n "$ports" ]] && port_info=" ${COLOR_DIM}[${ports}]${COLOR_RESET}"
            fi

            local w_marker=""
            [[ "$sess" == "$current_session" && "$win_idx" == "$current_win_idx" ]] && w_marker=" ${COLOR_GREEN}◀${COLOR_RESET}"

            local display_name
            if [[ -n "$project" ]]; then
                display_name="${COLOR_BOLD}${project}${COLOR_RESET}"
            else
                display_name="${win_name}"
            fi

            local display_dir=""
            [[ -n "$dir" ]] && display_dir=" ${COLOR_DIM}${dir/#$HOME/\~}${COLOR_RESET}"

            _TREE_LINES+=("${w_prefix}${icon}${display_name}${branch_info}${port_info}${display_dir}${w_marker}")
            _TREE_TARGETS+=("$win_target")
            _TREE_SELECTABLE+=($((_TREE_COUNT)))  # 0-based index
            _TREE_COUNT=$((_TREE_COUNT + 1))

            # Panes (only if >1)
            local panes
            panes=$(tmux list-panes -t "$win_target" -F '#{pane_id}	#{pane_pid}	#{pane_current_path}	#{pane_width}x#{pane_height}' 2>/dev/null)
            local pane_count
            pane_count=$(echo "$panes" | wc -l | tr -d ' ')
            if [[ $pane_count -gt 1 ]]; then
                local p_idx=0
                while IFS=$'\t' read -r pane_id pane_pid pane_path pane_size; do
                    p_idx=$((p_idx + 1))
                    local p_prefix="${w_cont}├── "
                    [[ $p_idx -eq $pane_count ]] && p_prefix="${w_cont}└── "

                    local pane_state p_icon=""
                    pane_state=$(_detect_pane_state "$pane_pid" "$pane_id")
                    case "$pane_state" in
                        PERMIT) p_icon="${COLOR_YELLOW}⚠${COLOR_RESET}" ;;
                        BUSY)   p_icon="${COLOR_CYAN}◉${COLOR_RESET}" ;;
                        IDLE)   p_icon="${COLOR_GREEN}●${COLOR_RESET}" ;;
                        SHELL)  p_icon="${COLOR_BLUE}■${COLOR_RESET}" ;;
                    esac

                    local pane_dir="${pane_path/#$HOME/\~}"
                    _TREE_LINES+=("${p_prefix}${p_icon} ${COLOR_DIM}${pane_id} (${pane_size}) ${pane_dir}${COLOR_RESET}")
                    _TREE_TARGETS+=("")
                    _TREE_COUNT=$((_TREE_COUNT + 1))
                done <<< "$panes"
            fi
        done <<< "$windows"
    done <<< "$all_sessions"
}

# Render tree with selection highlight
_render_tree() {
    local sel_line="$1"  # 0-based line index of selected item
    local buf=""

    for ((i=0; i<_TREE_COUNT; i++)); do
        if [[ "$i" -eq "$sel_line" ]]; then
            buf+="  ${COLOR_BOLD}▶${COLOR_RESET} ${_TREE_LINES[$i]}"$'\n'
        else
            buf+="    ${_TREE_LINES[$i]}"$'\n'
        fi
    done

    echo -n "$buf"
}

# Interactive tree popup
ccm_tree_interactive() {
    local refresh_interval="${CCM_DASHBOARD_INTERVAL:-2}"

    tput civis 2>/dev/null
    trap 'tput cnorm 2>/dev/null' EXIT

    # Show loading indicator immediately
    tput home 2>/dev/null
    tput ed 2>/dev/null
    printf '\n  %s\n' "${COLOR_DIM}Loading...${COLOR_RESET}"

    # Current selection index into _TREE_SELECTABLE array
    local sel_pos=0
    local needs_rebuild=1

    while true; do
        if [[ $needs_rebuild -eq 1 ]]; then
            _build_tree_data
            needs_rebuild=0
        fi

        local sel_count=${#_TREE_SELECTABLE[@]}
        if [[ $sel_count -eq 0 ]]; then
            sel_pos=0
        else
            [[ $sel_pos -lt 0 ]] && sel_pos=$((sel_count - 1))
            [[ $sel_pos -ge $sel_count ]] && sel_pos=0
        fi

        # The actual line index of the selected item
        local sel_line=-1
        [[ $sel_count -gt 0 ]] && sel_line=${_TREE_SELECTABLE[$sel_pos]}

        local buf=$'\n'
        buf+="$(_render_tree "$sel_line")"
        buf+=$'\n'
        buf+="  ${COLOR_DIM}[↑↓/jk] select  [Enter] attach  [q/Esc] quit${COLOR_RESET}"$'\n'

        tput home 2>/dev/null
        tput ed 2>/dev/null
        printf '%s' "$buf"

        local key
        if key=$(_read_key_ext "$refresh_interval"); then
            case "$key" in
                UP|k)   sel_pos=$((sel_pos - 1)) ;;
                DOWN|j) sel_pos=$((sel_pos + 1)) ;;
                ENTER)
                    if [[ $sel_count -gt 0 ]]; then
                        local win_target="${_TREE_TARGETS[${_TREE_SELECTABLE[$sel_pos]}]}"
                        if [[ -n "$win_target" ]]; then
                            # Auto-start Claude if SHELL
                            local project_tag
                            project_tag=$(tmux show-option -wt "$win_target" -qv @ccm_project 2>/dev/null) || true
                            if [[ -n "$project_tag" ]]; then
                                local state
                                state=$(_detect_window_state "$win_target")
                                if [[ "$state" == "SHELL" ]]; then
                                    tmux send-keys -t "$win_target" "$CCM_CLAUDE_CMD_RESUME" Enter
                                fi
                            fi
                            ccm_clear_done "$win_target"

                            local target_session="${win_target%%:*}"
                            local current_session
                            current_session=$(_ccm_session)
                            [[ "$target_session" != "$current_session" ]] && tmux switch-client -t "$target_session"
                            tmux select-window -t "$win_target"
                            break
                        fi
                    fi
                    ;;
                ESC|q|Q) break ;;
            esac
        else
            # Timeout — trigger rebuild on next iteration
            needs_rebuild=1
        fi
    done
}
