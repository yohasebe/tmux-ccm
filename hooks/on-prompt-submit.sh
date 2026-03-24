#!/usr/bin/env bash
# ccm hook: Claude Code UserPromptSubmit → write BUSY signal
# Installed by: ccm setup-hooks
# Input: JSON on stdin with { session_id, cwd, hook_event_name, ... }

set -euo pipefail

HOOK_DIR="${TMPDIR:-/tmp}/ccm-${UID}/hooks"
mkdir -p "$HOOK_DIR" 2>/dev/null || true

# Read cwd from stdin JSON (jq with fallback to grep/sed)
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

printf '%s BUSY' "$(date +%s)" > "${HOOK_DIR}/${KEY}"
