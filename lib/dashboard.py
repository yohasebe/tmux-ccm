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
from ccm_constants import CCM_TMP_DIR
import ccm_core  # noqa: E402
from ccm_core import (
    CCMError,
    build_project_list,
    hooks_configured,
    raise_on_die,
    tmux_cmd,
    touch_popup_session,
)
import ccm_menu_help
from ccm_render import truncate_to_width

# Max characters shown in the single-line message area
MSG_MAX_LEN = 200

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
)
from dashboard_actions import ActionsMixin  # noqa: E402
from dashboard_input import InputMixin  # noqa: E402
from dashboard_menu import MenuMixin  # noqa: E402
from dashboard_refresh import RefreshMixin  # noqa: E402
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


class Dashboard(RefreshMixin, InputMixin, ActionsMixin, RenderMixin, MenuMixin,
                SearchMixin, TreeMixin, LifecycleActions):
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
