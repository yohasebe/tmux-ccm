#!/usr/bin/env bats
# Tests for state detection logic (lib/detect.sh)

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

setup() {
    source "${CCM_ROOT}/tests/helpers/mock_tmux.bash"
    setup_mocks

    # Source only the needed libraries (avoid full ccm initialization)
    source "${CCM_ROOT}/lib/common.sh"
    source "${CCM_ROOT}/lib/detect.sh"

    # Override ccm_init_dirs to avoid creating real directories
    ccm_init_dirs() { :; }
    # Override notification to avoid side effects
    ccm_notify() { :; }
    # Override _ccm_session
    _ccm_session() { echo "test-session"; }
    # Reset global caches
    _PS_CACHE=""
    _PANES_CACHE=""
    _WIN_OPTS_CACHE=""
    _SCAN_CACHE_TIME=0
    _CCM_PGID="99999"  # Our own PGID (to be excluded)

    # Override CCM_HOOK_DIR to use temp directory for tests
    CCM_HOOK_DIR="${MOCK_DIR}/hooks"
    mkdir -p "$CCM_HOOK_DIR"

    # Override ccm_expand_path to return path as-is in tests
    ccm_expand_path() { echo "$1"; }
}

teardown() {
    teardown_mocks
}

# ============================================================
# _detect_pane_state tests
# ============================================================

@test "_detect_pane_state: SHELL when no claude process" {
    mock_ps_cache "  100     1   100 bash
  101   100   100 zsh"
    mock_panes_cache "test-session:0	100	%0"

    run _detect_pane_state 100 "%0"
    [[ "$output" == "SHELL" ]]
}

@test "_detect_pane_state: IDLE when claude has no meaningful children" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"

    run _detect_pane_state 100 "%0"
    [[ "$output" == "IDLE" ]]
}

@test "_detect_pane_state: IDLE when claude has only caffeinate children" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 caffeinate"
    mock_panes_cache "test-session:0	100	%0"

    run _detect_pane_state 100 "%0"
    [[ "$output" == "IDLE" ]]
}

@test "_detect_pane_state: BUSY when claude has children and no PERMIT text" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 node"
    mock_panes_cache "test-session:0	100	%0"
    mock_capture_pane "%0" "Processing files...
Building project...
Running tests..."

    run _detect_pane_state 100 "%0"
    [[ "$output" == "BUSY" ]]
}

@test "_detect_pane_state: PERMIT when claude has children and PERMIT text" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 node"
    mock_panes_cache "test-session:0	100	%0"
    mock_capture_pane "%0" "Reading file /path/to/file
Do you want to allow this tool call?
  Yes    No"

    run _detect_pane_state 100 "%0"
    [[ "$output" == "PERMIT" ]]
}

@test "_detect_pane_state: excludes ccm own PGID from children" {
    # Child process 300 has the same PGID as ccm (99999) → should be excluded
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200 99999 node"
    mock_panes_cache "test-session:0	100	%0"

    run _detect_pane_state 100 "%0"
    [[ "$output" == "IDLE" ]]
}

@test "_detect_pane_state: PERMIT detected with 'Esc to cancel' pattern" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 node"
    mock_panes_cache "test-session:0	100	%0"
    mock_capture_pane "%0" "Allow tool call: Edit
  /path/to/file
  Esc to cancel"

    run _detect_pane_state 100 "%0"
    [[ "$output" == "PERMIT" ]]
}

# ============================================================
# ccm_detect_window_state tests (DONE transitions)
# ============================================================

@test "DONE: BUSY→IDLE transition with input prompt triggers DONE" {
    # Raw state will be IDLE (claude exists, no children, input prompt visible)
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	BUSY		myproject	/tmp/test-project"
    # Input prompt visible → IDLE at pane level → DONE at window level
    mock_capture_pane "%0" "Here is the result.

> "
    # Also set for window-level capture (DONE/PERMIT check)
    mock_capture_pane "test-session:0" "Here is the result.

> "

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "DONE" ]]
}

@test "DONE: BUSY→IDLE with PERMIT text but input prompt present → DONE (not PERMIT)" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	BUSY		myproject	/tmp/test-project"
    # Old PERMIT text still on screen, but input prompt at bottom → DONE
    local content="Do you want to allow?
Yes    No
Result: success

> "
    mock_capture_pane "%0" "$content"
    mock_capture_pane "test-session:0" "$content"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "DONE" ]]
}

@test "PERMIT: BUSY→IDLE with PERMIT text and NO input prompt → PERMIT" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	BUSY		myproject	/tmp/test-project"
    # PERMIT text at bottom, no input prompt → PERMIT at both levels
    local content="Allow tool call: Edit
  /path/to/file
Do you want to allow this?
  Yes    No"
    mock_capture_pane "%0" "$content"
    mock_capture_pane "test-session:0" "$content"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "PERMIT" ]]
}

@test "DONE auto-expires after 30 seconds" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    local old_ts=$(( $(date +%s) - 31 ))
    mock_win_opts_cache "test-session:0	IDLE	${old_ts}	myproject	/tmp/test-project"
    mock_capture_pane "%0" "Old output.

> "
    mock_capture_pane "test-session:0" "Old output.

> "

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "IDLE" ]]
}

@test "DONE persists within 30 seconds (no PERMIT text)" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    local recent_ts=$(( $(date +%s) - 10 ))
    mock_win_opts_cache "test-session:0	IDLE	${recent_ts}	myproject	/tmp/test-project"
    local content="Result complete.

> "
    mock_capture_pane "%0" "$content"
    mock_capture_pane "test-session:0" "$content"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "DONE" ]]
}

@test "DONE reverts to PERMIT when PERMIT text appears late" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    local recent_ts=$(( $(date +%s) - 5 ))
    mock_win_opts_cache "test-session:0	IDLE	${recent_ts}	myproject	/tmp/test-project"
    local content="Allow tool call: Edit
  /path/to/file
Do you want to allow this?
  Yes    No"
    mock_capture_pane "%0" "$content"
    mock_capture_pane "test-session:0" "$content"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "PERMIT" ]]
}

@test "DONE flag cleared when state is not IDLE" {
    # Claude is BUSY (has children)
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 node"
    mock_panes_cache "test-session:0	100	%0"
    mock_capture_pane "test-session:0" "Processing..."
    local recent_ts=$(( $(date +%s) - 5 ))
    mock_win_opts_cache "test-session:0	IDLE	${recent_ts}	myproject	/tmp/test-project"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "BUSY" ]]
    # Verify @ccm_done was unset
    local done_val
    done_val=$(mock_get_option "test-session:0" "@ccm_done" || true)
    [[ -z "$done_val" ]]
}

@test "IDLE→IDLE does not trigger DONE" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	IDLE		myproject	/tmp/test-project"
    mock_capture_pane "%0" "Waiting.

> "

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "IDLE" ]]
}

# ============================================================
# _has_children tests
# ============================================================

@test "_has_children: true when child exists" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 node"

    _has_children 200
    # Should return 0 (success/true)
}

@test "_has_children: false when no children" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"

    ! _has_children 200
    # Should return 1 (failure/false)
}

@test "_has_children: excludes ccm own PGID" {
    _CCM_PGID="99999"
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200 99999 ccm-child"

    ! _has_children 200
    # Should return 1 (false) because child has ccm's PGID
}

@test "_has_children: excludes caffeinate (always running)" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 caffeinate
  301   200   200 caffeinate"

    ! _has_children 200
    # Should return 1 (false) — caffeinate is not meaningful work
}

@test "_has_children: true when non-caffeinate child exists alongside caffeinate" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 caffeinate
  400   200   200 node"

    _has_children 200
    # Should return 0 (true) — node is a real child
}

# ============================================================
# _detect_window_state tests (priority)
# ============================================================

@test "_detect_window_state: PERMIT takes priority over BUSY" {
    # pane_pid=100 has claude(200) with child(300) → BUSY
    # pane_pid=110 has claude(210) with child(310) → PERMIT
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 node
  110     1   110 bash
  210   110   110 claude
  310   210   210 node"
    # Panes cache: session:window \t pane_pid \t pane_id
    mock_panes_cache "test-session:0	100	%0
test-session:0	110	%1"
    # First pane (%0): BUSY (no PERMIT text)
    mock_capture_pane "%0" "Processing..."
    # Second pane (%1): PERMIT
    mock_capture_pane "%1" "Do you want to allow this?
  Yes    No"

    run _detect_window_state "test-session:0"
    [[ "$output" == "PERMIT" ]]
}

@test "_detect_window_state: DOWN when no panes" {
    mock_ps_cache ""
    mock_panes_cache ""

    run _detect_window_state "test-session:0"
    [[ "$output" == "DOWN" ]]
}

# ============================================================
# Hook-based detection tests
# ============================================================

@test "HOOK: raw=IDLE + hook=BUSY → BUSY (text generation)" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	IDLE		myproject	/tmp/test-project"
    mock_hook_signal "/tmp/test-project" "$(date +%s) BUSY"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "BUSY" ]]
}

@test "HOOK: raw=IDLE + hook=DONE → DONE (reliable)" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	IDLE		myproject	/tmp/test-project"
    mock_hook_signal "/tmp/test-project" "$(date +%s) DONE"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "DONE" ]]
}

@test "HOOK: raw=PERMIT + hook=BUSY → PERMIT (process tree wins)" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude
  300   200   200 node"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	IDLE		myproject	/tmp/test-project"
    mock_capture_pane "%0" "Do you want to allow?
  Yes    No"
    mock_hook_signal "/tmp/test-project" "$(date +%s) BUSY"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "PERMIT" ]]
}

@test "HOOK: raw=IDLE + hook=BUSY + PERMIT text → PERMIT (permission before tool runs)" {
    # Permission prompt shown BEFORE tool execution → no child processes
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	IDLE		myproject	/tmp/test-project"
    mock_capture_pane "test-session:0" "Bash command
  ls -la
Do you want to proceed?
  1. Yes
  2. No
Esc to cancel"
    mock_hook_signal "/tmp/test-project" "$(date +%s) BUSY"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "PERMIT" ]]
}

@test "HOOK: expired BUSY signal → fallback to IDLE" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	IDLE		myproject	/tmp/test-project"
    local old_ts=$(( $(date +%s) - 301 ))
    mock_hook_signal "/tmp/test-project" "$old_ts BUSY"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "IDLE" ]]
}

@test "HOOK: expired DONE signal → IDLE" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	IDLE		myproject	/tmp/test-project"
    local old_ts=$(( $(date +%s) - 31 ))
    mock_hook_signal "/tmp/test-project" "$old_ts DONE"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "IDLE" ]]
}

@test "HOOK: no hook file → fallback to existing behavior" {
    mock_ps_cache "  100     1   100 bash
  200   100   100 claude"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	IDLE		myproject	/tmp/test-project"
    # No mock_hook_signal → no hook file

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "IDLE" ]]
}

@test "HOOK: raw=SHELL + hook=BUSY → SHELL (contradictory, ignore hook)" {
    mock_ps_cache "  100     1   100 bash"
    mock_panes_cache "test-session:0	100	%0"
    mock_win_opts_cache "test-session:0	SHELL		myproject	/tmp/test-project"
    mock_hook_signal "/tmp/test-project" "$(date +%s) BUSY"

    run ccm_detect_window_state "test-session:0"
    [[ "$output" == "SHELL" ]]
}
