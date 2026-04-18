#!/usr/bin/env bash
# Set up a mock ccm environment for taking static screenshots.
#
# Creates an isolated tmux server with fake projects in various states.
# No Claude Code is started — states are simulated via tmux window options.
#
# Usage:
#   ./scripts/setup-screenshot.sh
#   ./scripts/capture-screenshot.sh
#   tmux -L ccm-ss kill-server

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CCM_ROOT="$(dirname "$SCRIPT_DIR")"
# Pull in the single-source state icon table so this mock stays in
# sync with the live detection-path icons.
# shellcheck source=../lib/state_meta.sh
source "${CCM_ROOT}/lib/state_meta.sh"
CCM_BIN="${CCM_ROOT}/ccm"
SOCKET="ccm-ss"
SESSION="work"
COLS=120
ROWS=35

T() { tmux -L "$SOCKET" "$@"; }

add_project() {
    local name="$1" dir="$2" state="$3" branch="$4" ports="${5:-}"
    # 4-state model (PERMIT/BUSY/IDLE/SHELL). The historical DONE
    # state has been replaced by a cosmetic ✔ marker driven off
    # @ccm_completed_at — pass "COMPLETED" here to simulate a window
    # that just finished. It is shown as IDLE in @ccm_prev_state so
    # the detection rules agree with the marker.
    local icon prev_state
    icon=$(ccm_state_icon "$state")
    case "$state" in
        COMPLETED) prev_state="IDLE" ;;
        *)         prev_state="$state" ;;
    esac

    local win_idx
    win_idx=$(T new-window -P -F "#{window_index}" -t "${SESSION}:" -n "${icon} ${name}")
    local win="${SESSION}:${win_idx}"

    T set-option -wt "$win" @ccm_project "$name"
    T set-option -wt "$win" @ccm_dir "$dir"
    T set-option -wt "$win" @ccm_orig_name "$name"
    T set-option -wt "$win" @ccm_prev_state "$prev_state"
    T set-option -wt "$win" automatic-rename off

    if [[ "$state" == "COMPLETED" ]]; then
        T set-option -wt "$win" @ccm_completed_at "$(date +%s)"
    fi

    # Hook signal file. Only states that correspond to a real hook
    # signal get a file (BUSY / PERMIT); IDLE and the cosmetic
    # COMPLETED have no signal because the Stop hook deletes the
    # file on completion.
    local hook_dir="${TMPDIR:-/tmp}/ccm-$(id -u)/hooks"
    mkdir -p "$hook_dir"
    local dir_hash
    dir_hash=$(printf '%s' "$dir" | md5 -q 2>/dev/null || printf '%s' "$dir" | md5sum | cut -d' ' -f1)
    if [[ "$state" == "BUSY" || "$state" == "PERMIT" ]]; then
        printf '%s %s' "$(date +%s)" "$state" > "${hook_dir}/${dir_hash}"
    else
        rm -f "${hook_dir}/${dir_hash}"
    fi

    # Git branch cache
    local git_cache_dir="${TMPDIR:-/tmp}/ccm-$(id -u)/git-cache"
    mkdir -p "$git_cache_dir"
    printf '%s' "$branch" > "${git_cache_dir}/${dir_hash}"

    # Port cache
    if [[ -n "$ports" ]]; then
        local port_cache_dir="${TMPDIR:-/tmp}/ccm-$(id -u)/port-cache"
        mkdir -p "$port_cache_dir"
        printf '%s' "$ports" > "${port_cache_dir}/${dir_hash}"
    fi

    echo "  ${icon} ${name} (${state}) [${branch}]${ports:+ :${ports}}"
}

# ── Setup ──

T kill-server 2>/dev/null || true
sleep 0.3
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/popup-session"

CONF="/tmp/ccm-ss-tmux.conf"
cat > "$CONF" << 'EOF'
set -g base-index 1
set -g allow-rename off
set -g @ccm-auto-restore off
set -g @ccm-status-line 1
EOF

T -f "$CONF" new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS"

# Mock mode: prevent inject-status from overwriting mock states.
# @ccm-mock-state is readable by #(...) processes (unlike set-environment).
T set -g @ccm-mock-state 1
T set-environment -g CCM_MOCK_STATE 1
T send-keys -t "${SESSION}:1" "export CCM_MOCK_STATE=1" Enter
sleep 0.3

T send-keys -t "${SESSION}:1" "bash $CCM_ROOT/ccm.tmux" Enter
sleep 3
T send-keys -t "${SESSION}:1" "clear" Enter
sleep 0.5

echo "Creating mock projects..."
echo ""

add_project "api-gateway"    "$HOME/code/api-gateway"    "BUSY"      "feat/rate-limiting"  "8080"
add_project "web-dashboard"  "$HOME/code/web-dashboard"  "IDLE"      "main"                "3000"
add_project "auth-service"   "$HOME/code/auth-service"   "COMPLETED" "fix/token-refresh"   "9090"
add_project "ml-pipeline"    "$HOME/code/ml-pipeline"    "PERMIT"    "main*"               ""
add_project "mobile-app"     "$HOME/code/mobile-app"     "IDLE"      "release/2.1"         "8081"
add_project "docs-site"      "$HOME/code/docs-site"      "SHELL"     "main"                "4321"

T select-window -t "${SESSION}:1"

echo ""
echo "Mock environment ready on tmux server '$SOCKET', session '$SESSION'."
echo ""
echo "To take screenshots: ./scripts/capture-screenshot.sh"
echo "To clean up:         tmux -L $SOCKET kill-server"
