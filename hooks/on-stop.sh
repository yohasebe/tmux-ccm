#!/usr/bin/env bash
# ccm hook: Claude Code Stop / StopFailure → write DONE signal
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

SIGNAL_FILE="${HOOK_DIR}/${KEY}"
NOW=$(date +%s)

# Safety: don't overwrite a newer BUSY signal (race condition protection)
if [[ -f "$SIGNAL_FILE" ]]; then
    EXISTING=$(cat "$SIGNAL_FILE" 2>/dev/null)
    EXISTING_TS="${EXISTING%% *}"
    EXISTING_STATE="${EXISTING##* }"
    if [[ "$EXISTING_STATE" == "BUSY" && "$EXISTING_TS" -ge "$NOW" ]] 2>/dev/null; then
        exit 0
    fi
fi

ccm_write_signal "$HOOK_DIR" "$KEY" "DONE" "$CWD"
