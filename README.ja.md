# ccm - Claude Code Manager

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) 向けのtmuxベースのマルチプロジェクト管理ツール。複数のClaude Codeセッションをtmuxウィンドウとして管理し、インタラクティブなダッシュボード、状態検出、スナップショット機能を提供します。

## 機能

- **ダッシュボード** — Claude Codeの状態（BUSY/IDLE/PERMIT/DONE）をリアルタイム表示するインタラクティブポップアップ
- **ツリービュー** — セッション/ウィンドウ/ペインの階層表示とナビゲーション
- **Git連携** — プロジェクトごとのブランチ名とdirty状態（`main*`）の表示
- **ポート検出** — プロジェクトごとのリスニングTCPポートの自動検出（キャッシュ付き）
- **スナップショット** — プロジェクトレイアウトのJSON保存・復元
- **自動起動** — SHELL状態のウィンドウに切り替えるとClaude Codeを自動起動
- **ステータスライン** — アクティブプロジェクトの状態をtmuxステータスバーに表示

## 動作要件

- tmux 3.2+（popup対応）
- jq
- fzf
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

## インストール

### TPM を使う場合（推奨）

`~/.tmux.conf` に追加：

```tmux
set -g @plugin 'yohasebe/ccm'
```

tmuxをリロードし、`prefix + I` でインストール。

### 手動インストール

```bash
git clone https://github.com/yohasebe/ccm.git ~/.tmux/plugins/ccm
```

`~/.tmux.conf` に追加：

```tmux
source-file ~/.tmux/plugins/ccm/ccm.tmux.conf
```

### PATHに追加

CLI（`ccm add`、`ccm status` 等）を使うには、プラグインディレクトリをPATHに追加：

```bash
# .zshrc または .bashrc に追加
export PATH="$HOME/.tmux/plugins/ccm:$PATH"
```

### Zsh補完（オプション）

```bash
# .zshrc に追加（compinit の前）
fpath=($HOME/.tmux/plugins/ccm/completions $fpath)
```

## 使い方

### キーバインド

| キー | 動作 |
|------|------|
| `prefix + Tab` | ダッシュボードをトグル |
| `prefix + T` | ツリービューをトグル |
| `prefix + C` | ccmメニューを開く |

`~/.tmux.conf` でカスタマイズ可能（プラグイン読み込み前に設定）：

```tmux
set -g @ccm-key-dashboard "Tab"
set -g @ccm-key-menu "C"
set -g @ccm-key-tree "T"
```

### ステータスバー

tmuxステータスバーにプロジェクト状態を表示します。表示モードを設定：

```tmux
set -g @ccm-status-line 1     # デフォルト
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

### ダッシュボード操作

| キー | 動作 |
|------|------|
| `↑↓` / `jk` | プロジェクト間をナビゲート |
| `Enter` | 選択プロジェクトに切替 |
| `s` | 分割表示（横並びペインで開く） |
| `p` | プレビュー表示（`c` でクリップボードにコピー） |
| `a` | プロジェクト追加 |
| `g` | 既存ウィンドウを登録 |
| `r` | プロジェクト削除 |
| `/` | プロジェクト名で検索 |
| `q` / `Esc` | 閉じる |

### CLIコマンド

```
ccm add <dir> [name]              プロジェクト追加（ウィンドウ作成+Claude起動）
ccm open <dir> [name]             現在のペインでClaude起動（split-pane用）
ccm register <window> [name]      既存ウィンドウをccmプロジェクトとして登録
ccm remove <name>                 プロジェクトウィンドウ削除
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
```

### ステータスアイコン

| アイコン | 状態 | 説明 |
|----------|------|------|
| ⚠ | PERMIT | ユーザーの許可待ち |
| ◉ | BUSY | Claude処理中 |
| ✔ | DONE | レスポンス完了（自動検出） |
| ● | IDLE | 入力待ち |
| ■ | SHELL | シェルのみ（Claude未起動） |
| ○ | DOWN | ウィンドウ利用不可 |

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

## 仕組み

- プロジェクトはtmuxウィンドウの `@ccm_project` / `@ccm_dir` タグで管理
- Claude Codeの状態はプロセスツリー検査で検出（画面スクレイピングではない）
- DONE状態はBUSY/PERMIT → IDLE遷移で自動検出
- gitブランチとポート情報は30秒キャッシュで負荷軽減
- ポップアップ内のセッション検出は一時ファイル（`$TMPDIR/ccm-$UID/`）経由

## ドキュメント

- **[User Guide (English)](docs/guide.md)** — チュートリアル、ワークフロー、状態検出、ステータスバーモード、スナップショット、Tips、トラブルシューティング
- **[ユーザーガイド（日本語）](docs/guide.ja.md)** — 同内容の日本語版

## ライセンス

MIT
