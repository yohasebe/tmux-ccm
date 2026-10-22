#!/usr/bin/env python3
"""ccm Dashboard — Python curses implementation for responsive TUI."""

import contextlib
import curses
import io
import locale
import os
import signal
import subprocess
import sys
import threading
import time
import unicodedata

# curses + wide characters require an initialized locale. Without
# this call, ncurses falls back to single-byte mode and `addstr`
# can raise OverflowError or render mojibake when the user has a
# Japanese / emoji project name. Calling at import time is safe —
# `setlocale("")` reads LC_ALL/LC_CTYPE/etc. from the environment
# and is a no-op when already set.
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass

# Add lib dir to path for ccm_core import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ccm_constants import (
    CCM_SNAPSHOT_DIR,
    CCM_TMP_DIR,
)
import ccm_core  # noqa: E402
from ccm_core import (
    CCMError,
    build_project_list,
    get_session,
    hooks_configured,
    log_caught_exception,
    ps_snapshot,
    raise_on_die,
    tmux_cmd,
    tmux_query,
    touch_popup_session,
)
from ccm_pane_state import enumerate_window_panes

#: Window option marking a window the dashboard opened to attach to a
#: background session; its value is the session's short id.
BG_ATTACH_TAG = "@ccm_bg_short"
import ccm_agentview
import ccm_spool
import ccm_presentation
import ccm_menu_help
from ccm_window import auto_start_claude, reset_window_after_attach


from ccm_commands import (
    cmd_add,
    cmd_ignore,
    cmd_register,
    cmd_remove,
    cmd_unignore,
    cmd_unregister,
)
from ccm_snapshot import cmd_snapshot_save
from ccm_render import (
    display_width,
    truncate_to_width,
)

# Max characters shown in the single-line message area
MSG_MAX_LEN = 200

REFRESH_INTERVAL = 2
# Fast-tick cadence for the hybrid refresh loop. Between full
# detection passes (REFRESH_INTERVAL), the refresh thread samples the
# PUSHED state channel — `@ccm_prev_state`, written instantly by the
# Claude Code hooks (and by the slow path's own commits) — with one
# `list-windows` subprocess per tick (~10 ms). A hook-driven state
# change therefore reaches the dashboard in ≤ ~0.3 s instead of
# waiting out the 2 s poll (worst case ~2.3 s), closing most of the
# latency gap vs purely event-driven consumers of the same hooks.
FAST_TICK_INTERVAL = 0.25
# A dashboard left open (a dock, typically) and not used for a while
# refreshes at a slower pace: the pushed channel is still checked every
# IDLE_FAST_TICK_INTERVAL, and full detection,
# which catches what no hook reports, runs every IDLE_REFRESH_INTERVAL.
# The first key press restores the normal pace.
IDLE_AFTER_SEC = 30
IDLE_FAST_TICK_INTERVAL = 0.5
IDLE_REFRESH_INTERVAL = 6
# How long the main loop waits for a key between redraw checks: 50 ms
# normally, longer when idle (a key still arrives at once; only the
# redraw of newly fetched data waits up to this long).
IDLE_KEY_WAIT_MS = 250

from dashboard_shared import (  # noqa: E402
    C_BUSY,
    C_COMPLETED,
    C_CYAN,
    C_DIM,
    C_DOCK_EDGE,
    C_IDLE,
    C_LOGO_C1,
    C_LOGO_C2,
    C_LOGO_M,
    C_PERMIT,
    C_SHELL,
    C_SYNCING,
    C_YELLOW,
    _announce_unlaunched,
)
from dashboard_menu import MenuMixin  # noqa: E402
from dashboard_render import RenderMixin  # noqa: E402
from dashboard_search import SearchMixin  # noqa: E402
from dashboard_tree import TreeMixin  # noqa: E402


# ─── Dashboard ───

from ccm_dashboard_lifecycle import LifecycleActions


class _DockEdgeScreen:
    """The screen of a docked dashboard: the whole pane less one row,
    which holds a coloured line on the side facing the work pane (the
    bottom row when docked at the top). tmux draws the border there as
    part of the pane below, so its colour cannot mark the dock.

    Every view draws through this object as if it were the screen, so
    none of them has to know the row is taken. It is rebuilt when the
    pane's size changes or the dock moves to the other edge. A pane too
    low to give up the row (the dock is opened a row taller, but a pane
    can be shrunk later) draws no line and gives the views all of it."""

    _SIDE_CHECK_SEC = 2.0

    def __init__(self, screen, attr, at_top, min_rows):
        self._screen = screen
        self._attr = attr
        self._at_top = at_top          # callable: is the dock at the top?
        self._min_rows = min_rows      # what the views need to draw at all
        self._keypad = None
        # timeout() and nodelay() set one thing, the wait for input; it is
        # kept as one value so a rebuild restores the latest of them.
        self._delay = None
        self._lined = False
        self._top = None
        self._checked = 0.0
        self._size = None
        self._win = None
        self._rebuild()

    def _rebuild(self):
        self._size = self._screen.getmaxyx()
        self._checked = time.monotonic()
        self._top = bool(self._at_top())
        height, width = self._size
        self._lined = height - 1 >= self._min_rows
        if self._lined:
            self._win = self._screen.derwin(height - 1, width, 0 if self._top else 1, 0)
        else:
            self._win = self._screen
        if self._keypad is not None:
            self._win.keypad(self._keypad)
        if self._delay is not None:
            self._win.timeout(self._delay)

    def _current(self):
        stale_side = time.monotonic() - self._checked > self._SIDE_CHECK_SEC
        if self._screen.getmaxyx() != self._size or (
                stale_side and bool(self._at_top()) != self._top):
            self._rebuild()
        elif stale_side:
            self._checked = time.monotonic()
        return self._win

    def keypad(self, flag):
        self._keypad = flag
        return self._current().keypad(flag)

    def timeout(self, delay):
        self._delay = delay
        return self._current().timeout(delay)

    def nodelay(self, flag):
        self._delay = 0 if flag else -1
        return self._current().nodelay(flag)

    def refresh(self):
        win = self._current()
        if self._lined:
            height, width = self._size
            row = height - 1 if self._top else 0
            try:
                self._screen.hline(row, 0, curses.ACS_HLINE | self._attr, width)
            except curses.error:
                pass
            self._screen.noutrefresh()
        win.noutrefresh()
        curses.doupdate()

    def __getattr__(self, name):
        return getattr(self._current(), name)


class _InputClock:
    """The screen every view reads keys from, noting when one arrives.
    The idle pace (see IDLE_AFTER_SEC) must end with any key, in any
    view: the search, prompts and menus read keys in loops of their own."""

    def __init__(self, screen, on_input):
        self._screen = screen
        self._on_input = on_input

    def getch(self, *args):
        key = self._screen.getch(*args)
        if key != -1:
            self._on_input()
        return key

    def get_wch(self, *args):
        key = self._screen.get_wch(*args)     # raises when no key arrived
        self._on_input()
        return key

    def __getattr__(self, name):
        return getattr(self._screen, name)


def _dock_edge_attr():
    """The curses attribute of the dock's edge line, or None for `off`."""
    import ccm_dock
    raw = (tmux_cmd("show-option", "-gqv", ccm_dock.EDGE_OPTION) or "").strip().lower()
    if raw == "off":
        return None
    try:
        colour = int(raw) if raw else ccm_dock.EDGE_DEFAULT
    except ValueError:
        colour = ccm_dock.EDGE_DEFAULT
    if not 0 <= colour <= 255:
        colour = ccm_dock.EDGE_DEFAULT
    try:
        if curses.COLORS >= 256:
            curses.init_pair(C_DOCK_EDGE, colour, -1)
        else:
            curses.init_pair(C_DOCK_EDGE, curses.COLOR_YELLOW, -1)
    except curses.error:
        return None
    return curses.color_pair(C_DOCK_EDGE) | curses.A_BOLD


def _dock_at_top():
    """Whether this dashboard's own pane is at the top of its window;
    taken to be when its pane is unknown or tmux does not answer."""
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return True
    return tmux_cmd("display-message", "-p", "-t", pane,
                    "#{pane_at_top}").strip() != "0"


class Dashboard(RenderMixin, MenuMixin, SearchMixin, TreeMixin, LifecycleActions):
    # Keys that only adjust a selection index. The main loop coalesces
    # these across terminal auto-repeat so a held arrow key renders
    # once at the end of the burst instead of once per keystroke.
    # All three modes (dashboard / tree / menu) share the same set
    # because each mode's ↑/↓/j/k handler is a safe selection-only
    # update with no side effects beyond `self.selected`,
    # `self.tree_selected`, or `self.menu_selected`.
    NAV_KEYS = frozenset((curses.KEY_UP, curses.KEY_DOWN, ord("j"), ord("k")))

    def __init__(self, initial_mode="dashboard", start_in_search=False, docked=False):
        self.projects = []
        # Frozen display order for the dashboard's lifetime: a list of
        # win_targets in the order first seen. `build_project_list`
        # re-sorts by state on every refresh, which would reshuffle
        # rows under the user's cursor mid-interaction; freezing the
        # order keeps rows put while the popup is open. Each open is a
        # fresh popup process, so the order is naturally re-decided on
        # reopen. See `_set_projects_stable`.
        self._display_order = []
        self.lock = threading.Lock()
        self.selected = 0
        self.running = True
        self.data_dirty = False
        self.initial_load = True
        # Last fast-tick snapshot of the pushed-state channel
        # ({win_target: @ccm_prev_state}). Only used by the refresh
        # thread; see `_fast_tick`.
        self._pushed_states = {}
        self.hooks_on = hooks_configured()
        self.hooks_status = "Hooks: ON" if self.hooks_on else "Hooks: OFF"
        self.mode = initial_mode  # "dashboard", "tree", "menu"
        self.start_in_search = start_in_search
        # Docked (a pane, not a popup): attaching keeps the dashboard open
        # and the window-change hook brings the pane along; quitting asks
        # tmux to close the pane so the other panes get their rows back.
        self.docked = docked
        self._last_input = time.monotonic()
        # Tree mode state
        self.tree_lines = []     # (indent, text, attr, win_target_or_none)
        self.tree_selected = 0
        self.tree_selectable = []  # indices into tree_lines that are selectable
        # Preview panel state
        preview_setting = tmux_cmd("show-option", "-gqv", "@ccm-preview") or "off"
        self.preview_enabled = preview_setting == "on"
        self.preview_position = tmux_cmd("show-option", "-gqv", "@ccm-preview-position") or "right"
        self.preview_cache = ""
        self._preview_lines = []
        self._last_preview_target = ""
        # Monotonic timestamp marking the end of a "navigation burst"
        # window. While now < _nav_deadline, render() skips the
        # expensive preview panel redraw (character-by-character
        # addstr with ANSI parsing) so holding ↑/↓ keeps up with
        # terminal auto-repeat. When the deadline passes, the main
        # loop triggers one full render to restore the preview.
        self._nav_deadline = 0.0
        # Menu mode state
        self.menu_items = []  # Built dynamically by _build_menu()
        self.menu_selected = 0
        # Non-blocking message display
        self._msg_text = ""
        self._msg_expires = 0.0
        # Background-session section (Claude Code agent-view roster).
        # Defaults to off (window=project purists are unaffected).
        # `@ccm-bg-section always` makes it visible on every dashboard
        # open; the `b` key toggles visibility session-locally
        # regardless of setting. Hides itself automatically when the
        # daemon isn't running (no clutter for non-users).
        bg_setting = tmux_cmd("show-option", "-gqv", "@ccm-bg-section") or "off"
        self.bg_section_setting = bg_setting if bg_setting in ("off", "always") else "off"
        self.bg_visible = self.bg_section_setting == "always"
        self.bg_sessions = []
        # Hand-off notices (`claude --continue` would start fresh),
        # computed on the refresh thread alongside the project list so
        # render never scans transcripts on the key-handling thread.
        self.handoff_warnings = []

    def run(self, stdscr):
        # Curses setup
        curses.curs_set(0)
        curses.use_default_colors()
        stdscr.keypad(True)
        stdscr.timeout(50)  # 50ms getch timeout → ~20Hz key polling

        # Set ESCDELAY for faster Escape handling
        try:
            curses.set_escdelay(25)
        except AttributeError:
            pass

        # Init colors
        self._init_colors()

        # Docked: give up one row for the line that marks the dock's edge.
        if self.docked:
            edge = _dock_edge_attr()
            if edge is not None:
                stdscr = _DockEdgeScreen(stdscr, edge, _dock_at_top, self.MIN_HEIGHT)
                stdscr.keypad(True)
                stdscr.timeout(50)

        # Every view reads keys through this, so any key ends the idle pace.
        stdscr = _InputClock(stdscr, self._note_input)

        # Instant first paint from cached state. This first build
        # establishes the frozen display order for the popup's lifetime.
        self._set_projects_stable(build_project_list(fast=True))
        self._select_current_project()
        # Fetch bg sessions synchronously on initial paint when the
        # section is visible, so the user doesn't see "Background
        # sessions (0)" flicker into the actual list 300 ms later.
        # Cheap on a local path (~/.claude is not synced) — a single
        # roster.json read plus N state.json reads where N is small.
        if self.bg_visible:
            self.bg_sessions = self._fetch_bg_sessions()
        if self.mode == "tree":
            self._build_tree()
        elif self.mode == "menu":
            self._build_menu()
        self._render_current(stdscr)

        # Start background refresh
        bg = threading.Thread(target=self._refresh_loop, daemon=True)
        bg.start()

        # If launched with --search, jump straight into the live-filter
        # search. Only meaningful in dashboard mode — tree and menu have
        # their own navigation. If the user attaches from the filter,
        # skip the main loop entirely so the popup closes immediately.
        if self.start_in_search and self.mode == "dashboard":
            action = self._do_search(stdscr)
            if self._should_stop(action):
                return
            self._render_current(stdscr)

        # Main event loop
        while self.running:
            touch_popup_session()

            # Other views set their own waits and return; this is set
            # each pass so the main loop's wait is always its own.
            stdscr.timeout(IDLE_KEY_WAIT_MS if self._idle() else 50)
            key = stdscr.getch()
            if key == -1:
                # Deferred preview restore: if a navigation burst just
                # ended (deadline passed), do one full render to bring
                # the preview panel back. Without this the preview
                # would stay blank until the next background refresh.
                if self._nav_deadline and time.monotonic() >= self._nav_deadline:
                    self._nav_deadline = 0.0
                    self._render_current(stdscr)
                elif self.data_dirty:
                    with self.lock:
                        self.data_dirty = False
                    self._render_current(stdscr)
                continue

            if key == curses.KEY_RESIZE:
                self._render_current(stdscr)
                continue

            action = self._dispatch_key(key, stdscr)
            if self._should_stop(action):
                break

            # Coalesce queued navigation keys. Terminal auto-repeat
            # outpaces the full render cycle (capture-pane subprocess
            # + ANSI parse + preview panel redraw), so without
            # coalescing a held ↓/↑ accumulates in the input buffer
            # and the cursor appears to lag then jump. Drain
            # contiguous nav keys with nodelay, applying each to the
            # selection, and render once at the end of the burst.
            # Non-nav keys encountered during drain are pushed back
            # with ungetch so the normal dispatch path handles them
            # on the next loop iteration.
            if key in self.NAV_KEYS:
                stdscr.nodelay(True)
                try:
                    while True:
                        nxt = stdscr.getch()
                        if nxt == -1:
                            break
                        if nxt in self.NAV_KEYS:
                            nxt_action = self._dispatch_key(nxt, stdscr)
                            if nxt_action in ("quit", "attached"):
                                action = nxt_action
                                break
                        else:
                            try:
                                curses.ungetch(nxt)
                            except curses.error:
                                pass
                            break
                finally:
                    stdscr.timeout(50)
                if self._should_stop(action):
                    break

            self._render_current(stdscr)

    def _closes_on_open(self):
        return self.docked and tmux_cmd(
            "show-option", "-gqv", "@ccm-dashboard-dock-close-on-open") == "on"

    def _before_switch(self):
        """Right before this dashboard switches the client to another
        window: a dock that is about to close marks itself, so the
        window-change hook does not first carry it into the new window
        (which would lay that window out twice, a visible flicker).

        The decision is made once here and kept, so the close that follows
        acts on the same answer even if the option changes in between."""
        if self._closes_on_open():
            import ccm_dock
            ccm_dock.mark_leaving(os.environ.get("TMUX_PANE", ""))
            self._closing_after_switch = True

    def _should_stop(self, action):
        """Whether the main loop ends after `action`."""
        if action not in ("quit", "attached"):
            if getattr(self, "_closing_after_switch", False):
                # The switch did not happen: stay, and follow again.
                import ccm_dock
                ccm_dock.mark_leaving(os.environ.get("TMUX_PANE", ""), False)
                self._closing_after_switch = False
            return False
        if not self.docked:
            return True
        marked = getattr(self, "_closing_after_switch", False)
        if action == "attached" and not marked:
            return False
        # tmux closes the pane and restores the layout; doing it from here
        # would kill this process before the layout is put back.
        import ccm_dock
        ccm_dock.request_close()
        # Stay alive until tmux removes the pane: exiting first would close
        # it before the layout could be measured and restored.
        time.sleep(5)
        return True

    def _render_current(self, stdscr):
        # Self-heal external screen corruption. tmux's popup overlay
        # clipping has (as of 3.7b) cases — notably a pane streaming
        # double-width CJK output behind the popup — where background
        # pane updates are drawn INTO the popup region, clobbering our
        # cells (upstream churn: tmux PR #4920 fixed one shape in 3.7,
        # PR #4997 another; wide-char cases persist). curses diffs
        # against its own model of the physical screen, so it believes
        # those cells are still correct and a normal refresh rewrites
        # nothing — the garbage sticks forever. redrawwin() marks the
        # whole window corrupted, forcing the next refresh to re-emit
        # every cell. Renders run on every keypress and every
        # REFRESH_INTERVAL tick, so damage heals within ~2 s. Full
        # re-emit of an 80%×60% popup over the local socket is
        # negligible.
        stdscr.redrawwin()
        if self.mode == "dashboard":
            self.render(stdscr)
        elif self.mode == "tree":
            self._render_max_col = 0  # Full width for tree/menu
            self._render_tree(stdscr)
        elif self.mode == "menu":
            self._render_max_col = 0
            self._render_menu(stdscr)

    def _dispatch_key(self, key, stdscr):
        if self.mode == "dashboard":
            return self._handle_key(key, stdscr)
        elif self.mode == "tree":
            return self._handle_tree_key(key, stdscr)
        elif self.mode == "menu":
            return self._handle_menu_key(key, stdscr)
        return ""

    def _init_colors(self):
        if curses.COLORS >= 256:
            # Salmon for BUSY (matches Claude Code's "Choreographing..." text)
            curses.init_pair(C_PERMIT, curses.COLOR_YELLOW, -1)
            curses.init_pair(C_BUSY, 216, -1)    # salmon (#ff9966, matches status bar #e8967d)
            curses.init_pair(C_COMPLETED, curses.COLOR_GREEN, -1)
            curses.init_pair(C_IDLE, 68, -1)      # blue
            curses.init_pair(C_SHELL, 245, -1)    # gray
            curses.init_pair(C_DIM, 242, -1)      # dim gray
            curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
            curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
            curses.init_pair(C_SYNCING, curses.COLOR_CYAN, -1)
            # Nearest 256-colour cells to the popup title's #E89B9B / #E8C76A / #86C99B.
            curses.init_pair(C_LOGO_C1, 16, 174)
            curses.init_pair(C_LOGO_C2, 16, 179)
            curses.init_pair(C_LOGO_M, 16, 108)
        else:
            curses.init_pair(C_PERMIT, curses.COLOR_YELLOW, -1)
            curses.init_pair(C_BUSY, curses.COLOR_RED, -1)
            curses.init_pair(C_COMPLETED, curses.COLOR_GREEN, -1)
            curses.init_pair(C_IDLE, curses.COLOR_BLUE, -1)
            curses.init_pair(C_SHELL, curses.COLOR_WHITE, -1)
            curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
            curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
            curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
            curses.init_pair(C_SYNCING, curses.COLOR_CYAN, -1)
            curses.init_pair(C_LOGO_C1, curses.COLOR_BLACK, curses.COLOR_RED)
            curses.init_pair(C_LOGO_C2, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            curses.init_pair(C_LOGO_M, curses.COLOR_BLACK, curses.COLOR_GREEN)

    MIN_HEIGHT = 10
    MIN_WIDTH = 40

    @staticmethod
    def _strip_last_grapheme(text):
        """Remove the last user-perceived character (grapheme cluster).

        Handles combining marks (category M) and zero-width joiners (U+200D)
        so that e.g. accented characters and ZWJ emoji sequences are deleted
        as a single unit on backspace.
        """
        if not text:
            return text
        i = len(text)
        while i > 0:
            i -= 1
            cat = unicodedata.category(text[i])
            if cat.startswith("M"):
                # Combining mark — keep walking back
                continue
            # Base character found; check if preceded by ZWJ
            if i > 0 and text[i - 1] == "\u200d":
                i -= 1  # skip ZWJ, continue to next base char
                continue
            break
        return text[:i]

    @staticmethod
    def _strip_first_grapheme(text):
        """Remove the first user-perceived character, with the same
        clustering as `_strip_last_grapheme` (combining marks and
        ZWJ-joined characters go with their base)."""
        i = 1 if text else 0
        while i < len(text):
            if unicodedata.category(text[i]).startswith("M"):
                i += 1
            elif text[i] == "\u200d":
                i = min(i + 2, len(text))
            else:
                break
        return text[i:]

    def _addstr(self, stdscr, y, x, text, attr=0, max_col=0):
        """Safe addstr that handles wide characters and boundary clipping.
        max_col: if >0, clip text to this column. Falls back to _render_max_col."""
        try:
            height, width = stdscr.getmaxyx()
            if y < 0 or y >= height or x >= width:
                return
            effective_max = max_col or getattr(self, '_render_max_col', 0) or width
            avail = effective_max - x - 1
            if avail <= 0:
                return
            clipped = truncate_to_width(text, avail)
            stdscr.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def _handle_key(self, key, stdscr):
        n = len(self.projects)
        # Unified selection: indices [0, n) address projects;
        # indices [n, n+m) address bg sessions when bg is visible.
        # Hidden bg never participates in navigation, so the
        # window=project mental model stays clean for users who
        # never toggle bg on.
        bg_count = len(self.bg_sessions) if self.bg_visible else 0
        total = n + bg_count

        if key in (curses.KEY_UP, ord("k")):
            if total > 0:
                self.selected = (self.selected - 1) % total
                self._last_preview_target = ""  # Force preview refresh
                self._nav_deadline = time.monotonic() + 0.1
        elif key in (curses.KEY_DOWN, ord("j")):
            if total > 0:
                self.selected = (self.selected + 1) % total
                self._last_preview_target = ""  # Force preview refresh
                self._nav_deadline = time.monotonic() + 0.1
        elif key in (curses.KEY_ENTER, 10, 13):
            bg_idx = self._selected_bg_index()
            if bg_idx is not None:
                return self._do_attach_bg(stdscr, bg_idx)
            # Guard against a stale selection that points past the
            # project list. The race: user selects a bg row, a refresh
            # tick removes the bg session, render() hasn't fired yet
            # (its clamp would otherwise normalise self.selected), and
            # Enter arrives in the 50 ms gap. Without the bound check
            # we'd index projects[N] and crash.
            if 0 <= self.selected < n:
                return self._do_attach(stdscr)
        elif ccm_menu_help.matches_key("quit", key) or key in (27, curses.KEY_F1):
            return "quit"
        elif ccm_menu_help.matches_key("spool", key):
            return self._do_spool(stdscr)
        elif ccm_menu_help.matches_key("save", key):
            self._do_save(stdscr)
        # The project-scoped action keys (p/n/r/i) share the Enter
        # key's stale-selection guard above: when a bg row is
        # selected, self.selected >= n and indexing projects[]
        # would raise IndexError.
        elif key in (ord("p"), ord("P")):
            if 0 <= self.selected < n:
                self._do_preview(stdscr)
        elif ccm_menu_help.matches_key("add", key):
            self._do_add(stdscr)
        elif key in (ord("n"), ord("N")):
            if 0 <= self.selected < n:
                self._do_rename(stdscr)
        elif key in (ord("r"), ord("R")):
            if 0 <= self.selected < n:
                self._do_remove(stdscr)
        elif key in (ord("g"), ord("G")):
            self._do_register(stdscr)
        elif ccm_menu_help.matches_key("ignore", key):
            if 0 <= self.selected < n:
                self._do_ignore_toggle(stdscr)
        elif key in (ord("x"), ord("X")):
            self._do_exit_all(stdscr)
        elif key == ord("/"):
            action = self._do_search(stdscr)
            if action == "attached":
                return "attached"
        elif ccm_menu_help.matches_key("tree", key):
            self.mode = "tree"
            self._build_tree()
        elif key in (ord("m"), ord("M"), ord("?")):
            # `?` is the universal "show me all the commands" key
            # (vim, fzf, etc.) — alias to menu mode which already
            # lists every action this dashboard exposes.
            self.mode = "menu"
            self.menu_selected = 0
            self._build_menu()
        elif key in (ord("w"), ord("W")):
            # Sidekick-attention toggle (`w` = watch). Unlike `b`,
            # this writes the GLOBAL @ccm-sidekick-attention option:
            # the badge colour, the status bar and the hook-side
            # desktop notification all read the same switch, and a
            # session-local flag would let those surfaces disagree —
            # the split-brain this feature exists to prevent.
            cur = ccm_core.tmux_cmd(
                "show-option", "-gqv", "@ccm-sidekick-attention")
            new = "on" if cur == "off" else "off"
            ccm_core.tmux_cmd(
                "set-option", "-g", "@ccm-sidekick-attention", new)
            self._show_message(stdscr, f"Sidekick attention: {new}", 1)
            self._trigger_rebuild()
        elif key in (ord("b"), ord("B")):
            # Session-local toggle for the background-sessions
            # section. Independent of the @ccm-bg-section persistent
            # setting — pressing `b` is always allowed even when the
            # setting is "always" (the user can hide on demand). The
            # toggle does NOT write back to tmux config; use the menu
            # for that.
            self.bg_visible = not self.bg_visible
            if self.bg_visible:
                # Sync-fetch on toggle-on so the section appears
                # populated from frame zero. Without this, the user
                # sees "Background sessions (0)" until the next
                # refresh tick (up to REFRESH_INTERVAL seconds).
                # I/O cost is tiny (single roster.json + N small
                # state.json files) and bounded.
                with self.lock:
                    self.bg_sessions = self._fetch_bg_sessions()
            elif self.selected >= n:
                # Clamp selection back into the project list if we
                # just hid a bg row that was selected. Otherwise the
                # ▶ indicator would point at no visible row.
                self.selected = max(0, n - 1)

        return ""

    def _spool_text(self, stdscr, title, body):
        """Scrollable full text, including lines wider than the popup."""
        offset = 0
        stdscr.timeout(-1)
        try:
            while True:
                height, width = stdscr.getmaxyx()
                rows = []
                for line in body.split("\n"):
                    row = ""
                    for char in line.expandtabs(4):
                        char = char if char.isprintable() else "�"
                        if row and display_width(row + char) > max(1, width - 3):
                            rows.append(row)
                            row = ""
                        row += char
                    rows.append(row)
                count = max(1, height - 2)
                offset = min(offset, max(0, len(rows) - count))
                stdscr.erase()
                self._addstr(stdscr, 0, 0, title + " — ↑↓/PgUp/PgDn scroll; q back", curses.A_BOLD)
                for i, row in enumerate(rows[offset:offset + count]):
                    self._addstr(stdscr, i + 1, 0, row)
                stdscr.refresh()
                key = stdscr.getch()
                if key in (27, ord("q"), 10, 13):
                    return
                if key in (curses.KEY_DOWN, ord("j")):
                    offset += 1
                elif key in (curses.KEY_UP, ord("k")):
                    offset = max(0, offset - 1)
                elif key == curses.KEY_NPAGE:
                    offset += count
                elif key == curses.KEY_PPAGE:
                    offset = max(0, offset - count)
        finally:
            stdscr.timeout(50)

    def _do_spool(self, stdscr):
        """An explicit decision view; no background typing or deletion."""
        previous_max_col = getattr(self, "_render_max_col", 0)
        self._render_max_col = 0
        records = ccm_spool.attention_records()
        selected = 0
        status = ""
        def identity(record):
            return record["project"], record["kind"], record["id"]
        try:
            while True:
                height, width = stdscr.getmaxyx()
                count = max(1, (height - 5) // 2)
                selected = min(selected, max(0, len(records) - 1))
                stdscr.erase()
                self._addstr(stdscr, 0, 0, "Undelivered messages", curses.A_BOLD)
                first = (selected // count) * count
                for i, record in enumerate(records[first:first + count]):
                    label = (f"{'▶' if first + i == selected else ' '} {ccm_presentation.record_heading(record['kind'])} "
                             f"{record['sender']} → {record['project']} · {record['age']}")
                    self._addstr(stdscr, 1 + i * 2, 0, label)
                    if ccm_spool.record_actions(record["kind"]):
                        self._addstr(stdscr, 2 + i * 2, 2, record["preview"])
                record = records[selected] if records else None
                actions = ccm_spool.record_actions(record["kind"]) if record else ()
                help_text = ccm_presentation.record_help(actions)
                if not records:
                    self._addstr(stdscr, 2, 0, "No undelivered messages.")
                self._addstr(stdscr, height - 3, 0, help_text)
                guidance = ""
                if record and actions:
                    spec = ccm_presentation.record_spec(record["kind"])
                    guidance = ccm_presentation.ACTIONS[spec.action_id].description
                self._addstr(stdscr, height - 2, 0, status or guidance)
                stdscr.refresh()
                stdscr.timeout(-1)
                key = stdscr.getch()
                if key in (27, ord("q")):
                    return ""
                if key in (curses.KEY_DOWN, ord("j")) and records:
                    selected = (selected + 1) % len(records)
                elif key in (curses.KEY_UP, ord("k")) and records:
                    selected = (selected - 1) % len(records)
                elif record:
                    args = [record["kind"], record["id"], record["project"]]
                    if not actions:
                        status = ccm_presentation.record_spec(record["kind"]).label
                        continue
                    if key in (10, 13, curses.KEY_ENTER) and "read" in actions:
                        try:
                            with raise_on_die():
                                body = ccm_spool.read_record(*args)
                            self._spool_text(stdscr, ccm_presentation.record_title(record["kind"], record["project"]), body)
                        except CCMError as e:
                            status = str(e)
                    elif key == ord("o") and "open" in actions:
                        # Selecting alone sends no keys and never auto-starts.
                        projects = build_project_list(fast=False)
                        project = next((p for p in projects if p.name == record["project"]), None)
                        if project is None:
                            status = "Project is not open. Open it first, then return here."
                        else:
                            self._before_switch()
                            target_session = project.win_target.split(":")[0]
                            if target_session != get_session():
                                tmux_cmd("switch-client", "-t", target_session)
                            tmux_cmd("select-window", "-t", project.win_target)
                            return "attached"
                    elif (key == ord("d") and "discard" in actions) or (key == ord("r") and "resend" in actions):
                        action = "discard" if key == ord("d") else "resend"
                        flags = ["--yes"]
                        verb = ("Discard selected record (input unchanged)"
                                if record["kind"] == "held" else "Discard selected record")
                        if action == "resend":
                            projects = build_project_list(fast=False)
                            project = next((p for p in projects if p.name == record["project"]), None)
                            verb = f"Resend selected message to {record['project']}"
                            if project is not None and project.state == "SHELL":
                                verb = f"Start Claude and resend to {record['project']}"
                                flags.append("--start")
                        answer = self._prompt(stdscr, f"{verb}? [y/N] ")
                        if not answer or answer.lower() != "y":
                            status = "Cancelled; record kept."
                            continue
                        ok = self._run_cmd(stdscr, ccm_spool.cmd_spool, [action] + args + flags)
                        if not ok:
                            status = self._msg_text
                        elif action == "resend":
                            status = "Message sent; original record cleared."
                        elif record["kind"] == "held":
                            status = "Record discarded; input box unchanged."
                        else:
                            status = "Record discarded."
                        # Refresh without sorting existing rows or moving selection
                        # to another identity when an earlier record disappears.
                        old_key = identity(record)
                        fresh = ccm_spool.attention_records()
                        by_key = {identity(r): r for r in fresh}
                        old_keys = {identity(r) for r in records}
                        records = ([by_key[identity(r)] for r in records if identity(r) in by_key]
                                   + [r for r in fresh if identity(r) not in old_keys])
                        selected = next((i for i, r in enumerate(records) if identity(r) == old_key), selected)
        finally:
            stdscr.timeout(50)
            self._render_max_col = previous_max_col

    def _selected_bg_index(self):
        """Return the index into `self.bg_sessions` of the currently
        selected row, or None when the selection is in the project
        list (or out of bounds). The bg section is selectable only
        while visible, so a hidden bg never claims the selection."""
        if not self.bg_visible:
            return None
        n = len(self.projects)
        if self.selected < n:
            return None
        idx = self.selected - n
        if idx >= len(self.bg_sessions):
            return None
        return idx

    def _do_attach_bg(self, stdscr, bg_idx):
        """Open a new tmux window and run `claude attach <short>` for
        the selected background session.

        Why a new window (not the current pane / not a split): ccm's
        `auto_start_claude` fires on attach to any SHELL-state pane in
        a ccm-tagged window, racing the user's `claude attach` (see
        Issue 6 in `project_agent_view_findings_2026_05_12`). A
        brand-new window has no `@ccm_project` / `@ccm_dir` tags, so
        it stays out of ccm's lifecycle entirely. The send-keys-driven
        `claude attach` then has a clean shell prompt to dispatch
        from. The user can close the window with `prefix + &` after
        detaching from claude.
        """
        s = self.bg_sessions[bg_idx]
        # Defence-in-depth: ccm_agentview already filters non-matching
        # shorts at the reader, but we re-check here because the value
        # is about to be embedded in a shell-bound `claude attach
        # <short>` command via tmux send-keys. The receiving pane's
        # shell interprets metacharacters in send-keys input.
        if not ccm_agentview.is_valid_short(s.short):
            self._show_message(stdscr, f"Refusing to attach: invalid short {s.short!r}", 2)
            return ""
        session = get_session()
        if not session:
            self._show_message(stdscr, "Cannot attach: no tmux session", 2)
            return ""
        # A window this dashboard already opened for the session is
        # switched to, not duplicated: every extra `claude attach` is
        # another client drawing the same conversation, and enough of
        # them garble it. The window is recognised by the tag set
        # below, not by its name (names can be changed), and only
        # while it still hosts claude — an attach that has since
        # ended leaves a plain shell, into which a fresh attach is
        # typed instead.
        verdict, windows = self._find_bg_attach_windows(session, s.short)
        if verdict == "one":
            self._before_switch()
            tmux_cmd("select-window", "-t", windows[0])
            return "attached"
        if verdict == "many":
            # Two live attach windows already draw this conversation;
            # a third would only add to the garbling. Name them and
            # let the person pick.
            self._show_message(
                stdscr, f"Already attached in {', '.join(windows)} — switch there", 3)
            return ""
        if verdict == "unknown":
            self._show_message(
                stdscr, "Cannot tell whether an attach window is open — not opening another", 3)
            return ""
        # `new-window -t <session>:` (no index) places the window at
        # the next available index. `-P -F` makes tmux print the new
        # window's resolved target (`session:idx`), which we then
        # send-keys to and select. Append `-c <cwd>` (NOT insert) so
        # we never sit between `-t` and its required value.
        # `-d`: created without switching to it, so the switch below is the
        # only one (a docked dashboard that is about to close is marked
        # before it; see `_before_switch`).
        args = ["new-window", "-d", "-t", f"{session}:", "-P",
                "-F", "#{session_name}:#{window_index}",
                "-n", f"bg-{s.short}"]
        # Tagged as this session's attach window so a later Enter on
        # the same row finds it (see `_find_bg_attach_windows`).
        cwd = os.path.expanduser(s.cwd) if s.cwd else ""
        # Reject cwd values that could be mis-parsed by tmux as a
        # flag (e.g. "-x"). tmux's `new-window -c <path>` does not
        # accept a `--` separator, so any cwd starting with "-" is
        # ambiguous; we silently fall back to no -c, which inherits
        # the caller's pwd. cwd from claude itself is always an
        # absolute path starting with "/", so this only rejects
        # upstream-malformed values.
        if cwd and not cwd.startswith("-") and os.path.isdir(cwd):
            args.extend(["-c", cwd])
        new_target = tmux_cmd(*args)
        if not new_target:
            self._show_message(stdscr, "Failed to open attach window", 2)
            return ""
        new_target = new_target.strip()
        tmux_cmd("set-option", "-wt", new_target, BG_ATTACH_TAG, s.short)
        tmux_cmd("send-keys", "-t", new_target,
                 f"claude attach {s.short}", "Enter")
        self._before_switch()
        tmux_cmd("select-window", "-t", new_target)
        return "attached"

    def _find_bg_attach_windows(self, session, short):
        """Windows in `session` tagged as attach windows for `short`
        and still hosting claude — the ones drawing its conversation.
        Returns `(verdict, targets)`: `("one", [t])` when exactly one
        live window was found, `("many", [...])` for more, `("none",
        [])` when there is none (tagged windows whose attach has
        ended are shells now, and do not count), and `("unknown",
        [])` when the answer could not be established — the listing
        or the process snapshot could not be read, or a tagged
        window's panes could not be listed. Liveness is checked
        before counting: a tagged window left behind by a finished
        attach must not make the live one ambiguous."""
        raw = tmux_query("list-windows", "-t", f"{session}:", "-F",
                         "#{session_name}:#{window_index}\t#{" + BG_ATTACH_TAG + "}")
        if raw is None:
            return "unknown", []
        tagged = [line.split("\t")[0] for line in raw.split("\n")
                  if line.count("\t") == 1 and line.split("\t")[1] == short]
        if not tagged:
            return "none", []
        ps_raw = ps_snapshot().strip()
        if not ps_raw:
            return "unknown", []
        ps_lines = ps_raw.split("\n")
        live = []
        for target in tagged:
            panes = enumerate_window_panes(target, ps_lines)
            if not panes:
                return "unknown", []
            if any(p.claude_pid for p in panes):
                live.append(target)
        if not live:
            return "none", []
        return ("one" if len(live) == 1 else "many"), live

    def _do_attach(self, stdscr):
        p = self.projects[self.selected]
        import ccm_roles
        if ccm_roles.pending(p.win_target, query=tmux_cmd):
            self._show_message(stdscr, "Restore incomplete; retry the snapshot load", 3)
            return ""
        # Check if project directory still exists
        if p.dir and not os.path.isdir(os.path.expanduser(p.dir)):
            self._show_message(stdscr, f"Directory not found: {p.dir}", 3)
            return ""
        # Ensure the target pane is not stuck in copy-mode / view-mode.
        # If it is, any subsequent send-keys would be interpreted as
        # copy-mode bindings (e.g. the user could land in a search
        # prompt instead of the shell). `-X cancel` is a safe no-op
        # when the pane is already in normal input mode.
        tmux_cmd("send-keys", "-t", p.win_target, "-X", "cancel")
        # Auto-start if SHELL. Routed through ccm_window.auto_start_claude
        # so the launch command goes to a shell-foreground pane only —
        # a bare `send-keys -t <window>` would type it into the active
        # pane, which in a split window may be an editor or pager.
        if p.state == "SHELL":
            _announce_unlaunched(auto_start_claude(p.win_target))
        reset_window_after_attach(p.win_target)
        self._before_switch()
        # Cross-session switch
        session = get_session()
        target_session = p.win_target.split(":")[0]
        if target_session != session:
            tmux_cmd("switch-client", "-t", target_session)
        tmux_cmd("select-window", "-t", p.win_target)
        return "attached"

    def _do_save(self, stdscr):
        default_name = time.strftime("save-%Y%m%d-%H%M")
        name = self._prompt(stdscr, f"Snapshot name [{default_name}]: ")
        if name is None:
            return
        if not name:
            name = default_name
        try:
            saved = cmd_snapshot_save(name, quiet=True)
            if saved is False:
                self._show_message(stdscr, f"Snapshot unchanged, empty or protected: {name}", 1.5)
                return
            try:
                import json
                with open(os.path.join(CCM_SNAPSHOT_DIR, f"{name}.json"), encoding="utf-8") as f:
                    count = len(json.load(f).get("projects", []))
                msg = f"Saved: {name} ({count} projects)"
            except Exception:
                msg = f"Saved: {name}"
        except SystemExit:
            msg = "Save failed: no active projects"
        except Exception as e:
            msg = f"Save failed: {str(e)[:50]}"
        self._show_message(stdscr, msg, 1.5)

    def _do_preview(self, stdscr):
        p = self.projects[self.selected]
        pane = self._resolve_preview_pane(p.win_target)
        captured = tmux_cmd("capture-pane", "-t", pane, "-p", "-S", "-30")
        if not captured or not captured.strip():
            captured = tmux_cmd("capture-pane", "-a", "-t", pane, "-p", "-S", "-30")
        if not captured:
            return
        stdscr.erase()
        lines = captured.split("\n")
        height, width = stdscr.getmaxyx()
        self._addstr(stdscr, 0, 0, f"=== {p.name} (press any key, 'c' to copy) ===",
                     curses.A_BOLD)
        for i, line in enumerate(lines[:height - 2]):
            self._addstr(stdscr, i + 1, 0, line, 0)
        stdscr.refresh()
        stdscr.timeout(-1)
        key = stdscr.getch()
        stdscr.timeout(50)
        if key in (ord("c"), ord("C")):
            try:
                for cmd in (["pbcopy"], ["clip.exe"], ["xclip", "-selection", "clipboard"], ["xsel", "-b"]):
                    try:
                        subprocess.run(cmd, input=captured.encode(), timeout=3)
                        break
                    except FileNotFoundError:
                        continue
            except Exception:
                pass

    def _do_add(self, stdscr):
        directory = self._prompt(stdscr, "Directory: ", path_completion=True)
        if not directory:
            return
        directory = os.path.expanduser(directory)
        create_dir = False
        if not os.path.isdir(directory):
            # Parent-must-exist gate: matches the CLI handler's
            # rule and `cmd_add(create_dir=True)`'s internal check.
            # Surfacing it here lets us give an immediate inline
            # message instead of bouncing through `_run_cmd` for
            # an obvious bad input.
            parent = os.path.dirname(os.path.abspath(directory)) or "/"
            if not os.path.isdir(parent):
                self._show_message(
                    stdscr,
                    f"Parent does not exist: {parent}",
                    2,
                )
                return
            answer = self._prompt(stdscr, f"Create '{directory}'? [y/N]: ")
            if answer is None:
                return
            if answer.strip().lower() not in ("y", "yes"):
                return
            create_dir = True
        name = self._prompt(stdscr, f"Name [{os.path.basename(directory)}]: ")
        if name is None:
            return
        if not name:
            name = os.path.basename(directory)
        if self._run_cmd(stdscr, cmd_add, directory, name,
                         create_dir=create_dir):
            self._trigger_rebuild()

    def _do_rename(self, stdscr):
        p = self.projects[self.selected]
        new_name = self._prompt(stdscr, f"New name for '{p.name}': ")
        if not new_name:
            return
        tmux_cmd("set-option", "-wt", p.win_target, "@ccm_project", new_name)
        tmux_cmd("rename-window", "-t", p.win_target, new_name)
        self._show_message(stdscr, f"Renamed: {p.name} → {new_name}", 1)
        self._trigger_rebuild()

    def _do_remove(self, stdscr):
        p = self.projects[self.selected]
        # `ignore` is deliberately NOT an option here. It is a
        # reversible state, and putting it beside an irreversible
        # delete invites the confirmation for one to be pressed on
        # the way to the other. Naming it keeps it findable without
        # mixing the two.
        choice = self._prompt(
            stdscr,
            f"Remove '{p.name}'? [u]nregister (keep window) / "
            f"[d]elete (close it) / Esc  ")
        if not choice:
            return
        if choice.lower() == "u":
            ok = self._run_cmd(stdscr, cmd_unregister, p.name)
        elif choice.lower() == "d":
            # One more step than unregister gets, because this one
            # cannot be undone: the window closes and the session in
            # it ends. The menu route already confirms; a single
            # keypress being the harsher of the two paths was the
            # wrong way around.
            confirm = self._prompt(
                stdscr, f"Close {p.name}'s window and its session? [y/N]: ")
            if not confirm or confirm.lower() != "y":
                return
            ok = self._run_cmd(stdscr, cmd_remove, p.name)
        else:
            return
        if ok:
            self._trigger_rebuild()

    def _do_ignore_toggle(self, stdscr):
        """Toggle CCM_IGNORE on the selected project's window. Ignoring
        hides every claude pane of the window from ccm (state,
        session tracking, `ccm send`, idle auto-exit) and silences its
        hooks/notifications; un-ignoring restores it. The current
        `ignored_panes` count decides the direction."""
        p = self.projects[self.selected]
        if self._toggle_project_ignore(stdscr, p):
            self._trigger_rebuild()

    def _toggle_project_ignore(self, stdscr, project):
        """Confirm hiding a project; restoring it needs no confirmation."""
        if getattr(project, "ignored_panes", 0):
            return self._run_cmd(stdscr, cmd_unignore, project.name)
        confirm = self._prompt(
            stdscr, f"Ignore {project.name}? It stops tracking, sends, "
            "auto-exit and notifications. [y/N]: ")
        if confirm not in ("y", "Y"):
            self._show_message(stdscr, "Ignore cancelled.", 1)
            return False
        return self._run_cmd(stdscr, cmd_ignore, project.name)

    def _do_register(self, stdscr):
        # List untagged windows
        raw = tmux_cmd("list-windows", "-a", "-F",
                       "#{session_name}:#{window_index}\t#{window_name}\t#{@ccm_project}")
        if not raw:
            return
        untagged = []
        for line in raw.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 3 and not parts[2]:
                untagged.append((parts[0], parts[1]))
        if not untagged:
            self._show_message(stdscr, "No untagged windows", 1)
            return
        # Show list and ask for selection
        msg = "Untagged: " + ", ".join(f"{wt}({n})" for wt, n in untagged[:5])
        win = self._prompt(stdscr, f"{msg}\nWindow name/index: ")
        if not win:
            return
        name = self._prompt(stdscr, f"Project name [{win}]: ")
        if name is None:
            return
        if not name:
            name = win
        if self._run_cmd(stdscr, cmd_register, win, name):
            self._trigger_rebuild()

    def _select_current_project(self):
        """Select the opening window once, after the initial display order is set."""
        self.selected = 0
        try:
            session = get_session()
            index = tmux_cmd("display-message", "-p", "#{window_index}")
            if not session or not index:
                return
            target = f"{session}:{index}"
            self.selected = next(
                (i for i, p in enumerate(self.projects) if p.win_target == target),
                0,
            )
        except Exception:
            log_caught_exception("dashboard._select_current_project")

    def _set_projects_stable(self, projects):
        """Assign `self.projects` while (a) holding the row order stable
        for the dashboard's lifetime and (b) pinning the selection to
        the same project across refreshes.

        `build_project_list` re-sorts by state every refresh, so a
        project changing state (e.g. BUSY→IDLE, or a new PERMIT) would
        otherwise move rows under the cursor — and since `self.selected`
        is a positional index, the highlight would jump to a *different*
        project mid-interaction. Here the order is frozen at first build
        (state-sorted, as `build_project_list` returns it): later
        refreshes follow that frozen order, projects opened while the
        dashboard is up append at the end, and vanished ones drop out.
        Each popup open is a fresh process, so reopening re-decides the
        order from current state — matching "decide order on show, keep
        it while shown". A state change still updates a row's icon/color
        in place; it just doesn't reshuffle.

        Caller holds `self.lock` (except the single-threaded initial
        build in `__init__`)."""
        # Remember the cursor's project by identity (None if the
        # selection is on a background-session row, past the projects).
        prev_target = None
        if 0 <= self.selected < len(self.projects):
            prev_target = self.projects[self.selected].win_target

        if not self._display_order:
            # First build establishes the frozen order.
            self._display_order = [p.win_target for p in projects]
            ordered = list(projects)
        else:
            rank = {t: i for i, t in enumerate(self._display_order)}
            known = sorted((p for p in projects if p.win_target in rank),
                           key=lambda p: rank[p.win_target])
            # Projects opened since the freeze: keep their (state-sorted)
            # relative order and append to the frozen order so they stay
            # put on subsequent refreshes. Stale entries in
            # `_display_order` (a project that vanished) are harmless —
            # they simply match nothing, and a project that flickers out
            # and back returns to its original slot.
            new = [p for p in projects if p.win_target not in rank]
            self._display_order.extend(p.win_target for p in new)
            ordered = known + new

        self.projects = ordered

        # Re-pin the cursor: the project's index may have shifted if a
        # project above it appeared or vanished.
        if prev_target is not None:
            for i, p in enumerate(ordered):
                if p.win_target == prev_target:
                    self.selected = i
                    break

    def _trigger_rebuild(self):
        """Force a rebuild in the background thread."""
        projects = build_project_list(fast=True)
        with self.lock:
            self._set_projects_stable(projects)
            self.data_dirty = True

    def _prompt(self, stdscr, prompt_text, path_completion=False):
        """Show prompt, return input string. Returns None on Escape.
        If path_completion=True, Tab key completes file/directory paths.
        """
        curses.curs_set(1)
        height, width = stdscr.getmaxyx()
        row = height - 1
        prompt_col = 2 + display_width(prompt_text)
        stdscr.timeout(-1)  # Block for input

        buf = ""

        def _redraw():
            stdscr.move(row, 0)
            stdscr.clrtoeol()
            self._addstr(stdscr, row, 0, f"  {prompt_text}", curses.color_pair(C_DIM))
            self._addstr(stdscr, row, prompt_col, buf, 0)
            cursor_col = prompt_col + display_width(buf)
            try:
                stdscr.move(row, cursor_col)
            except curses.error:
                pass

        _redraw()
        stdscr.refresh()

        while True:
            try:
                wch = stdscr.get_wch()
            except curses.error:
                continue

            # get_wch returns str for characters, int for special keys
            if isinstance(wch, int):
                if wch == 27:  # Escape
                    curses.curs_set(0)
                    stdscr.timeout(50)
                    return None
                elif wch in (curses.KEY_ENTER, 10, 13):
                    curses.curs_set(0)
                    stdscr.timeout(50)
                    return buf
                elif wch in (curses.KEY_BACKSPACE, 127, 8):
                    if buf:
                        buf = self._strip_last_grapheme(buf)
                        _redraw()
                        stdscr.refresh()
                elif wch == 9 and path_completion:  # Tab
                    buf = self._complete_path(buf, stdscr, row, prompt_col)
                    _redraw()
                    stdscr.refresh()
            else:
                # wch is a str (single character, may be wide/CJK)
                if wch == "\x1b":  # Escape
                    curses.curs_set(0)
                    stdscr.timeout(50)
                    return None
                elif wch in ("\n", "\r"):
                    curses.curs_set(0)
                    stdscr.timeout(50)
                    return buf
                elif wch in ("\x7f", "\b"):  # Backspace
                    if buf:
                        buf = self._strip_last_grapheme(buf)
                        _redraw()
                        stdscr.refresh()
                elif wch == "\t" and path_completion:
                    buf = self._complete_path(buf, stdscr, row, prompt_col)
                    _redraw()
                    stdscr.refresh()
                elif not wch.isspace() or wch == " ":
                    # Accept any printable character (ASCII, CJK, etc.)
                    if unicodedata.category(wch)[0] != "C":
                        buf += wch
                        _redraw()
                        stdscr.refresh()

        stdscr.timeout(50)
        curses.curs_set(0)
        return buf

    def _complete_path(self, text, stdscr, row, prompt_col):
        """Complete file/directory path from text. Returns completed text."""
        expanded = os.path.expanduser(text)

        if os.path.isdir(expanded) and not expanded.endswith("/"):
            return text + "/"

        parent = os.path.dirname(expanded) or "."
        prefix = os.path.basename(expanded)

        try:
            entries = os.listdir(parent)
        except OSError:
            return text

        # Filter matching entries
        matches = sorted([e for e in entries if e.startswith(prefix)])
        if not matches:
            return text

        if len(matches) == 1:
            completed = os.path.join(os.path.dirname(text), matches[0])
            # Re-add ~ prefix if original had it
            if text.startswith("~"):
                completed = "~/" + os.path.relpath(
                    os.path.join(parent, matches[0]),
                    os.path.expanduser("~")
                )
            if os.path.isdir(os.path.expanduser(completed)):
                completed += "/"
            return completed

        # Multiple matches: find common prefix
        common = os.path.commonprefix(matches)
        if len(common) > len(prefix):
            completed = os.path.join(os.path.dirname(text), common)
            if text.startswith("~"):
                completed = "~/" + os.path.relpath(
                    os.path.join(parent, common),
                    os.path.expanduser("~")
                )
            return completed

        # Show candidates on the line above
        height, width = stdscr.getmaxyx()
        display_row = row - 1
        if display_row >= 0:
            candidates = "  ".join(matches[:10])
            if len(matches) > 10:
                candidates += f"  (+{len(matches) - 10} more)"
            stdscr.move(display_row, 0)
            stdscr.clrtoeol()
            self._addstr(stdscr, display_row, 2, candidates, curses.color_pair(C_DIM))

        return text

    def _show_message(self, stdscr, msg, duration=1):
        self._msg_text = msg
        self._msg_expires = time.monotonic() + duration

    def _run_cmd(self, stdscr, func, *args, **kwargs):
        """Run a ccm_core command in raise_on_die() context.

        Errors are raised as CCMError (instead of stderr + sys.exit) and
        shown via _show_message. Also suppresses stdout/stderr (ccm_info /
        ccm_warn success messages) so they cannot bleed through the curses
        display and desync its differential redraw model.
        Returns True on success, False on error.
        """
        sink = io.StringIO()
        try:
            with raise_on_die(), \
                 contextlib.redirect_stdout(sink), \
                 contextlib.redirect_stderr(sink):
                func(*args, **kwargs)
            return True
        except CCMError as e:
            # Join multi-line messages, collapse whitespace, truncate
            msg = " | ".join(line.strip() for line in str(e).splitlines() if line.strip())
            if len(msg) > MSG_MAX_LEN:
                msg = msg[: MSG_MAX_LEN - 3] + "..."
            if msg:
                self._show_message(stdscr, msg, 3)
            return False

    @staticmethod
    def _preview_sound(sound_name):
        """Play a macOS system sound directly for instant preview."""
        sound_file = f"/System/Library/Sounds/{sound_name}.aiff"
        try:
            subprocess.Popen(
                ["afplay", sound_file],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass

    def _refresh_loop(self):
        """Background thread: periodic state refresh."""
        # First refresh: fast (no git/port)
        time.sleep(0.3)
        try:
            projects = build_project_list(fast=False)
            # Skip the bg-section roster read when the section is
            # hidden — users who never use agent view should pay zero
            # I/O for it. When the user toggles bg on with `b` (or via
            # the menu), the toggle handler does an immediate
            # synchronous fetch so the first render after toggle is
            # already populated, no flicker.
            #
            # `bg_visible` is toggled by the main thread (under the
            # lock); snapshot it under the lock here too so this
            # thread never reads a torn / mid-toggle value.
            with self.lock:
                bg_visible = self.bg_visible
            bg_sessions = self._fetch_bg_sessions() if bg_visible else []
            handoff_warnings = self._fetch_handoff_warnings(
                projects, bg_sessions if bg_visible else None)
            with self.lock:
                self._set_projects_stable(projects)
                self.bg_sessions = bg_sessions
                self.handoff_warnings = handoff_warnings
                self.initial_load = False
                self.data_dirty = True
        except Exception:
            log_caught_exception("dashboard._refresh_loop:initial")
            self.initial_load = False

        # Subsequent refreshes: hybrid loop. Fast ticks sample the
        # pushed-state channel between full (slow-path) detection
        # passes — see FAST_TICK_INTERVAL for the design rationale.
        last_full = time.monotonic()
        while self.running:
            tick = self._refresh_pace()[0]
            time.sleep(tick)
            if not self.running:
                return
            self._fast_tick()
            if time.monotonic() - last_full < self._refresh_pace()[1]:
                continue
            last_full = time.monotonic()
            try:
                # While a popup runs its own detection, a docked dashboard
                # reads the states it writes rather than detecting twice.
                projects = build_project_list(
                    fast=self.docked and popup_dashboard_alive())
                with self.lock:
                    bg_visible = self.bg_visible
                bg_sessions = (
                    self._fetch_bg_sessions() if bg_visible else []
                )
                handoff_warnings = self._fetch_handoff_warnings(
                    projects, bg_sessions if bg_visible else None)
                with self.lock:
                    self._set_projects_stable(projects)
                    self.bg_sessions = bg_sessions
                    self.handoff_warnings = handoff_warnings
                    self.data_dirty = True
                # Refresh preview content if enabled
                if self.preview_enabled and self.mode == "dashboard":
                    self._last_preview_target = ""  # Force refresh
            except Exception:
                log_caught_exception("dashboard._refresh_loop")

    def _note_input(self):
        self._last_input = time.monotonic()

    def _idle(self):
        return time.monotonic() - getattr(self, "_last_input", 0.0) > IDLE_AFTER_SEC

    def _refresh_pace(self):
        """(fast tick, full refresh) intervals: slower once nobody has
        pressed a key for IDLE_AFTER_SEC."""
        if self._idle():
            return IDLE_FAST_TICK_INTERVAL, IDLE_REFRESH_INTERVAL
        return FAST_TICK_INTERVAL, REFRESH_INTERVAL

    def _scan_pushed_states(self):
        """One `list-windows` subprocess reading the pushed-state
        channel: `{win_target: @ccm_prev_state}` for every ccm
        window. `@ccm_prev_state` is written instantly by the Claude
        Code hooks (`ccm_write_signal`) and by the slow path's own
        commits, so its TRANSITIONS are exactly the events worth
        reacting to between full detection passes."""
        raw = tmux_cmd(
            "list-windows", "-a", "-F",
            "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_prev_state}",
        )
        out = {}
        if not raw:
            return out
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or not parts[1]:
                continue
            out[parts[0]] = parts[2]
        return out

    def _fast_tick(self):
        """Overlay pushed-state TRANSITIONS onto the displayed list.

        Transition-gated on purpose: the overlay applies only to
        windows whose pushed value CHANGED since the previous tick —
        a fresh hook write or a slow-path commit. Reacting to the
        absolute pushed value instead would re-fight the slow path
        wherever the two legitimately diverge (HOLD_NO_WRITE rules
        like the startup transient deliberately display a state
        WITHOUT committing it to @ccm_prev_state), producing a
        2-second flicker cycle. Steady divergence is the slow path's
        call to make; this tick only relays fresh events.

        Runs on the refresh thread (never concurrent with the slow
        refresh); mutates project states under the lock. Errors are
        swallowed-but-logged like every other refresh-path failure —
        a broken fast tick must degrade to plain 2 s polling, not
        kill the dashboard.
        """
        try:
            pushed = self._scan_pushed_states()
        except Exception:
            log_caught_exception("dashboard._fast_tick")
            return
        prev = self._pushed_states
        self._pushed_states = pushed
        if not prev:
            return  # first sample is baseline only
        changed = {t: s for t, s in pushed.items()
                   if s and prev.get(t) != s}
        if not changed:
            return
        with self.lock:
            dirty = False
            for p in self.projects:
                s = changed.get(p.win_target)
                if s and s != p.state:
                    p.state = s
                    dirty = True
            if dirty:
                self.data_dirty = True

    def _fetch_handoff_warnings(self, projects, bg_sessions=None):
        """Hand-off notices for the project list, read on the refresh
        thread. Independent of the bg-section toggle: the reader is
        about to attach, and the empty session that would follow has
        no other explanation on this screen. When the bg section is
        shown, its roster snapshot from this same cycle is reused
        rather than read again. Never raises."""
        try:
            return ccm_agentview.continue_blocker_warnings(projects, bg_sessions)
        except Exception:
            log_caught_exception("dashboard._fetch_handoff_warnings")
            return []

    def _fetch_bg_sessions(self):
        """Read the agent-view roster. Returns `[]` on any failure
        (missing daemon, malformed file, etc.). Bounded I/O cost —
        9 small JSON files at most in practice."""
        try:
            return ccm_agentview.list_bg_sessions()
        except Exception:
            log_caught_exception("dashboard._fetch_bg_sessions")
            return []

    # Items whose label ends in `: <current value>`.
    _MENU_SETTINGS = frozenset({
        "status_mode", "auto_restore", "idle_timeout", "preview_toggle",
        "preview_position", "dock_mode", "dock_close", "bg_section", "auto_start", "notify",
        "notify_sound", "sound_name"})
    # Preview lines that name a command or a current value.
    _HELP_COMMAND_PREFIXES = ("Same as: ", "CLI: ")
    _HELP_VALUE_PREFIXES = ("Current: ", "現在値: ")

# ─── PID file management ───

def _pid_is_dashboard(pid):
    """Best-effort identity check: True only when the live process at
    `pid` looks like a ccm dashboard (its command line mentions
    `dashboard.py`).

    Exists so `acquire_pidfile` never SIGKILLs an unrelated process:
    a pidfile left behind by a crashed dashboard holds a stale PID,
    and the OS may have since recycled that PID for something else.
    macOS has no /proc, so identity is probed via `ps`. Any failure
    (ps missing, pid gone, timeout) returns False — the safe
    direction, since NOT killing merely leaves a stale dashboard
    running while a wrong kill is unrecoverable.

    Kept as a separate module-level function so tests can stub it
    (the conftest `block_live_subprocess` guard forbids real `ps`
    invocations from tests)."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return False
    return "dashboard.py" in out


def popup_dashboard_alive():
    """True while a popup dashboard (or menu / tree / filter) runs."""
    try:
        pid = int(open(os.path.join(CCM_TMP_DIR, "dashboard.pid"), encoding="utf-8").read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def acquire_pidfile(name="dashboard.pid"):
    """Replace a previous dashboard of the same kind. The docked dashboard
    keeps its own file, so opening a popup (menu, tree, filter) never
    ends it, and a new dock replaces only an old dock."""
    pidfile = os.path.join(CCM_TMP_DIR, name)
    if not ccm_core.secure_tmp_root(CCM_TMP_DIR):
        sys.exit(f"Refusing to use {CCM_TMP_DIR}: it must be a directory owned by you, not a symlink")
    # Kill existing
    if os.path.exists(pidfile):
        try:
            old_pid = int(open(pidfile, encoding="utf-8").read().strip())
            # Verify the stale PID is actually a dashboard before
            # signalling it. After an unclean exit the pidfile
            # survives and the PID may have been recycled for an
            # unrelated process — killing it blind would SIGTERM /
            # SIGKILL whatever now owns that PID.
            if old_pid != os.getpid() and _pid_is_dashboard(old_pid):
                os.kill(old_pid, signal.SIGTERM)
                time.sleep(0.2)
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass
    with open(pidfile, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return pidfile


def main():
    # Parse --mode and --search arguments
    mode = "dashboard"
    start_in_search = False
    docked = False
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--mode" and i < len(sys.argv):
            mode = sys.argv[i + 1]
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
        elif arg == "--search":
            start_in_search = True
        elif arg == "--docked":
            docked = True

    pidfile = acquire_pidfile("dashboard-dock.pid" if docked else "dashboard.pid")
    try:
        dashboard = Dashboard(initial_mode=mode, start_in_search=start_in_search, docked=docked)
        curses.wrapper(dashboard.run)
    except Exception:
        # Log errors for debugging
        import traceback
        err_file = os.path.join(CCM_TMP_DIR, "dashboard-error.log")
        with open(err_file, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        raise
    finally:
        try:
            os.unlink(pidfile)
        except OSError:
            pass


if __name__ == "__main__":
    main()
