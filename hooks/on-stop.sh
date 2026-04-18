#!/usr/bin/env bash
# ccm hook: Claude Code Stop / StopFailure → clear BUSY signal
# Deletes the signal file so detection transitions to IDLE.
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

# Delete the BUSY signal file (and .busy file) to clear the BUSY state.
# The detection layer will transition to IDLE on the next scan.
rm -f "$HOOK_DIR/$KEY" "$HOOK_DIR/$KEY.busy"

# Resolve project name for notification
project=""
win_info=$(tmux list-windows -a -F '#{session_name}:#{window_index}	#{@ccm_dir}	#{@ccm_project}' 2>/dev/null \
    | awk -F'\t' -v d="$CWD" '$2==d {print $1"\t"$3; exit}')
if [[ -n "$win_info" ]]; then
    project="${win_info##*	}"
fi

# Fire instant notification (if enabled). The key is passed so the
# dedup marker is per-project — critical when running several ccm
# projects concurrently, otherwise the first project's COMPLETED
# notification silently blocks all others for 10 seconds.
if [[ -n "$project" ]]; then
    _ccm_instant_notify "COMPLETED" "$project" "" "$KEY" &
fi
