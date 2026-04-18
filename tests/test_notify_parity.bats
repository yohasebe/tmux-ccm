#!/usr/bin/env bats
# Parity test: Python `notify()` and bash `_ccm_instant_notify` must
# reach the same fire/skip decision for every (@ccm-notify, state)
# combination. The two implementations are intentionally NOT merged
# (see project_r4_r5_decision memo) — the bash hot path cannot
# tolerate 30-80 ms of Python interpreter startup per hook event.
# This test is the drift guard that replaces structural unification.
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

    # Stub osascript: records "osascript <title>" to the log. Title
    # is the AppleScript fragment between `with title "` and the
    # trailing quote — enough to confirm the call happened and
    # identify which state fired.
    cat > "${MOCK_DIR}/bin/osascript" <<STUB
#!/usr/bin/env bash
# The -e argument holds a string like:
#   display notification "BODY" with title "TITLE"[ sound name "NAME"]
for arg in "\$@"; do
    case "\$arg" in
        *'with title '*)
            printf 'osascript %s\n' "\$arg" >> "$NOTIFY_LOG"
            ;;
    esac
done
STUB
    chmod +x "${MOCK_DIR}/bin/osascript"

    # notify-send fallback (only hit on Linux / when osascript is
    # missing). We still wire it up so the stub can never accidentally
    # run the host's real `notify-send`.
    cat > "${MOCK_DIR}/bin/notify-send" <<STUB
#!/usr/bin/env bash
printf 'notify-send %s\n' "\$*" >> "$NOTIFY_LOG"
STUB
    chmod +x "${MOCK_DIR}/bin/notify-send"

    # Required for `command -v osascript` in the bash implementation —
    # bash searches PATH, finds our stub, and treats it as available.
    # The Python side uses subprocess.Popen(["osascript", ...]) which
    # also resolves through PATH to the stub.

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
    local bash_key="bash-${state}-$$-${RANDOM}"
    _ccm_instant_notify "$state" "$project" "$detail" "$bash_key"

    PYTHONDONTWRITEBYTECODE=1 python3 -c "
import sys
sys.path.insert(0, '${CCM_ROOT}/lib')
import ccm_core
ccm_core.notify('${state}', '${project}', '${detail}')
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

@test "done (back-compat alias): COMPLETED fires in both" {
    mock_set_global_option @ccm-notify done
    _invoke_both COMPLETED proj
    assert_both_fired "ccm ✔ proj"
}

@test "done (back-compat alias): PERMIT skipped by both" {
    mock_set_global_option @ccm-notify done
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
import ccm_core
ccm_core.notify('BUSY', 'proj', '')
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
