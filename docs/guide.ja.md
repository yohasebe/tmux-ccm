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

### 0. Claude Codeの認証

Claude Codeを初めて使う場合は、最初に一度起動して認証を済ませてください：

```bash
claude
```

対話的なプロンプトに従い、プラン選択（サブスクリプションまたはAPIキー）とブラウザ認証を行います。完了すれば、ccmを使う準備ができます。

### 1. tmuxを起動

```bash
tmux new-session -s work
```

### 2. 最初のプロジェクトを追加

```bash
ccm add ~/code/my-project
```

新しいtmuxウィンドウが作られ、プロジェクトディレクトリに移動して、Claude Codeが `claude --continue` で起動します（そのディレクトリの最新の会話がある場合は自動的に再開します）。

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

> [!TIP]
> Claude Codeが動いていないウィンドウに切り替えると、`claude --continue` で自動的に前回の会話を再開します。

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

`prefix + Tab` で開きます。プロジェクト管理のメインインターフェースです。prefixなしの単一キー（例: `F1`）でトグルする設定も可能です — 詳しくは[READMEのキーバインドセクション](../README.ja.md#キーバインド)を参照してください。

> ```
> ── ccm Dashboard ──────────────
>   6 project(s)
>
> ▶ #5  ⚠PERMIT  ml-pipeline    ✔20s ~/code/ml-pipeline
>   #4  ✔DONE    auth-service   ✔2s  ~/code/auth-service
>   #2  ◉BUSY    api-gateway    ✔6s  ~/code/api-gateway
>   #3  ●IDLE    web-dashboard  ✔1m  ~/code/web-dashboard
>   #6  ●IDLE    mobile-app     ✔5m  ~/code/mobile-app
>   #7  ■SHELL   docs-site      ✔1d  ~/code/docs-site
>
> [↑↓/jk] select [Enter] attach [p]review [a]dd [n]ame [r]emove
> e[x]it all [s]ave [t]ree [m]enu [q] quit
> Hooks: ON
> ```

### ダッシュボードの操作

| キー | 動作 | 用途 |
|------|------|------|
| `↑↓` or `jk` | 選択移動 | プロジェクト間をナビゲート |
| `Enter` | 切替 | 選択したプロジェクトのウィンドウに移動 |
| `s` | 保存 | スナップショット保存（名前を入力、デフォルトは `_autosave`） |
| `p` | プレビュー | プロジェクトの画面内容を表示（`c` でコピー） |
| `a` | 追加 | 新しいプロジェクトディレクトリを登録 |
| `n` | 名前変更 | 選択プロジェクトの名前を変更 |
| `g` | 登録 | 既存のtmuxウィンドウをccmプロジェクトとしてタグ付け |
| `r` | 削除 | [u]nregister（ウィンドウ残す）か [d]elete（ウィンドウ閉じる）を選択 |
| `x` | 一括終了 | アイドル状態のClaude Codeを全て終了してリソースを解放 |
| `/` | 検索 | プロジェクト名でフィルタ |
| `t` | ツリー | ツリービューに切替 |
| `m` | メニュー | インタラクティブメニューに切替 |
| `q` / `Esc` | 閉じる | ダッシュボードを閉じる |

ダッシュボードは2秒間隔で自動リフレッシュされます。ナビゲーションキー（`↑↓/jk`）はリフレッシュを待たずに即座に反応します。

## ツリービュー

`prefix + T` で開きます。tmuxの全体構造を階層表示します：

> ```
> work ◀
>   ◉ my-project (main*) ~/code/my-project ◀
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

ccmはClaude Codeフック（推奨）とプロセスツリー検査を組み合わせたハイブリッド方式で状態を検出します。

### Claude Codeフック（推奨）

最良の検出精度を得るためにフックをインストールします：

```bash
ccm setup-hooks
```

`~/.claude/settings.json` にフックが追加されます：

| フック | 信号 | 検出内容 |
|--------|------|----------|
| `UserPromptSubmit` | BUSY | プロンプト送信 → Claude処理中（テキスト生成含む） |
| `PreToolUse` | BUSY | ツール実行開始（マルチターンの検出ギャップを解消） |
| `SubagentStart` | BUSY | サブエージェント起動（Agentツール） |
| `Stop` / `StopFailure` | DONE | Claude応答完了 |
| `PermissionRequest` | PERMIT | ツールがユーザーの許可を要求 |
| `Notification` | PERMIT / DONE | 許可プロンプト表示 / アイドル通知 |
| `SessionEnd` | SHELL | セッション終了（/exit、Ctrl+D等） |
| `PermissionDenied` | PERMIT | autoモードでの拒否（`/permissions`で再試行） |

> [!NOTE]
> フック信号は `$TMPDIR/ccm-$UID/hooks/` に書き込まれます。BUSY は Claude Code プロセスが生存している限り信頼されます（`Stop`/`SessionEnd` フックまたはプロセス終了でクリア）。DONE は30秒後、PERMIT は安全網として10分後に自動クリアされます。

フックの状態はダッシュボードのフッターと `ccm status` の出力に表示されます（Hooks: ON/OFF）。既にインストール済みの場合、`ccm setup-hooks` は再インストールをスキップします。ccmを別のパスに再インストールした場合は、フックのパスが自動的に更新されます。

削除するには: `ccm remove-hooks`

### 各状態の検出方法

| 状態 | 検出方法 | 詳細 |
|------|----------|------|
| **SHELL** | プロセスチェック | ウィンドウの子プロセスに `claude` が見つからない |
| **BUSY** | フック / プロセスツリー | フック: UserPromptSubmit, PreToolUse, SubagentStart。フォールバック: `claude` が子プロセスを持つ |
| **IDLE** | プロセスツリー | `claude` プロセスが存在するが子プロセスなし、新鮮なフック信号なし |
| **PERMIT** | フックのみ | PermissionRequest / PermissionDenied / Notification（permission_prompt）。`ccm setup-hooks` が必要 |
| **DONE** | フック信号 / 状態遷移 | フック: Stop発火。フォールバック: BUSY/PERMIT → IDLE遷移を検出 |

### フックなしでの検出

フックなしの場合、ccmはプロセスツリー検査とプロンプトパターンマッチにフォールバックします。この場合：
- テキスト生成中（ツール未使用）はBUSYではなくIDLEと表示される
- DONE検出はBUSY→IDLE遷移のヒューリスティクスに依存する

### DONE追跡

Claude Codeが処理を完了すると、ccmは：
1. 状態をDONEに設定
2. ウィンドウ名とステータスバーに `✔` を表示
3. デスクトップ通知を送信（設定時）

DONEフラグは以下の場合にクリアされます：
- 30秒経過（自動クリア）
- そのウィンドウに切り替えた時（ダッシュボード、ツリー、`ccm attach` 経由）
- 新しいプロンプトを送信した時（Claudeが BUSY になりフラグがクリア）

## ステータスバーモード

`~/.tmux.conf` で `set -g @ccm-status-line` を設定します。設定の詳細とスクリーンショットは[READMEのステータスバーセクション](../README.ja.md#ステータスバー)を参照してください。

### モード0 — アイコン表示（デフォルト）

既存のstatus-rightにアイコン1つを追加。時計やバッテリー表示はそのまま保持されます。全プロジェクトの中で最も優先度の高い状態を表示：

> ```
> 0:◉ my-project  1:⚠ api*  2:✔ web  3:● docs      07:30  ⚠ PERMIT
> ```

優先順: `⚠` PERMIT（黄） > `◉` BUSY（オレンジ） > `✔` DONE（緑） > `≡` 全IDLE（グレー）

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
| `◉` | BUSY | オレンジ |
| `✔` | DONE | 緑 |
| `●` | IDLE | ブルー |
| `■` | SHELL | 暗グレー |

- DONEは30秒後に自動クリアされIDLEに戻る
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

#### tmux起動時の自動復元

tmux起動時に最後の `_autosave` スナップショットを自動復元するには、`~/.tmux.conf` に以下を追加：

```tmux
set -g @ccm-auto-restore "on"    # デフォルト: off
```

> [!NOTE]
> TPM経由で起動時に `_autosave` をロードします。既にccmプロジェクトがある場合はスキップされます。

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
tmux source-file ~/.tmux/plugins/tmux-ccm/ccm.tmux.conf
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

ccmのウィンドウオプション（`@ccm_project`、`@ccm_dir`）はセッション復元プラグインでは自動保持されません。tmux復元後は `ccm start _autosave` で最後のautosaveスナップショットからプロジェクトを再登録してください。または `@ccm-auto-restore "on"` を設定すれば、tmux起動時に自動で復元されます。

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

## FAQ

### ターミナルアプリを閉じるとプロジェクトは失われますか？

いいえ。tmuxはターミナルエミュレータ（Ghostty、iTerm2など）とは独立したバックグラウンドサーバープロセスとして動作しています。ターミナルを閉じても表示が切断されるだけで、すべてのtmuxセッション、ウィンドウ、ccmプロジェクトは動作し続けます。ターミナルを再度開いて `tmux attach` を実行すれば再接続できます。

> [!TIP]
> 複数のtmuxセッションがある場合は、`tmux attach -t work`（`work` をセッション名に置き換え）で特定のセッションに再接続できます。

### Macがスリープするとプロジェクトは失われますか？

いいえ。スリープはすべてのプロセスを一時停止しますが、終了はしません。Macを起動すると、tmuxとすべてのccmプロジェクトは中断した場所からそのまま再開します。

### スナップショットのロードが必要になるのはいつですか？

tmuxサーバー自体が終了した場合のみです。これは以下の場合に発生します：

- コンピュータの再起動またはシャットダウン
- マシンのクラッシュまたは電源喪失
- `tmux kill-server` を手動で実行

このような場合、`ccm start _autosave` を実行して前回のワークスペースを復元してください。ヒント：`.tmux.conf` で `@ccm-auto-restore on` を設定すると、tmux起動時に自動復元されます。

### `ccm start` と `ccm snapshot load` の違いは何ですか？

同じです。`ccm start <name>` は `ccm snapshot load <name>` の短いエイリアスです。同様に、`ccm stop --all` は対となるコマンドで、`_autosave` スナップショットを保存してからすべてのプロジェクトウィンドウを閉じます。

### ccmを使う前にClaude Codeのセットアップが必要ですか？

はい。通常のターミナルで一度 `claude` を実行して、初回認証（サブスクリプションまたはAPIキーの設定）を済ませてください。認証後は、ccmが各プロジェクトウィンドウでClaude Codeを自動的に起動できるようになります。

### 複数のtmuxセッションでccmを使えますか？

ccmはプロジェクトを単一のtmuxセッション内のウィンドウとして管理します。ダッシュボードとステータスバーは全セッションのプロジェクトを表示しますが、`ccm add` は現在のセッションにウィンドウを作成します。異なるプロジェクトセットが必要な場合は、名前付きスナップショット（`ccm snapshot save work`、`ccm snapshot save personal`）を活用してください。

### 2つのプロジェクトを並べて表示できますか？

ccmはtmuxウィンドウ単位でプロジェクトを管理しているため、tmuxのペイン分割で2つのClaude Codeを同時に起動すると状態検出やフック信号が干渉するため推奨されません。

**推奨される方法:** 別のターミナルウィンドウ（例：Ghosttyの新しいウィンドウ）を**tmuxを使わずに**開き、プロジェクトディレクトリに移動してClaude Codeを直接起動します：

```bash
cd ~/code/other-project
claude --continue
```

これにより、ccm管理下のプロジェクトと並行して、完全に独立したClaude Codeセッションで作業できます。

**ccmとの同期:** 別ウィンドウでの作業後、ccm側のセッションは自動的にキャッチアップします。アイドル自動終了により、10分後に古いセッションが終了し、ウィンドウに切り替えるとClaude Codeが `--continue` で再起動して最新の会話を読み込みます。即座にキャッチアップしたい場合は、ccmウィンドウで `/exit` と入力してから一度離れて戻ってください。

### プロジェクトのClaude Codeを止めるには？

Claude Codeのプロンプトで `/exit` を入力してください。Claude Codeは終了しますが、**tmuxウィンドウとプロジェクト登録は保持**されます。プロジェクトはSHELL状態で表示され、ウィンドウに切り替えると自動的に再起動します。

tmuxウィンドウ自体を直接閉じないでください（`prefix + &` やシェルの `exit` など）。ウィンドウを閉じるとccm登録も消え、次のautosaveからプロジェクトが消えます。

ほとんどの場合、手動でClaude Codeを止める必要はありません。10分後にアイドル自動終了が処理します。

### `_autosave` と名前付きスナップショットの違いは？

| | `_autosave` | 名前付きスナップショット |
|---|---|---|
| **作成** | 2分ごとに自動 | ダッシュボードの `s` キーで手動 |
| **内容** | 常に現在のプロジェクト一覧を反映 | 保存時点のスナップショット |
| **上書き** | される（2分ごとに更新） | されない（日付ベースのユニークな名前） |
| **auto-restore** | 使用される | されない（`ccm start <名前>` で手動ロード） |

**ヒント:** シャットダウン前に全プロジェクトを確実に保存したい場合は、ダッシュボードの `s` キーで名前付きスナップショットを保存してください。`save-20260331-1230` のようなチェックポイントが作成され、自動上書きされません。後で `ccm start save-20260331-1230` で復元できます。
