#!/usr/bin/env bash
# tmux `pane-focus-in` handler for the unread mark (@ccm-pane-labels).
#
# Removes a pane's `@ccm_unread` once the user has been looking at it
# for a moment — not the instant it gets the focus. tmux reports focus
# to the active pane whenever the terminal comes back to the front or
# the window is switched to, which is exactly how one goes to look at
# a reply: clearing on that event removed the mark before it could be
# seen, so the only marks that ever showed were on a neighbouring
# pane of the window already in front.
#
# Usage (from ccm.tmux): on-pane-focus.sh <hook_pane> <pane_id>
# `@ccm-unread-linger` is how long the mark stays, in seconds.
pane="${1:-${2:-}}"
[[ "$pane" == %* ]] || exit 0

# Nothing to do for a pane that carries no mark: focus changes are
# frequent, and none of them should cost a sleeping process.
mark=$(tmux show-option -pqv -t "$pane" @ccm_unread 2>/dev/null)
[[ -n "$mark" ]] || exit 0

# This look, and no other. Every focus event starts a handler of its
# own, so without a stamp an earlier one would finish its wait in the
# middle of a later look and clear the mark a moment after the user
# came back — or clear a newer reply's mark that the earlier look
# never saw. The stamp says which look is current; the mark's value
# says which reply it was for.
look="$$-${RANDOM}"
tmux set-option -p -t "$pane" @ccm_unread_look "$look" 2>/dev/null || exit 0

linger=$(tmux show-option -gqv @ccm-unread-linger 2>/dev/null)
[[ "$linger" =~ ^[0-9]+([.][0-9]+)?$ ]] || linger=5
sleep "$linger"

# Still being looked at? A pane that was only passed through keeps
# its mark. One definition of "looked at", shared with the hooks.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/hooks/lib.sh" 2>/dev/null || exit 0
_ccm_pane_is_watched "$pane" || exit 0

# Compare and clear inside tmux, each clearing behind its own
# condition. Reading the stamp and the mark from here and then
# clearing would leave a gap: a reply completing in it, or the user
# coming back, sets a new mark and a new stamp, and this handler —
# having already passed its checks — would remove both. `if-shell -F`
# starts no shell and queues its body directly behind itself, so the
# comparison and the clearing it guards run back to back.
#
# The stamp gets a condition of its own rather than riding along with
# the mark. tmux runs `after-set-option` hooks between the two, and a
# hook that blocks there lets another client's commands through: a
# new look would set its stamp, and an unguarded second clear would
# take it.
tmux if-shell -F -t "$pane" \
    "#{&&:#{==:#{@ccm_unread_look},${look}},#{==:#{@ccm_unread},${mark}}}" \
    "set-option -pu -t '${pane}' @ccm_unread" 2>/dev/null || true
tmux if-shell -F -t "$pane" "#{==:#{@ccm_unread_look},${look}}" \
    "set-option -pu -t '${pane}' @ccm_unread_look" 2>/dev/null || true
