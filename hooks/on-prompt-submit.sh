#!/usr/bin/env bash
# ccm hook: Claude Code UserPromptSubmit → write BUSY signal
# Installed by: ccm setup-hooks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

# Cancel any COMPLETED notification scheduled by a recent Stop —
# the user is starting a new turn, not waiting on a finished response.
_ccm_cancel_pending_completion "$HOOK_DIR" "$KEY"

ccm_append_event "$HOOK_DIR" "$KEY" "prompt"
ccm_write_signal "$HOOK_DIR" "$KEY" "BUSY" "$CWD"
