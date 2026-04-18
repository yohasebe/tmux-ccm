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
        ccm_write_signal "$HOOK_DIR" "$KEY" "PERMIT" "$CWD"
        ;;
    elicitation_dialog)
        # MCP servers can request user input via elicitation dialogs
        # (Claude Code v2.1.107+). Functionally identical to a
        # permission prompt — Claude is paused waiting for the user.
        ccm_write_signal "$HOOK_DIR" "$KEY" "PERMIT" "$CWD"
        ;;
    idle_prompt)
        # Delete the signal file to clear the BUSY state.
        # Only delete if not already BUSY (avoid clearing active work signal
        # that was just written by a concurrent PreToolUse).
        SIGNAL_FILE="${HOOK_DIR}/${KEY}"
        NOW=$(date +%s)
        if [[ -f "$SIGNAL_FILE" ]]; then
            EXISTING=$(cat "$SIGNAL_FILE" 2>/dev/null)
            EXISTING_STATE="${EXISTING##* }"
            EXISTING_TS="${EXISTING%% *}"
            if [[ "$EXISTING_STATE" == "BUSY" && "$EXISTING_TS" -ge "$NOW" ]] 2>/dev/null; then
                exit 0
            fi
        fi
        rm -f "$SIGNAL_FILE" "$SIGNAL_FILE.busy"

        project=$(ccm_hook_resolve_project "$CWD")
        if [[ -n "$project" ]]; then
            _ccm_instant_notify "COMPLETED" "$project" "" "$KEY" &
        fi
        ;;
esac
