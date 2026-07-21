#!/usr/bin/env bash
# Mock framework for tmux and related commands
# Used by bats tests to simulate tmux behavior without a real tmux server

MOCK_DIR=""

setup_mocks() {
    MOCK_DIR="$(mktemp -d)"
    export MOCK_STATE_DIR="${MOCK_DIR}/state"
    mkdir -p "${MOCK_STATE_DIR}/options" "${MOCK_STATE_DIR}/capture" "${MOCK_DIR}/bin"

    # Create mock tmux command
    cat > "${MOCK_DIR}/bin/tmux" << 'MOCK_SCRIPT'
#!/usr/bin/env bash
# Mock tmux command — dispatches based on subcommand

_mock_capture_pane() {
    local target=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -t) target="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    target="${target//:/___}"
    local capfile="${MOCK_STATE_DIR}/capture/${target}"
    [[ -f "$capfile" ]] && cat "$capfile"
}

_mock_show_option() {
    local target="" key="" global=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -wt|-t) target="$2"; shift 2 ;;
            -gqv|-gv|-qv) global=1; shift ;;
            -g|-q) global=1; shift ;;
            -*) shift ;;
            *) key="$1"; shift ;;
        esac
    done
    local safe_target="${target//:/___}"
    local file
    if [[ $global -eq 1 ]]; then
        file="${MOCK_STATE_DIR}/options/global/${key}"
    else
        file="${MOCK_STATE_DIR}/options/${safe_target}/${key}"
    fi
    # Match real tmux `-q` semantics: unset options emit empty output
    # with exit 0 rather than a non-zero failure. Returning 1 here
    # leaks through `x=$(tmux show-option ...)` command substitutions
    # and trips bats' implicit error checking in callers.
    [[ -f "$file" ]] && cat "$file"
    return 0
}

_mock_set_option() {
    local target="" key="" value="" unset_flag=0 global=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -wt|-t) target="$2"; shift 2 ;;
            -g) global=1; shift ;;
            -u) unset_flag=1; shift ;;
            -*) shift ;;
            *)
                if [[ -z "$key" ]]; then
                    key="$1"
                else
                    value="$1"
                fi
                shift
                ;;
        esac
    done
    local safe_target="${target//:/___}"
    local dir
    if [[ $global -eq 1 ]]; then
        dir="${MOCK_STATE_DIR}/options/global"
    else
        dir="${MOCK_STATE_DIR}/options/${safe_target}"
    fi
    mkdir -p "$dir"
    if [[ $unset_flag -eq 1 ]]; then
        rm -f "${dir}/${key}"
    else
        printf '%s' "$value" > "${dir}/${key}"
    fi
}

cmd="$1"
shift
case "$cmd" in
    capture-pane)   _mock_capture_pane "$@" ;;
    list-panes)     [[ -f "${MOCK_STATE_DIR}/panes" ]] && cat "${MOCK_STATE_DIR}/panes" ;;
    list-windows)   [[ -f "${MOCK_STATE_DIR}/windows" ]] && cat "${MOCK_STATE_DIR}/windows" ;;
    list-sessions)  [[ -f "${MOCK_STATE_DIR}/sessions" ]] && cat "${MOCK_STATE_DIR}/sessions" ;;
    show-option)    _mock_show_option "$@" ;;
    set-option|set) _mock_set_option "$@" ;;
    display-message)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                -t) shift 2 ;;
                -p) shift; echo "${1:-}"; shift ;;
                *) shift ;;
            esac
        done
        ;;
    # `exit`, NOT `return`: this case sits at the top level of the
    # EXECUTED mock script, where `return` is a bash error ("can only
    # be used from a function"). The bug hid for months because no
    # bats test drove ccm_write_signal through a matching window, so
    # rename-window / refresh-client (the catch-all) were never hit.
    has-session)    exit 0 ;;
    rename-window)  exit 0 ;;
    *)              exit 0 ;;
esac
MOCK_SCRIPT
    chmod +x "${MOCK_DIR}/bin/tmux"
    export PATH="${MOCK_DIR}/bin:${PATH}"
}

teardown_mocks() {
    [[ -n "$MOCK_DIR" && -d "$MOCK_DIR" ]] && rm -rf "$MOCK_DIR"
}

# Helper: set capture-pane output for a target
mock_capture_pane() {
    local target="$1"
    local content="$2"
    local safe_target="${target//:/___}"
    printf '%s' "$content" > "${MOCK_STATE_DIR}/capture/${safe_target}"
}

# Helper: set a tmux window option
mock_set_option() {
    local target="$1"
    local key="$2"
    local value="$3"
    local safe_target="${target//:/___}"
    mkdir -p "${MOCK_STATE_DIR}/options/${safe_target}"
    printf '%s' "$value" > "${MOCK_STATE_DIR}/options/${safe_target}/${key}"
}

# Helper: set a global tmux option
mock_set_global_option() {
    local key="$1"
    local value="$2"
    mkdir -p "${MOCK_STATE_DIR}/options/global"
    printf '%s' "$value" > "${MOCK_STATE_DIR}/options/global/${key}"
}

# Helper: get a tmux window option (for assertions)
mock_get_option() {
    local target="$1"
    local key="$2"
    local safe_target="${target//:/___}"
    local file="${MOCK_STATE_DIR}/options/${safe_target}/${key}"
    [[ -f "$file" ]] && cat "$file"
}

# Helper: set PS cache directly (avoid mocking ps command)
mock_ps_cache() {
    _PS_CACHE="$1"
    _SCAN_CACHE_TIME=$(date +%s)
}

# Helper: set panes cache
mock_panes_cache() {
    _PANES_CACHE="$1"
}

# Helper: set win opts cache
mock_win_opts_cache() {
    _WIN_OPTS_CACHE="$1"
}

# Helper: create a mock hook signal file
# Usage: mock_hook_signal "/path/to/dir" "1711190400 BUSY"
mock_hook_signal() {
    local dir="$1"
    local content="$2"
    local key
    key=$(_ccm_md5 "$dir") || return 1
    mkdir -p "${CCM_HOOK_DIR}"
    printf '%s' "$content" > "${CCM_HOOK_DIR}/${key}"
}
