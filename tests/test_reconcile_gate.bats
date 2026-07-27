#!/usr/bin/env bats
# Rate limiting for the periodic status poll.
#
# tmux runs `#(ccm inject-status)` once per status-interval, which is
# every second on a config with a seconds clock. A full pass spawns
# ~24 processes; left ungated it made ccm a measurable share of a
# laptop's load and a contributor to a kernel zone exhaustion panic
# (2026-07-27). The gate keeps the seconds in between down to the
# shell itself, so these tests care as much about what does NOT run
# as about the decision.

setup() {
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    export CCM_TMP_DIR="$(mktemp -d)"
    STAMP="${CCM_TMP_DIR}/reconcile-stamp"
    source "${CCM_ROOT}/lib/common.sh"
}

teardown() {
    [[ -n "$CCM_TMP_DIR" && -d "$CCM_TMP_DIR" ]] && rm -rf "$CCM_TMP_DIR"
}

@test "reconcile: first call with no stamp runs" {
    run _ccm_should_reconcile
    [ "$status" -eq 0 ]
    [[ -f "$STAMP" ]]
}

@test "reconcile: second call within the interval is skipped" {
    export CCM_RECONCILE_INTERVAL=60
    _ccm_should_reconcile
    run _ccm_should_reconcile
    [ "$status" -eq 1 ]
}

@test "reconcile: call after the interval runs again" {
    export CCM_RECONCILE_INTERVAL=60
    _ccm_should_reconcile
    # Backdate the stamp past the window rather than sleeping.
    printf '%s\n' "$(( $(date +%s) - 61 ))" > "$STAMP"
    run _ccm_should_reconcile
    [ "$status" -eq 0 ]
}

@test "reconcile: a restarted server forces a run inside the interval" {
    # The stamp outlives the tmux server that wrote it ($TMPDIR is not
    # cleared on restart), so without this check a fresh server would
    # render from state nobody had computed for a whole interval. The
    # socket's mtime is the restart signal.
    export CCM_RECONCILE_INTERVAL=60
    export TMUX="${CCM_TMP_DIR}/fake-socket,1,0"
    : > "${CCM_TMP_DIR}/fake-socket"
    _ccm_should_reconcile
    run _ccm_should_reconcile
    [ "$status" -eq 1 ]          # still inside the window
    sleep 1
    touch "${CCM_TMP_DIR}/fake-socket"   # server restarted
    run _ccm_should_reconcile
    [ "$status" -eq 0 ]
}

@test "reconcile: an unreadable stamp does not wedge the gate" {
    # Fail open: a corrupt or truncated stamp must mean "reconcile",
    # never "skip forever".
    export CCM_RECONCILE_INTERVAL=60
    printf 'not-a-number\n' > "$STAMP"
    run _ccm_should_reconcile
    [ "$status" -eq 0 ]
}

@test "reconcile: the skip path never consults tmux or stat" {
    # The point of gating in the shell is to not pay for the work the
    # gate is avoiding. Consulting the tmux server or stat(1) to decide
    # would hand most of the saving back, so the restart check reads
    # the socket mtime with `[[ -nt ]]` — a builtin — and never runs a
    # tmux client. This half of the property holds on every bash.
    export CCM_RECONCILE_INTERVAL=60
    export TMUX="${CCM_TMP_DIR}/fake-socket,1,0"
    : > "${CCM_TMP_DIR}/fake-socket"
    _ccm_should_reconcile

    tmux() { echo "GATE FORKED tmux" >&2; return 1; }
    stat() { echo "GATE FORKED stat" >&2; return 1; }

    run _ccm_should_reconcile
    [ "$status" -eq 1 ]
    [[ "$output" != *"GATE FORKED"* ]] || {
        echo "gate shelled out on the skip path: $output"; return 1; }
}

@test "reconcile: skip path is fork-free where EPOCHSECONDS exists" {
    # Time is the one value the gate cannot get from a builtin on
    # every shell: EPOCHSECONDS arrived in bash 5, and macOS still
    # ships 3.2 as /bin/bash, so there the gate spends exactly one
    # `date`. That fallback is deliberate — 3 processes per second
    # instead of 2 is still an order of magnitude below the ~24 a full
    # pass costs — so this asserts the stronger property only where
    # the shell can actually deliver it, rather than pretending the
    # guarantee is universal (which is how this test first failed on
    # a macOS runner while passing on a homebrew bash 5).
    [[ -n "${EPOCHSECONDS:-}" ]] || skip "bash < 5: no EPOCHSECONDS builtin"
    export CCM_RECONCILE_INTERVAL=60
    export TMUX="${CCM_TMP_DIR}/fake-socket,1,0"
    : > "${CCM_TMP_DIR}/fake-socket"
    _ccm_should_reconcile

    date() { echo "GATE FORKED date" >&2; return 1; }

    run _ccm_should_reconcile
    [ "$status" -eq 1 ]
    [[ "$output" != *"GATE FORKED"* ]] || {
        echo "gate forked date despite EPOCHSECONDS: $output"; return 1; }
}

@test "ccm dispatcher: --fast is never rate-limited" {
    # The hook-driven push must not be delayed by the periodic poll's
    # budget; it is what makes a PERMIT show up immediately.
    grep -q 'inject-status" && "\$\*" != \*--fast\*' "${CCM_ROOT}/ccm"
}

@test "ccm dispatcher: the gate runs before ccm_init_dirs" {
    # ccm_init_dirs is mkdir plus two find sweeps — three more
    # processes per second for housekeeping that does not need 1 Hz.
    # The skip must happen first or the gate saves far less.
    gate_line=$(grep -n '_ccm_should_reconcile' "${CCM_ROOT}/ccm" | head -1 | cut -d: -f1)
    init_line=$(grep -n '^ccm_init_dirs' "${CCM_ROOT}/ccm" | head -1 | cut -d: -f1)
    [[ -n "$gate_line" && -n "$init_line" ]]
    [ "$gate_line" -lt "$init_line" ]
}
