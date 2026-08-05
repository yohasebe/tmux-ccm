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
# Testing this by hand: use a REAL pane id. ccm's reader reaps any
# marker whose pane no longer hosts an agent, so a synthetic
# `TMUX_PANE=%my-probe` is collected the moment ccm next builds its
# project list — which looks exactly like "the resolve step deleted
# the file" if a build lands between two invocations. (Reported by
# ringi 2026-08-05, who caught their own measurement before filing it.)
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
# `.message` is the last resort: Grok Build's permission Notification
# carries no tool fields at all (measured — only "Tool permission
# requested"), and a generic line still beats an empty one on a watch
# face, which is the surface ringi asked this field for.
SUMMARY=$(printf '%s' "$INPUT" | jq -r '
    . as $p
    | ([ ($p.tool_name // $p.toolName // empty),
         ($p.tool_input.command // $p.tool_input.file_path
           // $p.toolInput.command // $p.toolInput.file_path // empty)
       ] | map(select(. != null and . != "")) | join(": "))
    | if . == "" then ($p.message // "") else . end' 2>/dev/null | head -c 160)
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Normalize the event name to lowercase-without-separators before
# dispatching. Vendors disagree on casing for the SAME event and are
# free to change it: Kimi sends "PermissionRequest", Grok Build sends
# "pre_tool_use" / "stop" (measured 2026-08-05), and ringi found Grok's
# values snake_case where its own dispatch expected PascalCase — an
# accidental near-miss. Matching a normalized form instead of listing
# spellings is the same lesson 0.8.2 taught about upstream strings:
# never pin a shape the vendor can restyle.
# (`tr` rather than `${var,,}`: hooks must run under macOS bash 3.2.)
EVENT=$(printf '%s' "$EVENT" | tr '[:upper:]' '[:lower:]' | tr -d '_-')

# Notification is a family, not an event: the payload's type field
# says which side of the marker lifecycle it belongs to. Claude Code
# (routed via lib.sh's ignore branch) and Grok Build both use it —
# Grok has no PermissionRequest event at all, so for a Grok sidekick
# this IS the permission signal.
if [[ "$EVENT" == "notification" ]]; then
    NOTIF_TYPE=$(printf '%s' "$INPUT" | jq -r '.notification_type // .notificationType // empty' 2>/dev/null)
    case "$NOTIF_TYPE" in
        permission_prompt|elicitation_dialog) EVENT="permissionrequest" ;;
        idle_prompt) EVENT="stop" ;;
        *) exit 0 ;;
    esac
fi

case "$EVENT" in
    permissionrequest)
        mkdir -p "$ATTENTION_DIR" 2>/dev/null || exit 0
        # Double-fire guard: one Claude dialog raises BOTH
        # PermissionRequest and Notification(permission_prompt). The
        # first write owns the marker (and the one notification);
        # a second waiting-write for a still-open wait is a no-op.
        if [[ -f "$MARKER" ]] && jq -e '.state == "waiting"' "$MARKER" >/dev/null 2>&1; then
            exit 0
        fi
        # Write via tmp+rename, matching the resolve path below. A
        # direct `> "$MARKER"` truncates first, so a reader landing in
        # the gap between truncate and write sees an empty file,
        # treats it as unparseable and unlinks it — after which this
        # process keeps writing to an unlinked fd and the marker is
        # gone for good. rename(2) is atomic, so a concurrent reader
        # (ccm's GC, or ringi) only ever sees the old file or the new
        # one. Same reason the reader never edits a marker in place.
        if jq -cn \
            --arg agent "$AGENT" --arg id "${SESSION:-unknown}-$(date +%s)" \
            --arg cwd "$CWD" --arg ts "$NOW" --arg session "$SESSION" \
            --arg summary "$SUMMARY" --arg pane "$TMUX_PANE" --arg tool "$TOOL" \
            '{agent:$agent, state:"waiting", id:$id, cwd:$cwd, ts:$ts,
              session:$session, summary:$summary, pane:$pane, tool:$tool}' \
            > "${MARKER}.tmp" 2>/dev/null; then
            mv "${MARKER}.tmp" "$MARKER" 2>/dev/null || exit 0
        else
            rm -f "${MARKER}.tmp" 2>/dev/null
            exit 0
        fi

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
    permissionresult|interrupt|stop|stopfailure|sessionend|pretooluse|posttooluse|posttoolusefailure|userpromptsubmit|subagentstart|subagentstop|precompact|postcompact|permissiondenied)
        # Any of these ends a pending wait. Kimi has a true
        # resolution event (PermissionResult); Claude Code does not,
        # so for a Claude sidekick ANY subsequent activity event is
        # the proof the dialog was answered — an approved tool fires
        # PostToolUse, a denial's feedback round ends in Stop.
        # Overwrite in place, carrying the waiting marker's identity
        # forward so a consumer can match resolution to request by
        # `id`.
        [[ -f "$MARKER" ]] || exit 0
        PREV=$(cat "$MARKER" 2>/dev/null)
        printf '%s' "$PREV" | jq -e '.state == "waiting"' >/dev/null 2>&1 || exit 0
        printf '%s' "$PREV" | jq -c --arg rts "$NOW" \
            '.state = "resolved" | .resolved_ts = $rts' \
            > "${MARKER}.tmp" 2>/dev/null && mv "${MARKER}.tmp" "$MARKER" 2>/dev/null
        ;;
esac
exit 0
