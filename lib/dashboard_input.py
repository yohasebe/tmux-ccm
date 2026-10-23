"""The dashboard's one-line prompt: editing by user-perceived
character and path completion."""

import curses
import os
import unicodedata

from ccm_render import display_width
from dashboard_shared import C_DIM



class InputMixin:

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
