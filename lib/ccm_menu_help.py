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
    'finish_restore': 'End an incomplete restore with the projects restored so far.\nHeld-back projects leave the checkpoint, which is kept whole under a separate name. Asks for confirmation.\nSame as: ccm finish-restore',
    'reset': 'Clear runtime signals and caches for the selected project.\nConversations, processes, windows and checkpoints remain. Asks for confirmation.\nSame as: ccm reset <name>',
    'exit': 'Exit Claude in the selected project; keep its window and sidekicks.\nAsks for confirmation. In PERMIT, Escape rejects the pending tool call. Unsafe screens are refused.\nSame as: ccm exit <name> [-y]',
    'status_mode': 'Choose minimal, window-list or dedicated status display.\nPrompts for a mode and saves the setting; projects keep running.\nCurrent: {current}',
    'auto_restore': 'Toggle restoring the autosave checkpoint when tmux starts.\nApplies at the next startup; this does not start a restore now. Saves immediately.\nCurrent: {current}',
    'idle_timeout': 'Set how long idle Claude sessions wait before automatic exit.\nPrompts for minutes; 0 disables it. Saves the setting and keeps windows.\nCurrent: {current}',
    'preview_toggle': 'Toggle the project preview and menu descriptions.\nApplies immediately and saves the setting. Projects keep running.\nCurrent: {current}',
    'preview_position': 'Move the preview between the right and bottom.\nApplies immediately and saves the setting; small terminals hide the panel.\nCurrent: {current}',
    'dock_mode': 'Show the dashboard as a popup, or docked as a pane at the top or bottom.\nA docked dashboard stays in view and follows you between windows. Saves the setting; top or bottom applies when the dock next opens or moves to another window, and popup closes a docked dashboard.\nCurrent: {current}',
    'dock_close': 'Close the docked dashboard after opening a project from it.\nWhen off, it stays open and moves with you. Saves the setting.\nCurrent: {current}',
    'bg_section': 'Toggle the background-session section on the dashboard.\nSaves immediately. Background sessions keep running.\nCurrent: {current}',
    'notify': 'Cycle which state changes produce notifications.\nSaves immediately; running projects are unaffected.\nCurrent: {current}',
    'notify_sound': 'Toggle notification sounds.\nSaves immediately and plays a sample when enabled.\nCurrent: {current}',
    'sound_name': 'Choose the next notification sound and hear a sample.\nSaves immediately; notification event selection stays unchanged.\nCurrent: {current}',
    'auto_start': 'Toggle starting Claude when you open a shell project.\nSaves immediately; no agent starts just by changing this setting.\nCurrent: {current}',
    'dashboard': 'Return to the project dashboard.\nKeeps projects and running processes unchanged.\nSame as: ccm dashboard',
    'tree': 'Show the session, window and pane hierarchy.\nSwitching views leaves running processes unchanged.\nSame as: ccm tree-interactive',
    'quit': 'Close this menu and dashboard.\nProjects, windows and running agents remain.\nNo confirmation is needed.',
}


# Shared with dashboard key handling; only equivalent menu operations belong here.
MENU_KEYS = {'add': 'a', 'ignore': 'i', 'spool': 'u', 'save': 's', 'tree': 't', 'quit': 'q'}

def matches_key(action, key):
    hint = MENU_KEYS[action]
    return key in (ord(hint), ord(hint.upper()))


MENU_HELP_JA = {
    'add': 'プロジェクトのウィンドウを作成し、Claude を起動します。\nディレクトリと名前を入力します。存在しないディレクトリは作成前に確認します。\nCLI: ccm add <dir> [name]',
    'unregister': 'プロジェクトを ccm の管理対象から外します。\nウィンドウと実行中のプロセスは残ります。対象は入力画面で選びます。\nCLI: ccm unregister <name>',
    'delete': 'プロジェクトのウィンドウと実行中のプロセスを終了します。\n対象を選び、y で確認します。Enter または Esc でキャンセルします。\nCLI: ccm remove <name>',
    'ignore': 'プロジェクトを追跡対象から除外、または追跡対象に戻します。\nプロセスは動き続けます。除外時は確認し、復帰時は確認しません。\nCLI: ccm ignore <name> / ccm unignore <name>',
    'spool': '期限切れ・保留中のメッセージを確認します。\n閲覧だけでは送信しません。再送と削除には確認が必要です。\nCLI: ccm spool list / show / resend / discard',
    'save': '現在のプロジェクトとペイン配置を保存します。\n保存点の名前を入力します。保護された保存点は変更しません。\nCLI: ccm snapshot save <name>',
    'load': '保存点を選んで読み込み、または削除します。\n復元の進捗と結果を表示します。削除前に確認し、保護中の autosave は削除できません。\nCLI: ccm snapshot load <name> / delete <name>',
    'prepare_logout': 'ログアウト前に autosave を保存して保護します。\nウィンドウとエージェントは動き続けます。BUSY・PERMIT のプロジェクトがある場合は確認します。\nCLI: ccm prepare-logout',
    'cancel_logout': '保存点を残して、ログアウト用の保護を解除します。\n実行前に確認します。未完了の復元がある場合は先に完了させます。\nCLI: ccm prepare-logout --cancel',
    'continue_restore': '記録された保存点から未完了の復元を続けます。\n復元済みのペインは残ります。進捗と完了時の案内を表示します。\nCLI: ccm start <checkpoint>',
    'finish_restore': '未完了の復元を、ここまでに戻ったプロジェクトで終えます。\n保留のプロジェクトは保存点から外れ、元の保存点は別名で丸ごと残ります。確認があります。\nCLI: ccm finish-restore',
    'reset': '選択したプロジェクトの実行時シグナルとキャッシュを消去します。\n会話・プロセス・ウィンドウ・保存点は残ります。実行前に確認します。\nCLI: ccm reset <name>',
    'exit': '選択したプロジェクトの Claude を終了し、ウィンドウとサイドキックを残します。\n実行前に確認します。PERMIT では Escape で保留中のツール呼び出しを拒否します。安全に終了できない画面では実行しません。\nCLI: ccm exit <name> [-y]',
    'status_mode': 'ステータス表示を最小・ウィンドウ一覧・専用行から選びます。\n表示モードを入力して保存します。プロジェクトは動き続けます。\n現在値: {current}',
    'auto_restore': 'tmux 起動時に autosave を自動復元するか切り替えます。\n設定はすぐに保存され、次回起動時から適用されます。この操作では復元を開始しません。\n現在値: {current}',
    'idle_timeout': '操作のない Claude を自動終了するまでの時間を設定します。\n分単位で入力し、0 で無効にします。設定を保存し、ウィンドウは残します。\n現在値: {current}',
    'preview_toggle': 'プロジェクトのプレビューとメニューの説明表示を切り替えます。\nすぐに反映して保存します。プロジェクトは動き続けます。\n現在値: {current}',
    'preview_position': 'プレビューの位置を右側と下側で切り替えます。\nすぐに反映して保存します。画面が小さい場合は表示しません。\n現在値: {current}',
    'dock_mode': 'ダッシュボードをポップアップで出すか、窓の上か下に固定したペインで出すかを切り替えます。\n固定表示は作業中も表示され続け、窓を切り替えると付いてきます。設定を保存します。上・下は次に開くか別の窓へ移るときに反映し、popup にすると固定表示のダッシュボードを閉じます。\n現在値: {current}',
    'dock_close': '固定表示のダッシュボードから、プロジェクトを開いたら閉じるかを切り替えます。\noff のときは開いたまま一緒に移ります。設定を保存します。\n現在値: {current}',
    'bg_section': 'ダッシュボードのバックグラウンドセッション欄の表示を切り替えます。\n設定をすぐに保存します。バックグラウンドセッションは動き続けます。\n現在値: {current}',
    'notify': '通知する状態変化の組み合わせを順に切り替えます。\n設定をすぐに保存します。実行中のプロジェクトには影響しません。\n現在値: {current}',
    'notify_sound': '通知音の有効・無効を切り替えます。\n設定をすぐに保存し、有効にするとサンプル音を再生します。\n現在値: {current}',
    'sound_name': '次の通知音を選び、サンプル音を再生します。\n設定をすぐに保存します。通知する状態変化の設定は変わりません。\n現在値: {current}',
    'auto_start': 'シェル状態のプロジェクトを開いたときに Claude を起動するか切り替えます。\n設定をすぐに保存します。設定の変更だけではエージェントを起動しません。\n現在値: {current}',
    'dashboard': 'プロジェクトのダッシュボードに戻ります。\nプロジェクトと実行中のプロセスはそのまま残ります。\nCLI: ccm dashboard',
    'tree': 'セッション・ウィンドウ・ペインの階層を表示します。\n画面を切り替えても実行中のプロセスには影響しません。\nCLI: ccm tree-interactive',
    'quit': 'メニューとダッシュボードを閉じます。\nプロジェクト・ウィンドウ・実行中のエージェントは残ります。\n確認なしで閉じます。',
}


def description(action, label, language="en"):
    current = ccm_roles.clean(label.partition(': ')[2])
    table = MENU_HELP_JA if language == 'ja' else MENU_HELP
    return table[action].replace('{current}', current)


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
