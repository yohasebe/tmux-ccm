#!/usr/bin/env bash
# Record the session a popup was opened from: inside a popup tmux does
# not report it (see ccm_core.get_session). Run by the key bindings in
# ccm.tmux before the popup opens.
_dir="${CCM_TMP_DIR:-${TMPDIR:-/tmp}/ccm-$(id -u)}"
# shellcheck source=/dev/null
source "$(dirname "$0")/ccm_tmp_root.sh" 2>/dev/null \
    && ccm_secure_tmp_root "$_dir" || exit 0
printf '%s' "$1" > "${_dir}/popup-session"
