# State machine

Formal reference for the ccm detection pipeline. The implementation lives in [`lib/ccm_detection.py`](../lib/ccm_detection.py); this document describes the design contract that the two detection backbones (legacy `DETECTION_RULES` table and `derive_state_from_events`) must respect.

## States

| State | Icon | Meaning |
|---|---|---|
| `SHELL` | `■` | No `claude` process in the pane (the user is at a plain shell prompt, or the window has not been started). |
| `DOWN` | `○` | No pane process at all (window deleted while ccm was running). |
| `IDLE` | `●` | Claude is running and waiting for the next user input (`❯` prompt visible). |
| `BUSY` | `◉` | Claude is processing — thinking, streaming a response, or running a tool. |
| `CONT` | `◍` | Claude paused mid-turn for a tool result (Stop event seen with `stop_reason=tool_use`). Operationally equivalent to `BUSY` for `ccm send` (always rejects without `--force`). Only emitted by the event-log backbone. |
| `PERMIT` | `⚠` | A permission dialog or confirmation modal is on screen waiting for the user. `ccm send` always refuses, even with `--force`. |

`STATE_PRIORITY` (used for dashboard sorting and status-bar collapsed indicator): `PERMIT < BUSY = CONT < IDLE < SHELL < DOWN`. Smaller number = higher priority.

## Detection backbones

ccm runs two detection paths in parallel; the one that wins depends on the `CCM_USE_EVENT_LOG` env var:

| Mode | Resolved state | When |
|---|---|---|
| `auto` (default since 2026-04-25) | event-log if `derive_state_from_events` returns non-`None`, else legacy | normal production use |
| `off` | legacy only | explicit opt-out |
| `observe` | legacy (event-log computed for debug trace only) | observation runs paired with `CCM_DEBUG_TRACE` |
| `primary` | same as `auto` | reserved for future diagnostic use |

### Legacy backbone — `DETECTION_RULES` table

A priority-ordered list of `Rule` records evaluated top-to-bottom; first match wins. Each rule constrains the input `DetectionContext` (raw, hook_state, hook_age, prev_state, jsonl_age, jsonl_last_stop_reason, claude_pid_age) and emits a resolved state.

Current rules (order matters — see `lib/ccm_detection.py` for the live table):

1. `process_down` — raw=DOWN → DOWN
2. `process_shell` — raw=SHELL → SHELL
3. `hook_fresh_busy` — fresh BUSY hook (< 2 s) → BUSY
4. `hook_permit_jsonl_terminal_release` — stale PERMIT hook + JSONL terminal fresher than the hook → IDLE
5. `hook_permit_blocking` — hook=PERMIT + raw in (BUSY, PERMIT) → PERMIT
6. `hook_busy_jsonl_terminal_release` — stale BUSY hook + JSONL terminal fresher than the hook → IDLE
7. `hook_busy_idle` — hook=BUSY + raw=IDLE + JSONL fresh enough + recap-gap guard → BUSY
8. `hook_busy_idle_no_jsonl` — same but no JSONL exists yet → BUSY
9. `jsonl_tool_use_pending` — between-tools gap with stop_reason=tool_use → BUSY
10. `jsonl_fresh_activity` — JSONL written within 5 s → BUSY
11. `jsonl_holds_busy` — JSONL within 15 s + prev=BUSY → BUSY
12. `fallback_busy_to_idle` — prev=BUSY + JSONL aged → IDLE
13. `fallback_permit_hold` — prev=PERMIT + raw=IDLE within `PERMIT_GAP_TOLERANCE` → PERMIT
14. `startup_transient_raw_busy` — raw=BUSY + young pid + no hook → IDLE (MCP loading)
15. `raw_not_idle` — catch-all passthrough (raw≠IDLE → raw)
16. `default` — final catch-all → IDLE

### Event-log backbone — `derive_state_from_events`

A pure function over `(events, jsonl_stop_reason, jsonl_age, pid_present, claude_pid_age, raw, now)`. Decision tree:

1. `pid_present=False` → `SHELL` (process tree authoritative)
2. `events` empty / latest record malformed / unknown event type → `None` (defer to legacy)
3. Latest event is `session_end` with live pid → `None` (claude restarted, defer to legacy)
4. Latest event is permit-class (`permit_req` / `notify_permit`):
   - if JSONL terminal stop_reason is fresher than the event AND `raw≠"PERMIT"` → `IDLE` (silent permission resolution)
   - else → `PERMIT`
5. Latest event is start-class (`prompt` / `pretool` / `posttool` / `subagent` / `compact`):
   - if JSONL terminal stop_reason is fresher than the event AND `raw≠"PERMIT"` → `IDLE` (Esc interrupt or hook silence)
   - else → `BUSY`
6. Latest event is `notify_idle` → `IDLE`
7. Latest event is `stop`:
   - JSONL stop_reason terminal → `IDLE`
   - JSONL stop_reason `tool_use` or unknown → `CONT`
8. Final A' override: if `raw=="PERMIT"` and candidate ≠ `"PERMIT"` → `"PERMIT"` (capture-pane footer match wins)

## Invariants

These are tested by `TestPipelineInvariants` and `TestDeriveInvariants`:

- Every legacy `evaluate_rules` call returns a state in `{SHELL, DOWN, BUSY, CONT, IDLE, PERMIT}`.
- The legacy backbone never emits `CONT` (reserved for the event-log path).
- raw=SHELL always resolves to SHELL.
- raw=DOWN always resolves to DOWN.
- `derive_state_from_events` returns either `None` or a state in the same set.
- `pid_present=False` always resolves derive to `SHELL`.
- `raw=="PERMIT"` always pulls derive to `PERMIT` (or `None`, never `BUSY` / `CONT` / `IDLE`).

## Key discriminators (and why they exist)

### `event_age > jsonl_age` / `hook_after_real_activity_lt=0`

Used by both the start-class and permit-class release branches in derive, and by `hook_busy_jsonl_terminal_release` and `hook_permit_jsonl_terminal_release` in the legacy table.

The discriminator: a JSONL terminal stop_reason is treated as "this hook/event was actually resolved" only when the JSONL terminal happened **strictly after** the hook/event in question. Without this guard, a fresh prompt or a fresh permit_req fired right after a previous turn ended would falsely flip to IDLE — the JSONL terminal in that case belongs to the prior turn, not to anything related to the new event.

This is an information-theoretic invariant ("a completion signal must post-date the event it claims to complete"), not a heuristic threshold.

### `raw=="PERMIT"` precedence

The capture-pane footer (`Esc to cancel · Tab to amend`, `Enter to confirm · Esc to <verb>`, etc.) is the most physical signal we have — the modal is literally on screen waiting for a keypress. This signal trumps all event-log and hook-based derivations. raw=BUSY does **not** get the same precedence: that would re-introduce the false-BUSY-from-leftover-dev-server problem the event-log path was designed to fix.

### `claude_pid_age < STARTUP_GRACE_SEC`

A young claude PID (< 60 s) with raw=BUSY but no hook signal is almost certainly the MCP-loading startup transient (claude has children but hasn't drawn `❯` yet). The `startup_transient_raw_busy` rule demotes this to IDLE rather than reporting a false BUSY for 30 s after every attach.

## Stale-signal UI affordance

When a `BUSY` or `PERMIT` state survives past `JSONL_HOOK_GAP_TOLERANCE` (60 s) and the auto-release rules cannot safely fire (e.g. JSONL `stop_reason=tool_use` rather than terminal), the dashboard and `ccm status` append a parenthesised hook-signal age to the state cell:

```
⚠ PERMIT (8m)  monadic-chat
◉ BUSY  (2m)  ccm-dev
```

This is the principled response to the limitation — when ccm cannot prove the signal is stuck, it surfaces the age so the user can judge. Implementation: [`ccm_core.signal_age_suffix(project_dir, state)`](../lib/ccm_core.py) is the single source of truth, used by both renderers. Threshold is bound directly to `JSONL_HOOK_GAP_TOLERANCE` so the affordance appears exactly when the auto-release window has lapsed. Other states (`IDLE` / `SHELL` / `DOWN` / `CONT`) suppress the suffix — their hook signals are either absent or freshness-irrelevant.

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
