#!/usr/bin/env python3
"""ccm Dashboard — Python curses implementation for responsive TUI."""

import contextlib
import curses
import io
import locale
import os
import platform
import re
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
    CCM_GIT_CACHE_DIR,
    CCM_PORT_CACHE_DIR,
    CCM_ROOT,
    CCM_SNAPSHOT_DIR,
    CCM_TMP_DIR,
    COMPLETED_AT_TIMEOUT,
    IDLE_EXIT_TIMEOUT,
    PERMISSION_MODE_WARN,
    STATE_ICONS,
    STATE_PRIORITY,
    permission_mode_label,
)
from ccm_core import (
    CCMError,
    build_project_list,
    get_session,
    hooks_configured,
    log_caught_exception,
    md5_hash,
    ps_snapshot,
    raise_on_die,
    read_cache_file,
    save_tmux_conf_setting,
    tmux_cmd,
    touch_popup_session,
)
from ccm_pane_state import enumerate_window_panes
import ccm_agentview
from ccm_window import auto_start_claude, reset_window_after_attach
from ccm_canaries import (
    disable_all_hooks_warning,
    errors_log_burst_warning,
    hook_silence_warnings,
    hooks_log_warning,
    managed_hooks_only_warning,
    shell_cluster_warnings,
)
from ccm_commands import (
    cmd_add,
    cmd_ignore,
    cmd_register,
    cmd_remove,
    cmd_unignore,
    cmd_unregister,
)
from ccm_snapshot import cmd_snapshot_load, cmd_snapshot_save
from ccm_render import (
    display_width,
    format_dir,
    format_elapsed,
    signal_age_suffix,
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

# Visual cols reserved at the right edge of each project row for the
# `* elapsed` marker (`* ` + 3-char padded elapsed + 1 col margin).
# Always reserved (even on rows without elapsed) so the path column's
# right-clip width stays constant across refresh ticks, which in turn
# keeps the path's horizontal position stable as the marker appears,
# disappears, and ticks across 1↔2 digit boundaries. Paired with
# `format_elapsed`'s 3-char right-padding in lib/ccm_render.py.
ELAPSED_RIGHT_SLOT = 6

_IS_MACOS = platform.system() == "Darwin"

# Color pair IDs (curses-specific, stay in dashboard.py)
C_PERMIT = 1
C_BUSY = 2
C_COMPLETED = 3
C_IDLE = 4
C_SHELL = 5
C_DIM = 6
C_CYAN = 7
C_YELLOW = 8
C_SYNCING = 9

STATE_COLOR_PAIR = {
    "PERMIT": C_PERMIT, "BUSY": C_BUSY,
    "IDLE": C_IDLE, "SHELL": C_SHELL, "DOWN": C_SHELL,
}


# ─── Dashboard ───

class Dashboard:
    # Keys that only adjust a selection index. The main loop coalesces
    # these across terminal auto-repeat so a held arrow key renders
    # once at the end of the burst instead of once per keystroke.
    # All three modes (dashboard / tree / menu) share the same set
    # because each mode's ↑/↓/j/k handler is a safe selection-only
    # update with no side effects beyond `self.selected`,
    # `self.tree_selected`, or `self.menu_selected`.
    NAV_KEYS = frozenset((curses.KEY_UP, curses.KEY_DOWN, ord("j"), ord("k")))

    def __init__(self, initial_mode="dashboard", start_in_search=False):
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

        # Instant first paint from cached state. This first build
        # establishes the frozen display order for the popup's lifetime.
        self._set_projects_stable(build_project_list(fast=True))
        # Fetch bg sessions synchronously on initial paint when the
        # section is visible, so the user doesn't see "Background
        # sessions (0)" flicker into the actual list 300 ms later.
        # Cheap on a non-Dropbox path (~/.claude is local) — a single
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
            if action == "attached":
                return
            self._render_current(stdscr)

        # Main event loop
        while self.running:
            touch_popup_session()

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
            if action in ("quit", "attached"):
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
                if action in ("quit", "attached"):
                    break

            self._render_current(stdscr)

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

    def _resolve_preview_pane(self, win_target):
        """Return the pane to preview for a window: the TRACKED claude
        pane, not just whichever pane happens to be focused.

        `capture-pane -t <window>` grabs the window's ACTIVE pane, so
        in a split window with claude in a non-active pane the preview
        would show the wrong thing — a shell, an editor, or a
        CCM_IGNORE'd sidekick — instead of the session ccm is tracking.
        Resolve like `ccm send`'s delivery pane, but never fail (this
        is a read-only preview):
          - the active pane if it hosts a non-ignored claude (unchanged
            in the common single-pane / claude-focused case);
          - else the single (or first) non-ignored claude pane;
          - else the window target (active pane) as a fallback.
        CCM_IGNORE'd panes are always excluded — the window's tracked
        state is the primary session, so the preview shows that, never
        a hidden sidekick."""
        try:
            ps_lines = ps_snapshot().strip().split("\n")
        except Exception:
            return win_target
        # Non-ignored panes that host claude.
        live = [p for p in enumerate_window_panes(win_target, ps_lines)
                if not p.ignored and p.claude_pid]
        active = next((p.pane_id for p in live if p.active), None)
        if active:
            return active
        if live:
            return live[0].pane_id
        return win_target

    def _update_preview(self):
        """Fetch preview content for the selected project."""
        with self.lock:
            projects = list(self.projects)
        if not projects or self.selected >= len(projects):
            self.preview_cache = ""
            self._preview_lines = []
            return
        p = projects[self.selected]
        if p.win_target == self._last_preview_target and self._preview_lines:
            return  # Already cached for this target
        self._last_preview_target = p.win_target
        # Preview the tracked claude pane, not just the focused pane
        # (see _resolve_preview_pane).
        pane = self._resolve_preview_pane(p.win_target)
        # Capture with -e for ANSI escape sequences (color support)
        # Try normal screen first, then alternate screen (CLAUDE_CODE_NO_FLICKER=1)
        raw = tmux_cmd("capture-pane", "-e", "-t", pane, "-p", "-S", "-50")
        if not raw or not raw.strip():
            raw = tmux_cmd("capture-pane", "-e", "-a", "-t", pane, "-p", "-S", "-50")
        if raw:
            raw = self._strip_osc8_hyperlinks(raw)
        self.preview_cache = raw if raw else "(no content)"
        self._preview_lines = self.preview_cache.split("\n") if self.preview_cache else []

    # ANSI SGR to curses attribute mapping
    _ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')

    # OSC 8 hyperlink: \e]8;PARAMS;URL\e\\TEXT\e]8;;\e\\
    # (terminator can be ESC \ or BEL). Curses does not interpret OSC 8,
    # so we replace each whole sequence with the visible TEXT — the
    # link target is dropped, the user-visible label is kept.
    _OSC8_RE = re.compile(
        r'\x1b\]8;[^;]*;[^\x07\x1b]*(?:\x07|\x1b\\)'
        r'(.*?)'
        r'\x1b\]8;;(?:\x07|\x1b\\)',
        re.DOTALL,
    )

    @classmethod
    def _strip_osc8_hyperlinks(cls, text: str) -> str:
        """Replace OSC 8 hyperlink sequences with their visible label.

        Claude Code emits OSC 8 hyperlinks (e.g. for branch / PR
        references). `capture-pane -e` includes the raw escape codes,
        but curses cannot render them, so they show up in the
        dashboard preview as `^]8;id=...;URL ... ^]8;;`. Stripping
        them here keeps the link's visible text and drops the wrapper.
        """
        # Replace complete sequences first.
        text = cls._OSC8_RE.sub(r'\1', text)
        # Defensive: drop any orphaned OSC 8 starts/terminators that
        # remain (e.g. when the capture window cuts off mid-sequence).
        text = re.sub(r'\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)?', '', text)
        return text

    # Cache for dynamically allocated curses color pairs
    _color_pair_cache = {}
    _next_pair_id = 50

    @classmethod
    def _get_color_pair(cls, fg):
        """Get or create a curses color pair for a foreground color."""
        if fg < 0:
            return 0
        if fg in cls._color_pair_cache:
            return curses.color_pair(cls._color_pair_cache[fg])
        pair_id = cls._next_pair_id
        cls._next_pair_id += 1
        try:
            curses.init_pair(pair_id, fg, -1)
            cls._color_pair_cache[fg] = pair_id
            return curses.color_pair(pair_id)
        except (curses.error, ValueError):
            return 0

    @classmethod
    def _ansi_to_curses_attr(cls, codes):
        """Convert ANSI SGR code list to curses attribute.
        Handles: basic colors (30-37), 256-color (38;5;N), bold, dim, etc.
        """
        attr = 0
        fg = -1
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                attr = 0; fg = -1
            elif c == 1:
                attr |= curses.A_BOLD
            elif c == 2:
                attr |= curses.A_DIM
            elif c == 4:
                attr |= curses.A_UNDERLINE
            elif c == 7:
                attr |= curses.A_REVERSE
            elif 30 <= c <= 37:
                fg = c - 30
            elif c == 39:
                fg = -1
            elif c == 38:
                # Extended foreground: 38;5;N (256-color) or 38;2;R;G;B (RGB)
                if i + 1 < len(codes) and codes[i + 1] == 5:
                    if i + 2 < len(codes):
                        fg = codes[i + 2]  # 256-color index
                        i += 2
                elif i + 1 < len(codes) and codes[i + 1] == 2:
                    if i + 4 < len(codes):
                        # RGB: find nearest 256-color
                        r, g, b = codes[i + 2], codes[i + 3], codes[i + 4]
                        fg = 16 + (round(r / 255 * 5) * 36 + round(g / 255 * 5) * 6 + round(b / 255 * 5))
                        i += 4
            elif 90 <= c <= 97:
                fg = c - 90 + 8  # Bright colors
            i += 1
        if fg >= 0:
            attr |= cls._get_color_pair(fg)
        return attr

    def _render_preview(self, stdscr, start_col, start_row, panel_width, panel_height):
        """Render preview panel with ANSI color support."""
        # Draw vertical separator
        for r in range(start_row, start_row + panel_height):
            try:
                stdscr.addch(r, start_col, "│", curses.color_pair(C_DIM))
            except curses.error:
                pass

        lines = getattr(self, '_preview_lines', [])
        visible = lines[-(panel_height):]
        content_col = start_col + 2
        max_w = panel_width - 3

        for i, line in enumerate(visible):
            r = start_row + i
            if r >= start_row + panel_height:
                break

            # Parse ANSI escape sequences and render with colors
            col = content_col
            display_used = 0
            cur_attr = 0
            pos = 0

            while pos < len(line) and display_used < max_w:
                m = self._ANSI_RE.match(line, pos)
                if m:
                    # Parse SGR codes
                    code_str = m.group(1)
                    codes = [int(c) for c in code_str.split(";") if c.isdigit()] if code_str else [0]
                    cur_attr = self._ansi_to_curses_attr(codes)
                    pos = m.end()
                else:
                    ch = line[pos]
                    ch_w = display_width(ch)
                    if display_used + ch_w > max_w:
                        break
                    try:
                        stdscr.addstr(r, col, ch, cur_attr)
                    except curses.error:
                        pass
                    col += ch_w
                    display_used += ch_w
                    pos += 1

    def _render_preview_bottom(self, stdscr, start_row, panel_width, panel_height):
        """Render preview panel at the bottom with horizontal separator."""
        # Draw horizontal separator
        sep_str = "─" * (panel_width - 1)
        try:
            stdscr.addstr(start_row, 0, sep_str, curses.color_pair(C_DIM))
        except curses.error:
            pass

        lines = getattr(self, '_preview_lines', [])
        visible = lines[-(panel_height - 1):]  # -1 for separator
        max_w = panel_width - 2

        for i, line in enumerate(visible):
            r = start_row + 1 + i
            if r >= start_row + panel_height:
                break

            col = 1
            display_used = 0
            cur_attr = 0
            pos = 0

            while pos < len(line) and display_used < max_w:
                m = self._ANSI_RE.match(line, pos)
                if m:
                    code_str = m.group(1)
                    codes = [int(c) for c in code_str.split(";") if c.isdigit()] if code_str else [0]
                    cur_attr = self._ansi_to_curses_attr(codes)
                    pos = m.end()
                else:
                    ch = line[pos]
                    ch_w = display_width(ch)
                    if display_used + ch_w > max_w:
                        break
                    try:
                        stdscr.addstr(r, col, ch, cur_attr)
                    except curses.error:
                        pass
                    col += ch_w
                    display_used += ch_w
                    pos += 1

    # Background-session column colour table. Mirrors the bg state
    # colour mapping in ccm_render._BG_STATE_COLOR but renders via
    # curses color-pair IDs (ANSI escapes would not work here).
    _BG_STATE_PAIR = {
        "NEEDS": C_PERMIT,       # bold yellow — wants attention
        "WORKING": C_CYAN,
        "IDLE": C_IDLE,
        "DONE": C_DIM,
        "FAILED": C_BUSY,        # use BUSY's salmon for failed
        "UNKNOWN": C_DIM,
    }

    def _render_bg_section(self, stdscr, start_row, bg_sessions,
                           list_height, effective_width):
        """Render the background-sessions block starting at `start_row`.

        Returns the next available row after the block (so the help-
        line layout can resume below it). When the daemon isn't
        running, the header is shown as a gentle hint rather than
        an empty list — the user pressed `b` (or set the always
        flag) intentionally and deserves feedback.
        """
        # Reserve space for the help / footer rows below.
        max_row = list_height - 3
        if start_row >= max_row:
            return start_row

        row = start_row + 1  # spacer
        if row >= max_row:
            return start_row

        header = f"Background sessions ({len(bg_sessions)})"
        self._addstr(stdscr, row, 2, header,
                     curses.A_BOLD | curses.color_pair(C_DIM))
        row += 1

        if not bg_sessions:
            if row < max_row:
                hint = (
                    "  (no active agent-view sessions)"
                    if ccm_agentview.daemon_running()
                    else "  (Claude Code agent-view daemon is not running)"
                )
                self._addstr(stdscr, row, 2, hint, curses.color_pair(C_DIM))
                row += 1
            return row

        # Fixed columns for the bg rows. Designed to align with the
        # project rows above without forcing them into a sub-grid.
        COL_SHORT = 4
        COL_STATE = COL_SHORT + 9          # "12345678 " = 9
        COL_NAME = COL_STATE + 11          # "✽ WORKING  " = 11 visible cols
        # Reserve a few cols for age before the cwd.
        AGE_W = 6

        # Unified selection: bg rows occupy indices [n_projects,
        # n_projects + len(bg_sessions)) so the ▶ marker can move
        # smoothly between the two sections with ↑/↓.
        n_projects = len(self.projects)
        for bg_i, s in enumerate(bg_sessions):
            if row >= max_row:
                break

            is_selected = (self.selected - n_projects) == bg_i
            prefix = "  ▶ " if is_selected else "    "
            self._addstr(stdscr, row, 0, prefix, curses.color_pair(C_DIM))
            self._addstr(stdscr, row, COL_SHORT, s.short, curses.color_pair(C_DIM))

            icon = ccm_agentview.STATE_ICONS.get(s.state, "?")
            state_label = f"{icon} {s.state}"
            self._addstr(stdscr, row, COL_STATE, state_label,
                         curses.color_pair(self._BG_STATE_PAIR.get(s.state, C_DIM)))

            name = s.name or "(unnamed)"
            self._addstr(stdscr, row, COL_NAME, name, 0)

            name_w = display_width(name)
            after_name = COL_NAME + name_w + 1

            age_str = ""
            if s.created_at:
                age = int(time.time() - s.created_at)
                if age < 60:
                    age_str = f"{age}s"
                elif age < 3600:
                    age_str = f"{age // 60}m"
                elif age < 86400:
                    age_str = f"{age // 3600}h"
                else:
                    age_str = f"{age // 86400}d"
            if age_str:
                self._addstr(stdscr, row, after_name, age_str,
                             curses.color_pair(C_DIM))

            cwd_col = after_name + AGE_W
            if s.cwd and cwd_col < effective_width - 4:
                cwd_str = format_dir(s.cwd, cwd_col, effective_width)
                if cwd_str:
                    self._addstr(stdscr, row, cwd_col, cwd_str,
                                 curses.color_pair(C_DIM))

            row += 1

        return row

    MIN_HEIGHT = 10
    MIN_WIDTH = 40

    def render(self, stdscr):
        try:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            # Clamp selection into the current visible row count.
            # Background sessions can disappear between refresh ticks
            # (settled / stopped externally), which would otherwise
            # leave `self.selected` pointing past the last bg row and
            # silently hide the ▶ marker until the user pressed a
            # nav key.
            with self.lock:
                n_proj = len(self.projects)
                bg_count = len(self.bg_sessions) if self.bg_visible else 0
            total = n_proj + bg_count
            if total > 0 and self.selected >= total:
                self.selected = total - 1
            if self.selected < 0:
                self.selected = 0

            if height < self.MIN_HEIGHT or width < self.MIN_WIDTH:
                msg = f"Terminal too small ({width}x{height})"
                try:
                    stdscr.addstr(0, 0, msg[:width - 1])
                except curses.error:
                    pass
                stdscr.refresh()
                return

            # Preview panel layout
            preview_width = 0
            preview_col = 0
            preview_height = 0
            preview_row = 0
            list_width = width
            list_height = height
            if self.preview_enabled and self.mode == "dashboard":
                if self.preview_position == "right" and width >= 80:
                    preview_width = min(width // 2, width - 40)
                    list_width = width - preview_width - 1
                    preview_col = list_width
                    preview_height = height - 1
                elif self.preview_position == "bottom" and height >= 20:
                    preview_height = min(height // 2, height - 10)
                    list_height = height - preview_height - 1
                    preview_row = list_height
                    preview_width = width

            # Store list width for _addstr clipping in list area
            self._render_max_col = list_width if preview_width > 0 else 0

            row = 0

            # Header
            if self.initial_load:
                self._addstr(stdscr, row, 2, "Syncing...", curses.color_pair(C_SYNCING))
            else:
                session = get_session()
                if session and not session.isdigit():
                    header = f"{session} — {len(self.projects)} project(s)"
                else:
                    header = f"{len(self.projects)} project(s)"
                self._addstr(stdscr, row, 2, header, curses.color_pair(C_DIM))
            row += 1

            # Snapshot the project list once so the global banners and
            # the per-project canary below see the same data.
            with self.lock:
                projects = list(self.projects)

            # Hooks: OFF banner (shown once above project list)
            if not self.hooks_on:
                banner = "⚠ Hooks not installed — run 'ccm setup-hooks' for accurate state detection"
                self._addstr(stdscr, row, 2, banner, curses.color_pair(C_YELLOW))
                row += 1

            # Hooks log canary — only shown if size crosses threshold
            log_warning = hooks_log_warning()
            if log_warning:
                self._addstr(stdscr, row, 2, "⚠ " + log_warning, curses.color_pair(C_YELLOW))
                row += 1

            # disableAllHooks canary — silent failure mode if set
            disable_warning = disable_all_hooks_warning()
            if disable_warning:
                self._addstr(stdscr, row, 2, "⚠ " + disable_warning, curses.color_pair(C_YELLOW))
                row += 1

            # allowManagedHooksOnly canary — blocks all user hooks
            managed_warning = managed_hooks_only_warning()
            if managed_warning:
                self._addstr(stdscr, row, 2, "⚠ " + managed_warning, curses.color_pair(C_YELLOW))
                row += 1

            # Per-project SHELL cluster canary (#48069 silent-exit regression)
            for cluster_msg in shell_cluster_warnings(projects):
                self._addstr(stdscr, row, 2, "⚠ " + cluster_msg, curses.color_pair(C_YELLOW))
                row += 1

            # Hook-silence canary — opt-in (@ccm-hook-silence), so the
            # loop is empty for default users and only lights up for an
            # operator who asked to watch it during calibration.
            for silence_msg in hook_silence_warnings(projects):
                self._addstr(stdscr, row, 2, "⚠ " + silence_msg, curses.color_pair(C_YELLOW))
                row += 1

            # Silent-exception burst canary — surfaces poll-cycle bugs
            # within minutes rather than the operator having to think
            # to run `ccm errors`.
            burst_warning = errors_log_burst_warning()
            if burst_warning:
                self._addstr(stdscr, row, 2, "⚠ " + burst_warning, curses.color_pair(C_YELLOW))
                row += 1

            if not projects:
                self._addstr(stdscr, row + 1, 4, "No active projects.", curses.color_pair(C_DIM))
                row += 3
            else:
                # Scrolling: ensure selected project is visible
                visible_lines = list_height - 5  # header + help (up to 2 lines) + footer
                scroll_offset = getattr(self, '_scroll_offset', 0)
                if self.selected >= scroll_offset + visible_lines:
                    scroll_offset = self.selected - visible_lines + 1
                if self.selected < scroll_offset:
                    scroll_offset = self.selected
                if scroll_offset < 0:
                    scroll_offset = 0
                self._scroll_offset = scroll_offset

                # Calculate column widths for alignment
                max_idx_w = max((len(p.win_idx) for p in projects), default=1) + 1  # "#N"
                max_state_w = 8  # "● PERMIT" = 8

                # Fixed column positions for idx / state / name
                # only. Each row's annotation cluster ([N] /
                # `* elapsed` / stale|bg / branch) starts
                # immediately after THAT row's name — not after a
                # max-width fake name column, which would leave
                # short names with a wide blank before their
                # annotations.
                # The directory column IS pinned to a fixed
                # COL_DIR so paths line up across rows.
                COL_IDX = 4       # after "  ▶ "
                COL_STATE = COL_IDX + max_idx_w + 1
                COL_NAME = COL_STATE + max_state_w + 1

                now_ts = int(time.time())

                # Per-project annotation cache. Computed once
                # here so the rendering loop below does not have
                # to call signal_age_suffix / format_elapsed (file
                # I/O each time) a second time per refresh tick.
                # Format: per-project dict with the resolved
                # strings + the cluster's total width for
                # COL_DIR alignment.
                #
                # NOTE: `elapsed` is intentionally NOT in the cluster
                # width calc — it's rendered in a right-anchored slot
                # at the end of each row, not between name and path.
                # Including it here would make COL_DIR move every
                # second as the timer ticks and every time the marker
                # appears/disappears, dragging every project's path
                # left and right. The right-anchored placement keeps
                # paths visually stable across refresh ticks.
                annotations = []
                for proj in projects:
                    pieces = []  # widths for COL_DIR calc
                    elapsed_str = ""
                    # `* elapsed` is the "recently completed" marker —
                    # only meaningful when the project is currently
                    # IDLE. `@ccm_completed_at` is written on
                    # BUSY/PERMIT → IDLE transitions and is NOT cleared
                    # on subsequent transitions, so a project that just
                    # moved IDLE → BUSY (new prompt within
                    # COMPLETED_AT_TIMEOUT) would otherwise render
                    # `◉ BUSY * 5s` — confusing, since the * implies
                    # "finished 5s ago" but the project is busy now.
                    # Suppressing on non-IDLE keeps the marker honest.
                    if proj.state == "IDLE" and proj.completed_at:
                        age = now_ts - proj.completed_at
                        if 0 <= age < COMPLETED_AT_TIMEOUT:
                            elapsed_str = format_elapsed(proj.completed_at) or ""
                    suffix_str = signal_age_suffix(
                        proj.dir, proj.state,
                        session_id=proj.cached_session_id or "").strip()
                    pane_marker = f"[{proj.pane_count}]" if proj.pane_count > 1 else ""
                    # Permission-mode badge `{label}`. The everyday
                    # default (`manual`) is suppressed to keep rows
                    # quiet — the badge exists to flag the modes where
                    # dialogs are skipped or auto-resolved (accept /
                    # auto / dontAsk / bypass), because "no PERMIT
                    # ever shows up" is normal there and easy to
                    # misdiagnose as broken detection.
                    mode_label = permission_mode_label(proj.permission_mode)
                    mode_badge = (f"{{{mode_label}}}"
                                  if mode_label and mode_label != "manual"
                                  else "")
                    # Ignore marker `⊘`: a hidden sidekick pane is
                    # present but untracked. Dim, quiet aside.
                    ignore_marker = "⊘" if getattr(
                        proj, "ignored_panes", 0) else ""
                    # All width calculations go through `display_width`
                    # (terminal columns), not `len` (codepoints). Names
                    # and branches can contain CJK / emoji; mixing the
                    # two would silently misalign as soon as a wide
                    # character appears.
                    if pane_marker:
                        pieces.append(display_width(pane_marker))
                    if ignore_marker:
                        pieces.append(display_width(ignore_marker))
                    if mode_badge:
                        pieces.append(display_width(mode_badge))
                    if suffix_str:
                        pieces.append(display_width(suffix_str))
                    elif proj.bg_active:
                        pieces.append(display_width("(bg)"))
                    if proj.branch:
                        pieces.append(display_width(proj.branch) + 2)
                    cluster_w = (
                        1 + sum(pieces) + (len(pieces) - 1)
                        if pieces else 0
                    )
                    annotations.append({
                        "pane_marker": pane_marker,
                        "ignore_marker": ignore_marker,
                        "mode_badge": mode_badge,
                        "elapsed": elapsed_str,
                        "suffix": suffix_str,
                        "cluster_w": cluster_w,
                    })

                # Path lives at the column where the worst-case
                # name + annotation cluster ends, plus a 1-char
                # gap. Rows whose cluster ends earlier get padding
                # before the path column; rows with the worst-case
                # cluster get exactly one space before the path.
                COL_DIR = COL_NAME + max(
                    (display_width(p.name) + a["cluster_w"]
                     for p, a in zip(projects, annotations)),
                    default=0,
                ) + 1

                for i, (p, ann) in enumerate(zip(projects, annotations)):
                    if i < scroll_offset:
                        continue
                    if row >= list_height - 3:
                        break

                    is_selected = i == self.selected
                    y = row + 1

                    # Prefix
                    prefix = "  ▶ " if is_selected else "    "
                    self._addstr(stdscr, y, 0, prefix, curses.color_pair(C_DIM))

                    # Window index (right-aligned in column)
                    idx_str = f"#{p.win_idx}"
                    self._addstr(stdscr, y, COL_IDX, idx_str, curses.color_pair(C_DIM))

                    # State
                    state_cp = curses.color_pair(STATE_COLOR_PAIR.get(p.state, C_SHELL))
                    icon = STATE_ICONS.get(p.state, "?")
                    self._addstr(stdscr, y, COL_STATE, f"{icon} {p.state:<6}", state_cp)

                    # Project name
                    self._addstr(stdscr, y, COL_NAME, p.name, curses.A_BOLD)

                    # Annotations start one space after THIS row's
                    # name end. Each piece is split into its
                    # "attention-grabbing" sub-piece (coloured) and
                    # surrounding chrome (dim) so the user's eye
                    # lands on the meaningful value. ASCII-only
                    # markers ([N] for panes, * for completion)
                    # avoid font/terminal width ambiguity that
                    # would offset later columns.
                    col = COL_NAME + display_width(p.name) + 1

                    # Pane-count marker [N]: brackets dim, number
                    # cyan to draw the eye to the count.
                    if ann["pane_marker"]:
                        n = str(p.pane_count)
                        self._addstr(stdscr, y, col, "[", curses.color_pair(C_DIM))
                        self._addstr(stdscr, y, col + 1, n, curses.color_pair(C_CYAN))
                        self._addstr(stdscr, y, col + 1 + display_width(n), "]", curses.color_pair(C_DIM))
                        col += display_width(ann["pane_marker"]) + 1

                    # Ignore marker ⊘: a hidden sidekick pane exists.
                    # Dim — present-but-untracked, not a state.
                    if ann.get("ignore_marker"):
                        self._addstr(stdscr, y, col, ann["ignore_marker"],
                                     curses.color_pair(C_DIM))
                        col += display_width(ann["ignore_marker"]) + 1

                    # Permission-mode badge {label}: dim as secondary
                    # info, except bypassPermissions which gets bold
                    # yellow — every guardrail is off.
                    if ann["mode_badge"]:
                        if p.permission_mode in PERMISSION_MODE_WARN:
                            badge_attr = (curses.color_pair(C_YELLOW)
                                          | curses.A_BOLD)
                        else:
                            badge_attr = curses.color_pair(C_DIM)
                        self._addstr(stdscr, y, col, ann["mode_badge"],
                                     badge_attr)
                        col += display_width(ann["mode_badge"]) + 1

                    # NOTE: `* elapsed` used to live here inline. It
                    # was relocated to a right-anchored slot at the
                    # end of the row (rendered after the path below)
                    # so the path column stays put when the timer
                    # ticks or the marker appears/disappears.

                    # Stale-signal age (BUSY/PERMIT) or
                    # background-activity (IDLE) — disjoint, shared
                    # slot.
                    if ann["suffix"]:
                        self._addstr(stdscr, y, col, ann["suffix"],
                                     curses.color_pair(C_DIM))
                        col += display_width(ann["suffix"]) + 1
                    elif p.bg_active:
                        self._addstr(stdscr, y, col, "(bg)",
                                     curses.color_pair(C_DIM))
                        col += 5

                    # Branch
                    if p.branch:
                        self._addstr(stdscr, y, col, "(",
                                     curses.color_pair(C_DIM))
                        self._addstr(stdscr, y, col + 1, p.branch,
                                     curses.color_pair(C_CYAN))
                        self._addstr(stdscr, y, col + 1 + display_width(p.branch), ")",
                                     curses.color_pair(C_DIM))
                        col += display_width(p.branch) + 3

                    # Directory at fixed COL_DIR so the path
                    # column is vertically aligned across rows
                    # regardless of which inline annotations were
                    # rendered for this row.
                    #
                    # `effective_w - ELAPSED_RIGHT_SLOT` reserves
                    # constant space on the right for the
                    # right-anchored `* elapsed` marker (rendered
                    # below), so a long path never overlaps the
                    # marker even when both are shown.
                    if p.dir:
                        effective_w = list_width if preview_width > 0 else width
                        dir_str = format_dir(
                            p.dir, COL_DIR,
                            effective_w - ELAPSED_RIGHT_SLOT,
                        )
                        if dir_str:
                            self._addstr(stdscr, y, COL_DIR, dir_str,
                                         curses.color_pair(C_DIM))

                    # Right-anchored `* elapsed` marker. Lives in a
                    # fixed-width slot at the row's right edge so
                    # its appearance/disappearance and tick-induced
                    # width changes do not perturb any column on
                    # the left (most importantly, the path column).
                    # The slot is always reserved (above, via the
                    # format_dir clip) so non-IDLE rows simply leave
                    # the slot blank.
                    if ann["elapsed"]:
                        effective_w = list_width if preview_width > 0 else width
                        # "* " (2 visible cols) + 3-char elapsed = 5
                        # visible cols. Anchor at effective_w - 6 so
                        # the trailing 1 col is a right-margin gap.
                        elapsed_x = effective_w - 6
                        self._addstr(stdscr, y, elapsed_x, "*",
                                     curses.color_pair(C_COMPLETED))
                        self._addstr(stdscr, y, elapsed_x + 1,
                                     " " + ann["elapsed"],
                                     curses.color_pair(C_DIM))

                    row += 1

            # Background-session section: read-only display of the
            # per-user Claude Code agent-view daemon's roster.
            # Visibility is gated on `self.bg_visible` (off by default;
            # toggle with `b`; or set @ccm-bg-section=always for
            # persistent visibility). Always renders below the project
            # list; we deliberately do not introduce a separate panel
            # so it competes with neither the preview nor the help
            # line layout.
            with self.lock:
                bg_sessions = list(self.bg_sessions)
            if self.bg_visible:
                row = self._render_bg_section(
                    stdscr, row, bg_sessions, list_height,
                    list_width if preview_width > 0 else width,
                )

            # Help line — keys highlighted, wraps to 2 lines if needed
            avail_w = (list_width if preview_width > 0 else width) - 4  # padding
            help_items = [
                "[↑↓/jk] select", "[Enter] attach", "[/] search",
                "[p]review", "[a]dd", "re[g]ister", "re[n]ame",
                "[r]emove", "[i]gnore", "e[x]it all", "[s]ave", "[t]ree",
                "[b]g sessions", "[m/?] menu", "[q] quit",
            ]
            # Split into lines that fit within avail_w
            help_lines = []
            current_line = ""
            for item in help_items:
                test = f"{current_line}  {item}" if current_line else item
                if len(test) > avail_w and current_line:
                    help_lines.append(current_line)
                    current_line = item
                else:
                    current_line = test
            if current_line:
                help_lines.append(current_line)

            num_help_lines = len(help_lines)
            help_start_row = list_height - 1 - num_help_lines  # before footer
            for hi, hline in enumerate(help_lines):
                hr = help_start_row + hi
                if hr > row + 1:
                    self._render_help_line(stdscr, hr, 2, hline)

            # Footer
            footer_row = list_height - 1
            if footer_row > row + 1:
                footer_parts = []
                # Last saved time
                autosave = os.path.join(CCM_SNAPSHOT_DIR, "_autosave.json")
                try:
                    if os.path.exists(autosave):
                        mtime = os.path.getmtime(autosave)
                        save_time = time.strftime("%H:%M:%S", time.localtime(mtime))
                        footer_parts.append(f"Last saved: {save_time}")
                except OSError:
                    pass
                # Render footer: last saved + hooks status (hooks status colored separately)
                footer_text = "  ".join(footer_parts)
                self._addstr(stdscr, footer_row, 2, footer_text, curses.color_pair(C_DIM))
                hooks_col = 2 + len(footer_text) + 2 if footer_parts else 2
                hooks_color = curses.color_pair(C_CYAN) if self.hooks_on else curses.color_pair(C_YELLOW)
                self._addstr(stdscr, footer_row, hooks_col, self.hooks_status, hooks_color)

            # Preview panel. Skip during a navigation burst so rapid
            # ↑/↓ presses don't each pay the ~2000-addstr cost of
            # character-by-character ANSI-colored preview painting
            # (the single biggest expense per render). The main loop
            # schedules a full redraw once the burst settles, which
            # restores the preview.
            if preview_height > 0 and time.monotonic() >= self._nav_deadline:
                self._update_preview()
                if self.preview_position == "right":
                    self._render_preview(stdscr, preview_col, 0, preview_width, preview_height)
                elif self.preview_position == "bottom":
                    self._render_preview_bottom(stdscr, preview_row, width, preview_height)

            # Non-blocking message overlay
            if self._msg_text and time.monotonic() < self._msg_expires:
                self._addstr(stdscr, height - 1, 2, self._msg_text,
                             curses.color_pair(C_COMPLETED) | curses.A_BOLD)
            elif self._msg_text:
                self._msg_text = ""

            stdscr.refresh()
        except curses.error:
            pass

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

    def _render_help_line(self, stdscr, y, x, text):
        """Render help text with [key] portions highlighted."""
        col = x
        in_bracket = False
        dim = curses.color_pair(C_DIM)
        bright = curses.color_pair(C_CYAN) | curses.A_BOLD
        effective_max = getattr(self, '_render_max_col', 0)

        for ch in text:
            if effective_max and col >= effective_max - 1:
                break
            if ch == '[':
                in_bracket = True
                self._addstr(stdscr, y, col, ch, dim)
                col += 1
            elif ch == ']':
                in_bracket = False
                self._addstr(stdscr, y, col, ch, dim)
                col += 1
            else:
                attr = bright if in_bracket else dim
                self._addstr(stdscr, y, col, ch, attr)
                col += 1

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
        elif key in (ord("q"), ord("Q"), 27, curses.KEY_F1):
            return "quit"
        elif key in (ord("s"), ord("S")):
            self._do_save(stdscr)
        # The project-scoped action keys (p/n/r/i) share the Enter
        # key's stale-selection guard above: when a bg row is
        # selected, self.selected >= n and indexing projects[]
        # would raise IndexError.
        elif key in (ord("p"), ord("P")):
            if 0 <= self.selected < n:
                self._do_preview(stdscr)
        elif key in (ord("a"), ord("A")):
            self._do_add(stdscr)
        elif key in (ord("n"), ord("N")):
            if 0 <= self.selected < n:
                self._do_rename(stdscr)
        elif key in (ord("r"), ord("R")):
            if 0 <= self.selected < n:
                self._do_remove(stdscr)
        elif key in (ord("g"), ord("G")):
            self._do_register(stdscr)
        elif key in (ord("i"), ord("I")):
            if 0 <= self.selected < n:
                self._do_ignore_toggle(stdscr)
        elif key in (ord("x"), ord("X")):
            self._do_exit_all(stdscr)
        elif key == ord("/"):
            action = self._do_search(stdscr)
            if action == "attached":
                return "attached"
        elif key in (ord("t"), ord("T")):
            self.mode = "tree"
            self._build_tree()
        elif key in (ord("m"), ord("M"), ord("?")):
            # `?` is the universal "show me all the commands" key
            # (vim, fzf, etc.) — alias to menu mode which already
            # lists every action this dashboard exposes.
            self.mode = "menu"
            self._build_menu()
            self.menu_selected = 0
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
        # `new-window -t <session>:` (no index) places the window at
        # the next available index. `-P -F` makes tmux print the new
        # window's resolved target (`session:idx`), which we then
        # send-keys to and select. Append `-c <cwd>` (NOT insert) so
        # we never sit between `-t` and its required value.
        args = ["new-window", "-t", f"{session}:", "-P",
                "-F", "#{session_name}:#{window_index}",
                "-n", f"bg-{s.short}"]
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
        tmux_cmd("send-keys", "-t", new_target,
                 f"claude attach {s.short}", "Enter")
        tmux_cmd("select-window", "-t", new_target)
        return "attached"

    def _do_attach(self, stdscr):
        p = self.projects[self.selected]
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
            auto_start_claude(p.win_target)
        reset_window_after_attach(p.win_target)
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
            cmd_snapshot_save(name, quiet=True)
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
        choice = self._prompt(stdscr, f"Remove '{p.name}'? [u]nregister / [d]elete / Esc: ")
        if not choice:
            return
        if choice.lower() == "u":
            ok = self._run_cmd(stdscr, cmd_unregister, p.name)
        elif choice.lower() == "d":
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
        if getattr(p, "ignored_panes", 0):
            ok = self._run_cmd(stdscr, cmd_unignore, p.name)
        else:
            ok = self._run_cmd(stdscr, cmd_ignore, p.name)
        if ok:
            self._trigger_rebuild()

    def _do_exit_all(self, stdscr):
        """Exit all idle Claude Code sessions, optionally including BUSY/PERMIT."""
        with self.lock:
            projects = list(self.projects)

        if not projects:
            return

        idle_targets = [p for p in projects if p.state == "IDLE"]
        active_targets = [p for p in projects if p.state in ("BUSY", "PERMIT")]
        # SHELL projects already exited, skip

        total_exit = len(idle_targets)
        skip_count = len(active_targets)

        if total_exit == 0 and skip_count == 0:
            self._show_message(stdscr, "No active Claude Code sessions", 1)
            return

        # Build prompt
        if skip_count > 0:
            prompt = f"Exit {total_exit} idle sessions? ({skip_count} BUSY/PERMIT skipped) [y/n/a(all)]: "
        else:
            prompt = f"Exit all {total_exit} sessions? [Y/n]: "

        choice = self._prompt(stdscr, prompt)
        if choice is None:
            return

        choice = choice.lower().strip()
        if choice in ("", "y", "yes"):
            targets = idle_targets
        elif choice in ("a", "all"):
            targets = idle_targets + active_targets
        else:
            return

        exited = 0
        try:
            ps_lines = ps_snapshot().strip().split("\n")
        except Exception:
            ps_lines = []
        for p in targets:
            if p.state == "SHELL":
                continue
            # Resolve the claude-hosting pane and target IT, never the
            # window: `send-keys -t <window>` lands in the window's
            # ACTIVE pane, so in a split window with a shell focused
            # the Escape + `/exit` + Enter sequence would reach the
            # shell and kill the user's pane (the same incident
            # `auto_exit_idle`'s find_claude_pid resolution guards
            # against). An ignored pane is never a target — ignore
            # means ccm keeps its hands off it.
            panes = [pn for pn in enumerate_window_panes(p.win_target, ps_lines)
                     if not pn.ignored and pn.claude_pid]
            if not panes:
                # No (non-ignored) pane currently hosts claude — the
                # window may be transitioning. Defensive skip; without
                # a resolved pane there is no safe target.
                continue
            active = next((pn for pn in panes if pn.active), None)
            claude_pane = (active or panes[0]).pane_id
            # Exit any tmux mode (copy/view) first so /exit reaches the
            # pane's foreground process instead of a copy-mode binding.
            tmux_cmd("send-keys", "-t", claude_pane, "-X", "cancel")
            tmux_cmd("send-keys", "-t", claude_pane, "Escape")
            time.sleep(0.05)
            tmux_cmd("send-keys", "-t", claude_pane, "/exit", "Enter")
            exited += 1

        self._show_message(stdscr, f"Exited {exited} session(s)", 1)
        self._trigger_rebuild()

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

    def _do_search(self, stdscr):
        """Incremental live-filter search.

        Type to filter projects by case-insensitive substring match on
        the project name. Unicode-safe — Japanese project names match
        on Japanese query characters (`文脈` hits `文脈解析エンジン`)
        because Python's `in` on `str` is codepoint-based and Japanese
        has no case distinction to fold. Backspace is grapheme-aware
        via `_strip_last_grapheme` so a single delete removes one
        user-perceived character including combining marks.

        Returns:
            "attached" — Enter was pressed on a filtered match and the
                attach fired. The caller should propagate this so
                `run()` breaks out of the main event loop.
            ""         — Esc / Ctrl-C / Ctrl-G cancelled the search.
                The caller should re-render and continue.
        """
        buf = ""
        sel = 0  # index into `filtered`

        prev_cursor = 1
        try:
            prev_cursor = curses.curs_set(1)
        except curses.error:
            pass
        stdscr.timeout(-1)  # blocking get_wch

        try:
            while True:
                # Refresh projects snapshot each iteration so background
                # state updates are visible while filtering.
                with self.lock:
                    projects = list(self.projects)

                if buf:
                    q = buf.lower()
                    filtered = [(i, p) for i, p in enumerate(projects)
                                if q in p.name.lower()]
                else:
                    filtered = list(enumerate(projects))

                # Clamp selection after filter change.
                if not filtered:
                    sel = 0
                elif sel >= len(filtered):
                    sel = len(filtered) - 1
                elif sel < 0:
                    sel = 0

                self._render_search(stdscr, buf, filtered, sel, len(projects))

                try:
                    wch = stdscr.get_wch()
                except curses.error:
                    continue
                except KeyboardInterrupt:
                    return ""

                if isinstance(wch, str):
                    if wch == "\x1b":               # Esc
                        return ""
                    if wch in ("\n", "\r"):         # Enter
                        if filtered:
                            original_idx, _ = filtered[sel]
                            self.selected = original_idx
                            return self._do_attach(stdscr)
                        continue
                    if wch in ("\x7f", "\b"):       # Backspace / C-h
                        if buf:
                            buf = self._strip_last_grapheme(buf)
                            sel = 0
                        continue
                    if wch == "\x15":               # Ctrl-U clear
                        buf = ""
                        sel = 0
                        continue
                    if wch == "\x03":               # Ctrl-C
                        return ""
                    if wch == "\x07":               # Ctrl-G
                        return ""
                    if wch == "\x10":               # Ctrl-P (up)
                        if filtered:
                            sel = (sel - 1) % len(filtered)
                        continue
                    if wch == "\x0e":               # Ctrl-N (down)
                        if filtered:
                            sel = (sel + 1) % len(filtered)
                        continue
                    if wch >= " ":                  # Printable (incl. CJK)
                        buf += wch
                        sel = 0
                        continue
                else:
                    if wch in (curses.KEY_BACKSPACE, 127, 8):
                        if buf:
                            buf = self._strip_last_grapheme(buf)
                            sel = 0
                    elif wch == curses.KEY_UP:
                        if filtered:
                            sel = (sel - 1) % len(filtered)
                    elif wch == curses.KEY_DOWN:
                        if filtered:
                            sel = (sel + 1) % len(filtered)
                    elif wch == curses.KEY_RESIZE:
                        pass  # next loop iteration re-renders
        finally:
            try:
                curses.curs_set(prev_cursor)
            except curses.error:
                pass
            stdscr.timeout(50)  # restore non-blocking getch for main loop

    def _render_search(self, stdscr, buf, filtered, sel, total):
        """Render the quick-filter search UI (list + prompt + help)."""
        # Same self-heal as _render_current: force a full re-emit so
        # cells clobbered by tmux's popup-overlay bug get repaired.
        stdscr.redrawwin()
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        # Header
        self._addstr(stdscr, 0, 2, "ccm Quick Filter", curses.A_BOLD)

        # Reserve bottom 2 rows for prompt and help.
        list_top = 2
        list_bottom = height - 3
        max_rows = max(0, list_bottom - list_top + 1)

        if not filtered:
            if max_rows > 0 and list_top < height:
                self._addstr(stdscr, list_top, 4, "(no match)",
                             curses.color_pair(C_DIM))
        else:
            # Keep sel visible: scroll so the cursor row is within view.
            scroll = 0
            if sel >= max_rows:
                scroll = sel - max_rows + 1

            for i_row, (_orig, p) in enumerate(filtered[scroll:scroll + max_rows]):
                y = list_top + i_row
                absolute = scroll + i_row
                is_cur = (absolute == sel)

                prefix = "  ▶ " if is_cur else "    "
                self._addstr(stdscr, y, 0, prefix, curses.color_pair(C_DIM))

                idx_str = f"#{p.win_idx}"
                self._addstr(stdscr, y, 4, idx_str, curses.color_pair(C_DIM))

                state_cp = curses.color_pair(
                    STATE_COLOR_PAIR.get(p.state, C_SHELL))
                icon = STATE_ICONS.get(p.state, "?")
                name_col = 4 + max(3, len(idx_str)) + 1
                self._addstr(stdscr, y, name_col,
                             f"{icon} {p.state:<6}", state_cp)

                project_col = name_col + 9
                self._addstr(stdscr, y, project_col, p.name,
                             curses.A_BOLD if is_cur else 0)

        # Filter prompt
        prompt_row = height - 2
        if prompt_row >= 0:
            prompt_text = "Filter: "
            self._addstr(stdscr, prompt_row, 2, prompt_text,
                         curses.color_pair(C_DIM))
            prompt_col = 2 + len(prompt_text)
            self._addstr(stdscr, prompt_row, prompt_col, buf, 0)

            # Match count on the right side (e.g., "3/16")
            count_str = f"{len(filtered)}/{total}"
            count_col = width - len(count_str) - 2
            buf_end_col = prompt_col + display_width(buf)
            if count_col > buf_end_col + 2:
                self._addstr(stdscr, prompt_row, count_col, count_str,
                             curses.color_pair(C_DIM))

            try:
                stdscr.move(prompt_row, buf_end_col)
            except curses.error:
                pass

        # Help line
        help_row = height - 1
        if help_row >= 0:
            self._addstr(stdscr, help_row, 2,
                         "[↑↓] select  [Enter] attach  "
                         "[C-u] clear  [Esc] cancel",
                         curses.color_pair(C_DIM))

        stdscr.refresh()

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
            with self.lock:
                self._set_projects_stable(projects)
                self.bg_sessions = bg_sessions
                self.initial_load = False
                self.data_dirty = True
        except Exception:
            log_caught_exception("dashboard._refresh_loop:initial")
            self.initial_load = False

        # Subsequent refreshes: hybrid loop. Fast ticks sample the
        # pushed-state channel between full (slow-path) detection
        # passes — see FAST_TICK_INTERVAL for the design rationale.
        while self.running:
            # Check running before and after sleep to minimize exit delay
            for _ in range(max(1, int(REFRESH_INTERVAL / FAST_TICK_INTERVAL))):
                if not self.running:
                    return
                time.sleep(FAST_TICK_INTERVAL)
                self._fast_tick()
            try:
                projects = build_project_list(fast=False)
                with self.lock:
                    bg_visible = self.bg_visible
                bg_sessions = (
                    self._fetch_bg_sessions() if bg_visible else []
                )
                with self.lock:
                    self._set_projects_stable(projects)
                    self.bg_sessions = bg_sessions
                    self.data_dirty = True
                # Refresh preview content if enabled
                if self.preview_enabled and self.mode == "dashboard":
                    self._last_preview_target = ""  # Force refresh
            except Exception:
                log_caught_exception("dashboard._refresh_loop")

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

    def _fetch_bg_sessions(self):
        """Read the agent-view roster. Returns `[]` on any failure
        (missing daemon, malformed file, etc.). Bounded I/O cost —
        9 small JSON files at most in practice."""
        try:
            return ccm_agentview.list_bg_sessions()
        except Exception:
            log_caught_exception("dashboard._fetch_bg_sessions")
            return []

    # ─── Tree mode ───

    def _build_tree(self):
        """Build hierarchical tree data from ALL tmux sessions/windows/panes."""
        self.tree_lines = []
        self.tree_selectable = []
        self.tree_selected = 0

        sessions_raw = tmux_cmd("list-sessions", "-F", "#{session_name}")
        if not sessions_raw:
            return
        sessions = sorted(sessions_raw.split("\n"))
        current_session = get_session()
        current_win_idx = tmux_cmd("display-message", "-p", "#{window_index}")

        # Build project state lookup
        project_states = {}
        with self.lock:
            for p in self.projects:
                project_states[p.win_target] = p

        for si, sess in enumerate(sessions):
            is_last_session = si == len(sessions) - 1
            s_prefix = "└── " if is_last_session else "├── "
            s_cont = "    " if is_last_session else "│   "

            marker = " ◀" if sess == current_session else ""
            self.tree_lines.append((0, f"{s_prefix}{sess}{marker}", curses.A_BOLD, None))

            windows_raw = tmux_cmd(
                "list-windows", "-t", sess, "-F",
                "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}"
            )
            if not windows_raw:
                continue
            windows = windows_raw.split("\n")

            for wi, wline in enumerate(windows):
                parts = wline.split("\t")
                if len(parts) < 2:
                    continue
                # Pad missing fields (non-ccm windows have empty @ccm_project/@ccm_dir)
                while len(parts) < 4:
                    parts.append("")
                win_idx, win_name, project, wdir = parts[0], parts[1], parts[2], parts[3]
                win_target = f"{sess}:{win_idx}"
                is_last_win = wi == len(windows) - 1
                w_prefix = f"{s_cont}└── " if is_last_win else f"{s_cont}├── "

                # State and display for ccm projects
                proj = project_states.get(win_target)
                if proj:
                    icon = STATE_ICONS.get(proj.state, "?")
                    color_pair = STATE_COLOR_PAIR.get(proj.state, C_SHELL)
                    name_display = proj.name
                    branch = proj.branch
                    ports = proj.ports
                else:
                    icon = ""
                    color_pair = C_DIM
                    name_display = win_name
                    branch = ""
                    ports = ""

                line_text = f"{w_prefix}"
                if icon:
                    line_text += f"{icon} "
                line_text += name_display

                if branch:
                    line_text += f" ({branch})"
                if ports:
                    line_text += f" [:{ports}]"

                # Directory
                display_dir = ""
                if wdir:
                    display_dir = wdir.replace(os.path.expanduser("~"), "~")
                elif not project:
                    # Non-ccm window: show pane current path
                    pane_path = tmux_cmd("display-message", "-t", win_target, "-p", "#{pane_current_path}")
                    if pane_path:
                        display_dir = pane_path.replace(os.path.expanduser("~"), "~")
                if display_dir:
                    line_text += f" {display_dir}"

                # Current window marker
                if sess == current_session and win_idx == current_win_idx:
                    line_text += " ◀"

                sel_idx = len(self.tree_lines)
                self.tree_selectable.append(sel_idx)
                self.tree_lines.append((1, line_text, curses.color_pair(color_pair), win_target))

                # Multi-pane: show panes if window has more than one
                panes_raw = tmux_cmd(
                    "list-panes", "-t", win_target, "-F",
                    "#{pane_id}\t#{pane_current_path}\t#{pane_width}x#{pane_height}"
                )
                if panes_raw:
                    panes = panes_raw.strip().split("\n")
                    if len(panes) > 1:
                        w_cont = f"{s_cont}    " if is_last_win else f"{s_cont}│   "
                        for pi, pline in enumerate(panes):
                            pparts = pline.split("\t")
                            if len(pparts) < 3:
                                continue
                            pane_id, pane_path, pane_size = pparts
                            is_last_pane = pi == len(panes) - 1
                            p_prefix = f"{w_cont}└── " if is_last_pane else f"{w_cont}├── "
                            pane_dir = pane_path.replace(os.path.expanduser("~"), "~")
                            pane_text = f"{p_prefix}{pane_id} ({pane_size}) {pane_dir}"
                            self.tree_lines.append((2, pane_text, curses.color_pair(C_DIM), None))

    def _render_tree(self, stdscr):
        try:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            if height < self.MIN_HEIGHT or width < self.MIN_WIDTH:
                try:
                    stdscr.addstr(0, 0, f"Terminal too small ({width}x{height})"[:width - 1])
                except curses.error:
                    pass
                stdscr.refresh()
                return

            self._addstr(stdscr, 0, 2, "Tree View  (d=dashboard, q=quit)", curses.color_pair(C_DIM))

            # Build set of selected line indices for fast lookup
            sel_line_set = set()
            sel_line_idx = -1
            if self.tree_selectable and 0 <= self.tree_selected < len(self.tree_selectable):
                sel_line_idx = self.tree_selectable[self.tree_selected]
                sel_line_set.add(sel_line_idx)

            # Scrolling: ensure selected line is visible
            visible_lines = height - 2  # header + footer margin
            scroll_offset = 0
            if sel_line_idx >= 0:
                if sel_line_idx >= scroll_offset + visible_lines:
                    scroll_offset = sel_line_idx - visible_lines + 1
                if sel_line_idx < scroll_offset:
                    scroll_offset = sel_line_idx

            for i, (indent, text, attr, wt) in enumerate(self.tree_lines):
                row = i - scroll_offset + 1
                if row < 1:
                    continue
                if row >= height - 1:
                    break
                is_sel = i in sel_line_set
                prefix = "▶ " if is_sel else "  "
                self._addstr(stdscr, row, 0, prefix, curses.A_BOLD if is_sel else 0)
                self._addstr(stdscr, row, 2, text, attr | (curses.A_BOLD if is_sel else 0))

            stdscr.refresh()
        except curses.error:
            pass

    def _handle_tree_key(self, key, stdscr):
        n = len(self.tree_selectable)

        if key in (curses.KEY_UP, ord("k")):
            if n > 0:
                self.tree_selected = (self.tree_selected - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            if n > 0:
                self.tree_selected = (self.tree_selected + 1) % n
        elif key in (curses.KEY_ENTER, 10, 13):
            if n > 0:
                idx = self.tree_selectable[self.tree_selected]
                _, _, _, wt = self.tree_lines[idx]
                if wt:
                    # Defensively exit any stuck tmux copy/view mode on
                    # the target pane before sending keys to it.
                    tmux_cmd("send-keys", "-t", wt, "-X", "cancel")
                    # Auto-start Claude for ccm SHELL windows (routed
                    # through auto_start_claude so the command reaches
                    # a shell-foreground pane only, never whatever
                    # pane happens to be active).
                    with self.lock:
                        for p in self.projects:
                            if p.win_target == wt and p.state == "SHELL":
                                auto_start_claude(wt)
                                break
                    reset_window_after_attach(wt)
                    target_session = wt.split(":")[0]
                    session = get_session()
                    if target_session != session:
                        tmux_cmd("switch-client", "-t", target_session)
                    tmux_cmd("select-window", "-t", wt)
                    return "attached"
        elif key in (ord("d"), ord("D")):
            self.mode = "dashboard"
        elif key in (ord("q"), ord("Q"), 27, curses.KEY_F1):
            return "quit"

        return ""

    # ─── Menu mode ───

    def _build_menu(self):
        """Build menu items dynamically with current setting values."""
        # Status bar mode
        mode = tmux_cmd("show-option", "-gqv", "@ccm-status-line") or "2"
        mode_labels = {"0": "Minimal", "1": "Window list", "2": "Dedicated line"}
        mode_label = mode_labels.get(mode, mode)

        # Auto-restore
        auto_restore = tmux_cmd("show-option", "-gqv", "@ccm-auto-restore") or "off"

        # Idle timeout
        idle_str = tmux_cmd("show-option", "-gqv", "@ccm-idle-timeout")
        if idle_str:
            idle_label = f"{idle_str} min"
        else:
            idle_label = f"{IDLE_EXIT_TIMEOUT // 60} min (default)"

        # Preview
        preview_on = "on" if self.preview_enabled else "off"
        pos_labels = {"right": "Right", "bottom": "Bottom"}
        preview_pos_label = pos_labels.get(self.preview_position, self.preview_position)

        # Notifications
        notify = tmux_cmd("show-option", "-gqv", "@ccm-notify") or "permit,done"
        notify_sound = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound") or "off"
        sound_name = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound-name") or "Glass"

        # Auto-start
        auto_start = tmux_cmd("show-option", "-gqv", "@ccm-auto-start") or "on"

        is_macos = _IS_MACOS

        self.menu_items = [
            ("Add project", "add"),
            ("Save snapshot", "save"),
            ("Load snapshot", "load"),
            ("", ""),  # separator
            (f"Status bar mode: {mode_label}", "status_mode"),
            (f"Auto-restore: {auto_restore}", "auto_restore"),
            (f"Idle timeout: {idle_label}", "idle_timeout"),
            (f"Preview panel: {preview_on}", "preview_toggle"),
            (f"Preview position: {preview_pos_label}", "preview_position"),
            (f"Background sessions: {self.bg_section_setting}", "bg_section"),
            ("", ""),  # separator
            (f"Notifications: {notify}", "notify"),
        ]
        if is_macos:
            self.menu_items += [
                (f"Notification sound: {notify_sound}", "notify_sound"),
                (f"Sound name: {sound_name}", "sound_name"),
            ]
        self.menu_items += [
            (f"Auto-start Claude: {auto_start}", "auto_start"),
            ("", ""),  # separator
            ("Dashboard", "dashboard"),
            ("Tree view", "tree"),
            ("Quit", "quit"),
        ]

    def _render_menu(self, stdscr):
        try:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            if height < self.MIN_HEIGHT or width < self.MIN_WIDTH:
                try:
                    stdscr.addstr(0, 0, f"Terminal too small ({width}x{height})"[:width - 1])
                except curses.error:
                    pass
                stdscr.refresh()
                return

            self._addstr(stdscr, 0, 2, "Menu  (d=dashboard, q=quit)", curses.color_pair(C_DIM))

            row = 2
            for i, (label, action) in enumerate(self.menu_items):
                if row >= height - 1:
                    break
                if action == "":
                    # Separator
                    row += 1
                    continue
                is_sel = i == self.menu_selected
                prefix = "  ▶ " if is_sel else "    "
                attr = curses.A_BOLD if is_sel else 0
                self._addstr(stdscr, row, 0, f"{prefix}{label}", attr)
                row += 1

            stdscr.refresh()
        except curses.error:
            pass

    def _handle_menu_key(self, key, stdscr):
        # Skip separators when navigating
        selectable = [i for i, (_, a) in enumerate(self.menu_items) if a != ""]
        n = len(selectable)
        if n == 0:
            return ""

        def _cur_sel_idx():
            try:
                return selectable.index(self.menu_selected)
            except ValueError:
                return 0

        if key in (curses.KEY_UP, ord("k")):
            idx = (_cur_sel_idx() - 1) % n
            self.menu_selected = selectable[idx]
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (_cur_sel_idx() + 1) % n
            self.menu_selected = selectable[idx]
        elif key in (curses.KEY_ENTER, 10, 13):
            _, action = self.menu_items[self.menu_selected]
            if action == "add":
                self._do_add(stdscr)
                self._build_menu()
            elif action == "save":
                self._do_save(stdscr)
            elif action == "load":
                name = self._prompt(stdscr, "Snapshot name: ")
                if name:
                    try:
                        cmd_snapshot_load(name)
                    except SystemExit:
                        pass
                    self._trigger_rebuild()
            elif action == "status_mode":
                val = self._prompt(stdscr, "Status bar mode [0]=Minimal  [1]=Window list  [2]=Dedicated line: ")
                if val in ("0", "1", "2"):
                    tmux_cmd("set", "-g", "@ccm-status-line", val)
                    save_tmux_conf_setting(f"set -g @ccm-status-line {val}")
                    self._build_menu()
            elif action == "auto_restore":
                current = tmux_cmd("show-option", "-gqv", "@ccm-auto-restore") or "off"
                new_val = "off" if current == "on" else "on"
                tmux_cmd("set", "-g", "@ccm-auto-restore", new_val)
                save_tmux_conf_setting(f"set -g @ccm-auto-restore {new_val}")
                self._build_menu()
                self._show_message(stdscr, f"Auto-restore: {new_val}", 0.5)
            elif action == "idle_timeout":
                val = self._prompt(stdscr, "Idle timeout (minutes, 0=disabled): ")
                if val is not None:
                    try:
                        minutes = int(val)
                        if minutes >= 0:
                            tmux_cmd("set", "-g", "@ccm-idle-timeout", str(minutes))
                            save_tmux_conf_setting(f"set -g @ccm-idle-timeout {minutes}")
                            self._build_menu()
                    except ValueError:
                        self._show_message(stdscr, "Invalid number", 1)
            elif action == "preview_toggle":
                self.preview_enabled = not self.preview_enabled
                val = "on" if self.preview_enabled else "off"
                tmux_cmd("set", "-g", "@ccm-preview", val)
                save_tmux_conf_setting(f"set -g @ccm-preview {val}")
                self._build_menu()
            elif action == "preview_position":
                new_pos = "bottom" if self.preview_position == "right" else "right"
                self.preview_position = new_pos
                tmux_cmd("set", "-g", "@ccm-preview-position", new_pos)
                save_tmux_conf_setting(f"set -g @ccm-preview-position {new_pos}")
                self._build_menu()
            elif action == "bg_section":
                # Toggle the persistent setting between "off" and
                # "always". The session-local `b` key handles
                # on-demand visibility separately.
                new_val = "always" if self.bg_section_setting == "off" else "off"
                self.bg_section_setting = new_val
                self.bg_visible = (new_val == "always")
                if self.bg_visible:
                    # Same sync-fetch rationale as the `b` key path:
                    # avoid an empty-then-populated flicker the next
                    # time the user opens the dashboard.
                    with self.lock:
                        self.bg_sessions = self._fetch_bg_sessions()
                tmux_cmd("set", "-g", "@ccm-bg-section", new_val)
                save_tmux_conf_setting(f"set -g @ccm-bg-section {new_val}")
                self._build_menu()
                self._show_message(stdscr, f"Background sessions: {new_val}", 0.5)
            elif action == "notify":
                options = ["off", "permit", "completed", "permit,completed", "all"]
                current = tmux_cmd("show-option", "-gqv", "@ccm-notify") or "permit,completed"
                try:
                    idx = options.index(current)
                except ValueError:
                    idx = 3
                new_val = options[(idx + 1) % len(options)]
                tmux_cmd("set", "-g", "@ccm-notify", new_val)
                save_tmux_conf_setting(f'set -g @ccm-notify "{new_val}"')
                self._build_menu()
                self._show_message(stdscr, f"Notifications: {new_val}", 0.5)
            elif action == "notify_sound":
                current = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound") or "off"
                new_val = "off" if current == "on" else "on"
                tmux_cmd("set", "-g", "@ccm-notify-sound", new_val)
                save_tmux_conf_setting(f"set -g @ccm-notify-sound {new_val}")
                self._build_menu()
                if new_val == "on":
                    sound_name = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound-name") or "Glass"
                    self._preview_sound(sound_name)
                    self._show_message(stdscr, f"Sound: on ({sound_name})", 0.5)
                else:
                    self._show_message(stdscr, "Sound: off", 0.5)
            elif action == "sound_name":
                sounds = ["Glass", "Tink", "Pop", "Purr", "Ping", "Bottle", "Morse", "Basso"]
                current = tmux_cmd("show-option", "-gqv", "@ccm-notify-sound-name") or "Glass"
                try:
                    idx = sounds.index(current)
                except ValueError:
                    idx = 0
                new_val = sounds[(idx + 1) % len(sounds)]
                tmux_cmd("set", "-g", "@ccm-notify-sound-name", new_val)
                save_tmux_conf_setting(f"set -g @ccm-notify-sound-name {new_val}")
                self._build_menu()
                self._preview_sound(new_val)
                self._show_message(stdscr, f"Sound: {new_val}", 0.5)
            elif action == "auto_start":
                current = tmux_cmd("show-option", "-gqv", "@ccm-auto-start") or "on"
                new_val = "off" if current == "on" else "on"
                tmux_cmd("set", "-g", "@ccm-auto-start", new_val)
                save_tmux_conf_setting(f"set -g @ccm-auto-start {new_val}")
                self._build_menu()
                self._show_message(stdscr, f"Auto-start Claude: {new_val}", 0.5)
            elif action == "dashboard":
                self.mode = "dashboard"
            elif action == "tree":
                self.mode = "tree"
                self._build_tree()
            elif action == "quit":
                return "quit"
        elif key in (ord("d"), ord("D")):
            self.mode = "dashboard"
        elif key in (ord("q"), ord("Q"), 27, curses.KEY_F1):
            return "quit"

        return ""




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


def acquire_pidfile():
    pidfile = os.path.join(CCM_TMP_DIR, "dashboard.pid")
    os.makedirs(CCM_TMP_DIR, exist_ok=True)
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
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--mode" and i < len(sys.argv):
            mode = sys.argv[i + 1]
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
        elif arg == "--search":
            start_in_search = True

    pidfile = acquire_pidfile()
    try:
        dashboard = Dashboard(initial_mode=mode, start_in_search=start_in_search)
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
