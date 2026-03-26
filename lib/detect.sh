#!/usr/bin/env bash
# ccm - Claude Code state detection
#
# ══════════════════════════════════════════════════════════════
# STATE DETECTION SPECIFICATION (LEGACY — authoritative version is lib/ccm_core.py)
# ══════════════════════════════════════════════════════════════
#
# Pane-level detection (_detect_pane_state):
#
#   1. No "claude" process as child of pane shell → SHELL
#   2. Claude exists, has non-excluded children   → capture-pane:
#      - PERMIT pattern in bottom 8 lines         → PERMIT
#      - Normal input prompt (❯ ) visible AND
#        NOT accept-edits prompt (❯❯)             → IDLE
#        (children are background workers like MCP servers, not tool execution)
#      - Otherwise                                → BUSY
#   3. Claude exists, no meaningful children       → IDLE
#      (Text generation has no child processes and cannot be
#       reliably distinguished from IDLE via screen content,
#       because the ❯ prompt remains visible during generation.
#       Only tool execution — which spawns children — is detected as BUSY.)
#
#   "Excluded children" (not counted as meaningful):
#     - caffeinate: always running as Claude Code child
#     - Processes in ccm's own PGID (_CCM_PGID)
#
# Window-level detection (ccm_detect_window_state):
#
#   Raw state = highest priority across all panes:
#     PERMIT > BUSY > IDLE > SHELL > DOWN
#
#   Hook-based enhancement (when Claude Code hooks are configured):
#     Signal files: $CCM_HOOK_DIR/<md5_of_cwd>
#     Content: "<unix_timestamp> <state>"
#     Written by: hooks/on-prompt-submit.sh (BUSY)
#                 hooks/on-stop.sh (DONE)
#
#     Integration with raw state:
#       - raw=PERMIT/BUSY → use as-is (process tree is authoritative)
#       - raw=IDLE + hook=BUSY (age < CCM_HOOK_TIMEOUT):
#         → on state transition (prev != BUSY): capture-pane for PERMIT
#         → if PERMIT text found → PERMIT
#         → otherwise (or already BUSY) → BUSY
#         (capture-pane skipped when already BUSY to reduce overhead)
#       - raw=IDLE + hook=DONE (age < CCM_DONE_TIMEOUT):
#         → capture-pane: input prompt visible → DONE
#         → input prompt NOT visible → BUSY (safety net for stale/overwritten hook)
#       - raw=IDLE + no hook signal → fall back to transition-based DONE
#       - raw=SHELL/DOWN → use as-is (ignore hook)
#
#   DONE tracking fallback (when no hooks configured):
#     - BUSY/PERMIT → IDLE transition:
#       capture-pane to distinguish DONE vs late PERMIT
#       (input prompt present → DONE, PERMIT text → PERMIT)
#     - DONE persists for CCM_DONE_TIMEOUT seconds (timestamp)
#     - During DONE persistence: re-check for late PERMIT
#     - Non-IDLE raw state clears DONE flag
#
#   Final safety net (after all hook/fallback paths):
#     If raw=IDLE (Claude running, no children) and input prompt is NOT
#     visible → BUSY (catches multi-turn tool use with expired hook signal,
#     or any state where hooks didn't fire correctly)
#     If PERMIT pattern visible → PERMIT
#     Limitation: pure text generation where ❯ prompt remains visible
#     cannot be caught here (relies on BUSY hook signal instead)
#
#   @ccm_last_done: persistent timestamp (never auto-cleared)
#     for dashboard elapsed time display
#
# Key constants (defined in common.sh):
#   CCM_PATTERN_PERMIT:       grep -Ei pattern for permission prompts
#   CCM_PATTERN_INPUT_PROMPT: grep -E pattern for Claude's normal input prompt (❯ )
#     IMPORTANT: Only matches ❯ (U+276F), NOT > (ASCII greater-than).
#     The > character appears in Claude's output (Markdown blockquotes, shell
#     output, UI decorations) and causes false IDLE if included in the pattern.
#     Must also NOT match accept-edits prompt (❯❯).
#   CCM_PATTERN_ACCEPT_EDITS: grep -E pattern for accept-edits prompt (❯❯)
#     Used to prevent false IDLE when children are present in accept-edits mode.
#   CCM_DONE_TIMEOUT:         seconds before DONE auto-clears
#   CCM_HOOK_DIR:             directory for hook signal files
#   CCM_HOOK_TIMEOUT:         seconds before BUSY hook signal expires
# ══════════════════════════════════════════════════════════════

# Cached outputs to avoid repeated calls within the same scan cycle
_PS_CACHE=""
_PANES_CACHE=""
_SCAN_CACHE_TIME=0

# Refresh all caches (call once per scan cycle)
_refresh_scan_cache() {
    _PS_CACHE=$(ps -eo pid,ppid,pgid,comm 2>/dev/null)
    # Batch: get all panes across all sessions in one call
    _PANES_CACHE=$(tmux list-panes -a -F '#{session_name}:#{window_index}	#{pane_pid}	#{pane_id}' 2>/dev/null)
    # Extended pane info for tree view (includes path and size)
    _PANES_EXT_CACHE=$(tmux list-panes -a -F '#{session_name}:#{window_index}	#{pane_id}	#{pane_pid}	#{pane_current_path}	#{pane_width}x#{pane_height}' 2>/dev/null)
    # Batch: get all ccm window options in one call
    _WIN_OPTS_CACHE=$(tmux list-windows -a -F '#{session_name}:#{window_index}	#{@ccm_prev_state}	#{@ccm_done}	#{@ccm_project}	#{@ccm_dir}' 2>/dev/null)
    _SCAN_CACHE_TIME=$(date +%s)
}

# Ensure caches are fresh (within 2 seconds)
_ensure_scan_cache() {
    local now
    now=$(date +%s)
    if [[ -z "$_PS_CACHE" || $(( now - _SCAN_CACHE_TIME )) -ge 2 ]]; then
        _refresh_scan_cache
    fi
}

# Find a claude process among children of a given PID
_find_claude_pid() {
    local parent_pid="$1"
    _ensure_scan_cache
    echo "$_PS_CACHE" | awk -v p="$parent_pid" -v c="$CCM_CLAUDE_PROCESS_NAME" '$2==p && $4==c {print $1; exit}'
}

# Check if a process has any meaningful child processes
# Check if a process has meaningful child processes (indicating active work)
# Excludes: caffeinate (always running as Claude Code child)
#           ccm's own process group (self-detection)
# PS_CACHE format: pid ppid pgid comm
_CCM_PGID=""

_has_children() {
    local pid="$1"
    [[ -z "$_CCM_PGID" ]] && _CCM_PGID=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
    _ensure_scan_cache
    echo "$_PS_CACHE" | awk -v p="$pid" -v cpg="$_CCM_PGID" '
        $2==p && $3!=cpg && $4!="caffeinate" {found=1; exit}
        END {exit !found}
    '
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

    # Check children (excluding caffeinate and ccm's own PGID)
    if _has_children "$claude_pid"; then
        # Has meaningful children (tool execution, etc.) → BUSY, PERMIT, or IDLE
        local captured
        captured=$(tmux capture-pane -t "$pane_target" -p -S -10 2>/dev/null)
        local near_bottom
        near_bottom=$(echo "$captured" | sed '/^[[:space:]]*$/d' | tail -8)

        if echo "$near_bottom" | grep -qEi "$CCM_PATTERN_PERMIT"; then
            echo "PERMIT"
            return
        fi

        # If the normal input prompt (❯ ) is visible despite children, Claude is idle.
        # Children are background workers (MCP servers, etc.), not active tool execution.
        # During real tool execution, the prompt is replaced by tool output/progress.
        # Exclude accept-edits prompt (❯❯) — Claude may still be executing tools.
        if echo "$near_bottom" | grep -qE "$CCM_PATTERN_INPUT_PROMPT" && \
           ! echo "$near_bottom" | grep -qE "$CCM_PATTERN_ACCEPT_EDITS"; then
            echo "IDLE"
            return
        fi

        echo "BUSY"
        return
    fi

    # No meaningful children → IDLE
    # Note: text generation (no child processes) is not detected as BUSY.
    # Only tool execution (spawns child processes) is detected as BUSY.
    # This trade-off avoids false BUSY from stale screen content.
    echo "IDLE"
}

# Detect the raw state of a window by scanning all its panes
# Args: window target (e.g., "session:window_index")
# Priority: PERMIT > BUSY > IDLE > SHELL > DOWN
_detect_window_state() {
    local win_target="$1"
    _ensure_scan_cache

    # Extract panes for this window from batch cache (no tmux call needed)
    local pane_info
    pane_info=$(echo "$_PANES_CACHE" | awk -F'\t' -v w="$win_target" '$1==w {print $2, $3}')
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
    local now_ts
    now_ts=$(date +%s)
    local raw_state
    raw_state=$(_detect_window_state "$win_target")

    # SHELL/DOWN: no Claude running, skip all further checks
    if [[ "$raw_state" == "SHELL" || "$raw_state" == "DOWN" ]]; then
        tmux set-option -wt "$win_target" @ccm_prev_state "$raw_state" 2>/dev/null
        tmux set-option -wt "$win_target" -u @ccm_done 2>/dev/null || true
        echo "$raw_state"
        return
    fi

    # Single capture-pane for this window (reused by all subsequent checks)
    # Only captured when needed (raw_state == IDLE or has children)
    local _win_captured="" _win_bottom=""
    _capture_win_once() {
        if [[ -z "$_win_captured" ]]; then
            _win_captured=$(tmux capture-pane -t "$win_target" -p -S -10 2>/dev/null)
            _win_bottom=$(echo "$_win_captured" | sed '/^[[:space:]]*$/d' | tail -8)
        fi
    }

    _ensure_scan_cache
    # Read prev_state, done_flag, project_name, and project_dir from batch cache
    # Use newline-separated output to avoid IFS tab collapsing empty fields
    local _cached_fields
    _cached_fields=$(echo "$_WIN_OPTS_CACHE" | awk -F'\t' -v w="$win_target" '$1==w {print $2; print $3; print $4; print $5; exit}')
    local prev_state done_flag project_name project_dir
    { read -r prev_state; read -r done_flag; read -r project_name; read -r project_dir; } <<< "$_cached_fields"

    tmux set-option -wt "$win_target" @ccm_prev_state "$raw_state" 2>/dev/null

    # PERMIT notification: notify when newly entering PERMIT state
    if [[ "$raw_state" == "PERMIT" && "$prev_state" != "PERMIT" ]]; then
        [[ -n "$project_name" ]] && ccm_notify "PERMIT" "$project_name"
    fi

    # --- Hook-based enhancement ---
    if [[ -n "$project_dir" ]]; then
        local hook_signal
        hook_signal=$(_ccm_read_hook_signal "$project_dir")
        if [[ -n "$hook_signal" ]]; then
            local hook_ts="${hook_signal%% *}"
            local hook_state="${hook_signal##* }"
            local hook_age=$(( now_ts - hook_ts ))

            if [[ "$raw_state" == "IDLE" ]]; then
                # raw=IDLE + hook=BUSY: check for PERMIT first (no child processes
                # during permission prompts), then return BUSY for text generation
                if [[ "$hook_state" == "BUSY" && $hook_age -lt ${CCM_HOOK_TIMEOUT:-300} ]]; then
                    # PERMIT check only on state transitions (skip when already BUSY)
                    if [[ "$prev_state" != "BUSY" || "$prev_state" == "PERMIT" ]]; then
                        _capture_win_once
                        if echo "$_win_bottom" | grep -qEi "$CCM_PATTERN_PERMIT"; then
                            tmux set-option -wt "$win_target" @ccm_prev_state "PERMIT" 2>/dev/null
                            [[ -n "$project_name" ]] && ccm_notify "PERMIT" "$project_name"
                            echo "PERMIT"
                            return
                        fi
                    fi
                    echo "BUSY"
                    return
                fi

                # raw=IDLE + hook=DONE → reliable DONE detection
                # Safety net: verify input prompt is visible. If not, Claude
                # may be busy with a new task (missed/overwritten BUSY signal).
                if [[ "$hook_state" == "DONE" && $hook_age -lt ${CCM_DONE_TIMEOUT:-30} ]]; then
                    _capture_win_once
                    if ! echo "$_win_bottom" | grep -qE "$CCM_PATTERN_INPUT_PROMPT"; then
                        # No input prompt → Claude is likely busy, not done
                        echo "BUSY"
                        return
                    fi
                    tmux set-option -wt "$win_target" @ccm_done "$hook_ts" 2>/dev/null
                    tmux set-option -wt "$win_target" @ccm_last_done "$hook_ts" 2>/dev/null
                    if [[ "$prev_state" != "DONE" && -z "$done_flag" ]]; then
                        [[ -z "$project_name" ]] && project_name=$(tmux display-message -t "$win_target" -p '#{window_name}' 2>/dev/null)
                        ccm_notify "DONE" "$project_name"
                    fi
                    echo "DONE"
                    return
                fi
            fi
            # raw=PERMIT/BUSY → process tree is authoritative, fall through
            # raw=SHELL/DOWN → ignore hook signal, fall through
        fi
    fi

    # --- Fallback: transition-based DONE tracking (when no hooks configured) ---
    if [[ "$raw_state" == "IDLE" ]]; then
        if [[ "$prev_state" == "BUSY" || "$prev_state" == "PERMIT" ]]; then
            _capture_win_once

            if ! echo "$_win_bottom" | grep -qE "$CCM_PATTERN_INPUT_PROMPT"; then
                if echo "$_win_bottom" | grep -qEi "$CCM_PATTERN_PERMIT"; then
                    tmux set-option -wt "$win_target" @ccm_prev_state "PERMIT" 2>/dev/null
                    [[ -n "$project_name" ]] && ccm_notify "PERMIT" "$project_name"
                    echo "PERMIT"
                    return
                fi
            fi

            tmux set-option -wt "$win_target" @ccm_done "$now_ts" 2>/dev/null
            tmux set-option -wt "$win_target" @ccm_last_done "$now_ts" 2>/dev/null
            [[ -z "$project_name" ]] && project_name=$(tmux display-message -t "$win_target" -p '#{window_name}' 2>/dev/null)
            ccm_notify "DONE" "$project_name"
            echo "DONE"
            return
        elif [[ -n "$done_flag" ]]; then
            local done_age=$(( now_ts - ${done_flag:-0} ))
            if [[ $done_age -ge 0 && $done_age -lt ${CCM_DONE_TIMEOUT:-30} ]]; then
                _capture_win_once
                if ! echo "$_win_bottom" | grep -qE "$CCM_PATTERN_INPUT_PROMPT"; then
                    if echo "$_win_bottom" | grep -qEi "$CCM_PATTERN_PERMIT"; then
                        tmux set-option -wt "$win_target" -u @ccm_done 2>/dev/null || true
                        tmux set-option -wt "$win_target" @ccm_prev_state "PERMIT" 2>/dev/null
                        [[ -n "$project_name" ]] && ccm_notify "PERMIT" "$project_name"
                        echo "PERMIT"
                        return
                    fi
                fi
                echo "DONE"
                return
            fi
            tmux set-option -wt "$win_target" -u @ccm_done 2>/dev/null || true
        fi
    else
        tmux set-option -wt "$win_target" -u @ccm_done 2>/dev/null || true
    fi

    # --- Final safety net ---
    # If raw=IDLE (Claude running but no children) and input prompt is NOT
    # visible, Claude is likely busy (e.g., multi-turn tool use where the
    # BUSY hook signal expired, or text generation without hooks).
    # Uses tail -8 to account for Claude Code UI elements below the prompt
    # (help hints, context info, etc.)
    if [[ "$raw_state" == "IDLE" ]]; then
        _capture_win_once
        if ! echo "$_win_bottom" | grep -qE "$CCM_PATTERN_INPUT_PROMPT"; then
            if echo "$_win_bottom" | grep -qEi "$CCM_PATTERN_PERMIT"; then
                tmux set-option -wt "$win_target" @ccm_prev_state "PERMIT" 2>/dev/null
                [[ -n "$project_name" ]] && ccm_notify "PERMIT" "$project_name"
                echo "PERMIT"
                return
            fi
            echo "BUSY"
            return
        fi
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
# Uses ccm_detect_window_state() for accurate state (including hook PERMIT detection)
# Only calls tmux rename-window for windows that actually changed
ccm_update_window_names() {
    _ensure_scan_cache

    # Single batch read: window_target, project, current_name
    local all_windows
    all_windows=$(tmux list-windows -a \
        -F '#{session_name}:#{window_index}	#{@ccm_project}	#{window_name}' 2>/dev/null \
        | awk -F'\t' '$2 != ""')
    [[ -z "$all_windows" ]] && return

    local -a _wn_targets=() _wn_new_names=()
    local has_changes=0

    while IFS=$'\t' read -r win_target project current_name; do
        # Use the full detection pipeline (process tree + hooks + PERMIT check)
        local state
        state=$(ccm_detect_window_state "$win_target")

        local icon
        case "$state" in
            PERMIT) icon="⚠" ;;
            BUSY)   icon="◉" ;;
            DONE)   icon="✔" ;;
            IDLE)   icon="●" ;;
            SHELL)  icon="■" ;;
            *)      icon="" ;;
        esac

        local new_name="${icon} ${project}"
        if [[ "$current_name" != "$new_name" ]]; then
            _wn_targets+=("$win_target")
            _wn_new_names+=("$new_name")
            has_changes=1
        fi
    done <<< "$all_windows"

    # Only call tmux rename-window for windows that actually changed
    if [[ $has_changes -eq 1 ]]; then
        for ((i=0; i<${#_wn_targets[@]}; i++)); do
            tmux rename-window -t "${_wn_targets[$i]}" "${_wn_new_names[$i]}" 2>/dev/null
        done
    fi
}

# Get a formatted status line for a window
ccm_format_window_status() {
    local win_target="$1"
    local state
    state=$(ccm_detect_window_state "$win_target")

    case "$state" in
        PERMIT) echo -e "${COLOR_STATE_PERMIT}${STATUS_PERMIT}${COLOR_RESET}" ;;
        IDLE)   echo -e "${COLOR_STATE_IDLE}${STATUS_IDLE}${COLOR_RESET}" ;;
        BUSY)   echo -e "${COLOR_STATE_BUSY}${STATUS_BUSY}${COLOR_RESET}" ;;
        DONE)   echo -e "${COLOR_STATE_DONE}${STATUS_DONE}${COLOR_RESET}" ;;
        SHELL)  echo -e "${COLOR_STATE_SHELL}${STATUS_SHELL}${COLOR_RESET}" ;;
        DOWN)   echo -e "${COLOR_STATE_DOWN}${STATUS_DOWN}${COLOR_RESET}" ;;
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

    # Show hooks status
    if ccm_hooks_configured; then
        echo -e "${COLOR_DIM}Hooks: ON${COLOR_RESET}"
    else
        echo -e "${COLOR_DIM}Hooks: OFF (run 'ccm setup-hooks' for improved detection)${COLOR_RESET}"
    fi
    echo ""

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
            state=$(ccm_detect_window_state "$win_target")
            case "$state" in
                PERMIT) icon="${COLOR_STATE_PERMIT}⚠${COLOR_RESET} " ;;
                BUSY)   icon="${COLOR_STATE_BUSY}◉${COLOR_RESET} " ;;
                DONE)   icon="${COLOR_STATE_DONE}✔${COLOR_RESET} " ;;
                IDLE)   icon="${COLOR_STATE_IDLE}●${COLOR_RESET} " ;;
                SHELL)  icon="${COLOR_STATE_SHELL}■${COLOR_RESET} " ;;
                DOWN)   icon="${COLOR_STATE_DOWN}○${COLOR_RESET} " ;;
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

            # Panes in this window (from batch cache)
            _ensure_scan_cache
            local panes
            panes=$(echo "$_PANES_EXT_CACHE" | awk -F'\t' -v w="$win_target" '$1==w {print $2"\t"$3"\t"$4"\t"$5}')
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
                        PERMIT) p_icon="${COLOR_STATE_PERMIT}⚠${COLOR_RESET}" ;;
                        BUSY)   p_icon="${COLOR_STATE_BUSY}◉${COLOR_RESET}" ;;
                        IDLE)   p_icon="${COLOR_STATE_IDLE}●${COLOR_RESET}" ;;
                        SHELL)  p_icon="${COLOR_STATE_SHELL}■${COLOR_RESET}" ;;
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
