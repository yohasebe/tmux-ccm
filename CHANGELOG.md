# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Event-log detection backbone** (`derive_state_from_events`). Per-project hook events are appended to `$HOOK_DIR/<md5>.events.jsonl` as `{"ts": …, "type": …}` records, and state is derived as a pure function of the event tail plus the most recent assistant `stop_reason` from the project's JSONL session log. The 9-member event vocabulary (`prompt`, `pretool`, `posttool`, `subagent`, `compact`, `stop`, `permit_req`, `notify_permit`, `notify_idle`, `session_end`) maps to the 4-state model with a single `STARTUP_GRACE_SEC` time window. `CCM_USE_EVENT_LOG=auto` (the default) commits the event-log state when derive returns non-`None`; `off` is a diagnostic kill-switch for legacy-only operation.
- **`(bg)` UI affordance** — IDLE projects whose process tree shows leftover background activity (typically a dev server or orphan tool spawned by a previous Claude turn) render `(bg)` in the dashboard, `ccm status`, and status bar. The state still says IDLE (the user has the conversation ball) but the suffix communicates that something Claude spawned is still alive.
- **Stale-signal age suffix `(Nm)`** — BUSY / PERMIT entries whose underlying hook signal is older than `JSONL_HOOK_GAP_TOLERANCE` (60 s) carry a parenthesised age (e.g. `⚠ PERMIT (8m)`) so the user can judge whether the displayed state is fresh or stuck. Surfaces in the dashboard, `ccm status`, and status bar (mode 1 / 2).
- **Multi-pane window indicator `[N]`** — windows with more than one tmux pane render `[N]` (brackets dim, digit cyan) immediately after the project name. Surfaces Agent Teams workflows, casual splits, and orphan panes that aggregate into the window state.
- **Recently-completed marker `* elapsed`** — when a project transitions out of BUSY / PERMIT into IDLE, the dashboard shows `* <elapsed>` (asterisk green, time dim) for `COMPLETED_AT_TIMEOUT` seconds. ASCII-only (rather than `✔`) so column math is consistent across terminals and fonts.
- **Window-state aggregation with sliver exclusion** — `detect_window_raw` aggregates pane states by priority `PERMIT > BUSY > IDLE > SHELL` after filtering out panes shorter than `SLIVER_HEIGHT_THRESHOLD` (4 rows by default). Tall panes drive Agent Teams visibility; sliver panes (1-row strips, hidden splits) cannot reliably report state and are excluded.
- **Auto-focus to PERMIT pane on attach** — `reset_window_after_attach` calls `auto_focus_attention_pane(win_target)` after its existing wipes. When the window has multiple eligible panes and one is in PERMIT while the active pane is not, ccm runs `tmux select-pane` to move focus there. Saves a manual `prefix + arrow` after attaching to a project waiting on a permission modal.
- **Live state-detection trace** — `ccm debug trace <project> [interval]` prints one JSON line per scan with every `DetectionContext` input, the event log derivation, and the resolved state. Read-only; runs alongside the normal detection pipeline. `CCM_DEBUG_TRACE=<path>` does the same for the production path with a `TRACE_MAX_BYTES` size cap.
- **PERMIT modal classification on `ccm send` refusal** — `classify_permit_modal()` distinguishes `session-resume`, `permission-request`, `confirmation-modal`, and `unknown-permit` from the captured pane tail. The refusal text quotes the matched category, embeds tailored guidance, and appends the last 8 non-empty lines of the pane so the calling agent can explain the situation to the user. PERMIT remains unconditionally non-bypassable, even with `--force`.
- **`docs/state-machine.md`** — formal reference for the detection pipeline: state model, event-log decision tree, legacy fallback table, key discriminators, time-window heuristics, and lifecycle walk-throughs.
- **Test invariants** — `TestPipelineInvariants` and `TestDeriveInvariants` parametrize over the input space and assert global contracts (resolved state always in the documented set; raw=SHELL/DOWN always pass through; pid_present=False always shells derive; raw=PERMIT precedence). Plus drift-guard tests for hook signal parity (Python `notify()` ↔ bash `_ccm_instant_notify`) and rule phase annotations.
- **`terminal-notifier` integration for macOS notifications** — when installed (`brew install terminal-notifier`), ccm sends notifications with `-group ccm-<project>` so a fresh notification for a project replaces the previous one in Notification Center rather than accumulating. Without this, long-running multi-project sessions can pile up hundreds of stale notifications and drive WindowServer / NotificationCenter to high CPU. Falls back to `osascript` when `terminal-notifier` is absent.
- **`ccm clear-notifications`** — bulk-removes ccm notifications from macOS Notification Center. Requires `terminal-notifier`.
- **`CCM_STATUS_INTERVAL` env var** lets users tune how often tmux invokes `ccm inject-status` (default 5 s, was 2 s). Hook-driven `@ccm-permit-pending` keeps PERMIT-axis responsiveness independent of the polling cadence.
- **Themable mode-2 colours** via `@ccm-status-bg` / `@ccm-status-gutter-bg` / `@ccm-status-fg` / `@ccm-status-fg-dim` tmux options. Defaults match the dark-grey palette; override any subset to integrate with light themes or custom dark schemes without forking ccm.

### Changed
- **Claude Code v2.1.107+ is now required.** `ccm setup-hooks` hard-fails on older clients (or when `claude` is not on PATH) instead of silently skipping the `elicitation_dialog` Notification matcher. The matcher is registered unconditionally; `ccm_hooks_configured` always requires it.
- **State model is 4 + 1** (PERMIT / BUSY / IDLE / SHELL / DOWN). The legacy DETECTION_RULES table is the safety net for cases where the event-log path returns `None` (empty log, malformed records, post-`session_end` transient with a live pid). For projects with hooks installed and an active session, the event-log path is authoritative.
- **Markers in dashboard / status bar / `ccm status`** are pure ASCII (`[N]` for pane count, `* elapsed` for completion) to eliminate East-Asian-Width / font-rendering edge cases that can offset later columns. Brackets / time render dim; the digit / asterisk get the eye-catching colour.
- **Window state is per-window aggregated, not per-pane** — Agent Teams (split panes per teammate) and casual splits both surface attention-needing panes regardless of which pane is active. Sliver-pane filter prevents an invisible 1-row pane from infecting the visible window state.

### Fixed
- **Spurious BUSY on attach to a SHELL window.** Auto-started `claude --continue` spawns MCP servers as direct children before the `❯` prompt renders; the `has_child=True + no prompt` signature is indistinguishable from streaming. Resolved by the `startup_transient_raw_busy` rule, which keys on the `claude` pid's own age (`ps etime`, kernel-supplied) — under `STARTUP_GRACE_SEC` (default 60 s) with no hook signal, raw=BUSY is demoted to IDLE.
- **Spurious BUSY when foreground is a shell.** A pane with a backgrounded `claude` in its process tree but the foreground program being a shell (`zsh` / `bash` / etc.) used to false-read BUSY. `detect_pane_state` now consults `tmux #{pane_current_command}`; if the foreground is in the `SHELL_FOREGROUND_COMMANDS` set, the pane resolves to SHELL regardless of leftover claude pids. Editor / pager foregrounds (`vim`, `less`, …) intentionally do not short-circuit, so auto-start does not fire over them.
- **Stuck BUSY after Esc-interrupt.** Claude Code does not fire Stop / StopFailure when the user presses Esc, so the BUSY hook signal stays live and the latest event log entry remains start-class. The event-log path now releases to IDLE when the JSONL stop_reason is terminal AND fresher than the latest event AND `raw≠"PERMIT"`.
- **Stuck PERMIT after silent permission resolution.** When a permission auto-resolves under `accept edits on` mode, Claude Code does not fire a "permission-resolved" hook, so the PERMIT signal lingers. The event-log permit branch now releases to IDLE when JSONL terminal stop_reason is fresher than the permit event, and re-classifies to BUSY when JSONL `tool_use` is fresher (auto-approved + tool actively running).
- **Phantom-subagent stuck BUSY.** Upstream Claude Code occasionally fires spurious `SubagentStart` / `SubagentStop` events during otherwise-idle periods. Pattern: `... stop, notify_idle, subagent` with no follow-up. The event-log start-class branch walks back through stacked subagent events; landing on `notify_idle` without crossing a real `prompt` / tool event identifies the phantom and defers to legacy.
- **Phantom JSONL "fresh activity" from upstream housekeeping.** v2.1.108+ records (`away_summary`, `turn_duration`, `stop_hook_summary`, `task_reminder`) and v2.1.117 startup records (`permission-mode`, `file-history-snapshot`, `last-prompt`) are filtered from the JSONL real-activity heartbeat. Records without a `timestamp` field are not counted as activity (defense-in-depth for future no-timestamp types).
- **`/model` picker / session-resume modal misclassified as BUSY.** `PATTERN_PERMIT_FOOTER` now matches `Esc to <verb>` (cancel / exit / close / quit / dismiss) so any future modal-author wording lands on PERMIT consistently. Bare `Esc to cancel` slash-menu navigation (`/hooks`, `/skills`) remains non-PERMIT.
- **Phantom "very late" COMPLETED desktop notification.** `on-notification.sh` no longer fires `_ccm_instant_notify` on the `idle_prompt` branch (anthropics/claude-code#5186 documents 10–60s+ delay; the per-project 10 s dedup window was too short to absorb the late echo). The on-stop.sh grace-scheduled notification is now the single authoritative completion ping.
- **`@ccm-notify` parity** — Python `notify()` and bash `_ccm_instant_notify` reach identical fire/skip decisions for every `@ccm-notify` × state combination, enforced by `tests/test_notify_parity.bats`.
- **Mode 2 stale `status-format` slots.** The leftover-slot cleanup capped at slot 5, so a layout that previously expanded past 4 entry rows (extreme narrow terminal + many projects) could leave `status-format[6+]` populated after the row count shrank. Cleanup now extends to a `_MODE2_MAX_SLOTS=16` ceiling, covering any realistic configuration.
- **Silent notification failures for non-Terminal.app users.** `_ccm_instant_notify` (bash) and `notify()` (Python) used to pass `-sender com.apple.Terminal` to terminal-notifier so the icon would show as Terminal.app, but that delivered the notification under Terminal.app's bundle identity — and macOS silently drops it for every user not running Terminal.app (iTerm2, WezTerm, kitty, ghostty, …). `terminal-notifier` returned exit=0 even though nothing arrived, so the osascript fallback never fired. The flag is now omitted; notifications flow under terminal-notifier's own bundle id (one-time permission grant, terminal-emulator-independent). `clear-notifications` likewise no longer scopes by `-sender`, keeping send/remove identities aligned.
- **`ccm clear-notifications` no longer wipes unrelated terminal-notifier notifications.** Previously called `terminal-notifier -remove ALL`, which removed every notification terminal-notifier had sent — including those from the user's other scripts (deploy alerts, monitoring tools). Now enumerates `-list ALL` and removes only group ids prefixed with `ccm-`. The CLI feedback message reports the actual count removed.
- **Mode-2 layout cap.** `inject_status` mode 2 used to compute an unbounded `num_lines`; an extreme degenerate case (very narrow terminal × many projects) could write past `_MODE2_MAX_SLOTS`, leaving high slots stale on the next render. `num_lines` now caps at `_MODE2_MAX_SLOTS - 1` and the last entry row packs in any overflow rather than dropping projects from view.
- **`@ccm-status-bg` / `-gutter-bg` / `-fg` / `-fg-dim` validation.** A typo'd colour value used to be passed straight through to `#[bg=garbage]`, producing a blank or malformed status bar. `_opt_color` now validates against tmux's accepted colour syntax (`#RGB` / `#RRGGBB` / `colour123` / named colours) and falls back to the default on anything else.
- **Silent autosave failures during `ccm stop --all`.** `_autosave_trigger` and the inline autosave block in `cmd_stop` swallowed every exception, so a write-permission or disk-space problem left the user believing a snapshot existed. Both paths now surface a `ccm_warn` while still proceeding with the stop.

## [0.2.0] - 2026-04-18

Initial public release.

### Added
- Window-based project management (`ccm add/open/remove/attach/list/register/unregister/rename`)
- 4-state detection model (PERMIT/BUSY/IDLE/SHELL) via Claude Code hooks + multi-layer fallback:
  - 14 hook events (7 scripts): UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart/Stop, PreCompact/PostCompact, Stop/StopFailure, PermissionRequest, PermissionDenied, Notification (permission_prompt / elicitation_dialog / idle_prompt), SessionEnd
  - JSONL session-log heartbeat with system-record filtering (recap / away_summary safe)
  - Process grandchild detection (foreground tool execution)
  - Permission dialog footer match (capture-pane fallback)
  - Canaries: hooks.log bloat, disableAllHooks, allowManagedHooksOnly, cluster-SHELL (#48069)
- ✔ completion marker: display-layer "recently completed" icon (30s after BUSY/PERMIT→IDLE, cosmetic only — not a detection state)
- Interactive dashboard (`prefix + Tab`) with live status, preview panel, add/remove/rename/save
- Live incremental filter search (`/` in dashboard or `prefix + /` via `@ccm-key-search`). Unicode-safe
- Interactive tree view (`prefix + T`) with session/window/pane hierarchy
- Interactive menu (`prefix + C`) for settings management
- `ccm send` — cross-project prompt injection with state-gated safety (PERMIT hard guard)
- Three status bar modes (`@ccm-status-line` 0/1/2) with theme compatibility
- Desktop notifications (`@ccm-notify`) with instant hook delivery, sound options, per-project dedup markers (concurrent projects never suppress each other's notifications), and a grace window (`CCM_COMPLETION_GRACE_SEC`) that absorbs the Stop hooks Claude Code fires at multi-turn tool boundaries so the alert only arrives on a genuine completion
- Snapshot save/load/list/delete with `_autosave` on `ccm stop --all`
- Auto-start Claude Code on window switch; auto-exit idle sessions after 10 minutes
- Git branch (with dirty indicator) and listening TCP port detection per project
- Cluster-SHELL transition canary for macOS silent-exit regression (anthropics/claude-code#48069)
- `ccm setup-hooks` / `ccm remove-hooks` with version-gated elicitation_dialog matcher
- `ccm setup-claude-md` / `ccm remove-claude-md` for `~/.claude/CLAUDE.md` integration
- `ccm init` interactive setup wizard
- Zsh completion
- Bilingual documentation (English / Japanese)
- 274 pytest tests + bats hook tests
