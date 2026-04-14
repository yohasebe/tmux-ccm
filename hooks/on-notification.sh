#!/usr/bin/env bash
# ccm hook: Claude Code Notification → write PERMIT/DONE signal
# Matches: permission_prompt → PERMIT, idle_prompt → DONE
# Installed by: ccm setup-hooks
set -euo pipefail

HOOK_DIR="${TMPDIR:-/tmp}/ccm-${UID}/hooks"
mkdir -p "$HOOK_DIR" 2>/dev/null || true
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || \
CWD=$(printf '%s' "$INPUT" | grep -o '"cwd" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
[[ -z "$CWD" ]] && exit 0

NOTIFY_TYPE=$(printf '%s' "$INPUT" | jq -r '.notification_type // empty' 2>/dev/null) || \
NOTIFY_TYPE=$(printf '%s' "$INPUT" | grep -o '"notification_type" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
[[ -z "$NOTIFY_TYPE" ]] && exit 0

if command -v realpath &>/dev/null && [[ -e "$CWD" ]]; then
    CWD=$(realpath "$CWD" 2>/dev/null) || true
fi

if command -v md5 &>/dev/null; then
    KEY=$(printf '%s' "$CWD" | md5)
elif command -v md5sum &>/dev/null; then
    KEY=$(printf '%s' "$CWD" | md5sum | cut -d' ' -f1)
else
    exit 0
fi

NOW=$(date +%s)

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
        # Write DONE (may also be written by on-stop.sh — harmless, same effect)
        # Only write if not already BUSY (avoid overwriting active work signal)
        SIGNAL_FILE="${HOOK_DIR}/${KEY}"
        if [[ -f "$SIGNAL_FILE" ]]; then
            EXISTING=$(cat "$SIGNAL_FILE" 2>/dev/null)
            EXISTING_STATE="${EXISTING##* }"
            EXISTING_TS="${EXISTING%% *}"
            if [[ "$EXISTING_STATE" == "BUSY" && "$EXISTING_TS" -ge "$NOW" ]] 2>/dev/null; then
                exit 0
            fi
        fi
        ccm_write_signal "$HOOK_DIR" "$KEY" "DONE" "$CWD"
        ;;
esac
