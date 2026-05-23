#!/usr/bin/env bash
# ccm hook: Claude Code Stop / StopFailure → clear BUSY signal
# Deletes the signal file so detection transitions to IDLE.
# Installed by: ccm setup-hooks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

ccm_append_event "$HOOK_DIR" "$KEY" "stop"

# Delete the BUSY signal file (and .busy file) to clear the BUSY state.
# The detection layer will transition to IDLE on the next scan.
rm -f "$HOOK_DIR/$KEY" "$HOOK_DIR/$KEY.busy"

# Schedule a COMPLETED notification after a short grace period so
# Stop events fired at multi-turn tool boundaries do not produce a
# premature alert. If the next PreToolUse / UserPromptSubmit arrives
# within the grace window, it cancels the pending notification. See
# `_ccm_schedule_completed_notify` in lib.sh for the rationale.
#
# Suppress entirely when Claude Code reports outstanding background
# work in the payload. The Stop hook fires at the end of every turn
# — but a non-empty `background_tasks` (e.g. `/bg` dispatch, an
# async tool still running) or `session_crons` (e.g. a `/loop`
# scheduled to wake later) means the user's intent is NOT yet
# "done". Without this guard, every iteration of a `/goal` or
# `/loop` would surface a COMPLETED desktop alert and train the
# user to ignore the signal. Schema: Claude Code 2.1.145+ emits
# these as JSON arrays in the Stop / SubagentStop payload; on
# older Claude Code versions the fields are absent and `// []`
# defaults the length to 0, preserving the legacy behaviour.
bg_remaining=$(printf '%s' "$INPUT" | \
    jq -r '((.background_tasks // []) | length) + ((.session_crons // []) | length)' \
    2>/dev/null) || bg_remaining=0
bg_remaining=${bg_remaining:-0}

if [[ "$bg_remaining" == "0" ]]; then
    project=$(ccm_hook_resolve_project "$CWD")
    if [[ -n "$project" ]]; then
        _ccm_schedule_completed_notify "$HOOK_DIR" "$KEY" "$CWD" "$project"
    fi
fi
