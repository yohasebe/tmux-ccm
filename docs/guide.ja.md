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
> ▶ #5  ⚠ PERMIT  ml-pipeline                ~/code/ml-pipeline
>   #4  ● IDLE    auth-service     * 2s      ~/code/auth-service
>   #2  ◉ BUSY    api-gateway                ~/code/api-gateway
>   #3  ● IDLE    web-dashboard              ~/code/web-dashboard
>   #6  ● IDLE    mobile-app                 ~/code/mobile-app
>   #7  ■ SHELL   docs-site                  ~/code/docs-site
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
| `/` | フィルタ | ライブインクリメンタル検索: タイプで絞り込み、`↑↓`/`C-p`/`C-n` で候補選択、`Enter` でアタッチ、`C-u` でクリア、`Esc` でキャンセル。Unicode 対応で日本語プロジェクト名も日本語でマッチ |
| `t` | ツリー | ツリービューに切替 |
| `m` | メニュー | インタラクティブメニューに切替 |
| `q` / `Esc` | 閉じる | ダッシュボードを閉じる |

ダッシュボードは2秒間隔で自動リフレッシュされます。ナビゲーションキー（`↑↓/jk`）はリフレッシュを待たずに即座に反応します。

### フィルタ直行ショートカット

ダッシュボードを開いた直後によく `/` を押すなら、`~/.tmux.conf` で `@ccm-key-search` をバインドするとダッシュボードを最初からライブフィルタモードで開けます:

```tmux
set -g @ccm-key-search "/"   # prefix + / でダッシュボードをフィルタモードで開く
```

シェルや別の tmux バインディングから `ccm search`（または `ccm dashboard --search`）を呼んでも同じです。プロジェクト数が多いときに、リストをスクロールせず数文字で目的のプロジェクトに直行できます。

### プレフィックスなしのダッシュボードホットキー

`prefix + Tab` よりファンクションキー一発で開きたい場合、`@ccm-key-dashboard-noprefix` で直接バインドできます:

```tmux
# 必ず ccm プラグインの読み込み行よりも前に配置してください
# (詳細は README.md#キーバインド の IMPORTANT 参照)。
set -g @ccm-key-dashboard-noprefix "F1"   # F1 単独 (prefix 不要) → ダッシュボード
```

prefix 経由の binding と同じ `display-popup` 呼び出しを通るので、popup タイトルの色付き ccm ロゴはそのまま表示されます。自前で `bind-key -n F1 display-popup …` を書くこともできますが、`-T` の format 文字列を完全にコピーしないとロゴは出ません。

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

## プロジェクト間のプロンプト送信

`ccm send` は別プロジェクトの Claude Code セッションに直接プロンプトを投入するコマンドです。`tmux send-keys -l ... Enter` を手動で組み立てる必要がなくなります。

```bash
# 位置引数のシンプルな送信（TTY なら確認プロンプトあり）
ccm send blog "前回のレビュー結果をまとめてください"

# ファイルから
ccm send research --file /tmp/brief.md

# パイプから — MCP サーバー（Gmail, GitHub 等）から直接渡すのに便利
echo "parser リポジトリの Issue #42 の調査をお願いします" | ccm send fzf-workflow --stdin -y

# 複数行の本文 — \n は Claude の「改行のみ(送信しない)」キーに変換されるので、
# 複数行プロンプトとして 1 つのメッセージで届きます
printf 'context:\nbug: 120 行目で NPE\nplease fix' | ccm send api-server --stdin -y

# 送信せずプロンプトに入力だけ(ユーザーがターゲットペインで続きを書く)
ccm send blog --no-enter "TODO: "
```

### 状態に応じた動作

| ターゲット状態 | デフォルト | `--force` | `--start` |
|---|---|---|---|
| **IDLE** | 即送信 | — | — |
| **BUSY** | 拒否(進行中のターンと混線するため) | 入力バッファにキューして送信 | — |
| **SHELL**(Claude 未起動) | 拒否 | — | Claude を起動して 2 秒待機後に送信 |
| **PERMIT**(許可ダイアログ表示中) | **強拒否** | **`--force` でも拒否** — permission ダイアログに文字を送ると誤ってツール実行を承認/拒否する危険 | — |

### フラグ一覧

| フラグ | 用途 |
|---|---|
| `--file <path>` | ファイルからメッセージを読む |
| `--stdin`(または単独の `-`) | 標準入力から読む |
| `--no-enter` | 最後の Enter を送らない(プロンプトへの入力のみ) |
| `--force` | BUSY なターゲットへの送信を許可(Claude の入力バッファにキュー) |
| `--start` | SHELL 状態なら Claude を自動起動してから送信 |
| `-y`, `--yes` | 対話的な確認プロンプトをスキップ |
| `--` | 以降をフラグとして解釈しない(`-` で始まるメッセージ用) |

stdin/stdout が TTY でない場合(パイプ経由)、確認プロンプトは自動スキップされます。`echo "..." \| ccm send ...` が `-y` なしでそのまま動きます。

ターゲット指定はプロジェクト名、`#<idx>`、またはウィンドウ番号単独のいずれでも OK です。

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
| `PostToolUse` | BUSY | ツール実行完了 — permission 後のBUSYシグナルを維持 |
| `PostToolUseFailure` | BUSY | ツール実行失敗 |
| `SubagentStart` / `SubagentStop` | BUSY | サブエージェント実行中（親エージェントは作業継続中） |
| `PreCompact` / `PostCompact` | BUSY | コンテキスト圧縮はビジー作業 |
| `Stop` / `StopFailure` | BUSY信号クリア | Claude応答完了（信号ファイルを削除） |
| `PermissionRequest` | PERMIT | ツールがユーザーの許可を要求 |
| `Notification` | PERMIT / 信号クリア | 許可プロンプトまたは MCP elicitation ダイアログ表示 / アイドル通知（matcher: `permission_prompt`, `elicitation_dialog`, `idle_prompt`）|
| `SessionEnd` | SHELL | セッション終了（/exit、Ctrl+D等） |
| `PermissionDenied` | PERMIT | autoモードでの拒否（`/permissions`で再試行） |

> [!NOTE]
> フック信号は `$TMPDIR/ccm-$UID/hooks/` に書き込まれます。BUSY は Claude Code プロセスが生存している限り信頼されます（`Stop`/`SessionEnd` フックまたはプロセス終了でクリア）。PERMIT は安全網として10分後に自動クリアされます。

フックの状態はダッシュボードのフッターと `ccm status` の出力に表示されます（Hooks: ON/OFF）。既にインストール済みの場合、`ccm setup-hooks` は再インストールをスキップします。ccmを別のパスに再インストールした場合は、フックのパスが自動的に更新されます。

削除するには: `ccm remove-hooks`

### 各状態の検出方法

| 状態 | 検出方法 | 詳細 |
|------|----------|------|
| **SHELL** | プロセスチェック | ウィンドウの子プロセスに `claude` が見つからない |
| **BUSY** | event-log + JSONL stop_reason | 主経路: BUSY 系フック (`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`SubagentStart`/`Stop`、`PreCompact`/`PostCompact`) が追記する per-session event log (`hooks/<sessionId>.events.jsonl`、Claude Code の session UUID でキー)。`derive_state_from_events` が tail を純関数で評価し、最新エントリが start-class なら BUSY を返す。フック沈黙時は JSONL `stop_reason` で橋渡し: 直近の `tool_use` ならツールターン境界の Stop を超えて BUSY を維持、`end_turn` / `max_tokens` / `stop_sequence` が最新 event より新しければ数秒以内に IDLE へ release。Claude Code housekeeping レコード（`system/away_summary`、`turn_duration`、`attachment/task_reminder`、`permission-mode`、`file-history-snapshot`、`last-prompt`）は JSONL 活動から除外され、recap や起動時 housekeeping が偽の活動として検出されない |
| **IDLE** | event-log + capture-pane | event log の最新エントリが end-class（`stop` / `notify_idle` / `notify_permit` 解消後）で、入力プロンプト `❯ ` が表示されており、PERMIT フッターにマッチしない状態。フックなし時は legacy fallback がプロセスツリー + プロンプト可視性のみで判定 |
| **PERMIT** | フック + capture-pane フォールバック | 主経路: `PermissionRequest` / `PermissionDenied` / `Notification`（permission_prompt）フック。フォールバック: モーダルフッター（permission ダイアログの `Esc to cancel · Tab to amend`、confirmation modal の `Enter to confirm · Esc to <verb>` — v2.1.144 の `/model` 形式 `Enter to confirm · d to set as default for new sessions · Esc to cancel` のような中間 `· <action key>` セグメントも許容）をペインから直接検出 — フックが途中で停止したセッションでも捕捉可能 |
| **完了（`* elapsed`）** | 表示レイヤー | 一時的マーカー: BUSY/PERMIT → IDLE遷移後に30秒間表示、その後クリア。アスタリスクは緑（直近の完了に視線を誘導）、経過時間は dim |
| **マルチペイン（`[N]`）** | ウィンドウ検査 | tmux ペインを 2 つ以上含むウィンドウに対し、全レンダラー（dashboard / status bar / `ccm status`）でプロジェクト名直後に表示。角括弧 dim、数字 cyan。集約状態が非アクティブペインのものである可能性をユーザーが認識できるようにする。詳細（sliver 保護と PERMIT 自動フォーカス）は下記「Agent Teamsとの併用」を参照 |

### フックなしでの検出

フックなしの場合、ccmはプロセスツリー検査とプロンプトパターンマッチにフォールバックします。この場合：
- テキスト生成中（ツール未使用）はBUSYではなくIDLEと表示される
- 完了検出はBUSY→IDLE遷移のヒューリスティクスに依存する

### 完了追跡

Claude Code が処理を完了すると、ccm は:
1. 完了タイムスタンプを記録 (プロジェクトは IDLE に遷移)
2. ダッシュボード / ステータスバー / `ccm status` のプロジェクト名の右に `* <elapsed>` を「最近完了」マーカーとして表示 (アスタリスクは緑、時間は dim)
3. デスクトップ通知を送信 (設定時)

`* <elapsed>` マーカーは以下の場合にクリアされます:
- 30 秒経過 (自動クリア)
- そのウィンドウに切り替えた時 (ダッシュボード、ツリー、`ccm attach` 経由)
- 新しいプロンプトを送信した時 (Claude が BUSY になりマーカーがクリア)

## ステータスバーモード

`~/.tmux.conf` で `set -g @ccm-status-line` を設定します。設定の詳細とスクリーンショットは[READMEのステータスバーセクション](../README.ja.md#ステータスバー)を参照してください。

### モード0 — アイコン表示

既存の status-right にアイコン 1 つを追加。時計やバッテリー表示はそのまま保持されます。全プロジェクトのうち最優先の状態を表示：

> ```
> 5: PERMIT ⚠   13:30
> ```

優先順: `⚠` PERMIT（黄） > `◉` BUSY（オレンジ） > `≡` 全IDLE（グレー）

- 向いている人: 既存のテーマと最も保守的に共存したい人
- 注意: プロジェクトごとの詳細はダッシュボードで確認

### モード1 — 全表示（ccm形式ウィンドウリスト）

tmux標準のウィンドウリストをccm形式の色付きエントリに置換。既存のstatus-rightは保持。

> ```
> myapp:● | sideproject:◉ | docs:● | 21:30 12-25
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
| `●` | IDLE | ブルー |
| `* <elapsed>`（プロジェクト名の後ろ） | IDLE（最近完了） | アスタリスク緑、時間 dim |
| `■` | SHELL | 暗グレー |

- `* <elapsed>` マーカーは完了後30秒間表示され、その後消えます（プロジェクトは `●` IDLE のまま）
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

`_autosave` スナップショットは、ccm プロジェクトが存在する間 **2 分ごとに自動更新** されます。加えて `ccm stop --all` 実行時にも書き込まれます:

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

`(Nm)` suffix が付いて (例: `BUSY (5m)`) BUSY のまま戻らない場合は、ccm が自分のシグナルが stale であることを検出したが「実は IDLE」を証明できない状態です。通常は upstream の double silent fail (Stop hook 喪失 AND JSONL に完了記録なし) が原因。最終手段として:

```bash
ccm reset my-project      # フックシグナル、event log、キャッシュ済み state option をクリア
```

`ccm reset` は会話履歴・スナップショット・実行中の `claude` プロセスには触りません — 検出が読む ephemeral な runtime artefact だけを wipe します。次のスキャンで一から再解決されます。通常の「Claude が固まった」状況では `/exit` をペイン内で入力するのが筋です。

### 全プロジェクトが同じ状態で固まる

**すべて**のプロジェクトが同じ状態（例: 全て BUSY）で固まり、リフレッシュしても更新されない場合、検出サイクル自体が silent な例外を踏んでいる可能性があります。ログを確認:

```bash
ccm errors
```

各行は捕捉された例外（タイムスタンプ、スコープ、トレースバック付き）です。空（`No silent-caught errors logged.`）であれば検出サイクルは正常です。エントリが蓄積している場合、最新のトレースバックが失敗箇所を示します。`ccm errors --clear` でアクティブログとローテートされた `errors.log.1` の両方を削除できます。

## Agent Teamsとの併用

ccmはClaude Codeの[Agent Teams](https://code.claude.com/docs/en/agent-teams)と併用できます。両者は異なるレベルで動作し、補完関係にあります：

- **ccm** はプロジェクトをtmuxの**ウィンドウ**として管理（1プロジェクト = 1 Claude Code）
- **Agent Teams** は1つのウィンドウ内で並行エージェントを**ペイン**として実行

### 連携の仕組み

ccm管理のプロジェクトウィンドウ内でAgent Teamsを実行すると、ccmの状態検出が全ペインを自動的に集約します：

- いずれかのチームメイトがPERMIT状態 → プロジェクトは `⚠ PERMIT` と表示
- いずれかがBUSY → `◉ BUSY` と表示
- 全チームメイトがIDLE → `● IDLE` と表示

ccmのダッシュボードやステータスバーで、追加設定なしにAgent Teamsの活動状況を確認できます。マルチペインのウィンドウには、プロジェクト名直後に `[N]` マーカー（角括弧 dim、数字 cyan）が全レンダラーで付与されるので、並列のチームメイトを抱えるプロジェクトを一目で識別できます。

**Sliver 保護**: `SLIVER_HEIGHT_THRESHOLD`（デフォルト 4 行 — 下記「環境変数」参照）より低いペインは状態集約から除外されます。極端に小さいペイン（過去の split で残った 1 行のストリップなど）では Claude の `❯` プロンプトが描画されず、capture-pane 検出が「子プロセス + プロンプト不可視」を BUSY と誤判定してしまうため、除外することで不可視 sliver がウィンドウ全体の状態を汚染するのを防ぎます。Agent Teams で意図的に小さいペインを使っており除外したくない場合は `CCM_SLIVER_HEIGHT_THRESHOLD` で閾値を上げてください。

**Attach 時の自動フォーカス**: ccm 経由で attach（dashboard / `ccm attach`）した際、いずれかのチームメイトペインが許可待ち（`⚠ PERMIT`）でアクティブペインがそれ以外なら、ccm が自動で PERMIT ペインにフォーカスを切り替えます。許可が必要なプロジェクトに attach するたびに `prefix + 矢印` する手間を省きます。PERMIT 限定 — BUSY のチームメイトは監視対象であってユーザー入力を要求するわけではないので、フォーカスは奪いません。

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

## agent view（バックグラウンドセッション）との併用

Claude Code 2.1.139 で導入された [agent view](https://claude.com/blog/agent-view-in-claude-code) は、`claude agents`（TUI）/ `claude --bg <prompt>`（バックグラウンドディスパッチ）/ `claude attach <short>`（フォアグラウンドアタッチ）の 3 つの入口を持ちます。これらはすべてユーザーごとの supervisor daemon の下で動作し、tmux の外側にいます。ccm はこの daemon の状態を読んでダッシュボードに読み取り専用セクションとして表示するため、tmux 管理下のプロジェクトと daemon 管理下のバックグラウンドセッションを 1 つのビューで俯瞰できます。

### セクションを有効にする

デフォルトはオフ（agent view を使わないユーザーには余計な表示は出ません）。3 通りの方法で表示できます:

- ダッシュボードで `b` を押す — その popup の間だけトグル、設定は永続化しない。
- `~/.tmux.conf` に `set -g @ccm-bg-section "always"` を追加 — 毎回開くたびに表示。
- ダッシュボードメニュー（`m`）で `Background sessions: …` 行をトグル — 上記オプションを `~/.tmux.conf` に書き込みます。

セクションはプロジェクト一覧の下に表示され、各アクティブワーカーの short ID、正規化された状態（`✽ WORKING` / `✻ NEEDS` / `● IDLE` / `✓ DONE` / `✕ FAILED`）、可読な名前、経過時間、作業ディレクトリを一覧します。

### ダッシュボードから attach する

`↑/↓` で bg 行に選択を移動して（プロジェクト一覧と bg セクション間をシームレスに行き来できます）`Enter` を押すと、ccm が現在の tmux セッションに新規ウィンドウを作成し、その中で `claude attach <short>` を実行します。ウィンドウの作業ディレクトリは bg セッションのものを継承し、ウィンドウ名は `bg-<short>` になるので `prefix + w`（choose-tree）から見つけやすい構成です。

新規ウィンドウは ccm プロジェクトとして登録 **されません** — `@ccm_project` / `@ccm_dir` タグを持たないため、ccm の `auto_start_claude` が `claude --continue` を injection で先回りすることがありません。これが attach と auto-start の競合（Issue 6）に対する構造的回避策です。これなしに ccm 管理ウィンドウから attach すると、`claude attach <short>` がシェルコマンドではなく既存の `claude --continue` への user message として届いてしまいます。claude から detach した後はそのウィンドウを `prefix + &` で閉じてください。

### ライフサイクル操作は `claude` に残す

ccm は daemon を **観察のみ** します — `~/.claude/daemon/` に書き込んだりシグナルを送ったりはしません。Dispatch / 終了は `claude` CLI 側の責務です:

```bash
claude agents                 # インタラクティブTUI
claude --bg "<prompt>"        # fire-and-forget バックグラウンド起動
claude attach <short>         # 既存セッションをフォアグラウンド化
claude stop <short>           # セッションを停止
```

ダッシュボード外では `ccm bg list` でカラー付きテーブルを表示できます。

### データソース

リーダーは daemon が書き出す 2 つのファイルを結合します（ccm 側は read-only）:

- `~/.claude/daemon/roster.json` — 現在アクティブなワーカー（pid / sessionId / cwd / cliVersion / dispatch メタデータ）。idle 1 時間程度で `settled (done)` となり roster から外れるため、ccm が表示する範囲は `claude agents` 自身が表示するものと一致します。
- `~/.claude/jobs/<short>/state.json` — セッションごとのライブ状態（`working` / `needs_input` / `idle` / `done` / `failed`）、tempo、進行中タスク数、自動生成された name。

ファイル欠落・JSON 破損・daemon 未起動はすべて「アクティブなバックグラウンドセッションなし」として安全に解決されるため、agent view の不在がダッシュボードを壊すことはありません。

## 環境変数

ccmはいくつかのチューニング用環境変数を公開しています。デフォルト値は多くのユーザーにとって適切に動作するよう選ばれており、特定の問題が観察された場合にのみ調整してください。tmuxを起動する前にシェルの rc ファイル（例: `~/.zshrc`）で設定します。

### 検出タイミング

| 変数 | デフォルト | 用途 |
|------|-----------|------|
| `CCM_BUSY_HOOK_JSONL_WINDOW` | `600`（秒） | event-log path の combined-stale fallback 窓。最新イベントと JSONL の両方がこの秒数より古い場合、derive は legacy fallback に委ねる（最終的に IDLE に解決される）。abandoned session や上流 silence の長期テールを救う |
| `CCM_JSONL_HOOK_GAP_TOLERANCE` | `60`（秒） | recap phantom 判別（legacy `hook_fresh_busy` ルール）。直前の実会話 activity から秒数以上後に発火した BUSY フックを phantom として拒否（上流 `away_summary` 等）。derive の Esc-release / silent-completion 鮮度チェックも同じ窓を使う |
| `CCM_COMPLETED_AT_TIMEOUT` | `30`（秒） | BUSY/PERMIT → IDLE 遷移後にダッシュボードで `* elapsed` 完了マーカーが表示される時間 |
| `CCM_COMPLETION_GRACE_SEC` | `3`（秒） | Stop hook 発火から COMPLETED デスクトップ通知までの猶予時間。Claude Code は各ターン境界（ツール実行中も含む）で Stop を発火するため、ccm はこの秒数だけ待ってから通知する。その間に次の PreToolUse / UserPromptSubmit が発火すれば通知はキャンセルされる |
| `CCM_PERMIT_MAX_TIMEOUT` | `600`（秒） | `evaluate_fast` (statusline) パスの安全網: これより古い PERMIT フックを stale 扱いし、上流の signal-clearing 失敗で PERMIT が貼り付くのを防ぐ |
| `CCM_IDLE_EXIT_TIMEOUT` | `600`（秒） | Claude Code セッションが IDLE 状態でいられる最大時間（`x` 一括終了の対象となる閾値、自動終了のトリガー） |
| `CCM_STARTUP_GRACE_SEC` | `60`（秒） | legacy `startup_transient_raw_busy` ルールが hook signal 未着の raw=BUSY を IDLE に降格させる claude プロセス年齢の窓。`claude --continue` 起動時の MCP ロード (通常 10-30 秒) をカバー |
| `CCM_SLIVER_HEIGHT_THRESHOLD` | `4`（行） | ウィンドウの状態集約に参加する tmux ペインの最小高さ。これより小さいペインは Claude の `❯` プロンプトを描画できず、capture-pane 検出が「子プロセスあり + プロンプト不可視」で BUSY と誤判定するため除外する。Agent Teams で意図的に小さいペインを使っており除外したくない場合は上げる、フィルタを完全無効化したい場合は 1 まで下げる |
| `CCM_HOOK_CMD_TIMEOUT` | `5000`（ms） | Claude Code が ccm の各フック呼び出しに与えるタイムアウト。ccm のフックはシグナルファイル 1 つを書くだけで完結するため、デフォルト値で十分余裕がある。フックのハングを調査する際に下げる用途以外は変更不要 |
| `CCM_START_WAIT_SEC` | `10`（秒） | `ccm send --start` が SHELL 状態のターゲットに `claude --continue` を送った後、IDLE に到達するまでポーリングする最大秒数。実際の 2 ケースに合わせた値: 通常の resume は 1-5 秒で IDLE に到達、長いセッションの auto-`/compact` は 10-60 秒以上 BUSY が続く (どのみち送信は届かない) → 10 秒で refuse する方が操作者に早く制御を返せる。インタラクティブ実行時は 1 秒ごとに進捗を表示するので待ち時間が可視化される。環境的にもっと必要なら上げる |

### ランタイムディレクトリ

| 変数 | デフォルト | 用途 |
|------|-----------|------|
| `CCM_TMP_DIR` | `${TMPDIR:-/tmp}/ccm-$UID` | ユーザー単位のランタイムディレクトリ。フック信号、通知マーカー、ポート/git キャッシュ、ポップアップセッションマーカーを格納。デモやテストセッションを通常運用と分離する場合に上書きする |
| `CCM_DATA_DIR` | `~/.local/share/ccm` | スナップショットなど永続的な状態の格納先。完全に隔離した環境を作る場合は `CCM_TMP_DIR` と組で上書きする |

### カナリア閾値

| 変数 | デフォルト | 用途 |
|------|-----------|------|
| `CCM_HOOKS_LOG_WARN_BYTES` | `104857600`（100 MB） | `~/.claude/hooks.log` 肥大化カナリアのサイズ閾値。Claude Code はこのファイルをローテートせず、肥大化するとフック発火が silent fail する（anthropics/claude-code#16047） |
| `CCM_SHELL_CLUSTER_COUNT` | `3` | silent-exit カナリア (anthropics/claude-code#48069) を発動させる SHELL 遷移回数 |
| `CCM_SHELL_CLUSTER_WINDOW` | `600`（秒） | SHELL 遷移カウントの時間窓 |
| `CCM_ERRORS_BURST_THRESHOLD` | `20` | silent-fail-loop カナリアを発動させる `errors.log` 記録数。poll-cycle バグ (`inject_status` の refresh ごとに例外発生など) は約 30 records/min を記録するため、ランナウェイループと単発ノイズを確実に区別できる閾値 |
| `CCM_ERRORS_BURST_WINDOW` | `300`（秒） | silent-fail 記録カウントの時間窓 |

### デバッグトレース

| 変数 | デフォルト | 用途 |
|------|-----------|------|
| `CCM_DEBUG_TRACE` | (未設定) | JSONL トレースファイルのパス。設定すると slow-path 検出スキャン (`inject-status`、dashboard、`ccm status`) が各スキャンで `DetectionContext` 全体 + マッチルール + 解決された state を 1 行追記する。[状態検出の挙動デバッグ](#状態検出の挙動デバッグ) 参照。tmux 起動後の設定は `tmux set-environment -g CCM_DEBUG_TRACE <path>` で行う（シェルの `export` では tmux サブプロセスに届かない） |
| `CCM_TRACE_MAX_BYTES` | `104857600`（100 MB） | `CCM_DEBUG_TRACE` ログファイルのサイズ上限。超過時は `{"event":"trace_cap_reached", ...}` の sentinel 行を 1 回だけ書いて以降の追記を停止し、解除忘れでディスクを食い尽くすのを防ぐ |
| `CCM_TRACE_ONLY_DIFF` | (未設定) | truthy 値を設定すると、`CCM_DEBUG_TRACE` の書き込みを「legacy と event-log の判定が食い違った行」のみに絞る。長時間トレースを小さく保てる。`CCM_USE_EVENT_LOG=off` 時は無効（diff 対象がない） |
| `CCM_USE_EVENT_LOG` | `auto` | `auto`（デフォルト）は [`derive_state_from_events`](../lib/ccm_activity.py) が non-`None` を返したらその結果を採用、それ以外は legacy `DETECTION_RULES`（[`lib/ccm_rules.py`](../lib/ccm_rules.py)）にフォールバック。`off`（または `0` / `no` / `false`）は診断用キルスイッチで legacy 単独動作（event log の読み取りも行わない）。それ以外の値は `auto` に解決される |

### キャッシュ TTL

| 変数 | デフォルト | 用途 |
|------|-----------|------|
| `CCM_CACHE_TTL` | `30`（秒） | Git ブランチ / ポート検出キャッシュの寿命 |
| `CCM_JSONL_CACHE_TTL` | `30`（秒） | JSONL パス解決キャッシュの寿命 |

### 表示と可観測性

| 変数 | デフォルト | 用途 |
|------|-----------|------|
| `CCM_AMBIGUOUS_WIDTH` | `1` | East Asian Ambiguous 文字（IDLE アイコン `●`、SHELL アイコン `■` など）のターミナル列幅。CJK locale ターミナルで Ambiguous 文字が 2 列幅でレンダリングされる場合は `2` に設定すると、ダッシュボード / `ccm status` のカラム整列が崩れない。モジュール読み込み時に評価されるため、変更後は inject-status / dashboard を再起動 |
| `CCM_ERRORS_LOG_MAX_BYTES` | `1048576`（1 MB） | `$TMPDIR/ccm-$UID/errors.log`（silent-exception ログ）のサイズ上限。上限到達時はアクティブログを `errors.log.1` にローテーションし、新しいログを開始する（ディスク使用量は上限の約 2 倍）。`ccm errors` で表示、`ccm errors --clear` で削除 |
| `CCM_SESSION_INFO_AGE_DRIFT_SEC` | `10`（秒） | session_info の pid 再利用チェックのドリフト許容秒数。`read_session_info` が `ps` snapshot を渡されたとき、Claude Code が記録した `startedAt` と live プロセスの etime 由来の起動時刻を照合する。許容を超える乖離は「pid が再利用された旧セッションの json」と判断して reject（呼び出し側は legacy fallback へ）。10 秒は通常のクロックドリフト・NTP 補正・fork から session_info 書き込みまでの数秒をカバーする値 |

### チューニング例

```bash
# 完了後の "* elapsed" マーカー表示時間を延長
export CCM_COMPLETED_AT_TIMEOUT=60

# hooks.log 肥大化警告を早めに（10 MB）
export CCM_HOOKS_LOG_WARN_BYTES=10485760

# 低速マシンやバッテリー駆動時のポーリングコスト削減 (tmux オプション、env var ではない)
# ~/.tmux.conf に追加:  set -g status-interval 10

# 診断用 kill-switch: event-log path をバイパス
export CCM_USE_EVENT_LOG=off
```

### Claude Code 自身の環境変数との相互作用

Claude Code には ccm と機能的に重なる非公開の環境変数がいくつかあります。両方を設定する場合は挙動の重なりに注意してください:

| Claude Code env | ccm との相互作用 |
|-----------------|------------------|
| `CLAUDE_CODE_EXIT_AFTER_STOP_DELAY` | Stop イベントから指定秒後に Claude Code 自身が exit する。`CCM_IDLE_EXIT_TIMEOUT` と機能が重複するので片方に統一すべき。両方設定すると先に発火した方が勝ち、もう一方は SHELL 状態となったウィンドウで no-op になる |
| `CLAUDE_CODE_IDLE_THRESHOLD_MINUTES`, `CLAUDE_CODE_IDLE_TOKEN_THRESHOLD` | Claude Code 独自の idle 判定。発火すると SessionEnd hook が走って ccm はウィンドウを SHELL と認識する（競合はしないが意図せぬ auto-exit 経路が増える） |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | Claude Code が SessionEnd hook (ccm の `on-session-end.sh`) に与える実行時間上限。ccm のフックはシグナルファイル 1 つを書くだけなので、どんな値でも余裕で収まる |
| `CLAUDE_CODE_NO_FLICKER` | ccm 対応済。alternate screen buffer を使うペインのプレビューキャプチャで自動的に `tmux capture-pane -a` にフォールバック |
| `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` | 競合なし。Claude Code による tmux ウィンドウタイトル書き換えが嫌な場合はシェル rc で `1` に設定するとよい。ccm 側のウィンドウ名 (state アイコン) の命名はどちらの場合も優先される |
| `DISABLE_UPDATES` | 競合なし。Claude Code のすべての更新経路（手動 `claude update` 含む）をブロックする（`DISABLE_AUTOUPDATER` より厳格）。スナップショットで Claude Code のバージョンを固定したい、セッション途中での予期せぬアップグレードを避けたいユーザー向け |
| `CLAUDE_CODE_HIDE_CWD` | 競合なし。Claude Code 起動時のロゴに表示される作業ディレクトリを非表示にする。ccm は `ccm status` とダッシュボードで各プロジェクトのディレクトリを既に表示しているため、ペイン内のロゴ側は安全に非表示にして視覚的な重複を減らせる |

これらは ccm の動作に必須ではありません。Claude Code をカスタマイズしているユーザーが機能の重なりを事前に把握できるようにするための記載です。

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

#### 状態検出の挙動デバッグ

プロジェクトが意図せず BUSY / IDLE 表示になるとき、以下のライブトレーサーで原因を切り分けられます。

**プロジェクト単位のライブトレース** (読み取り専用、状態を書き換えない):

```bash
# 別ペインで実行してから、メインペインで問題の操作を再現する
ccm debug trace <project-name>           # デフォルト 0.3 秒間隔
ccm debug trace <project-name> 0.5       # 間隔指定
```

1 行につき 1 スキャンで、検出コンテキストとマッチしたルール、解決された状態を表示します:

```
19:48:55  raw=IDLE  prev=IDLE  hook=-,-  pid_age=653  jsonl=6883,end_turn  default[-] → IDLE [WRITE]
```

`rule_name[phase]` カラムはマッチしたルール名とその session-lifecycle phase (`shell` / `startup` / `midturn` / `between_tools` / `idle` / `permit`、真の catch-all passthrough (`default` 等) は `-`)。Ctrl-C で停止。ダッシュボードと並行実行しても干渉しません。

**パイプライン全体のトレース** (環境変数で有効化、全プロジェクトの全スキャンを記録):

```bash
# tmux サーバー側に設定すること。inject-status は tmux のサブプロセス
# として起動し、サーバー起動時の環境を継承するため、シェル側の export
# だけでは届きません。
tmux set-environment -g CCM_DEBUG_TRACE /tmp/ccm-trace.jsonl
# status-interval 1 回分待ってから問題を再現。
# jq で特定ウィンドウや state に絞る：
jq -c 'select(.target=="0:20")' /tmp/ccm-trace.jsonl | tail -50
jq -c 'select(.state=="BUSY")' /tmp/ccm-trace.jsonl | tail -20
# 作業が終わったら必ず解除 (ファイルが肥大化するため)。
# 100 MB で自動的に追記停止します (上限は CCM_TRACE_MAX_BYTES で変更可)。
tmux set-environment -gu CCM_DEBUG_TRACE
```

両方のトレーサーは同じフィールドを記録するため、出力は相互に読み替え可能です。`CCM_DEBUG_TRACE` は slow-path (実際に `@ccm_prev_state` に書き込む判断) のみを記録します。statusline fast-path は read-only で書き込みをしないため記録対象外です。

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
