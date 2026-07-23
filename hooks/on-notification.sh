#!/usr/bin/env bash
# ccm hook: Claude Code Notification → write PERMIT signal / clear signal
# Matches: permission_prompt → PERMIT, idle_prompt → clear signal
# Installed by: ccm setup-hooks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

NOTIFY_TYPE=$(printf '%s' "$INPUT" | jq -r '.notification_type // empty' 2>/dev/null) || \
NOTIFY_TYPE=$(printf '%s' "$INPUT" | grep -o '"notification_type" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
[[ -z "$NOTIFY_TYPE" ]] && exit 0

case "$NOTIFY_TYPE" in
    permission_prompt)
        ccm_append_event "$HOOK_DIR" "$KEY" "notify_permit"
        ccm_write_signal "$HOOK_DIR" "$KEY" "PERMIT" "$CWD"
        ;;
    elicitation_dialog)
        # MCP servers can request user input via elicitation
        # dialogs. Functionally identical to a permission prompt —
        # Claude is paused waiting for the user.
        ccm_append_event "$HOOK_DIR" "$KEY" "notify_permit"
        ccm_write_signal "$HOOK_DIR" "$KEY" "PERMIT" "$CWD"
        ;;
    idle_prompt)
        ccm_append_event "$HOOK_DIR" "$KEY" "notify_idle"
        # Delete the signal file to clear the BUSY state.
        #
        # Claude Code delivers idle_prompt 10-60s+ late
        # (anthropics/claude-code#5186 — see the COMPLETED-notification
        # comment below), so the BUSY signal currently on disk may have
        # been written by a PreToolUse / UserPromptSubmit that fired
        # AFTER Claude generated this notification. Deleting such a
        # fresh BUSY signal would drop an actively working session to
        # IDLE — and a false IDLE feeds auto-exit's kill path. Guard:
        # skip deletion while the BUSY signal is younger than the
        # maximum documented idle_prompt delay
        # (CCM_IDLE_PROMPT_GUARD_SEC, default 60 — consistent with the
        # delay budget this same hook cites for notifications and with
        # the grace-period style used by CCM_COMPLETION_GRACE_SEC in
        # lib.sh). Signals older than that predate any plausible
        # idle_prompt lag and are cleared as before.
        SIGNAL_FILE="${HOOK_DIR}/${KEY}"
        NOW=$(date +%s)
        GUARD_SEC="${CCM_IDLE_PROMPT_GUARD_SEC:-60}"
        if [[ -f "$SIGNAL_FILE" ]]; then
            EXISTING=$(cat "$SIGNAL_FILE" 2>/dev/null)
            # Signal format: "<ts> <state>" or "<ts> <state> <detail>".
            # The state is the SECOND field — strip the leading ts,
            # then take everything up to the next space. A last-field
            # extraction (`##* `) would return the detail's last word
            # instead of the state whenever a detail is present.
            # (BUSY writers don't pass details today, but the parse
            # must not silently break the day one does.)
            EXISTING_REST="${EXISTING#* }"
            EXISTING_STATE="${EXISTING_REST%% *}"
            EXISTING_TS="${EXISTING%% *}"
            # Validate the stored ts is numeric before doing arithmetic
            # on it — a corrupt/truncated signal would otherwise
            # evaluate as 0 in (( )), making the age look huge and
            # silently disabling the guard (same failure mode the
            # notify-marker dedup in lib.sh guards against).
            if [[ "$EXISTING_STATE" == "BUSY" && "$EXISTING_TS" =~ ^[0-9]+$ ]] \
                && (( NOW - EXISTING_TS < GUARD_SEC )); then
                exit 0
            fi
        fi
        rm -f "$SIGNAL_FILE" "$SIGNAL_FILE.busy"

        # Intentionally do NOT fire a COMPLETED desktop notification
        # here. Claude Code's idle_prompt carries a documented 10-60s+
        # delay (anthropics/claude-code#5186), so a notification on
        # this path arrives long after the response actually finished
        # and reads as a phantom "very late" alert. The authoritative
        # completion ping comes from on-stop.sh's grace-scheduled
        # notification, which fires within CCM_COMPLETION_GRACE_SEC
        # (default 3) of the real Stop event.
        ;;
esac
