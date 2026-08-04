#!/usr/bin/env bash
# sidekick-attention.sh <agent> — attention-marker adapter for a
# sidekick CLI's own hook system (NOT a Claude Code hook).
#
# Installed into the sidekick's hook config by
# `ccm setup-sidekick-hooks <agent>` (Kimi: `[[hooks]]` entries in
# ~/.kimi-code/config.toml). The sidekick invokes it with the event
# JSON on stdin; it maintains ONE marker file per tmux pane under
# $TMPDIR/ccm-$UID/attention/ so ccm (and any other local consumer,
# e.g. ringi) can see that a sidekick is waiting for a decision —
# without anyone parsing the sidekick's screen.
#
# Marker contract v1 (single-line JSON, see ccm_constants.py):
#   waiting  → written on the CLI's permission-request event
#   resolved → the SAME file overwritten (never deleted here) when the
#              wait ends: resolution, interrupt, turn end, session end.
#              Deletion is the reader's job (ccm GCs), because a
#              vanished file cannot be told apart from a stale one —
#              a consumer that sees `resolved` KNOWS the wait ended.
#
# Fail-quiet by design: the sidekick's hook runners are fail-open with
# short timeouts, and a broken marker must never cost the user their
# sidekick. Anything unexpected → exit 0 with no artefact.

set -o pipefail

AGENT="${1:-}"
[[ -n "$AGENT" ]] || exit 0
# Not inside tmux → no pane to key the marker by, and no ccm to read
# it. (The $TMUX_PANE bridge is the same one CCM_IGNORE rides.)
[[ -n "${TMUX_PANE:-}" ]] || exit 0

ATTENTION_DIR="${TMPDIR:-/tmp}/ccm-${UID}/attention"
MARKER="${ATTENTION_DIR}/${TMUX_PANE}.json"

INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0

# One jq pass for every field we consume. Kimi's payload field names
# are snake_case and PascalCase event values ("PermissionRequest") —
# read the common dual-case forms so another CLI's adapter can reuse
# this script unchanged if its payload is close enough.
EVENT=$(printf '%s' "$INPUT" | jq -r '.hook_event_name // .hookEventName // empty' 2>/dev/null)
[[ -n "$EVENT" ]] || exit 0
SESSION=$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionId // empty' 2>/dev/null)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // .toolName // empty' 2>/dev/null)
# One-line summary: tool name plus its most descriptive input field,
# hard-capped — this string crosses into other tools' UIs (ccm
# notification, ringi's Watch face), so it is data, never markup.
SUMMARY=$(printf '%s' "$INPUT" | jq -r '
    [ (.tool_name // .toolName // empty),
      (.tool_input.command // .tool_input.file_path
        // .toolInput.command // .toolInput.file_path // empty)
    ] | map(select(. != null and . != "")) | join(": ")' 2>/dev/null | head -c 160)
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

case "$EVENT" in
    PermissionRequest|permission_request)
        mkdir -p "$ATTENTION_DIR" 2>/dev/null || exit 0
        jq -cn \
            --arg agent "$AGENT" --arg id "${SESSION:-unknown}-$(date +%s)" \
            --arg cwd "$CWD" --arg ts "$NOW" --arg session "$SESSION" \
            --arg summary "$SUMMARY" --arg pane "$TMUX_PANE" --arg tool "$TOOL" \
            '{agent:$agent, state:"waiting", id:$id, cwd:$cwd, ts:$ts,
              session:$session, summary:$summary, pane:$pane, tool:$tool}' \
            > "$MARKER" 2>/dev/null || exit 0

        # Desktop notification, gated by the same toggle the display
        # honours. Fires once per dialog (the event itself is the
        # dedup), so no marker-window bookkeeping is needed here.
        # `-sender` is deliberately absent (silent-drop trap).
        if [[ "$(tmux show-option -gqv @ccm-sidekick-attention 2>/dev/null)" != "off" ]]; then
            TITLE="ccm: ${AGENT} needs a decision"
            BODY="${SUMMARY:-permission requested}${CWD:+ — ${CWD##*/}}"
            if command -v terminal-notifier >/dev/null 2>&1; then
                terminal-notifier -title "$TITLE" -message "$BODY" \
                    -group "ccm-sidekick-${TMUX_PANE}" >/dev/null 2>&1 || true
            elif command -v osascript >/dev/null 2>&1; then
                osascript -e "display notification \"${BODY//\"/}\" with title \"${TITLE//\"/}\"" \
                    >/dev/null 2>&1 || true
            elif command -v notify-send >/dev/null 2>&1; then
                notify-send "$TITLE" "$BODY" >/dev/null 2>&1 || true
            fi
        fi
        ;;
    PermissionResult|permission_result|Interrupt|interrupt|Stop|stop|StopFailure|stop_failure|SessionEnd|session_end)
        # Any of these ends a pending wait. Overwrite in place,
        # carrying the waiting marker's identity forward so a consumer
        # can match resolution to request by `id`.
        [[ -f "$MARKER" ]] || exit 0
        PREV=$(cat "$MARKER" 2>/dev/null)
        printf '%s' "$PREV" | jq -e '.state == "waiting"' >/dev/null 2>&1 || exit 0
        printf '%s' "$PREV" | jq -c --arg rts "$NOW" \
            '.state = "resolved" | .resolved_ts = $rts' \
            > "${MARKER}.tmp" 2>/dev/null && mv "${MARKER}.tmp" "$MARKER" 2>/dev/null
        ;;
esac
exit 0
