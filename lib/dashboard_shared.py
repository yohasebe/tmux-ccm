"""Definitions the dashboard's modules share: curses colour pair ids and
the message an attach shows when it could not start Claude."""

import ccm_window
from ccm_core import tmux_cmd


# Color pair IDs (curses-specific, dashboard only)
C_PERMIT = 1
C_BUSY = 2
C_COMPLETED = 3
C_IDLE = 4
C_SHELL = 5
C_DIM = 6
C_CYAN = 7
C_YELLOW = 8
C_SYNCING = 9
# The ccm logo cells (rose / amber / sage, black letters) drawn at the top
# of a docked dashboard, which has no popup title to carry the logo.
C_LOGO_C1 = 10
C_LOGO_C2 = 11
C_LOGO_M = 12
C_DOCK_EDGE = 13
LOGO_CELLS = ((" c ", C_LOGO_C1), (" c ", C_LOGO_C2), (" m ", C_LOGO_M))

STATE_COLOR_PAIR = {
    "PERMIT": C_PERMIT, "BUSY": C_BUSY,
    "IDLE": C_IDLE, "SHELL": C_SHELL, "DOWN": C_SHELL,
}


def _announce_unlaunched(result):
    """After an attach that meant to start Claude: when nothing could
    be started, say so on the tmux message line — the dashboard is
    the main attach path and the popup is gone by the time the user
    would otherwise notice an empty shell. Silent for the outcomes
    that need no action (launched, already running, auto-start off)."""
    if result is not None and result.outcome == ccm_window.UNAVAILABLE:
        tmux_cmd("display-message", "-d", "8000",
                 "ccm: " + (result.reason or "Claude not auto-started — no pane could be "
                 "verified as a shell prompt. Start it by hand.").replace("#", "##"))
