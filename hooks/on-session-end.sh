#!/usr/bin/env bash
# ccm hook: Claude Code SessionEnd → write SHELL signal
# Fires when Claude Code session ends (user types /exit, Ctrl+D, etc.)
# Installed by: ccm setup-hooks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

ccm_write_signal "$HOOK_DIR" "$KEY" "SHELL" "$CWD"
