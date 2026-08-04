#!/usr/bin/env bash
# ccm hook library — shared functions for all hook scripts

# State icon table is shared with the Python detector via
# state_meta.sh. Both notification-path call sites
# (`ccm_write_signal`, `_ccm_instant_notify`) source it from here.
# `BASH_SOURCE` resolves to hooks/lib.sh, so its sibling lib/
# directory holds state_meta.sh via ../lib.
_CCM_HOOK_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/state_meta.sh
source "${_CCM_HOOK_LIB_DIR}/../lib/state_meta.sh"

# Run the boilerplate preamble common to every on-*.sh hook: sets up
# HOOK_DIR, consumes Claude Code's JSON payload from stdin into
# `INPUT`, extracts `CWD` (still useful for project-name lookup) and
# `SESSION_ID`, and uses session_id as the file `KEY`. All variables
# are set in the caller's scope. Returns 0 on success and 1 when the
# payload lacks a session id — hook scripts should
# `ccm_hook_init || exit 0` to short-circuit.
#
# session_id is the primary key for hook artefacts:
#   - stable for the lifetime of a Claude Code session (UUID per
#     session, written by the runtime)
#   - distinct across sessions, so a fresh `claude --continue` cannot
#     read state left by a prior session in the same cwd
#   - unaffected by `cd` mid-session
#
# Reads stdin exactly once. If a script needs additional fields from
# the payload, parse them from "$INPUT" after calling ccm_hook_init.
# Stamp the two ignore markers on the current pane so ccm skips it:
#   1. the `@ccm_ignore` tmux pane option — read by the Python
#      detection layer straight out of its bulk `list-panes` query, so
#      the pane is dropped from state aggregation, session tracking,
#      `ccm send` delivery, and idle auto-exit;
#   2. a pane title (visible only when the user runs
#      `pane-border-status`, opt-in via `@ccm-ignore-pane-border on`).
# `$TMUX_PANE` is set by tmux in every pane's shell and inherited all
# the way down to the hook subprocess, so it is the reliable pane id
# here. No-op outside tmux. The per-session marker file (hook
# suppression) is written by the caller, which knows the session id.
_ccm_mark_ignored_pane() {
    [[ -n "${TMUX_PANE:-}" ]] || return 0
    tmux set-option -p -t "$TMUX_PANE" @ccm_ignore 1 2>/dev/null || true
    tmux select-pane -t "$TMUX_PANE" -T "⊘ ccm-ignored" 2>/dev/null || true
    # Opt-in: reveal the pane title by turning on tmux's pane border.
    # This IS a global layout change, so it happens only when the user
    # explicitly asked for it — otherwise ccm never touches their
    # tmux chrome (dashboard marker remains the cross-user cue).
    if [[ "$(tmux show-option -gqv @ccm-ignore-pane-border 2>/dev/null)" == "on" ]]; then
        tmux set-option -g pane-border-status top 2>/dev/null || true
    fi
    return 0
}

ccm_hook_init() {
    HOOK_DIR="${TMPDIR:-/tmp}/ccm-${UID}/hooks"
    mkdir -p "$HOOK_DIR" 2>/dev/null || true

    INPUT=$(cat)

    # CCM_IGNORE: launch-time opt-out (`CCM_IGNORE=1 claude`). Mark the
    # pane immediately — this only needs $TMUX_PANE, so it works even
    # if the session id cannot be read below — then suppress the hook.
    if [[ -n "${CCM_IGNORE:-}" ]]; then
        _ccm_mark_ignored_pane
    fi

    # Foreign-harness gate. Grok Build reads `~/.claude/settings.json`
    # hooks BY DEFAULT for Claude Code compatibility (grok-build docs,
    # user-guide/10-hooks.md), so these scripts can be invoked by a
    # non-Claude agent with a payload that parses well enough to slip
    # through — camelCase `sessionId` is accepted below on purpose.
    # Left unguarded, a Grok sidekick would write BUSY signals under
    # its own session id, stamp `@ccm_prev_state` through the fast-path
    # spawn, and fire COMPLETED notifications for turns no Claude ran.
    # `workspaceRoot` is the discriminator: Grok sends it on every
    # event, Claude Code sends it on none. (ringi adopted the same
    # test for the same exposure, their commit 9813d68.)
    if printf '%s' "$INPUT" | jq -e 'has("workspaceRoot")' >/dev/null 2>&1; then
        return 1
    fi

    # session_id: the primary KEY. Try snake_case then camelCase —
    # upstream payload schema uses both depending on the field.
    SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionId // empty' 2>/dev/null) || \
        SESSION_ID=$(printf '%s' "$INPUT" | grep -oE '"sessionI?d?_?i?d?" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
    [[ -z "$SESSION_ID" ]] && return 1
    KEY="$SESSION_ID"

    # Ignore gate. Two entry points converge on the same suppression:
    #   - launch-time `CCM_IGNORE` (drops the session marker now so
    #     later fires take the same path even if the env var is gone);
    #   - runtime `ccm ignore`, which wrote `$HOOK_DIR/<sid>.ignore`.
    # An ignored session writes no signal/event artefacts and fires no
    # desktop notifications — it is invisible to ccm.
    if [[ -n "${CCM_IGNORE:-}" ]]; then
        : > "${HOOK_DIR}/${KEY}.ignore" 2>/dev/null || true
        return 1
    fi
    [[ -f "${HOOK_DIR}/${KEY}.ignore" ]] && return 1

    # cwd is used by `_ccm_instant_notify` to find the matching tmux
    # window for project-name lookup, and by the project-name cache
    # file. Best-effort extraction; a missing cwd is tolerable
    # (instant notification falls back to "ccm" as group name).
    CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || \
        CWD=$(printf '%s' "$INPUT" | grep -o '"cwd" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
    if [[ -n "$CWD" ]] && command -v realpath &>/dev/null && [[ -e "$CWD" ]]; then
        CWD=$(realpath "$CWD" 2>/dev/null) || true
    fi

    # permission_mode: optional common field on every hook payload.
    # `ccm_append_event` copies it onto the event record so the
    # dashboard / `ccm status` can surface each project's permission
    # mode. Best-effort: an absent field leaves it empty and the event
    # record omits it. Unlike the hard-coded event types this value
    # crosses from the upstream payload into our JSONL, so it is
    # sanitized to a conservative charset and length-capped — a mode
    # name will never need escaping downstream.
    PERMISSION_MODE=$(printf '%s' "$INPUT" | jq -r '.permission_mode // empty' 2>/dev/null) || \
        PERMISSION_MODE=$(printf '%s' "$INPUT" | grep -o '"permission_mode" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
    PERMISSION_MODE="${PERMISSION_MODE//[^A-Za-z0-9_-]/}"
    PERMISSION_MODE="${PERMISSION_MODE:0:32}"

    return 0
}

# Extract hook_event_name from Claude Code's hook payload.
# Expects `INPUT` in scope (populated by ccm_hook_init). Writes the
# event name to stdout (e.g., "PreToolUse", "Stop"); empty on failure.
# Used by on-pre-tool-use.sh (shared across 7 Claude Code events) and
# the event-log writer to dispatch on the authoritative upstream name
# rather than guessing from hook script identity.
ccm_hook_event_name() {
    local name
    name=$(printf '%s' "$INPUT" | jq -r '.hook_event_name // empty' 2>/dev/null) || \
        name=$(printf '%s' "$INPUT" | grep -o '"hook_event_name" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
    printf '%s' "$name"
}

# Append a single event record to the per-project event log.
# This is the authoritative state timeline consumed by the Python
# detection layer in Phase 2+ of the event-log redesign. Phase 1
# (current) writes events in parallel to the existing signal-file
# mechanism; detection logic is unchanged until Phase 2 opts in.
#
# Format: JSONL, append-only. One hook invocation = one line.
# Path: $HOOK_DIR/<session_id>.events.jsonl
# Schema: {"ts": <unix_seconds>, "type": "<normalized>"[, "mode": "<permission_mode>"]}
#
# `type` is restricted to the 9-type normalized vocabulary:
#   prompt, pretool, posttool, subagent, compact, stop,
#   permit_req, notify_permit, notify_idle, session_end
# (call sites use hard-coded values, so no escaping needed.)
#
# `mode` rides along when the payload carried `permission_mode`
# (extracted and sanitized by ccm_hook_init in the caller's scope).
# Fields other than "type" are opaque to the detection layer —
# `derive_state_from_events` keys on "type" only; display readers use
# `ccm_signals.read_latest_permission_mode` to surface the mode badge.
#
# Atomicity: POSIX O_APPEND makes writes <PIPE_BUF (4KB) atomic
# without flock. Errors are suppressed: a write failure must not
# prevent the legacy signal-file path from running.
#
# Args: $1=HOOK_DIR, $2=KEY (Claude Code session_id), $3=TYPE (normalized event type)
ccm_append_event() {
    local hook_dir="$1" key="$2" type="$3"
    local ts events_file mode
    [[ -z "$hook_dir" || -z "$key" || -z "$type" ]] && return 0
    ts=$(date +%s)
    events_file="${hook_dir}/${key}.events.jsonl"
    mode="${PERMISSION_MODE:-}"
    if [[ -n "$mode" ]]; then
        printf '{"ts":%s,"type":"%s","mode":"%s"}\n' "$ts" "$type" "$mode" >> "$events_file" 2>/dev/null || true
    else
        printf '{"ts":%s,"type":"%s"}\n' "$ts" "$type" >> "$events_file" 2>/dev/null || true
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
# Args: $1=HOOK_DIR, $2=KEY (session_id), $3=STATE (BUSY/PERMIT/SHELL), $4=CWD, $5=DETAIL (optional)
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
    # Find the window whose @ccm_dir matches this cwd. The listing
    # also carries @ccm_prev_state so the push below can fire only on
    # a real transition — same subprocess, no extra cost.
    local win_target project prev_state
    local win_info
    win_info=$(tmux list-windows -a -F '#{session_name}:#{window_index}	#{@ccm_dir}	#{@ccm_project}	#{@ccm_prev_state}' 2>/dev/null \
        | awk -F'\t' -v d="$cwd" '$2==d {print $1"\t"$3"\t"$4; exit}')
    if [[ -n "$win_info" ]]; then
        IFS=$'\t' read -r win_target project prev_state <<< "$win_info"
        # Update state option
        tmux set-option -wt "$win_target" @ccm_prev_state "$state" 2>/dev/null
        # Update window name icon for instant status bar change
        if [[ -n "$project" ]]; then
            local icon
            icon=$(ccm_state_icon "$state")
            tmux rename-window -t "$win_target" "${icon} ${project}" 2>/dev/null
        fi

        # Instant desktop notification for PERMIT
        # (eliminates up to 3s polling delay for critical states)
        if [[ "$state" == "PERMIT" && -n "$project" ]]; then
            _ccm_instant_permit_icon "$win_target" "$project" &
            _ccm_instant_notify "PERMIT" "$project" "$detail" "$cwd" &
        else
            # Non-PERMIT transitions also benefit from forcing an
            # immediate status redraw so BUSY ↔ IDLE flips appear in
            # ~100 ms instead of waiting up to status-interval (1 s).
            # The PERMIT branch above already calls refresh-client via
            # `_ccm_instant_permit_icon`, so this `else` covers BUSY
            # signal writes (PreToolUse / PostToolUse / etc.) without
            # double-refreshing.
            tmux refresh-client -S 2>/dev/null
        fi

        # Push the freshly written state into the rendered status bar
        # NOW. The rename-window above updates window NAMES instantly,
        # but mode 0's status-right icon and mode 2's dedicated
        # line(s) are literal text baked by inject-status — a plain
        # refresh-client redraws the STALE bake, so those surfaces
        # otherwise lag one status-interval (~1 s) behind the hook.
        # `inject-status --fast` re-renders from @ccm_prev_state
        # (which this function just wrote) without running detection:
        # read-only, lock-tolerant, ~100 ms — and backgrounded so
        # hook latency is unaffected. Gated on a real state
        # TRANSITION so BUSY→BUSY bursts (PreToolUse/PostToolUse
        # pairs during rapid tool sequences) spawn nothing.
        # CCM_BIN exists for tests to stub the spawn target.
        if [[ "$prev_state" != "$state" ]]; then
            ( "${CCM_BIN:-$_CCM_HOOK_LIB_DIR/../ccm}" inject-status --fast \
                </dev/null >/dev/null 2>&1 & ) 2>/dev/null
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
# Args: $1=HOOK_DIR, $2=KEY (session_id, for the .pending sentinel),
#       $3=CWD (for notification dedup keying), $4=PROJECT_NAME,
#       $5=GRACE_SEC (default: CCM_COMPLETION_GRACE_SEC env, or 3)
_ccm_schedule_completed_notify() {
    local hook_dir="$1" key="$2" cwd="$3" project="$4"
    local grace="${5:-${CCM_COMPLETION_GRACE_SEC:-3}}"
    local pending="${hook_dir}/${key}.pending"

    printf '%s' "$(date +%s)" > "$pending" 2>/dev/null

    # Detach into its own process group so the sleep survives hook
    # exit (Claude Code gives hooks a few-second timeout; the bg
    # subshell must outlive that).
    (
        sleep "$grace"
        if [[ -f "$pending" ]]; then
            rm -f "$pending" 2>/dev/null
            _ccm_instant_notify "COMPLETED" "$project" "" "$cwd"
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
# notification for the same project. The marker is keyed on md5(cwd)
# so a Claude restart in the same directory still hits the dedup
# window (session_id keying would treat the restarted session as
# brand new and miss legitimate duplicates). Per-project scoping
# prevents project A's notification from suppressing project B's.
# Must stay in sync with `read_project_notify_marker` in ccm_signals.py.
# Args: $1=STATE (PERMIT/COMPLETED), $2=PROJECT_NAME, $3=DETAIL, $4=CWD
_ccm_instant_notify() {
    local state="$1" project="$2" detail="${3:-}" cwd="${4:-}"

    local tmp_dir="${TMPDIR:-/tmp}/ccm-${UID}"
    local marker_dir="${tmp_dir}/notified"
    mkdir -p "$marker_dir" 2>/dev/null || true
    # Per-project marker keyed on md5(cwd). Fall back to the legacy
    # global path if no cwd was supplied so older hook scripts continue
    # to function during an in-place upgrade.
    local marker
    if [[ -n "$cwd" ]]; then
        local cwd_hash
        cwd_hash=$(printf '%s' "$cwd" | md5 -q 2>/dev/null) || \
            cwd_hash=$(printf '%s' "$cwd" | md5sum 2>/dev/null | cut -d' ' -f1)
        marker="${marker_dir}/${cwd_hash}"
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
        # Validate the stored ts is numeric before doing arithmetic on
        # it — a corrupt/truncated marker would otherwise evaluate as 0
        # in (( )), making the age look huge and silently disabling the
        # dedup (duplicate notifications) instead of failing visibly.
        if [[ "$prev_ts" =~ ^[0-9]+$ ]] && [[ "$prev_state" == "$state" ]] \
            && (( now_ts - prev_ts < 10 )) 2>/dev/null; then
            return 0
        fi
    fi

    # Write marker BEFORE sending so a concurrent invocation sees it.
    printf '%s %s' "$(date +%s)" "$state" > "${marker}" 2>/dev/null

    # Check notification setting
    local notify_setting
    notify_setting=$(tmux show-option -gqv @ccm-notify 2>/dev/null)
    notify_setting="${notify_setting:-permit,completed}"
    # Explicit `return 0` on every skip path. A bare `return` would
    # inherit the preceding command's exit status (e.g. a failed `[[ ]]`
    # test), making "we decided not to notify" indistinguishable from
    # an error at the call site.
    [[ "$notify_setting" == "off" ]] && return 0

    # Check if this state's notifications are enabled
    # bash 3.2 (stock macOS /bin/bash) has no `${var,,}` — use tr so
    # the hook does not die with "Bad substitution" on stock installs.
    # PERMIT→permit, COMPLETED→completed
    local state_lower
    state_lower=$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')
    case "$notify_setting" in
        all) ;;
        *"$state_lower"*) ;;
        *) return 0 ;;
    esac

    # (Marker already written above for both hook-vs-hook and hook-vs-inject dedup.)

    # Sound setting
    local sound_setting
    sound_setting=$(tmux show-option -gqv @ccm-notify-sound 2>/dev/null)
    sound_setting="${sound_setting:-off}"

    # Icon comes from the state-meta table; body text is notification-
    # specific (not part of state metadata) so it stays inline.
    local icon title body
    icon=$(ccm_state_icon "$state")
    case "$state" in
        PERMIT)
            if [[ -n "$detail" ]]; then
                body="Permission required: ${detail}"
            else
                body="Action required — respond to the permission prompt"
            fi
            ;;
        COMPLETED)
            body="Response complete"
            ;;
        *)
            body="State changed to ${state}"
            ;;
    esac
    title="ccm ${icon} ${project}"

    # Escape for AppleScript
    title="${title//\\/\\\\}" ; title="${title//\"/\\\"}"
    body="${body//\\/\\\\}" ; body="${body//\"/\\\"}"

    # Prefer terminal-notifier when available — `-group` makes
    # repeat notifications for the same project REPLACE rather
    # than accumulate in macOS Notification Center, which prevents
    # the WindowServer / NotificationCenter CPU drain that long-
    # running ccm sessions can otherwise produce.
    #
    # Notably we do NOT pass `-sender com.apple.Terminal`. Earlier
    # versions did, intending to render the notification with the
    # Terminal.app icon, but that meant the notification was
    # delivered under Terminal.app's bundle identity — and macOS
    # silently drops it for every user not running Terminal.app
    # (iTerm2, WezTerm, kitty, ghostty, ...). Without the flag the
    # notification flows under terminal-notifier's own bundle id,
    # which the user authorises once and is independent of which
    # terminal emulator they actually use.
    if command -v terminal-notifier &>/dev/null; then
        local tn_args=(-message "$body" -title "$title"
                       -group "ccm-${project}")
        if [[ "$sound_setting" == "on" ]]; then
            local sound_name
            sound_name=$(tmux show-option -gqv @ccm-notify-sound-name 2>/dev/null)
            tn_args+=(-sound "${sound_name:-Glass}")
        fi
        terminal-notifier "${tn_args[@]}" >/dev/null 2>&1 &
    elif command -v osascript &>/dev/null; then
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
