#!/usr/bin/env bash
# ccm state metadata (bash-side single source of truth)
#
# The Python side keeps an equivalent table in `lib/ccm_core.py`
# (STATE_ICONS). When adding a new detection state or changing an
# icon, update BOTH. Because bash hooks must not incur a Python
# start-up cost per invocation (~50ms × hook events = visible
# status-bar lag), we cannot generate this from the Python table
# at runtime — a manual cross-sync is the pragmatic trade-off.
#
# State taxonomy:
#   Detection states (written to @ccm_prev_state, used by
#   DETECTION_RULES): PERMIT / BUSY / CONT / IDLE / SHELL / DOWN
#   Display-only "recently completed" marker: COMPLETED — not a
#   detection state, only ever shown as the ✔ next to an IDLE row
#   that just transitioned from BUSY/PERMIT.
#
# CONT: continuation-of-BUSY state emitted by the event-log
# detection path (phase 2+). Claude stopped mid-turn with
# JSONL stop_reason="tool_use" and no follow-up hook has
# fired yet. Treated as BUSY-equivalent by `ccm send`.
#
# Usage:
#   source "${SCRIPT_DIR}/state_meta.sh"
#   icon=$(ccm_state_icon PERMIT)    # => ⚠
#   icon=$(ccm_state_icon UNKNOWN)   # => ● (fallback)

ccm_state_icon() {
    case "${1:-}" in
        PERMIT)    printf '⚠' ;;
        BUSY)      printf '◉' ;;
        CONT)      printf '◍' ;;
        COMPLETED) printf '✔' ;;
        IDLE)      printf '●' ;;
        SHELL)     printf '■' ;;
        DOWN)      printf '○' ;;
        *)         printf '●' ;;
    esac
}
