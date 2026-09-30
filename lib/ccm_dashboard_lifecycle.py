"""Dashboard access to the existing snapshot and recovery commands."""
import contextlib
import curses
import io
import json
import re
import sys
import time

import ccm_core
import ccm_roles
import ccm_snapshot as snapshot
import ccm_snapshot_store as store
import ccm_commands as commands


def snapshots():
    """Read metadata without initialization, recovery or a write lock."""
    rows = []
    for path in sorted(store.directory().glob('*.json')):
        if not path.stem.strip('.') or snapshot._sanitize_snapshot_name(path.stem) != path.stem:
            continue
        try:
            data = store.read(path)
            if data:
                rows.append((path.stem, f"{path.stem}  {data.get('created', '-')}  "
                             f"{len(data['projects'])} projects  v{data['version']}  "
                             f"{'protected' if store.sealed(data) else 'unprotected'}"))
        except store.SnapshotError:
            rows.append((path.stem, path.stem + '  unreadable — load to see the reason'))
    return rows


def restore_status(query):
    """One actionable line and the checkpoint to resume, if known."""
    try:
        progress = store.directory() / '.restore-state'
        if progress.exists():
            job = json.loads(progress.read_text())
            name = job.get('source')
            if not isinstance(name, str) or not name.strip('.') or snapshot._sanitize_snapshot_name(name) != name:
                return 'Restore needs attention; open Menu → Continue restore.', None
            return 'Restore incomplete; open Menu → Continue restore.', name
        sealed = store.sealed(store.read(store.directory() / '_autosave.json'))
        pending = query('list-windows', '-a', '-F', '#{@ccm_restore_pending}') or ''
        if '1' in pending.splitlines():
            return 'Restore incomplete; open Menu → Continue restore.', '_autosave' if sealed else None
        if sealed:
            return 'Checkpoint protected; use Menu to restore or cancel protection.', '_autosave'
    except (OSError, ValueError, AttributeError, store.SnapshotError):
        return 'Checkpoint needs attention; open Menu → Saved checkpoints.', None
    return '', None


class TerminalOutput:
    """Stream and retain output, neutralizing terminal controls from names."""
    def __init__(self, target):
        self.target = target
        self.buffer = io.StringIO()

    def write(self, text):
        # Keep newlines for progress and the final scrollable result. Use the
        # same neutralization as restore/role guidance for every other control.
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        safe = '\n'.join(ccm_roles.clean(line) for line in text.split('\n'))
        self.buffer.write(safe)
        self.target.write(safe)
        self.target.flush()
        return len(text)

    def flush(self):
        self.target.flush()


class LifecycleActions:
    def _lifecycle_text(self, stdscr, title, body):
        previous = getattr(self, '_render_max_col', 0)
        self._render_max_col = 0
        try:
            self._spool_text(stdscr, title, body)
        finally:
            self._render_max_col = previous

    def _run_terminal_command(self, stdscr, title, function, *args):
        """Run the CLI function outside curses; retain both output streams."""
        output = TerminalOutput(sys.stdout)
        success = False
        curses.def_prog_mode()
        curses.endwin()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                print(ccm_roles.clean(title), flush=True)
                try:
                    with ccm_core.raise_on_die():
                        function(*args)
                    success = True
                    print('\nCompleted.', flush=True)
                except (ccm_core.CCMError, OSError, ValueError) as exc:
                    print('\nFailed: ' + str(exc), flush=True)
                except SystemExit as exc:
                    success = exc.code in (None, 0)
                    print('\nCompleted.' if success else '\nFailed; see the reason above.', flush=True)
                except Exception as exc:
                    print('\nFailed: ' + str(exc), flush=True)
                except KeyboardInterrupt:
                    print('\nCancelled; inspect the result before retrying.', flush=True)
        finally:
            curses.reset_prog_mode()
            curses.curs_set(0)
            stdscr.timeout(50)
            stdscr.clearok(True)
            stdscr.refresh()
            self._lifecycle_checked = 0
            self._trigger_rebuild()
        self._lifecycle_text(stdscr, title, output.buffer.getvalue())
        return success

    def _lifecycle_banner(self, query):
        now = time.monotonic()
        if now - getattr(self, '_lifecycle_checked', 0) >= 2:
            self._lifecycle_line, _ = restore_status(query)
            self._lifecycle_checked = now
        return getattr(self, '_lifecycle_line', '')

    def _do_prepare_logout(self, stdscr, cancel=False):
        if cancel and self._prompt(stdscr, 'Release logout protection? [y/N]: ') not in ('y', 'Y'):
            return
        self._run_terminal_command(stdscr, 'Cancel logout protection' if cancel else 'Prepare for logout',
                                   snapshot.cmd_prepare_logout, ['--cancel'] if cancel else [])

    def _do_continue_restore(self, stdscr, query):
        _, name = restore_status(query)
        if name:
            self._run_terminal_command(stdscr, 'Continue restore', snapshot.cmd_snapshot_load, name)
        else:
            self._do_snapshots(stdscr)

    def _do_snapshots(self, stdscr):
        selected = 0
        rows = snapshots()
        previous_width = getattr(self, '_render_max_col', 0)
        self._render_max_col = 0
        stdscr.timeout(-1)
        try:
            while True:
                selected = min(selected, max(0, len(rows) - 1))
                height, _ = stdscr.getmaxyx()
                count = max(1, height - 3)
                start = max(0, selected - count + 1)
                stdscr.erase()
                self._addstr(stdscr, 0, 0, 'Saved checkpoints — Enter load; d delete; Esc back', curses.A_BOLD)
                if not rows:
                    self._addstr(stdscr, 2, 0, 'No saved checkpoints. Use Menu → Prepare for logout or Save snapshot.')
                for i, (_, label) in enumerate(rows[start:start + count], start):
                    self._addstr(stdscr, i - start + 2, 0,
                                 ('▶ ' if i == selected else '  ') + ccm_roles.clean(label))
                stdscr.refresh()
                key = stdscr.getch()
                if key in (27, ord('q')):
                    return
                if key in (curses.KEY_UP, ord('k')):
                    selected = max(0, selected - 1)
                elif key in (curses.KEY_DOWN, ord('j')):
                    selected = min(max(0, len(rows) - 1), selected + 1)
                elif rows and key in (10, 13, curses.KEY_ENTER, ord('d')):
                    name = rows[selected][0]
                    if key == ord('d'):
                        self._lifecycle_text(stdscr, 'Delete checkpoint', ccm_roles.clean(name))
                        if self._prompt(stdscr, 'Delete this checkpoint? [y/N]: ') not in ('y', 'Y'):
                            continue
                        self._run_terminal_command(stdscr, 'Delete checkpoint', snapshot.cmd_snapshot_delete, name)
                    else:
                        self._run_terminal_command(stdscr, 'Load checkpoint', snapshot.cmd_snapshot_load, name)
                    rows = snapshots()
                    selected = next((i for i, row in enumerate(rows) if row[0] == name), selected)
                    stdscr.timeout(-1)
        finally:
            self._render_max_col = previous_width
            stdscr.timeout(50)

    def _do_project_recovery(self, stdscr, action):
        with self.lock:
            project = self.projects[self.selected] if 0 <= self.selected < len(self.projects) else None
        if project is None:
            self._show_message(stdscr, 'Select a project in the dashboard first.', 3)
            return
        detail = ccm_roles.clean(project.name + ': ' + project.state)
        if action == 'reset':
            detail += '\nClear runtime signals and caches; keep conversations, processes, windows and checkpoints.'
            prompt = 'Clear runtime signals and caches? [y/N]: '
            function = commands.cmd_reset
        else:
            detail += '\nKeep the window and sidekicks. In PERMIT, Escape rejects the pending tool call.'
            prompt = 'Exit Claude, keeping its window? [y/N]: '
            function = commands.cmd_exit
        self._lifecycle_text(stdscr, 'Review selected project', detail)
        if self._prompt(stdscr, prompt) in ('y', 'Y'):
            self._run_terminal_command(stdscr, 'Reset runtime state' if action == 'reset' else 'Exit Claude', function, project.name if action == 'reset' else
                                       ([project.name, '-y'] if project.state in ('BUSY', 'PERMIT') else [project.name]))

    def _do_exit_all(self, stdscr):
        with self.lock:
            targets = [p for p in self.projects if p.state in ('IDLE', 'BUSY', 'PERMIT')]
        if not targets:
            self._show_message(stdscr, 'No running Claude sessions to exit.', 3)
            return
        body = '\n'.join(ccm_roles.clean(p.name + ': ' + p.state) for p in targets)
        body += '\n\nWindows and sidekicks remain. In PERMIT, Escape rejects the pending tool call.'
        self._lifecycle_text(stdscr, 'Review Claude sessions to exit', body)
        if self._prompt(stdscr, 'Exit Claude in the listed projects? [y/N]: ') not in ('y', 'Y'):
            return
        def batch():
            failed = False
            for project in targets:
                try:
                    commands.cmd_exit([project.name, '-y'] if project.state in ('BUSY', 'PERMIT') else [project.name])
                except ccm_core.CCMError as exc:
                    failed = True
                    print(str(exc))
            if failed:
                ccm_core.ccm_die('Some Claude sessions remain; inspect the reasons above.')
        self._run_terminal_command(stdscr, 'Exit Claude sessions', batch)
