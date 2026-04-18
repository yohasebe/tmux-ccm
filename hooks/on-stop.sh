#!/usr/bin/env bash
# ccm hook: Claude Code Stop / StopFailure → clear BUSY signal
# Deletes the signal file so detection transitions to IDLE.
# Installed by: ccm setup-hooks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

# Delete the BUSY signal file (and .busy file) to clear the BUSY state.
# The detection layer will transition to IDLE on the next scan.
rm -f "$HOOK_DIR/$KEY" "$HOOK_DIR/$KEY.busy"

# Schedule a COMPLETED notification after a short grace period so
# Stop events fired at multi-turn tool boundaries do not produce a
# premature alert. If the next PreToolUse / UserPromptSubmit arrives
# within the grace window, it cancels the pending notification. See
# `_ccm_schedule_completed_notify` in lib.sh for the rationale.
project=$(ccm_hook_resolve_project "$CWD")
if [[ -n "$project" ]]; then
    _ccm_schedule_completed_notify "$HOOK_DIR" "$KEY" "$project"
fi
