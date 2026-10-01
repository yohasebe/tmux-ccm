#!/usr/bin/env bats
load helpers/tmux_guard.bash

@test "notification options resolve explicit window then global window then global session" {
    local socket window
    socket=$(ccm_test_new_socket)
    window=$(tmux -L "$socket" -f /dev/null new-session -d -P -F '#{window_id}' -s option-check /bin/sh)
    run env PYTHONPATH="$BATS_TEST_DIRNAME/../lib" python3 "$BATS_TEST_DIRNAME/helpers/sidekick_option_probe.py" "$socket" "$window"
    [ "$status" -eq 0 ]
    tmux -L "$socket" -f /dev/null kill-server
}
