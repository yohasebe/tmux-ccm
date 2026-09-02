#!/usr/bin/env bats
# Parity test: Python `notify()` and bash `_ccm_instant_notify` must
# reach the same fire/skip decision for every (@ccm-notify, state)
# combination. The two implementations are intentionally NOT merged
# — the bash hot path cannot tolerate 30-80 ms of Python interpreter
# startup per hook event. This test is the drift guard that replaces
# structural unification.
#
# Observation approach: PATH-override. We drop stub `osascript` and
# `notify-send` executables into a sandbox bin, which log the call
# to a shared file. If the stub is hit, the implementation decided
# "fire"; if the log is empty, the implementation decided "skip".
# No production code is modified.

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

load helpers/mock_tmux.bash

setup() {
    SANDBOX="$(mktemp -d)"
    export TMPDIR="${SANDBOX}/tmp"
    mkdir -p "$TMPDIR"

    setup_mocks  # lays down mock tmux, prepends ${MOCK_DIR}/bin to PATH

    NOTIFY_LOG="${SANDBOX}/notify.log"
    : > "$NOTIFY_LOG"
    export NOTIFY_LOG

    # Stub terminal-notifier: extracts the title and records it.
    # Both impls prefer terminal-notifier when on PATH; we want the
    # parity test to exercise that preferred branch.
    cat > "${MOCK_DIR}/bin/terminal-notifier" <<STUB
#!/usr/bin/env bash
# Walk -title <value> pairs.
title=""
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        -title) title="\$2"; shift 2 ;;
        *) shift ;;
    esac
done
[[ -n "\$title" ]] && printf 'terminal-notifier %s\n' "\$title" >> "$NOTIFY_LOG"
STUB
    chmod +x "${MOCK_DIR}/bin/terminal-notifier"

    # Stub osascript fallback (only hit when terminal-notifier is
    # absent; kept for the cross-platform / minimal-install path).
    cat > "${MOCK_DIR}/bin/osascript" <<STUB
#!/usr/bin/env bash
for arg in "\$@"; do
    case "\$arg" in
        *'with title '*)
            printf 'osascript %s\n' "\$arg" >> "$NOTIFY_LOG"
            ;;
    esac
done
STUB
    chmod +x "${MOCK_DIR}/bin/osascript"

    # notify-send fallback (Linux).
    cat > "${MOCK_DIR}/bin/notify-send" <<STUB
#!/usr/bin/env bash
printf 'notify-send %s\n' "\$*" >> "$NOTIFY_LOG"
STUB
    chmod +x "${MOCK_DIR}/bin/notify-send"

    # Reset Python's terminal-notifier path cache so it picks up
    # the sandboxed stub instead of any cached real path.
    export CCM_RESET_NOTIFIER_CACHE=1

    source "${CCM_ROOT}/hooks/lib.sh"
}

teardown() {
    teardown_mocks
    [[ -n "$SANDBOX" && -d "$SANDBOX" ]] && rm -rf "$SANDBOX"
}

# Invoke both implementations with the given state / project /
# detail, using a fresh marker key per call so the bash dedup never
# interferes. Waits briefly for async notification subprocesses
# (bash uses `&`, Python uses Popen without wait) to finish writing
# to the log.
_invoke_both() {
    local state="$1" project="$2" detail="${3:-}"
    : > "$NOTIFY_LOG"

    # Both implementations must return 0 on either fire-or-skip; a
    # non-zero here means they leaked an error exit code and the test
    # should fail loudly rather than mask it with `|| true`.
    local bash_cwd="/tmp/parity-${state}-$$-${RANDOM}"
    _ccm_instant_notify "$state" "$project" "$detail" "$bash_cwd"

    PYTHONDONTWRITEBYTECODE=1 python3 -c "
import sys
sys.path.insert(0, '${CCM_ROOT}/lib')
import ccm_notify
ccm_notify.notify('${state}', '${project}', '${detail}')
"

    # Poll the log briefly; stubs are fast but Popen/& are async.
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        [[ -s "$NOTIFY_LOG" ]] && break
        sleep 0.05
    done
    # Even after a "fire" observation, give a small window for the
    # *other* implementation to also complete before we count.
    sleep 0.1
}

# Count fire-records attributable to a specific title substring.
# Both impls embed the project name in the title (e.g. "ccm ⚠ proj"),
# so a per-state title substring isolates the right call in the log.
_log_count() {
    local pattern="$1"
    grep -c -- "$pattern" "$NOTIFY_LOG" 2>/dev/null || true
}

# Assert both impls fired (bash via `&` + Python via Popen = 2 log
# lines matching the expected title fragment).
assert_both_fired() {
    local title_frag="$1"
    local count
    count=$(_log_count "$title_frag")
    [[ "$count" -eq 2 ]] || {
        echo "expected 2 fires matching '$title_frag', got $count" >&2
        echo "--- log ---" >&2
        cat "$NOTIFY_LOG" >&2
        return 1
    }
}

# Assert neither impl fired.
assert_neither_fired() {
    local size
    size=$(wc -c < "$NOTIFY_LOG" | tr -d ' ')
    [[ "$size" -eq 0 ]] || {
        echo "expected no fires, log non-empty:" >&2
        cat "$NOTIFY_LOG" >&2
        return 1
    }
}

# ============================================================
# Matrix: @ccm-notify × state
# ============================================================

@test "off: PERMIT skipped by both" {
    mock_set_global_option @ccm-notify off
    _invoke_both PERMIT proj
    assert_neither_fired
}

@test "off: COMPLETED skipped by both" {
    mock_set_global_option @ccm-notify off
    _invoke_both COMPLETED proj
    assert_neither_fired
}

@test "permit: PERMIT fires in both" {
    mock_set_global_option @ccm-notify permit
    _invoke_both PERMIT proj "Bash: ls"
    assert_both_fired "ccm ⚠ proj"
}

@test "permit: COMPLETED skipped by both" {
    mock_set_global_option @ccm-notify permit
    _invoke_both COMPLETED proj
    assert_neither_fired
}

@test "completed: COMPLETED fires in both" {
    mock_set_global_option @ccm-notify completed
    _invoke_both COMPLETED proj
    assert_both_fired "ccm ✔ proj"
}

@test "completed: PERMIT skipped by both" {
    mock_set_global_option @ccm-notify completed
    _invoke_both PERMIT proj
    assert_neither_fired
}

@test "permit,completed: PERMIT fires in both" {
    mock_set_global_option @ccm-notify permit,completed
    _invoke_both PERMIT proj
    assert_both_fired "ccm ⚠ proj"
}

@test "permit,completed: COMPLETED fires in both" {
    mock_set_global_option @ccm-notify permit,completed
    _invoke_both COMPLETED proj
    assert_both_fired "ccm ✔ proj"
}

@test "all: PERMIT fires in both" {
    mock_set_global_option @ccm-notify all
    _invoke_both PERMIT proj
    assert_both_fired "ccm ⚠ proj"
}

@test "all: COMPLETED fires in both" {
    mock_set_global_option @ccm-notify all
    _invoke_both COMPLETED proj
    assert_both_fired "ccm ✔ proj"
}

@test "all: BUSY fires in Python; bash has no BUSY entry point" {
    # _ccm_instant_notify is only wired for PERMIT / COMPLETED in the
    # hook scripts — there is no BUSY call site. Python `notify()`
    # does emit BUSY when `@ccm-notify=all`. This row documents the
    # asymmetry so future edits to either side are forced to
    # consciously decide whether to add BUSY to the bash API.
    mock_set_global_option @ccm-notify all
    : > "$NOTIFY_LOG"

    PYTHONDONTWRITEBYTECODE=1 python3 -c "
import sys
sys.path.insert(0, '${CCM_ROOT}/lib')
import ccm_notify
ccm_notify.notify('BUSY', 'proj', '')
"
    # Wait for Popen to complete.
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        [[ -s "$NOTIFY_LOG" ]] && break
        sleep 0.05
    done
    sleep 0.05

    local count
    count=$(_log_count "ccm ◉ proj")
    [[ "$count" -eq 1 ]] || {
        echo "expected 1 BUSY fire from Python, got $count" >&2
        cat "$NOTIFY_LOG" >&2
        return 1
    }
}

@test "default setting (empty/unset): PERMIT fires in both" {
    # The tmux option is unset — both impls default to permit,completed.
    _invoke_both PERMIT proj
    assert_both_fired "ccm ⚠ proj"
}

@test "default setting (empty/unset): COMPLETED fires in both" {
    _invoke_both COMPLETED proj
    assert_both_fired "ccm ✔ proj"
}

# ============================================================
# bash 3.2 compatibility (stock macOS /bin/bash)
# ============================================================

@test "bash 3.2 compat: _ccm_instant_notify survives stock macOS /bin/bash" {
    # Hook scripts run via `#!/usr/bin/env bash`. For users without
    # Homebrew bash that resolves to bash 3.2.57, where bash-4-only
    # syntax (e.g. `${state,,}`) dies with "Bad substitution" — fatal
    # under the hook scripts' `set -euo pipefail`, silently killing
    # the entire notification path. Pin the code path against the
    # real 3.2 binary.
    local bash_major
    bash_major=$(/bin/bash -c 'echo "${BASH_VERSINFO[0]}"')
    [[ "$bash_major" == "3" ]] || skip "/bin/bash is not bash 3.x (got major=$bash_major)"

    mock_set_global_option @ccm-notify all
    : > "$NOTIFY_LOG"

    # `set -euo pipefail` mirrors the on-*.sh hook preamble; a Bad
    # substitution there aborts the script, so status must be 0.
    # PATH is inherited, so the sandboxed tmux / terminal-notifier
    # stubs are used.
    run /bin/bash -c '
        set -euo pipefail
        source "'"$CCM_ROOT"'/hooks/lib.sh"
        _ccm_instant_notify "PERMIT" "proj32" "" "/tmp/parity-bash32-$$"
    '
    [ "$status" -eq 0 ]

    # The notification itself is dispatched async (`&`); poll briefly.
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        [[ -s "$NOTIFY_LOG" ]] && break
        sleep 0.05
    done
    sleep 0.1
    [[ "$(_log_count "ccm ⚠ proj32")" -ge 1 ]] || {
        echo "expected notification under bash 3.2, log:" >&2
        cat "$NOTIFY_LOG" >&2
        return 1
    }
}

@test "bash 3.2 compat: no bash-4-only syntax in hooks/*.sh" {
    # Static guard so the next `${var,,}` / `${var^^}` / `declare -A` /
    # `mapfile` / `;&` slips in nowhere, not just on the notify path.
    # Comments are stripped first so documentation may mention the
    # constructs without tripping the guard.
    local f hit out=""
    for f in "$CCM_ROOT"/hooks/*.sh; do
        hit=$(sed 's/#.*$//' "$f" | grep -nE '\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^)|declare -A|local -A|mapfile|readarray|;&' || true)
        [[ -n "$hit" ]] && out+="${f}:"$'\n'"${hit}"$'\n'
    done
    [[ -z "$out" ]] || {
        echo "bash-4-only syntax found in hooks:" >&2
        echo "$out" >&2
        return 1
    }
}

# ============================================================
# @ccm-notify-transport: forced osascript transport
# ============================================================

@test "transport=osascript: both impls skip terminal-notifier for the osascript path" {
    mock_set_global_option @ccm-notify "permit,completed"
    mock_set_global_option @ccm-notify-transport "osascript"
    _invoke_both PERMIT transportproj
    assert_both_fired "transportproj"
    [[ "$(_log_count 'terminal-notifier')" -eq 0 ]]
    [[ "$(_log_count 'osascript')" -eq 2 ]]
}

@test "transport unset: terminal-notifier stays preferred (both impls)" {
    mock_set_global_option @ccm-notify "permit,completed"
    _invoke_both PERMIT autoproj
    assert_both_fired "autoproj"
    [[ "$(_log_count 'terminal-notifier')" -eq 2 ]]
    [[ "$(_log_count 'osascript')" -eq 0 ]]
}
