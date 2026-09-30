# 診断参照

導入と日常の操作は[ユーザーガイド](guide.ja.md)を参照してください。ここでは状態検出の詳細、調整項目、診断手順を説明します。

## 状態検出

### Claude Codeフック（推奨）

`ccm setup-hooks` は `~/.claude/settings.json` に以下のフックを追加します。

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
> フック信号は `$TMPDIR/ccm-$UID/hooks/` に書き込まれます。BUSY は `Stop`/`SessionEnd` フックまたはプロセス終了でクリアされます。BUSY フックと JSONL の両方が `CCM_BUSY_HOOK_JSONL_WINDOW`（デフォルト10分）を超えて沈黙した場合、ccm は古い信号の信頼を打ち切り IDLE へフォールバックするため、`Stop` の取りこぼしで BUSY に張り付くことはありません。PERMIT も同様に解放されます。permission の解決時に上流はフックを発火しないため、permit イベントが最新で、ペインにモーダルが表示されておらず、セッションログが `CCM_PERMIT_MAX_TIMEOUT`（デフォルト10分）を超えて凍結している場合、ccm はその信頼を打ち切り IDLE へフォールバックします。ダイアログが画面に出ている場合はペインから直接読み取られるため、どれだけ待っていても PERMIT のままです。Esc 中断で残った BUSY（`Stop` フックが発火せず、ログがツール実行中で凍結）はより早く解放されます。画面がアイドルなプロンプトで、ログが `CCM_BUSY_STALE_RELEASE_SEC`（デフォルト60秒）を超えて凍結している場合、ccm は IDLE に委ねます。

`ccm doctor` は対処が必要な項目・検査できなかった項目・次の操作を表示します。完了した検査で問題が見つからなければ総括を1行だけ表示します。正常な検査結果、版、パス、全プロジェクト、session ID、Codex の結び付け、ログ件数は `ccm doctor --verbose` で確認できます。どちらも同じ検査を行い、診断記録を保持します。

ccm が `~/.claude/settings.json` で編集するのは自分のフックのエントリだけです。ccm のフックとは、コマンドが ccm のフックスクリプトへのパスそのもので、そのディレクトリがこの ccm の `hooks/` であるものです（symlink 経由の表記や、大文字小文字を区別しないファイルシステム上での大文字小文字違いの表記も含みます）。他ツールのフックは、ccm のフックと同じ matcher エントリにあるものも含めてそのまま残します。ccm のスクリプトと名前が同じそれ以外のフックも、移動・削除した ccm のものを含めてすべて残します。設定だけからは他ツールのフックと区別できないためです。`ccm setup-hooks`、`ccm remove-hooks`、`ccm doctor --verbose` がそれらの名前を挙げるので（doctor --verbose はディレクトリごとの登録件数も示します）、古い ccm の残りだと分かっているものだけを手で削除してください。`ccm doctor` は、設定を読み取れて、この ccm の `hooks/` にあるフックが 1 つもないと分かった場合、インストール済みとは表示せずにその旨を示します。すべてのイベントが登録されているかまでは確認しません。

### 各状態の検出方法

ペインがバックグラウンドへ預けた会話を表示している場合、ccm は Claude Code の
セッション登録簿の `parkedJobId` をたどり、預け先のフックとトランスクリプトを読みます。
agents 画面を経由して預け、会話へ戻った場合も対象です。預け先を特定できないときは、
まず BUSY（ダイアログが見える場合は PERMIT）を維持します。入力待ちの継続観測で、
別会話の完了を流用せずに解放できます。許容する観測間隔は `CCM_RECONCILE_INTERVAL`
と tmux の `status-interval` に合わせます。既定ではダッシュボードの2秒周期なら62秒、
ステータスバーのみの20秒周期なら80秒で解放します。設定した周期が長い場合も機能しますが、
1回の観測で加算する証拠時間は `CCM_BUSY_STALE_RELEASE_SEC`（既定60秒）の半分までとし、
一度長く待っただけでは解放しません。作業中表示、ダイアログ、identity や時計の変化、
解放後の新しい状態通知、設定に基づく許容値を超えた観測中断では計り直します。
hand-off の起動警告は SHELL ウィンドウだけが対象で、すでに開いている会話には表示しません。

応答の後ろにある巨大な補助レコードは活動に数えません。トランスクリプトを後ろから
探索し、変更されたファイルごとに最大 1 MiB・200 レコードまで読みます。この範囲内なら
添付や履歴スナップショットの前にある最後の応答も参照できます。未変更のファイルは
キャッシュを再利用します。

`Stop` 後の終了理由が読めない、または不明な場合、入力待ちの画面で、Stop と読めた活動の
両方が `CCM_BUSY_STALE_RELEASE_SEC`（既定 60 秒）を超えると BUSY を解放できます。
ツール実行待ちや新規プロンプトの進行が確認できる場合は BUSY を維持します。
画面上の作業中表示や許可ダイアログも解放を防ぎます。auto-exit は引き続き、既定で
600 秒間の IDLE 継続を必要とします。


| 状態 | 検出方法 | 詳細 |
|------|----------|------|
| **SHELL** | プロセスチェック | ウィンドウの子プロセスに `claude` が見つからない |
| **IGNORED** | ペインオプション + プロセスチェック | `claude` をホストするペインが全て `@ccm_ignore` で除外され、可視ペインのどれもホストしていない状態 — ccm は意図的にその Claude を見ていないため、SHELL/DOWN（「Claude は起動していない」）は根拠のない主張になる。PERMIT > BUSY > IDLE > SHELL の梯子の段ではなく、SHELL/DOWN の主張に先立って判定される可視性の結論。`⊘`（dim）で表示。`ccm send` は拒否され `ccm unignore` を案内。auto-exit は反応しない（IDLE にのみ作用） |
| **BUSY** | event-log + JSONL stop_reason | 主経路: BUSY 系フック (`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`SubagentStart`/`Stop`、`PreCompact`/`PostCompact`) が追記する per-session event log (`hooks/<sessionId>.events.jsonl`、Claude Code の session UUID でキー)。`derive_state_from_events` が tail を純関数で評価し、最新エントリが start-class なら BUSY を返す。フック沈黙時は JSONL `stop_reason` で橋渡し: 直近の `tool_use` ならツールターン境界の Stop を超えて BUSY を維持、`end_turn` / `max_tokens` / `stop_sequence` が最新 event より新しければ数秒以内に IDLE へ release。Claude Code housekeeping レコード（`system/away_summary`、`turn_duration`、`attachment/task_reminder`、`permission-mode`、`file-history-snapshot`、`last-prompt`）は JSONL 活動から除外され、recap や起動時 housekeeping が偽の活動として検出されない |
| **IDLE** | event-log + capture-pane | event log の最新エントリが end-class（`stop` / `notify_idle` / `notify_permit` 解消後）で、入力プロンプト `❯ ` が表示されており、PERMIT フッターにマッチしない状態。フックなし時は legacy fallback がプロセスツリー + プロンプト可視性のみで判定 |
| **PERMIT** | フック + capture-pane フォールバック | 主経路: `PermissionRequest` / `PermissionDenied` / `Notification`（permission_prompt）フック。フォールバック: モーダルフッター（permission ダイアログの `Esc to cancel · Tab to amend`、confirmation modal の `Enter to confirm · Esc to <verb>` — v2.1.144 の `/model` 形式 `Enter to confirm · d to set as default for new sessions · Esc to cancel` のような中間 `· <action key>` セグメントも許容）をペインから直接検出 — フックが途中で停止したセッションでも捕捉可能 Enter より前の調整・切替ヒントは、確認できた確認ダイアログの形を列挙して対応する方式です。`/autocompact`・`/effort` の `←/→ to adjust` と `/fast` の `Space to toggle` に対応します。前2つは2.1.280で実測し、利用可能な `/fast` の形はバンドル由来で実画面は未取得です。未登録の先頭ヒントは検出できないという限界があります。`Enter to select` を含む形も含め、`/permissions` のナビゲーション用フッターは PERMIT に含めません。対応形式と検証範囲は[状態機械の仕様](state-machine.md)を参照してください。 |
| **完了（`* elapsed`）** | 表示レイヤー | 一時的マーカー: BUSY/PERMIT → IDLE遷移後に30秒間表示、その後クリア。アスタリスクは緑（直近の完了に視線を誘導）、経過時間は dim |
| **マルチペイン（`[N]`）** | ウィンドウ検査 | tmux ペインを 2 つ以上含むウィンドウに対し、全レンダラー（dashboard / status bar / `ccm status`）でプロジェクト名直後に表示。角括弧 dim、数字 cyan。集約状態が非アクティブペインのものである可能性をユーザーが認識できるようにする。詳細（sliver 保護と PERMIT 自動フォーカス）は[Agent Teamsとの併用](guide.ja.md#agent-teamsとの併用)を参照 |
| **Permission mode（`{mode}`）** | Hook payload | Claude Code が hook payload に付与する `permission_mode` field 由来の表示専用バッジ。最新値を `ccm status` の MODE 列と、ダッシュボードのプロジェクト名直後の `{mode}` バッジ（`{accept}` / `{plan}` / `{auto}` / `{dontAsk}` / `{bypass}`）として表示。日常デフォルトの `manual` はダッシュボードでは非表示。`{bypass}` は警告色。ダイアログを自動解決するモードでは PERMIT がそもそも発生しないため、その沈黙を検出不具合と誤診しないためのバッジ。状態検出はこの値を一切参照しない |
| **無視（`⊘`）** | ペインオプション | ウィンドウに `CCM_IGNORE` されたペイン（ccm が意図的に追跡しないセッション）があるとき、ダッシュボード / `ccm status` の行に dim `⊘` を表示（[別モデルをサイドキックとして使う](guide.ja.md#別モデルをサイドキックとして使うccm_ignore)参照）。存在するが未追跡、という印であって状態ではない |

## トラブルシューティング

### フックを停止する設定

`ccm doctor` と `ccm status` は、`allowManagedHooksOnly` によって ccm の
user-scope フックが止まる場合、出所の `managed-settings.json` を表示します。
Claude Code 2.1.282 では `true` と `"true"` がロックを有効にし、`false`、
`"false"`、`null`、鍵の未指定は有効にしません。それ以外の真偽値でない値
（`0`、`1`、`"yes"`、空文字列、配列、オブジェクトを含む）は、修正されるまで
ロックを有効にします。警告では真偽値でない値とリテラルの `true` を区別します。

`disableAllHooks` は例外で、Claude Code 2.1.282 はこの鍵の真偽値でない値を
無視します。ccm は管理者設定の不正な値を修正できるよう表示し、フックが
停止したとは断定しません。リテラルの `true` は設定されたフックとカスタム
statusLine を停止します。管理者に不正な値の修正を依頼し、適用中のポリシーは
Claude Code の `/status` で確認してください。ccm が読むのは管理者設定ファイルで、
MDM やコンソールのポリシーは読みません。user / project ファイルの判定は従来どおりで、
`disableAllHooks` はリテラルの `true` のみを検出し、`allowManagedHooksOnly` は管理者設定のみが対象です。

### 検出が反応しなくなった（hook 沈黙カナリア）

Claude Code はセッションの途中でフックの発火を止めることがあります。ccm の精密な
検出はフックに依存しているため、止まると粗い信号にフォールバックし、状態の反映が
遅れたり固まったりします。原因は upstream 側で ccm ではありませんが、外から見ると
両者は区別がつきません。

opt-in のカナリアがその違いを報告します:

```tmux
set -g @ccm-hook-silence on
```

セッションの transcript には最近の活動があるのに、フックのログがそれを記録して
いない場合——つまりフックが眠っている間に進んだ作業がある場合——に、`ccm status`・
`ccm doctor`・ダッシュボードのフッターで警告します。既定は off です。閾値の調整を
誤っても、監視を自分から選んだ人しか誤解しないためです。

閾値は `CCM_HOOK_SILENCE_FRESH`（transcript の活動がどれだけ新しければよいか、
既定 90 秒）と `CCM_HOOK_SILENCE_GAP`（フックログがどれだけ遅れていれば警告するか、
既定 120 秒）です。発火のたびに `~/.local/share/ccm/state/hook-silence.log` へ
1 行記録され（プロジェクトごとにレート制限）、`ccm doctor --verbose` が件数を表示します。

該当セッションの Claude を再起動すればフックは復帰します。

### 全プロジェクトが同じ状態で固まる

**すべて**のプロジェクトが同じ状態（例: 全て BUSY）で固まり、リフレッシュしても更新されない場合、検出サイクル自体が silent な例外を踏んでいる可能性があります。ログを確認:

```bash
ccm errors
```

各行は捕捉された例外（タイムスタンプ、スコープ、トレースバック付き）です。空（`No silent-caught errors logged.`）であれば検出サイクルは正常です。エントリが蓄積している場合、最新のトレースバックが失敗箇所を示します。`ccm errors --clear` でアクティブログとローテートされた `errors.log.1` の両方を削除できます。

## agent view（バックグラウンドセッション）との併用

### データソース

リーダーは daemon が書き出す 2 つのファイルを結合します（ccm 側は read-only）:

- `~/.claude/daemon/roster.json` — 現在アクティブなワーカー（pid / sessionId / cwd / cliVersion / dispatch メタデータ）。idle 1 時間程度で `settled (done)` となり roster から外れるため、ccm が表示する範囲は `claude agents` 自身が表示するものと一致します。
- `~/.claude/jobs/<short>/state.json` — セッションごとのライブ状態（`working` / `blocked` / `done` / `failed` / `stopped`。以前のリリースの `needs_input` / `idle` も引き続き読みます）、blocked 時の要求内容（`needs`）、tempo、進行中タスク数、自動生成された name。

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
| `CCM_PERMIT_MAX_TIMEOUT` | `600`（秒） | stale PERMIT の解放: permit イベントが最新で、かつペインにモーダルが**表示されていない**状態でセッションログがこの秒数以上凍結していたら、その permit の信頼を打ち切り IDLE へフォールバックする。permission の解決時に上流はフックを発火しないため、これがないと数分前に承認（または Esc で解除）した permission が `⚠ PERMIT` を無期限に保持しうる。モーダルが**画面に出ている**場合はペイン自体から検出されるため、どれだけ長く放置しても解放されない |
| `CCM_BUSY_STALE_RELEASE_SEC` | `60`（秒） | stale BUSY の解放: Esc でツール実行を中断したセッション（`Stop` フックが発火せず、セッションログが `tool_use` で凍結し、画面はアイドルなプロンプト）が BUSY のまま残る時間。ちらつき防止の窓であり、ログが凍結する実稼働セッション（長時間の無言 build 等）を誤って殺さない安全網は `CCM_IDLE_EXIT_TIMEOUT`（10分の IDLE 持続が必要）側にある |
| `CCM_SPINNER_STALE_RELEASE_SEC` | `30`（秒） | スピナーの経過時間フッターが静止したまま「稼働中」と信じられ続ける時間。生きたフッターは毎秒 tick するが、凍結フレーム（描画後にハングしたセッション）、トランスクリプト中の引用、そして Claude Code のアニメーション低減設定下のフッターは静止しており、この窓を超えるとウィンドウを BUSY に保つ主張を降ろす — raw の BUSY には他に解放経路がないため重要 |
| `CCM_THINKING_HINT_RESAMPLE_SEC` | `0.5`（秒） | ペインのスピナーフッターが思考ヒントだけを出して経過時間を出していないとき（狭いペインでの長い思考中）、ccm が追加で 2 回撮り直すキャプチャの間隔。Claude Code はスピナーの記号を 2 秒周期の cos 関数で回すので、0.5 秒離れた 2 枚が同じフレームになることはあり得るが、0.5 秒後と 1 秒後の両方が最初と一致することはなく、そのために 2 回撮る。1 回の検出の中で記号が動いたことがそのターンを稼働中と見なす根拠になる（検出の周期は 2 秒の整数倍になり得るので、検出をまたいだ比較では同じ位相ばかりを読んでしまう。この間隔を 2 秒の倍数にしても同じことが起きる）。凍結したフレームや、Claude Code のアニメーション低減設定で描かれる固定の `●` は動かないので、そのペインは idle と読まれる |
| `CCM_IDLE_EXIT_TIMEOUT` | `600`（秒） | Claude Code セッションが IDLE 状態でいられる最大時間（`x` 一括終了の対象となる閾値、自動終了のトリガー） |
| `CCM_IDLE_PROMPT_GUARD_SEC` | `60`（秒） | `on-notification.sh` の idle_prompt ガード。idle_prompt は 10〜60 秒以上遅れて届く（anthropics/claude-code#5186）ため、この秒数より新しい BUSY シグナルは通知の生成「後」に開始された作業によるものである可能性があり、削除すると稼働中セッションが IDLE に落ちる（auto-exit の kill path にも乗る）。ガードより新しいシグナルは保持し、古いものは従来通り削除する。`0` で opt-out して旧挙動（常に削除）に戻る |
| `CCM_IGNORE` | 未設定 | チューニング値ではなく起動時フラグ: `CCM_IGNORE=1 claude` で ccm が完全に無視するセッションを起動（[別モデルをサイドキックとして使う](guide.ja.md#別モデルをサイドキックとして使うccm_ignore)参照）。稼働中のセッションは `ccm ignore` / `ccm unignore` でトグル |
| `CCM_STARTUP_GRACE_SEC` | `60`（秒） | legacy `startup_transient_raw_busy` ルールが hook signal 未着の raw=BUSY を IDLE に降格させる claude プロセス年齢の窓。`claude --continue` 起動時の MCP ロード (通常 10-30 秒) をカバー |
| `CCM_SLIVER_HEIGHT_THRESHOLD` | `4`（行） | ウィンドウの状態集約に参加する tmux ペインの最小高さ。これより小さいペインは Claude の `❯` プロンプトを描画できず、capture-pane 検出が「子プロセスあり + プロンプト不可視」で BUSY と誤判定するため除外する。Agent Teams で意図的に小さいペインを使っており除外したくない場合は上げる、フィルタを完全無効化したい場合は 1 まで下げる |
| `CCM_HOOK_CMD_TIMEOUT` | `5`（秒） | Claude Code が ccm の各フック呼び出しに与えるタイムアウト。`ccm setup-hooks` がフックのエントリに書き込む。このフィールドの単位は**秒**（Claude Code 自身の既定は 60）。以前の ccm はミリ秒のつもりで `5000` を書いており、Claude Code はこれを 5000 秒と読んでいた。既存のインストールに対して `ccm setup-hooks` を実行すると、ccm 自身のフックコマンドの timeout だけをこの値に揃え、それ以外は変更しない（同じ matcher エントリにある他ツールのフックもそのまま）。古い値のままのインストールは `ccm doctor` が名指しする。ここに設定した値はそのまま書き込まれるので、ミリ秒で設定していた場合は実行前に秒へ直すか解除すること |
| `CCM_SPOOL_TTL_SEC` | `3600`（秒） | `ccm send` がキューした（store-and-forward）メッセージが配送可能でいられる時間。TTL を超えると配送の代わりに `expired/` に移され、`ccm status` / `ccm doctor` に表示される — 遅れて届いた陳腐な指示は文脈から外れた実行になるため。[スプール](guide.ja.md#スプールstore-and-forward)参照 |
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
| `CCM_SHELL_CLUSTER_COUNT` | `3` | cluster-SHELL 警告を出す、窓内の SHELL への遷移回数。警告は観測した事実 — Claude が繰り返しペインから居なくなった — を伝え、ここからは見分けのつかない原因（アップデートによるその場での再起動、手動終了、予期しない終了）を並べる。anthropics/claude-code#48069（macOS の silent exit）は予期しない終了の既知の一因として挙げるもので、診断ではない |
| `CCM_SHELL_CLUSTER_WINDOW` | `600`（秒） | SHELL 遷移カウントの時間窓 |
| `CCM_ERRORS_BURST_THRESHOLD` | `20` | silent-fail-loop カナリアを発動させる `errors.log` 記録数。poll-cycle バグ (`inject_status` の refresh ごとに例外発生など) は約 30 records/min を記録するため、ランナウェイループと単発ノイズを確実に区別できる閾値 |
| `CCM_HOOK_SILENCE_FRESH` | `90`（秒） | opt-in の hook 沈黙カナリア: セッションの transcript の活動がこれより新しいときに、遅れたフックログを沈黙とみなす |
| `CCM_HOOK_SILENCE_GAP` | `120`（秒） | フックログがその活動からこれ以上遅れていたらカナリアが発火する |
| `CCM_HOOK_SILENCE_LOG_INTERVAL` | `600`（秒） | 1 プロジェクトあたりの記録間隔の下限。長い沈黙が数百行でなく数行として読める |
| `CCM_HOOK_SILENCE_LOG_MAX_BYTES` | `1048576`（1 MB） | 発火ログのローテーション上限 |
| `CCM_ERRORS_BURST_WINDOW` | `300`（秒） | silent-fail 記録カウントの時間窓 |

### デバッグトレース

| 変数 | デフォルト | 用途 |
|------|-----------|------|
| `CCM_DEBUG_TRACE` | (未設定) | JSONL トレースファイルのパス。設定すると slow-path 検出スキャン (`inject-status`、dashboard、`ccm status`) が各スキャンで `DetectionContext` 全体 + マッチルール + 解決された state を 1 行追記する。[状態検出の挙動デバッグ](#状態検出の挙動デバッグ) 参照。tmux 起動後の設定は `tmux set-environment -g CCM_DEBUG_TRACE <path>` で行う（シェルの `export` では tmux サブプロセスに届かない） |
| `CCM_SEND_TRACE` | (未設定) | 真値のとき、`ccm send` / `ccm sidekick-send` が行う `tmux send-keys` を全て `$CCM_TMP_DIR/send-trace.log` に追記する。「届いていない」と言われた配送の切り分け用 |
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
| `CCM_ATTENTION_WAITING_TTL_SEC` | `3600`（秒） | サイドキックの未応答マーカーを ccm が破棄するまでの時間。エージェントが解決せずに終了した場合に効く |
| `CCM_ATTENTION_RESOLVED_GC_SEC` | `300`（秒） | 解決済みマーカーを回収するまでの保持時間。読み取りの遅い消費側でも見えるようにする |
| `CCM_AMBIGUOUS_WIDTH` | `1` | East Asian Ambiguous 文字（IDLE アイコン `●`、SHELL アイコン `■` など）のターミナル列幅。CJK locale ターミナルで Ambiguous 文字が 2 列幅でレンダリングされる場合は `2` に設定すると、ダッシュボード / `ccm status` のカラム整列が崩れない。**`@ccm-ambiguous-width` の退避経路**（tmux の外で ccm を実行する場合用）。環境変数はバーを描画する 2 つの親の片方にしか届かないため、tmux オプションを優先すること — [曖昧幅グリフ](#曖昧幅グリフ)参照。プロセスごとに 1 回評価 |
| `CCM_ERRORS_LOG_MAX_BYTES` | `1048576`（1 MB） | `$TMPDIR/ccm-$UID/errors.log`（silent-exception ログ）のサイズ上限。上限到達時はアクティブログを `errors.log.1` にローテーションし、新しいログを開始する（ディスク使用量は上限の約 2 倍）。`ccm errors` で表示、`ccm errors --clear` で削除 |
| `CCM_SESSION_INFO_AGE_DRIFT_SEC` | `10`（秒） | session_info の pid 再利用チェックのドリフト許容秒数。`read_session_info` が `ps` snapshot を渡されたとき、Claude Code が記録した `startedAt` と live プロセスの etime 由来の起動時刻を照合する。許容を超える乖離は「pid が再利用された旧セッションの json」と判断して reject（呼び出し側は legacy fallback へ）。10 秒は通常のクロックドリフト・NTP 補正・fork から session_info 書き込みまでの数秒をカバーする値 |
| `CCM_STATUS_INTERVAL` | `5`（秒） | tmux `status-interval` の目標値 — ステータスバーの再描画間隔。プラグインはロード時に、現在の設定がこの値より大きい場合のみ引き下げる（引き上げはしない）。shell の `export` ではなく `tmux set-environment -g` でプラグインのロード前に設定 — [ステータス更新間隔](#ステータス更新間隔)参照 |
| `CCM_RECONCILE_INTERVAL` | `20`（秒） | 定期ステータス更新が**フル実行**する間隔。tmux は `#(ccm inject-status)` を `status-interval` ごとに起動し（秒表示の時計を出していれば毎秒）、フル実行は約24プロセスを要するため、その間の呼び出しはシェルの fork だけに抑えられる。状態変化はこれを待たない — フックが遷移のたびに即時更新を push する。ここで律速されるのはフックが発火しないもの（git ブランチ切替、新しいリスニングポート、stale-BUSY の窓跨ぎ）のみ。`CCM_BUSY_STALE_RELEASE_SEC` より小さく保つこと（その解放はイベントではなく閾値跨ぎで、reconciliation が走るまで誰も再評価しないため） |
| `CCM_RESIZE_SETTLE` | `0.4`（秒） | 最後の `client-resized` イベントからステータスバーを再配置するまでの待ち時間。バーのレイアウトは描画時の端末幅で焼き込まれるため、リサイズ後は何かが再描画するまで古い幅のままになる。tmux はドラッグの 1 ステップごとにこのイベントを発火するので、この窓でまとめて 1 回だけ、ドラッグが終わったサイズで描画する。ドラッグ途中で再配置されてしまう場合は大きくする |
| `CCM_AUTO_EXIT_LOG` | `$CCM_DATA_DIR/state/auto-exit.log` | ccm 自身が終了させたセッションの記録先。Claude Code は auto-exit を `SessionEnd` の reason `prompt_input_exit` として報告するが、これは人間が `/exit` を打った場合とまったく同じ値なので、両者を事後に区別できるのはこのファイルだけ。1 回の終了につき JSON 1 行（時刻・プロジェクト・session id・idle 秒）。`ccm doctor --verbose` が件数を表示 |
| `CCM_AUTO_EXIT_LOG_MAX_BYTES` | `1048576`（1 MB） | auto-exit ログのサイズ上限。到達時は `auto-exit.log.1` にローテーション。1 終了につき 1 行なので上限到達は事実上ありえず、暴走ループがディスクを埋めないための保険 |
| `CCM_AUTO_EXIT_DECLINED_LOG` | `$CCM_DATA_DIR/state/auto-exit-declined.log` | auto-exit が見送った・確認できなかった終了の記録先: ペインが agent view を表示していた（何も打たない）、ペインを読めなかった（何も打たない）、attach 中のバックグラウンドセッションに `/exit` が入り終了ではなく detach になった。1 件につき JSON 1 行（時刻・プロジェクト・session id・結果種別。ペインの内容は書かない）。`auto-exit.log` とは分けてあり、あちらは本当の終了だけを数え続ける。件数は `ccm doctor --verbose` に出る |
| `CCM_AUTO_EXIT_DECLINED_LOG_INTERVAL` | `600`（秒） | 見送りログのレート制限（プロジェクト × 結果種別ごと）。同じ状態が続く窓は poll ごとではなく、この間隔に 1 回だけ記録される |

**すべての読み手に届けること。** ccm は環境の異なる 3 か所から起動されます — tmux の `#()`、Claude Code が spawn するフック、そしてあなたのシェルです。シェルだけで設定した変数は `ccm status` とダッシュボードには読まれますがステータスバーには届かず、同じウィンドウが 2 通りに説明されることになります。状態の計算や描画に影響する値は、シェルの設定ファイルではなく tmux の環境（`tmux set-environment -g NAME value` のあとバーを再起動）に置いてください。tmux オプションが用意されているものは、そちらを使う方が確実です。

### チューニング例

```bash
# 完了後の "* elapsed" マーカー表示時間を延長
export CCM_COMPLETED_AT_TIMEOUT=60

# hooks.log 肥大化警告を早めに（10 MB）
export CCM_HOOKS_LOG_WARN_BYTES=10485760

# 低速マシンやバッテリー駆動時のポーリングコスト削減 (tmux 環境変数、プラグインのロード時に読まれる)
tmux set-environment -g CCM_STATUS_INTERVAL 10

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

### ステータス更新間隔

ccmのステータスバー更新はtmuxの `status-interval` に駆動されます。プラグインはロード時に、現在の設定がtmuxデフォルト（15秒）のままなど 5 秒より大きい場合、自動的に 5 秒へ引き下げます（値を下げるだけで、上げることはありません）。別の間隔にしたい場合は、プラグインのロード前にtmux環境変数 `CCM_STATUS_INTERVAL` を設定してください：

```bash
tmux set-environment -g CCM_STATUS_INTERVAL 10   # 10秒ごとのポーリングに変更
```

値を下げるとCPU使用量がわずかに増加します。

### 狭いペイン・長い思考・アニメーション低減

Claude Code のスピナーフッターは、経過時間と思考ヒントの両方が入らない狭さのペインでは経過時間のほうを落とし、思考が 45 秒を超えるとヒントが長くなります（`deep in thought`）。経過時間は ccm がターン稼働中の印として読むものなので、無いときは 0.5 秒離れた 2 枚のキャプチャでスピナー記号が動いたかを見ます。Claude Code のアニメーション低減設定では記号が固定の `●` になり、そのペインには動くものがありません。経過時間も当てになりません。この設定では定期的な更新が止まることがあり、表示されている時間は稼働の証拠になりません（2.1.278 で実測。作業中に 30 秒以上同じ時間のまま。アニメーションありでは毎秒動いていました。ウィンドウのリサイズなど別の再描画が起きれば追いつきます）。ccm は `CCM_SPINNER_STALE_RELEASE_SEC` の間動かない時計を静止とみなすので、この読み取り（画面から読むフォールバック）は、時間が表示されていても、その時間を過ぎると idle になります。こうしたセッションを支えるのはフックで、フックが発火している間はそちらの判断で BUSY を保ちます。フックがセッションを BUSY に保つのはその窓の間だけで、十分長く続いた誤 idle は auto-exit が動く根拠になります。思考が idle timeout より長く続けば、セッションが終了され得ます。auto-exit の設定はグローバルに 1 つ（`@ccm-idle-timeout`、分単位、`0` で無効）です。ペインを広げれば狭いペインの問題は解決しますが、アニメーション低減には効きません（幅に関係なく時間が止まるため）。この場合はフックを入れたままにし、timeout を延ばすか `0` にしてください。

### 曖昧幅グリフ

罫線素片、幾何学記号、そして Nerd Font のアイコンは、端末とフォント次第で 1 列にも 2 列にも描かれます。Unicode はこれらを "Ambiguous" と呼ぶだけで、どちらかは規定しません。私用領域の文字にいたっては原理的に規定できません（コードポイントは幅を持たず、フォントだけが知っています）。

分からないことのコストは、モード 1 の左配置で顕在化します。tmux はこれらを 1 列として `status-right` の位置を決めるため、端末が 2 列で描くと `status-left` が tmux の想定を越えて伸び、先頭のエントリを上書きします。左配置がまさに見せようとしている最優先のエントリです。そこで ccm は既定で、広い側を見込んで場所を予約します。

狭く描く端末では、この予約は使われません。そして左配置では、使われなかった列が `status-left` の後の空白としてそのまま画面に残ります。こうしたグリフを多用するテーマで約 12 列です。

端末の挙動が分かっているなら、それを伝えれば予約は止まります。

```bash
tmux set -g @ccm-ambiguous-width 1   # 狭く描く（多くの非 CJK 端末）
tmux set -g @ccm-ambiguous-width 2   # 広く描く（CJK locale の端末）
```

`1` を設定することと未設定のままにすることは、グリフを 1 列として数える点では同じですが、意味が違います。未設定は「不明」で ccm は予約し、`1` は「狭い」で予約をやめます。

反映は次の描画からです。描画のたびに 1 回だけ値を解決して保持するため、既に画面に出ているバーは以前の値で描かれています。効いたかどうかは 1 ティック待ってから判断してください。`source-file` の直後はさらに長く待つ必要があります — reload でバーはいったんテーマ素の `status-right` に戻り、ccm が再注入するまで 最大 `CCM_RECONCILE_INTERVAL` かかります。reload 直後に見えるものは「新しい設定が効いていない」のではなく「ccm がまだ描いていない」状態です。

環境変数 `CCM_AMBIGUOUS_WIDTH` ではなく tmux オプションを使ってください。環境変数は tmux の外で ccm を実行する場合の退避経路としてのみ残しています。ステータスバーは **2 つの異なる親**から描画されます — tmux の `#()` と、Claude Code が spawn するフックです。`tmux set-environment` で設定した値は前者にしか届かず、しかもフック経由の描画は rate-limit されない側なので、宣言はほとんど上書きされ、バーが 2 つのレイアウトの間で点滅します。tmux オプションは誰が尋ねても同じ値を返します。両方設定されている場合はオプションが優先されます。

自分の端末がどちらか調べるには、罫線素片と普通の文字を並べて列が揃うか見てください。あるいは単に `1` を設定してみて、先頭エントリが 1 文字欠けるようなら `2` にしてください。

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

trace は読み取り専用で、保存された unresolved の入力待ち履歴を進めません。
`idle_credit=保存済み->候補` は保存済みの証拠時間と、その時点で完全判定を確定した場合の
値を示します。`gap` は許容観測間隔です。確定値の進行を見るには、定期ステータス判定か
ダッシュボードを動かしておきます。trace だけでは、それらの判定周期の代わりになりません。


対象には ccm が管理していない tmux のペイン / ウィンドウ / `session:index` も
指定できます。実験用に一時的に立てたセッションを観察するときはこの形を使います:

```bash
ccm debug trace %42                      # ペイン
ccm debug trace @7                       # ウィンドウ
ccm debug trace probe:0                  # session:index
```

フックのイベントログを手作業で読むより、こちらを使ってください。イベントログは
Claude Code の session id をキーにして分かれているため、
`$TMPDIR/ccm-$UID/hooks/*.events.jsonl` のようなグロブで数えると、その時点で
動いている全セッションの分が合算され、別セッションのイベントを調査対象のものと
取り違えます。ペインをトレースすれば観察は 1 セッションに限定されます。登録済み
プロジェクト名は常に tmux ターゲットより優先されるので、既存のコマンドの意味は
変わりません。

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

## スナップショットの保存点

保存形式 v2 は v1 の `projects` と `name` / `dir` / `auto_start_claude` を維持し、各窓の `restore` に非ズーム時の layout、幅・高さ、窓順序、ズーム、ペインの slot・cwd・役割・agent 種別・ignore 意図、アクティブ slot と主 Claude slot を記録します。layout 内の数値 ID と `layout_id` は保存した配置を対応付けるための値で、再起動後のペイン識別子ではありません。

役割は保存時の観測です。明示的な ignore はプロセスがシェルでも記録します。非 ignore の Claude が一意に観測できた場合だけ主ペインとし、曖昧なら主 slot は null にします。シェルから過去の agent を推測しません。`checkpoint` は収集日時（トップレベル `created`）、単一セッションの範囲、完全収集印、`sealed`、中断したプロジェクトの名前と PERMIT／BUSY の別を持ちます。中断一覧は `prepare-logout` 時に検出を読み直し、通常保存では直近の検出状態を記録します。画面本文、入力途中の文、承認内容、コマンド引数、環境変数、会話 ID は保存しません。cwd とプロジェクト名は保存されます。

全書き込みは共通ロック下で収集し、前後の窓・ペイン一覧を照合します。構成変更・収集失敗・空の状態では既存の保存点を残します。一時ファイルを再読込し、fsync と原子的置換で保存します。保護中は全自動保存経路と手動の `_autosave` 保存・削除を拒否します。`prepare-logout` の再実行は保護した保存点を明示的に置き換え、`--cancel` は内容を残して保護を解除します。構成の保存であり、作業中プロセスの停止や承認の引き継ぎは行いません。

固定予備は `_autosave.prev` の 1 つです。正常に置き換えた直前の検証済み保存点を持ち、同内容・空・失敗時や保護解除では更新しません。`snapshot list` には出ません。ファイルは 0600、保存ディレクトリは 0700 です。書き込み途中の `.snapshot-transaction` が残った場合、次の書き込みはロック下で保存点と予備を元に戻してから進みます。復旧自体も I/O エラーになる場合は非ゼロ終了し、取引記録を残します。ディスクの問題を解消して再実行してください。`ccm doctor --verbose` は保護状態・中断一覧・予備の有無を読み取り、保留中の取引や読めない保存点は通常の doctor でも報告します。

予備を使う場合は保存ディレクトリ（既定 `~/.local/share/ccm/snapshots`、`CCM_SNAPSHOT_DIR` を優先、なければ `CCM_DATA_DIR` 配下）を開き、`_autosave.prev` を未使用の名前 `recovery.json` にコピーします。コピーはモード 0600 にし、`ccm start recovery` でプロジェクト窓を読み込めます。現在のロードは各プロジェクトにシェル窓を作り、分割・役割・ズームは適用しません。保護した `_autosave` はロード後も保持されます。

v1 は従来どおり読み、未知の版は窓を変更する前に拒否します。**旧版 ccm は v2 の追加項目を無視してロードし、その後の autosave で v1 に書き換え得ます。旧版は sealed も尊重しません。** ダウングレード前に保存点と予備を別の場所へコピーしてください。既存の旧版プロセスも終了してから新版の保護を使ってください。保護は新版 ccm の書き込み経路に対するもので、外部のファイル編集や同期を制御しません。設定した保存先が同期対象なら、この情報も同期されます。
