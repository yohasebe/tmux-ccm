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

teardown() {
    [[ -n "${FAKE_PID:-}" ]] && kill "$FAKE_PID" 2>/dev/null
    [[ -n "${GATE:-}" ]] && touch "$GATE.open"            # never leave a hook waiting
    if [[ -n "${REAL_SOCK:-}" ]]; then
        command tmux -L "$REAL_SOCK" kill-server 2>/dev/null
        [[ -n "${REAL_SOCK_PATH:-}" ]] && rm -f "$REAL_SOCK_PATH" 2>/dev/null
    fi
    return 0
}

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
    *"set-hook -ga pane-focus-in"*) echo "pane-focus-in[1] \$4" >> "${BATS_TEST_TMPDIR}/hooks" ;;
    *"set-hook -gwu pane-focus-in["*) n="\${3#pane-focus-in[}"; n="\${n%]}"
        grep -v "^pane-focus-in\\[\$n\\] " "${BATS_TEST_TMPDIR}/hooks" > "${BATS_TEST_TMPDIR}/hooks.new"
        mv "${BATS_TEST_TMPDIR}/hooks.new" "${BATS_TEST_TMPDIR}/hooks" ;;
esac
exit 0
SHIM
    chmod +x "$bin/tmux"
    printf 'ORIGINAL' > "${BATS_TEST_TMPDIR}/format"

    PATH="$bin:$PATH" bash "${CCM_ROOT}/ccm.tmux" >/dev/null 2>&1 || true
    ! grep -q "pane-focus-in" "$log"                       # off: untouched
    [[ "$(cat "${BATS_TEST_TMPDIR}/format")" == "ORIGINAL" ]]

    echo on > "${BATS_TEST_TMPDIR}/labels"
    # An install from before the handler existed: it cleared in the hook
    # itself, and left beside the new one it would go on clearing at once.
    cat > "${BATS_TEST_TMPDIR}/hooks" <<'HOOKS'
pane-focus-in[0] set-option -pu @ccm_unread
pane-focus-in[2] set-option -g @mine yes
pane-focus-in[9] set-option -pu @ccm_unread
pane-focus-in[10] run-shell "echo #{@ccm_unread} >> my.log"
pane-focus-in[11] set-option -pu @ccm_unread
HOOKS
    PATH="$bin:$PATH" bash "${CCM_ROOT}/ccm.tmux" >/dev/null 2>&1 || true
    PATH="$bin:$PATH" bash "${CCM_ROOT}/ccm.tmux" >/dev/null 2>&1 || true
    [[ "$(grep -c "set-hook -ga pane-focus-in" "$log")" -eq 1 ]]
    grep -q "on-pane-focus.sh" "${BATS_TEST_TMPDIR}/hooks"
    ! grep -q "set-option -pu @ccm_unread$" "${BATS_TEST_TMPDIR}/hooks"
    # The user's own hooks stay, including one that reads the mark.
    grep -q "^pane-focus-in\[2\] set-option -g @mine yes$" "${BATS_TEST_TMPDIR}/hooks"
    grep -q "^pane-focus-in\[10\] run-shell" "${BATS_TEST_TMPDIR}/hooks"
    [[ "$(grep -o "@ccm_unread" "${BATS_TEST_TMPDIR}/format" | wc -l | tr -d ' ')" -eq 1 ]]
    [[ "$(cat "${BATS_TEST_TMPDIR}/format")" == *"ORIGINAL" ]]
}

# ─── The focus handler: the mark outlives the moment of arriving ───

_focus_handler() {   # runs lib/on-pane-focus.sh against a tmux that keeps pane options in files
    local bin="${BATS_TEST_TMPDIR}/fbin"; FLOG="${BATS_TEST_TMPDIR}/focus.log"
    mkdir -p "$bin"; touch "$FLOG"
    cat > "$bin/tmux" <<SHIM
#!/usr/bin/env bash
echo "\$*" >> "$FLOG"
opt() { echo "${BATS_TEST_TMPDIR}/opt-\${1#@}"; }
case "\$1 \$2" in
    "show-option -pqv") cat "\$(opt "\$5")" 2>/dev/null ;;
    "show-option -gqv") echo "\${LINGER:-0.2}" ;;
    "set-option -p")    printf '%s' "\$6" > "\$(opt "\$5")" ;;
    "set-option -pu")   rm -f "\$(opt "\$5")" ;;
    "if-shell -F")      # the handler's guarded clears; real tmux is asked below
        ok=1
        if [[ "\$5" == *"@ccm_unread_look},"* ]]; then
            want=\$(sed -n 's/.*@ccm_unread_look},\\([^}]*\\)}.*/\\1/p' <<<"\$5")
            [[ "\$(cat "\$(opt @ccm_unread_look)" 2>/dev/null)" == "\$want" ]] || ok=0
        fi
        if [[ "\$5" == *"#{@ccm_unread},"* ]]; then
            want=\$(sed -n 's/.*#{@ccm_unread},\\([^}]*\\)}.*/\\1/p' <<<"\$5")
            [[ "\$(cat "\$(opt @ccm_unread)" 2>/dev/null)" == "\$want" ]] || ok=0
        fi
        if [[ \$ok == 1 ]]; then
            [[ "\$6" == *"@ccm_unread_look"* ]] && rm -f "\$(opt @ccm_unread_look)" || rm -f "\$(opt @ccm_unread)"
        fi ;;
    "list-clients -F")  cat "${BATS_TEST_TMPDIR}/clients" 2>/dev/null ;;
esac
exit 0
SHIM
    chmod +x "$bin/tmux"
    PATH="$bin:$PATH" TMPDIR="${BATS_TEST_TMPDIR}" LINGER="${LINGER:-0.2}" \
        "${CCM_ROOT}/lib/on-pane-focus.sh" "$@"
}
_mark() { printf '%s' "$1" > "${BATS_TEST_TMPDIR}/opt-ccm_unread"; }
_marked() { [[ -s "${BATS_TEST_TMPDIR}/opt-ccm_unread" ]]; }
_looking_at() { echo "$1 attached,focused,UTF-8" > "${BATS_TEST_TMPDIR}/clients"; }

@test "focus handler: a pane still being looked at after the linger loses its mark" {
    _mark 1; _looking_at "%7"
    _focus_handler "%7" "%7"
    ! _marked
}

@test "focus handler: a pane that was only passed through keeps its mark" {
    _mark 1; _looking_at "%8"          # moved on
    _focus_handler "%7" "%7"
    _marked
}

@test "focus handler: the mark is not cleared before the linger has passed" {
    _mark 1; _looking_at "%7"
    LINGER=3 _focus_handler "%7" "%7" &
    sleep 1
    _marked
    wait
    ! _marked
}

@test "focus handler: coming back starts the wait again, and the earlier wait clears nothing" {
    # Look, leave, come back just before the first wait would end: the
    # mark must still be there a moment after returning, which is the
    # whole point of lingering.
    _mark 1; _looking_at "%7"
    local stamp="${BATS_TEST_TMPDIR}/opt-ccm_unread_look" i look1
    LINGER=2 _focus_handler "%7" "%7" & local first=$!
    for i in $(seq 1 50); do [[ -s "$stamp" ]] && break; sleep 0.1; done
    look1=$(cat "$stamp")
    LINGER=2 _focus_handler "%7" "%7" &     # the return: a second focus event
    for i in $(seq 1 50); do [[ "$(cat "$stamp" 2>/dev/null)" != "$look1" ]] && break; sleep 0.1; done
    [[ "$(cat "$stamp")" != "$look1" ]]      # the second look is the current one
    wait "$first"                            # the first wait has ended, whenever that was
    _marked
    wait
    ! _marked                                # the second wait, run in full, clears it
}

@test "focus handler: a newer reply's mark is not cleared by the look at an older one" {
    _mark 301; _looking_at "%7"
    LINGER=1 _focus_handler "%7" "%7" &
    # Wait for the handler to have started its look — it stamps the pane
    # once it has read the mark — rather than guessing how long that takes.
    local i; for i in $(seq 1 50); do
        [[ -s "${BATS_TEST_TMPDIR}/opt-ccm_unread_look" ]] && break; sleep 0.1
    done
    [[ -s "${BATS_TEST_TMPDIR}/opt-ccm_unread_look" ]]
    _mark 302                                # another reply completed meanwhile
    wait
    _marked
    [[ "$(cat "${BATS_TEST_TMPDIR}/opt-ccm_unread")" == "302" ]]
}

@test "focus handler: a pane with no mark costs no waiting" {
    rm -f "${BATS_TEST_TMPDIR}/opt-ccm_unread"
    local start=$SECONDS
    LINGER=5 _focus_handler "%7" "%7"
    [[ $((SECONDS - start)) -lt 3 ]]
    ! grep -q "set-option" "${BATS_TEST_TMPDIR}/focus.log"
}

@test "focus handler: the pane comes from the hook, or from the format beside it" {
    _mark 1; _looking_at "%9"
    _focus_handler "" "%9"
    grep -q "^if-shell -F -t %9 " "${BATS_TEST_TMPDIR}/focus.log"
    ! _marked
}

@test "focus handler, real tmux: a reply and a return landing after its checks are left alone" {
    # The handler has passed every check and is about to clear when a
    # new reply completes and the user comes back: a new mark, a new
    # stamp. Asked of a real tmux server, because whether compare and
    # clear can be separated is tmux's answer to give, not a shim's.
    # `command -v` would find the function setup() defines; ask for the file.
    type -P tmux >/dev/null || skip "tmux not installed"
    local sock="ccm-labels-$$-${RANDOM}"; REAL_SOCK="$sock" bin="${BATS_TEST_TMPDIR}/rbin"
    tmux() { command tmux -L "$sock" -f /dev/null "$@"; }
    tmux new-session -d -x 80 -y 24 -s t
    REAL_SOCK_PATH=$(tmux display-message -p '#{socket_path}')   # where tmux put it, not a guess
    local pane; pane=$(tmux display-message -p -t t '#{pane_id}')
    tmux set-option -g @ccm-unread-linger 0.2
    tmux set-option -p -t "$pane" @ccm_unread 301
    mkdir -p "$bin"
    cat > "$bin/tmux" <<SHIM
#!/usr/bin/env bash
real() { command -p env PATH="$PATH" tmux -L "$sock" "\$@"; }
if [[ "\$1" == "list-clients" ]]; then
    # The last thing the handler asks before clearing. While it waits for
    # the answer, the world moves on.
    real set-option -p -t "$pane" @ccm_unread 302
    real set-option -p -t "$pane" @ccm_unread_look newer-look
    echo "$pane attached,focused,UTF-8"
    exit 0
fi
real "\$@"
SHIM
    chmod +x "$bin/tmux"
    PATH="$bin:$PATH" TMPDIR="${BATS_TEST_TMPDIR}" "${CCM_ROOT}/lib/on-pane-focus.sh" "$pane" "$pane"
    local mark look
    mark=$(tmux show-option -pqv -t "$pane" @ccm_unread)
    look=$(tmux show-option -pqv -t "$pane" @ccm_unread_look)
    tmux kill-server
    [[ "$mark" == "302" ]] || { echo "mark is [$mark]"; return 1; }
    [[ "$look" == "newer-look" ]] || { echo "look is [$look]"; return 1; }
}

@test "focus handler, real tmux: a hook blocking between the two clears cannot cost a newer look its stamp" {
    # tmux runs `after-set-option` hooks between commands, and one that
    # blocks lets another client's commands through. The mark is gone by
    # then; the stamp must go only if it is still this handler's.
    type -P tmux >/dev/null || skip "tmux not installed"
    local sock="ccm-labels-$$-${RANDOM}"; REAL_SOCK="$sock"
    local bin="${BATS_TEST_TMPDIR}/rbin3" gate="${BATS_TEST_TMPDIR}/gate"
    tmux() { command tmux -L "$sock" -f /dev/null "$@"; }
    tmux new-session -d -x 80 -y 24 -s t
    REAL_SOCK_PATH=$(tmux display-message -p '#{socket_path}')   # where tmux put it, not a guess
    local pane; pane=$(tmux display-message -p -t t '#{pane_id}')
    tmux set-option -g @ccm-unread-linger 0.2
    tmux set-option -p -t "$pane" @ccm_unread 301
    mkdir -p "$bin"
    cat > "$bin/tmux" <<SHIM
#!/usr/bin/env bash
real() { command -p env PATH="$PATH" tmux -L "$sock" "\$@"; }
if [[ "\$1" == "list-clients" ]]; then
    # From here on, the first set-option to finish blocks until released.
    real set-hook -g after-set-option "run-shell '$bin/block.sh'"
    touch "$gate.armed"
    echo "$pane attached,focused,UTF-8"; exit 0
fi
real "\$@"
SHIM
    chmod +x "$bin/tmux"
    # The hook, as a file of its own so nothing in it needs escaping. It
    # disarms itself before blocking, so it holds up exactly one command —
    # the handler's first clear — and not the test's own writes after it.
    # `held` records how long it really waited: a hook that returns at
    # once makes this test pass whatever the handler does.
    cat > "$bin/block.sh" <<BLOCK
#!/usr/bin/env bash
[[ -e "$gate.armed" ]] || exit 0
rm -f "$gate.armed"; touch "$gate.blocked"
n=0; while [[ ! -e "$gate.open" && \$n -lt 200 ]]; do sleep 0.05; n=\$((n+1)); done
# Why it stopped waiting matters: giving up on its own means the queue
# was let go before the test wrote anything, and the test proves nothing.
[[ -e "$gate.open" ]] && echo "released \$n" > "$gate.held" || echo "gave-up \$n" > "$gate.held"
BLOCK
    chmod +x "$bin/block.sh"
    GATE="$gate"
    PATH="$bin:$PATH" TMPDIR="${BATS_TEST_TMPDIR}" "${CCM_ROOT}/lib/on-pane-focus.sh" "$pane" "$pane" &
    local i; for i in $(seq 1 100); do [[ -e "$gate.blocked" ]] && break; sleep 0.05; done
    [[ -e "$gate.blocked" ]] || { echo "the hook never blocked"; return 1; }
    # While the old handler's first clear is held up: a new reply, a new look.
    tmux set-option -p -t "$pane" @ccm_unread 302
    tmux set-option -p -t "$pane" @ccm_unread_look newer-look
    sleep 0.3                                 # the hook is still holding the queue
    touch "$gate.open"; wait
    [[ "$(cat "$gate.held")" == released\ * && "$(cut -d' ' -f2 "$gate.held")" -ge 3 ]] \
        || { echo "the hook did not hold the queue until released: $(cat "$gate.held")"; return 1; }
    [[ "$(tmux show-option -pqv -t "$pane" @ccm_unread)" == "302" ]]
    [[ "$(tmux show-option -pqv -t "$pane" @ccm_unread_look)" == "newer-look" ]]
}

@test "focus handler, real tmux: an undisturbed look clears the mark and its stamp" {
    # `command -v` would find the function setup() defines; ask for the file.
    type -P tmux >/dev/null || skip "tmux not installed"
    local sock="ccm-labels-$$-${RANDOM}"; REAL_SOCK="$sock" bin="${BATS_TEST_TMPDIR}/rbin2"
    tmux() { command tmux -L "$sock" -f /dev/null "$@"; }
    tmux new-session -d -x 80 -y 24 -s t
    REAL_SOCK_PATH=$(tmux display-message -p '#{socket_path}')   # where tmux put it, not a guess
    local pane; pane=$(tmux display-message -p -t t '#{pane_id}')
    tmux set-option -g @ccm-unread-linger 0.2
    tmux set-option -p -t "$pane" @ccm_unread 301
    mkdir -p "$bin"
    cat > "$bin/tmux" <<SHIM
#!/usr/bin/env bash
real() { command -p env PATH="$PATH" tmux -L "$sock" "\$@"; }
if [[ "\$1" == "list-clients" ]]; then echo "$pane attached,focused,UTF-8"; exit 0; fi
real "\$@"
SHIM
    chmod +x "$bin/tmux"
    PATH="$bin:$PATH" TMPDIR="${BATS_TEST_TMPDIR}" "${CCM_ROOT}/lib/on-pane-focus.sh" "$pane" "$pane"
    local mark look
    mark=$(tmux show-option -pqv -t "$pane" @ccm_unread)
    look=$(tmux show-option -pqv -t "$pane" @ccm_unread_look)
    tmux kill-server
    [[ -z "$mark" && -z "$look" ]] || { echo "mark=[$mark] look=[$look]"; return 1; }
}
