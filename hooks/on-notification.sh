#!/usr/bin/env bash
# ccm hook: Claude Code Notification → write PERMIT/IDLE signal
# Matches: permission_prompt → PERMIT, idle_prompt → DONE
# Installed by: ccm setup-hooks
# Input: JSON on stdin with { session_id, cwd, hook_event_name, notification_type, message, ... }

set -euo pipefail

HOOK_DIR="${TMPDIR:-/tmp}/ccm-${UID}/hooks"
mkdir -p "$HOOK_DIR" 2>/dev/null || true

# Read cwd and notification_type from stdin JSON
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || \
CWD=$(printf '%s' "$INPUT" | grep -o '"cwd" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
[[ -z "$CWD" ]] && exit 0

NOTIFY_TYPE=$(printf '%s' "$INPUT" | jq -r '.notification_type // empty' 2>/dev/null) || \
NOTIFY_TYPE=$(printf '%s' "$INPUT" | grep -o '"notification_type" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
[[ -z "$NOTIFY_TYPE" ]] && exit 0

# Resolve to canonical path
if command -v realpath &>/dev/null && [[ -e "$CWD" ]]; then
    CWD=$(realpath "$CWD" 2>/dev/null) || true
fi

# MD5 hash of cwd as filename
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
        printf '%s PERMIT' "$NOW" > "${HOOK_DIR}/${KEY}"
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
        printf '%s DONE' "$NOW" > "${HOOK_DIR}/${KEY}"
        ;;
esac
