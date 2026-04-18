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

ccm_write_signal "$HOOK_DIR" "$KEY" "BUSY" "$CWD"
