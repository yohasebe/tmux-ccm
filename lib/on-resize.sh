#!/usr/bin/env bash
# Re-render the status bar once the terminal has stopped resizing.
#
# The bar is a baked string. Mode 1 decides how many entries fit and
# how much padding to place from the width at render time, and writes
# the result into `status-right` as literal text; modes 0 and 2 bake
# their own layout the same way. tmux redraws that text on resize but
# does not recompute it, and nothing re-runs ccm on a resize, so the
# bar stays laid out for the old width until the next periodic pass —
# up to CCM_RECONCILE_INTERVAL away.
#
# Both directions are visible. Narrower: the block is now wider than
# the bar, and tmux clips `status-right` from the LEFT, which in left
# placement is the highest-priority entry. Wider: the entry count is
# the one chosen for the old width, so the bar shows fewer projects
# than it plainly has room for.
#
# Rendering on every event would start a python process per step of a
# drag, and a drag is many steps — the shape of subprocess-per-tick
# that CCM_RECONCILE_INTERVAL exists to keep out of the poll path. So
# each event stamps a file and waits; only the invocation whose stamp
# is still the newest goes on to render. One drag costs one render,
# at the size the drag ended on.

CCM_RESIZE_SETTLE="${CCM_RESIZE_SETTLE:-0.4}"

_dir="${CCM_TMP_DIR:-${TMPDIR:-/tmp}/ccm-${UID}}"
mkdir -p "$_dir" 2>/dev/null || exit 0
# One stamp per user, not per tmux server: a resize on another
# socket suppresses this one's render for the settle window. Worth
# knowing where isolated sockets are routine; the periodic tick
# still catches up.
_stamp_file="${_dir}/resize-stamp"

# The pid is the token: every hook invocation is its own process, and
# two of them cannot share a pid inside the settle window. Avoids
# needing sub-second clocks, which BSD `date` does not offer and
# bash 3.2 (still what macOS ships) has no EPOCHREALTIME for.
_stamp="$$"
printf '%s\n' "$_stamp" > "$_stamp_file" 2>/dev/null || exit 0

sleep "$CCM_RESIZE_SETTLE"

read -r _latest < "$_stamp_file" 2>/dev/null || exit 0
[ "$_latest" = "$_stamp" ] || exit 0   # a later resize is already waiting

if [ -n "${CCM_BIN:-}" ] && [ -x "$CCM_BIN" ]; then
    _bin="$CCM_BIN"
else
    _bin="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/ccm"
fi
[ -x "$_bin" ] || exit 0

exec "$_bin" inject-status --fast 2>/dev/null
