"""Menu descriptions and shared preview geometry. No terminal queries."""
import ccm_roles
from ccm_render import display_width

# One source for the English preview text. Keys are the menu's action values.
MENU_HELP = {
    'add': 'Create a project window and start Claude.\nPrompts for a directory and name; asks before creating a missing directory.\nSame as: ccm add <dir> [name]',
    'unregister': 'Remove a project from ccm management.\nIts window and running processes remain. Choose the project at the prompt.\nSame as: ccm unregister <name>',
    'delete': 'Close a project window and its running processes.\nChoose the project, then confirm with y; Enter or Esc cancels.\nSame as: ccm remove <name>',
    'ignore': 'Hide a project from tracking, or restore it.\nIts processes keep running. Hiding asks for confirmation; restoring does not.\nSame as: ccm ignore <name> / ccm unignore <name>',
    'spool': 'Review expired and held messages.\nReading sends nothing. Resending and deleting require confirmation.\nCLI: ccm spool list / show / resend / discard',
    'save': 'Save the current projects and pane layout.\nPrompts for a checkpoint name. Protected checkpoints stay unchanged.\nSame as: ccm snapshot save <name>',
    'load': 'Choose a saved checkpoint to load or delete.\nLoading shows progress and results. Deletion asks for confirmation; protected autosave cannot be deleted.\nCLI: ccm snapshot load <name> / delete <name>',
    'prepare_logout': 'Save and protect the autosave checkpoint before logout.\nWindows and agents keep running. BUSY or PERMIT projects require confirmation.\nSame as: ccm prepare-logout',
    'cancel_logout': 'Release logout protection while keeping the checkpoint.\nAsks for confirmation; an incomplete restore must finish first.\nSame as: ccm prepare-logout --cancel',
    'continue_restore': 'Continue an incomplete restore from its recorded checkpoint.\nExisting completed panes remain. Progress and final guidance stay readable.\nSame as: ccm start <checkpoint>',
    'reset': 'Clear runtime signals and caches for the selected project.\nConversations, processes, windows and checkpoints remain. Asks for confirmation.\nSame as: ccm reset <name>',
    'exit': 'Exit Claude in the selected project; keep its window and sidekicks.\nAsks for confirmation. In PERMIT, Escape rejects the pending tool call. Unsafe screens are refused.\nSame as: ccm exit <name> [-y]',
    'status_mode': 'Choose minimal, window-list or dedicated status display.\nPrompts for a mode and saves the setting; projects keep running.\nCurrent: {current}',
    'auto_restore': 'Toggle restoring the autosave checkpoint when tmux starts.\nApplies at the next startup; this does not start a restore now. Saves immediately.\nCurrent: {current}',
    'idle_timeout': 'Set how long idle Claude sessions wait before automatic exit.\nPrompts for minutes; 0 disables it. Saves the setting and keeps windows.\nCurrent: {current}',
    'preview_toggle': 'Toggle the project preview and menu descriptions.\nApplies immediately and saves the setting. Projects keep running.\nCurrent: {current}',
    'preview_position': 'Move the preview between the right and bottom.\nApplies immediately and saves the setting; small terminals hide the panel.\nCurrent: {current}',
    'bg_section': 'Toggle the background-session section on the dashboard.\nSaves immediately. Background sessions keep running.\nCurrent: {current}',
    'notify': 'Cycle which state changes produce notifications.\nSaves immediately; running projects are unaffected.\nCurrent: {current}',
    'notify_sound': 'Toggle notification sounds.\nSaves immediately and plays a sample when enabled.\nCurrent: {current}',
    'sound_name': 'Choose the next notification sound and hear a sample.\nSaves immediately; notification event selection stays unchanged.\nCurrent: {current}',
    'auto_start': 'Toggle starting Claude when you open a shell project.\nSaves immediately; no agent starts just by changing this setting.\nCurrent: {current}',
    'dashboard': 'Return to the project dashboard.\nKeeps projects and running processes unchanged.\nSame as: ccm dashboard',
    'tree': 'Show the session, window and pane hierarchy.\nSwitching views leaves running processes unchanged.\nSame as: ccm tree-interactive',
    'quit': 'Close this menu and dashboard.\nProjects, windows and running agents remain.\nNo confirmation is needed.',
}


def description(action, label):
    current = ccm_roles.clean(label.partition(': ')[2])
    return MENU_HELP[action].replace('{current}', current)


def preview_geometry(width, height, enabled, position):
    """list width/height, preview column/row/width/height."""
    if enabled and position == 'right' and width >= 80:
        panel_width = min(width // 2, width - 40)
        return width - panel_width - 1, height, width - panel_width - 1, 0, panel_width, height - 1
    if enabled and position == 'bottom' and height >= 20:
        panel_height = min(height // 2, height - 10)
        return width, height - panel_height - 1, 0, height - panel_height - 1, width, panel_height
    return width, height, 0, 0, 0, 0


def wrap_text(text, width):
    """Wrap by terminal columns, including long words and wide characters."""
    width = max(1, width)
    rows = []
    for line in text.split('\n'):
        row = ''
        for word in line.split():
            if row and display_width(row + ' ' + word) <= width:
                row += ' ' + word
                continue
            if row:
                rows.append(row)
                row = ''
            for char in word:
                if row and display_width(row + char) > width:
                    rows.append(row)
                    row = ''
                row += char
        rows.append(row)
    return rows
