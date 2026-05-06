# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- `ccm send <name> --start <msg>` no longer silently loses the message when `claude --continue` resumes into an auto-action. The previous fixed two-second wait could deliver keystrokes mid-`/compact` (long-session resume) or into a session-resume picker — the operator saw `Sent to <name>` while the message never reached the prompt. The launch path now polls the target's detected state every 0.5 s and proceeds only when state is `IDLE`. `PERMIT` short-circuits the wait (modal needs operator action; sending keystrokes there could approve/deny tools); the timeout (default 10 s, `CCM_START_WAIT_SEC` overrides) refuses the send with the captured pane tail so the operator can finish by hand. Progress is printed once per second when run interactively.

## [0.3.0] - 2026-05-05

Initial public release. ccm is a tmux plugin that manages Claude Code sessions as tmux windows, with live state detection, an interactive dashboard, status-bar integration, and snapshot save/restore.

### Project management
- Window-based project model: `ccm add` / `open` / `register` / `unregister` / `remove` / `attach` / `list` / `rename`. Each project is a tagged tmux window; the window options `@ccm_project` and `@ccm_dir` are the source of truth.
- `ccm send <project> <message>` for cross-project prompt injection, with state-gated safety (PERMIT modals are unconditionally non-bypassable, including `--force`). Refusals classify the modal (session-resume, permission-request, confirmation-modal, unknown-permit) and quote the captured pane tail so the calling agent can explain the situation.
- Snapshot save / load / list / delete; `_autosave` snapshot is updated automatically every 2 minutes while projects exist, and also written on `ccm stop --all`. Optional `@ccm-auto-restore on` reloads the autosave on tmux start.
- Auto-start Claude Code on attach to a SHELL window via `claude --continue`. Idle sessions auto-exit after 10 minutes (`CCM_IDLE_EXIT_TIMEOUT`) to free resources; the next attach restarts and resumes the conversation.

### State detection
- Four states (PERMIT / BUSY / IDLE / SHELL) plus DOWN for tmux-down windows. Detection runs as `classify_activity` (input normalisation + Activity enum) followed by `map_activity_to_state` (small decision tree); intermediate Activity values (AT_REST / AWAITING_PERMIT / IN_PROGRESS / UNKNOWN) match the user-facing question "do I need to do something right now?".
- Hook artefacts (signal file, event log) are keyed on Claude Code's session_id (UUID per session): stable for the session's lifetime, distinct across sessions, unaffected by mid-session `cd`. ccm resolves session_id via `@ccm_dir → tmux pane → claude pid → ~/.claude/sessions/<pid>.json`; the slow path caches it on the `@ccm_session_id` tmux window option for the fast statusline path to reuse.
- Detection layers, in priority order:
  - **Event log** — every Claude Code hook appends a `{ts, type}` record to `$HOOK_DIR/<sessionId>.events.jsonl`. `derive_state_from_events` reads the tail as a pure function and produces the resolved state.
  - **JSONL stop_reason bridge** — when hooks fall silent (Esc-interrupted turn, hook-delivery silent failure), the most recent assistant `stop_reason` from `~/.claude/projects/<slug>/<sessionId>.jsonl` releases stuck BUSY (`end_turn` / `max_tokens` / `stop_sequence`) or holds it (`tool_use` mid-turn).
  - **Capture-pane footer** — `PATTERN_PERMIT_FOOTER` matches modal footers (`Esc to cancel · Tab to amend`, `Enter to confirm · Esc to <verb>`) directly, so PERMIT is detected even when hooks have stopped firing.
  - **Legacy DETECTION_RULES** — a small declarative rule table covers cases the event log can't resolve (empty log, malformed records, post-`session_end` transient with a live pid).
- Phantom-subagent recognition: subagent events that fire after a session reaches a rest state (`notify_idle` / terminal `stop` / `session_end`) are spurious upstream firings; `_strip_phantom_subagents` filters them so detection sees the underlying rest marker.
- Window state aggregates pane states by priority `PERMIT > BUSY > IDLE > SHELL`. Agent Teams (split panes per teammate) and casual splits surface attention-needing panes regardless of which pane is currently active.
- Sliver pane filter — panes shorter than `SLIVER_HEIGHT_THRESHOLD` (4 rows) are excluded from window-state aggregation, since they cannot reliably render the prompt indicator.
- 14 hook events across 7 scripts: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStart`/`Stop`, `PreCompact`/`PostCompact`, `Stop`/`StopFailure`, `PermissionRequest`, `PermissionDenied`, `Notification` (permission_prompt / idle_prompt / elicitation_dialog), `SessionEnd`.
- Hook signal writes trigger an immediate `tmux refresh-client -S`, so BUSY ↔ IDLE / PERMIT flips appear in ~100 ms instead of waiting for the next polling cycle.
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
- Interactive menu (`prefix + C`); `?` is also bound as an alias following the vim / fzf convention.

### Status bar
- Three modes via `@ccm-status-line`:
  - `0` — priority icon appended to your existing `status-right` (most conservative).
  - `1` — replaces tmux's window list with ccm-style coloured entries.
  - `2` (default) — adds a dedicated row below the main status bar with branch / port details for every project.
- Mode 2 colour palette is themable via `@ccm-status-bg` / `-gutter-bg` / `-fg` / `-fg-dim`. Invalid colour values silently fall back to the defaults.
- Polling cadence tunable via `CCM_STATUS_INTERVAL` (default 5 s). Hook-driven `@ccm-permit-pending` keeps PERMIT-axis responsiveness independent of the polling rate.
- Bounded subprocess count: a single bulk `list-windows` query gathers all per-project tmux options once per cycle, so detection cost scales linearly with project count rather than quadratically.

### Notifications
- Desktop notifications via `@ccm-notify` (`permit` / `completed` / `all`), with sound options.
- macOS: when `terminal-notifier` is installed (`brew install terminal-notifier`), notifications use per-project `-group ccm-<project>` so a fresh notification replaces the previous one in Notification Center rather than accumulating. `osascript` is used as a fallback.
- `ccm clear-notifications` bulk-removes ccm notifications from macOS Notification Center, scoped to `ccm-`-prefixed group ids only — notifications from unrelated terminal-notifier scripts are left intact.
- Per-project dedup markers ensure concurrent projects never suppress each other's notifications.
- Grace window (`CCM_COMPLETION_GRACE_SEC`, default 3 s) absorbs the Stop hooks Claude Code fires at multi-turn tool boundaries, so the COMPLETED alert only arrives on a genuine completion.
- Linux: `notify-send` is used. There is no per-project dedup equivalent; pin to `@ccm-notify "permit"` to limit volume.

### Robustness
- **Hook log canary** — warns in `ccm status` and the dashboard footer when `~/.claude/hooks.log` exceeds 100 MB (the documented root cause of upstream silent hook delivery failure).
- **Setting canaries** — surface a warning when `disableAllHooks` or `allowManagedHooksOnly` is set in `~/.claude/settings.json`, since both disable every ccm hook silently.
- **Cluster-SHELL canary** — detects rapid SHELL transitions (3 in 10 minutes) and warns the user, surfacing the macOS silent-exit class of regression.
- **Setup version gate** — `ccm setup-hooks` hard-fails on Claude Code below v2.1.107 and on missing `claude` binary, instead of installing a partial hook set.
- **Multi-byte text safety** — `display_width()` / `truncate_to_width()` / `pad_to_width()` (in `ccm_render`) replace `len()` / f-string `<N` for terminal-column calculations, so CJK and emoji project names align correctly across the dashboard, `ccm status`, `ccm ports`, `ccm list`, and `ccm snapshot list`. All text-mode `open()` calls pass `encoding="utf-8"` explicitly. `ps_snapshot` and `tmux_cmd` decode subprocess output with `errors="replace"` so a truncated multi-byte process name (e.g. macOS `ps comm` slicing `⌘英かな`) cannot crash the detection cycle. `dashboard.py` initializes `locale.setlocale(LC_ALL, "")` at import so curses can render wide characters. CJK locale terminals can opt into 2-column rendering for East Asian Ambiguous chars via `CCM_AMBIGUOUS_WIDTH=2`.
- **Per-project exception barrier** in `build_project_list` — a bug in detection for one project leaves the others unaffected; the failing project carries forward its previous `@ccm_prev_state` while the rest of the loop continues.
- **PID reuse defence** in `read_session_info` — cross-checks Claude Code's recorded `startedAt` against the live process's etime-derived start time. A drift beyond `CCM_SESSION_INFO_AGE_DRIFT_SEC` (default 10 s) treats the json file as belonging to a recycled pid's prior session and skips it.
- **Silent-exception log** — `inject_status` and `dashboard._refresh_loop` route caught exceptions into `$TMPDIR/ccm-$UID/errors.log` (1 MB cap, rotates to `errors.log.1` once for ~2 MB total; `CCM_ERRORS_LOG_MAX_BYTES` overrides). Crashing the status refresh is still avoided, but the next failure of this class is debuggable without enabling `CCM_DEBUG_TRACE` in advance. Autosave failures (`_force_autosave`, `periodic_autosave`) and other best-effort writes route through the same channel.
- **`ccm errors [--clear]`** subcommand prints the silent-exception log in chronological order or clears it.
- **`ccm doctor`** aggregates dependency versions, hook installation, every canary above, per-project state and session_id, the silent-exception log count, and configuration paths into a single self-check command — the first thing to run when something feels wrong, and a drop-in artefact for bug reports.
- **Property-based invariants** (`tests/test_derive_invariants_pbt.py`) using `hypothesis`: `derive_state_from_events` is total over its input space, `pid_present=False` always yields SHELL, `raw=PERMIT` never overrules a visible modal, and empty event logs route through to the legacy fallback.

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
