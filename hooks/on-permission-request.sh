#!/usr/bin/env bash
# ccm hook: Claude Code PermissionRequest → write PERMIT signal
# Fires BEFORE the permission dialog appears (earlier than Notification).
# Installed by: ccm setup-hooks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

DETAIL=$(ccm_hook_format_tool_detail)

ccm_append_event "$HOOK_DIR" "$KEY" "permit_req"
ccm_write_signal "$HOOK_DIR" "$KEY" "PERMIT" "$CWD" "$DETAIL"
