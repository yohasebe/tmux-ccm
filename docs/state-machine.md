# State machine

Formal reference for the ccm detection pipeline. The implementation lives in [`lib/ccm_detection.py`](../lib/ccm_detection.py); this document describes the design contract that the two detection backbones (legacy `DETECTION_RULES` table and `derive_state_from_events`) must respect.

## Design principle

States answer one question: **does the user need to take action right now?**

- `PERMIT` — yes, the user must approve / dismiss / answer a modal
- `BUSY` — no, claude has the ball; wait
- `IDLE` — no, the user has the ball, but no immediate action is required
- `SHELL` / `DOWN` — environmental (claude not running)

Background activity (leftover dev servers, phantom upstream events, cron-triggered processes) is **informational, not action-actionable** — it goes into a parenthesised suffix (`(bg)`, `(Nm)`) rather than the state label. The state label is the one-glance answer to "do I need to do something?"; suffixes carry context for users who want the deeper picture.

When choosing between adding a new state, a new rule, or a new suffix, ask the principle question first. If the new condition does not change "what should the user do right now?", it belongs in a suffix or stays implicit — not in the state label.

## States

| State | Icon | Meaning |
|---|---|---|
| `SHELL` | `■` | No `claude` process in the pane (the user is at a plain shell prompt, or the window has not been started). |
| `DOWN` | `○` | No pane process at all (window deleted while ccm was running). |
| `IDLE` | `●` | Claude is running and waiting for the next user input (`❯` prompt visible). |
| `BUSY` | `◉` | Claude is processing — thinking, streaming a response, running a tool, or paused mid-turn for a tool result. Any state where the ball is on Claude's side. |
| `PERMIT` | `⚠` | A permission dialog or confirmation modal is on screen waiting for the user. `ccm send` always refuses, even with `--force`. |

`STATE_PRIORITY` (used for dashboard sorting and status-bar collapsed indicator): `PERMIT < BUSY < IDLE < SHELL < DOWN`. Smaller number = higher priority.

Pre-v0.3.0 the model carried a sixth detection state `CONT` (Claude paused with `stop_reason=tool_use`) emitted only by the event-log backbone. It was collapsed into `BUSY` because the user-action axis (the design principle above) does not distinguish "actively running tool" from "between tools" — both are "wait, the ball is on Claude's side". Removing the distinction simplified the state machine, the priority table, and the `ccm send` dispatcher without losing actionable information.

## Window-to-pane projection

A tmux window can host multiple panes, but ccm reports a single state per window (per project). The projection rule is **aggregation with sliver exclusion**:

1. Filter out panes shorter than `SLIVER_HEIGHT_THRESHOLD` (4 rows by default, `CCM_SLIVER_HEIGHT_THRESHOLD` overrides). Such panes cannot render Claude's `❯` prompt + accept-edits indicator + footer, so capture-pane–based prompt detection silently fails and the pane false-reads BUSY (children present, no prompt visible).
2. Aggregate the remaining panes by priority: `PERMIT > BUSY > IDLE > SHELL`. The window state is the most attention-needing pane's state.
3. If every pane is below the threshold (impossible in practice), bypass the filter — better to give a possibly-wrong answer than fall through to SHELL on a window full of slivers.

This rule satisfies two competing requirements:

- **Multi-pane Agent Teams** (and any deliberate `prefix " ` / `prefix %` split): the user can see all panes simultaneously and wants the window's reported state to surface attention-needing panes even when focus is on a different teammate. A single PERMIT teammate must show `⚠ PERMIT` for the whole window.
- **Sliver / orphan panes** (the personal regression that motivated the rewrite): a 1-row sliver pane holding a long-idle `claude --continue` falsely reads BUSY because the prompt can't render in 1 row. Pre-fix that BUSY infected the whole window; post-fix the sliver is excluded and the visible panes drive the result.

The two earlier designs were both wrong:

- Pre-2026-04-27 "most-active-wins aggregation": correct for Agent Teams, broken for slivers.
- 2026-04-27 morning's "active-pane authoritative" (commit `2024c30`, reverted): correct for slivers, broken for Agent Teams (a non-active teammate's PERMIT was invisible).

Sliver-exclusion is the discriminator that lets one rule serve both cases.

### Auto-focus on attach

`reset_window_after_attach` (called from every ccm-mediated attach path: `cmd_attach`, dashboard `_do_attach`, dashboard tree-mode attach) calls `auto_focus_attention_pane(win_target)` after its existing wipes. If the window has multiple eligible (non-sliver) panes and one is in PERMIT and the active pane is not, focus is switched to the PERMIT pane via `tmux select-pane`.

Scope is intentionally narrow: PERMIT only. BUSY panes are interesting to monitor but do not require user input, and stealing focus to one would surprise users who deliberately positioned themselves elsewhere. Manual `prefix + N` window-switch is not hooked — the auto-focus only fires through ccm-mediated attaches.

Inactive panes also drive the `(bg)` UI affordance (state=IDLE with raw=BUSY from grandchild processes) when applicable.

## Detection backbones

The event-log path (`derive_state_from_events`) is the primary detection mechanism. The legacy `DETECTION_RULES` table is the safety net for cases where the event log is empty / malformed / in a post-`session_end` transient — the dispatcher commits the event-log state when derive returns non-`None`, and falls back to legacy otherwise. `CCM_USE_EVENT_LOG` selects between `auto` (default; the dispatcher above), `off` (legacy only — diagnostic opt-out), `observe` (legacy committed, event-log computed for trace diff), and `primary` (alias for `auto`).

### Legacy backbone — `DETECTION_RULES` table

The legacy table is intentionally minimal: it exists only to produce a sensible state when the event-log path declines to answer. All hook / JSONL freshness reasoning lives in the event-log path. Each rule constrains the input `DetectionContext` (raw, hook_state, hook_age, prev_state, jsonl_age, jsonl_last_stop_reason, claude_pid_age) and emits a resolved state. First match wins.

1. `process_down` — raw=DOWN → DOWN
2. `process_shell` — raw=SHELL → SHELL
3. `hook_fresh_busy` — fresh BUSY hook (< 2 s) + recap-gap guard → BUSY
4. `startup_transient_raw_busy` — raw=BUSY + young pid + no hook → IDLE (MCP loading window)
5. `raw_busy_passthrough` — raw=BUSY → BUSY (no-hooks process-tree fallback)
6. `raw_permit_passthrough` — raw=PERMIT → PERMIT (capture-pane modal footer fallback)
7. `default` — final catch-all → trust raw state

### Event-log backbone — `derive_state_from_events`

A pure function over `(events, jsonl_stop_reason, jsonl_age, pid_present, claude_pid_age, raw, now)`. Decision tree:

1. `pid_present=False` → `SHELL` (process tree authoritative)
2. `events` empty / latest record malformed / unknown event type → `None` (defer to legacy)
3. Latest event is `session_end` with live pid → `None` (claude restarted, defer to legacy)
4. Latest event is permit-class (`permit_req` / `notify_permit`):
   - if `raw=="PERMIT"` (modal physically on screen) → `PERMIT` (capture-pane authoritative)
   - else if JSONL terminal stop_reason is fresher than the event → `IDLE` (silent permission resolution / Esc dismiss)
   - else if JSONL `stop_reason=tool_use` is fresher than the event AND within `BUSY_HOOK_JSONL_WINDOW` → `BUSY` (auto-approved permit, tool actively running)
   - else → `PERMIT`
5. Latest event is start-class (`prompt` / `pretool` / `posttool` / `subagent` / `compact`):
   - if `latest.type == "subagent"` AND the immediately-preceding non-subagent event is `notify_idle` AND `raw≠"PERMIT"` → `None` (phantom-subagent shortcut — Claude Code's upstream fires occasional spurious `subagent` events in idle periods; legitimate subagent invocations always come mid-conversation, never after the explicit idle marker. Walks back through stacked phantom subagent events to handle the chain case)
   - else if JSONL terminal stop_reason is fresher than the event AND `raw≠"PERMIT"` → `IDLE` (Esc interrupt or hook silence within 60 s)
   - else if both event_age AND jsonl_age exceed `BUSY_HOOK_JSONL_WINDOW` (10 min) AND `raw≠"PERMIT"` → `None` (combined-stale fallback — defer to legacy, which resolves via `fallback_busy_to_idle` → `IDLE`. Catches other spurious upstream firings beyond the specific subagent shortcut above)
   - else → `BUSY`
6. Latest event is `notify_idle` → `IDLE`
7. Latest event is `stop`:
   - JSONL stop_reason terminal → `IDLE`
   - JSONL stop_reason `tool_use` or unknown → `BUSY` (intermediate Stop boundary, ball still on Claude's side)
8. Final A' override: if `raw=="PERMIT"` and candidate ≠ `"PERMIT"` → `"PERMIT"` (capture-pane footer match wins)

## Invariants

These are tested by `TestPipelineInvariants` and `TestDeriveInvariants`:

- Every legacy `evaluate_rules` call returns a state in `{SHELL, DOWN, BUSY, IDLE, PERMIT}`.
- raw=SHELL always resolves to SHELL.
- raw=DOWN always resolves to DOWN.
- `derive_state_from_events` returns either `None` or a state in the same set.
- `pid_present=False` always resolves derive to `SHELL`.
- `raw=="PERMIT"` always pulls derive to `PERMIT` (or `None`, never `BUSY` / `IDLE`).

## Key discriminators (and why they exist)

### `event_age > jsonl_age` / `hook_after_real_activity_lt=0`

Used by both the start-class and permit-class release branches in derive, and by `hook_busy_jsonl_terminal_release` and `hook_permit_jsonl_terminal_release` in the legacy table.

The discriminator: a JSONL terminal stop_reason is treated as "this hook/event was actually resolved" only when the JSONL terminal happened **strictly after** the hook/event in question. Without this guard, a fresh prompt or a fresh permit_req fired right after a previous turn ended would falsely flip to IDLE — the JSONL terminal in that case belongs to the prior turn, not to anything related to the new event.

This is an information-theoretic invariant ("a completion signal must post-date the event it claims to complete"), not a heuristic threshold.

### `raw=="PERMIT"` precedence

The capture-pane footer (`Esc to cancel · Tab to amend`, `Enter to confirm · Esc to <verb>`, etc.) is the most physical signal we have — the modal is literally on screen waiting for a keypress. This signal trumps all event-log and hook-based derivations. raw=BUSY does **not** get the same precedence: that would re-introduce the false-BUSY-from-leftover-dev-server problem the event-log path was designed to fix.

### `claude_pid_age < STARTUP_GRACE_SEC`

A young claude PID (< 60 s) with raw=BUSY but no hook signal is almost certainly the MCP-loading startup transient (claude has children but hasn't drawn `❯` yet). The `startup_transient_raw_busy` rule demotes this to IDLE rather than reporting a false BUSY for 30 s after every attach.

## UI affordances

Two parenthesised suffixes communicate sub-state context that does not warrant a separate state label:

### `(Nm)` — stale-signal age

When a `BUSY` or `PERMIT` state survives past `JSONL_HOOK_GAP_TOLERANCE` (60 s) and the auto-release rules cannot safely fire (e.g. JSONL `stop_reason=tool_use` rather than terminal), the dashboard, `ccm status`, and status bar (mode 1 / 2) append a parenthesised hook-signal age to the state cell:

```
⚠ PERMIT (8m)  monadic-chat
◉ BUSY  (2m)  ccm-dev
```

This is the principled response to the limitation — when ccm cannot prove the signal is stuck, it surfaces the age so the user can judge. Implementation: [`ccm_core.signal_age_suffix(project_dir, state)`](../lib/ccm_core.py) is the single source of truth, used by all three renderers. Threshold is bound directly to `JSONL_HOOK_GAP_TOLERANCE` so the affordance appears exactly when the auto-release window has lapsed. Other states (`IDLE` / `SHELL` / `DOWN`) suppress the suffix — their hook signals are either absent or freshness-irrelevant.

### `(bg)` — background activity in user's turn

When the committed state is `IDLE` (the conversation turn has returned to the user) but `raw=BUSY` (the process tree still shows tool / dev-server grandchildren), the renderers append `(bg)` to indicate "Claude is at rest, but something it spawned is still running":

```
● IDLE (bg)  monadic-chat
```

Conceptually distinct from `(Nm)`: the stale-signal suffix is a "this might be stuck" hint, while `(bg)` is a positive statement about leftover processes. They are mutually exclusive — `(Nm)` only fires for `BUSY` / `PERMIT`, `(bg)` only fires for `IDLE`. `apply_actions` writes the `@ccm_bg_active` tmux window option whenever `state == "IDLE"` and `ctx.raw == "BUSY"`; clears it otherwise. `Project.bg_active` carries the flag to the renderers. `ccm send` treats `IDLE (bg)` exactly like plain `IDLE` — the user has the conversation ball regardless of background processes.

### `[N]` — multi-pane window

When a window has more than one pane (`Project.pane_count > 1`), the dashboard, `ccm status`, and status bar (mode 1 / 2) append `[N]` (where N is the pane count) immediately after the project name:

```
ccm-dev [3]    ⚠ PERMIT (8m)
teaching [2]   ● IDLE
```

Brackets render dim, the digit cyan, so the eye lands on the count without reading the chrome. Surfaces the multi-pane case (Agent Teams workflow, casual `prefix " ` / `prefix %` splits, leftover orphan panes from earlier ad-hoc work) so the user can spot windows whose aggregated state may belong to a non-active pane. Combines freely with `(Nm)`, `(bg)`, and `*elapsed` — a stuck-PERMIT split-pane window reads `monadic-chat [3]   ⚠ PERMIT (8m)`. Independent of state semantics; populated unconditionally from `panes_cache` in `build_project_list`. The marker is plain ASCII (rather than a Unicode glyph like `⊞`) so column math is identical across all terminals and font configurations.

### `* elapsed` — recently completed turn

When a project transitions out of BUSY / PERMIT into IDLE, the dashboard records the timestamp in `@ccm_completed_at`. For `COMPLETED_AT_TIMEOUT` seconds afterward, the rendered row carries a `* <elapsed>` annotation right after the project name (and after `[N]` if the window has multiple panes):

```
ccm-dev * 25s   …/code/ccm
```

The asterisk renders green to draw attention to the just-completed transition; the elapsed time itself is dim. Display-only — the marker does not feed back into the state machine. ASCII so column math is consistent.

## Time-window heuristics

These are tunable thresholds with empirically-chosen defaults:

| Constant | Default | Purpose |
|---|---|---|
| `HOOK_FRESH_THRESHOLD` | 2 s | "Hook just fired" gate for `hook_fresh_busy` |
| `JSONL_FRESH_THRESHOLD` | 5 s | `jsonl_fresh_activity` window |
| `JSONL_ACTIVE_THRESHOLD` | 15 s | `jsonl_holds_busy` post-Stop bridge |
| `JSONL_HOOK_GAP_TOLERANCE` | 60 s | recap-defense gap; also the freshness window for the new release rules |
| `PERMIT_GAP_TOLERANCE` | 60 s | `fallback_permit_hold` after-IDLE permit hold |
| `BUSY_HOOK_JSONL_WINDOW` | 600 s | `hook_busy_idle` long-tool tolerance + final stuck-BUSY release cap |
| `STARTUP_GRACE_SEC` | 60 s | startup transient pid-age window |
| `SLIVER_HEIGHT_THRESHOLD` | 4 rows | minimum pane height to participate in window aggregation |

## Transition examples (lifecycle walk-through)

### Normal turn (hooks healthy, no Esc)

| Step | Inputs | State | Rule |
|---|---|---|---|
| Start | raw=IDLE, hook=, prev=IDLE | IDLE | `default` |
| User submits | raw=IDLE, hook=BUSY age 0, jsonl=0, prev=IDLE | BUSY | `hook_fresh_busy` |
| Streaming | raw=IDLE, hook=BUSY age 5, jsonl=2, prev=BUSY | BUSY | `hook_busy_idle` |
| Stop fires | raw=IDLE, hook=, jsonl=1, prev=BUSY | BUSY | `jsonl_fresh_activity` |
| Settled | raw=IDLE, hook=, jsonl=20, prev=BUSY | IDLE | `fallback_busy_to_idle` |

### Esc interrupt (no Stop hook fires)

| Step | Inputs | State | Rule |
|---|---|---|---|
| User submits | raw=IDLE, hook=BUSY age 0, jsonl=0, prev=IDLE | BUSY | `hook_fresh_busy` |
| Streaming | raw=IDLE, hook=BUSY age 5, jsonl=2, prev=BUSY | BUSY | `hook_busy_idle` |
| Esc + JSONL terminal | raw=IDLE, hook=BUSY age 30, jsonl=20, jsonl_stop=stop_sequence | IDLE | `hook_busy_jsonl_terminal_release` |

### Permission accepted, then silent completion (the monadic-chat regression)

| Step | Inputs | State | Rule |
|---|---|---|---|
| Tool wants permission | raw=PERMIT, hook=PERMIT age 0 | PERMIT | `hook_permit_blocking` |
| User accepts (modal gone, accept-edits keeps raw=BUSY) | raw=BUSY, hook=PERMIT age 30 | PERMIT | `hook_permit_blocking` |
| Claude completes silently (no permission-resolved hook) | raw=BUSY, hook=PERMIT age 90, jsonl=30, jsonl_stop=end_turn | IDLE | `hook_permit_jsonl_terminal_release` |

## When editing the state machine

1. Edit the rule in `DETECTION_RULES` (legacy) or the relevant branch of `derive_state_from_events` (event-log).
2. Set the `phase` field on any new legacy rule (`shell` / `startup` / `midturn` / `between_tools` / `idle` / `permit`, or `None` for genuine catch-alls).
3. Update `TestRulePhaseAnnotations.test_specific_rule_phase_classifications` to register the new rule.
4. Add a parametrized test in `TestEvaluateRules` (Context built directly, no mocks).
5. If the change introduces a new lifecycle, add a scenario test in `TestLifecycleSequences`.
6. Run `python3 -m pytest tests/test_ccm_core.py -v` and verify both the new test and the property invariants in `TestPipelineInvariants` / `TestDeriveInvariants` still pass.
