#!/usr/bin/env bash
# ccm hook: Claude Code Stop / StopFailure → clear BUSY signal
# Deletes the signal file so detection transitions to IDLE.
# Installed by: ccm setup-hooks
set -euo pipefail

_ccm_src="${BASH_SOURCE[0]}"; [[ "$_ccm_src" == */* ]] || _ccm_src="./$_ccm_src"
SCRIPT_DIR="$(cd "${_ccm_src%/*}" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

ccm_hook_init || exit 0

# Background work Claude Code reports outstanding (see below), read
# first so the stop event carries it for the `(bg)` note.
if [[ -n "${_CCM_FIELDS_READ:-}" ]]; then
    bg_remaining="$HOOK_BG_REMAINING"
    bg_kinds="$HOOK_BG_KINDS"
else
    bg_remaining=$(printf '%s' "$INPUT" | \
        jq -r "$_CCM_BG_JQ_DEFS"' ccm_bg_counted' 2>/dev/null) || bg_remaining=0
    bg_kinds=$(printf '%s' "$INPUT" | \
        jq -r "$_CCM_BG_JQ_DEFS"' ccm_bg_kinds' 2>/dev/null) || bg_kinds=""
fi
bg_remaining=${bg_remaining:-0}

ccm_append_event "$HOOK_DIR" "$KEY" "stop" "$bg_remaining" "$bg_kinds"
# The same count kept apart from the event log, whose tail the reader
# may no longer reach once a turn interrupted with Esc has logged many
# events and no Stop (`read_stop_background`).
printf '%s\n' "$bg_remaining" > "$HOOK_DIR/$KEY.stop-bg" 2>/dev/null || true
# Delete the BUSY signal file (and .busy file) to clear the BUSY state.
# The detection layer will transition to IDLE on the next scan.
rm -f "$HOOK_DIR/$KEY" "$HOOK_DIR/$KEY.busy"

# Schedule a COMPLETED notification after a short grace period so
# Stop events fired at multi-turn tool boundaries do not produce a
# premature alert. If the next PreToolUse / UserPromptSubmit arrives
# within the grace window, it cancels the pending notification. See
# `_ccm_schedule_completed_notify` in lib.sh for the rationale.
#
# Suppress entirely when Claude Code reports outstanding background
# work in the payload. The Stop hook fires at the end of every turn
# — but a non-empty `background_tasks` (e.g. `/bg` dispatch, an
# async tool still running) or `session_crons` (e.g. a `/loop`
# scheduled to wake later) means the user's intent is NOT yet
# "done". Without this guard, every iteration of a `/goal` or
# `/loop` would surface a COMPLETED desktop alert and train the
# user to ignore the signal. Schema: Claude Code 2.1.145+ emits
# these as JSON arrays in the Stop / SubagentStop payload; on
# older Claude Code versions the fields are absent and `// []`
# defaults the length to 0, preserving the legacy behaviour.
if [[ "$bg_remaining" == "0" ]]; then
    project=$(ccm_hook_resolve_project "$CWD")
    if [[ -n "$project" ]]; then
        _ccm_schedule_completed_notify "$HOOK_DIR" "$KEY" "$CWD" "$project"
    fi
fi

# What that count is made of, for the dashboard: each counted task's
# kind, label (its description, unless it looks like it carries a
# credential) and when it started, carried over from the previous list
# by the task's id, so the age reads how long it has been left running.
# A task new to the list is looked up once in the transcript: the
# first line naming its id, normally the launch result, gives the
# start; failing that it counts from this Stop. Display only, so it runs last, after the signal and the
# notification above, and a failure here is ignored. The lookup reads
# the transcript at most once, stops when every id is found, and
# skips a transcript over 1 GiB.
bg_tasks="$HOOK_DIR/$KEY.stop-bg-tasks"
if [[ "$bg_remaining" =~ ^[1-9][0-9]*$ ]]; then
    bg_now=${EPOCHSECONDS:-$(date +%s)}
    bg_prev=$(cat "$bg_tasks" 2>/dev/null) || bg_prev=""
    bg_starts="{}"
    bg_new=$(printf '%s' "$INPUT" | jq -r --arg prev "$bg_prev" \
        "$_CCM_BG_JQ_DEFS"' (.transcript_path // "" | ccm_bg_text),
            (ccm_bg_task_list(-1; $prev; {})[] | select(.since == -1) | .id
             | select(test("^[A-Za-z0-9_-]{1,64}$")))' \
        2>/dev/null) || bg_new=""
    bg_transcript=${bg_new%%$'\n'*}
    bg_ids=()
    if [[ "$bg_new" == *$'\n'* ]]; then
        while IFS= read -r bg_id; do
            [[ -n "$bg_id" ]] && bg_ids+=("$bg_id")
        done <<<"${bg_new#*$'\n'}"
    fi
    if [[ ${#bg_ids[@]} -gt 0 && -f "$bg_transcript" && -r "$bg_transcript" ]] \
            && (( $(wc -c <"$bg_transcript" 2>/dev/null || echo 0) <= 1073741824 )); then
        # One pass in perl: a line naming a wanted id settles that id
        # once its top-level timestamp reads as a time; settled ids
        # leave the pattern, and the pass ends when none are left.
        # Launch results are a few KiB: lines over 64 KiB are skipped
        # without being held, and parsing stops after 1 MiB of
        # candidate lines.
        bg_starts=$({ perl -e '
                use strict;
                eval { require JSON::PP; require Time::Local; 1 } or exit 0;
                my %w = map { $_ => 1 } @ARGV; @ARGV = ();
                my ($re, $budget) = (undef, 1048576);
                my $mk = sub { my $alt = join "|", map { quotemeta } sort keys %w; $re = qr/"($alt)"/ };
                $mk->();
                my $see = sub {
                    my ($l) = @_;
                    return 1 if length($l) > 65536;
                    my @m = $l =~ /$re/g or return 1;
                    return 1 if index($l, q("timestamp")) < 0;
                    return 0 if ($budget -= length($l)) < 0;
                    my $d = eval { JSON::PP::decode_json($l) };
                    return 1 unless ref $d eq "HASH" && defined $d->{timestamp} && !ref $d->{timestamp}
                        && $d->{timestamp} =~ /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?Z\z/;
                    my $t = eval { Time::Local::timegm($6, $5, $4, $3, $2 - 1, $1) };
                    return 1 unless defined $t;
                    for my $id (@m) { print "$id\t$t\n" if delete $w{$id}; }
                    return 0 unless %w;
                    $mk->();
                    return 1;
                };
                # Read in blocks so a line past the limit is dropped as it
                # streams by, never held whole.
                my ($buf, $skip) = ("", 0);
                READ: while (read(STDIN, my $chunk, 65536)) {
                    $buf .= $chunk;
                    while ((my $i = index($buf, "\n")) >= 0) {
                        my $l = substr($buf, 0, $i + 1, "");
                        if ($skip) { $skip = 0; next; }
                        last READ unless $see->($l);
                    }
                    if (length($buf) > 65536) { ($buf, $skip) = ("", 1); }
                }' "${bg_ids[@]}" <"$bg_transcript" 2>/dev/null || true; } \
            | jq -nRc '[inputs | split("\t") | select(length == 2)
                        | {(.[0]): (.[1] | tonumber? // empty)}] | add // {}' \
                2>/dev/null) || bg_starts="{}"
        [[ "$bg_starts" == \{* ]] || bg_starts="{}"
    fi
    if printf '%s' "$INPUT" | jq -c --argjson now "$bg_now" --arg prev "$bg_prev" \
            --argjson starts "$bg_starts" \
            "$_CCM_BG_JQ_DEFS"' {v: 1, tasks: ccm_bg_task_list($now; $prev; $starts)}' \
            > "$bg_tasks.$$" 2>/dev/null; then
        mv -f "$bg_tasks.$$" "$bg_tasks" 2>/dev/null || rm -f "$bg_tasks.$$" 2>/dev/null || true
    else
        rm -f "$bg_tasks.$$" "$bg_tasks" 2>/dev/null || true
    fi
else
    rm -f "$bg_tasks" 2>/dev/null || true
fi
