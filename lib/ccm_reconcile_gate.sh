# shellcheck shell=bash
# The rate limit of the periodic status pass, kept apart from common.sh
# so the wrapper can apply it before reading anything else: on most
# seconds the pass does nothing, and then reading 800 lines of helpers
# was most of what it cost. Sourced by `ccm` (first) and by common.sh.

# Temp directory (user-scoped; overridable for isolation)
CCM_TMP_DIR="${CCM_TMP_DIR:-${TMPDIR:-/tmp}/ccm-${UID}}"

# Seconds between full status reconciliations. The periodic
# `#(ccm inject-status)` in status-right fires once per
# `status-interval` — every second on a config that shows a seconds
# clock — but a full run costs ~174 ms and spawns ~24 processes
# (python3 plus 21 tmux clients plus this wrapper), which is how a
# status bar ends up holding a measurable share of the machine.
# State changes do not need that poll: the hooks already push
# `inject-status --fast` on every transition, so the periodic pass
# only has to catch what fires no hook — a git branch switch, a new
# listening port, a stale-BUSY release crossing its window.
#
# 20 s is chosen against BUSY_STALE_RELEASE_SEC (60 s): the release
# it must observe is a threshold crossing rather than an event, so
# nothing re-evaluates it until a reconciliation runs. Keeping the
# interval well under that window bounds the worst case for an
# Esc-interrupted session at roughly 80 s rather than leaving it to
# the next unrelated hook.
CCM_RECONCILE_INTERVAL="${CCM_RECONCILE_INTERVAL:-20}"

# True when the periodic (non-`--fast`) status pass should do a full
# run. Deliberately implemented in the shell wrapper rather than in
# `inject_status.py`: reaching Python already costs a python3 start,
# and the whole point is to not pay it on the seconds in between.
#
# Written to fork nothing on the common path — a gate that spawns
# `date` and `tmux` to decide not to spawn python3 gives most of the
# saving back. `EPOCHSECONDS` is a bash 5 builtin (macOS ships 3.2 as
# /bin/bash, hence the fallback), and the server-restart check reads
# the socket's mtime with `[[ -nt ]]`, which is a builtin test.
#
# The socket mtime is the restart signal: it equals the server's
# `#{start_time}` (verified), so a socket newer than our stamp means
# the server restarted since the last reconciliation. Without that
# check a fresh server would render from state nobody had computed
# for up to CCM_RECONCILE_INTERVAL seconds — the stamp lives in
# $TMPDIR and outlives the server that wrote it.
_ccm_should_reconcile() {
    local stamp="${CCM_TMP_DIR}/reconcile-stamp"
    local now last
    now="${EPOCHSECONDS:-}"
    [[ -z "$now" ]] && now=$(date +%s)
    # Builtin checks only (this path must fork nothing): read the stamp
    # only from a root that is ours and only if it is a plain file.
    if [[ ! -L "$CCM_TMP_DIR" && -O "$CCM_TMP_DIR" && -f "$stamp" && ! -L "$stamp" ]]; then
        # Server restarted after our last run → reconcile now.
        local sock="${TMUX%%,*}"
        if [[ -z "$sock" || ! "$sock" -nt "$stamp" ]]; then
            read -r last < "$stamp" 2>/dev/null || true
            if [[ "$last" =~ ^[0-9]+$ ]] \
                && (( now - last < CCM_RECONCILE_INTERVAL )); then
                return 1
            fi
        fi
    fi
    # Writing waits for the full check; an untrusted root then stops
    # the run in ccm_init_dirs.
    ccm_secure_tmp_root "$CCM_TMP_DIR" && printf '%s\n' "$now" > "$stamp" 2>/dev/null
    return 0
}
