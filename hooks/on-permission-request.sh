#!/usr/bin/env bash
# ccm hook: Claude Code PermissionRequest → write PERMIT signal
# Fires BEFORE the permission dialog appears (earlier than Notification).
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

# Extract tool name for detailed notification (e.g., "Bash", "Edit", "Write")
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null) || \
TOOL_NAME=$(printf '%s' "$INPUT" | grep -o '"tool_name" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')

# Extract brief detail from tool_input (file path or command snippet)
TOOL_DETAIL=""
if [[ -n "$TOOL_NAME" ]]; then
    case "$TOOL_NAME" in
        Bash|bash)
            TOOL_DETAIL=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null | head -c 60)
            ;;
        Edit|Write|Read)
            TOOL_DETAIL=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
            # Shorten home directory
            TOOL_DETAIL="${TOOL_DETAIL/#$HOME/\~}"
            ;;
    esac
fi

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

# Build detail string: "Bash: rm -rf ..." or "Edit: ~/src/main.rs"
DETAIL=""
if [[ -n "$TOOL_NAME" ]]; then
    if [[ -n "$TOOL_DETAIL" ]]; then
        DETAIL="${TOOL_NAME}: ${TOOL_DETAIL}"
    else
        DETAIL="${TOOL_NAME}"
    fi
fi

ccm_write_signal "$HOOK_DIR" "$KEY" "PERMIT" "$CWD" "$DETAIL"
