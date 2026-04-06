# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `SessionEnd` hook for instant SHELL state detection when Claude Code session ends (`/exit`, Ctrl+D, etc.) — eliminates up to 2s polling delay for session exit detection
- `PermissionDenied` hook for auto mode support — when auto mode classifier denies an action, ccm shows PERMIT state with "Denied: <tool>" detail and sends notification
- `ccm setup-hooks` now installs 8 hook events (was 5): added `SessionEnd → SHELL`, `PermissionDenied → PERMIT`
- **Instant PERMIT notification** — desktop notification fires immediately from hook (~100ms) instead of waiting for next polling cycle (up to 3s)
- **Instant PERMIT status-right update** — mode 0 icon updates immediately from hook, bypassing `status-interval` cache delay
- PERMIT notifications now include tool name and context (e.g., "Permission required: Bash: rm -rf ..." or "Edit: ~/src/main.rs")
- Mode 1/2 status bar highlights the active (current) window with white bold text
- Auto-install/update hooks on plugin load (`ccm.tmux`) — TPM updates automatically register new hook types
- Dashboard shows "Hooks: OFF" banner when hooks are not installed, with colored status indicator (yellow=OFF, cyan=ON)
- Dashboard menu: Notifications, Notification sound, Sound name, Auto-start Claude (all auto-persisted to tmux.conf)
- Sound preview on name change / sound enable (macOS, uses `afplay` for instant playback)
- `@ccm-notify-sound-name` option for custom notification sound (default: Glass, 8 choices)
- Dashboard prompt: Unicode/CJK input support (Japanese project names, directory paths with non-ASCII characters)
- Dashboard: `F1` key toggles dashboard open/close (works in dashboard, tree view, and menu modes)
- `ccm setup-claude-md` / `ccm remove-claude-md` — add/remove ccm commands section in `~/.claude/CLAUDE.md` so every Claude Code session can discover sibling projects

### Fixed
- Mode 2 status line: fix double-counted separator width causing unnecessary extra lines
- PERMIT state now persists indefinitely until user responds (was expiring after 5 minutes)
- Fix false SHELL state from stale SessionEnd hook signal after Claude restarts with `--continue`
- Fix notification sound not playing for DONE notifications (sound now applied to both PERMIT and DONE)
- Dashboard: fix garbled display when add/register/remove fails (e.g. duplicate directory). Errors from `ccm_die` now propagate as `CCMError` within the dashboard and are shown in the message area instead of leaking to stderr and corrupting the curses screen
- Dashboard: fix display not updating after successful unregister/remove (stdout from `ccm_info` was desyncing curses differential redraw)
- Dashboard prompt: fix cursor/input position offset when prompt text contains CJK characters (used character count instead of display width)

### Changed
- Default `@ccm-notify` changed from `off` to `permit,done` — new users get PERMIT and DONE notifications out of the box
- Default `@ccm-notify-sound` changed to `off` (was `on`); notification sound: Basso → Glass
- Default idle auto-exit timeout changed from 5 to 10 minutes
- Notification sound settings (sound on/off, sound name) are macOS-only (hidden on Linux)
- Removed `ccm pane-title` command and `@ccm-pane-title` option (minimal value in single-pane windows)
- **Session management and snapshots migrated to Python** — `lib/session.sh` and `lib/snapshot.sh` eliminated, all logic consolidated in `lib/ccm_core.py`
  - `jq` dependency removed for snapshot operations (Python `json` module)
  - `ccm` bash dispatcher now routes all subcommands to `python3 ccm_core.py`
  - `lib/common.sh` reduced to bash-only essentials: init wizard, hook setup/removal, dependency checks, output formatting
  - Autosave (`_force_autosave`, `periodic_autosave`) calls Python directly instead of subprocess
  - 17 new pytest tests for `validate_name`, `find_window`, `list_windows_raw`, `cmd_snapshot_save`
  - `test_snapshot.bats` replaced by pytest equivalents
- **Core logic rewritten in Python** — state detection, status bar, dashboard all use `lib/ccm_core.py` as single source of truth (no more duplicate bash/Python logic)
  - `lib/ccm_core.py`: shared detection, project list, auto-exit, autosave
  - `lib/dashboard.py`: curses TUI with background thread (UI never blocks)
  - `lib/inject_status.py`: Python replacement for bash inject-status
  - Python 3.9+ is now a hard requirement (bash fallback removed)
  - Integrated tree view (`t`) and menu (`m`) modes within dashboard
  - Preview panel: live pane content alongside project list (`@ccm-preview on`)
  - ANSI color rendering in preview (256-color and RGB support)
  - Aligned column layout for project list
  - Scrolling support for large project lists
  - CJK wide character display width handling
- Hook scripts now update tmux window state instantly via `hooks/lib.sh` (no polling delay)
- PERMIT→BUSY transition detected instantly via `window_activity` timestamp comparison
- `PermissionRequest` hook for earlier PERMIT detection (fires before permission dialog)
- `CLAUDE_CODE_NO_FLICKER=1` enabled by default for reduced UI flicker in tmux
- `capture-pane` supports alternate screen mode (fallback to `-a` flag when empty)
- Guard against excessive CPU: enforce `status-interval >= 3` seconds
- Tree and menu keybindings (`prefix + T`, `prefix + C`) now opt-in to avoid plugin conflicts
- Dashboard sorts projects by state priority (PERMIT > DONE > BUSY > IDLE > SHELL), then by most recent activity within each group
- Idle auto-exit: Claude Code sessions exit after 5 minutes idle to free resources (`@ccm-idle-timeout`)
- Unified Claude Code launch command to `claude --continue` (was `--resume` for new windows)
- Add `claude` to dependency check (`ccm_check_deps`)
- **Expanded hook coverage** — 7 Claude Code hooks for comprehensive state detection:
  - `UserPromptSubmit` → BUSY (user prompt)
  - `PreToolUse` → BUSY (tool execution start, solves multi-turn gap)
  - `SubagentStart` → BUSY (subagent spawned)
  - `Stop` → DONE (response complete)
  - `StopFailure` → DONE (API error: rate limit, auth failure, etc.)
  - `Notification` → PERMIT (permission_prompt) / DONE (idle_prompt)
- Safety net "no prompt → BUSY" heuristic removed; trust process tree + hooks
- Snapshot restore no longer auto-starts Claude Code (SHELL state, starts on window switch)

### Security
- Escape project names in AppleScript notification commands to prevent injection via double quotes (Python and bash)
- Replace `echo -e` with `printf '%s'` in common.sh to prevent escape sequence injection from user input

### Fixed
- Restore desktop notifications for PERMIT/DONE state transitions (lost during Python rewrite)
- Restore Tab path completion in dashboard add-project prompt (lost during Python rewrite)
- Tree mode now auto-starts Claude Code when attaching to SHELL-state windows
- Snapshot load only restoring the first project due to `set -e` catching false condition in autosave guard
- Restore mouse click on status bar ccm icon to open dashboard (mode 0)
- Dashboard sort now works on macOS default bash 3.2 (removed `local -n` nameref dependency)
- False BUSY when background children (MCP servers, etc.) present but user is at input prompt
- Safety net prompt detection range expanded from 4 to 8 non-empty lines to avoid false BUSY from Claude Code UI elements
- False IDLE during text generation caused by `>` (ASCII) in output matching input prompt pattern
- Idle auto-exit sending `/exit` into partially typed user input (now sends Ctrl+C first to clear)
- Idle auto-exit timer based on `window_activity` (tmux's last-activity timestamp) instead of only `last_done`, preventing premature exit while user is composing input
- Duplicate dashboard processes not killed (force kill with SIGKILL if SIGTERM ignored)
- Autosave and auto-exit unreachable in status bar mode 1/2 (moved before mode-specific branches)
- Autosave project check now scans all sessions (was current session only)
- Deduplicate capture-pane calls: window-level captures now happen at most once per detection cycle
- False PERMIT from capture-pane matching "approve" in tip text — PERMIT now hook-only
- Status bar notifications not detecting state transitions (notify-cache file fix)
- PERMIT→BUSY transition not detected when prev_state was BUSY
- inject-status PID file race condition replaced with `fcntl.flock` file locking
- Cache file writes (status-cache, notify-cache, snapshots) use atomic temp+rename pattern
- Dashboard `_show_message()` no longer blocks UI with `time.sleep()` (non-blocking overlay)
- Dashboard operation callbacks (add/remove/register) no longer block 0.3-0.5s after completion
- Dashboard refresh loop exits within 0.2s instead of up to 2s delay
- ANSI parser no longer crashes on malformed SGR codes with non-numeric values
- Redundant double tilde expansion in `_resolve_project_dir()` and `read_cache_file()` simplified
- Dashboard shows "Terminal too small" instead of crashing on very small terminals (<40x10)

### Changed
- inject-status batches multiple `tmux set` calls into single `tmux_batch()` subprocess (~50% fewer subprocess calls per cycle)
- Timeout constants (`DONE_TIMEOUT`, `HOOK_TIMEOUT`, `IDLE_EXIT_TIMEOUT`, `CACHE_TTL`) overridable via environment variables (`CCM_DONE_TIMEOUT`, etc.)

### Added
- `ccm init` interactive setup wizard (hooks, auto-restore, status bar mode in one step)
- First-time setup guide in README and user guide (authenticate Claude Code before using ccm)
- `ccm_hooks_configured()` function to detect whether Claude Code hooks are installed
- Hook status display in dashboard footer and `ccm status` output (Hooks: ON/OFF)
- `ccm setup-hooks` now detects already-installed hooks and skips re-installation
- Claude Code hooks integration for improved state detection (`ccm setup-hooks` / `ccm remove-hooks`)
  - `UserPromptSubmit` hook → BUSY signal (detects text generation without child processes)
  - `Stop` hook → DONE signal (reliable response completion detection)
  - Hook signal files at `$TMPDIR/ccm-$UID/hooks/` with automatic expiry
  - Fully backward compatible: falls back to process tree inspection when hooks are not configured
- `_ccm_md5()` cross-platform MD5 hash utility (replaces inline md5/md5sum calls)

### Removed
- Legacy session-based detection functions: `ccm_detect_state()`, `_detect_raw_state()`, `ccm_format_status()`, `ccm_clear_done_session()`
- Legacy session-based project functions: `ccm_session_name()`, `ccm_project_name()`, `ccm_list_sessions()`, `ccm_session_exists()`, `CCM_SESSION_PREFIX`
- Legacy cache aliases: `_refresh_ps_cache()`, `_ensure_ps_cache()` (replaced with direct calls)
- Unused `STATUS_WORK` constant and corresponding dashboard case branch
- Dead bash code in common.sh: `_ccm_notify()`, `ccm_notify()`, `CCM_PATTERN_*`, `CCM_CLAUDE_PROCESS_NAME`, `COLOR_STATE_*`, `TMUX_COLOR_*`, `STATUS_*`, `CCM_DASHBOARD_INTERVAL`, `CCM_POPUP_WIDTH/HEIGHT`, unused timeout constants
- `lib/session.sh` — all session management functions migrated to Python
- `lib/snapshot.sh` — all snapshot functions migrated to Python
- Bash helpers migrated to Python: `ccm_current_session`, `_ccm_session`, `ccm_list_windows`, `ccm_find_window`, `ccm_project_exists`, `ccm_validate_name`, `ccm_expand_path`, `ccm_auto_start_claude`, `ccm_detect_window_state`, `ccm_clear_done`, `ccm_clipboard_copy`, `ccm_detect_ports`, `ccm_git_branch`, `_ccm_read_hook_signal`, `_ccm_md5`, `ccm_window_name`, `ccm_project_name_from_window`, `ccm_project_dir`
- `tests/test_snapshot.bats` — replaced by pytest tests in `test_ccm_core.py`

### Fixed
- Fix false IDLE/DONE display when Claude is actually busy (multi-turn tool use with expired hook signal): add capture-pane safety net verifying input prompt visibility before returning IDLE
- Fix false DONE display when hook=DONE but input prompt not visible: add capture-pane verification
- Fix race condition in Stop hook where delayed execution could overwrite a newer BUSY signal (`-gt` → `-ge`)

### Changed
- Dashboard initial display reads hook signal files for real-time accuracy (BUSY/DONE visible immediately)
- `ccm_update_window_names()` rewritten: single batch tmux call, hook signal integration, rename only on actual change (reduces flickering)
- `ccm setup-hooks` now strips old ccm hooks before adding (idempotent; handles path changes on reinstall)
- `ccm setup-hooks` and `ccm remove-hooks` require `jq` for JSON manipulation of Claude Code settings
- `settings.json` writes use atomic temp-file + `mv` to prevent corruption
- Hook BUSY capture-pane PERMIT check only runs on state transitions (reduces overhead)
- Split-pane safety: `on-stop.sh` preserves newer BUSY signals from other panes

### Fixed
- Unified all state detection calls to use `ccm_detect_window_state()` (7 call sites were using raw `_detect_window_state()`, missing hook signals and PERMIT detection)
- Fixed hook BUSY signal hiding PERMIT state: permission prompts have no child processes, so capture-pane PERMIT check is now performed before returning hook-based BUSY
- Fixed `IFS=$'\t' read` collapsing consecutive tab delimiters in window option cache parsing (empty fields were silently dropped)

## [0.1.0] - 2026-03-23

Initial public release.

### Added
- Window-based project management (`ccm add/remove/attach/list/register/unregister`)
- Claude Code state detection via process tree inspection (PERMIT/BUSY/IDLE/DONE/SHELL/DOWN)
- Interactive dashboard with `tmux display-popup` (`prefix + Tab`)
- Interactive tree view (`prefix + T`)
- Interactive menu (`prefix + C`)
- Three status bar display modes (`@ccm-status-line` 0/1/2)
- Theme compatibility: inject-status auto-detects external status-right changes
- Snapshot save/load/list/delete with auto-save on `ccm stop --all`
- Auto-restore on tmux start (`@ccm-auto-restore`)
- `ccm start <snapshot>` for workspace restoration
- `ccm capture` for pane content capture
- Git branch display (with dirty indicator) and listening port detection
- Desktop notifications (`@ccm-notify`, `@ccm-notify-sound`)
- Auto-start Claude Code on window switch (`@ccm-auto-start`)
- DONE state with 30-second auto-clear (`CCM_DONE_TIMEOUT`)
- DONE elapsed time display in dashboard (e.g., `✔3m`)
- PERMIT detection during BUSY→IDLE transition via input prompt check
- PERMIT re-check during DONE persistence (late-render recovery)
- Configurable keybindings (`@ccm-key-dashboard`, `@ccm-key-menu`, `@ccm-key-tree`)
- Mode 0 status bar shows window index with state (e.g., `7: BUSY ◉`)
- zsh completion
- Bilingual documentation (English / Japanese)
- Agent Teams compatibility
- Test infrastructure with bats-core (25 tests for state detection and snapshots)

### Fixed
- Window numbering unified to tmux window indices (dashboard, status bar, CLI)
- `stty -echo` prevents raw escape sequences during dashboard rebuild
- Buffered keystrokes during rebuild are interpreted as navigation

### Performance
- Dashboard instant open: cached state from tmux options + git/port cache files
- Full data (state detection, git, ports) loads on first refresh cycle
- Batched tmux commands (`list-panes -a`, `list-windows -a`)
- PS cache with PGID-based self-exclusion
- Skip capture-pane for IDLE panes
- Batched window option reads
- Git/port detection cached for 30 seconds
- Instant dashboard open using cached snapshot
