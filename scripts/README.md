# Screenshot Scripts

README用のスクリーンショットを撮影するためのツール群。

## ファイル

| ファイル | 用途 |
|---------|------|
| `setup-screenshot.sh` | モック環境のセットアップ（隔離tmuxサーバー + 架空プロジェクト） |
| `capture-screenshot.sh` | freeze によるSVG/PNG自動生成（フォールバック用） |
| `generate-svg.py` | 手作りSVG生成（フォールバック用） |

## 撮影手順

### 前提

- Ghostty（または任意のターミナル）
- tmux外の新しいウィンドウで作業すること

### 1. モック環境セットアップ

```bash
cd /path/to/ccm
tmux -L ccm-ss kill-server 2>/dev/null
./scripts/setup-screenshot.sh
tmux -L ccm-ss attach -t work
```

### 2. セッション内の準備

```bash
export CCM_MOCK_STATE=1
printf '\033]0;work\007'    # ペインタイトルを上書き
```

### 3. ダッシュボード撮影

```bash
CCM_MOCK_STATE=1 ccm dashboard
```

- Cmd+Shift+4 で撮影
- `q` で閉じる

### 4. ステータスバー撮影（モード0〜2）

```bash
# モード0（status-right にサマリーアイコン）
tmux set -g @ccm-status-line 0 && ccm inject-status
# → Cmd+Shift+4

# モード1（ウィンドウリストをccm形式に置換）
tmux set -g @ccm-status-line 1 && ccm inject-status
# → Cmd+Shift+4

# モード2（専用ステータス行を追加）
tmux set -g @ccm-status-line 2 && ccm inject-status
# → Cmd+Shift+4
```

### 5. クリーンアップ

```bash
# Ctrl+b d でデタッチ後:
tmux -L ccm-ss kill-server
```

### 6. 画像配置

撮影した画像を `assets/` にコピー:

```bash
cp ~/Desktop/dashboard.png assets/
cp ~/Desktop/statusbar-mode-0.png assets/statusbar-mode0.png
cp ~/Desktop/statusbar-mode-1.png assets/statusbar-mode1.png
cp ~/Desktop/statusbar-mode-2.png assets/statusbar-mode2.png
```

## モック環境の仕組み

- `tmux -L ccm-ss` で隔離されたtmuxサーバーを使用
- `@ccm-mock-state 1` tmuxオプションで `inject-status` の実プロセス検出をバイパス
- `CCM_MOCK_STATE=1` 環境変数で `ccm dashboard` の状態検出もバイパス
- `@ccm_prev_state` ウィンドウオプションに設定した値がそのまま表示される
- 6つの架空プロジェクトが各状態（PERMIT, BUSY, IDLE, IDLE+完了マーカー, IDLE, SHELL）で作成される

## Ghostty Tips

- ウィンドウパディング: `window-padding-x = 4` / `window-padding-y = 4`（ステータスバーの角が欠けない）
- ウィンドウサイズ: 90列×25行程度が適切
