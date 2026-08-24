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
#   DETECTION_RULES): PERMIT / BUSY / IDLE / SHELL / DOWN / IGNORED
#   (IGNORED: every Claude pane is hidden via @ccm_ignore, so the
#   window's state is unknowable — not a claim that Claude is absent)
#   Display-only "recently completed" marker: COMPLETED — not a
#   detection state, only ever shown as the ✔ next to an IDLE row
#   that just transitioned from BUSY/PERMIT.
#
# Usage:
#   source "${SCRIPT_DIR}/state_meta.sh"
#   icon=$(ccm_state_icon PERMIT)    # => ⚠
#   icon=$(ccm_state_icon UNKNOWN)   # => ● (fallback)

ccm_state_icon() {
    case "${1:-}" in
        PERMIT)    printf '⚠' ;;
        BUSY)      printf '◉' ;;
        COMPLETED) printf '✔' ;;
        IDLE)      printf '●' ;;
        SHELL)     printf '■' ;;
        DOWN)      printf '○' ;;
        IGNORED)   printf '⊘' ;;
        *)         printf '●' ;;
    esac
}
