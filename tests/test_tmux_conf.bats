#!/usr/bin/env bats
# Tests for tmux.conf reading/writing helpers (lib/common.sh)
#
# The bug these guard against: one ccm option name is a prefix of
# another (`@ccm-status-line` vs `@ccm-status-line-position`), so any
# substring match silently damages the user's own config file.

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

setup() {
    MOCK_DIR="$(mktemp -d)"
    export HOME="$MOCK_DIR"
    CONF="${MOCK_DIR}/tmux.conf"
    source "${CCM_ROOT}/lib/common.sh"
}

teardown() {
    rm -rf "$MOCK_DIR"
}

write_conf() {
    cat > "$CONF" <<'EOF'
set -g mouse on
set -g @ccm-status-line 1
set -g @ccm-status-line-hide-shell on
set -g @ccm-status-line-position left
set -g @ccm-notify permit
run '~/.tmux/plugins/tpm/tpm'
EOF
}

# --- writing ---------------------------------------------------------

@test "saving an option leaves options whose name extends it alone" {
    write_conf
    _ccm_save_tmux_conf "set -g @ccm-status-line 2" "$CONF"

    grep -q '@ccm-status-line-hide-shell on' "$CONF" \
        || { echo "hide-shell line was deleted:"; cat "$CONF"; return 1; }
    grep -q '@ccm-status-line-position left' "$CONF" \
        || { echo "position line was deleted:"; cat "$CONF"; return 1; }
}

@test "saving an option replaces its own previous line" {
    write_conf
    _ccm_save_tmux_conf "set -g @ccm-status-line 2" "$CONF"

    run grep -c '@ccm-status-line ' "$CONF"
    [[ "$output" -eq 1 ]] || { echo "expected 1 line, got $output"; return 1; }
    grep -q 'set -g @ccm-status-line 2' "$CONF" || return 1
}

@test "saving an option leaves unrelated settings alone" {
    write_conf
    _ccm_save_tmux_conf "set -g @ccm-status-line 2" "$CONF"

    grep -q 'set -g mouse on' "$CONF" || return 1
    grep -q '@ccm-notify permit' "$CONF" || return 1
}

@test "saving a child option leaves the parent alone" {
    write_conf
    _ccm_save_tmux_conf "set -g @ccm-status-line-position right" "$CONF"

    grep -q 'set -g @ccm-status-line 1' "$CONF" \
        || { echo "parent line was deleted:"; cat "$CONF"; return 1; }
    grep -q '@ccm-status-line-position right' "$CONF" || return 1
}

@test "the new setting is placed before the TPM load line" {
    write_conf
    _ccm_save_tmux_conf "set -g @ccm-auto-restore on" "$CONF"

    local setting_at tpm_at
    setting_at=$(grep -n '@ccm-auto-restore' "$CONF" | head -1 | cut -d: -f1)
    tpm_at=$(grep -n 'tpm/tpm' "$CONF" | head -1 | cut -d: -f1)
    [[ "$setting_at" -lt "$tpm_at" ]] \
        || { echo "setting at $setting_at, tpm at $tpm_at"; return 1; }
}

@test "rewriting the config keeps its permissions" {
    write_conf
    chmod 644 "$CONF"
    _ccm_save_tmux_conf "set -g @ccm-status-line 2" "$CONF"

    run stat -f '%Lp' "$CONF"
    [[ "$output" == "644" ]] || { echo "permissions became $output"; return 1; }
}

@test "a config without ccm or TPM lines gets the setting appended" {
    printf 'set -g mouse on\n' > "$CONF"
    _ccm_save_tmux_conf "set -g @ccm-status-line 2" "$CONF"

    run tail -1 "$CONF"
    [[ "$output" == "set -g @ccm-status-line 2" ]] || return 1
}

# --- reading ---------------------------------------------------------

@test "reading an option returns its own value, not a longer option's" {
    write_conf
    run _ccm_conf_option_value '@ccm-status-line' "$CONF"
    [[ "$output" == "1" ]] || { echo "got: [$output]"; return 1; }
}

@test "reading an option is not confused by several matching lines" {
    write_conf
    # The substring bug emitted one value per matching line, which the
    # wizard then rendered as its current mode.
    run _ccm_conf_option_value '@ccm-status-line' "$CONF"
    [[ "${#lines[@]}" -eq 1 ]] \
        || { echo "expected 1 value, got ${#lines[@]}: [$output]"; return 1; }
}

@test "reading a child option returns the child's value" {
    write_conf
    run _ccm_conf_option_value '@ccm-status-line-position' "$CONF"
    [[ "$output" == "left" ]] || { echo "got: [$output]"; return 1; }
}

@test "reading an option present only as a longer name yields nothing" {
    printf 'set -g @ccm-status-line-position left\n' > "$CONF"
    run _ccm_conf_option_value '@ccm-status-line' "$CONF"
    [[ -z "$output" ]] || { echo "got: [$output]"; return 1; }
}

@test "commented-out settings are not read as the current value" {
    printf '# set -g @ccm-status-line 0\nset -g @ccm-status-line 2\n' > "$CONF"
    run _ccm_conf_option_value '@ccm-status-line' "$CONF"
    [[ "$output" == "2" ]] || { echo "got: [$output]"; return 1; }
}

@test "the last of several settings for one option wins, as tmux does" {
    printf 'set -g @ccm-status-line 0\nset -g @ccm-status-line 2\n' > "$CONF"
    run _ccm_conf_option_value '@ccm-status-line' "$CONF"
    [[ "$output" == "2" ]] || { echo "got: [$output]"; return 1; }
}

@test "a missing config file reads as no value, not an error" {
    run _ccm_conf_option_value '@ccm-status-line' "${MOCK_DIR}/absent.conf"
    [[ "$status" -eq 0 ]] || { echo "status $status"; return 1; }
    [[ -z "$output" ]] || { echo "got: [$output]"; return 1; }
}
