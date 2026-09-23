#!/usr/bin/env bash
# This PATH guard is test infrastructure, not an OS sandbox.
reject() {
    printf '%s: ' "${BATS_TEST_NAME:-unknown test}" >> "$CCM_TEST_GUARD_DIR/denied"
    printf '%q ' "$@" >> "$CCM_TEST_GUARD_DIR/denied"
    printf '\n' >> "$CCM_TEST_GUARD_DIR/denied"
    exit 99
}
original=("$@")
[[ "$1" == -L && "$2" == ccm-test.* && "$2" != */* &&
   -d "$CCM_TEST_GUARD_DIR/sockets/$2" ]] || reject "${original[@]}"
socket=$2
shift 2
# Permit the test's empty configuration, but no second selector or
# other server options that could override the isolation above.
if [[ "$1" == -f && "$2" == /dev/null ]]; then shift 2; fi
[[ -n "$1" && "$1" != -* ]] || reject "${original[@]}"
[[ -n "$CCM_TEST_REAL_TMUX" ]] || exit 127
unset TMUX TMUX_PANE
exec "$CCM_TEST_REAL_TMUX" -L "$socket" -f /dev/null "$@"
