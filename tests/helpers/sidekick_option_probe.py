"""Called only through the Bats tmux guard with an allocated isolated socket."""
import subprocess
import sys
import ccm_core
import ccm_sidekick_notify as notices

socket, window = sys.argv[1:]
def query(*args, **kwargs):
    return subprocess.check_output(['tmux', '-L', socket, '-f', '/dev/null', *args], text=True).strip()
ccm_core.tmux_query = query
for name, default, global_value, local_value in (
        (notices.OPTION, 'off', 'on', 'off'),
        (notices.LIMIT, '20', '7', '0'),
        (notices.EXCERPT, 'on', 'off', 'on')):
    assert notices.effective_option(window, name) == (default, 'default')
    query('set-option', '-g', name, global_value)
    assert query('show-options', '-wAqv', '-t', window, name) == ''
    assert notices.effective_option(window, name) == (global_value, 'global (-g)')
    query('set-option', '-gw', name, local_value)
    assert query('show-options', '-wqv', '-t', window, name) == ''
    assert query('show-options', '-wAqv', '-t', window, name) == local_value
    assert notices.effective_option(window, name) == (local_value, 'global (-gw)')
    query('set-option', '-w', '-t', window, name, global_value)
    assert notices.effective_option(window, name) == (global_value, 'window')
    query('set-option', '-wu', '-t', window, name)
    query('set-option', '-gwu', name)
    assert notices.effective_option(window, name) == (global_value, 'global (-g)')
