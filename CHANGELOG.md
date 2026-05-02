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
- Silent-catch sites (`inject_status` top level, `dashboard._refresh_loop`) now record exceptions to `$TMPDIR/ccm-$UID/errors.log` (1 MB cap, rotates to `errors.log.1` once for ~2 MB total). Crashing the status refresh is still avoided, but the next detection-cycle regression is debuggable without having to enable `CCM_DEBUG_TRACE` in advance. `CCM_ERRORS_LOG_MAX_BYTES` overrides the cap.
- `ccm errors [--clear]` subcommand prints the silent-exception log in chronological order (or clears it). Empty log prints "No silent-caught errors logged."
- Multi-byte text (Japanese / Chinese / Korean / emoji) handling hardened end-to-end:
  - `display_width()`, `truncate_to_width()`, `pad_to_width()` (in `ccm_render`) replace every `len()` / f-string `<N` spec used for terminal-column calculations. Project names like `日本語プロジェクト` no longer misalign columns in the dashboard, `ccm status`, `ccm ports`, `ccm list`, or `ccm snapshot list`.
  - All text-mode `open()` calls now pass `encoding="utf-8"` explicitly. Snapshot files (which can store CJK project names) and `~/.tmux.conf` / `~/.claude/settings.json` are no longer subject to the locale-default encoding (would fall back to ASCII under `LANG=C`).
  - `dashboard.py` initializes `locale.setlocale(LC_ALL, "")` at import. Without this, ncurses falls back to single-byte mode and `addstr` can raise `OverflowError` when a wide character reaches the curses layer.
  - East Asian Ambiguous characters (the IDLE icon `●` and SHELL icon `■` are EAW='A'; PERMIT `⚠` and BUSY `◉` are EAW='N') default to 1 column. CJK locale terminals that render Ambiguous chars as 2 columns can opt in with `CCM_AMBIGUOUS_WIDTH=2`. Neutral symbols stay at 1 column either way — that gap would require the external `wcwidth` package to close, which the plugin deliberately avoids.
- Phantom-subagent guard now commits `IDLE` directly when prev event is `notify_idle` or a terminal `stop` (rather than deferring to legacy). Prevents `raw_busy_passthrough` from latching false BUSY when `❯` is briefly off-screen during otherwise-idle periods. Mid-tool `stop` and `session_end` still defer (raw is the authoritative signal for those cases).
- Faster PERMIT → BUSY transition. After a user accepts a permission modal in accept-edits mode, Claude often spends several seconds in extended thinking before emitting the next assistant record — during which JSONL is not updated. The previous design required JSONL to be fresher than the permit event before promoting to BUSY, leaving the dashboard stuck on PERMIT for the entire thinking phase. Detection now treats `raw=IDLE` (modal physically gone, `❯` visible) the same as `raw=BUSY` for the tool_use override: as long as JSONL's last assistant `stop_reason` is `tool_use` within the long-tool window, return BUSY. The narrow trade-off — a one-poll-cycle false BUSY immediately after Esc-cancel before Claude writes a terminal stop_reason — is acceptable because the common accept path is now responsive.
- Cross-session events filter. `events.jsonl` is keyed on cwd and is append-only, so when a user `cd`s out of a subdirectory back to `@ccm_dir` between sessions, the new claude session re-uses the parent-key events file which still holds the prior session's tail. The previous design would commit the prior session's last state (BUSY at a `pretool`, PERMIT at a `permit_req`, etc.) until the new session wrote its first hook — multiple seconds of stale state right at startup. `derive_state_from_events` now drops events whose `ts` predates the live claude process start time (`now - claude_pid_age`, both kernel-monotonic values) and defers to legacy. Unknown `claude_pid_age` (-1) skips the filter so a malformed `ps` row cannot accidentally erase the event-log signal.
- Hook signal/events keying switched from `md5(cwd)` to Claude Code's `session_id` (UUID per session). cwd is mutable mid-session (`cd`) and reused across sessions — both silently produced writer/reader divergence, and the previous workarounds (`<key>.cwd` sidecar, claude_pid_age cross-session filter) only patched the symptoms. session_id is stable for a session's lifetime, distinct across sessions, and unaffected by `cd`, so the bug class is structurally closed: there is no observable difference between writer key and reader key when both derive from the same authoritative session_id. ccm-side resolution is `@ccm_dir → tmux pane → claude pid → ~/.claude/sessions/<pid>.json → sessionId`; the slow path caches the result on the `@ccm_session_id` tmux window option and the fast path reads it. Net code reduction: ~140 lines and 10 tests removed alongside the workarounds they supported.
- Property-based invariant suite (`tests/test_derive_invariants_pbt.py`) using `hypothesis`. Asserts that `derive_state_from_events` is total over its input space, that pid_absent always yields SHELL, that raw=PERMIT never overrules a visible modal, and that empty event logs route through to the legacy fallback. Catches whole classes of regression in the rule logic that example-based tests cannot.
- `derive_state_from_events` restructured into `classify_activity` (input normalisation + Activity enum) and `map_activity_to_state` (small decision tree). Replaces the previous 200-line per-event-class branch cascade with a two-phase pipeline whose intermediate Activity values (AT_REST / AWAITING_PERMIT / IN_PROGRESS / UNKNOWN) match the user-facing question "do I need to do something right now?". Phantom-subagent handling moves into a small reusable normaliser. The public `derive_state_from_events` contract is unchanged, but the internals are now auditable as a state machine instead of a dispatch.
- Hook signal writes now call `tmux refresh-client -S` for non-PERMIT transitions too (PERMIT was already covered). BUSY ↔ IDLE flips appear in ~100 ms instead of waiting up to `status-interval` (1 s) — the floor on user-perceived latency drops to dispatch overhead.
- `ccm doctor` subcommand: single self-check that aggregates dependency versions (claude / tmux / jq / fzf), hook installation, runtime canaries (hooks.log size, disableAllHooks, allowManagedHooksOnly, cluster-SHELL transitions), per-project state and session_id, silent-exception log count, and the active configuration paths. Designed as "first thing to run when something feels wrong" and as a drop-in artefact for bug reports.
- Dashboard `?` key now opens menu mode (alias of `m`), matching the standard "show all commands" key in vim / fzf / etc. Help line at the bottom expanded to include `[/] search` and `re[g]ister` so every dashboard-mode keybinding is visible at a glance.
- Error messages tightened on the most common failure paths: "Invalid project name" / "Invalid snapshot name" now state the allowed character set, "Not inside a tmux session" suggests `tmux new-session` as the next step, "Failed to create window" points at `tmux info` for diagnosis.
- N+1 tmux subprocess elimination. `_session_id_from_tmux` and the slow-path `show-option @ccm_session_id` were called once per project per refresh; both now read from the bulk `list-windows` query that already runs per cycle. For 4 projects on the dev machine: fast path 3 → 2 tmux calls/refresh, slow path 12 → 9. Saves linearly with project count.
- Pid-reuse defence in `read_session_info`. When passed a `ps` snapshot, cross-checks Claude Code's recorded `startedAt` against the live process's etime-derived start time. A drift beyond `CCM_SESSION_INFO_AGE_DRIFT_SEC` (default 10 s) means the file is from a previous session whose pid got recycled — read returns None so the caller falls through to legacy detection rather than reading the wrong session's events. Without `ps_lines` the cross-check is skipped (defensive).
- Silent autosave failures now logged via `log_caught_exception`. `_force_autosave` and `periodic_autosave` previously swallowed all exceptions silently; a recurring failure (disk full, snapshot dir permissions changed, etc.) is now visible via `ccm errors`.

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
