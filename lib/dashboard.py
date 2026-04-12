#!/usr/bin/env python3
"""ccm Dashboard — Python curses implementation for responsive TUI."""

import contextlib
import curses
import io
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata

# Add lib dir to path for ccm_core import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ccm_core import (
    CCM_ROOT, CCM_TMP_DIR, CCM_SNAPSHOT_DIR, CCM_GIT_CACHE_DIR, CCM_PORT_CACHE_DIR,
    DONE_TIMEOUT, IDLE_EXIT_TIMEOUT,
    STATE_PRIORITY, STATE_ICONS, CLAUDE_CMD,
    tmux_cmd, md5_hash, get_session, touch_popup_session, read_hook_signal,
    read_cache_file, build_project_list, format_elapsed, format_dir,
    hooks_configured, hooks_log_warning, save_tmux_conf_setting,
    cmd_add, cmd_remove, cmd_unregister, cmd_register,
    cmd_snapshot_save, cmd_snapshot_load,
    CCMError, raise_on_die,
)

# Max characters shown in the single-line message area
MSG_MAX_LEN = 200

REFRESH_INTERVAL = 2

_IS_MACOS = platform.system() == "Darwin"

# Color pair IDs (curses-specific, stay in dashboard.py)
C_PERMIT = 1
C_BUSY = 2
C_DONE = 3
C_IDLE = 4
C_SHELL = 5
C_DIM = 6
C_CYAN = 7
C_YELLOW = 8
C_SYNCING = 9

STATE_COLOR_PAIR = {
    "PERMIT": C_PERMIT, "BUSY": C_BUSY, "DONE": C_DONE,
    "IDLE": C_IDLE, "SHELL": C_SHELL, "DOWN": C_SHELL,
}


# ─── Dashboard ───

class Dashboard:
    def __init__(self, initial_mode="dashboard"):
        self.projects = []
        self.lock = threading.Lock()
        self.selected = 0
        self.running = True
        self.data_dirty = False
        self.initial_load = True
        self.hooks_on = hooks_configured()
        self.hooks_status = "Hooks: ON" if self.hooks_on else "Hooks: OFF"
        self.mode = initial_mode  # "dashboard", "tree", "menu"
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
        # Menu mode state
        self.menu_items = []  # Built dynamically by _build_menu()
        self.menu_selected = 0
        # Non-blocking message display
        self._msg_text = ""
        self._msg_expires = 0.0

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

        # Instant first paint from cached state
        self.projects = build_project_list(fast=True)
        if self.mode == "tree":
            self._build_tree()
        elif self.mode == "menu":
            self._build_menu()
        self._render_current(stdscr)

        # Start background refresh
        bg = threading.Thread(target=self._refresh_loop, daemon=True)
        bg.start()

        # Main event loop
        while self.running:
            touch_popup_session()

            key = stdscr.getch()
            if key == -1:
                if self.data_dirty:
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
            self._render_current(stdscr)

    def _render_current(self, stdscr):
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
            curses.init_pair(C_DONE, curses.COLOR_GREEN, -1)
            curses.init_pair(C_IDLE, 68, -1)      # blue
            curses.init_pair(C_SHELL, 245, -1)    # gray
            curses.init_pair(C_DIM, 242, -1)      # dim gray
            curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
            curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
            curses.init_pair(C_SYNCING, curses.COLOR_CYAN, -1)
        else:
            curses.init_pair(C_PERMIT, curses.COLOR_YELLOW, -1)
            curses.init_pair(C_BUSY, curses.COLOR_RED, -1)
            curses.init_pair(C_DONE, curses.COLOR_GREEN, -1)
            curses.init_pair(C_IDLE, curses.COLOR_BLUE, -1)
            curses.init_pair(C_SHELL, curses.COLOR_WHITE, -1)
            curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
            curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
            curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
            curses.init_pair(C_SYNCING, curses.COLOR_CYAN, -1)

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
        # Capture with -e for ANSI escape sequences (color support)
        # Try normal screen first, then alternate screen (CLAUDE_CODE_NO_FLICKER=1)
        raw = tmux_cmd("capture-pane", "-e", "-t", p.win_target, "-p", "-S", "-50")
        if not raw or not raw.strip():
            raw = tmux_cmd("capture-pane", "-e", "-a", "-t", p.win_target, "-p", "-S", "-50")
        self.preview_cache = raw if raw else "(no content)"
        self._preview_lines = self.preview_cache.split("\n") if self.preview_cache else []

    # ANSI SGR to curses attribute mapping
    _ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')

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
                    ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
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
                    ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                    if display_used + ch_w > max_w:
                        break
                    try:
                        stdscr.addstr(r, col, ch, cur_attr)
                    except curses.error:
                        pass
                    col += ch_w
                    display_used += ch_w
                    pos += 1

    MIN_HEIGHT = 10
    MIN_WIDTH = 40

    def render(self, stdscr):
        try:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

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

            # Project list
            with self.lock:
                projects = list(self.projects)

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
                max_name_w = max((len(p.name) for p in projects), default=5)

                # Fixed column positions
                COL_IDX = 4       # after "  ▶ "
                COL_STATE = COL_IDX + max_idx_w + 1
                COL_NAME = COL_STATE + max_state_w + 1
                COL_REST = COL_NAME + max_name_w + 1

                for i, p in enumerate(projects):
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

                    # Remaining info after name column
                    col = COL_REST

                    # Branch
                    if p.branch:
                        self._addstr(stdscr, y, col, "(", curses.color_pair(C_DIM))
                        self._addstr(stdscr, y, col + 1, p.branch, curses.color_pair(C_CYAN))
                        self._addstr(stdscr, y, col + 1 + len(p.branch), ")", curses.color_pair(C_DIM))
                        col += len(p.branch) + 3

                    # Elapsed time
                    elapsed = format_elapsed(p.last_done_ts)
                    if elapsed:
                        self._addstr(stdscr, y, col, "✔ ", curses.color_pair(C_DONE))
                        col += 2
                        self._addstr(stdscr, y, col, elapsed, curses.color_pair(C_DIM))
                        col += len(elapsed) + 1

                    # Directory (truncated to fit)
                    if p.dir:
                        effective_w = list_width if preview_width > 0 else width
                        dir_str = format_dir(p.dir, col, effective_w)
                        if dir_str:
                            self._addstr(stdscr, y, col, dir_str, curses.color_pair(C_DIM))

                    row += 1

            # Help line — keys highlighted, wraps to 2 lines if needed
            avail_w = (list_width if preview_width > 0 else width) - 4  # padding
            help_items = [
                "[↑↓/jk] select", "[Enter] attach", "[p]review", "[a]dd",
                "[n]ame", "[r]emove", "e[x]it all", "[s]ave", "[t]ree", "[m]enu", "[q] quit",
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

            # Preview panel
            if preview_height > 0:
                self._update_preview()
                if self.preview_position == "right":
                    self._render_preview(stdscr, preview_col, 0, preview_width, preview_height)
                elif self.preview_position == "bottom":
                    self._render_preview_bottom(stdscr, preview_row, width, preview_height)

            # Non-blocking message overlay
            if self._msg_text and time.monotonic() < self._msg_expires:
                self._addstr(stdscr, height - 1, 2, self._msg_text,
                             curses.color_pair(C_DONE) | curses.A_BOLD)
            elif self._msg_text:
                self._msg_text = ""

            stdscr.refresh()
        except curses.error:
            pass

    @staticmethod
    def _display_width(text):
        """Calculate display width accounting for wide (CJK) characters."""
        w = 0
        for c in text:
            w += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        return w

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
    def _truncate_to_width(text, max_width):
        """Truncate text to fit within max_width display columns."""
        w = 0
        for i, c in enumerate(text):
            cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
            if w + cw > max_width:
                return text[:i]
            w += cw
        return text

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
            clipped = self._truncate_to_width(text, avail)
            stdscr.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def _handle_key(self, key, stdscr):
        n = len(self.projects)

        if key in (curses.KEY_UP, ord("k")):
            if n > 0:
                self.selected = (self.selected - 1) % n
                self._last_preview_target = ""  # Force preview refresh
        elif key in (curses.KEY_DOWN, ord("j")):
            if n > 0:
                self.selected = (self.selected + 1) % n
                self._last_preview_target = ""  # Force preview refresh
        elif key in (curses.KEY_ENTER, 10, 13):
            if n > 0:
                return self._do_attach(stdscr)
        elif key in (ord("q"), ord("Q"), 27, curses.KEY_F1):
            return "quit"
        elif key in (ord("s"), ord("S")):
            self._do_save(stdscr)
        elif key in (ord("p"), ord("P")):
            if n > 0:
                self._do_preview(stdscr)
        elif key in (ord("a"), ord("A")):
            self._do_add(stdscr)
        elif key in (ord("n"), ord("N")):
            if n > 0:
                self._do_rename(stdscr)
        elif key in (ord("r"), ord("R")):
            if n > 0:
                self._do_remove(stdscr)
        elif key in (ord("g"), ord("G")):
            self._do_register(stdscr)
        elif key in (ord("x"), ord("X")):
            self._do_exit_all(stdscr)
        elif key == ord("/"):
            self._do_search(stdscr)
        elif key in (ord("t"), ord("T")):
            self.mode = "tree"
            self._build_tree()
        elif key in (ord("m"), ord("M")):
            self.mode = "menu"
            self._build_menu()
            self.menu_selected = 0

        return ""

    def _do_attach(self, stdscr):
        p = self.projects[self.selected]
        # Check if project directory still exists
        if p.dir and not os.path.isdir(os.path.expanduser(p.dir)):
            self._show_message(stdscr, f"Directory not found: {p.dir}", 3)
            return ""
        # Auto-start if SHELL
        if p.state == "SHELL":
            tmux_cmd("send-keys", "-t", p.win_target, CLAUDE_CMD, "Enter")
        # Clear DONE
        tmux_cmd("set-option", "-wt", p.win_target, "-u", "@ccm_done")
        tmux_cmd("set-option", "-wt", p.win_target, "-u", "@ccm_prev_state")
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
                with open(os.path.join(CCM_SNAPSHOT_DIR, f"{name}.json")) as f:
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
        captured = tmux_cmd("capture-pane", "-t", p.win_target, "-p", "-S", "-30")
        if not captured or not captured.strip():
            captured = tmux_cmd("capture-pane", "-a", "-t", p.win_target, "-p", "-S", "-30")
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
        if not os.path.isdir(directory):
            self._show_message(stdscr, "Directory not found", 1)
            return
        name = self._prompt(stdscr, f"Name [{os.path.basename(directory)}]: ")
        if name is None:
            return
        if not name:
            name = os.path.basename(directory)
        if self._run_cmd(stdscr, cmd_add, directory, name):
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

    def _do_exit_all(self, stdscr):
        """Exit all idle Claude Code sessions, optionally including BUSY/PERMIT."""
        with self.lock:
            projects = list(self.projects)

        if not projects:
            return

        idle_targets = [p for p in projects if p.state in ("IDLE", "DONE")]
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
        for p in targets:
            if p.state == "SHELL":
                continue
            tmux_cmd("send-keys", "-t", p.win_target, "Escape")
            time.sleep(0.05)
            tmux_cmd("send-keys", "-t", p.win_target, "/exit", "Enter")
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
        query = self._prompt(stdscr, "Search: ")
        if not query:
            return
        query_lower = query.lower()
        for i, p in enumerate(self.projects):
            if query_lower in p.name.lower():
                self.selected = i
                break

    def _trigger_rebuild(self):
        """Force a rebuild in the background thread."""
        projects = build_project_list(fast=True)
        with self.lock:
            self.projects = projects
            self.data_dirty = True

    def _prompt(self, stdscr, prompt_text, path_completion=False):
        """Show prompt, return input string. Returns None on Escape.
        If path_completion=True, Tab key completes file/directory paths.
        """
        curses.curs_set(1)
        height, width = stdscr.getmaxyx()
        row = height - 1
        prompt_col = 2 + self._display_width(prompt_text)
        stdscr.timeout(-1)  # Block for input

        buf = ""

        def _redraw():
            stdscr.move(row, 0)
            stdscr.clrtoeol()
            self._addstr(stdscr, row, 0, f"  {prompt_text}", curses.color_pair(C_DIM))
            self._addstr(stdscr, row, prompt_col, buf, 0)
            cursor_col = prompt_col + self._display_width(buf)
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
            with self.lock:
                self.projects = projects
                self.initial_load = False
                self.data_dirty = True
        except Exception:
            self.initial_load = False

        # Subsequent refreshes
        while self.running:
            # Check running before and after sleep to minimize exit delay
            for _ in range(int(REFRESH_INTERVAL / 0.2)):
                if not self.running:
                    return
                time.sleep(0.2)
            try:
                projects = build_project_list(fast=False)
                with self.lock:
                    self.projects = projects
                    self.data_dirty = True
                # Refresh preview content if enabled
                if self.preview_enabled and self.mode == "dashboard":
                    self._last_preview_target = ""  # Force refresh
            except Exception:
                pass

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
                    # Auto-start Claude for ccm SHELL windows
                    with self.lock:
                        for p in self.projects:
                            if p.win_target == wt and p.state == "SHELL":
                                tmux_cmd("send-keys", "-t", wt, CLAUDE_CMD, "Enter")
                                break
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
        mode = tmux_cmd("show-option", "-gqv", "@ccm-status-line") or "0"
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
            elif action == "notify":
                options = ["off", "permit", "done", "permit,done", "all"]
                current = tmux_cmd("show-option", "-gqv", "@ccm-notify") or "permit,done"
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

def acquire_pidfile():
    pidfile = os.path.join(CCM_TMP_DIR, "dashboard.pid")
    os.makedirs(CCM_TMP_DIR, exist_ok=True)
    # Kill existing
    if os.path.exists(pidfile):
        try:
            old_pid = int(open(pidfile).read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                time.sleep(0.2)
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    return pidfile


def main():
    # Parse --mode argument
    mode = "dashboard"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--mode" and i < len(sys.argv):
            mode = sys.argv[i + 1]
            break
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
            break

    pidfile = acquire_pidfile()
    try:
        dashboard = Dashboard(initial_mode=mode)
        curses.wrapper(dashboard.run)
    except Exception:
        # Log errors for debugging
        import traceback
        err_file = os.path.join(CCM_TMP_DIR, "dashboard-error.log")
        with open(err_file, "w") as f:
            traceback.print_exc(file=f)
        raise
    finally:
        try:
            os.unlink(pidfile)
        except OSError:
            pass


if __name__ == "__main__":
    main()
