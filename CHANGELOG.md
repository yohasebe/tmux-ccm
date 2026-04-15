# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Dashboard attach and `x` (exit-all) actions could leave the target pane stuck in a tmux copy-mode / view-mode state, producing a `(jump to forward)` prompt or similar instead of the expected shell or Claude session. The root cause was that `send-keys` to a pane already in a tmux mode feeds the keys to copy-mode bindings instead of the pane's foreground process (so `/exit` would trigger a copy-mode search instead of exiting Claude, and `CLAUDE_CMD` would type into copy-mode). Dashboard now sends `send-keys -X cancel` to the target pane before any literal-text `send-keys` so the pane is always in normal input mode first. Applied in `_do_attach`, `_do_exit_all`, and the tree-view attach path
- `disable_all_hooks_warning()` now also tells the user that a custom `statusLine` is suppressed by the same setting. Claude Code v2.1.108 documentation clarified that `disableAllHooks: true` disables both hooks AND any configured `statusLine` command; users seeing their embedded statusLine disappear alongside hook signals were left without a pointer at the cause
- Desktop notifications could fire at times that did not match any real PERMIT / DONE event, as the polling notification path in `inject_status` interpreted fallback-derived state transitions (e.g. `jsonl_holds_busy` releasing, capture-pane footer match) as notification triggers. The polling path is now a strict safety net for the hook instant-notification path: it only fires when the project's own hook signal corroborates the state and is within `DONE_TIMEOUT`. Fallback-derived transitions no longer produce notifications
- Long thinking phases could flip from BUSY to DONE to IDLE even while Claude was still generating, when Claude Code's hook pipeline had gone silent (anthropics/claude-code#25655) after an earlier `PermissionRequest` and pure-thinking gaps exceeded the 5-second `jsonl_fresh_activity` window. A new `jsonl_holds_busy` detection rule now holds BUSY whenever `prev_state=BUSY` and the project's newest JSONL record was touched within `JSONL_ACTIVE_THRESHOLD` (default 120s, configurable via `CCM_JSONL_ACTIVE_THRESHOLD`). The rule only suppresses a BUSY→DONE transition; it does not promote IDLE to BUSY on its own
- Mode 1 / mode 2 status bar incorrectly bolded multiple windows when projects across different tmux sessions shared the same window index (e.g. window `2` in two sessions both rendered as bold/active). The active-window comparison now uses the full `session:index` target instead of the bare index
- Grandchild process detection no longer overrides a visible `❯ ` input prompt. A leftover dev server started by a previous Bash tool (e.g. `claude → zsh → ruby`) was falsely classified as active foreground tool execution — causing the project to stay BUSY even after Claude finished responding. The input prompt is now authoritative at the pane level; the v2.1+ case where `❯ ` appears above a still-running tool is handled at the window level by `hook_busy_idle` and `jsonl_fresh_activity` rules instead

### Changed
- **Conditional `elicitation_dialog` matcher installation gated on Claude Code version.** `ccm setup-hooks` now runs `claude --version` and omits the `elicitation_dialog` Notification matcher when the running client is older than v2.1.107. v2.1.101–v2.1.106 clients (which may silently reject a hook section containing an unknown matcher value) continue to work with the remaining 13 hook events — they lose only MCP elicitation detection until the client is upgraded. `ccm_hooks_configured` gates its matcher presence check on the same version predicate so older clients do not enter an infinite reinstall loop. README and hook installation output now describe v2.1.107 as recommended rather than strictly required (v2.1.101 remains the hard minimum because of `PostToolUseFailure`)

### Added
- **`ccm search` shortcut and `@ccm-key-search` tmux binding** — open the dashboard directly in the search prompt, skipping the intermediate `Dashboard → /` keystroke. `ccm dashboard` also accepts `--search` for the same effect. Opt-in via `set -g @ccm-key-search "/"` in `~/.tmux.conf` to bind `prefix + /`
- **`Notification` `elicitation_dialog` matcher support** (Claude Code v2.1.107+) — MCP servers can now request user input via elicitation dialogs, which are functionally identical to permission prompts (Claude is paused, user action required). `ccm setup-hooks` now registers a third Notification matcher alongside `permission_prompt` and `idle_prompt`, and `on-notification.sh` writes a PERMIT signal for it. `ccm_hooks_configured` verifies the matcher is present so an in-place reinstall picks up the upgrade
- **`allowManagedHooksOnly: true` setting canary** (Claude Code v2.1.107+) — when set, every user-scope hook is silently blocked. Since ccm installs all 14 of its hooks at user scope, this would take ccm's entire fast-path signal offline with no error. `managed_hooks_only_warning()` surfaces a warning in `ccm status` and the dashboard footer (same pattern as the existing `disableAllHooks` and `hooks.log` bloat canaries)
- **Authoritative PID→session mapping via `~/.claude/sessions/{pid}.json`** — Claude Code writes a runtime session file at session start with `{pid, sessionId, cwd, kind, entrypoint, startedAt}`. `read_session_info()` reads it and `_jsonl_from_session_info()` resolves the exact JSONL path for a running Claude process. `read_jsonl_age()` prefers this resolution over the slug-based directory scan when a `claude_pid` is available, so symlink / worktree / cwd-drift edge cases no longer produce wrong age readings. Headless `-p` (`kind="cli"`) sessions are skipped cleanly. Falls back to slug scan on older Claude Code versions
- **`disableAllHooks: true` setting canary** — Claude Code v2.1.104+ supports a `disableAllHooks` setting that silently disables all hooks AND any custom `statusLine`, taking ccm's fast-path signal offline. `disable_all_hooks_warning()` surfaces a warning in `ccm status` and the dashboard footer so the cause is discoverable (same pattern as the `hooks.log` bloat canary)
- **Documented Claude Code v2.1.101 as the minimum supported version** — earlier versions silently reject the entire `settings.json` when any hook event name is unknown, which can disable all ccm hooks if Claude Code drops a hook name ccm has registered
- **JSONL session-log activity heartbeat** — A new hook-independent BUSY signal: `read_jsonl_age()` polls the mtime of the newest `~/.claude/projects/<slug>/<sessionId>.jsonl` file. Claude Code appends one record at every conversation turn boundary (user prompt, assistant message, tool_use, tool_result), so a fresh mtime is unambiguous evidence that the session is alive and exchanging records — even when hooks have stopped firing (anthropics/claude-code#16047, #25655). Used as a positive-only signal: a fresh JSONL overrides raw=IDLE to BUSY when no hook signal has won. Pure thinking phases do not update the file, so stale JSONL never implies IDLE. Slug rule matches Claude Code's literal cwd convention (no realpath resolution); verified empirically against active sessions
- **`~/.claude/hooks.log` bloat canary** — `hooks_log_warning()` surfaces a warning in `ccm status` and the dashboard footer when Claude Code's unrotated hooks log exceeds 100 MB. This is the documented root cause of anthropics/claude-code#16047 — bloated logs silently disable all hook firing, and the only remediation (still missing upstream) is `: > ~/.claude/hooks.log`. The threshold is configurable via `CCM_HOOKS_LOG_WARN_BYTES`
- **Process-grandchild BUSY detection** — `detect_pane_state` now treats the presence of a grandchild process under `claude` (e.g. `claude → bash → xcodebuild`) as unambiguous evidence of foreground tool execution and resolves to BUSY, even when the v2.1+ Claude Code UI shows an empty `❯ ` input prompt above the running tool to advertise ctrl+b ctrl+b backgrounding. MCP servers and language servers stay direct children only and continue to be classified as background workers, so this does not regress the "MCP server + ❯ visible → IDLE" rule
- **Three additional hook events registered** by `ccm setup-hooks`: `SubagentStop`, `PreCompact`, `PostCompact` — all routed to `on-pre-tool-use.sh` (BUSY). SubagentStop closes a BUSY-hold gap when a subagent finishes but the parent agent is still working. PreCompact/PostCompact treat compaction as busy work and survive the post-compaction hook outage described in #25655 by being the last hooks to fire reliably
- **Hook-independent PERMIT detection** — `detect_pane_state` now recognizes Claude Code v2.1.101+ permission dialogs by matching the footer `Esc to cancel · Tab to amend · ctrl+e to explain` at the start of a line in the visible pane. This is a fallback for cases where Claude Code stops firing `PermissionRequest` hooks mid-session (see anthropics/claude-code#16047, #13193). The pattern is line-anchored to avoid false positives when the same words appear inside a Claude response, and other menus (`/hooks`, slash command pickers) use a different footer so are not classified as PERMIT
- `PostToolUseFailure` hook registration — v2.1.101 split tool failures out of `PostToolUse` into a dedicated event; ccm now writes BUSY for both, so state stays accurate across tool errors. `ccm_hooks_configured` also verifies this event is registered so that an in-place `ccm setup-hooks` can upgrade older configs
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
- Logo: SVG-based ccm logo with status-colored circles, light/dark mode PNG variants for README
- Re-inject status bar on `client-attached` — fixes mode 2 status disappearing after tmux detach/reattach when theme plugins overwrite status-right

### Removed
- `CCM_HOOK_TIMEOUT` environment variable and the `HOOK_TIMEOUT` constant. The BUSY hook detection path no longer caps signal age (see corresponding Fixed entry). If you were setting `CCM_HOOK_TIMEOUT` in your shell config, it is now silently ignored — you can remove it.

### Fixed
- **PERMIT state could get stuck indefinitely** if Claude Code crashed during a permission dialog. The `fallback_permit_hold` rule now requires the PERMIT hook signal to still be present and within `PERMIT_MAX_TIMEOUT`, so a stale prev_state=PERMIT without a live hook signal correctly falls through to IDLE.
- **Long-running tool execution no longer shows false IDLE** — removed the 5-minute `HOOK_TIMEOUT` cap on the BUSY hook detection path. Previously, any tool run or text-generation phase exceeding 5 minutes without an intervening `PreToolUse`/`PostToolUse` refresh would cause the BUSY hook rule to expire, fall through to `fallback_busy_to_done` (false DONE), and after 30 s become IDLE while Claude was still working. BUSY hook signals are now trusted regardless of age; `raw=SHELL`/`raw=DOWN` from the process tree remains the authoritative clear. The `CCM_HOOK_TIMEOUT` env var and the `HOOK_TIMEOUT` constant are removed.
- Mode 2 status line: fix double-counted separator width causing unnecessary extra lines
- PERMIT state now persists indefinitely until user responds (was expiring after 5 minutes)
- Fix false SHELL state from stale SessionEnd hook signal after Claude restarts with `--continue`
- Fix notification sound not playing for DONE notifications (sound now applied to both PERMIT and DONE)
- Dashboard: fix garbled display when add/register/remove fails (e.g. duplicate directory). Errors from `ccm_die` now propagate as `CCMError` within the dashboard and are shown in the message area instead of leaking to stderr and corrupting the curses screen
- Dashboard: fix display not updating after successful unregister/remove (stdout from `ccm_info` was desyncing curses differential redraw)
- Dashboard prompt: fix cursor/input position offset when prompt text contains CJK characters (used character count instead of display width)

### Changed
- **State detection refactored to a declarative rule table** — `detect_window_state` is now a thin 3-step orchestration (build context → evaluate rules → apply actions). All 14 state transitions are declared in `DETECTION_RULES` as a priority-ordered table, replacing ~130 lines of nested if/else. Pure `evaluate_rules()` function enables testing without tmux/ps/filesystem mocks.
- **Fast statusline path unified with rule table** — `build_project_list(fast=True)` now calls the shared `evaluate_fast()` helper, which runs the same `DETECTION_RULES` against a synthetic context derived from `prev_state`. Eliminates the duplicate hook-override logic that previously had to be kept in sync by hand.
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
