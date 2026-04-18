#!/usr/bin/env bash
# ccm hook: Claude Code PermissionDenied → write PERMIT signal
# Fires when auto mode classifier denies an action.
# User must check /permissions → Recent to retry with 'r'.
# Installed by: ccm setup-hooks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

# "Denied " prefix distinguishes this from interactive PERMIT prompts.
DETAIL=$(ccm_hook_format_tool_detail "Denied ")

ccm_write_signal "$HOOK_DIR" "$KEY" "PERMIT" "$CWD" "$DETAIL"
