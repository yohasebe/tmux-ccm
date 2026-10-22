"""The dashboard's tree view: every tmux session, window and pane, with
ccm projects marked by state. A mixin of `dashboard.Dashboard`."""

import curses
import os

from ccm_constants import STATE_ICONS
from ccm_core import get_session, tmux_cmd
from ccm_window import auto_start_claude, reset_window_after_attach
from dashboard_shared import C_DIM, C_SHELL, STATE_COLOR_PAIR, _announce_unlaunched


class TreeMixin:

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
                                _announce_unlaunched(auto_start_claude(wt))
                                break
                    reset_window_after_attach(wt)
                    self._before_switch()
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
