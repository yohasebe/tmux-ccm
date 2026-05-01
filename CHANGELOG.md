# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-05-01

Initial public release. ccm is a tmux plugin that manages Claude Code sessions as tmux windows, with live state detection, an interactive dashboard, status-bar integration, and snapshot save/restore.

### Project management
- Window-based project model: `ccm add` / `open` / `register` / `unregister` / `remove` / `attach` / `list` / `rename`. Each project is a tagged tmux window; the window options `@ccm_project` and `@ccm_dir` are the source of truth.
- `ccm send <project> <message>` for cross-project prompt injection, with state-gated safety (PERMIT modals are unconditionally non-bypassable, including `--force`). Refusals classify the modal (session-resume, permission-request, confirmation-modal, unknown-permit) and quote the captured pane tail so the calling agent can explain the situation.
- Snapshot save / load / list / delete; `_autosave` snapshot is taken automatically on `ccm stop --all`. Optional `@ccm-auto-restore on` reloads the autosave on tmux start.
- Auto-start Claude Code on attach to a SHELL window via `claude --continue`. Idle sessions auto-exit after 10 minutes (`CCM_IDLE_EXIT_TIMEOUT`) to free resources; the next attach restarts and resumes the conversation.

### State detection
- Four states (PERMIT / BUSY / IDLE / SHELL) plus DOWN for tmux-down windows. Detection is event-log primary with a legacy declarative-rules fallback:
  - **Event log** — every Claude Code hook appends a `{ts, type}` record to `$HOOK_DIR/<md5>.events.jsonl`. `derive_state_from_events` reads the tail as a pure function and produces the resolved state.
  - **JSONL stop_reason bridge** — when hooks fall silent (e.g. Esc-interrupted turn, hook-delivery silent failure), the most recent assistant `stop_reason` from `~/.claude/projects/<slug>/<sessionId>.jsonl` releases stuck BUSY (`end_turn` / `max_tokens` / `stop_sequence`) or holds it (`tool_use` mid-turn).
  - **Legacy DETECTION_RULES** — a small declarative table covers cases the event log can't resolve (empty log, malformed records, post-`session_end` transient with a live pid).
  - **Capture-pane footer fallback** — `PATTERN_PERMIT_FOOTER` matches modal footers (`Esc to cancel · Tab to amend`, `Enter to confirm · Esc to <verb>`) directly, so PERMIT is detected even when hooks have stopped firing.
  - **Sliver pane filter** — panes shorter than `SLIVER_HEIGHT_THRESHOLD` (4 rows) are excluded from window-state aggregation, since they cannot reliably render the prompt indicator.
- Window state aggregates pane states by priority `PERMIT > BUSY > IDLE > SHELL`. Agent Teams (split panes per teammate) and casual splits surface attention-needing panes regardless of which pane is currently active.
- 14 hook events across 7 scripts: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStart`/`Stop`, `PreCompact`/`PostCompact`, `Stop`/`StopFailure`, `PermissionRequest`, `PermissionDenied`, `Notification` (permission_prompt / idle_prompt / elicitation_dialog), `SessionEnd`.
- Live state-detection trace (`ccm debug trace <project>`) prints one JSON line per scan with every `DetectionContext` input, the event log derivation, and the resolved state. `CCM_DEBUG_TRACE=<path>` does the same for the production polling path.

### Dashboard
- Toggleable popup (`prefix + Tab`) shows every project's state, git branch, listening ports, and pane count.
- `[N]` multi-pane indicator (brackets dim, digit cyan) marks windows with more than one tmux pane.
- `* elapsed` recently-completed marker fires for `COMPLETED_AT_TIMEOUT` seconds after a BUSY/PERMIT → IDLE transition. Suppressed on non-IDLE rows so the marker never contradicts the current state.
- `(bg)` affordance highlights IDLE projects whose process tree shows leftover background activity (typically a dev server spawned during a previous turn).
- `(Nm)` stale-signal age suffix appears on BUSY / PERMIT entries whose hook signal is older than `JSONL_HOOK_GAP_TOLERANCE` (60 s), giving the user a visible hint that auto-release windows have lapsed.
- Auto-focus to a pane in PERMIT on attach when the active pane is not the one waiting on a permission modal.
- Live filter search (`/`) navigates large project lists; opening the dashboard directly into search mode is available via the optional `@ccm-key-search` binding.
- Interactive tree view (`prefix + T`) with session / window / pane hierarchy.
- Interactive menu (`prefix + C`) for settings management.

### Status bar
- Three modes via `@ccm-status-line`:
  - `0` — priority icon appended to your existing `status-right` (most conservative).
  - `1` — replaces tmux's window list with ccm-style coloured entries.
  - `2` (default) — adds a dedicated row below the main status bar with branch / port details for every project.
- Mode 2 colour palette is themable via `@ccm-status-bg` / `-gutter-bg` / `-fg` / `-fg-dim`. Invalid colour values silently fall back to the defaults.
- Polling cadence tunable via `CCM_STATUS_INTERVAL` (default 5 s). Hook-driven `@ccm-permit-pending` keeps PERMIT-axis responsiveness independent of the polling rate.

### Notifications
- Desktop notifications via `@ccm-notify` (`permit` / `completed` / `all`), with sound options.
- macOS: when `terminal-notifier` is installed (`brew install terminal-notifier`), notifications use per-project `-group ccm-<project>` so a fresh notification replaces the previous one in Notification Center rather than accumulating. `osascript` is used as a fallback.
- `ccm clear-notifications` bulk-removes ccm notifications from macOS Notification Center, scoped to `ccm-`-prefixed group ids only — notifications from unrelated terminal-notifier scripts are left intact.
- Per-project dedup markers ensure concurrent projects never suppress each other's notifications.
- Grace window (`CCM_COMPLETION_GRACE_SEC`, default 3 s) absorbs the Stop hooks Claude Code fires at multi-turn tool boundaries, so the COMPLETED alert only arrives on a genuine completion.
- Linux: `notify-send` is used. There is no per-project dedup equivalent; pin to `@ccm-notify "permit"` to limit volume.

### Robustness canaries
- `~/.claude/hooks.log` size canary — warns in `ccm status` and the dashboard footer when the file exceeds 100 MB (the documented root cause of upstream silent hook delivery failure).
- `disableAllHooks` and `allowManagedHooksOnly` canaries — surface a warning when these Claude Code settings are in effect, since they disable every ccm hook silently.
- Cluster-SHELL canary — detects rapid SHELL transitions (3 in 10 minutes) and warns the user, surfacing the macOS silent-exit class of regression.
- `ccm setup-hooks` hard-fails on Claude Code below v2.1.107 and on missing `claude` binary, instead of installing a partial hook set.
- `ps_snapshot` and `tmux_cmd` decode subprocess output with `errors="replace"`. macOS truncates the `ps comm` column at a fixed byte width, slicing multi-byte characters mid-codepoint (e.g. an app named `⌘英かな` produces orphan UTF-8 bytes); a decode error there would silently kill the entire detection cycle and freeze every project's `@ccm_prev_state`. `clear_notifications` and the `cmd_attach` claude-child probe got the same treatment.
- Per-project exception barrier in `build_project_list`. A bug in detection for one project no longer freezes every other project's state — the failing project carries forward its previous `@ccm_prev_state` while the rest of the loop continues.
- Silent-catch sites (`inject_status` top level, `dashboard._refresh_loop`) now record exceptions to `$TMPDIR/ccm-$UID/errors.log` (1 MB cap). Crashing the status refresh is still avoided, but the next detection-cycle regression is debuggable without having to enable `CCM_DEBUG_TRACE` in advance.
- Multi-byte text (Japanese / Chinese / Korean / emoji) handling hardened end-to-end:
  - `display_width()`, `truncate_to_width()`, `pad_to_width()` (in `ccm_render`) replace every `len()` / f-string `<N` spec used for terminal-column calculations. Project names like `日本語プロジェクト` no longer misalign columns in the dashboard, `ccm status`, `ccm ports`, `ccm list`, or `ccm snapshot list`.
  - All text-mode `open()` calls now pass `encoding="utf-8"` explicitly. Snapshot files (which can store CJK project names) and `~/.tmux.conf` / `~/.claude/settings.json` are no longer subject to the locale-default encoding (would fall back to ASCII under `LANG=C`).
  - `dashboard.py` initializes `locale.setlocale(LC_ALL, "")` at import. Without this, ncurses falls back to single-byte mode and `addstr` can raise `OverflowError` when a wide character reaches the curses layer.

### Setup / integration
- `ccm init` interactive setup wizard.
- `ccm setup-hooks` / `ccm remove-hooks` — install or uninstall ccm's Claude Code hook set. `remove-hooks` preserves any non-ccm hook entries the user has configured.
- `ccm setup-claude-md` / `ccm remove-claude-md` — add or remove the ccm section in `~/.claude/CLAUDE.md`.
- Zsh completion.
- Bilingual documentation: English and Japanese READMEs and user guides are kept in sync.

### Requirements
- tmux 3.2+ (popup support).
- Claude Code v2.1.107+.
- jq, fzf, Python 3.
