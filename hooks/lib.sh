#!/usr/bin/env bash
# ccm hook library — shared functions for all hook scripts

# Run the boilerplate preamble common to every on-*.sh hook: sets up
# HOOK_DIR, consumes Claude Code's JSON payload from stdin into
# `INPUT`, extracts `CWD`, resolves symlinks when possible, and
# computes the md5-of-cwd `KEY`. All four variables are set in the
# caller's scope (bash function-local is opt-in via `local`, so the
# plain assignments below propagate). Returns 0 on success and 1
# when the payload lacks a cwd or no md5 implementation is available
# — hook scripts should `ccm_hook_init || exit 0` to short-circuit.
#
# Reads stdin exactly once. If a script needs additional fields from
# the payload, parse them from "$INPUT" after calling ccm_hook_init.
ccm_hook_init() {
    HOOK_DIR="${TMPDIR:-/tmp}/ccm-${UID}/hooks"
    mkdir -p "$HOOK_DIR" 2>/dev/null || true

    INPUT=$(cat)
    CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || \
        CWD=$(printf '%s' "$INPUT" | grep -o '"cwd" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
    [[ -z "$CWD" ]] && return 1

    if command -v realpath &>/dev/null && [[ -e "$CWD" ]]; then
        CWD=$(realpath "$CWD" 2>/dev/null) || true
    fi

    if command -v md5 &>/dev/null; then
        KEY=$(printf '%s' "$CWD" | md5)
    elif command -v md5sum &>/dev/null; then
        KEY=$(printf '%s' "$CWD" | md5sum | cut -d' ' -f1)
    else
        return 1
    fi
    return 0
}

# Build a human-readable tool detail string from Claude Code's hook
# payload (the Permission* hooks use this to enrich their desktop
# notification, e.g. "Bash: rm -rf ..." or "Edit: ~/src/main.rs").
# Expects `INPUT` in scope (populated by ccm_hook_init). Writes the
# formatted string to stdout; empty output means "no tool info".
# Args: $1=OPTIONAL prefix (e.g. "Denied ") prepended to the result.
ccm_hook_format_tool_detail() {
    local prefix="${1:-}"
    local tool_name tool_detail detail=""

    tool_name=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null) || \
        tool_name=$(printf '%s' "$INPUT" | grep -o '"tool_name" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')

    if [[ -n "$tool_name" ]]; then
        case "$tool_name" in
            Bash|bash)
                tool_detail=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null | head -c 60)
                ;;
            Edit|Write|Read)
                tool_detail=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
                tool_detail="${tool_detail/#$HOME/\~}"
                ;;
        esac
        if [[ -n "$tool_detail" ]]; then
            detail="${tool_name}: ${tool_detail}"
        else
            detail="${tool_name}"
        fi
    fi

    if [[ -n "$prefix" && -n "$detail" ]]; then
        printf '%s%s' "$prefix" "$detail"
    elif [[ -n "$prefix" ]]; then
        # Drop trailing space when there is no tool info
        printf '%s' "${prefix% }"
    else
        printf '%s' "$detail"
    fi
}

# Look up the ccm project name for a given cwd by scanning tmux
# windows with `@ccm_dir` set. Writes the project name to stdout
# (empty if no matching window). on-stop.sh and on-notification.sh
# share this so they can feed a meaningful name into the desktop
# notification ("ccm ✔ my-project" rather than "ccm ✔ ").
# Args: $1=CWD to match against `@ccm_dir`.
ccm_hook_resolve_project() {
    local cwd="$1"
    tmux list-windows -a -F '#{session_name}:#{window_index}	#{@ccm_dir}	#{@ccm_project}' 2>/dev/null \
        | awk -F'\t' -v d="$cwd" '$2==d {print $3; exit}'
}

# Write signal to hook file AND directly update tmux window option
# for instant status bar reflection (no polling delay).
# Args: $1=HOOK_DIR, $2=KEY (md5), $3=STATE (BUSY/PERMIT/SHELL), $4=CWD, $5=DETAIL (optional)
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
                PERMIT) icon="⚠" ;; BUSY) icon="◉" ;;
                IDLE) icon="●" ;; SHELL) icon="■" ;; *) icon="●" ;;
            esac
            tmux rename-window -t "$win_target" "${icon} ${project}" 2>/dev/null
        fi

        # Instant desktop notification for PERMIT
        # (eliminates up to 3s polling delay for critical states)
        if [[ "$state" == "PERMIT" && -n "$project" ]]; then
            _ccm_instant_permit_icon "$win_target" "$project" &
            _ccm_instant_notify "PERMIT" "$project" "$detail" "$key" &
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

# Schedule a COMPLETED notification after a short grace period.
# Addresses the multi-turn Stop hook pattern: Claude Code fires Stop
# at every turn boundary (including after a tool call, not just at the
# true end of a response). A naive "notify on Stop" design sends an
# alert at the FIRST turn boundary and then silently dedups all
# subsequent completions, including the real one — users see a
# premature notification during active work and no alert when Claude
# actually finishes.
#
# Fix: write a `<key>.pending` sentinel and schedule the notification
# asynchronously. If PreToolUse or UserPromptSubmit clears the
# sentinel within `grace_sec`, we treat the Stop as a turn boundary
# and cancel. If the sentinel survives, the Stop was genuine and the
# notification fires. idle_prompt (on-notification.sh) still calls
# `_ccm_instant_notify` directly — for a real completion its
# notification arrives before this delayed check, and per-project
# dedup suppresses the follow-up.
#
# Args: $1=HOOK_DIR, $2=KEY, $3=PROJECT_NAME, $4=GRACE_SEC
#       (default: CCM_COMPLETION_GRACE_SEC env, or 3)
_ccm_schedule_completed_notify() {
    local hook_dir="$1" key="$2" project="$3"
    local grace="${4:-${CCM_COMPLETION_GRACE_SEC:-3}}"
    local pending="${hook_dir}/${key}.pending"

    printf '%s' "$(date +%s)" > "$pending" 2>/dev/null

    # Detach into its own process group so the sleep survives hook
    # exit (Claude Code gives hooks a few-second timeout; the bg
    # subshell must outlive that).
    (
        sleep "$grace"
        if [[ -f "$pending" ]]; then
            rm -f "$pending" 2>/dev/null
            _ccm_instant_notify "COMPLETED" "$project" "" "$key"
        fi
    ) </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
}

# Cancel any pending COMPLETED notification for a project — called
# from hooks that indicate ongoing work (PreToolUse, UserPromptSubmit,
# SubagentStart, PreCompact, etc.). If no pending sentinel exists,
# this is a no-op.
# Args: $1=HOOK_DIR, $2=KEY
_ccm_cancel_pending_completion() {
    local hook_dir="$1" key="$2"
    rm -f "${hook_dir}/${key}.pending" 2>/dev/null || true
}

# Send desktop notification immediately for PERMIT or COMPLETED state.
# Writes a per-project marker so inject-status can skip the duplicate
# notification for the same project. The marker is keyed off the
# project's hook-signal `key` (md5 of cwd) so concurrent ccm projects
# do not dedup each other — a global marker would cause project B's
# COMPLETED to be silently dropped whenever project A completed within
# 10 seconds, which is the common case when running several projects
# in parallel.
# Args: $1=STATE (PERMIT/COMPLETED), $2=PROJECT_NAME, $3=DETAIL, $4=KEY
_ccm_instant_notify() {
    local state="$1" project="$2" detail="${3:-}" key="${4:-}"

    local tmp_dir="${TMPDIR:-/tmp}/ccm-${UID}"
    local marker_dir="${tmp_dir}/notified"
    mkdir -p "$marker_dir" 2>/dev/null || true
    # Per-project marker (keyed on md5-of-cwd). Fall back to the legacy
    # global path if no key was supplied so older hook scripts continue
    # to function during an in-place upgrade.
    local marker
    if [[ -n "$key" ]]; then
        marker="${marker_dir}/${key}"
    else
        marker="${tmp_dir}/hook-notified"
    fi

    # Dedup: skip if the same state was already notified within 10 seconds
    # FOR THIS PROJECT. Stop and Notification(idle_prompt) both fire
    # COMPLETED a few seconds apart for the same completion event; this
    # check avoids the duplicate. Cross-project dedup is intentionally
    # NOT done here — separate projects must be able to fire their own
    # notifications independently.
    if [[ -f "$marker" ]]; then
        local content prev_ts prev_state now_ts
        content=$(cat "$marker" 2>/dev/null) || true
        prev_ts="${content%% *}"
        prev_state="${content##* }"
        now_ts=$(date +%s)
        if [[ "$prev_state" == "$state" ]] && (( now_ts - prev_ts < 10 )) 2>/dev/null; then
            return 0
        fi
    fi

    # Write marker BEFORE sending so a concurrent invocation sees it.
    printf '%s %s' "$(date +%s)" "$state" > "${marker}" 2>/dev/null

    # Check notification setting
    local notify_setting
    notify_setting=$(tmux show-option -gqv @ccm-notify 2>/dev/null)
    notify_setting="${notify_setting:-permit,completed}"
    [[ "$notify_setting" == "off" ]] && return

    # Check if this state's notifications are enabled
    local state_lower="${state,,}"  # PERMIT→permit, COMPLETED→completed
    case "$notify_setting" in
        all) ;;
        *"$state_lower"*) ;;
        # Backwards compat: "done" in setting also matches "completed"
        *"done"*) [[ "$state_lower" == "completed" ]] || return ;;
        *) return ;;
    esac

    # (Marker already written above for both hook-vs-hook and hook-vs-inject dedup.)

    # Sound setting
    local sound_setting
    sound_setting=$(tmux show-option -gqv @ccm-notify-sound 2>/dev/null)
    sound_setting="${sound_setting:-off}"

    local icon title body
    case "$state" in
        PERMIT)
            icon="⚠"
            if [[ -n "$detail" ]]; then
                body="Permission required: ${detail}"
            else
                body="Action required — respond to the permission prompt"
            fi
            ;;
        COMPLETED)
            icon="✔"
            body="Response complete"
            ;;
        *)
            icon="●"
            body="State changed to ${state}"
            ;;
    esac
    title="ccm ${icon} ${project}"

    # Escape for AppleScript
    title="${title//\\/\\\\}" ; title="${title//\"/\\\"}"
    body="${body//\\/\\\\}" ; body="${body//\"/\\\"}"

    if command -v osascript &>/dev/null; then
        local sound_opt=""
        if [[ "$sound_setting" == "on" ]]; then
            local sound_name
            sound_name=$(tmux show-option -gqv @ccm-notify-sound-name 2>/dev/null)
            sound_name="${sound_name:-Glass}"
            sound_opt=" sound name \"${sound_name}\""
        fi
        osascript -e "display notification \"${body}\" with title \"${title}\"${sound_opt}" 2>/dev/null &
    elif command -v notify-send &>/dev/null; then
        notify-send "$title" "$body" 2>/dev/null &
    fi
}
