"""The dashboard's actions on the selected row: attach, save, preview,
spool, and adding, renaming, removing, ignoring and registering
projects."""

import curses
import os
import subprocess
import time

import ccm_agentview
import ccm_presentation
import ccm_spool
from ccm_commands import (
    cmd_add,
    cmd_ignore,
    cmd_register,
    cmd_remove,
    cmd_unignore,
    cmd_unregister,
)
from ccm_constants import CCM_SNAPSHOT_DIR
from ccm_core import (
    CCMError,
    build_project_list,
    get_session,
    ps_snapshot,
    raise_on_die,
    tmux_cmd,
    tmux_query,
)
from ccm_pane_state import enumerate_window_panes
from ccm_render import display_width
from ccm_snapshot import cmd_snapshot_save
from ccm_window import auto_start_claude, reset_window_after_attach
from dashboard_shared import _announce_unlaunched

#: Window option marking a window the dashboard opened to attach to a
#: background session; its value is the session's short id.
BG_ATTACH_TAG = "@ccm_bg_short"



class ActionsMixin:

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
