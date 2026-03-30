#!/usr/bin/env bash
# ccm hook library — shared functions for all hook scripts

# Write signal to hook file AND directly update tmux window option
# for instant status bar reflection (no polling delay).
# Args: $1=HOOK_DIR, $2=KEY (md5), $3=STATE (BUSY/DONE/PERMIT), $4=CWD
ccm_write_signal() {
    local hook_dir="$1" key="$2" state="$3" cwd="$4"

    # Write signal file (for dashboard/inject-status polling)
    printf '%s %s' "$(date +%s)" "$state" > "${hook_dir}/${key}"

    # Direct tmux update for instant status bar reflection
    # Find the window whose @ccm_dir matches this cwd
    local win_target project
    local win_info
    win_info=$(tmux list-windows -a -F '#{session_name}:#{window_index}	#{@ccm_dir}	#{@ccm_project}' 2>/dev/null \
        | awk -F'\t' -v d="$cwd" '$2==d {print $1"\t"$3; exit}')
    if [[ -n "$win_info" ]]; then
        win_target="${win_info%%	*}"
        project="${win_info##*	}"
        # Update state option
        tmux set-option -wt "$win_target" @ccm_prev_state "$state" 2>/dev/null
        # Update window name icon for instant status bar change
        if [[ -n "$project" ]]; then
            local icon
            case "$state" in
                PERMIT) icon="⚠" ;; BUSY) icon="◉" ;; DONE) icon="✔" ;;
                IDLE) icon="●" ;; SHELL) icon="■" ;; *) icon="●" ;;
            esac
            tmux rename-window -t "$win_target" "${icon} ${project}" 2>/dev/null
        fi
    fi
}
