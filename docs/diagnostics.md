# Diagnostics reference

See the [user guide](guide.md) for setup and everyday use. This reference covers detection details, tuning, and diagnostic procedures.

## State Detection

### Claude Code Hooks (Recommended)

`ccm setup-hooks` adds the following hooks to `~/.claude/settings.json`:

| Hook | Signal | Detects |
|------|--------|---------|
| `UserPromptSubmit` | BUSY | Prompt submitted → Claude is processing (including text generation) |
| `PreToolUse` | BUSY | Tool execution starting (solves multi-turn detection gap) |
| `PostToolUse` | BUSY | Tool execution completed — keeps BUSY held across post-permission gaps |
| `PostToolUseFailure` | BUSY | Tool execution failed |
| `SubagentStart` / `SubagentStop` | BUSY | Subagent execution start/end (parent agent still working) |
| `PreCompact` / `PostCompact` | BUSY | Context compaction is busy work |
| `Stop` / `StopFailure` | clears BUSY | Claude finished responding (signal file deleted) |
| `PermissionRequest` | PERMIT | Tool requires user permission |
| `Notification` | PERMIT / clears signal | Permission prompt or MCP elicitation dialog shown / idle notification (matchers: `permission_prompt`, `elicitation_dialog`, `idle_prompt`) |
| `SessionEnd` | SHELL | Claude Code session ended (/exit, Ctrl+D, etc.) |
| `PermissionDenied` | PERMIT | Auto mode denied an action (check `/permissions` to retry) |

> [!NOTE]
> Hook signals are written to `$TMPDIR/ccm-$UID/hooks/`. BUSY is cleared by `Stop`/`SessionEnd` or by process exit; if both the BUSY hook and the JSONL stay silent beyond `CCM_BUSY_HOOK_JSONL_WINDOW` (default 10 min), ccm stops trusting the stale signal and lets the state fall back to IDLE, so a missed `Stop` cannot strand a project in BUSY. PERMIT is released the same way: resolving a permission fires no hook upstream, so if a permit event is the newest one, the pane shows no modal, and the session log stays frozen beyond `CCM_PERMIT_MAX_TIMEOUT` (default 10 min), ccm stops trusting it and falls back to IDLE. A dialog still on screen is read from the pane directly and stays PERMIT no matter how long it waits. A BUSY left by an Esc interrupt (no `Stop` hook, the log frozen mid-tool) is released sooner: with an idle prompt on screen and the log frozen past `CCM_BUSY_STALE_RELEASE_SEC` (default 60 s), ccm defers to IDLE.

`ccm doctor` shows issues, checks that could not be completed, and next steps. If no issues are found in the completed checks, it prints one summary line. Use `ccm doctor --verbose` for normal results, versions, paths, all projects, session IDs, Codex bindings and log counts. Both modes run the same checks and retain diagnostic records.

ccm edits only its own hook entries in `~/.claude/settings.json`. A hook is ccm's when its command is a bare path to one of ccm's hook scripts in this ccm's `hooks/` directory, however the directory is spelled (through a symlink, or in another case on a filesystem that ignores case). Another tool's hooks are left exactly as they are — even one inside the same matcher entry as a ccm hook — and so is every other hook named like a ccm script, including those of a ccm that has since moved or been removed: from the settings alone they cannot be told apart from another tool's. `ccm setup-hooks`, `ccm remove-hooks` and `ccm doctor --verbose` name such hooks (doctor --verbose with the number of entries in each directory), so you can delete them by hand if you know they are left over from an older ccm. When `ccm doctor` can read the settings and finds none of the hooks in this ccm's `hooks/` directory, it says so instead of reporting them as installed. Its installed check does not verify that every event is registered.

### How each state is detected

When a pane is displaying a conversation parked in the background, ccm follows
`parkedJobId` in Claude Code's session registration and reads the background
session's hooks and transcript. This includes parking through the agents view
and returning to the conversation. If the target cannot be identified, ccm
initially keeps the pane BUSY (or PERMIT when a dialog is visible). Idle-screen
observations can release it without borrowing another conversation's completion.
The allowed gap follows `CCM_RECONCILE_INTERVAL` and tmux's `status-interval`.
With defaults, a 2 s dashboard cadence releases after 62 s; status-only detection
at 20 s intervals releases after 80 s. Longer configured intervals also work,
but each observation adds at most half of `CCM_BUSY_STALE_RELEASE_SEC` (default
60 s) to the evidence, so a single long wait cannot release the hold.
Visible work, a dialog, changed identity or clock, a new state notification
after release, or a gap beyond the configured tolerance restarts the wait.
The hand-off launch warning applies only to SHELL windows, not a conversation
already open in a pane.

Large housekeeping records after a reply do not count as activity. Transcript
reads search backwards, bounded to 1 MiB and 200 records per changed file,
so attachments and history snapshots can be skipped without hiding the last
reply within that limit. Unchanged files reuse cached results.

After a `Stop` with an unreadable or unknown stop reason, an idle prompt can
release BUSY once both the Stop and any readable activity are older than
`CCM_BUSY_STALE_RELEASE_SEC` (default 60 s). A known pending tool or new prompt
keeps BUSY; visible work and permission dialogs also prevent this release.
Auto-exit still requires 600 s of sustained IDLE by default.


| State | Method | Details |
|-------|--------|---------|
| **SHELL** | Process check | No `claude` process found among window's child processes |
| **IGNORED** | Pane option + process check | Every pane hosting `claude` is excluded via `@ccm_ignore` and no visible pane hosts one — ccm deliberately cannot see the window's Claude, so SHELL/DOWN ("Claude is not running") would be a claim without basis. Not a rung of the PERMIT > BUSY > IDLE > SHELL ladder but a visibility verdict, checked before SHELL/DOWN is allowed to stand. Renders `⊘` (dim). `ccm send` is refused and points at `ccm unignore`; auto-exit never acts on it (it only ever acts on IDLE) |
| **BUSY** | Event log + JSONL stop_reason | Primary: the per-session event log (`hooks/<sessionId>.events.jsonl`, keyed on Claude Code's session UUID) appended by every BUSY-class hook (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStart`/`Stop`, `PreCompact`/`PostCompact`). `derive_state_from_events` evaluates the tail as a pure function and returns BUSY while the most recent entry is start-class. Hook silence is bridged by JSONL `stop_reason`: a fresh `tool_use` keeps BUSY across the tool-turn Stop boundary; an `end_turn` / `max_tokens` / `stop_sequence` newer than the latest event releases to IDLE within seconds. Claude Code housekeeping records (`system/away_summary`, `turn_duration`, `attachment/task_reminder`, `permission-mode`, `file-history-snapshot`, `last-prompt`) are filtered from JSONL activity so recap and startup housekeeping do not register as fresh activity |
| **IDLE** | Event log + capture-pane | The event log's most recent entry is end-class (`stop`, `notify_idle`, `notify_permit`-resolved), the input prompt `❯ ` is visible, and no PERMIT footer matches. With hooks disabled the legacy fallback uses process tree + prompt visibility only |
| **PERMIT** | Hook + capture-pane fallback | Primary: `PermissionRequest` / `PermissionDenied` / `Notification` (permission_prompt) hooks. Fallback: capture-pane match on the modal footer (`Esc to cancel · Tab to amend` for permission dialogs; `Enter to confirm · Esc to <verb>` for confirmation modals, including the v2.1.144 `/model` form `Enter to confirm · d to set as default for new sessions · Esc to cancel` where intermediate `· <action key>` segments are tolerated) — catches sessions where hooks have stopped firing Leading adjustment/toggle hints before Enter are supported through an explicit list of verified confirmation forms, including `/autocompact` and `/effort` (`←/→ to adjust`) and `/fast` (`Space to toggle`). The first two were captured on 2.1.280; the available `/fast` form is source-derived and has not been captured live. Unregistered leading hints remain a detection limit. Navigation-only `/permissions` footers, including the form with `Enter to select`, remain outside PERMIT; see [the state-machine reference](state-machine.md) for forms and verification limits. |
| **Completion (`* elapsed`)** | Display layer | Transient marker: shown for 30s after BUSY/PERMIT → IDLE transition, then clears. Asterisk renders green (drawing the eye to the just-completed transition); the elapsed time is dim |
| **Multi-pane (`[N]`)** | Window inspection | Marker on every renderer (dashboard, status bar, `ccm status`) when a window holds more than one tmux pane (Agent Teams, casual splits, leftover orphan panes). Brackets dim, digit cyan. Lets you spot windows whose aggregated state may belong to a non-active pane. See [Using with Agent Teams](guide.md#using-with-agent-teams) for related details (sliver protection and PERMIT auto-focus) |
| **Permission mode (`{mode}`)** | Hook payload | Display-only badge from the `permission_mode` field Claude Code attaches to hook payloads; the newest value is shown as the `MODE` column in `ccm status` and a `{mode}` badge after the project name in the dashboard (`{accept}`, `{plan}`, `{auto}`, `{dontAsk}`, `{bypass}`). The everyday `manual` mode is suppressed in the dashboard. `{bypass}` renders in warning color. Modes that auto-resolve dialogs never produce PERMIT — the badge preempts misreading that silence as broken detection. Never consulted by state detection |
| **Ignored (`⊘`)** | Pane option | Dim `⊘` on the dashboard and `ccm status` row when the window has a `CCM_IGNORE`'d pane — a session ccm deliberately does not track (see [Running a second model as a sidekick](guide.md#running-a-second-model-as-a-sidekick-ccm_ignore)). Present-but-untracked; not a state |

## Troubleshooting

### Settings that stop hooks

`ccm doctor` and `ccm status` name `managed-settings.json` when its
`allowManagedHooksOnly` value blocks ccm's user-scope hooks. In Claude Code
2.1.282, `true` and `"true"` enable this lock; `false`, `"false"`, `null`, and
an absent key do not. Other non-boolean values (including `0`, `1`, `"yes"`,
empty strings, arrays, and objects) enable the lock until corrected.
A warning distinguishes a non-boolean value from a literal `true`.

`disableAllHooks` is an exception: Claude Code 2.1.282 ignores non-boolean
values for this key. ccm reports the invalid managed value so it can be
corrected, without claiming hooks are disabled. A literal `true` disables
configured hooks and the custom statusLine. Ask your administrator to correct
invalid managed values and use Claude Code's `/status` to check the policy
in force. ccm reads the managed settings file, not MDM or console policies.
The checks for user and project files are unchanged: only literal `true`
triggers `disableAllHooks`; `allowManagedHooksOnly` is managed-only.

### Detection has gone quiet (hook-silence canary)

Claude Code sometimes stops firing hooks partway through a session. ccm's
precise detection depends on them, so when they stop it falls back to coarser
signals and states can lag or stick. The cause is upstream, not ccm, but from
the outside the two look identical.

An opt-in canary reports the difference:

```tmux
set -g @ccm-hook-silence on
```

It warns in `ccm status`, `ccm doctor` and the dashboard footer when a
session's transcript shows recent activity that its hook log never recorded —
work that happened with the hooks asleep. Off by default: a threshold tuned
wrong can only mislead someone who asked to watch it.

Thresholds are `CCM_HOOK_SILENCE_FRESH` (how recent the transcript activity
must be, default 90 s) and `CCM_HOOK_SILENCE_GAP` (how far the hook log must
lag it, default 120 s). Each firing is also appended to
`~/.local/share/ccm/state/hook-silence.log`, rate-limited per project, and
`ccm doctor --verbose` reports the count.

Restarting the affected Claude session restores the hooks.

### Every project frozen at the same state

If **all** projects are stuck at the same state (e.g. all BUSY, no longer updating after refresh), the detection cycle itself may have hit a silent exception. Check the log:

```bash
ccm errors
```

Each line is a previously-swallowed exception with timestamp, scope, and traceback. An empty log (`No silent-caught errors logged.`) means the cycle is healthy. If entries keep accumulating, the most recent traceback identifies the failing call site. `ccm errors --clear` removes both the active log and the rotated `errors.log.1`.

## Using with agent view (background sessions)

### Data sources

The reader joins two files, both written by the daemon (read-only on the ccm side):

- `~/.claude/daemon/roster.json` — currently-active workers (pid, sessionId, cwd, cliVersion, dispatch metadata). Sessions are removed from this file after ~1 hour idle (`settled (done)`), matching what `claude agents` itself shows.
- `~/.claude/jobs/<short>/state.json` — per-session live state (`working` / `blocked` / `done` / `failed` / `stopped`; `needs_input` and `idle` from earlier releases are still read), the ask when blocked (`needs`), tempo, in-flight task counts, and an auto-generated name.

Missing files, malformed JSON, or a daemon-down state all gracefully resolve to "no background sessions" — agent view's absence never crashes the dashboard.

## Environment Variables

ccm exposes several tuning knobs via environment variables. Defaults are chosen to work well for most users; adjust only if you observe a specific problem. Set them in your shell rc file (e.g. `~/.zshrc`) before tmux starts.

### Detection timing

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_BUSY_HOOK_JSONL_WINDOW` | `600` (seconds) | Combined-stale fallback window in the event-log path: when both the latest event AND the JSONL are older than this, derive defers to the legacy fallback (which resolves to IDLE). Catches abandoned sessions and other long-tail upstream silences |
| `CCM_JSONL_HOOK_GAP_TOLERANCE` | `60` (seconds) | Recap-phantom discriminator (legacy `hook_fresh_busy` rule). A BUSY hook that fired more than this many seconds AFTER the last real conversation activity is rejected as phantom (e.g. upstream `away_summary` recap). Same window also gates the Esc-release / silent-completion freshness check in derive |
| `CCM_COMPLETED_AT_TIMEOUT` | `30` (seconds) | How long the `* elapsed` "recently completed" marker stays visible in the dashboard after a BUSY/PERMIT → IDLE transition |
| `CCM_COMPLETION_GRACE_SEC` | `3` (seconds) | Grace period between a Stop hook firing and the COMPLETED desktop notification. Claude Code fires Stop at every turn boundary (including mid-tool-use); ccm waits this long before alerting so a subsequent PreToolUse / UserPromptSubmit can cancel the pending notification. Lower = faster alerts but higher risk of notifying mid-conversation |
| `CCM_PERMIT_MAX_TIMEOUT` | `600` (seconds) | Stale-permit release: when a permit event is the newest one, the pane shows **no** modal, and the session log has been frozen this long, ccm stops trusting the permit and lets the state fall back to IDLE. Resolving a permission fires no hook upstream, so without this a permission answered (or dismissed with Esc) minutes ago could hold `⚠ PERMIT` forever. A modal still **on screen** is detected from the pane itself and is never released, however long you leave it |
| `CCM_BUSY_STALE_RELEASE_SEC` | `60` (seconds) | Stale-BUSY release: how long a session interrupted with Esc mid-tool (no `Stop` hook, session log frozen at `tool_use`, idle prompt on screen) may stay `BUSY` before ccm defers to IDLE. A flicker-prevention window — the safety net for a genuinely working session with a frozen log (e.g. a long silent build) is `CCM_IDLE_EXIT_TIMEOUT`, which requires 10 minutes of sustained IDLE before auto-exit |
| `CCM_SPINNER_STALE_RELEASE_SEC` | `30` (seconds) | How long the spinner's elapsed-time footer may stand still before ccm stops believing it marks a running turn. A live footer ticks every second; a frozen frame (session hung after rendering), a transcript line quoting a footer, and a footer under Claude Code's reduced-motion setting are static, and past this window they stop holding the window `BUSY` — which matters because raw `BUSY` has no other release path |
| `CCM_THINKING_HINT_RESAMPLE_SEC` | `0.5` (seconds) | Gap between the two extra captures ccm takes when a pane's spinner footer shows a thinking hint but no elapsed time (a narrow pane in a long thinking phase). Claude Code animates the spinner glyph on a fixed two-second cosine cycle; two captures half a second apart can still land on the same frame, but captures at half a second and a full second apart cannot both, which is why two are taken. The glyph moving within one pass is what marks the turn as running there, since across passes the poll interval can sit on the animation's period — and so would this interval, if set to a multiple of two seconds. A frozen frame, or the fixed `●` drawn under Claude Code's reduced-motion setting, never differs, and such a pane reads idle |
| `CCM_IDLE_EXIT_TIMEOUT` | `600` (seconds) | How long a Claude Code session can be IDLE before `x` (exit all) targets it, and how long before auto-exit triggers |
| `CCM_IDLE_PROMPT_GUARD_SEC` | `60` (seconds) | Guard in `on-notification.sh` for the `idle_prompt` Notification: idle_prompt arrives 10–60+ s late (anthropics/claude-code#5186), so a BUSY signal younger than this may have been written by work that started AFTER the notification was generated — deleting it would drop an actively working session to IDLE (and feed auto-exit's kill path). Signals younger than the guard are kept; older ones are cleared as before. Set to `0` to opt out and restore the old always-delete behaviour |
| `CCM_IGNORE` | unset | Launch-time flag, not a tunable: `CCM_IGNORE=1 claude` starts a session that ccm ignores entirely (see [Running a second model as a sidekick](guide.md#running-a-second-model-as-a-sidekick-ccm_ignore)). Toggle an already-running session with `ccm ignore` / `ccm unignore` instead |
| `CCM_STARTUP_GRACE_SEC` | `60` (seconds) | Window during which the legacy `startup_transient_raw_busy` rule demotes raw=BUSY to IDLE when no hook signal is present — covers Claude's MCP-loading phase after `claude --continue`, which typically completes in 10–30 s |
| `CCM_SLIVER_HEIGHT_THRESHOLD` | `4` (rows) | Minimum tmux pane height for a pane to participate in window-state aggregation. Panes shorter than this cannot render Claude's `❯` prompt, so capture-pane–based detection cannot tell them apart from a genuinely BUSY pane. Raise if you have legitimate small Agent Teams panes that should still count; lower (down to 1) to disable the filter entirely |
| `CCM_HOOK_CMD_TIMEOUT` | `5` (seconds) | Timeout Claude Code applies to each ccm hook invocation, written into the hook entries by `ccm setup-hooks`. The field is in seconds — Claude Code's own default is 60. Earlier versions of ccm wrote `5000`, meaning milliseconds, which Claude Code read as 5000 seconds. On an existing install, `ccm setup-hooks` sets the timeout of ccm's own hook commands to this value and changes nothing else — other tools' hooks, even in the same matcher entry, are left as they are — and `ccm doctor` names an install still carrying the old value. A value you set here is written as given, so one set in milliseconds must be changed to seconds (or unset) before running it |
| `CCM_SPOOL_TTL_SEC` | `3600` (seconds) | How long a message queued by `ccm send` (store-and-forward) stays deliverable. Past the TTL it moves to `expired/` and shows up in `ccm status` / `ccm doctor` instead of arriving — a stale instruction delivered late executes out of context. See [The spool](guide.md#the-spool-store-and-forward) |
| `CCM_START_WAIT_SEC` | `10` (seconds) | How long `ccm send --start` polls a SHELL-state target for IDLE after sending `claude --continue`, before refusing the send. Tuned for the two real cases: a normal resume reaches IDLE in 1-5 s, while an auto-`/compact` on a long-session resume can keep BUSY for 10-60+ s — no reasonable wait gets the message through anyway, so refusing at 10 s gives the operator a useful response time. Progress is printed once per second when run interactively so the wait is visible. Raise if your environment routinely needs more |

### Runtime directories

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_TMP_DIR` | `${TMPDIR:-/tmp}/ccm-$UID` | Per-user runtime directory: hook signals, notification markers, port/git caches, popup-session marker. Override to isolate a demo / test session from your normal ccm runtime |
| `CCM_DATA_DIR` | `~/.local/share/ccm` | Snapshot files and other persistent state. Override paired with `CCM_TMP_DIR` for fully isolated environments |

### Canary thresholds

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_HOOKS_LOG_WARN_BYTES` | `104857600` (100 MB) | Size threshold for the `~/.claude/hooks.log` bloat canary. Claude Code does not rotate this file and bloated logs silently disable hook firing (anthropics/claude-code#16047) |
| `CCM_SHELL_CLUSTER_COUNT` | `3` | How many transitions into SHELL within the window trigger the cluster-SHELL warning. The warning reports what was seen — Claude left the pane repeatedly — and names the causes that look identical from here (an update relaunching in place, manual exits, unexpected exits); anthropics/claude-code#48069 (macOS silent exit) is offered as one known cause of the last, not as a diagnosis |
| `CCM_SHELL_CLUSTER_WINDOW` | `600` (seconds) | Time window for counting SHELL transitions |
| `CCM_ERRORS_BURST_THRESHOLD` | `20` | How many `errors.log` records within the burst window triggers the silent-fail-loop canary. A poll-cycle bug (e.g. an exception fired by every `inject_status` refresh) accumulates roughly 30 records/min, so this threshold reliably distinguishes a runaway loop from one-off noise |
| `CCM_HOOK_SILENCE_FRESH` | `90` (seconds) | Opt-in hook-silence canary: how recent a session's transcript activity must be before a lagging hook log counts as silence |
| `CCM_HOOK_SILENCE_GAP` | `120` (seconds) | How far the hook log must lag that activity before the canary fires |
| `CCM_HOOK_SILENCE_LOG_INTERVAL` | `600` (seconds) | Minimum gap between logged firings for one project, so a long episode reads as a few lines rather than hundreds |
| `CCM_HOOK_SILENCE_LOG_MAX_BYTES` | `1048576` (1 MB) | Size cap on the firing log before it rotates |
| `CCM_ERRORS_BURST_WINDOW` | `300` (seconds) | Time window for counting silent-fail records |

### Debug tracing

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_DEBUG_TRACE` | (unset) | Path to a JSONL trace file. When set, every slow-path detection scan (`inject-status`, dashboard, `ccm status`) appends a record with the full `DetectionContext`, matched rule, and resolved state. See [Detection-behaviour debugging](#detection-behaviour-debugging). Remember to set it via `tmux set-environment -g`, not shell `export`, so the tmux-spawned subprocesses see it |
| `CCM_SEND_TRACE` | (unset) | When truthy, appends every `tmux send-keys` call `ccm send` / `ccm sidekick-send` makes to `$CCM_TMP_DIR/send-trace.log`, for diagnosing a delivery a recipient says never arrived |
| `CCM_TRACE_MAX_BYTES` | `104857600` (100 MB) | Size cap for the `CCM_DEBUG_TRACE` log. Once exceeded, a single `{"event":"trace_cap_reached", ...}` sentinel is written and subsequent appends are skipped, so a forgotten trace cannot fill the disk |
| `CCM_TRACE_ONLY_DIFF` | (unset) | When set to a truthy value, restricts `CCM_DEBUG_TRACE` writes to rows where the legacy and event-log derivations disagree. Lets long-running traces stay small. No effect when `CCM_USE_EVENT_LOG=off` (no event-log state to diff against) |
| `CCM_USE_EVENT_LOG` | `auto` | `auto` (default) commits the event-log state when [`derive_state_from_events`](../lib/ccm_activity.py) returns a non-`None` answer; otherwise legacy `DETECTION_RULES` (in [`lib/ccm_rules.py`](../lib/ccm_rules.py)) takes over. `off` (or `0` / `no` / `false`) is the diagnostic kill-switch — legacy-only, no event-log read. Anything else resolves to `auto` |

### Cache TTLs

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_CACHE_TTL` | `30` (seconds) | Git branch / port detection cache lifetime |
| `CCM_JSONL_CACHE_TTL` | `30` (seconds) | JSONL path resolution cache lifetime |

### Display and observability

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_ATTENTION_WAITING_TTL_SEC` | `3600` (seconds) | How long a sidekick's unanswered attention marker survives before ccm drops it, for the case where the agent exits without resolving it |
| `CCM_ATTENTION_RESOLVED_GC_SEC` | `300` (seconds) | How long a resolved marker is kept before collection, so a slow consumer still sees it |
| `CCM_AMBIGUOUS_WIDTH` | `1` | Fallback for `@ccm-ambiguous-width`, for running ccm outside tmux. Prefer the tmux option: an environment variable reaches only one of the two parents that render the bar — see [Ambiguous glyph width](#ambiguous-glyph-width). Read once per process |
| `CCM_ERRORS_LOG_MAX_BYTES` | `1048576` (1 MB) | Size cap for `$TMPDIR/ccm-$UID/errors.log` (the silent-exception log). At the cap, the active log rotates to `errors.log.1` and a fresh log starts (total disk use ~2 × cap). View with `ccm errors`; clear with `ccm errors --clear` |
| `CCM_SESSION_INFO_AGE_DRIFT_SEC` | `10` (seconds) | Drift tolerance for the session_info pid-reuse check. When `read_session_info` is given a `ps` snapshot, it cross-checks Claude Code's recorded `startedAt` against the live process's etime-derived start time; a discrepancy beyond this tolerance means the json file is from a recycled pid's prior session and is rejected (caller falls through to legacy detection). 10 s comfortably covers normal clock drift / NTP corrections / the few-second gap between fork and Claude writing session_info |
| `CCM_STATUS_INTERVAL` | `5` (seconds) | Target tmux `status-interval` — how often the status bar re-renders. On plugin load, ccm lowers `status-interval` to this value if your current setting is higher (it never raises it). Set via `tmux set-environment -g` before the plugin loads, not shell `export` — see [Status refresh interval](#status-refresh-interval) |
| `CCM_RECONCILE_INTERVAL` | `20` (seconds) | How often the periodic status pass does a **full** run. tmux fires `#(ccm inject-status)` once per `status-interval` — every second if you show a seconds clock — and a full pass costs ~24 processes, so the invocations in between are rate-limited to a shell fork. State changes do not wait for it: the hooks push an immediate refresh on every transition. This only paces what fires no hook — a git branch switch, a new listening port, a stale-BUSY release crossing its window. Keep it below `CCM_BUSY_STALE_RELEASE_SEC`, since that release is a threshold crossing that nothing re-evaluates until a reconciliation runs |
| `CCM_RESIZE_SETTLE` | `0.4` (seconds) | How long after the last `client-resized` event ccm waits before re-laying the status bar. The bar's layout is baked from the terminal width at render time, so a resize leaves it laid out for the old width until something re-renders; tmux fires the event for every step of a drag, and this window collapses the burst into one render at the size the drag ended on. Raise it if your terminal emits resize events slowly enough that the bar re-lays mid-drag |
| `CCM_AUTO_EXIT_LOG` | `$CCM_DATA_DIR/state/auto-exit.log` | Where ccm records the sessions it closed itself. Claude Code reports an auto-exit as `SessionEnd` with reason `prompt_input_exit`, which is exactly what a person typing `/exit` produces — so this file is the only place the two can be told apart afterwards. One JSON record per exit: timestamp, project, session id, idle seconds. `ccm doctor --verbose` shows the count |
| `CCM_AUTO_EXIT_LOG_MAX_BYTES` | `1048576` (1 MB) | Size cap for the auto-exit log; at the cap it rotates to `auto-exit.log.1`. One record per auto-exit puts the cap decades away — it exists so a pathological loop cannot fill the disk |
| `CCM_AUTO_EXIT_DECLINED_LOG` | `$CCM_DATA_DIR/state/auto-exit-declined.log` | Where auto-exit records the exits it declined or could not confirm: the pane showed the agent view (nothing typed), the pane could not be read (nothing typed), or `/exit` detached an attached background session instead of ending it. One JSON record per event — timestamp, project, session id, outcome; never pane content. Kept apart from `auto-exit.log` so that file keeps counting only real exits. `ccm doctor --verbose` shows the count |
| `CCM_AUTO_EXIT_DECLINED_LOG_INTERVAL` | `600` (seconds) | Rate limit for the declined log, per project and outcome: a window that stays in the same state is recorded once per interval, not once per poll |

**Reaching every reader.** ccm runs from three places with different environments: tmux's `#()`, the hooks Claude Code spawns, and your own shell. A variable set only in your shell is read by `ccm status` and the dashboard but not by the status bar, so the same window can be described two ways at once. Put anything that changes how state is computed or drawn into tmux's environment (`tmux set-environment -g NAME value`, then restart the bar) rather than a shell profile — or, where a tmux option exists for it, use that instead.

### Tuning examples

```bash
# Longer "* elapsed" marker visibility after completion
export CCM_COMPLETED_AT_TIMEOUT=60

# Earlier hooks.log bloat warning (10 MB)
export CCM_HOOKS_LOG_WARN_BYTES=10485760

# Lower polling cost on slow / battery-bound machines (tmux env, read on plugin load)
tmux set-environment -g CCM_STATUS_INTERVAL 10

# Diagnostic kill-switch: bypass the event-log path entirely
export CCM_USE_EVENT_LOG=off
```

### Interactions with Claude Code's own environment variables

A few undocumented Claude Code env vars overlap with ccm's behavior. If you set both, be aware of the interaction:

| Claude Code env | Interaction with ccm |
|-----------------|----------------------|
| `CLAUDE_CODE_EXIT_AFTER_STOP_DELAY` | Makes Claude Code exit itself some seconds after a Stop event. This duplicates `CCM_IDLE_EXIT_TIMEOUT` — pick one path. If both are set, whichever fires first wins, and the other becomes a no-op on a SHELL-state window |
| `CLAUDE_CODE_IDLE_THRESHOLD_MINUTES`, `CLAUDE_CODE_IDLE_TOKEN_THRESHOLD` | Claude Code's own idle detection. When it fires, your SessionEnd hook runs and ccm observes the window transition to SHELL (no conflict, just additional auto-exit paths you may not expect) |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | Upper bound Claude Code gives the SessionEnd hook (ccm's `on-session-end.sh`). ccm's hook is one signal-file write — comfortably within any reasonable value |
| `CLAUDE_CODE_NO_FLICKER` | Already handled by ccm. Preview capture falls back to `tmux capture-pane -a` when the pane uses the alternate screen buffer |
| `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` | No conflict. If you dislike Claude Code rewriting your tmux window title, set this to `1` in your shell rc — ccm's own window naming (state icons) takes precedence either way |
| `DISABLE_UPDATES` | No conflict. Blocks all Claude Code update paths including manual `claude update` (stricter than `DISABLE_AUTOUPDATER`). Useful if you pin Claude Code versions in snapshots and want to avoid surprise upgrades mid-session |
| `CLAUDE_CODE_HIDE_CWD` | No conflict. Hides the working directory in Claude Code's startup logo. ccm already displays the directory under each project in `ccm status` and the dashboard, so you can safely hide it from the in-pane logo to reduce visual redundancy |

These are not required for ccm to work. They are listed only so that users who customize Claude Code can predict overlaps.

## Known Limitations

### Status refresh interval

ccm's status bar updates are driven by tmux's `status-interval`. On load, the plugin automatically lowers it to 5 seconds (from tmux's default of 15) if your current setting is higher — it only ever lowers the value, never raises it. To use a different target, set `CCM_STATUS_INTERVAL` in tmux's environment before the plugin loads:

```bash
tmux set-environment -g CCM_STATUS_INTERVAL 10   # poll every 10 seconds instead
```

Lower values increase CPU usage slightly.

### Narrow panes, long thinking, reduced motion

Claude Code's spinner footer drops its elapsed time when the pane is too narrow for both the time and the thinking hint, and after 45 seconds of a thinking phase the hint grows (`deep in thought`). The elapsed time is what ccm reads as the sign of a running turn; without it, ccm looks for the spinner glyph moving between two captures half a second apart. With Claude Code's reduced-motion setting the glyph is a fixed `●`, so the pane has nothing that moves. The elapsed time is not dependable there either: its periodic updates can stop, so the displayed time is not evidence of a running turn. Measured against 2.1.278, a reduced-motion footer held the same time for over 30 seconds of real work while the animated one ticked every second; another redraw (a resize, for instance) brings it up to date. ccm treats a clock that has not moved for `CCM_SPINNER_STALE_RELEASE_SEC` as static, so this reading — the pane-reading fallback — goes idle once that window passes, whether or not the footer shows a time. Hooks are the path that carries such a session: while they are firing, the session's state comes from them and stays `BUSY`. Hooks hold the session BUSY only for as long as their own window allows, and a false idle that lasts long enough is the reading auto-exit acts on: a thinking phase outlasting the idle timeout can get the session exited. Auto-exit is one global setting — `@ccm-idle-timeout` in minutes, `0` to turn it off. Widening the pane helps the narrow-pane case, but not reduced motion, where the time stands still at any width: there, keep hooks installed, and raise the timeout or set it to `0`.

### Ambiguous glyph width

Box drawing characters, geometric shapes, and every Nerd Font icon draw one column or two depending on the terminal and its font. Unicode calls them "Ambiguous" and does not say which; characters in the Unicode PUA (U+E000–U+F8FF) cannot say at all, since the codepoint carries no width and only the font knows.

Not knowing is expensive in mode 1's left placement. tmux positions `status-right` counting these as one column, so if the terminal draws them as two, `status-left` runs past where tmux placed it and paints over the first entry — the highest-priority one, which is exactly what left placement exists to show. So by default ccm reserves room for the worse case.

On a terminal that draws them narrow, that reservation is never used, and in left placement the unused columns stay on screen as empty space after `status-left` — around 12 columns with a theme that uses such glyphs freely.

If you know what your terminal does, say so and the reservation stops:

```bash
tmux set -g @ccm-ambiguous-width 1   # drawn narrow (most non-CJK terminals)
tmux set -g @ccm-ambiguous-width 2   # drawn wide (CJK locale terminals)
```

Setting `1` is not the same as leaving it unset even though both count a glyph as one column. Unset means "unknown", and ccm hedges; `1` means "narrow", and it stops.

The change takes effect from the next render, not the current one: each render resolves the answer once and keeps it, so a bar already on screen was drawn with the previous value. Wait a tick before judging whether it worked — and longer after `source-file`, which drops the bar back to your theme's own `status-right` until ccm re-injects, up to `CCM_RECONCILE_INTERVAL` later. What you see right after a reload is not the new setting failing; it is ccm not having drawn yet.

Use the tmux option rather than the `CCM_AMBIGUOUS_WIDTH` environment variable, which is kept only for running ccm outside tmux. The status bar is rendered by two different parents — tmux's `#()` and the hooks, which Claude Code spawns — and an environment variable set with `tmux set-environment` reaches the first but not the second. Since the hook-driven render is the one that is never rate-limited, a declaration made that way is mostly overruled, and the bar alternates between two layouts. A tmux option answers the same to whoever asks. If both are set, the option wins.

To find out which your terminal does, put a box drawing character next to a plain one and see whether the columns line up — or simply set `1`, and if the first entry loses a character, set `2` instead.

### Debugging

To check ccm's current state:

```bash
ccm status                    # show all projects with state
ccm tree                      # show full hierarchy
tmux show-option -gv status-right   # inspect status-right content
tmux show-option -gqv @ccm-status-line  # current mode (0/1/2)
```

#### Detection-behaviour debugging

If a project shows BUSY when you expect IDLE (or vice-versa), use one of the two tracers:

**Live, per-project trace** — read-only, does not modify state:

```bash
# In a separate pane, then reproduce the problematic event in the main pane
ccm debug trace <project-name>           # default 0.3 s interval
ccm debug trace <project-name> 0.5       # or specify interval
```

Trace is read-only: it does not advance the saved unresolved-idle history.
`idle_credit=stored->candidate` shows the saved evidence and what a detection
commit at that instant would record; `gap` shows the allowed observation gap.
Keep periodic status detection or the dashboard running to observe committed
progress. Tracing alone does not replace those detection passes.


The target can also be a tmux pane, window, or `session:index` that ccm does
not manage, which is how you observe a throwaway session started for an
experiment:

```bash
ccm debug trace %42                      # a pane
ccm debug trace @7                       # a window
ccm debug trace probe:0                  # session:index
```

Prefer this over reading the hook event logs by hand. Those files are keyed on
Claude Code's session id, so a glob such as `$TMPDIR/ccm-$UID/hooks/*.events.jsonl`
sums every session running at that moment and silently attributes another
session's events to the one under test. Tracing the pane keeps the observation
scoped to a single session. A registered project name always wins over a tmux
target, so existing commands keep their meaning.

Each line shows the full detection context, the rule that matched, and the resolved state:

```
19:48:55  raw=IDLE  prev=IDLE  hook=-,-  pid_age=653  jsonl=6883,end_turn  default[-] → IDLE [WRITE]
```

The `rule_name[phase]` column shows the matched rule and its session-lifecycle phase (`shell` / `startup` / `midturn` / `between_tools` / `idle` / `permit`, or `-` for genuine catch-all passthroughs like `default`). Ctrl-C to stop. Safe to run alongside the live dashboard.

**Whole-pipeline trace** — env var, captures every detection scan across all projects:

```bash
# Set on the tmux SERVER (not just your shell). inject-status runs as
# a subprocess of tmux and inherits environment from the server at
# its start — plain `export` in your shell won't reach it.
tmux set-environment -g CCM_DEBUG_TRACE /tmp/ccm-trace.jsonl
# Wait one status-interval tick, then reproduce the event.
# Slice with jq by window target or state:
jq -c 'select(.target=="0:20")' /tmp/ccm-trace.jsonl | tail -50
jq -c 'select(.state=="BUSY")' /tmp/ccm-trace.jsonl | tail -20
# Remove the env var when done — the file grows with every scan.
# (The log also auto-suspends at 100 MB; override with
#  CCM_TRACE_MAX_BYTES if you need a larger cap.)
tmux set-environment -gu CCM_DEBUG_TRACE
```

Both tracers record the same fields so output is interchangeable between them. Note that `CCM_DEBUG_TRACE` captures only the slow path (the decisions that actually write `@ccm_prev_state`); the statusline fast path is read-only and not traced.

To reset ccm state completely:

```bash
rm -rf "${TMPDIR:-/tmp}/ccm-$(id -u)"
tmux source-file ~/.tmux.conf
```

## Snapshot checkpoints

Format v2 retains v1's `projects` and each entry's `name`, `dir` and `auto_start_claude`. Each window's `restore` records its unzoomed layout, width, height, window order, zoom, pane slots, cwd, role, agent kind, ignore intent, active slot and primary Claude slot. Numeric IDs inside the layout and `layout_id` only associate saved geometry with slots; they are not pane identities after restart.

Roles describe observations at save time. Explicit ignore intent is recorded even when the pane has become a shell. Only a uniquely observed non-ignored Claude pane becomes primary; ambiguity leaves the primary slot null. Shells do not imply a past agent. `checkpoint` records scope (one session), completeness, `sealed` and interrupted project names with PERMIT/BUSY; capture time is the top-level `created`. Preparation reads current detection; ordinary saves record the latest cached detection state. Screen contents, drafts, approval details, command arguments, environment values and conversation IDs are omitted. Project names and cwd are stored.

All writers collect under one lock and compare window and pane inventories before and after capture. Changed topology, failed collection and empty inventories preserve the checkpoint. Temporary files are read back, fsynced and atomically replaced. Protection blocks every automatic writer and manual `_autosave` saves/deletion. Running `prepare-logout` again explicitly replaces the protected checkpoint; `--cancel` releases protection while retaining its contents. Saving configuration does not stop processes or transfer approvals.

One fixed `_autosave.prev` contains the validated checkpoint preceding the last successful replacement. Identical, empty and failed saves, and releasing protection, do not rotate it. It is absent from `snapshot list`. Files use mode 0600 and the snapshot directory uses 0700. An interrupted `.snapshot-transaction` causes the next writer to roll back both files under the lock before proceeding. If recovery itself encounters I/O errors, the command exits nonzero and retains the transaction record. Fix the disk problem and retry. `ccm doctor --verbose` reads protection, interrupted projects and backup availability; pending transactions and unreadable checkpoints also appear in ordinary doctor output.

To use the backup, open the snapshot directory (default `~/.local/share/ccm/snapshots`; `CCM_SNAPSHOT_DIR` takes precedence, otherwise under `CCM_DATA_DIR`). Copy `_autosave.prev` to an unused name such as `recovery.json`, set the copy's mode to 0600, then run `ccm start recovery`. v2 loading restores splits, cwd, roles, active panes and zoom as shells. Full success releases the loaded checkpoint’s seal; failure retains it. v1 still creates one window per project.

v1 remains readable. Unknown versions are refused before any window changes. **Older ccm ignores v2 additions when loading and can overwrite them with v1 during its subsequent autosave. Older ccm also ignores sealed protection.** Copy the checkpoint and backup elsewhere before downgrading. Stop older ccm processes before relying on the new protection. Protection covers current ccm writers; external file editing and sync are outside its control. A configured directory inside a synced location will sync these records too.


### Restore validation and reserved roles

Restoration holds the common save lock and preflights all directories and window conflicts. Registered windows must match both name and base cwd. An overlapping name or any saved cwd in an unregistered window holds that project back without adoption; problems of the session or the checkpoint itself stop the whole restore. The saved layout tree is validated and scaled to the actual window size, with a 2×2 minimum per pane. Every old pane ID is remapped to its new ID, including when IDs partially overlap.

`.restore-state` is one fixed progress record. It checks `@ccm_restore_job` ownership and verifies counts, positions and cwd even when a split succeeded but its reply was lost. User changes that do not match are left intact. The source checkpoint and backup remain until every window is restored; autosave, prepare and deletion are paused too. `@ccm_restore_pending` excludes windows not yet published from detection, attach, send and auto-exit. Each window is published right after its own validation, and the record notes when its publication starts, so a stop part way through is finished on retry without rewriting a window that was already released. Windows held back and their reasons stay in the record; a retry handles only them. Once none is left, only the seal is released; the loader does not recapture and replace the source manifest.

Window option `@ccm_restore_managed` makes a unique primary reservation constrain launch and exit selection; pane option `@ccm_restore_role` stores role/agent/ignore intent. Reservations never count as PERMIT/BUSY/IDLE or live ignore state. Positive observation of a different agent replaces stale intent with a readable reservation; waiting shells retain it. Explicitly cleared roles remain `manual` even when an agent is observed, and survive snapshots and restores. `ccm roles [target] --clear` marks a pane for manual launch and exit, and `ccm unignore` also clears reserved ignore intent. Launch an ignored Claude sidekick manually with `CCM_IGNORE=1` before its first hook.

A successfully read empty role means **no reservation**. This can be a newly split pane or a pane whose reservation an older version removed; `ccm roles` shows `reserved`, `no reservation`, or `invalid role` without guessing its origin. An unreserved pane does not block the window. While a known external agent such as Codex is running there, it is excluded from launch, exit and idle-exit candidates. Without a primary, an unreserved shell follows normal shell selection and an unreserved Claude follows normal exit rules. With a unique primary, only that primary can be launched or exited. Live ignore markers still exclude panes. Observation does not write a reservation into an unreserved pane: after the external agent stops, its shell can be eligible again. Saving a snapshot while the external agent is running records it as a sidekick.

Restoration starts your login shell: tmux's `default-shell`, then `$SHELL`, then `/bin/sh`, accepting only executable absolute paths with a recognized shell name. It bypasses `default-command`. Shell startup files run normally. At each layout step ccm waits up to 10 seconds for a shell foreground to settle, including child processes sharing its process group during rc execution. Background jobs in separate groups are retained. If rc starts an agent or another persistent foreground process, restoration pauses without killing or restarting it; inspect the pane and retry after it returns to the shell. Already completed panes are not restarted on retry. Saved commands are not executed. Claude hand-off guidance is reevaluated at attach by the existing `continue_blocker_notice`, using current evidence rather than saved PIDs. Role text and resume guidance neutralize control characters and tmux formats.

### Menu language

| tmux option | Default | Values and scope |
|---|---|---|
| `@ccm-lang` | `en` | `en` / `ja`; unsupported values use English. Menu description body only. |

Set `set -g @ccm-lang "ja"` in `~/.tmux.conf` for Japanese menu descriptions (`en` is the default; unsupported values use English). Reload the configuration and reopen the menu to apply it. Only the description body changes; labels and other screens remain English. Matching dashboard shortcuts appear as dim `[key]` hints at the right edge; narrow menus omit hints to preserve item names.

### Codex sidekick notifications

| tmux option | Default | Meaning |
|---|---|---|
| `@ccm-sidekick-notify` | `off` | Deliver Codex turn-end notices to Claude in the same window (`on` / `off`). |
| `@ccm-sidekick-notify-limit` | `20` | Maximum attempts per rolling hour per window; `0` suppresses delivery. |
| `@ccm-sidekick-notify-excerpt` | `on` | Include the final message’s first line (`on` / `off`). |

For all sidekick windows, put `set -g @ccm-sidekick-notify on` in `~/.tmux.conf` (recommended), then reload the configuration. A command entered only into the running server is not persistent. This enables delivery when matching Codex hooks are installed and trusted; it does not install or trust hooks.

All three notification options resolve in this order: explicit window value (`set -w`), global window value (`set -gw`), global session value (`set -g`), built-in default. A window's `off` overrides global `on`. If both global tables are set, `-gw` wins; prefer using only `-g` in the configuration. Empty values act as unset. Use `tmux set -wu -t <window> @ccm-sidekick-notify` to remove a window override. A failed query stops resolution and disables notification/excerpt delivery for that read.

`ccm doctor --verbose` shows the effective notification, hourly limit and excerpt values with their sources (`window`, `global (-gw)`, `global (-g)`, `default`, or `unavailable` on query failure). Enabled windows without observed Codex hooks still produce a warning. Hook creation, delivery, diagnostics and dashboard notice previews share the same resolver.

Snapshots v1/v2 are unchanged: they do not save notification overrides. Global configuration applies to newly restored windows. A window-only override is lost when its window is recreated; reapply exceptions before starting sidekicks. If notifications or excerpts must stay disabled after restart, set the corresponding global option to `off` in `~/.tmux.conf`.
