#!/usr/bin/env bash
# ccm hook: Claude Code Stop → write DONE signal
# Installed by: ccm setup-hooks
# Input: JSON on stdin with { session_id, cwd, hook_event_name, ... }

set -euo pipefail

HOOK_DIR="${TMPDIR:-/tmp}/ccm-${UID}/hooks"
mkdir -p "$HOOK_DIR" 2>/dev/null || true

# Read cwd from stdin JSON (jq with fallback)
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || \
CWD=$(printf '%s' "$INPUT" | grep -o '"cwd" *: *"[^"]*"' | head -1 | sed 's/.*: *"//;s/"$//')
[[ -z "$CWD" ]] && exit 0

# Resolve to canonical path (match ccm's ccm_expand_path behavior)
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

SIGNAL_FILE="${HOOK_DIR}/${KEY}"
NOW=$(date +%s)

# Split-pane safety: if another pane wrote a newer BUSY signal, don't overwrite.
# This prevents one pane's Stop from clearing another pane's active BUSY state.
if [[ -f "$SIGNAL_FILE" ]]; then
    EXISTING=$(cat "$SIGNAL_FILE" 2>/dev/null)
    EXISTING_TS="${EXISTING%% *}"
    EXISTING_STATE="${EXISTING##* }"
    if [[ "$EXISTING_STATE" == "BUSY" && "$EXISTING_TS" -gt "$NOW" ]] 2>/dev/null; then
        exit 0
    fi
fi

printf '%s DONE' "$NOW" > "$SIGNAL_FILE"
