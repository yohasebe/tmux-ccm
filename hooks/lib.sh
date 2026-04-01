#!/usr/bin/env bash
# ccm hook library — shared functions for all hook scripts

# Write signal to hook file AND directly update tmux window option
# for instant status bar reflection (no polling delay).
# Args: $1=HOOK_DIR, $2=KEY (md5), $3=STATE (BUSY/DONE/PERMIT/SHELL), $4=CWD, $5=DETAIL (optional)
ccm_write_signal() {
    local hook_dir="$1" key="$2" state="$3" cwd="$4" detail="${5:-}"

    # Write signal file (for dashboard/inject-status polling)
    # Format: "<timestamp> <state>" or "<timestamp> <state> <detail>"
    if [[ -n "$detail" ]]; then
        printf '%s %s %s' "$(date +%s)" "$state" "$detail" > "${hook_dir}/${key}"
    else
        printf '%s %s' "$(date +%s)" "$state" > "${hook_dir}/${key}"
    fi

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

        # PERMIT: instant status-right icon update + desktop notification
        # (eliminates up to 3s polling delay for the most critical state)
        if [[ "$state" == "PERMIT" && -n "$project" ]]; then
            _ccm_instant_permit_icon "$win_target" "$project" &
            _ccm_instant_notify "$project" "$detail" &
        fi
    fi
}

# Signal inject-status to prioritize PERMIT display on next cycle.
# Instead of modifying status-right directly (which races with inject-status),
# we set a tmux option flag that inject-status reads and clears.
# Combined with window rename (already done by ccm_write_signal), this gives
# instant visual feedback via window name + fast status-right update.
_ccm_instant_permit_icon() {
    local win_target="$1" project="$2"
    local win_idx="${win_target##*:}"

    # Set flag with project info for inject-status to consume
    tmux set -g @ccm-permit-pending "${win_idx}:${project}" 2>/dev/null

    # Force tmux to redraw status bar — this triggers #(ccm inject-status)
    # if status-interval has elapsed, giving faster pickup
    tmux refresh-client -S 2>/dev/null
}

# Send desktop notification immediately for PERMIT state.
# Writes a marker so inject-status can skip the duplicate notification.
_ccm_instant_notify() {
    local project="$1" detail="${2:-}"

    # Check notification setting
    local notify_setting
    notify_setting=$(tmux show-option -gqv @ccm-notify 2>/dev/null)
    notify_setting="${notify_setting:-permit,done}"
    [[ "$notify_setting" == "off" ]] && return

    # Check if PERMIT notifications are enabled
    case "$notify_setting" in
        all|*permit*) ;;
        *) return ;;
    esac

    # Write marker to prevent inject-status from sending duplicate notification
    local tmp_dir="${TMPDIR:-/tmp}/ccm-${UID}"
    printf '%s' "$(date +%s)" > "${tmp_dir}/permit-notified" 2>/dev/null

    # Sound setting
    local sound_setting
    sound_setting=$(tmux show-option -gqv @ccm-notify-sound 2>/dev/null)
    sound_setting="${sound_setting:-off}"

    local title="ccm ⚠ ${project}"
    local body
    if [[ -n "$detail" ]]; then
        body="Permission required: ${detail}"
    else
        body="Action required — respond to the permission prompt"
    fi

    # Escape for AppleScript
    title="${title//\\/\\\\}" ; title="${title//\"/\\\"}"
    body="${body//\\/\\\\}" ; body="${body//\"/\\\"}"

    if command -v osascript &>/dev/null; then
        local sound_opt=""
        if [[ "$sound_setting" == "on" ]]; then
            sound_opt=' sound name "Glass"'
        fi
        osascript -e "display notification \"${body}\" with title \"${title}\"${sound_opt}" 2>/dev/null &
    elif command -v notify-send &>/dev/null; then
        notify-send "$title" "$body" 2>/dev/null &
    fi
}
