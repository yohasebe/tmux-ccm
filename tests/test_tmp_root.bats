#!/usr/bin/env bats
load helpers/tmux_guard.bash
# The per-user temp root must be a directory owned by the user and closed to others.
# Checked by lib/ccm_tmp_root.sh (CLI and hooks); the Python twin is
# covered in tests/test_tmp_root.py with the same cases.

setup() {
    CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
    source "$CCM_ROOT/lib/ccm_tmp_root.sh"
    BASE="$BATS_TEST_TMPDIR/base"
    mkdir -p "$BASE"
}

mode_of() {
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

@test "a new root is created closed to others" {
    run ccm_secure_tmp_root "$BASE/ccm-1"
    [ "$status" -eq 0 ]
    [ "$(mode_of "$BASE/ccm-1")" = 700 ]
}

@test "an existing open root is closed" {
    mkdir -m 755 "$BASE/ccm-1"
    run ccm_secure_tmp_root "$BASE/ccm-1"
    [ "$status" -eq 0 ]
    [ "$(mode_of "$BASE/ccm-1")" = 700 ]
}

@test "a symlink or a file is refused" {
    mkdir "$BASE/elsewhere"
    ln -s "$BASE/elsewhere" "$BASE/link"
    : > "$BASE/file"
    run ccm_secure_tmp_root "$BASE/link"
    [ "$status" -ne 0 ]
    run ccm_secure_tmp_root "$BASE/file"
    [ "$status" -ne 0 ]
    run ccm_secure_tmp_root ""
    [ "$status" -ne 0 ]
}

@test "ccm refuses to run with an untrusted temp root" {
    mkdir "$BASE/elsewhere"
    ln -s "$BASE/elsewhere" "$BASE/link"
    run env CCM_TMP_DIR="$BASE/link" CCM_DATA_DIR="$BASE/data" bash -c \
        "source '$CCM_ROOT/lib/common.sh'; ccm_init_dirs"
    [ "$status" -ne 0 ]
    [[ "$output" == *"Refusing to use"* ]]
    [ -z "$(ls "$BASE/elsewhere")" ]
}

@test "a hook writes nothing under an untrusted temp root and still exits 0" {
    mkdir "$BASE/elsewhere"
    ln -s "$BASE/elsewhere" "$BASE/ccm-$UID"
    run env TMPDIR="$BASE" bash -c \
        "source '$CCM_ROOT/hooks/lib.sh'; ccm_hook_init <<< '{}'; echo reached"
    [ "$status" -eq 0 ]
    [[ "$output" != *reached* ]]
    [ -z "$(ls "$BASE/elsewhere")" ]
}

@test "a root of ours others could write is moved aside unopened and made afresh" {
    for m in 777 775 770; do
        mkdir "$BASE/ccm-$m"; chmod "$m" "$BASE/ccm-$m"
        ln -s "$BASE/elsewhere" "$BASE/ccm-$m/planted"
        run ccm_secure_tmp_root "$BASE/ccm-$m"
        [ "$status" -eq 0 ]
        [ "$(mode_of "$BASE/ccm-$m")" = 700 ]
        [ -z "$(ls -A "$BASE/ccm-$m")" ]
        aside=("$BASE/ccm-$m".untrusted-*)
        [ -L "${aside[0]}/planted" ]          # kept, not deleted
    done
}

@test "a failed permission check refuses the root without changing its mode" {
    mkdir "$BASE/ccm-1"
    chmod 777 "$BASE/ccm-1"
    ls() { return 1; }
    run ccm_secure_tmp_root "$BASE/ccm-1"
    [ "$status" -ne 0 ]
    [ "$(mode_of "$BASE/ccm-1")" = 777 ]
}

@test "a trailing slash or dot does not hide a symlink" {
    mkdir "$BASE/elsewhere"
    ln -s "$BASE/elsewhere" "$BASE/link"
    for p in "$BASE/link/" "$BASE/link//" "$BASE/link/." "$BASE/link/./"; do
        run ccm_secure_tmp_root "$p"
        [ "$status" -ne 0 ]
    done
    run ccm_secure_tmp_root "$BASE/ccm-1/"
    [ "$status" -eq 0 ]
}

# An open root with a link planted where ccm writes; the target must
# come through unchanged.
plant() {
    ROOT="$BASE/ccm-$UID"
    mkdir -p "$ROOT"; chmod 777 "$ROOT"
    VICTIM="$BASE/victim"
    printf 'KEEP' > "$VICTIM"
    ln -s "$VICTIM" "$ROOT/$1"
}

@test "the status poll gate writes nothing under an untrusted root" {
    plant reconcile-stamp
    run env CCM_TMP_DIR="$ROOT" CCM_DATA_DIR="$BASE/data" bash -c \
        "source '$CCM_ROOT/lib/common.sh'; _ccm_should_reconcile"
    [ "$(cat "$VICTIM")" = KEEP ]
}

@test "the resize hook writes nothing under an untrusted root" {
    plant resize-stamp
    # A copy with no ccm beside it, so nothing renders either way.
    mkdir -p "$BASE/plugin/lib"
    cp "$CCM_ROOT/lib/on-resize.sh" "$CCM_ROOT/lib/ccm_tmp_root.sh" "$BASE/plugin/lib/"
    run env CCM_TMP_DIR="$ROOT" CCM_RESIZE_SETTLE=0 CCM_BIN=/nonexistent \
        bash "$BASE/plugin/lib/on-resize.sh"
    [ "$(cat "$VICTIM")" = KEEP ]
    [ "$status" -eq 0 ]
}

@test "the popup session record writes nothing under an untrusted root" {
    plant popup-session
    run env CCM_TMP_DIR="$ROOT" bash "$CCM_ROOT/lib/popup-session.sh" main
    [ "$status" -eq 0 ]
    [ "$(cat "$VICTIM")" = KEEP ]
    # and writes it where the root is trusted
    run env CCM_TMP_DIR="$BASE/good" bash "$CCM_ROOT/lib/popup-session.sh" main
    [ "$(cat "$BASE/good/popup-session")" = main ]
}

@test "a sidekick resolving event writes nothing under an untrusted root" {
    command -v jq >/dev/null || skip "jq not installed"
    plant attention-unused
    mkdir -p "$ROOT/attention"
    printf '{"state":"waiting"}' > "$ROOT/attention/%1.json"
    ln -s "$VICTIM" "$ROOT/attention/%1.json.tmp"
    run env TMPDIR="$BASE" TMUX_PANE=%1 bash "$CCM_ROOT/hooks/sidekick-attention.sh" kimi \
        <<< '{"hook_event_name":"Stop"}'
    [ "$status" -eq 0 ]
    [ "$(cat "$VICTIM")" = KEEP ]
}

@test "the sidekick resolving event still works under a trusted root" {
    command -v jq >/dev/null || skip "jq not installed"
    (umask 077; mkdir -p "$BASE/ccm-$UID/attention")
    printf '{"state":"waiting"}' > "$BASE/ccm-$UID/attention/%1.json"
    run env TMPDIR="$BASE" TMUX_PANE=%1 bash "$CCM_ROOT/hooks/sidekick-attention.sh" kimi \
        <<< '{"hook_event_name":"Stop"}'
    [ "$(jq -r .state "$BASE/ccm-$UID/attention/%1.json")" = resolved ]
}
