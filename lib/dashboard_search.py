"""The dashboard's live-filter search: type to narrow the project list,
Enter to attach. A mixin of `dashboard.Dashboard`."""

import curses

from ccm_constants import STATE_ICONS
from ccm_render import display_width
from dashboard_shared import C_DIM, C_SHELL, STATE_COLOR_PAIR


class SearchMixin:

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
