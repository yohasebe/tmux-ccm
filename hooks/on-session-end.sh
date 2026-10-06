#!/usr/bin/env bash
# ccm hook: Claude Code SessionEnd → write SHELL signal
# Fires when Claude Code session ends (user types /exit, Ctrl+D, etc.)
# Installed by: ccm setup-hooks
set -euo pipefail

_ccm_src="${BASH_SOURCE[0]}"; [[ "$_ccm_src" == */* ]] || _ccm_src="./$_ccm_src"
SCRIPT_DIR="$(cd "${_ccm_src%/*}" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

ccm_append_event "$HOOK_DIR" "$KEY" "session_end"
ccm_write_signal "$HOOK_DIR" "$KEY" "SHELL" "$CWD"
