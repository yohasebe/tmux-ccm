# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

### Changed
- Dashboard initial display reads hook signal files for real-time accuracy (BUSY/DONE visible immediately)
- `ccm_update_window_names()` rewritten: single batch tmux call, hook signal integration, rename only on actual change (reduces flickering)
- `ccm setup-hooks` now strips old ccm hooks before adding (idempotent; handles path changes on reinstall)
- Hook scripts use `grep`/`sed` fallback when `jq` is unavailable
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
