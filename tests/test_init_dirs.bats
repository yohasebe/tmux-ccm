#!/usr/bin/env bats
# ccm_gc_tmp ages out disposable caches only; control state and
# per-session hook files are not subject to the hourly sweep.
# ccm_init_dirs only creates directories.

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

setup() {
    MOCK_DIR="$(mktemp -d)"
    export HOME="$MOCK_DIR"
    export CCM_TMP_DIR="${MOCK_DIR}/tmp"
    export CCM_DATA_DIR="${MOCK_DIR}/data"
    source "${CCM_ROOT}/lib/common.sh"
    export CCM_TMP_DIR="${MOCK_DIR}/tmp"
    export CCM_SNAPSHOT_DIR="${CCM_DATA_DIR}/snapshots"
    export CCM_STATE_DIR="${CCM_DATA_DIR}/state"
    export CCM_HOOK_DIR="${CCM_TMP_DIR}/hooks"
    mkdir -p "${CCM_TMP_DIR}/git-cache" "${CCM_TMP_DIR}/port-cache" "${CCM_TMP_DIR}/notified" "${CCM_TMP_DIR}/hooks"
}

teardown() {
    [[ -n "$MOCK_DIR" && -d "$MOCK_DIR" ]] && rm -rf "$MOCK_DIR"
}

_old() { touch -t 202001010000 "$1"; }

@test "gc_tmp: old cache entries are removed" {
    _old "${CCM_TMP_DIR}/git-cache/abc"
    _old "${CCM_TMP_DIR}/port-cache/abc"
    _old "${CCM_TMP_DIR}/notified/abc"
    ccm_gc_tmp
    [ ! -e "${CCM_TMP_DIR}/git-cache/abc" ]
    [ ! -e "${CCM_TMP_DIR}/port-cache/abc" ]
    [ ! -e "${CCM_TMP_DIR}/notified/abc" ]
}

@test "gc_tmp: a fresh cache entry survives" {
    touch "${CCM_TMP_DIR}/git-cache/fresh"
    ccm_gc_tmp
    [ -e "${CCM_TMP_DIR}/git-cache/fresh" ]
}

@test "gc_tmp: an old dashboard pid marker, lock and popup file survive" {
    _old "${CCM_TMP_DIR}/dashboard.pid"
    _old "${CCM_TMP_DIR}/inject.lock"
    _old "${CCM_TMP_DIR}/popup-session"
    _old "${CCM_TMP_DIR}/mode2-active"
    ccm_gc_tmp
    [ -e "${CCM_TMP_DIR}/dashboard.pid" ]
    [ -e "${CCM_TMP_DIR}/inject.lock" ]
    [ -e "${CCM_TMP_DIR}/popup-session" ]
    [ -e "${CCM_TMP_DIR}/mode2-active" ]
}

@test "gc_tmp: old per-session hook files survive" {
    _old "${CCM_TMP_DIR}/hooks/some-session.events.jsonl"
    _old "${CCM_TMP_DIR}/hooks/some-session"
    ccm_gc_tmp
    [ -e "${CCM_TMP_DIR}/hooks/some-session.events.jsonl" ]
    [ -e "${CCM_TMP_DIR}/hooks/some-session" ]
}

@test "init_dirs: creates the directories it needs" {
    rm -rf "${CCM_TMP_DIR}" "${CCM_DATA_DIR}"
    ccm_init_dirs
    [ -d "${CCM_TMP_DIR}/git-cache" ] && [ -d "${CCM_TMP_DIR}/port-cache" ] && [ -d "${CCM_HOOK_DIR}" ]
}

@test "init_dirs: does not sweep caches itself" {
    _old "${CCM_TMP_DIR}/git-cache/abc"
    ccm_init_dirs
    [ -e "${CCM_TMP_DIR}/git-cache/abc" ]
}

@test "dispatcher: the cache sweep is skipped on the --fast path" {
    run grep -n 'ccm_gc_tmp' "${CCM_ROOT}/ccm"
    [ "$status" -eq 0 ]
    # The call sits inside a guard that excludes --fast invocations.
    guard_line=$(grep -n '"\$\*" != \*--fast\*' "${CCM_ROOT}/ccm" | tail -1 | cut -d: -f1)
    call_line=$(grep -n '^    ccm_gc_tmp' "${CCM_ROOT}/ccm" | head -1 | cut -d: -f1)
    [ -n "$guard_line" ] && [ -n "$call_line" ] && [ "$call_line" -gt "$guard_line" ]
}
