# ccm - Claude Code Manager for tmux

**[English README](README.md)**

複数の [Claude Code](https://docs.anthropic.com/en/docs/claude-code) セッションを並行して実行。プロジェクトを瞬時に切り替え、どれが注意を必要としているか一目で把握し、ワークスペースを失うことなく作業を続けられます。

ccmはClaude Codeセッションをtmuxウィンドウとして管理するtmuxプラグインです。ライブダッシュボード、状態検出、スナップショット復元機能を備えています。

**ダッシュボード** (`prefix + Tab`):

> ```
> -- ccm Dashboard --
>
> > #1 ◉ BUSY    my-app (main*) [:3000] ~/code/my-app
>   #2 ⚠ PERMIT  api-server (dev) ~/code/api-server
>   #3 ✔ DONE    web-client (main) ~/code/web-client
>   #4 ● IDLE    docs (main) ~/code/docs
>
> [↑↓/jk] select [Enter] attach [p]review [a]dd [s]ave [q] quit
> Last saved: 10:30:45
> ```

**ステータスバー**（モード0）:

> ```
> 0:◉ my-app  1:⚠ api*  2:✔ web  3:● docs      10:30  ◉ BUSY
> ```

## 機能

- **リソース管理** — アイドル状態のClaude Codeセッションを5分後に自動終了し、メモリとCPUを解放。ウィンドウに戻ると `--continue` で自動再起動（各Node.jsインスタンスは大量のリソースを消費します）
- **ダッシュボード** — Claude Codeの状態（BUSY/IDLE/PERMIT/DONE）をリアルタイム表示するインタラクティブポップアップ
- **ツリービュー** — セッション/ウィンドウ/ペインの階層表示とナビゲーション
- **Git連携** — プロジェクトごとのブランチ名とdirty状態（`main*`）の表示
- **ポート検出** — プロジェクトごとのリスニングTCPポートの自動検出（キャッシュ付き）
- **スナップショット** — プロジェクトレイアウトのJSON保存・復元
- **自動起動** — SHELL状態のウィンドウに切り替えるとClaude Codeを自動起動
- **ステータスライン** — アクティブプロジェクトの状態をtmuxステータスバーに表示
- **Agent Teams対応** — Claude Codeの[Agent Teams](https://code.claude.com/docs/en/agent-teams)と併用可能：ccmでプロジェクトを管理しつつ、各プロジェクト内で並行エージェントを実行

## 動作要件

- tmux 3.2+（popup対応）
- Python 3.9+（macOSおよび主要なLinuxディストリビューションに標準搭載）
- [TPM](https://github.com/tmux-plugins/tpm)（プラグインインストール用。手動インストールも可）
- jq
- fzf
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

## インストール

### TPM を使う場合（推奨）

`~/.tmux.conf` に追加：

```tmux
set -g @plugin 'yohasebe/tmux-claude-code-manager'
```

tmuxをリロードし、`prefix + I` でインストール。

### 手動インストール

```bash
git clone https://github.com/yohasebe/tmux-claude-code-manager.git ~/.tmux/plugins/tmux-claude-code-manager
```

`~/.tmux.conf` に追加：

```tmux
source-file ~/.tmux/plugins/tmux-claude-code-manager/ccm.tmux.conf
```

### PATHに追加

CLI（`ccm add`、`ccm status` 等）を使うには、プラグインディレクトリをPATHに追加：

```bash
# .zshrc または .bashrc に追加
export PATH="$HOME/.tmux/plugins/tmux-claude-code-manager:$PATH"
```

### Zsh補完（オプション）

```bash
# .zshrc に追加（compinit の前）
fpath=($HOME/.tmux/plugins/tmux-claude-code-manager/completions $fpath)
```

## 初回セットアップ

Claude Codeを初めて使う場合は、先に認証を済ませてください：

```bash
claude
```

対話的なセットアップが始まり、プラン選択（サブスクリプションまたはAPIキー）とブラウザ認証を行います。認証が完了したら、セットアップウィザードを実行してください：

```bash
ccm init
```

フックのインストール、自動復元、ステータスバーの設定をまとめて案内します。

## 使い方

### キーバインド

| キー | 動作 | デフォルト |
|------|------|-----------|
| `prefix + Tab` | ダッシュボードをトグル | 有効 |
| `prefix + T` | ツリービューをトグル | 無効（オプトイン） |
| `prefix + C` | ccmメニューを開く | 無効（オプトイン） |

他のプラグインとの競合を避けるため、ダッシュボードのみデフォルトで有効です。ツリービューとメニューはダッシュボード内で `t` または `m` を押してもアクセスできます。

専用キーを追加で設定するには `~/.tmux.conf` に追加してください：

```tmux
set -g @ccm-key-menu "C"        # 任意: prefix + C でメニュー
set -g @ccm-key-tree "T"        # 任意: prefix + T でツリービュー
```

> **重要:** すべての `set -g @ccm-*` オプションは、ccmプラグインが読み込まれるよりも**前に** `~/.tmux.conf` で設定する必要があります。`source-file` 行（手動インストール）およびTPMの `run` 行（TPMインストール）よりも前に配置してください。プラグインは読み込み時にこれらのオプションを参照するため、後に配置された設定は反映されません。

### デスクトップ通知

プロジェクトの状態変化時にデスクトップ通知（macOS / Linux対応）を送信できます：

```tmux
set -g @ccm-notify "permit,done"     # PERMITとDONEで通知
```

| 値 | 動作 |
|----|------|
| `off`（デフォルト） | 通知なし |
| `permit` | 許可が必要な時に通知 |
| `done` | レスポンス完了時に通知 |
| `permit,done` | 両方 |
| `all` | 全ての状態変化 |

通知音を無効化：

```tmux
set -g @ccm-notify-sound "off"    # デフォルト: on
```

### ステータスバー

tmuxステータスバーにプロジェクト状態を表示します。表示モードを設定：

```tmux
set -g @ccm-status-line 0     # デフォルト
```

| 値 | モード | 説明 |
|----|--------|------|
| `0` | アイコン（デフォルト） | status-rightにアイコン1つを追記 |
| `1` | 全表示 | ウィンドウリストをccm形式の色付きエントリに置換 |
| `2` | 専用行 | ブランチ・ポート情報付きで全プロジェクトを専用行に表示 |

#### モード0 — アイコン表示（デフォルト）

既存のstatus-rightにアイコン1つを追加。時計やバッテリー表示はそのまま保持。全プロジェクトの中で最も優先度の高い状態を表示：

| 優先度 | 条件 | アイコン | 色 |
|--------|------|---------|-----|
| 1（最高） | PERMITのプロジェクトあり | `⚠` | 黄 |
| 2 | BUSYのプロジェクトあり | `◉` | シアン |
| 3 | DONEのプロジェクトあり | `✔` | 緑 |
| 4（最低） | 全てIDLE | `≡` | グレー |

アイコンをクリックするとダッシュボードが開く。

#### モード1 — 全表示（ccm形式ウィンドウリスト）

tmux標準のウィンドウリストをccm形式の色付きエントリに置換。既存のstatus-right（時計等）は保持。

```
openai-workflow:● │ ccm:◉ │ monadic-chat:● │ 21:30 2026-03-21
```

#### モード2 — 専用ステータス行

メインバーの下に専用ステータス行を追加。IDLEを含む全プロジェクトをgitブランチ・ポート情報付きで表示。メインバーは変更しない。

```
my-project:◉(main*) │ api:●(dev)[:8080] │ ccm:✔(main*)
```

| 状態 | アイコン | 色 |
|------|---------|-----|
| PERMIT | `⚠` | 黄 |
| BUSY | `◉` | シアン |
| DONE | `✔` | 緑 |
| IDLE | `●` | グレー |
| SHELL | `■` | 暗グレー |

端末幅とプロジェクト数に応じて行数が自動拡張。

> **注意:** モード2は `status-format[1]` 〜 `status-format[5]` を使用します。他のプラグインがこれらのインデックスを使用している場合、衝突が発生する可能性があります。

### ダッシュボード操作

| キー | 動作 |
|------|------|
| `↑↓` / `jk` | プロジェクト間をナビゲート |
| `Enter` | 選択プロジェクトに切替 |
| `s` | スナップショット保存 |
| `p` | プレビュー表示（`c` でクリップボードにコピー） |
| `a` | プロジェクト追加 |
| `g` | 既存ウィンドウを登録 |
| `r` | 削除 — [u]nregister（ウィンドウ残す）か [d]elete を選択 |
| `/` | プロジェクト名で検索 |
| `q` / `Esc` | 閉じる |

### CLIコマンド

```
ccm add <dir> [name]              プロジェクト追加（ウィンドウ作成+Claude起動）
ccm open <dir> [name]             現在のペインでClaude起動（split-pane用）
ccm register <window> [name]      既存ウィンドウをccmプロジェクトとして登録
ccm unregister <name>             ccm管理から外す（ウィンドウは残る）
ccm remove <name>                 プロジェクトウィンドウ削除（ウィンドウを閉じる）
ccm attach <name|number>          プロジェクトウィンドウに切替
ccm list                          管理中プロジェクト一覧
ccm status                        全プロジェクト状態表示（ブランチ・ポート含む）
ccm tree                          セッション/ウィンドウ/ペインの階層表示
ccm ports                         プロジェクトごとのリスニングポート表示
ccm capture [--copy] <name|#id>   ペイン内容をキャプチャ（--copy: クリップボード）
ccm dashboard                     インタラクティブダッシュボードを開く
ccm menu                          インタラクティブメニュー
ccm snapshot save|load|list|delete  スナップショット管理
ccm start <snapshot>              スナップショットから復元
ccm stop [--all|name]             プロジェクト停止（--all時は_autosave自動保存）
ccm setup-hooks                   Claude Codeフックをインストール（検出精度向上）
ccm remove-hooks                  Claude Codeフックをアンインストール
```

### ステータスアイコン

| アイコン | 状態 | 説明 |
|----------|------|------|
| ⚠ | PERMIT | ユーザーの許可待ち |
| ◉ | BUSY | Claude処理中 |
| ✔ | DONE | レスポンス完了（30秒後に自動クリア） |
| ● | IDLE | 入力待ち |
| ■ | SHELL | シェルのみ（Claude未起動） |
| ○ | DOWN | ウィンドウ利用不可 |

### Claude Codeフック（推奨）

より正確な状態検出のために、Claude Codeフックをインストールします：

```bash
ccm setup-hooks
```

`~/.claude/settings.json` にフックが追加され、状態変化を通知します：
- **UserPromptSubmit** → プロンプト送信時にBUSYをマーク（テキスト生成を検出）
- **Stop** → Claude応答完了時にDONEをマーク

フックなしの場合、ccmはプロセスツリー検査を使用しますが、テキスト生成中をBUSYとして検出できません（IDLEと表示されます）。フックはオプションです — インストールしなくても動作しますが、検出精度が低下します。

フックの状態はダッシュボードのフッターと `ccm status` の出力に表示されます（Hooks: ON/OFF）。既にインストール済みの場合、`ccm setup-hooks` は再インストールをスキップします。ccmを別のパスに再インストールした場合は、フックのパスが自動的に更新されます。

削除するには: `ccm remove-hooks`

### スナップショット

ワークスペースのレイアウトを保存して後から復元：

```bash
ccm snapshot save my-workspace
ccm snapshot list
ccm start my-workspace
```

`ccm stop --all` 実行時に `_autosave` として自動保存：

```bash
ccm start _autosave   # 前回のセッションを復元
```

#### tmux起動時の自動復元

tmux起動時に最後の `_autosave` スナップショットを自動復元：

```tmux
set -g @ccm-auto-restore "on"    # デフォルト: off
```

有効にすると、tmux起動時にTPM経由で `_autosave` スナップショットを自動ロードします（既にccmプロジェクトがある場合はスキップ）。

### アイドル自動終了

一定時間IDLEのままのClaude Codeセッションは自動的に終了し、システムリソースを解放します。終了したウィンドウに切り替えると、Claude Codeが `--continue` で自動再起動し、会話を再開します。

```tmux
set -g @ccm-idle-timeout "5"     # 分（デフォルト: 5、0で無効化）
```

## 仕組み

- プロジェクトはtmuxウィンドウの `@ccm_project` / `@ccm_dir` タグで管理
- Claude Codeの状態はプロセスツリー検査で検出（画面スクレイピングではない）
- DONE状態はBUSY/PERMIT → IDLE遷移で自動検出（30秒後に自動クリア）
- tmuxテーマとの併用に対応（status-rightの変更を自動検出）
- gitブランチとポート情報は30秒キャッシュで負荷軽減
- ポップアップ内のセッション検出は一時ファイル（`$TMPDIR/ccm-$UID/`）経由

## アンインストール

1. `~/.tmux.conf` から削除：
   ```tmux
   # この行を削除:
   set -g @plugin 'yohasebe/tmux-claude-code-manager'
   # または source-file の場合:
   # source-file ~/.tmux/plugins/tmux-claude-code-manager/ccm.tmux.conf
   ```

2. tmux状態をクリーンアップ：
   ```bash
   # ccmオプションを削除
   tmux set -g -u @ccm-orig-status-right 2>/dev/null
   tmux set -g -u @ccm-orig-sr-length 2>/dev/null
   tmux set -g -u @ccm-status-line 2>/dev/null
   tmux set -g -u window-status-format 2>/dev/null
   tmux set -g -u window-status-current-format 2>/dev/null

   # 一時ファイルを削除
   rm -rf "${TMPDIR:-/tmp}/ccm-$(id -u)"

   # ランタイムデータを削除（任意 — スナップショットも消えます）
   rm -rf ~/.local/share/ccm
   ```

3. tmuxをリロード: `tmux source-file ~/.tmux.conf`

## ドキュメント

- **[User Guide (English)](docs/guide.md)** — チュートリアル、ワークフロー、状態検出、ステータスバーモード、スナップショット、Tips、FAQ、トラブルシューティング
- **[ユーザーガイド（日本語）](docs/guide.ja.md)** — 同内容の日本語版

## ライセンス

MIT
