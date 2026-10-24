"""The dashboard's main view: the project list, the background-session
block and the preview pane."""

import curses
import os
import re
import time

import ccm_agentview
import ccm_menu_help
import ccm_signals
import ccm_spool
from ccm_canaries import (
    disable_all_hooks_warning,
    errors_log_burst_warning,
    hook_silence_warnings,
    hooks_log_warning,
    managed_hooks_only_warning,
    shell_cluster_warnings,
)
from ccm_constants import (
    CCM_SNAPSHOT_DIR,
    COMPLETED_AT_TIMEOUT,
    PERMISSION_MODE_WARN,
    STATE_ICONS,
    permission_mode_label,
)
from ccm_core import get_session, ps_snapshot, tmux_cmd, tmux_query
from ccm_pane_state import enumerate_window_panes
from ccm_render import (
    display_width,
    EXTERNAL_AGENT_MARK,
    external_agent_badge,
    external_agent_state,
    format_age_short,
    format_dir,
    format_elapsed,
    signal_age_suffix,
    spool_expired_warning,
    truncate_to_width,
)
from dashboard_shared import (
    C_BUSY,
    C_COMPLETED,
    C_CYAN,
    C_DIM,
    C_IDLE,
    C_PERMIT,
    C_SHELL,
    C_SYNCING,
    C_YELLOW,
    LOGO_CELLS,
    STATE_COLOR_PAIR,
)

# Visual cols reserved at the right edge of each project row for the
# `* elapsed` marker (`* ` + 3-char padded elapsed + 1 col margin).
# Always reserved (even on rows without elapsed) so the path column's
# right-clip width stays constant across refresh ticks, which in turn
# keeps the path's horizontal position stable as the marker appears,
# disappears, and ticks across 1↔2 digit boundaries. Paired with
# `format_elapsed`'s 3-char right-padding in lib/ccm_render.py.
ELAPSED_RIGHT_SLOT = 6


class RenderMixin:

    def _draw_logo(self, stdscr, row):
        """The popup title's logo and name, for a docked dashboard (a pane
        has no title of its own). Returns the column after it."""
        col = 2
        for text, pair in LOGO_CELLS:
            self._addstr(stdscr, row, col, text, curses.color_pair(pair) | curses.A_BOLD)
            col += len(text)
        self._addstr(stdscr, row, col, " Dashboard", curses.A_BOLD)
        return col + len(" Dashboard") + 2

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

    #: Lines the preview gives to a `(bg)` project's background tasks.
    _BG_PREVIEW_MAX_LINES = 3

    def _bg_note(self, proj, now_ts):
        """The `(bg)` note of an IDLE row, with how long the oldest of
        the background tasks the newest Stop reported has been running
        once that is a minute or more: `(bg 9h)`. Plain `(bg)` when no
        task list is known (live child processes, a Stop before ccm
        kept the list). Keeps the tasks for the preview."""
        tasks = ccm_signals.read_stop_background_tasks(
            proj.dir, session_id=proj.cached_session_id or "")
        self._bg_tasks[proj.win_target] = tasks
        if not tasks:
            return "(bg)"
        age = now_ts - min(t["since"] for t in tasks)
        return f"(bg {format_age_short(age)})" if age >= 60 else "(bg)"

    def _preview_bg_lines(self, room):
        """Lines naming the selected project's background tasks, at
        most `room` of them: `bg: <kind> <age>  <description>`, the
        last one `bg: +N more` when they do not all fit."""
        by_window = getattr(self, "_bg_tasks", None)
        if room <= 0 or not by_window:
            return []
        with self.lock:
            projects = list(self.projects)
        if self.selected >= len(projects):
            return []
        tasks = by_window.get(projects[self.selected].win_target)
        if not tasks:
            return []
        now_ts = int(time.time())
        fit = tasks if len(tasks) <= room else tasks[:room - 1]
        lines = []
        for t in fit:
            age = now_ts - t["since"]
            head = f"bg: {t['kind']} {format_age_short(age) if age >= 60 else '<1m'}"
            lines.append((head, t["label"]))
        if len(fit) < len(tasks):
            lines.append((f"bg: +{len(tasks) - len(fit)} more", ""))
        return lines

    def _render_bg_lines(self, stdscr, row, col, max_w, lines):
        """Draw `_preview_bg_lines` from `row`: the kind and age in
        BUSY's colour, the description dim after them."""
        edge = col + max_w + 1
        for i, (head, label) in enumerate(lines):
            text = truncate_to_width(head, max_w)
            self._addstr(stdscr, row + i, col, text, curses.color_pair(C_BUSY),
                         max_col=edge)
            rest = max_w - display_width(text) - 2
            if label and rest > 0:
                self._addstr(stdscr, row + i, col + display_width(text) + 2,
                             truncate_to_width(label, rest),
                             curses.color_pair(C_DIM), max_col=edge)

    def _render_preview(self, stdscr, start_col, start_row, panel_width, panel_height):
        """Render preview panel with ANSI color support."""
        # Draw vertical separator
        for r in range(start_row, start_row + panel_height):
            try:
                stdscr.addch(r, start_col, "│", curses.color_pair(C_DIM))
            except curses.error:
                pass

        content_col = start_col + 2
        max_w = panel_width - 3
        bg_lines = self._preview_bg_lines(
            min(self._BG_PREVIEW_MAX_LINES, panel_height // 3))
        self._render_bg_lines(stdscr, start_row, content_col, max_w, bg_lines)
        start_row += len(bg_lines)
        panel_height -= len(bg_lines)

        lines = getattr(self, '_preview_lines', [])
        visible = lines[-(panel_height):] if panel_height > 0 else []

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

        max_w = panel_width - 2
        bg_lines = self._preview_bg_lines(
            min(self._BG_PREVIEW_MAX_LINES, (panel_height - 1) // 3))
        self._render_bg_lines(stdscr, start_row + 1, 1, max_w, bg_lines)
        start_row += len(bg_lines)
        panel_height -= len(bg_lines)

        lines = getattr(self, '_preview_lines', [])
        # -1 for the separator
        visible = lines[-(panel_height - 1):] if panel_height > 1 else []

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
        "STOPPED": C_DIM,
        "UNKNOWN": C_DIM,
    }

    #: Project rows the background block may never push out.
    _BG_MIN_PROJECT_ROWS = 3

    def _bg_section_rows(self, bg_sessions, available):
        """Rows the background-session block gets out of `available` —
        the rows left for the list and the block together, below the
        header and whatever banners were drawn. It asks for a spacer,
        the header, and one row per session (or the one-line hint
        when there are none), and gets that only after
        `_BG_MIN_PROJECT_ROWS` project rows are kept: the list is
        what the dashboard is for, so it yields last. The block clips
        its own rows when it gets less than it asked for."""
        wanted = 2 + max(len(bg_sessions), 1)
        room = available - self._BG_MIN_PROJECT_ROWS
        # Fewer rows than spacer, header and one line show nothing
        # (the block draws only once both header rows fit), so they
        # are not taken from the list either.
        if room < 3:
            return 0
        return min(wanted, room)

    def _render_bg_section(self, stdscr, start_row, bg_sessions,
                           body_end, effective_width):
        """Render the background-sessions block starting at `start_row`.

        Returns the next available row after the block (so the help-
        line layout can resume below it). When the daemon isn't
        running, the header is shown as a gentle hint rather than
        an empty list — the user pressed `b` (or set the always
        flag) intentionally and deserves feedback.
        """
        # Below `body_end` are the help and footer rows.
        max_row = body_end
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
            if s.needs and cwd_col < effective_width - 4:
                # What the session is waiting for is worth more than
                # where it runs: shown in the directory's slot, so a
                # blocked row says what would unblock it.
                room = effective_width - cwd_col - 1
                if display_width(s.needs) <= room:
                    ask = s.needs
                else:
                    ask = truncate_to_width(s.needs, room - 1) + "…"
                self._addstr(stdscr, row, cwd_col, ask,
                             curses.color_pair(self._BG_STATE_PAIR.get(s.state, C_DIM)))
            elif s.cwd and cwd_col < effective_width - 4:
                cwd_str = format_dir(s.cwd, cwd_col, effective_width)
                if cwd_str:
                    self._addstr(stdscr, row, cwd_col, cwd_str,
                                 curses.color_pair(C_DIM))

            row += 1

        return row

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

            # Dashboard and menu share the same preview dimensions.
            (list_width, list_height, preview_col, preview_row,
             preview_width, preview_height) = ccm_menu_help.preview_geometry(
                width, height, self.preview_enabled and self.mode == "dashboard",
                self.preview_position)

            # Store list width for _addstr clipping in list area
            self._render_max_col = list_width if preview_width > 0 else 0

            row = 0

            # Header
            header_col = self._draw_logo(stdscr, row) if getattr(self, "docked", False) else 2
            if self.initial_load:
                self._addstr(stdscr, row, header_col, "Syncing...", curses.color_pair(C_SYNCING))
            else:
                session = get_session()
                if session and not session.isdigit():
                    header = f"{session} — {len(self.projects)} project(s)"
                else:
                    header = f"{len(self.projects)} project(s)"
                self._addstr(stdscr, row, header_col, header, curses.color_pair(C_DIM))
            row += 1

            recovery_line = self._lifecycle_banner(tmux_query)
            if recovery_line:
                self._addstr(stdscr, row, 2, recovery_line, curses.color_pair(C_YELLOW))
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

            # A spooled message that expired without arriving. The
            # sender was told "queued", so the loss has no other
            # trace here — the queue length alone reads as empty.
            expired_msg = spool_expired_warning()
            if expired_msg:
                self._addstr(stdscr, row, 2, "⚠ " + expired_msg,
                             curses.color_pair(C_YELLOW))
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

            # A project whose `claude --continue` would start fresh:
            # its newest transcript hands off to a background session
            # the daemon still lists. Computed by the refresh thread
            # (`_fetch_handoff_warnings`); read here.
            with self.lock:
                handoff_warnings = list(self.handoff_warnings)
            for handoff_msg in handoff_warnings:
                self._addstr(stdscr, row, 2, "⚠ " + handoff_msg, curses.color_pair(C_YELLOW))
                row += 1

            # Silent-exception burst canary — surfaces poll-cycle bugs
            # within minutes rather than the operator having to think
            # to run `ccm errors`.
            burst_warning = errors_log_burst_warning()
            if burst_warning:
                self._addstr(stdscr, row, 2, "⚠ " + burst_warning, curses.color_pair(C_YELLOW))
                row += 1

            # The background-session block is drawn below the project
            # list. With more projects than the popup has rows, the
            # list would otherwise take every row and the block
            # would be skipped for want of space — `b` toggling a
            # section that never appears. So when the block is
            # visible its rows are reserved first, and the list
            # scrolls within what is left.
            with self.lock:
                bg_sessions = list(self.bg_sessions)
            # Rows below `row` (header and banners drawn) down to the
            # help line, estimated one row short so the layout errs
            # towards leaving the help line room. Banners come out of
            # this too — a reservation made against the popup height
            # alone would let them push the block, or the selected
            # project, off the bottom.
            # The help lines are laid out first and the list stops
            # above them: with more projects than rows, a list drawn
            # first takes every row and the keys go unannounced.
            help_items = [
                "[↑↓/jk] select", "[Enter] attach", "[/] search",
                "[p]review", "[a]dd", "re[g]ister", "re[n]ame",
                "[r]emove…", "[i]gnore", "e[x]it all", "[s]ave", "[t]ree",
                "[b]g sessions", "[w]atch sidekicks",
            ]
            help_lines = self._help_lines(
                help_items, (list_width if preview_width > 0 else width) - 4,
                list_height - 3 - row - self._HELP_MIN_BODY_ROWS)
            # The list and the background block end above a blank row,
            # the help lines and the footer.
            body_end = list_height - 2 - len(help_lines)
            available = max(0, (body_end - 1) - row - 1)
            bg_reserved = (self._bg_section_rows(bg_sessions, available)
                           if self.bg_visible else 0)

            if not projects:
                self._addstr(stdscr, row + 1, 4, "No active projects.", curses.color_pair(C_DIM))
                row += 3
            else:
                # Scrolling: ensure selected project is visible. The
                # count is what is actually left after the block's
                # reservation — derived, not assumed, so banners never
                # scroll the selected row off the bottom. It can be
                # zero on a very short popup; then nothing is drawn
                # and the offset simply follows the selection.
                visible_lines = available - bg_reserved
                scroll_offset = getattr(self, '_scroll_offset', 0)
                if visible_lines <= 0:
                    scroll_offset = self.selected
                elif self.selected >= scroll_offset + visible_lines:
                    scroll_offset = self.selected - visible_lines + 1
                if self.selected < scroll_offset:
                    scroll_offset = self.selected
                if scroll_offset < 0:
                    scroll_offset = 0
                self._scroll_offset = scroll_offset

                # Calculate column widths for alignment
                max_idx_w = max((len(p.win_idx) for p in projects), default=1) + 1  # "#N"
                # "● PERMIT" = 8. Deliberately constant: the first
                # paint happens before the full detection pass has
                # found external agents, and anything row-dependent
                # here arrives late, widens the column, and shoves
                # every name sideways in front of the user.
                max_state_w = 8

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
                # Background tasks of this pass's `(bg)` rows, by
                # window, for the preview's lines about them.
                self._bg_tasks = {}
                spool_counts = ccm_spool.pending_counts()
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
                    # External-agent badge `▸<name>`: a pane running an
                    # external agent CLI exists in this window, with
                    # the state's icon while it waits or works.
                    ext_badge = external_agent_badge(proj)
                    # Spool marker `✉N`: N messages queued for this
                    # project. Dim — a queue length, not a state.
                    spool_n = spool_counts.get(proj.name, 0)
                    spool_marker = f"✉{spool_n}" if spool_n else ""
                    # All width calculations go through `display_width`
                    # (terminal columns), not `len` (codepoints). Names
                    # and branches can contain CJK / emoji; mixing the
                    # two would silently misalign as soon as a wide
                    # character appears.
                    if pane_marker:
                        pieces.append(display_width(pane_marker))
                    if ignore_marker:
                        pieces.append(display_width(ignore_marker))
                    if ext_badge:
                        pieces.append(display_width(ext_badge))
                    if spool_marker:
                        pieces.append(display_width(spool_marker))
                    if mode_badge:
                        pieces.append(display_width(mode_badge))
                    bg_label = ""
                    if not suffix_str and proj.bg_active:
                        bg_label = self._bg_note(proj, now_ts)
                    if suffix_str:
                        pieces.append(display_width(suffix_str))
                    elif bg_label:
                        pieces.append(display_width(bg_label))
                    if proj.branch:
                        pieces.append(display_width(proj.branch) + 2)
                    cluster_w = (
                        1 + sum(pieces) + (len(pieces) - 1)
                        if pieces else 0
                    )
                    annotations.append({
                        "pane_marker": pane_marker,
                        "ignore_marker": ignore_marker,
                        "ext_badge": ext_badge,
                        "spool_marker": spool_marker,
                        "mode_badge": mode_badge,
                        "elapsed": elapsed_str,
                        "suffix": suffix_str,
                        "bg_label": bg_label,
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
                    if row >= body_end - 1 - bg_reserved:
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
                    # SHELL-row external-agent note `(name)`: dim,
                    # right after the 8-col state cell (see the
                    # max_state_w widening above).

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

                    # Stale-signal age (BUSY/PERMIT) or
                    # background-activity (IDLE) — disjoint, shared
                    # slot. Both describe this row's Claude, so they
                    # sit by its name, ahead of the sidekick markers
                    # (which read as the sidekick's otherwise).
                    # `(bg)` takes BUSY's colour: the window is IDLE,
                    # but something Claude started is still running.
                    if ann["suffix"]:
                        self._addstr(stdscr, y, col, ann["suffix"],
                                     curses.color_pair(C_DIM))
                        col += display_width(ann["suffix"]) + 1
                    elif ann.get("bg_label"):
                        self._addstr(stdscr, y, col, ann["bg_label"],
                                     curses.color_pair(C_BUSY))
                        col += display_width(ann["bg_label"]) + 1

                    # Ignore marker ⊘: a hidden sidekick pane exists.
                    # Dim — present-but-untracked, not a state — until
                    # that hidden Claude is blocked on a permission
                    # dialog, when it borrows PERMIT's yellow (the
                    # ⊘-wearing counterpart of the ▸ attention colour).
                    if ann.get("ignore_marker"):
                        ig_attr = (
                            curses.color_pair(C_PERMIT) | curses.A_BOLD
                            if "claude" in getattr(p, "attention_agents", ())
                            else curses.color_pair(C_DIM))
                        self._addstr(stdscr, y, col, ann["ignore_marker"],
                                     ig_attr)
                        col += display_width(ann["ignore_marker"]) + 1

                    # External-agent badge ▸<name>: a pane running an
                    # external agent CLI exists. Dim while it only sits
                    # there; PERMIT's bold yellow and ⚠ while it waits
                    # for a person, BUSY's colour and ◉ while it works
                    # — the window's colour vocabulary, without joining
                    # the 4-state model.
                    if ann.get("ext_badge"):
                        ext_state = external_agent_state(p)
                        badge_attr = (
                            curses.color_pair(C_PERMIT) | curses.A_BOLD
                            if ext_state == "PERMIT"
                            else curses.color_pair(C_BUSY)
                            if ext_state == "BUSY"
                            else curses.color_pair(C_DIM))
                        mark = EXTERNAL_AGENT_MARK
                        self._addstr(stdscr, y, col, mark,
                                     curses.color_pair(C_DIM))
                        self._addstr(stdscr, y, col + display_width(mark),
                                     ann["ext_badge"][len(mark):], badge_attr)
                        col += display_width(ann["ext_badge"]) + 1

                    # Spool marker ✉N: messages queued for this project
                    # (store-and-forward). Dim — a queue length, not a
                    # state.
                    if ann.get("spool_marker"):
                        self._addstr(stdscr, y, col, ann["spool_marker"],
                                     curses.color_pair(C_DIM))
                        col += display_width(ann["spool_marker"]) + 1

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
            if self.bg_visible:
                row = self._render_bg_section(
                    stdscr, row, bg_sessions, body_end,
                    list_width if preview_width > 0 else width,
                )

            for hi, hline in enumerate(help_lines):
                hr = body_end + 1 + hi
                if hr > row:  # a popup too short for any list row
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

    # Kept last in the help: the one line left when rows run short.
    _HELP_TAIL = ("[?/m] actions & settings", "[q] quit")
    # Project rows the help lines leave before they give way.
    _HELP_MIN_BODY_ROWS = 3

    @classmethod
    def _help_lines(cls, items, avail_w, room):
        """Pack `items` and then `_HELP_TAIL` into lines `avail_w`
        wide. With fewer than that many lines of `room`, the tail,
        which names the menu, comes first and the leading lines of
        `items` fill what is left."""
        def pack(group):
            lines, current = [], ""
            for item in group:
                test = f"{current}  {item}" if current else item
                if len(test) > avail_w and current:
                    lines.append(current)
                    current = item
                else:
                    current = test
            if current:
                lines.append(current)
            return lines
        lines = pack(list(items) + list(cls._HELP_TAIL))
        if len(lines) <= max(room, 1):
            return lines
        tail = pack(cls._HELP_TAIL)
        keep = max(room - len(tail), 0)
        return (pack(items)[:keep] if keep else []) + tail[:max(room, 1)]

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
