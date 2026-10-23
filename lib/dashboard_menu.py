"""The dashboard's menu: actions, settings and navigation, with a
description pane and a live filter. A mixin of `dashboard.Dashboard`."""

import curses
import platform
import subprocess

import ccm_menu_help
import ccm_roles
from ccm_commands import cmd_remove, cmd_unregister
from ccm_constants import IDLE_EXIT_TIMEOUT
from ccm_core import save_tmux_conf_setting, tmux_cmd, tmux_query
from ccm_render import display_width
from dashboard_shared import C_COMPLETED, C_CYAN, C_DIM, C_YELLOW

_IS_MACOS = platform.system() == "Darwin"


class MenuMixin:

    def _do_menu_removal(self, stdscr, action):
        """Unregister, delete, or ignore-toggle a project named at
        the prompt.

        Reached from the menu, which has no selection of its own —
        its job is to make the operation findable at all. The
        dashboard list is the quicker route once you know it exists,
        and the not-found message points there.
        """
        verb = {"unregister": "Unregister", "delete": "Delete",
                "ignore": "Ignore or unignore"}[action]
        # The list's selection survives into menu mode, so a plain
        # Enter targets it — typing a name the dashboard is already
        # pointing at would be busywork. Esc still cancels; with no
        # usable selection an empty answer cancels as before.
        default = ""
        if 0 <= self.selected < len(self.projects):
            default = self.projects[self.selected].name
        suffix = f" [{default}]" if default else ""
        name = self._prompt(stdscr, f"{verb} which project{suffix}? ")
        if name is None:
            return
        name = name.strip() or default
        if not name:
            return
        known = {p.name for p in self.projects}
        if name not in known:
            self._show_message(
                stdscr,
                f"No such project: {name}"
                f"  (tip: select it in the list and press r or i)", 2)
            return
        if action == "ignore":
            target = next(p for p in self.projects if p.name == name)
            ok = self._toggle_project_ignore(stdscr, target)
        elif action == "unregister":
            ok = self._run_cmd(stdscr, cmd_unregister, name)
        else:
            confirm = self._prompt(
                stdscr, f"Close {name}'s window and its session? [y/N]: ")
            if not confirm or confirm.lower() != "y":
                return
            ok = self._run_cmd(stdscr, cmd_remove, name)
        if ok:
            self.mode = "dashboard"
            self._trigger_rebuild()

    def _build_menu(self):
        """Build menu items dynamically with current setting values."""
        self.help_language = tmux_cmd("show-option", "-gqv", "@ccm-lang")
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

        # Dashboard display: popup, or docked at the top / bottom
        dock_mode = tmux_cmd("show-option", "-gqv", "@ccm-dashboard-dock")
        dock_mode = dock_mode if dock_mode in ("top", "bottom") else "popup"
        dock_close = tmux_cmd("show-option", "-gqv", "@ccm-dashboard-dock-close-on-open") or "off"

        is_macos = _IS_MACOS

        self.menu_items = [
            ("Actions", ""),  # heading
            ("Add project", "add"),
            # Removal belongs here too. The menu is where a reader
            # finds out WHAT ccm can do, and leaving these out made
            # unregistering look unsupported — the dashboard offers
            # both, but only behind `r`, which says only "remove".
            ("Unregister project (keep window)", "unregister"),
            ("Delete project (close window)", "delete"),
            # Listed beside them so the reversible option is found by
            # anyone looking for a way out, without sharing a
            # confirmation prompt with the irreversible one.
            ("Ignore / unignore project", "ignore"),
            ("Undelivered messages", "spool"),
            ("Save snapshot", "save"),
            ("Saved checkpoints (load / delete)", "load"),
            ("Prepare for logout", "prepare_logout"),
            ("Cancel logout protection", "cancel_logout"),
            ("Continue restore", "continue_restore"),
            ("Finish restore (keep what is restored)", "finish_restore"),
            ("Reset selected project's runtime state", "reset"),
            ("Exit Claude in selected project (keep window)", "exit"),
            ("Settings", ""),  # heading
            (f"Status bar mode: {mode_label}", "status_mode"),
            (f"Auto-restore: {auto_restore}", "auto_restore"),
            (f"Idle timeout: {idle_label}", "idle_timeout"),
            (f"Preview panel: {preview_on}", "preview_toggle"),
            (f"Preview position: {preview_pos_label}", "preview_position"),
            (f"Dashboard display: {dock_mode}", "dock_mode"),
        ]
        if dock_mode != "popup":
            self.menu_items += [
                (f"Close dock after opening a project: {dock_close}", "dock_close"),
            ]
        self.menu_items += [
            (f"Background sessions: {self.bg_section_setting}", "bg_section"),
            (f"Auto-start Claude: {auto_start}", "auto_start"),
            (f"Notifications: {notify}", "notify"),
        ]
        if is_macos:
            self.menu_items += [
                (f"Notification sound: {notify_sound}", "notify_sound"),
                (f"Sound name: {sound_name}", "sound_name"),
            ]
        self.menu_items += [
            ("Navigate", ""),  # heading
            ("Dashboard", "dashboard"),
            ("Tree view", "tree"),
            ("Quit", "quit"),
        ]
        self._normalize_menu_selection()

    def _normalize_menu_selection(self):
        """Headings are not selectable: land on the next item instead."""
        selectable = [i for i, (_, a) in enumerate(self.menu_items) if a]
        if selectable and self.menu_selected not in selectable:
            self.menu_selected = next((i for i in selectable if i >= self.menu_selected),
                                      selectable[0])

    @staticmethod
    def _menu_value_attr(value):
        if value in ("on", "always"):
            return curses.color_pair(C_COMPLETED)
        if value == "off":
            return curses.color_pair(C_DIM)
        return curses.color_pair(C_CYAN)

    def _put_segments(self, stdscr, y, x, segments, max_col=0):
        """Draw (text, attr) pieces left to right; returns the end column."""
        for text, attr in segments:
            if text:
                self._addstr(stdscr, y, x, text, attr, max_col=max_col)
                x += display_width(text)
        return x

    def _menu_label_segments(self, label, action, selected, query=""):
        text = ccm_roles.clean(label)
        base = curses.A_BOLD if selected else 0
        name, sep, value = text.partition(": ")
        if action in self._MENU_SETTINGS and sep:
            pieces = [(name + sep, base), (value, self._menu_value_attr(value) | base)]
        else:
            pieces = [(text, base)]
        if not query:
            return pieces
        # Highlight the first match over whatever styling the span had.
        folded = query.casefold()
        start = text.casefold().find(folded)
        # Folding that changes lengths (ß → ss) would misplace the span.
        if start < 0 or len(text.casefold()) != len(text) or len(folded) != len(query):
            return pieces
        end = start + len(query)
        out, pos = [], 0
        for piece, attr in pieces:
            lo, hi = pos, pos + len(piece)
            cut = [lo, max(lo, min(hi, start)), max(lo, min(hi, end)), hi]
            for (i, j), hit in zip(zip(cut, cut[1:]), (False, True, False)):
                if i < j:
                    out.append((text[i:j], curses.color_pair(C_YELLOW) | curses.A_BOLD if hit else attr))
            pos = hi
        return out

    def _render_menu_description(self, stdscr, col, row, width, height):
        if not (0 <= self.menu_selected < len(self.menu_items)):
            return
        label, action = self.menu_items[self.menu_selected]
        if not action:
            return
        if self.preview_position == "right":
            for y in range(row, row + height):
                stdscr.addch(y, col, "│", curses.color_pair(C_DIM))
            x, y, available, count = col + 2, row, width - 3, height
        else:
            stdscr.addstr(row, 0, "─" * (width - 1), curses.color_pair(C_DIM))
            x, y, available, count = 1, row + 1, width - 2, height - 1
        limit = x + available + 1
        rows = [(line, [(line, curses.A_BOLD | curses.color_pair(C_CYAN))])
                for line in ccm_menu_help.wrap_text(ccm_roles.clean(label), available)]
        rows.append(("", []))
        body = ccm_menu_help.description(action, label, getattr(self, "help_language", "en"))
        for source in body.split("\n"):
            prefix = next((p for p in self._HELP_COMMAND_PREFIXES + self._HELP_VALUE_PREFIXES
                           if source.startswith(p)), "")
            if prefix in self._HELP_COMMAND_PREFIXES:
                rest_attr = curses.color_pair(C_YELLOW)
            elif prefix:
                rest_attr = self._menu_value_attr(source[len(prefix):].strip())
            else:
                rest_attr = 0
            for n, wrapped in enumerate(ccm_menu_help.wrap_text(source, available)):
                if n == 0 and prefix and wrapped.startswith(prefix.rstrip()):
                    head = wrapped[:len(prefix.rstrip())]
                    rows.append((wrapped, [(head, curses.color_pair(C_DIM)),
                                           (wrapped[len(head):], rest_attr)]))
                else:
                    rows.append((wrapped, [(wrapped, rest_attr)]))
        for i, (_, segments) in enumerate(rows[:count]):
            self._put_segments(stdscr, y + i, x, segments, max_col=limit)

    def _render_menu(self, stdscr, query=None, matches=None):
        """Draw the menu. With `query` not None, only `matches` (menu
        indices) are listed and a filter prompt occupies the bottom."""
        self._render_max_col = 0
        try:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            if height < self.MIN_HEIGHT or width < self.MIN_WIDTH:
                stdscr.addstr(0, 0, f"Terminal too small ({width}x{height})"[:width - 1])
                stdscr.refresh()
                return

            (list_width, list_height, col, row, panel_width,
             panel_height) = ccm_menu_help.preview_geometry(
                width, height, self.preview_enabled, self.preview_position)
            self._render_max_col = list_width if panel_width else 0
            self._normalize_menu_selection()
            self._put_segments(stdscr, 0, 2, [
                ("Menu", curses.A_BOLD | curses.color_pair(C_CYAN)),
                ("  (/=filter, d=dashboard, q=quit)", curses.color_pair(C_DIM))])
            filtering = query is not None
            shown = matches if filtering else range(len(self.menu_items))
            bottom = list_height - (3 if filtering else 1)
            menu_row = 2
            visible = max(1, bottom - menu_row)
            position = list(shown).index(self.menu_selected) if self.menu_selected in shown else 0
            start = max(0, position - visible + 1)
            for i in list(shown)[start:]:
                if menu_row >= bottom:
                    break
                label, action = self.menu_items[i]
                if not action:
                    self._addstr(stdscr, menu_row, 2, ccm_roles.clean(label),
                                 curses.A_BOLD | curses.color_pair(C_DIM))
                    menu_row += 1
                    continue
                selected = i == self.menu_selected
                prefix = "  ▶ " if selected else "    "
                self._addstr(stdscr, menu_row, 0, prefix,
                             curses.A_BOLD | curses.color_pair(C_CYAN) if selected else 0)
                end = self._put_segments(stdscr, menu_row, display_width(prefix),
                                         self._menu_label_segments(label, action, selected, query or ""))
                key = ccm_menu_help.MENU_KEYS.get(action)
                if key:
                    hint = f"[{key}]"
                    hint_col = list_width - 1 - display_width(hint)
                    if end + 2 <= hint_col:
                        self._addstr(stdscr, menu_row, hint_col, hint,
                                     curses.color_pair(C_DIM))
                menu_row += 1
            if filtering:
                if not matches:
                    self._addstr(stdscr, 2, 4, "(no match)", curses.color_pair(C_DIM))
                total = sum(1 for _, a in self.menu_items if a)
                prompt_row = list_height - 2
                self._addstr(stdscr, prompt_row, 2, "Filter: ", curses.color_pair(C_DIM))
                count = f"{len(matches)}/{total}"
                count_col = list_width - display_width(count) - 2
                # Scroll a long query so its end and the cursor stay in view.
                shown_query = query
                while shown_query and 10 + display_width(shown_query) > list_width - 2:
                    shown_query = self._strip_first_grapheme(shown_query)
                self._addstr(stdscr, prompt_row, 10, shown_query, curses.A_BOLD)
                if count_col > 10 + display_width(shown_query) + 2:
                    self._addstr(stdscr, prompt_row, count_col, count, curses.color_pair(C_DIM))
                self._addstr(stdscr, list_height - 1, 2,
                             "[↑↓] select  [Enter] run  [C-u] clear  [Esc] back",
                             curses.color_pair(C_DIM))
            if panel_height and (not filtering or matches):
                self._render_menu_description(stdscr, col, row, panel_width, panel_height)
            if filtering:
                try:
                    stdscr.move(list_height - 2, 10 + display_width(shown_query))
                except curses.error:
                    pass
            stdscr.refresh()
        except curses.error:
            pass

    def _menu_matches(self, query):
        """Menu indices whose label, or description summary in the
        current help language, contains `query` (case-insensitive)."""
        q = query.casefold()
        language = getattr(self, "help_language", "en")
        found = []
        for i, (label, action) in enumerate(self.menu_items):
            if not action:
                continue
            if q in ccm_roles.clean(label).casefold():
                found.append(i)
                continue
            summary = ccm_menu_help.description(action, label, language).split("\n")[0]
            if q in summary.casefold():
                found.append(i)
        return found

    def _do_menu_filter(self, stdscr):
        """Live filter for the menu, like the project filter. Enter runs
        the selected item through the normal menu handler; Esc returns
        to the full menu with that item still selected."""
        buf, sel, chosen = "", 0, False
        try:
            prev_cursor = curses.curs_set(1)
        except curses.error:
            prev_cursor = 1
        stdscr.timeout(-1)
        try:
            while True:
                matches = self._menu_matches(buf)
                sel = min(max(sel, 0), max(len(matches) - 1, 0))
                if matches:
                    self.menu_selected = matches[sel]
                self._render_menu(stdscr, buf, matches)
                try:
                    wch = stdscr.get_wch()
                except curses.error:
                    continue
                except KeyboardInterrupt:
                    return ""
                if isinstance(wch, str):
                    if wch in ("\x1b", "\x03", "\x07"):
                        return ""
                    if wch in ("\n", "\r"):
                        if matches:
                            chosen = True
                            break
                        continue
                    if wch in ("\x7f", "\b"):
                        buf, sel = self._strip_last_grapheme(buf), 0
                    elif wch == "\x15":
                        buf, sel = "", 0
                    elif wch == "\x10":
                        sel = (sel - 1) % len(matches) if matches else 0
                    elif wch == "\x0e":
                        sel = (sel + 1) % len(matches) if matches else 0
                    elif wch >= " ":
                        buf, sel = buf + wch, 0
                elif wch == curses.KEY_ENTER:
                    if matches:
                        chosen = True
                        break
                elif wch in (curses.KEY_BACKSPACE, 127, 8):
                    buf, sel = self._strip_last_grapheme(buf), 0
                elif wch == curses.KEY_UP and matches:
                    sel = (sel - 1) % len(matches)
                elif wch == curses.KEY_DOWN and matches:
                    sel = (sel + 1) % len(matches)
        finally:
            try:
                curses.curs_set(prev_cursor)
            except curses.error:
                pass
            stdscr.timeout(50)
        return self._handle_menu_key(10, stdscr) if chosen else ""

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

        if key == ord("/"):
            return self._do_menu_filter(stdscr)
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
            elif action in ("unregister", "delete", "ignore"):
                self._do_menu_removal(stdscr, action)
                self._build_menu()
            elif action == "spool":
                return self._do_spool(stdscr)
            elif action == "save":
                self._do_save(stdscr)
            elif action == "load":
                self._do_snapshots(stdscr)
            elif action in ("prepare_logout", "cancel_logout"):
                self._do_prepare_logout(stdscr, cancel=action == "cancel_logout")
            elif action == "continue_restore":
                self._do_continue_restore(stdscr, tmux_query)
            elif action == "finish_restore":
                self._do_finish_restore(stdscr)
            elif action in ("reset", "exit"):
                self._do_project_recovery(stdscr, action)
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
            elif action == "dock_mode":
                current = tmux_cmd("show-option", "-gqv", "@ccm-dashboard-dock")
                order = ["off", "top", "bottom"]
                new_val = order[(order.index(current) + 1) % 3 if current in order else 1]
                tmux_cmd("set", "-g", "@ccm-dashboard-dock", new_val)
                save_tmux_conf_setting(f"set -g @ccm-dashboard-dock {new_val}")
                self._build_menu()
                if self.docked and new_val == "off":
                    # The dock is closing with docking turned off.
                    self._show_message(stdscr, "Dashboard display: popup (closing the dock)", 1)
                    return "quit"
                self._show_message(stdscr, "Dashboard display: "
                                   + ("popup" if new_val == "off" else new_val)
                                   + (" (when the dock next opens or moves)" if self.docked
                                      else " (next time the dashboard opens)"), 1)
            elif action == "dock_close":
                current = tmux_cmd("show-option", "-gqv", "@ccm-dashboard-dock-close-on-open") or "off"
                new_val = "off" if current == "on" else "on"
                tmux_cmd("set", "-g", "@ccm-dashboard-dock-close-on-open", new_val)
                save_tmux_conf_setting(f"set -g @ccm-dashboard-dock-close-on-open {new_val}")
                self._build_menu()
                self._show_message(stdscr, f"Close dock after opening a project: {new_val}", 0.5)
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

    # Items whose label ends in `: <current value>`.
    _MENU_SETTINGS = frozenset({
        "status_mode", "auto_restore", "idle_timeout", "preview_toggle",
        "preview_position", "dock_mode", "dock_close", "bg_section", "auto_start", "notify",
        "notify_sound", "sound_name"})
    # Preview lines that name a command or a current value.
    _HELP_COMMAND_PREFIXES = ("Same as: ", "CLI: ")
    _HELP_VALUE_PREFIXES = ("Current: ", "現在値: ")
