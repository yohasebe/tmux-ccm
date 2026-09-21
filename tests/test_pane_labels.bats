#!/usr/bin/env bats
# The unread mark on a pane's border (opt-in: @ccm-pane-labels on).
# hooks/lib.sh sets `@ccm_unread` on a pane when a reply completes in
# it while the user is looking elsewhere. tmux is a function here that
# answers from variables and records what it was asked to do, so the
# tests need no tmux server.

setup() {
    CCM_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    CALLS="${BATS_TEST_TMPDIR}/calls"; : > "$CALLS"
    LABELS=on          # @ccm-pane-labels
    # What `list-clients -F '#{pane_id} #{client_flags}'` reports: the
    # pane each client is showing as its active one, and its flags.
    CLIENTS=("%3 attached,UTF-8")
    export TMPDIR="${BATS_TEST_TMPDIR}"
    export TMUX_PANE="%7"
    tmux() {
        echo "$*" >> "$CALLS"
        case "$1" in
            show-option)      [[ "$*" == *"@ccm-pane-labels"* ]] && echo "$LABELS" ;;
            list-clients)     printf '%s\n' "${CLIENTS[@]}" ;;
        esac
        return 0
    }
    source "${CCM_ROOT}/hooks/lib.sh"
}

_set_calls() { grep -c "^set-option -p -t %7 @ccm_unread " "$CALLS" || true; }

@test "a reply completing in a pane nobody is watching marks it" {
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 1 ]]
}

@test "the mark carries when the reply completed" {
    _ccm_mark_unread
    grep -Eq "^set-option -p -t %7 @ccm_unread [0-9]{9,}$" "$CALLS"
}

@test "a reply completing in the pane being watched is not marked" {
    CLIENTS=("%7 attached,focused,UTF-8")
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 0 ]]
}

@test "the active pane of a terminal that lost focus is not being watched" {
    # The pane is in front in tmux, but the terminal app is not.
    CLIENTS=("%7 attached,UTF-8")
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 1 ]]
}

@test "a focused client showing another pane is not watching this one" {
    CLIENTS=("%8 attached,focused,UTF-8")
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 1 ]]
}

@test "a pane whose id only begins the same is another pane" {
    CLIENTS=("%70 attached,focused,UTF-8")
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 1 ]]
}

@test "one client watching is enough, whichever session the window is linked into" {
    # The same window linked into two sessions: in front for one
    # client, not for the other. Resolving the pane on its own picks
    # one of them; each client has to be asked.
    CLIENTS=("%2 attached,UTF-8" "%7 attached,focused,UTF-8")
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 0 ]]
}

# A process whose command line says `dashboard.py`, as the real one's does.
_fake_dashboard() {
    local script="${BATS_TEST_TMPDIR}/dashboard.py"
    printf 'import time\ntime.sleep(30)\n' > "$script"
    python3 "$script" & FAKE_PID=$!
}

teardown() { [[ -n "${FAKE_PID:-}" ]] && kill "$FAKE_PID" 2>/dev/null || true; }

@test "nothing under ccm's own dashboard popup counts as watched" {
    # tmux goes on reporting the covered pane as the client's active
    # one, and says nothing about the popup.
    CLIENTS=("%7 attached,focused,UTF-8")
    _fake_dashboard
    mkdir -p "${TMPDIR}/ccm-${UID}"
    echo "$FAKE_PID" > "${TMPDIR}/ccm-${UID}/dashboard.pid"
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 1 ]]
}

@test "the dashboard's pid file is looked for where CCM_TMP_DIR puts it" {
    CLIENTS=("%7 attached,focused,UTF-8")
    _fake_dashboard
    export CCM_TMP_DIR="${BATS_TEST_TMPDIR}/custom-runtime"
    mkdir -p "$CCM_TMP_DIR"
    echo "$FAKE_PID" > "${CCM_TMP_DIR}/dashboard.pid"
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 1 ]]
}

@test "a pid that now belongs to some other process is not the dashboard" {
    # A crashed dashboard leaves its pid behind, and the OS reuses it.
    CLIENTS=("%7 attached,focused,UTF-8")
    mkdir -p "${TMPDIR}/ccm-${UID}"
    echo "$$" > "${TMPDIR}/ccm-${UID}/dashboard.pid"
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 0 ]]
}

@test "a dashboard pid file left by a process that is gone covers nothing" {
    CLIENTS=("%7 attached,focused,UTF-8")
    mkdir -p "${TMPDIR}/ccm-${UID}"
    echo "999999" > "${TMPDIR}/ccm-${UID}/dashboard.pid"
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 0 ]]
}

@test "nothing is marked unless the option is on" {
    LABELS=""
    _ccm_mark_unread
    [[ "$(_set_calls)" -eq 0 ]]
}

@test "outside tmux there is no pane to mark" {
    unset TMUX_PANE
    _ccm_mark_unread
    [[ ! -s "$CALLS" ]]
}

@test "a new prompt clears the pane's mark" {
    _ccm_clear_unread
    grep -q "^set-option -pu -t %7 @ccm_unread$" "$CALLS"
}

# ─── Wiring: the helpers above are reached from the real entry points ───

@test "the completion grace path marks the pane" {
    _ccm_instant_notify() { :; }
    _ccm_schedule_completed_notify "${BATS_TEST_TMPDIR}" "key" "/cwd" "proj" 0
    local i; for i in 1 2 3 4 5 6 7 8 9 10; do
        [[ "$(_set_calls)" -eq 1 ]] && break; sleep 0.2
    done
    [[ "$(_set_calls)" -eq 1 ]]
}

@test "a completion cancelled within the grace period marks nothing" {
    _ccm_instant_notify() { :; }
    # The notify stub leaves a file when the grace job reaches its end,
    # cancelled or not... it does not: a cancelled job skips it. So the
    # job's own end is what is waited for, not a guessed interval.
    _ccm_schedule_completed_notify "${BATS_TEST_TMPDIR}" "key" "/cwd" "proj" 1
    local job=$!
    _ccm_cancel_pending_completion "${BATS_TEST_TMPDIR}" "key"
    local i; for i in $(seq 1 50); do kill -0 "$job" 2>/dev/null || break; sleep 0.1; done
    ! kill -0 "$job" 2>/dev/null
    [[ "$(_set_calls)" -eq 0 ]]
}

@test "on-prompt-submit.sh, run as the hook, clears the mark on its pane" {
    local home="${BATS_TEST_TMPDIR}/home" bin="${BATS_TEST_TMPDIR}/hookbin"
    local log="${BATS_TEST_TMPDIR}/hook-tmux.log"
    mkdir -p "$home" "$bin"; : > "$log"
    printf '#!/usr/bin/env bash\necho "$*" >> "%s"\nexit 0\n' "$log" > "$bin/tmux"
    chmod +x "$bin/tmux"
    PATH="$bin:$PATH" TMPDIR="$home" HOME="$home" TMUX_PANE="%7" \
        bash -c "cd '${CCM_ROOT}' && mkdir -p \"\$TMPDIR/ccm-\$UID/hooks\" && \
                 echo '{\"cwd\":\"/work\",\"session_id\":\"sid-labels\"}' | hooks/on-prompt-submit.sh"
    grep -q "^set-option -pu -t %7 @ccm_unread$" "$log"
}

@test "ccm.tmux registers the border format and the focus hook once, only when asked" {
    local bin="${BATS_TEST_TMPDIR}/bin" log="${BATS_TEST_TMPDIR}/tmux.log"
    mkdir -p "$bin"; : > "$log"
    cat > "$bin/tmux" <<SHIM
#!/usr/bin/env bash
echo "\$*" >> "$log"
case "\$*" in
    *"show-option -gqv @ccm-pane-labels"*) cat "${BATS_TEST_TMPDIR}/labels" 2>/dev/null ;;
    *"show-option -gqv pane-border-format"*) cat "${BATS_TEST_TMPDIR}/format" 2>/dev/null ;;
    *"show-hooks -gw"*) cat "${BATS_TEST_TMPDIR}/hooks" 2>/dev/null ;;
    *"set-option -g pane-border-format"*) shift 3; printf '%s' "\$*" > "${BATS_TEST_TMPDIR}/format" ;;
    *"set-hook -ga pane-focus-in"*) echo "pane-focus-in[0] set-option -pu @ccm_unread" >> "${BATS_TEST_TMPDIR}/hooks" ;;
esac
exit 0
SHIM
    chmod +x "$bin/tmux"
    printf 'ORIGINAL' > "${BATS_TEST_TMPDIR}/format"

    PATH="$bin:$PATH" bash "${CCM_ROOT}/ccm.tmux" >/dev/null 2>&1 || true
    ! grep -q "pane-focus-in" "$log"                       # off: untouched
    [[ "$(cat "${BATS_TEST_TMPDIR}/format")" == "ORIGINAL" ]]

    echo on > "${BATS_TEST_TMPDIR}/labels"
    PATH="$bin:$PATH" bash "${CCM_ROOT}/ccm.tmux" >/dev/null 2>&1 || true
    PATH="$bin:$PATH" bash "${CCM_ROOT}/ccm.tmux" >/dev/null 2>&1 || true
    [[ "$(grep -c "set-hook -ga pane-focus-in" "$log")" -eq 1 ]]
    [[ "$(grep -o "@ccm_unread" "${BATS_TEST_TMPDIR}/format" | wc -l | tr -d ' ')" -eq 1 ]]
    [[ "$(cat "${BATS_TEST_TMPDIR}/format")" == *"ORIGINAL" ]]
}
