#!/usr/bin/env python3
"""The docked dashboard: a full-width pane at the top (or bottom) of the
current window instead of a popup, so the dashboard stays visible while
you work. It follows you: switching windows moves the pane, and the
window it left gets its layout back.

    ccm dock toggle   open here, move here, or close (bound to the key)
    ccm dock follow   move to the current window if it is elsewhere (hook)
    ccm dock close    close it and give its rows back

The pane carries `@ccm_dock` so the rest of ccm can set it aside: the
checkpoint collector leaves it out, and launching never picks it.
"""
import contextlib
import fcntl
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccm_core  # noqa: E402
import ccm_layout  # noqa: E402
import ccm_snapshot_store as store  # noqa: E402

DOCK_OPTION = '@ccm_dock'
# Set on a dock that is about to close after opening a project, so a
# window switch does not carry it into the new window first.
LEAVING_OPTION = '@ccm_dock_leaving'
# The window's layout before the dock arrived, and the rest of the window
# as the dock left it. If the rest is still that, the window gets back
# exactly what it had (shrinking loses rounding that rescaling cannot
# recover); if the user changed it meanwhile, their version is kept.
BEFORE_OPTION = '@ccm_dock_before'
AFTER_OPTION = '@ccm_dock_after'
POSITION_OPTION = '@ccm-dashboard-dock'
SIZE_OPTION = '@ccm-dashboard-dock-size'
DEFAULT_SIZE = 40
# The dashboard refuses to draw below this many rows.
MIN_ROWS = 10


def position():
    """'top', 'bottom', or '' when docking is off (the popup is used)."""
    value = (ccm_core.tmux_cmd('show-option', '-gqv', POSITION_OPTION) or '').strip()
    return value if value in ('top', 'bottom') else ''


def rows_for(window_height):
    raw = (ccm_core.tmux_cmd('show-option', '-gqv', SIZE_OPTION) or '').strip().rstrip('%')
    try:
        percent = int(raw)
    except ValueError:
        percent = DEFAULT_SIZE
    percent = min(max(percent, 10), 80)
    return max(MIN_ROWS, window_height * percent // 100)


def find(include_leaving=True):
    """(pane_id, window_id) of the docked dashboard, or None. A dock that
    is about to close counts only with `include_leaving`."""
    listing = ccm_core.tmux_query('list-panes', '-a', '-F',
                                  '#{pane_id}\t#{window_id}\t#{' + DOCK_OPTION + '}\t#{'
                                  + LEAVING_OPTION + '}')
    for line in (listing or '').splitlines():
        parts = line.split('\t')
        if len(parts) >= 3 and parts[2] == '1':
            if not include_leaving and len(parts) > 3 and parts[3] == '1':
                return None
            return parts[0], parts[1]
    return None


def mark_leaving(pane, leaving=True):
    """Mark `pane` (the dock itself) as closing, or clear the mark; see
    LEAVING_OPTION."""
    if not pane:
        return
    if leaving:
        ccm_core.tmux_query('set-option', '-p', '-t', pane, LEAVING_OPTION, '1')
    else:
        ccm_core.tmux_query('set-option', '-pu', '-t', pane, LEAVING_OPTION)


def current_window():
    return ccm_core.tmux_query('display-message', '-p', '#{window_id}') or ''


def _window_size(window):
    raw = ccm_core.tmux_query('display-message', '-p', '-t', window,
                              '#{window_layout}\t#{window_height}') or ''
    layout, _, height = raw.partition('\t')
    return layout, int(height) if height.isdigit() else 0


@contextlib.contextmanager
def _serialized():
    """One dock operation at a time across the server: key presses and
    window hooks run concurrently, and each re-reads the dock under this
    lock, so two cannot both see "no dock" and open two."""
    os.makedirs(ccm_core.CCM_TMP_DIR, exist_ok=True)
    with open(os.path.join(ccm_core.CCM_TMP_DIR, 'dock.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _window_option(window, name):
    return ccm_core.tmux_query('show-option', '-wqv', '-t', window, name) or ''


def layout_without_dock(window, layout, pane):
    """`window`'s layout without the dock pane: what it had before the dock
    came, when the rest is untouched since; otherwise the current rest."""
    try:
        rest = ccm_layout.without(layout, pane)
    except (store.SnapshotError, KeyError, ValueError, TypeError):
        return None
    if rest == _window_option(window, AFTER_OPTION):
        return _window_option(window, BEFORE_OPTION) or rest
    return rest


def _layout_without(window, pane):
    layout, _ = _window_size(window)
    return layout_without_dock(window, layout, pane)


def _remember(window, before, pane):
    layout, _ = _window_size(window)
    try:
        after = ccm_layout.without(layout, pane)
    except (store.SnapshotError, KeyError, ValueError, TypeError):
        return
    ccm_core.tmux_query('set-option', '-w', '-t', window, BEFORE_OPTION, before)
    ccm_core.tmux_query('set-option', '-w', '-t', window, AFTER_OPTION, after)


def _forget(window):
    for name in (BEFORE_OPTION, AFTER_OPTION):
        ccm_core.tmux_query('set-option', '-wu', '-t', window, name)


def _restore(window, layout):
    """Give the rows back in the proportions the other panes had."""
    if layout:
        ccm_core.tmux_query('select-layout', '-t', window, layout)
    _forget(window)


def dashboard_command():
    # Tests replace the dashboard with an inert command.
    if os.environ.get('CCM_DOCK_COMMAND'):
        return os.environ['CCM_DOCK_COMMAND']
    ccm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ccm')
    return f'{shlex.quote(ccm)} dashboard --docked'


def open_here(window):
    where = position()
    before, height = _window_size(window)
    rows = rows_for(height)
    args = ['split-window', '-f', '-v', '-l', str(rows), '-t', window, '-P', '-F', '#{pane_id}']
    if where == 'top':
        args.insert(1, '-b')
    pane = ccm_core.tmux_query(*args, dashboard_command())
    if pane:
        ccm_core.tmux_query('set-option', '-p', '-t', pane, DOCK_OPTION, '1')
        _remember(window, before, pane)
    return pane


def move_to(pane, source, target):
    """Move the dock pane into `target`, keeping focus where it is."""
    restored = _layout_without(source, pane)
    before, height = _window_size(target)
    args = ['join-pane', '-d', '-f', '-v', '-l', str(rows_for(height)), '-s', pane, '-t', target]
    if position() == 'top':
        args.insert(1, '-b')
    if ccm_core.tmux_query(*args) is None:
        return False
    _remember(target, before, pane)
    _restore(source, restored)
    return True


def close():
    with _serialized():
        _close()


def _close():
    found = find()
    if not found:
        return
    pane, window = found
    restored = _layout_without(window, pane)
    ccm_core.tmux_query('kill-pane', '-t', pane)
    _restore(window, restored)


def request_close():
    """Close the dock from inside it: tmux runs the close, so it can restore
    the layout after the pane (and the dashboard in it) is gone."""
    ccm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ccm')
    ccm_core.tmux_query('run-shell', '-b', f'{shlex.quote(ccm)} dock close')


def toggle(here=None):
    with _serialized():
        _toggle(here)


def _toggle(here):
    here = here or current_window()
    found = find()
    if not found:
        open_here(here)
    elif found[1] == here:
        _close()
    elif move_to(found[0], found[1], here):
        ccm_core.tmux_query('select-pane', '-t', found[0])


def watched_window(hint=''):
    """The window the most recently active client is showing, or ''.

    Activity is kept in whole seconds, so clients used within the same
    second tie; the tie goes to `hint` (the window the hook reported) when
    one of them shows it, otherwise to the first listed. Control-mode
    clients (usually scripts, without a screen) count only when no
    ordinary client is attached."""
    listing = ccm_core.tmux_query('list-clients', '-F',
                                  '#{client_activity}\t#{client_control_mode}\t#{window_id}') or ''
    rows = []
    for line in listing.splitlines():
        parts = line.split('\t')
        if len(parts) == 3 and parts[0].isdigit() and parts[2]:
            rows.append((int(parts[0]), parts[1] == '1', parts[2]))
    screens = [r for r in rows if not r[1]] or rows
    if not screens:
        return ''
    latest = max(r[0] for r in screens)
    tied = [r[2] for r in screens if r[0] == latest]
    return hint if hint in tied else tied[0]


def follow(here=None):
    """Move the dock to the window being worked in. Window hooks can run
    out of order after quick switches, also across sessions, so the target
    is decided afresh under the lock from the most recently active client;
    the hook's window is only a fallback when no client can be read."""
    with _serialized():
        found = find(include_leaving=False)
        if not found:
            return
        target = watched_window(here or '') or here or current_window()
        if target and found[1] != target:
            move_to(found[0], found[1], target)


def main(argv):
    command = argv[0] if argv else 'toggle'
    # Key bindings and hooks pass the window as `#{window_id}`, expanded by
    # tmux in the client's context; run from a pane, "current" would mean
    # the pane's own window instead.
    window = argv[1] if len(argv) > 1 else None
    if command == 'toggle':
        toggle(window)
    elif command == 'follow':
        follow(window)
    elif command == 'close':
        close()
    else:
        print('Usage: ccm dock [toggle|follow|close]', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
