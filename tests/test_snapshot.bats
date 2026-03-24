#!/usr/bin/env bats
# Tests for snapshot save/load logic (lib/snapshot.sh)

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

setup() {
    source "${CCM_ROOT}/tests/helpers/mock_tmux.bash"
    setup_mocks

    source "${CCM_ROOT}/lib/common.sh"
    source "${CCM_ROOT}/lib/detect.sh"
    source "${CCM_ROOT}/lib/session.sh"
    source "${CCM_ROOT}/lib/snapshot.sh"

    # Override functions to avoid side effects
    ccm_init_dirs() { :; }
    ccm_notify() { :; }
    _ccm_session() { echo "test-session"; }
    ccm_auto_start_claude() { :; }

    # Use temp dir for snapshots
    CCM_SNAPSHOT_DIR="$(mktemp -d)"
    CCM_TMP_DIR="$(mktemp -d)"

    _PS_CACHE=""
    _PANES_CACHE=""
    _WIN_OPTS_CACHE=""
    _SCAN_CACHE_TIME=0
    _CCM_PGID="99999"
}

teardown() {
    teardown_mocks
    [[ -n "$CCM_SNAPSHOT_DIR" ]] && rm -rf "$CCM_SNAPSHOT_DIR"
    [[ -n "$CCM_TMP_DIR" ]] && rm -rf "$CCM_TMP_DIR"
}

# ============================================================
# Snapshot save validation
# ============================================================

@test "snapshot save: skips entries with empty project" {
    # Mock ccm_list_windows to return a line with empty project
    ccm_list_windows() {
        printf '0\twindow0\tvalid-project\t/tmp/valid\n'
        printf '1\twindow1\t\t/tmp/empty-project\n'
    }

    ccm_snapshot_save "test-snap"

    local count
    count=$(jq '.projects | length' "${CCM_SNAPSHOT_DIR}/test-snap.json")
    [[ "$count" == "1" ]]

    local name
    name=$(jq -r '.projects[0].name' "${CCM_SNAPSHOT_DIR}/test-snap.json")
    [[ "$name" == "valid-project" ]]
}

@test "snapshot save: skips entries with empty dir" {
    ccm_list_windows() {
        printf '0\twindow0\tproject-a\t/tmp/dir-a\n'
        printf '1\twindow1\tproject-b\t\n'
    }

    ccm_snapshot_save "test-snap"

    local count
    count=$(jq '.projects | length' "${CCM_SNAPSHOT_DIR}/test-snap.json")
    [[ "$count" == "1" ]]
}

# ============================================================
# Snapshot load validation
# ============================================================

@test "snapshot load: skips null name entries" {
    cat > "${CCM_SNAPSHOT_DIR}/test-snap.json" << 'EOF'
{
    "version": 1,
    "name": "test-snap",
    "created": "2026-03-23T00:00:00+0000",
    "projects": [
        {"name": null, "dir": null, "auto_start_claude": true},
        {"name": "valid", "dir": "/tmp", "auto_start_claude": true}
    ]
}
EOF

    # Track which projects ccm_add is called with
    local added_projects=()
    ccm_add() { added_projects+=("$2"); }
    ccm_project_exists() { return 1; }
    ccm_expand_path() { echo "$1"; }

    ccm_snapshot_load "test-snap"

    [[ "${#added_projects[@]}" == "1" ]]
    [[ "${added_projects[0]}" == "valid" ]]
}

@test "snapshot load: skips empty string entries" {
    cat > "${CCM_SNAPSHOT_DIR}/test-snap.json" << 'EOF'
{
    "version": 1,
    "name": "test-snap",
    "created": "2026-03-23T00:00:00+0000",
    "projects": [
        {"name": "", "dir": "", "auto_start_claude": true},
        {"name": "good", "dir": "/tmp", "auto_start_claude": true}
    ]
}
EOF

    local added_projects=()
    ccm_add() { added_projects+=("$2"); }
    ccm_project_exists() { return 1; }
    ccm_expand_path() { echo "$1"; }

    ccm_snapshot_load "test-snap"

    [[ "${#added_projects[@]}" == "1" ]]
    [[ "${added_projects[0]}" == "good" ]]
}

@test "snapshot load: _CCM_LOADING_SNAPSHOT suppresses autosave in ccm_add" {
    # Verify that during load, ccm_add does NOT trigger autosave
    local autosave_called=0
    # Save original
    local orig_save="$(declare -f ccm_snapshot_save)"

    cat > "${CCM_SNAPSHOT_DIR}/test-snap.json" << 'EOF'
{
    "version": 1,
    "name": "test-snap",
    "created": "2026-03-23T00:00:00+0000",
    "projects": [
        {"name": "proj1", "dir": "/tmp", "auto_start_claude": false}
    ]
}
EOF

    # Override ccm_add to check the flag
    ccm_add() {
        if [[ -n "${_CCM_LOADING_SNAPSHOT:-}" ]]; then
            : # Flag is set, autosave should be suppressed
        else
            autosave_called=1
        fi
    }
    ccm_project_exists() { return 1; }
    ccm_expand_path() { echo "$1"; }
    # Suppress the post-load autosave
    ccm_snapshot_save() { :; }

    ccm_snapshot_load "test-snap"

    [[ "$autosave_called" == "0" ]]
}

@test "snapshot load: skips non-existent directories" {
    cat > "${CCM_SNAPSHOT_DIR}/test-snap.json" << 'EOF'
{
    "version": 1,
    "name": "test-snap",
    "created": "2026-03-23T00:00:00+0000",
    "projects": [
        {"name": "missing", "dir": "/nonexistent/path/12345", "auto_start_claude": true},
        {"name": "exists", "dir": "/tmp", "auto_start_claude": true}
    ]
}
EOF

    local added_projects=()
    ccm_add() { added_projects+=("$2"); }
    ccm_project_exists() { return 1; }
    ccm_expand_path() { echo "$1"; }
    ccm_snapshot_save() { :; }

    ccm_snapshot_load "test-snap"

    [[ "${#added_projects[@]}" == "1" ]]
    [[ "${added_projects[0]}" == "exists" ]]
}
