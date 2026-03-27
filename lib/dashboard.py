#!/usr/bin/env python3
"""ccm Dashboard — Python curses implementation for responsive TUI."""

import curses
import os
import signal
import subprocess
import sys
import threading
import time

# Add lib dir to path for ccm_core import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ccm_core import (
    CCM_ROOT, CCM_TMP_DIR, CCM_SNAPSHOT_DIR, CCM_GIT_CACHE_DIR, CCM_PORT_CACHE_DIR,
    DONE_TIMEOUT, HOOK_TIMEOUT,
    STATE_PRIORITY, STATE_ICONS, CLAUDE_CMD,
    tmux_cmd, md5_hash, get_session, touch_popup_session, read_hook_signal,
    read_cache_file, build_project_list, format_elapsed, format_dir, hooks_configured,
)

REFRESH_INTERVAL = 3

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
        self.hooks_status = "Hooks: ON" if hooks_configured() else "Hooks: OFF"
        self.mode = initial_mode  # "dashboard", "tree", "menu"
        # Tree mode state
        self.tree_lines = []     # (indent, text, attr, win_target_or_none)
        self.tree_selected = 0
        self.tree_selectable = []  # indices into tree_lines that are selectable
        # Menu mode state
        self.menu_items = []  # Built dynamically by _build_menu()
        self.menu_selected = 0

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
            self._render_tree(stdscr)
        elif self.mode == "menu":
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
            curses.init_pair(C_BUSY, 209, -1)    # salmon
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

    def render(self, stdscr):
        try:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
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

            # Project list
            with self.lock:
                projects = list(self.projects)

            if not projects:
                self._addstr(stdscr, row + 1, 4, "No active projects.", curses.color_pair(C_DIM))
                row += 3
            else:
                # Scrolling: ensure selected project is visible
                visible_lines = height - 4  # header + help + footer
                scroll_offset = getattr(self, '_scroll_offset', 0)
                if self.selected >= scroll_offset + visible_lines:
                    scroll_offset = self.selected - visible_lines + 1
                if self.selected < scroll_offset:
                    scroll_offset = self.selected
                if scroll_offset < 0:
                    scroll_offset = 0
                self._scroll_offset = scroll_offset

                for i, p in enumerate(projects):
                    if i < scroll_offset:
                        continue
                    if row >= height - 3:
                        break

                    is_selected = i == self.selected
                    prefix = "  ▶ " if is_selected else "    "

                    col = 0
                    attr_bold = curses.A_BOLD if is_selected else 0

                    # Prefix + window index
                    self._addstr(stdscr, row + 1, col, prefix, curses.color_pair(C_DIM))
                    col += len(prefix)
                    self._addstr(stdscr, row + 1, col, f"#{p.win_idx} ", curses.color_pair(C_DIM))
                    col += len(f"#{p.win_idx} ")

                    # State icon
                    state_cp = curses.color_pair(STATE_COLOR_PAIR.get(p.state, C_SHELL))
                    icon = STATE_ICONS.get(p.state, "?")
                    state_text = f"{icon} {p.state}"
                    self._addstr(stdscr, row + 1, col, state_text, state_cp)
                    col += len(state_text)

                    # Project name
                    self._addstr(stdscr, row + 1, col, "  ", 0)
                    col += 2
                    name_attr = curses.A_BOLD if p.tagged else curses.color_pair(C_DIM)
                    self._addstr(stdscr, row + 1, col, p.name, name_attr)
                    col += len(p.name)

                    # Branch
                    if p.branch:
                        branch_str = f" ({p.branch})"
                        self._addstr(stdscr, row + 1, col, " (", curses.color_pair(C_DIM))
                        self._addstr(stdscr, row + 1, col + 2, p.branch, curses.color_pair(C_CYAN))
                        self._addstr(stdscr, row + 1, col + 2 + len(p.branch), ")", curses.color_pair(C_DIM))
                        col += len(branch_str)

                    # Ports
                    if p.ports:
                        port_str = f" [:{p.ports}]"
                        self._addstr(stdscr, row + 1, col, f" [:", curses.color_pair(C_DIM))
                        self._addstr(stdscr, row + 1, col + 3, p.ports, curses.color_pair(C_YELLOW))
                        self._addstr(stdscr, row + 1, col + 3 + len(p.ports), "]", curses.color_pair(C_DIM))
                        col += len(port_str)

                    # Elapsed time
                    elapsed = format_elapsed(p.last_done_ts)
                    if elapsed:
                        self._addstr(stdscr, row + 1, col, " ✔ ", curses.color_pair(C_DONE))
                        col += 3
                        self._addstr(stdscr, row + 1, col, elapsed, curses.color_pair(C_DIM))
                        col += len(elapsed)

                    # Directory (truncated to fit)
                    if p.dir:
                        dir_str = format_dir(p.dir, col + 1, width)
                        if dir_str:
                            self._addstr(stdscr, row + 1, col, " ", 0)
                            col += 1
                            self._addstr(stdscr, row + 1, col, dir_str, curses.color_pair(C_DIM))

                    row += 1

            # Help line
            help_row = height - 3
            if help_row > row + 1:
                if width >= 100:
                    help_text = "[↑↓/jk] select  [Enter] attach  [p]review  [a]dd  [n]ame  [r]emove  [s]ave  [t]ree  [m]enu  [q] quit"
                elif width >= 60:
                    help_text = "[↑↓] select [Enter] attach [a]dd [n]ame [r]emove [s]ave [t]ree [m]enu [q] quit"
                else:
                    help_text = "[↑↓] sel [⏎] go [a]dd [r]m [s]ave [q] quit"
                self._addstr(stdscr, help_row, 2, help_text, curses.color_pair(C_DIM))

            # Footer
            footer_row = height - 2
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
                footer_parts.append(self.hooks_status)
                footer = "  ".join(footer_parts)
                self._addstr(stdscr, footer_row, 2, footer, curses.color_pair(C_DIM))

            stdscr.refresh()
        except curses.error:
            pass

    def _addstr(self, stdscr, y, x, text, attr=0):
        """Safe addstr that doesn't crash on boundary."""
        try:
            height, width = stdscr.getmaxyx()
            if y < 0 or y >= height or x >= width:
                return
            max_len = width - x - 1
            if max_len <= 0:
                return
            stdscr.addnstr(y, x, text, max_len, attr)
        except curses.error:
            pass

    def _handle_key(self, key, stdscr):
        n = len(self.projects)

        if key in (curses.KEY_UP, ord("k")):
            if n > 0:
                self.selected = (self.selected - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            if n > 0:
                self.selected = (self.selected + 1) % n
        elif key in (curses.KEY_ENTER, 10, 13):
            if n > 0:
                return self._do_attach(stdscr)
        elif key in (ord("q"), ord("Q"), 27):
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
        name = self._prompt(stdscr, "Snapshot name [_autosave]: ")
        if name is None:
            return
        if not name:
            name = "_autosave"
        ccm_bin = os.path.join(CCM_ROOT, "ccm")
        result = subprocess.run([ccm_bin, "snapshot", "save", name],
                                capture_output=True, text=True, timeout=10)
        msg = "Saved!" if result.returncode == 0 else "Save failed"
        self._show_message(stdscr, msg, 1)

    def _do_preview(self, stdscr):
        p = self.projects[self.selected]
        captured = tmux_cmd("capture-pane", "-t", p.win_target, "-p", "-S", "-30")
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
        ccm_bin = os.path.join(CCM_ROOT, "ccm")
        subprocess.run([ccm_bin, "add", directory, name],
                       capture_output=True, timeout=10)
        time.sleep(0.5)
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
        ccm_bin = os.path.join(CCM_ROOT, "ccm")
        if choice.lower() == "u":
            subprocess.run([ccm_bin, "unregister", p.name], capture_output=True, timeout=10)
        elif choice.lower() == "d":
            subprocess.run([ccm_bin, "remove", p.name], capture_output=True, timeout=10)
        else:
            return
        time.sleep(0.3)
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
        ccm_bin = os.path.join(CCM_ROOT, "ccm")
        subprocess.run([ccm_bin, "register", win, name], capture_output=True, timeout=10)
        time.sleep(0.3)
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
        prompt_col = 2 + len(prompt_text)
        stdscr.timeout(-1)  # Block for input

        buf = ""
        col = prompt_col

        def _redraw():
            stdscr.move(row, 0)
            stdscr.clrtoeol()
            self._addstr(stdscr, row, 0, f"  {prompt_text}", curses.color_pair(C_DIM))
            self._addstr(stdscr, row, prompt_col, buf, 0)
            stdscr.move(row, prompt_col + len(buf))

        _redraw()
        stdscr.refresh()

        while True:
            ch = stdscr.getch()
            if ch == 27:  # Escape
                curses.curs_set(0)
                stdscr.timeout(50)
                return None
            elif ch in (curses.KEY_ENTER, 10, 13):
                curses.curs_set(0)
                stdscr.timeout(50)
                return buf
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf = buf[:-1]
                    _redraw()
                    stdscr.refresh()
            elif ch == 9 and path_completion:  # Tab
                buf = self._complete_path(buf, stdscr, row, prompt_col)
                _redraw()
                stdscr.refresh()
            elif 32 <= ch <= 126:
                buf += chr(ch)
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
        height, _ = stdscr.getmaxyx()
        self._addstr(stdscr, height - 1, 2, msg, curses.color_pair(C_DONE) | curses.A_BOLD)
        stdscr.refresh()
        time.sleep(duration)

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
            time.sleep(REFRESH_INTERVAL)
            if not self.running:
                break
            try:
                projects = build_project_list(fast=False)
                with self.lock:
                    self.projects = projects
                    self.data_dirty = True
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
        elif key in (ord("q"), ord("Q"), 27):
            return "quit"

        return ""

    # ─── Menu mode ───

    def _build_menu(self):
        """Build menu items dynamically with current setting values."""
        # Status bar mode
        mode = tmux_cmd("show-option", "-gqv", "@ccm-status-line") or "0"
        mode_labels = {"0": "Icon only", "1": "Window list", "2": "Dedicated line"}
        mode_label = mode_labels.get(mode, mode)

        # Auto-restore
        auto_restore = tmux_cmd("show-option", "-gqv", "@ccm-auto-restore") or "off"

        # Idle timeout
        idle_str = tmux_cmd("show-option", "-gqv", "@ccm-idle-timeout")
        if idle_str:
            idle_label = f"{idle_str} min"
        else:
            idle_label = f"{IDLE_EXIT_TIMEOUT // 60} min (default)"

        self.menu_items = [
            ("Add project", "add"),
            ("Save snapshot", "save"),
            ("Load snapshot", "load"),
            ("", ""),  # separator
            (f"Status bar mode: {mode_label}", "status_mode"),
            (f"Auto-restore: {auto_restore}", "auto_restore"),
            (f"Idle timeout: {idle_label}", "idle_timeout"),
            ("", ""),  # separator
            ("Dashboard", "dashboard"),
            ("Tree view", "tree"),
            ("Quit", "quit"),
        ]

    def _render_menu(self, stdscr):
        try:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

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
                    ccm_bin = os.path.join(CCM_ROOT, "ccm")
                    subprocess.run([ccm_bin, "snapshot", "load", name],
                                   capture_output=True, timeout=30)
                    self._trigger_rebuild()
            elif action == "status_mode":
                self._cycle_setting(
                    "@ccm-status-line",
                    [("0", "Icon only"), ("1", "Window list"), ("2", "Dedicated line")],
                    stdscr,
                )
            elif action == "auto_restore":
                self._cycle_setting(
                    "@ccm-auto-restore",
                    [("on", "on"), ("off", "off")],
                    stdscr,
                )
            elif action == "idle_timeout":
                val = self._prompt(stdscr, "Idle timeout (minutes, 0=disabled): ")
                if val is not None:
                    try:
                        minutes = int(val)
                        if minutes >= 0:
                            tmux_cmd("set", "-g", "@ccm-idle-timeout", str(minutes))
                            self._build_menu()
                    except ValueError:
                        self._show_message(stdscr, "Invalid number", 1)
            elif action == "dashboard":
                self.mode = "dashboard"
            elif action == "tree":
                self.mode = "tree"
                self._build_tree()
            elif action == "quit":
                return "quit"
        elif key in (ord("d"), ord("D")):
            self.mode = "dashboard"
        elif key in (ord("q"), ord("Q"), 27):
            return "quit"

        return ""

    def _cycle_setting(self, option, values, stdscr):
        """Cycle a tmux option through a list of (value, label) pairs."""
        current = tmux_cmd("show-option", "-gqv", option) or values[0][0]
        current_idx = 0
        for i, (val, _) in enumerate(values):
            if val == current:
                current_idx = i
                break
        next_idx = (current_idx + 1) % len(values)
        next_val, next_label = values[next_idx]
        tmux_cmd("set", "-g", option, next_val)
        self._build_menu()
        self._show_message(stdscr, f"Set to: {next_label}", 0.5)


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
