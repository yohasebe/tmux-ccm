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
# Whatever the command (tmux takes `new` for `new-session`, and any
# command may start a server), the user's HOME never reaches tmux: a
# server's children run with the server's environment. A test that set
# its own HOME keeps it.
if [[ -z "$HOME" || "$HOME" == "$CCM_TEST_REAL_HOME" ]]; then
    export HOME="$CCM_TEST_GUARD_DIR/home"
fi
if [[ "$1" == new-session ]]; then
    # Keep tmux's stderr, then add only allowlisted diagnostic metadata.
    "$CCM_TEST_REAL_TMUX" -L "$socket" -f /dev/null "$@"
    result=$?
    if [[ "$result" -ne 0 ]]; then
        export LC_ALL=C
        socket_path="$TMUX_TMPDIR/tmux-$(id -u)/$socket"
        resolved_dir=$(cd "$TMUX_TMPDIR" && pwd -P)
        resolved_path="$resolved_dir/tmux-$(id -u)/$socket"
        printf 'tmux new-session failed: exit=%s socket=%s socket_bytes=%s config=/dev/null\n' \
            "$result" "$socket_path" "${#socket_path}" >&2
        printf 'resolved_socket_bytes=%s\n' "${#resolved_path}" >&2
        "$CCM_TEST_REAL_TMUX" -V >&2
        for name in HOME SHELL ZDOTDIR ENV BASH_ENV XDG_CONFIG_HOME TMUX_TMPDIR TERM; do
            # Bash may create unexported defaults (for example TERM=dumb).
            if value=$(command -p printenv "$name"); then present=yes; else present=no; fi
            printf '%s: set=%s bytes=%s\n' "$name" "$present" "${#value}" >&2
        done
    fi
    exit "$result"
fi
exec "$CCM_TEST_REAL_TMUX" -L "$socket" -f /dev/null "$@"
