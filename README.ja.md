<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.png">
  <img alt="ccm — Claude Code Manager for tmux" src="assets/logo-light.png" width="380" align="left">
</picture>
<br clear="left">
<br>

[![tested](https://img.shields.io/badge/tested-Claude%20Code%20v2.1.220-brightgreen)](https://docs.anthropic.com/en/docs/claude-code)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![tmux](https://img.shields.io/badge/tmux-3.2%2B-blue)](https://github.com/tmux/tmux)

ccm は、[Claude Code](https://docs.anthropic.com/en/docs/claude-code) を複数プロジェクトで並行して使う開発者のための tmux プラグインです。各 Claude セッションをタグ付きの tmux ウィンドウとして扱い、状態をライブで検出するので、「どれが応答中で、どれが許可待ちで、どれは終わってるんだっけ」と tmux ウィンドウを行き来する必要がなくなります。

実質的に **アテンション・マネージャ** として動きます。各プロジェクトに状態 (BUSY / IDLE / 許可待ち) を付与し、ダッシュボードはそれを優先度順に並べ、キー 1 つで「次に対応すべきウィンドウ」に移動できます。

ポップアップダッシュボードで全プロジェクトの状態 (busy / idle / 許可待ち)、git ブランチ、リスニングポートを一目で確認できます。スナップショットで tmux 再起動後のレイアウトを復元し、デスクトップ通知で対応が必要なプロジェクトだけを知らせます。

ccm の価値は並行度に応じて伸びます。2–3 プロジェクトでも有用、4 つ以上を並行で動かすと日常のインフラになります。Max プランで複数プロジェクトに agent を並行で走らせている層に最もフィットします。

## ccm のメンタルモデル

1 つの **プロジェクト** = 1 つの **フォルダ** = 1 つの **tmux ウィンドウ**。ウィンドウは任意の数のペインを持ちます (claude code、シェル、開発サーバー、Agent Teams のチームメイト等)。各 claude code ペインの状態 (PERMIT / BUSY / IDLE) はウィンドウへ集約され、ccm ダッシュボード (`prefix + Tab`) でウィンドウ間を切り替えられます。

![ccm メンタルモデル: tmux session が複数のウィンドウを持ち (各ウィンドウ = 1 プロジェクト = 1 フォルダ)、各ウィンドウに claude code とその他のペインが入り、claude code の状態はウィンドウに集約され、ccm が prefix + Tab でウィンドウ間を切り替える](assets/mental-model.svg)

**ダッシュボード** (`prefix + Tab`) — 画面下部のモード 2 ステータスバーも同時に映っています:

![ccm dashboard](assets/dashboard.png)

## 機能

- **リソース管理** — アイドル状態の Claude Code セッションを 10 分後に自動終了してメモリと CPU を解放。ウィンドウに戻ると会話を引き継いで自動再起動
- **ダッシュボード** — Claude Code の状態 (BUSY / IDLE / PERMIT) をリアルタイム表示するインタラクティブポップアップ
- **ツリービュー** — セッション / ウィンドウ / ペインの階層表示とナビゲーション
- **Git 連携** — プロジェクトごとに git ブランチと dirty 状態を表示
- **ポート検出** — プロジェクトごとのリスニングポートを表示
- **スナップショット** — プロジェクトレイアウトの保存と復元
- **プロジェクト間メッセージング** — `ccm send <project> <message>` で他プロジェクトの Claude にプロンプト送信、状態に応じた安全な制御 (PERMIT は拒否、BUSY は `--force` でキュー投入)
- **Permission mode の可視化** — 各プロジェクトの Claude Code permission mode (manual / accept / plan / auto / bypass) を `ccm status` とダッシュボードに表示。「このプロジェクトは許可を求めてこない」理由が一目で分かる
- **サイドキック対応** — 分割ペインで2つ目のエージェント CLI を動かせます。`CCM_IGNORE`（`⊘`）でセッションを ccm の追跡から外し、presence バッジ（`⚙`）で Claude 以外のエージェントの存在を示します。[別モデルをサイドキックとして使う](docs/guide.ja.md#別モデルをサイドキックとして使うccm_ignore)を参照
- **自動起動** — Claude Code が起動していないプロジェクトに attach すると自動起動
- **ステータスライン** — アクティブプロジェクトの状態を tmux ステータスバーに表示
- **多バイト文字対応** — CJK 文字や絵文字を含むプロジェクト名がダッシュボード・ステータスバー・CLI テーブルで正しく整列
- **Agent Teams 対応** — Claude Code の [Agent Teams](https://code.claude.com/docs/en/agent-teams) と併用可能: ccm でプロジェクトを管理しつつ、各プロジェクト内で並行エージェントを実行

## 動作要件

- tmux 3.2+
- Python 3.9+
- [TPM](https://github.com/tmux-plugins/tpm)（手動インストールも可）
- jq
- fzf
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) **v2.1.107 以上**

## インストール

### TPM を使う場合（推奨）

`~/.tmux.conf` に追加：

```tmux
set -g @plugin 'yohasebe/tmux-ccm'
```

tmuxをリロードし、`prefix + I` でインストール。

> ccm は標準的なプラグインエントリポイントを使用するため、TPM 互換の
> プラグインマネージャであれば動作します。[TPM](https://github.com/tmux-plugins/tpm)
> でも、その drop-in replacement である
> [tpack](https://github.com/tmuxpack/tpack) でも、ccm 固有の追加設定は
> 不要です。

### 手動インストール

```bash
git clone https://github.com/yohasebe/tmux-ccm.git ~/.tmux/plugins/tmux-ccm
```

`~/.tmux.conf` に追加：

```tmux
source-file ~/.tmux/plugins/tmux-ccm/ccm.tmux.conf
```

### PATHに追加

CLI（`ccm add`、`ccm status` 等）を使うには、プラグインディレクトリをPATHに追加：

```bash
# .zshrc または .bashrc に追加
export PATH="$HOME/.tmux/plugins/tmux-ccm:$PATH"
```

### Zsh補完（オプション）

```bash
# .zshrc に追加（compinit の前）
fpath=($HOME/.tmux/plugins/tmux-ccm/completions $fpath)
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

> [!NOTE]
> フックはプラグイン読み込み時（TPM経由）に自動インストールされ、プラグイン更新時にも自動で最新化されます。手動で管理したい場合は `ccm setup-hooks` / `ccm remove-hooks` を使用してください。

## 使い方

### キーバインド

| キー | 動作 | デフォルト |
|------|------|-----------|
| `prefix + Tab` | ダッシュボードをトグル | 有効 |
| `prefix + T` | ツリービューをトグル | 無効（オプトイン） |
| `prefix + C` | ccmメニューを開く | 無効（オプトイン） |

他のプラグインとの競合を避けるため、ダッシュボードのみデフォルトで有効です。ツリービューとメニューはダッシュボード内で `t` または `m` を押してもアクセスできます。

ダッシュボードのキーを変更したり、任意キーを有効化するには `~/.tmux.conf` に追加してください：

> [!IMPORTANT]
> すべての `set -g @ccm-*` オプションは、ccm プラグインが読み込まれるよりも**前に** `~/.tmux.conf` で設定する必要があります。`source-file` 行(手動インストール)および TPM の `run` 行(TPM インストール)よりも**前**に配置してください。プラグインは読み込み時にこれらのオプションを参照するため、後に配置された設定は黙って無視されます。

```tmux
set -g @ccm-key-dashboard "Tab"  # デフォルト。リマップする場合に上書き(例: "F12")
set -g @ccm-key-menu "C"         # 任意: prefix + C でメニュー
set -g @ccm-key-tree "T"         # 任意: prefix + T でツリービュー
set -g @ccm-key-search "/"       # 任意: prefix + / でダッシュボードをライブフィルタ検索で開く
                                  # (日本語対応、入力でリアルタイム絞り込み、
                                  #  Enter でアタッチ、Esc でキャンセル)
```

> [!TIP]
> prefix なしの単一キーでダッシュボードをトグルする場合は `@ccm-key-dashboard-noprefix` を使います。例えば `F1` に割り当てる場合:
>
> ```tmux
> # 必ず ccm プラグインの読み込み行よりも前に配置 (上記 IMPORTANT 参照)。
> set -g @ccm-key-dashboard-noprefix "F1"
> ```
>
> `F1` で開き、もう一度 `F1` で閉じます。popup タイトルの色付き ccm ロゴもそのまま表示されます (自前で `bind-key -n F1 display-popup …` を書くこともできますが、`-T` の format 文字列を完全にコピーしないとロゴは出ません)。

### デスクトップ通知

プロジェクトの状態変化時にデスクトップ通知（macOS / Linux対応）を送信できます：

```tmux
set -g @ccm-notify "permit,completed"     # PERMITと完了時に通知
```

| 値 | 動作 |
|----|------|
| `permit,completed`（デフォルト） | 許可プロンプトと完了時に通知 |
| `permit` | 許可が必要な時に通知 |
| `completed` | 完了時に通知（Claudeのレスポンス完了） |
| `all` | 全ての状態変化 |
| `off` | 通知なし |

通知音を有効化：

```tmux
set -g @ccm-notify-sound "on"     # デフォルト: off（macOSでは「Glass」サウンドを再生）
set -g @ccm-notify-sound-name "Ping"  # 任意: サウンドをカスタマイズ（macOSのみ）
```

#### macOS — `terminal-notifier` の導入を推奨

デフォルトでは ccm は macOS 通知に `osascript` を使用します。各通知は通知センターに追加され、ユーザーが消すまで残ります。マルチプロジェクトで長時間運用すると数百件の古い通知が蓄積し、WindowServer / NotificationCenter が高 CPU になることがあります。

`terminal-notifier` を導入するとこの問題は解消します — ccm がプロジェクトごとの group ID (`-group ccm-<project>`) で送信するため、同じプロジェクトの新しい通知は前の通知を **置き換え** ます。

```bash
brew install terminal-notifier
```

ccm が `PATH` 上の有無を自動検出して優先使用します。設定不要。

> [!IMPORTANT]
> `terminal-notifier` が初めて通知を送る際、macOS は通知許可ダイアログを表示することがあります — 許可してください。導入後に ccm 通知が届かない場合は「システム設定 → 通知 → terminal-notifier」を開き「通知を許可」が ON になっているか確認してください。通知は terminal-notifier 自身の bundle id 名義で配送されるため（使用しているターミナルエミュレータには非依存）、許可は一度で済み、Terminal.app / iTerm2 / WezTerm / kitty / ghostty いずれでも同じ動作になります。

通知センターに残った ccm 通知を一括削除（`terminal-notifier` が必要）:

```bash
ccm clear-notifications
```

削除対象は group id が `ccm-` で始まる通知のみで、ユーザーが他のスクリプト（デプロイ通知 / モニタリング等）で送った terminal-notifier 通知は影響を受けません。実際に削除した件数を表示します。

`terminal-notifier` を導入できず蓄積問題に当たった場合は、`@ccm-notify "permit"` に設定して頻度の高い COMPLETED 通知をスキップし、要対応の PERMIT 通知のみ受け取るのが良いでしょう。

#### Linux — `notify-send`

Linux では `notify-send` を使用します。macOS の `-group` のようなプロジェクト単位の重複防止機構は無いため、長時間運用時は `@ccm-notify "permit"` に設定して通知量を抑えてください。

`ccm clear-notifications` は macOS 専用です。Linux には既存通知を一覧/削除するコマンドラインAPIがないため、コマンドは「terminal-notifier is not installed」と表示して終了します。

### ステータスバー

tmuxステータスバーにプロジェクト状態を表示します。表示モードを設定：

```tmux
set -g @ccm-status-line 2     # デフォルト
```

| 値 | モード | 説明 |
|----|--------|------|
| `0` | アイコン | status-right にウィンドウ番号付きアイコンを追記（最も控えめ — 既存テーマを変えない） |
| `1` | 全表示 | ウィンドウリストを ccm 形式の色付きエントリに置換 |
| `2` | 専用行（デフォルト） | メインバー下に専用行を追加し、全プロジェクトをブランチ・ポート情報付きで表示 |

**モード 2 がデフォルト**。既存の tmux テーマをそのまま残したい場合はモード 0 が最も保守的な選択です。

#### モード0 — アイコン＋ウィンドウ番号

既存のstatus-rightにウィンドウ番号付きアイコンを追加。時計やバッテリー表示はそのまま保持。アクティブなプロジェクトがある場合はウィンドウ番号も表示（例: `5: PERMIT ⚠`）。全てIDLEの場合は `≡` アイコンのみ：

```
 ... 既存のstatus-right ...   5: PERMIT ⚠   14:32
                              └────────────┘
                          ウィンドウ番号 + 最高優先度の状態
```

| 優先度 | 条件 | アイコン | 色 |
|--------|------|---------|-----|
| 1（最高） | PERMITのプロジェクトあり | `⚠` | 黄 |
| 2 | BUSYのプロジェクトあり | `◉` | オレンジ |
| 3（最低） | 全てIDLE | `≡` | グレー |

アイコンをクリックするとダッシュボードが開く。

#### モード1 — 全表示（ccm形式ウィンドウリスト）

tmux標準のウィンドウリストをccm形式の色付きエントリに置換。既存のstatus-right（時計等）は保持。

```
 2:weather-cli ●   3:recipe-blog ◉   4:todo-react ◉   5:api-server ⚠
```

#### モード2 — 専用ステータス行

メインバーの下に専用ステータス行を追加。IDLEを含む全プロジェクトをgitブランチ・ポート情報付きで表示。メインバーは変更しない（上のダッシュボード画像下部にも mode 2 が映っています）。

```
 ... 通常のステータス行はそのまま ...                            14:32
 ────────────────────────────────────────────────────────────────── (ガター)
 5:api-server (main) ⚠ PERMIT  ·  3:recipe-blog (main) ◉ BUSY
   ·  4:todo-react ◉ BUSY  ·  2:weather-cli ● IDLE * 20s
```

| 状態 | アイコン | 色 |
|------|---------|-----|
| PERMIT | `⚠` | 黄 |
| BUSY | `◉` | オレンジ |
| IDLE | `●` | ブルー |
| IDLE（最近完了） | プロジェクト名後に `* <elapsed>` | 緑 |
| SHELL | `■` | 暗グレー |

端末幅とプロジェクト数に応じて行数が自動拡張。

ダークグレーのガターが既存テーマと合わない場合は、tmux オプションで配色を上書きできます:

```tmux
set -g @ccm-status-bg         "#262626"   # エントリ行の背景色
set -g @ccm-status-gutter-bg  "#1a1a1a"   # メインバーとエントリ行の間のガター（1行分）
set -g @ccm-status-fg         "#9E9E9E"   # デフォルト前景色
set -g @ccm-status-fg-dim     "#5a5a5a"   # 区切り記号やヒント表示
```

受理される値: 16 進カラー (`#RGB` / `#RRGGBB`)、`colour123` 形式のパレットインデックス、または名前付きカラー (`red` / `blue` / `default` 等)。無効な値はデフォルトにフォールバックします。

> [!NOTE]
> モード2は追加の `status-format` スロット（1行のガター + レイアウト1行ごとに1スロット、最大 16）を使用します。他のプラグインがこれらのインデックスを使用している場合、衝突が発生する可能性があります。

### ダッシュボード操作

| キー | 動作 |
|------|------|
| `↑↓` / `jk` | プロジェクト間をナビゲート |
| `Enter` | 選択プロジェクトに切替 |
| `s` | スナップショット保存 |
| `p` | プレビュー表示（`c` でクリップボードにコピー） |
| `a` | プロジェクト追加 |
| `n` | 選択プロジェクトの名前変更 |
| `g` | 既存ウィンドウを登録 |
| `r` | 削除 — [u]nregister（ウィンドウ残す）か [d]elete を選択 |
| `i` | 無視トグル — プロジェクトを隠す/戻す（[別モデルをサイドキックとして使う](docs/guide.ja.md#別モデルをサイドキックとして使うccm_ignore)参照） |
| `x` | アイドル状態のClaude Codeを一括終了 |
| `/` | プロジェクト名で検索 |
| `t` | ツリービューに切替 |
| `b` | [バックグラウンドセッションセクション](#バックグラウンドセッションセクションagent-view共存) を切替 |
| `m` | メニューに切替 |
| `q` / `Esc` / `F1` | 閉じる |

### CLIコマンド

```
ccm add <dir> [name]              プロジェクト追加（ウィンドウ作成+Claude起動）。<dir> が
                                  存在しない場合は作成するか確認（親ディレクトリは存在必須）
ccm open <dir> [name]             現在のペインでClaude起動（split-pane用）
ccm register <window> [name]      既存ウィンドウをccmプロジェクトとして登録
ccm unregister <name>             ccm管理から外す（ウィンドウは残る）
ccm rename <name> <new_name>      プロジェクト名を変更
ccm remove <name>                 プロジェクトウィンドウ削除（ウィンドウを閉じる）
ccm attach <name|number>          プロジェクトウィンドウに切替
ccm list                          管理中プロジェクト一覧
ccm status                        全プロジェクト状態表示（permission mode・ブランチ・ポート含む）
ccm tree                          セッション/ウィンドウ/ペインの階層表示
ccm ports                         プロジェクトごとのリスニングポート表示
ccm capture [--copy] <name|#id>   ペイン内容をキャプチャ（--copy: クリップボード）
ccm dashboard [--search]          インタラクティブダッシュボードを開く
ccm search                        ダッシュボードをライブフィルタ検索モードで開く
ccm menu                          インタラクティブメニュー
ccm snapshot save|load|list|delete  スナップショット管理
ccm start <snapshot>              スナップショットから復元
ccm stop [--all|name]             プロジェクト停止（--all時は_autosave自動保存）
ccm send <name> <msg> [flags]     他プロジェクトのClaude Codeセッションにプロンプト送信
                                  flags: --file --stdin --no-enter --force --start -y --
ccm bg list                       外部の Claude Code agent-view セッション一覧（読み取り専用）
ccm init                          対話型セットアップウィザード（フック・復元・ステータスバー）
ccm setup-hooks                   Claude Codeフックをインストール（検出精度向上）
ccm remove-hooks                  Claude Codeフックをアンインストール
ccm setup-claude-md               ~/.claude/CLAUDE.mdにccmセクションを追加
ccm remove-claude-md              ~/.claude/CLAUDE.mdからccmセクションを削除
ccm statusline                    1行ステータス出力（tmuxステータスバー用）
ccm inject-status                 tmuxステータスバー更新（内部使用）
ccm debug trace <name> [interval] 状態検出のライブトレース（読み取り専用、Ctrl-Cで終了）
ccm errors [--clear]              silent-exception ログの表示／クリア
ccm reset <name>                  プロジェクトの runtime 状態をリセット (最終手段の復旧)
ccm ignore [project]              ペイン(既定:現在)またはプロジェクトをccmから隠す
ccm unignore [project]            隠したペイン/プロジェクトをccmに戻す
ccm doctor                        セルフチェック (依存・フック・カナリア・プロジェクト・エラー)
ccm clear-notifications           macOS 通知センターから ccm 通知を削除
```

> [!TIP]
> 多くのコマンドに短縮エイリアスがあります: `ls` (list), `st` (status), `a` (attach), `rm` (remove), `d` (dashboard), `reg` (register), `unreg` (unregister), `mv` (rename), `cap` (capture), `snap` (snapshot), `sl` (statusline)

### ステータスアイコン

| アイコン | 状態 | 説明 |
|----------|------|------|
| ⚠ | PERMIT | ユーザーの許可待ち |
| ◉ | BUSY | Claude処理中 |
| ● | IDLE | 入力待ち |
| `* <elapsed>` | IDLE（最近完了） | BUSY/PERMIT → IDLE 遷移後 30 秒間表示される一時マーカー（アスタリスク緑、時間 dim） |
| `[N]` | （任意の状態、マルチペインウィンドウ） | ペイン数マーカー（角括弧 dim、数字 cyan）。ウィンドウに 2 ペイン以上ある場合に表示 |
| `⊘` | （全状態） | 非表示ペインのマーカー（dim）: `CCM_IGNORE` で ccm から除外されたペインがウィンドウ内にある |
| `⚙<name>` | （全状態） | presence マーカー（dim）: ウィンドウ内のペインで外部エージェント CLI が動作中。存在は示すが状態は追跡しない |
| ■ | SHELL | シェルのみ（Claude未起動） |
| ○ | DOWN | ウィンドウ利用不可 |

### Claude Codeフック（推奨）

より正確な状態検出のために、Claude Codeフックをインストールします：

```bash
ccm setup-hooks
```

`~/.claude/settings.json` にフックが追加され、状態変化を通知します：
- **UserPromptSubmit** → プロンプト送信時にBUSY（テキスト生成を検出）
- **PreToolUse / PostToolUse / PostToolUseFailure** → ツール実行中にBUSY
- **SubagentStart / SubagentStop** → サブエージェント実行中にBUSY（親エージェントは作業継続中）
- **PreCompact / PostCompact** → コンテキスト圧縮中にBUSY
- **Stop / StopFailure** → Claude応答完了時にBUSY信号をクリア
- **PermissionRequest** → ツールの許可が必要な時にPERMIT
- **Notification** → 許可プロンプト/MCP elicitation 表示時にPERMIT（permission_prompt / elicitation_dialog）、アイドル通知時に信号クリア（idle_prompt）
- **SessionEnd** → セッション終了時にSHELL（/exit、Ctrl+D等）
- **PermissionDenied** → autoモードで拒否時にPERMIT（`/permissions`で再試行）

ccm はフック非依存のフォールバックを備えており、Claude Code がセッション途中でフック発火を停止しても検出が継続します:

- **JSONL セッションログ心拍**: プロジェクトの最新 `~/.claude/projects/.../jsonl` に user/assistant レコードがあればセッション稼働中の証拠（housekeeping レコードは除外）
- **許可ダイアログのフッター検出**: 許可フッター（`Esc to cancel · Tab to amend`）をペインから直接検出
- **`~/.claude/hooks.log` 肥大化カナリア**: このファイルが 100MB を超えると silent fail の原因になるため警告表示。修復: `: > ~/.claude/hooks.log`

フックの状態はダッシュボードのフッターと `ccm status` の出力に表示されます（Hooks: ON/OFF）。既にインストール済みの場合、`ccm setup-hooks` は再インストールをスキップします。ccmを別のパスに再インストールした場合は、フックのパスが自動的に更新されます。

削除するには: `ccm remove-hooks`

### スナップショット

ワークスペースのレイアウトを保存して後から復元：

```bash
ccm snapshot save my-workspace
ccm snapshot list
ccm start my-workspace
```

`_autosave` スナップショットはプロジェクトがアクティブな間、2分間隔で自動更新されます。`ccm stop --all` 実行時にも保存：

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
set -g @ccm-idle-timeout "10"    # 分（デフォルト: 10、0で無効化）
```

### ダッシュボードプレビューパネル

選択中のプロジェクトのターミナル内容をプロジェクトリストの横にライブ表示します：

```tmux
set -g @ccm-preview "on"              # デフォルト: off
set -g @ccm-preview-position "right"  # または "bottom"
```

カーソル移動で即座に更新され、自動リフレッシュもされます。ANSIカラー（256色、RGB）に対応。端末幅80列以上（右配置）または高さ20行以上（下配置）が必要です。ダッシュボードメニュー（`m`）からもトグル可能。

### Claude Code自動起動

SHELL状態（Claude Codeが終了済み）のプロジェクトウィンドウに切り替えると、ccmが自動的に `--continue` 付きでClaude Codeを再起動し、会話を再開します。

```tmux
set -g @ccm-auto-start "on"     # デフォルト: on（"off"で無効化）
```

ダッシュボードメニュー（`m`）からも設定可能です。

### バックグラウンドセッションセクション（agent-view共存）

Claude Code 2.1.139 で導入された agent view（`claude agents` / `claude --bg` / `claude attach`）は、ユーザーごとの supervisor daemon の下で複数セッションを実行します。ccm はこれら外部セッションをプロジェクト一覧の下に読み取り専用セクションとして表示できるため、tmux 管理下のプロジェクトと daemon 管理下のバックグラウンドセッションを 1 つのダッシュボードで俯瞰できます。

```tmux
set -g @ccm-bg-section "always"   # デフォルト: off
```

デフォルトはオフ（「window = project」運用には影響なし）。ダッシュボードで `b` を押すと一時的にトグル（設定は永続化しない）、`always` にすると常時表示されます（ダッシュボードメニュー `m` からも切替可能）。bg 行で `Enter` を押すと新規 tmux ウィンドウ（ccm 管理外）が開き `claude attach <short>` が自動実行されます — ccm 管理外のウィンドウを使うことで auto-start による attach 横取り（Issue 6）を構造的に回避します。ccm は daemon の `roster.json` と各セッションの `state.json` を観察するだけで、dispatch / stop などのライフサイクル操作は `claude` CLI の責務のまま残しています。ダッシュボード外では `ccm bg list` で同じ情報を取得できます。

### アンチフリッカー

ccmはtmux内でのClaude CodeのUIフリッカーを軽減するため、`CLAUDE_CODE_NO_FLICKER=1` を自動設定します。ユーザー側の設定は不要です。

## Tips

### Claude Codeに他プロジェクトの存在を教える

デフォルトでは、各Claude Codeセッションは他のプロジェクトの存在を知りません。`ccm setup-claude-md` を実行すると、グローバル設定ファイル（`~/.claude/CLAUDE.md`）にccmセクションが追記され、これを改善できます：

```bash
ccm setup-claude-md     # ~/.claude/CLAUDE.md にccmセクションを追加
ccm remove-claude-md    # ccmセクションを削除
```

このセクションは、すべてのClaude Codeセッションに2つのことを教えます。

**他プロジェクトの発見と参照** — `ccm list` / `ccm status` / `ccm capture`。これにより、あるライブラリが別のプロジェクトでどう使われているかを調べたり、別プロジェクトの `CLAUDE.md` を読んだりできます。`ccm capture` は分割ウィンドウにも対応しており、各ペインを実行中のプロセス名付きでキャプチャするため、**自分自身のプロジェクト**を指定すれば Claude セッションが隣のペインを読むこともできます。

**`ccm send` によるプロンプト送信** — こちらは**書き込み**を行うため、実行前に知っておく価値があります。他プロジェクトのClaude Codeセッションにプロンプトを打ち込むコマンドです。安全性を担保する state ポリシーも併せて教えられます。とりわけ、permission ダイアログを表示しているセッションには `--force` を付けても決して送信されません。

`ccm setup-claude-md` は書き込み前に全文を表示して確認を求めるため、実際に何が追加されるかをその場で確認できます。

### tmuxセッションでプロジェクトをカテゴリ分け

tmuxセッションを分けることで、プロジェクトを文脈ごとにグループ化できます（例: 業務、OSS、研究）。`ccm add` は現在のセッションにプロジェクトウィンドウを作成し、ダッシュボードとステータスバーには全セッションのプロジェクトが表示されます。

```bash
tmux new-session -s work       # 業務プロジェクト
tmux new-session -s oss        # OSSプロジェクト

# 各セッション内で通常通りプロジェクトを追加:
ccm add ~/code/auth-service
ccm add ~/code/dashboard-ui

# セッション間の切り替え:
tmux switch-client -t oss      # tmux標準のセッション切り替え
```

> [!TIP]
> スナップショットとの併用も効果的です。各セッションで `ccm snapshot save` / `ccm start` を使えば、プロジェクトレイアウトの保存・復元をセッション単位で独立して行えます。

## 仕組み

- プロジェクトはtmuxウィンドウの `@ccm_project` / `@ccm_dir` タグで管理
- Claude Codeの状態はフック信号＋プロセスツリー検査で検出（プロンプトパターンマッチも補助的に使用）
- 完了マーカー（`* <elapsed>`）はBUSY/PERMIT → IDLE遷移後に30秒間表示
- tmuxテーマとの併用に対応（status-rightの変更を自動検出）

## アンインストール

1. `~/.tmux.conf` から削除：
   ```tmux
   # この行を削除:
   set -g @plugin 'yohasebe/tmux-ccm'
   # または source-file の場合:
   # source-file ~/.tmux/plugins/tmux-ccm/ccm.tmux.conf
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

- [ユーザーガイド](docs/guide.ja.md)
- [English README](README.md) / [User Guide](docs/guide.md)

## ライセンス

MIT
