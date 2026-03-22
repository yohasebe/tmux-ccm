# ccm ユーザーガイド

## ccmとtmuxの関係

tmuxはターミナルを階層的に管理します。ccmはこの構造の中で動作します：

```
ターミナル（Ghostty, iTerm2 等）
 └── tmux サーバー
      └── セッション         ← 作業コンテキスト
           ├── ウィンドウ 0   ← プロジェクトA（ccm管理）
           ├── ウィンドウ 1   ← プロジェクトB（ccm管理）
           ├── ウィンドウ 2   ← プロジェクトC（ccm管理）
           └── ウィンドウ 3   ← 通常のシェル（ccm管理外）
```

**基本概念:** ccmはClaude Codeのセッションを**tmuxウィンドウ**として管理します。各プロジェクトが1つのウィンドウを持ち、ウィンドウを切り替えるだけでプロジェクト間を移動できます。

### ccmがやらないこと

- プロジェクトごとに別のtmuxセッションを作らない
- ターミナルエミュレータの設定を変更しない
- 特定のターミナルエミュレータを要求しない

## はじめかた

### 1. tmuxを起動

```bash
tmux new-session -s work
```

### 2. 最初のプロジェクトを追加

```bash
ccm add ~/code/my-project
```

新しいtmuxウィンドウが作られ、プロジェクトディレクトリに移動して、Claude Codeが `claude --resume` で起動します（過去の会話がある場合は選択して再開できます）。

### 3. プロジェクトを追加

```bash
ccm add ~/code/another-project
ccm add ~/code/third-project api-server   # カスタム名
```

### 4. プロジェクト間を切り替え

ダッシュボード（`prefix + Tab`）を使うか：

```bash
ccm attach my-project    # 名前で
ccm attach 2             # 番号で
```

Claude Codeが動いていないウィンドウに切り替えると、`claude --continue` で自動的に前回の会話を再開します。

### 5. 状態を確認

```bash
ccm status
```

```
STATUS       PROJECT              BRANCH           PORTS        DIRECTORY
------       -------              ------           -----        ---------
◉ BUSY       my-project           main*            3000         ~/code/my-project
● IDLE       another-project      feature-x        -            ~/code/another-project
⚠ PERMIT     api-server           main             8080         ~/code/api-server
```

## ダッシュボード

`prefix + Tab` で開きます。プロジェクト管理のメインインターフェースです。

> ```
> -- ccm Dashboard --
>
> > #1 ◉ BUSY    my-project (main*) ~/code/my-project
>   #2 ● IDLE    another-project (feature-x) ~/code/another-project
>   #3 ⚠ PERMIT  api-server (main) [:8080] ~/code/api-server
>
> [↑↓/jk] select [Enter] attach [p]review [a]dd [s]ave [q] quit
> Last saved: 10:30:45
> ```

### ダッシュボードの操作

| キー | 動作 | 用途 |
|------|------|------|
| `↑↓` or `jk` | 選択移動 | プロジェクト間をナビゲート |
| `Enter` | 切替 | 選択したプロジェクトのウィンドウに移動 |
| `s` | 保存 | スナップショット保存（名前を入力、デフォルトは `_autosave`） |
| `p` | プレビュー | プロジェクトの画面内容を表示（`c` でコピー） |
| `a` | 追加 | 新しいプロジェクトディレクトリを登録 |
| `g` | 登録 | 既存のtmuxウィンドウをccmプロジェクトとしてタグ付け |
| `r` | 削除 | [u]nregister（ウィンドウ残す）か [d]elete（ウィンドウ閉じる）を選択 |
| `/` | 検索 | プロジェクト名でフィルタ |
| `q` or `Esc` | 閉じる | ダッシュボードを閉じる |

ダッシュボードは2秒間隔で自動リフレッシュされます。ナビゲーションキー（`↑↓/jk`）はリフレッシュを待たずに即座に反応します。

## ツリービュー

`prefix + T` で開きます。tmuxの全体構造を階層表示します：

> ```
> work <
>   ◉ my-project (main*) ~/code/my-project <
>   ● another-project (feature-x) ~/code/another-project
>   ⚠ api-server (main) [:8080] ~/code/api-server
>   ■ bash ~/home
> other-session
>   ■ bash ~/home
>
> [↑↓/jk] select  [Enter] attach  [q/Esc] quit
> ```

- `◀` は現在のセッション/ウィンドウを示す
- ウィンドウのみ選択可能（セッションやペインは選択不可）
- ペインは複数ある場合のみ表示

## 状態検出

ccmはClaude Codeの状態をプロセスツリー検査で検出します。画面出力の解析はPERMIT検出のみで使用しており、Claude CodeのUI変更に強い設計です。

### 各状態の検出方法

| 状態 | 検出方法 | 詳細 |
|------|----------|------|
| **SHELL** | プロセスチェック | ウィンドウの子プロセスに `claude` が見つからない |
| **BUSY** | プロセスツリー | `claude` プロセスが子プロセスを持っている（ツール実行中） |
| **IDLE** | プロセスツリー | `claude` プロセスが存在するが子プロセスなし |
| **PERMIT** | 画面キャプチャ | 末尾8行に許可キーワード（"Do you want", "Allow" 等）を検出 |
| **DONE** | 状態遷移 | BUSY/PERMIT → IDLE への遷移を検出 |

### DONE追跡

Claude Codeが処理を完了すると（BUSY → IDLE）、ccmは：
1. 状態をDONEに設定
2. ウィンドウ名とステータスバーに `✔` を表示
3. tmuxメッセージ `✔ project-name: response complete` を表示

DONEフラグは以下の場合にクリアされます：
- そのウィンドウに切り替えた時（ダッシュボード、ツリー、`ccm attach` 経由）
- 新しいプロンプトを送信した時（Claudeが BUSY になりフラグがクリア）

## ステータスバーモード

`~/.tmux.conf` で `set -g @ccm-status-line` を設定します。

### モード0 — アイコン表示（デフォルト）

既存のstatus-rightにアイコン1つを追加。時計やバッテリー表示はそのまま保持されます。全プロジェクトの中で最も優先度の高い状態を表示：

> ```
> 0:◉ my-project  1:⚠ api*  2:✔ web  3:● docs      07:30  ⚠ PERMIT
> ```

優先順: `⚠` PERMIT（黄） > `◉` BUSY（シアン） > `✔` DONE（緑） > `≡` 全IDLE（グレー）

- 向いている人: ステータスバーへの影響を最小限にしたい人
- 注意: プロジェクトごとの詳細はダッシュボードで確認

### モード1 — 全表示（ccm形式ウィンドウリスト）

tmux標準のウィンドウリストをccm形式の色付きエントリに置換。既存のstatus-rightは保持。

> ```
> openai-workflow:● | ccm:◉ | monadic-chat:● | 21:30 2026-03-21
> ```

- 向いている人: メインバーに色付きプロジェクト状態を常時表示したい人
- 注意: tmux標準のウィンドウリストが置換される

### モード2 — 専用行表示

メインバーの下に専用ステータス行を追加。IDLEを含む全プロジェクトをgitブランチ・ポート情報付きで表示します。

> ```
> メインバー:  0:bash  1:my-project  2:api-server     21/03  07:30:00
> ccm行:      my-project:◉(main*) | another-project:●(dev) | api-server:⚠(main)[:8080]
> ```

| アイコン | 状態 | 色 |
|----------|------|-----|
| `⚠` | PERMIT | 黄 |
| `◉` | BUSY | シアン |
| `✔` | DONE | 緑 |
| `●` | IDLE | グレー |
| `■` | SHELL | 暗グレー |

- 向いている人: status-rightを維持しつつ全プロジェクトを常時確認したい人
- 注意: 画面が1行狭くなる（プロジェクト数に応じて自動拡張）

## スナップショット

プロジェクトのレイアウトを保存して、後から復元できます。

### 保存

```bash
ccm snapshot save my-workspace
```

### 復元

```bash
ccm start my-workspace
```

### 自動保存

`ccm stop --all` 実行時に、現在のレイアウトが `_autosave` として自動保存されます：

```bash
# 全プロジェクト停止（自動保存される）
ccm stop --all

# 翌日、前回の構成を復元
ccm start _autosave
```

### スナップショット管理

```bash
ccm snapshot list          # 一覧表示
ccm snapshot delete old    # 削除
```

## Tips

### 既存ウィンドウの登録

Claude Codeが既に動いているtmuxウィンドウを、再起動せずにccm管理下に置けます：

1. ダッシュボード（`prefix + Tab`）を開く
2. `g`（登録）を押す
3. 未登録のウィンドウを選択
4. 名前を入力

コマンドラインからも可能：

```bash
ccm register 3 my-project    # ウィンドウインデックス3を登録
```

### キャプチャとコピー

プロジェクトに切り替えずに画面内容を確認：

```bash
ccm capture my-project              # ターミナルに出力
ccm capture --copy my-project       # クリップボードにコピー
```

ダッシュボードからは `p` でプレビュー、`c` でコピー。

### Git連携

各プロジェクトのgitブランチとdirty状態を表示：

- `main` — クリーンな作業ツリー
- `main*` — 未コミットの変更あり（ステージ済み・未ステージ含む）

ダッシュボード、ツリービュー、`ccm status` で確認できます。

### ポート検出

プロジェクトディレクトリのプロセスがリッスンしているTCPポートを自動検出します：

```
 my-app:◉ [:3000]    api:● [:8080,8443]
```

ポート検出結果は30秒間キャッシュされます。

## トラブルシューティング

### ダッシュボードが開かない

ダッシュボードが表示されてすぐ消える場合：

```bash
# 古いPIDファイルを削除
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/dashboard.pid"
```

### ステータスバーに古いデータが表示される

```bash
# キャッシュをクリア
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/status-cache"
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/status-right-original"
tmux source-file ~/.tmux/plugins/tmux-claude-code-manager/ccm.tmux.conf
```

### セッションのコンテキストがずれる

プロジェクトが間違ったセッションに表示される場合：

```bash
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/popup-session"
```

### 状態がBUSYのまま

プロジェクトがBUSY表示だがClaude Codeは実際にはIDLEの場合、子プロセスが残っている可能性があります：

```bash
ccm capture my-project    # 画面の内容を確認
```

子プロセスが終了すれば、次の2秒リフレッシュで状態が修正されます。

## Agent Teamsとの併用

ccmはClaude Codeの[Agent Teams](https://code.claude.com/docs/en/agent-teams)と併用できます。両者は異なるレベルで動作し、補完関係にあります：

- **ccm** はプロジェクトをtmuxの**ウィンドウ**として管理（1プロジェクト = 1 Claude Code）
- **Agent Teams** は1つのウィンドウ内で並行エージェントを**ペイン**として実行

### 連携の仕組み

ccm管理のプロジェクトウィンドウ内でAgent Teamsを実行すると、ccmの状態検出が全ペインを自動的に集約します：

- いずれかのチームメイトがPERMIT状態 → プロジェクトは `⚠ PERMIT` と表示
- いずれかがBUSY → `◉ BUSY` と表示
- 全チームメイトがIDLE → `● IDLE` と表示

ccmのダッシュボードやステータスバーで、追加設定なしにAgent Teamsの活動状況を確認できます。

### 競合なし

| 機能 | Agent Teams | ccm | 競合 |
|------|------------|-----|------|
| キーボードショートカット | `Shift+↓`, `Ctrl+T`（Claude Code内部） | `prefix + Tab/T/C`（tmuxレベル） | なし |
| ペイン管理 | ウィンドウ内でペイン分割 | ウィンドウを管理 | なし |
| ウィンドウ名 | 変更しない | アイコン+名前を設定 | なし |

### 典型的なワークフロー

1. `ccm add` で複数プロジェクトを登録
2. ダッシュボード（`prefix + Tab`）でプロジェクトに切替
3. そのプロジェクト内でClaude Codeに Agent Team を作成させる
4. Agent Teamsがウィンドウを各チームメイト用にペイン分割
5. ccmのダッシュボードに全チームメイトの集約状態が表示される
6. チームが作業中に `prefix + Tab` で別プロジェクトに切替可能

## 既知の制限

### tmux-resurrect / tmux-continuum

ccmのウィンドウオプション（`@ccm_project`、`@ccm_dir`）はセッション復元プラグインでは自動保持されません。tmux復元後は `ccm start _autosave` で最後のautosaveスナップショットからプロジェクトを再登録してください。

### ステータス更新間隔

ccmのステータスバー更新はtmuxの `status-interval` 設定（デフォルト: 15秒）に依存します。より高速な更新が必要な場合：

```tmux
set -g status-interval 5    # 5秒ごとに更新
```

値を下げるとCPU使用量がわずかに増加します。

### デバッグ

ccmの現在の状態を確認するには：

```bash
ccm status                    # 全プロジェクトの状態を表示
ccm tree                      # 全体の階層を表示
tmux show-option -gv status-right   # status-rightの内容を確認
tmux show-option -gqv @ccm-status-line  # 現在のモード（0/1/2）
```

ccmの状態を完全にリセットするには：

```bash
rm -rf "${TMPDIR:-/tmp}/ccm-$(id -u)"
tmux source-file ~/.tmux.conf
```
