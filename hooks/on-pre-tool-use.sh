#!/usr/bin/env bash
# ccm hook: Claude Code PreToolUse / SubagentStart → write BUSY signal
# Solves multi-turn gap: re-asserts BUSY when Stop fired at turn boundary.
# Installed by: ccm setup-hooks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

# Cancel any COMPLETED notification scheduled by a recent Stop at a
# turn boundary — a new tool call means Claude is still working.
_ccm_cancel_pending_completion "$HOOK_DIR" "$KEY"

# This script is shared across 7 Claude Code events. Dispatch event
# type from hook_event_name (authoritative upstream signal) so the
# event log captures the actual upstream event rather than a single
# blended "pretool" bucket.
_event_name=$(ccm_hook_event_name)
case "$_event_name" in
    PreToolUse)                       _event_type="pretool"  ;;
    PostToolUse|PostToolUseFailure)   _event_type="posttool" ;;
    SubagentStart|SubagentStop)       _event_type="subagent" ;;
    PreCompact|PostCompact)           _event_type="compact"  ;;
    *)                                _event_type="pretool"  ;;
esac
ccm_append_event "$HOOK_DIR" "$KEY" "$_event_type"

ccm_write_signal "$HOOK_DIR" "$KEY" "BUSY" "$CWD"
