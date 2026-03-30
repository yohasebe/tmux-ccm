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
    local win_target
    win_target=$(tmux list-windows -a -F '#{session_name}:#{window_index}	#{@ccm_dir}' 2>/dev/null \
        | awk -F'\t' -v d="$cwd" '$2==d {print $1; exit}')
    if [[ -n "$win_target" ]]; then
        tmux set-option -wt "$win_target" @ccm_prev_state "$state" 2>/dev/null
    fi
}
