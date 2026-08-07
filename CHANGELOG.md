# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Verified against Claude Code v2.1.222 through v2.1.224. 2.1.224 adds
  cross-session `SendMessage`/`ListAgents` — Claude sessions messaging each
  other over on-disk inbox sockets — which was measured live to leave ccm's
  detection intact: a received message reads as a new turn (BUSY) and a tool it
  triggers still surfaces PERMIT, exactly as a typed prompt would; the new hook
  events it can fire are ones ccm does not subscribe to, and unknown events
  fall through to legacy detection by design. The `roster.json` ccm's `bg`
  section reads is unchanged.
- Verified against Claude Code v2.1.222 and v2.1.223. All four detection
  patterns matched a live pane unaltered on both (including the sub-1k token
  spinner form), the hook event sequence was unchanged, and the
  permission-request escalation held at ~6 s throughout — as did the
  Esc-interrupt transcript record ccm now releases on, exercised live under
  2.1.222. Neither release touches the surfaces ccm reads. Both do tighten
  permission checking (2.1.222: background-task auto-allow; 2.1.223: commands
  hiding parts of themselves from the approval dialog via crafting or invisible
  Unicode), so sessions may see more permission prompts — which ccm reports the
  same way.

### Fixed
- The dashboard's `w` key is listed in the README's control table. It appears
  in the dashboard's own on-screen help, so leaving it out of the table left a
  reader who saw it on screen with nowhere to look it up.
- A Japanese README link pointed at a guide section under a heading it no longer
  has, so it landed at the top of the page instead.


## [0.9.0] - 2026-08-05

Sidekick support grows a second half. 0.8.x could show you that another agent
CLI was running beside Claude; it could not show you that the agent had stopped
and was waiting for a human. It can now — without ccm reading anyone's screen.

### Added
- Sidekick attention. When a sidekick blocks on a permission dialog, its badge
  turns PERMIT-yellow on the dashboard, `ccm status` and the status bar, and a
  desktop notification names what it is asking about; answering clears it. The
  window's own state never changes — PERMIT still means *Claude* needs you.
  Toggle with `w` in the dashboard or `@ccm-sidekick-attention off`.

  A second Claude Code sidekick (`CCM_IGNORE=1 claude`) needs no setup: those
  sessions already ran ccm's hooks and were exiting silently, so the ignore
  branch now forwards their permission events. The ignore contract is
  untouched — nothing reaches window state, `ccm send` delivery, or auto-exit.

  Support for non-Claude agents is exploratory and documented in the guide
  rather than the CLI table: it works by asking each vendor's own hook system
  to self-report, and those contracts are young. Screen parsing was rejected
  outright — 0.8.2 shipped three fixes for exactly that class of assumption
  against a single product's strings.

### Fixed
- A session interrupted with Esc is released at once instead of after a minute.
  Detection had been built on the belief that a user interrupt leaves no trace —
  a code comment stated the turn "fires no Stop hook and writes no further
  JSONL" — so the only way out was an aging guard that waits
  `BUSY_STALE_RELEASE_SEC` before trusting an idle screen. The second half of
  that belief is false: Claude Code writes a transcript record reading
  `[Request interrupted by user]`. It is now recognised as a synthetic terminal
  `stop_reason`, joining the values the existing release path already keys on,
  so no interrupt-specific branch was needed anywhere in the decision tree.

  Read naively the record makes things worse, and the traps are pinned by tests.
  It is newer than the assistant turn it cut short, so counting it as activity
  restarted the very wait being served out; its type is `user`, so it would have
  promoted to "a prompt was just submitted" and held BUSY for ten minutes; and
  matching the phrase as a substring fires on any message that merely mentions
  it — a false IDLE, which is the dangerous direction, since `ccm send` would
  then deliver into a working session.
- The aging guard works without a readable transcript. It compared the JSONL
  age against its window, and an unreadable transcript reports `-1` — never past
  any window — so a session whose transcript ccm cannot find stayed BUSY with no
  release path at all, while legacy detection (which would have freed it) was
  never consulted. It now falls back to the age of the newest event.
- ccm's hook scripts reject payloads from foreign agent harnesses. Grok Build
  reads `~/.claude/settings.json` hooks by default for Claude Code
  compatibility, and its payloads parse well enough to slip through, so a Grok
  sidekick would have written BUSY signals under its own session id and fired
  completion notifications for turns no Claude ran. Every such event carries
  `workspaceRoot`, which Claude Code never sends.
- Agent CLIs launched through a platform-suffixed binary are recognised again.
  A launcher symlinked to a per-platform binary makes tmux report the resolved
  name truncated, so an allowlist entry matching the launcher name never fired —
  costing the presence badge entirely.

### Changed
- Verified against Claude Code v2.1.221. Nothing in ccm needed changing: the
  four detection patterns matched the live pane unaltered, the hook event
  sequence was unchanged, and the permission-request escalation timing held.
  The release does move permission checking — Bash commands hidden in `[[ ]]`
  conditionals now prompt, and an auto-mode check left pending across a mode
  switch re-prompts rather than applying the stale result — so sessions may see
  more permission prompts than before, which ccm reports the same way.

## [0.8.2] - 2026-07-30

Three detection fixes, all of them cases where ccm looked confident and was
wrong: a working session reported as needing you, and a delivered message
reported as lost.

### Fixed
- A turn running past the hour mark went invisible to raw detection. The
  active-work spinner is the one piece of direct on-screen evidence that Claude
  is working, and its elapsed gains an `h` unit past 60 minutes —
  `(3h 11m 16s · ↓ 8.8k tokens)` — which the minutes-only pattern did not match.
  Because an accept-edits pane keeps `❯` on screen while a tool runs, raw then
  fell to IDLE, taking with it the promotion that rescues a permission already
  approved (Claude Code fires `PermissionRequest` but nothing on resolution, so
  the approved permit stays the latest event until the tool finishes). Observed
  on ccm-dev: a permission granted within ~6 s left the dashboard on `⚠ PERMIT`
  for the remaining ~110 s of a `bats` run, with no user action pending. Each
  unit is now independently optional, since nothing promises Claude Code prints
  a zero-valued field. Same lesson as the sub-1k token count in 0.6.0 — an
  assumed shape for a string that upstream is free to reformat.
- The session-resume modal went unrecognised for any session under an hour. Its
  age line was matched as a fixed `\d+h \d+m` pair, so `This session is 45m
  old` missed — and `2d 4h old`, which `--continue` invites, missed too. Found
  by the same review that caught the spinner, and it is the same mistake; both
  now spell the age as repeated units rather than a fixed shape. Impact was
  limited because the picker's recommended-summary line matches independently,
  which is why it survived this long.
- `ccm send --start` no longer refuses a long message that did arrive. Delivery
  verification looked for the body's opening line, which holds for Claude's
  composer (it grows upward) but not for one that scrolls a long body and keeps
  only the trailing rows — observed against Kimi K3, a ~30-line brief rendering
  as `↑ 24 more`. A signature from each end is now checked and either satisfies
  it, so the retry path no longer re-types a body already on screen and then
  declines a send that had in fact landed.
- `ccm doctor`'s multi-claude scan reports itself when it cannot run, instead of
  omitting the row — in a diagnostic command a check that vanishes silently
  reads as a check that passed. Its guard against empty input also now works:
  `"".split("\n")` is `[""]`, truthy, so an absent process list used to enter
  the scan rather than skip it.

## [0.8.1] - 2026-07-30

Sidekick support shipped in 0.8.0 as a badge and an ignore flag; this release
is the part that makes it usable — the arrangement drawn and written down, and
the one step in it that could silently drop a message put right.

### Added
- A diagram for the sidekick arrangement (`assets/sidekick-model.svg`), embedded
  in the guide's relay section: which pane ccm tracks, which one it only shows a
  badge for, and how the two exchange messages. Sister figure to the mental
  model — same palette, badges and edge idiom, so the pair reads as one system.
- The external-agent allowlist now covers Codex, Gemini and Grok alongside Kimi,
  so those panes get a presence badge too. Only the Kimi names are measured
  against a running pane; the rest are the CLIs' binary names, which is safe in
  a way worth stating: a name that never appears simply never matches, while a
  correct guess starts working the day the tool is installed.
- The guide and the sidekick diagram now say outright that the sidekick may be
  another Claude Code — the arrangement `CCM_IGNORE` was written for, which a
  list of third-party CLIs had come to read as excluding. What differs is only
  the badge: `⊘` for a session ccm would otherwise track, `⚙` for one it never
  would.
- `ccm doctor` lists windows hosting more than one visible Claude session under
  *multi-claude windows*, naming both readings — an Agent Teams split, or a
  sidekick nobody hid — without recommending either. A standing dashboard hint
  was considered and dropped: it would have reached Agent Teams users too, for
  whom hiding a teammate costs that teammate's PERMIT.
- A sidekick is now documented as answering only to the Claude session sharing
  its window; to reach another project's sidekick you ask that project's Claude
  to relay. `ccm send` has always drawn this line (a sidekick is dropped from
  delivery so it cannot intercept a message), but the raw `tmux send-keys` path
  had no stated boundary, and the guide's `ccm capture <project>` read as an
  invitation to reach into someone else's window. The session next door knows
  whether its peer is idle and which keys its TUI takes; from outside, both are
  guesses — and two senders in one composer interleave into one garbled prompt.

### Fixed
- The documented way to reach a sidekick pane could deliver nothing while
  looking like it worked. Chaining the body and `Enter` with `&&` lets the
  peer's TUI read the `Enter` as a newline, leaving the message unsent in its
  composer — measured against Kimi K3, no gap fails every time while 0.3 s
  succeeds. The guide now includes the pause, and says plainly that text
  visible in the peer's input box is proof of failure rather than delivery.
  `ccm setup-claude-md` had never carried this procedure at all, so every
  Claude session was improvising it; it is in the template now. Existing
  `~/.claude/CLAUDE.md` sections are not rewritten — re-run
  `ccm remove-claude-md && ccm setup-claude-md` to pick it up.

### Changed
- The `ccm send` ambiguity refusal now offers the way out. Two visible Claude
  panes and a focus elsewhere is unresolvable by focus alone; the message names
  `CCM_IGNORE` and the dashboard's `i` key, which drop the sidekick from
  delivery for good. This is the one place ccm volunteers hiding a pane —
  the reader has already hit the ambiguity, so the advice cannot misfire.
- Product names are capitalised consistently (`Claude Code`, not `claude code`)
  across the diagrams, README and guide. The figures had drifted to lowercase
  while the prose used the proper name 84 times to 4; commands and tool names
  (`ccm`, `tmux`) stay lowercase, which is their own convention.

## [0.8.0] - 2026-07-27

### Fixed
- The status bar no longer runs a full detection pass every second. tmux fires
  `#(ccm inject-status)` once per `status-interval`, and a full pass costs
  ~174 ms across ~24 processes (python3, 21 tmux clients, the wrapper) — so on
  a config with a seconds clock, ccm was holding roughly a fifth of a core
  continuously and hammering the kernel's process-info path. On one machine
  that ran for 15 days until a kernel zone (`data.kalloc.1024`) exhausted at
  20 GB and panicked; bisecting by `status-interval` attributed about half the
  allocation rate to ccm.

  The periodic pass is now rate-limited to `CCM_RECONCILE_INTERVAL` (20 s) and
  the seconds in between cost a shell fork: ~10 ms, 2 processes. Nothing about
  responsiveness changes, because state transitions were never carried by the
  poll — the hooks already push an immediate refresh, and `--fast` is never
  rate-limited. What the poll paces is only what fires no hook: a git branch
  switch, a new listening port, a stale-BUSY release crossing its window. The
  interval is set below `CCM_BUSY_STALE_RELEASE_SEC` for that last reason.

  Measured on the affected machine: allocation rate fell from 16–20/s to
  1.5/s, which is at or below the rate previously attributed to everything
  else on the system.

- A session interrupted with Esc mid-tool no longer shows `BUSY` for up to 10
  minutes. Esc fires no `Stop` hook, so the start-class event stayed "latest"
  and the session log froze at a non-terminal `tool_use` — leaving every
  release path closed. The aging guard now releases a BUSY candidate with an
  idle screen and a frozen log past `BUSY_STALE_RELEASE_SEC` (default 60 s,
  `CCM_BUSY_STALE_RELEASE_SEC`). The window is flicker prevention, not a
  longest-silent-tool estimate: the safety net for a genuinely working session
  whose log also freezes (a long silent build) is `CCM_IDLE_EXIT_TIMEOUT`
  requiring 10 minutes of sustained IDLE before auto-exit, so 600→60 only
  moves the worst-case kill threshold from 1200 s to 660 s of silence. The
  release applies only to start-class origins (the Esc case): a BUSY promoted
  from a permit event (auto-approved tool, which may run for minutes) is
  exempt. The other `BUSY_HOOK_JSONL_WINDOW` uses (permit promotion,
  combined-stale, legacy rules) are unchanged, and PERMIT keeps its own
  `PERMIT_MAX_TIMEOUT` window.

### Added (internal)
- `tests/test_reconcile_gate.bats`: the gate's decisions, and two properties
  that are easy to lose silently — that it spawns nothing on the skip path
  (checked by shadowing `date`/`tmux`/`stat` as functions that fail the test
  if reached), and that it runs before `ccm_init_dirs`, whose mkdir and two
  find sweeps would otherwise keep three processes per second.

### Added
- README's sidekick entry now mentions that the two agents can hand work back
  and forth without a human relaying text, and links to the guide section
  below — the capability was documented but not discoverable from the feature
  list.
- Guide section on relaying with a second agent CLI, and the conventions that
  make it work written into the `setup-claude-md` template. Running another
  agent CLI in a split pane already worked — presence badge, `ccm capture`
  across panes, `ccm send` inbound — but nothing said how the two sides should
  hand work back to each other, so a human ended up copying text between panes.
  The convention is to report rather than poll: neither side can observe the
  other's progress, so whoever finishes a request sends the result back itself
  and it arrives as the other's next turn. Vendor-neutral, and it needs no
  tracking of the other agent — which is why it works for any CLI that can run
  a shell command.

## [0.7.1] - 2026-07-27

### Fixed
- Uninstall instructions now detach ccm from Claude Code first. They cleaned up
  tmux state but never mentioned `ccm remove-hooks` or `ccm remove-claude-md`,
  so following them left seven hooks registered in `~/.claude/settings.json`
  pointing at absolute paths inside the deleted plugin directory — Claude Code
  would keep trying to run scripts that no longer exist, on every event, plus a
  `CLAUDE.md` section still instructing sessions to use removed commands. That
  is ccm breaking a different tool on its way out. The step is now first and
  marked as such, because the commands that undo those changes ship with the
  plugin being removed.
- `ccm send`'s help outputs document `--` and `--yes`. `--` is the only way to
  send a message beginning with a dash, and someone who needs it is by
  definition staring at a message ccm just parsed as flags — the guide had it,
  both help outputs did not.

### Added (internal)
- Docs-consistency coverage extended to `ccm send`'s flags (extracted from the
  parser, so a new flag cannot be added without documenting it) and to the
  uninstall section (both detach commands present, and ordered before plugin
  removal). Flag matching is token-bounded: a substring check reports the bare
  `--` as documented because it occurs inside `--file` and every other long
  flag, which would certify precisely the flag most likely to be missing.
- README no longer understates what `ccm setup-claude-md` writes into your
  global Claude Code instructions. It embedded a copy of the template that had
  drifted to a third of its length and had lost `ccm send` entirely, so a
  reader would conclude only read-only discovery commands were added — when the
  section in fact teaches a command that types prompts into other sessions. The
  copy is gone (it was the source of the drift); the section now describes both
  halves in prose, names the PERMIT policy that makes sending safe, and notes
  that `ccm setup-claude-md` prints the full text for confirmation before
  writing.
- README documents `ccm search`, and the status-icon table documents the `⊘`
  hidden-pane and `⚙<name>` external-agent markers, alongside a Features entry
  for sidekick support. All shipped in 0.7.0 but reachable only from the guide.

- Docs-consistency tests (`tests/test_docs_consistency.py`): the README CLI
  table is checked against the dispatcher's own command list in both
  directions, the status-icon table against `STATE_ICONS`, both editions
  against each other, and the setup-claude-md section for the disclosure and
  no-embedded-copy properties above. The 2026-07-27 audit found three drifts
  at once, each invisible until someone read both sides line by line; these
  turn that class of review into a test run.
- `ccm send <this project>` no longer reports a misleading `BUSY`. Delivery
  resolves to the pane hosting Claude, so a Claude session addressing its own
  project resolves to the pane it is running in — and the state gate then
  consults a state the caller itself is producing (a session is BUSY precisely
  because it is running the command). Reported as "sending to the other agent
  in my window says BUSY when it isn't": the verdict was real, but it described
  the sender, not the target. Self-delivery is now refused with an explanation,
  including a pointer to `ccm capture`, which is the route that actually reads
  another pane. A sidekick pane sending to the Claude beside it — the supported
  direction — is unaffected.

  Documented alongside it: a project's state describes its **Claude** pane, so
  it must not be used to judge whether a second agent sharing the window is
  free. That agent has no Claude in its pane and contributes nothing to the
  state; only its captured content says what it is doing.
- A resolved permission no longer holds `⚠ PERMIT` forever. Nothing upstream
  reports that a permission was answered — approving fires no hook at all
  (anthropics/claude-code#79651) and dismissing with Esc fires no `Stop`
  either — so a permit event stayed the newest event indefinitely and ccm kept
  claiming the project needed attention. Observed for 15+ minutes on a pane
  sitting at an empty `❯` prompt. Detection now releases a permit whose modal
  is **not** on screen once the session log has been frozen past
  `CCM_PERMIT_MAX_TIMEOUT` (default 10 min), falling back to IDLE.

  This case was previously left alone because a stale permit with an idle
  screen looked identical to an interactive choice menu still awaiting a
  selection, and aging it risked the opposite error — a menu reading as IDLE.
  Measuring a live menu retired that concern: it renders the footer
  `Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel`, which
  the permit-footer pattern already matches, so a displayed menu is detected
  from the pane and is never released however long it waits. Both halves of
  that measurement are now regression tests, so a future upstream reword fails
  the suite instead of silently turning a waiting menu into a false IDLE.

  `CCM_PERMIT_MAX_TIMEOUT` had been a dead constant since detection moved to
  the event-log path — defined, imported, and referenced only by tests, with
  nothing behind the documented "PERMIT auto-clears after 10 min" promise. It
  is wired up again here, as a knob separate from the BUSY window, and the
  guide now describes what actually happens.

## [0.7.0] - 2026-07-26

### Fixed
- `ccm capture` now captures every pane of a split window instead of only the
  focused one. `capture-pane -t <window>` delivers the window's ACTIVE pane, so
  a two-pane project silently returned half its content — from inside the window
  that was the caller's own pane, and from outside it was whichever pane happened
  to hold focus, making the result non-deterministic. (Same window-vs-pane flaw
  the dashboard preview had before it resolved the tracked Claude pane.) Each
  pane is now printed under a `--- pane %id [role] ---` header, where the role is
  resolved from the process tree (a Claude pane reports a versioned launcher as
  its foreground command, which reads as noise). Single-pane windows print
  exactly as before, with no headers and no extra `ps` snapshot. `CCM_IGNORE`'d
  panes are included and marked `(ignored)`: hiding a pane means ccm does not
  track or type into it, not that it vanishes from a read the user explicitly
  asked for. A side effect worth knowing: pointing `ccm capture` at a session's
  own project is how a Claude session reads the pane beside it.
- `inject-status --fast` no longer runs the periodic maintenance tasks
  (window-name updates, notify-transition cache writes, autosave, and idle
  auto-exit). The fast path bypasses the flock by design so a focus refresh
  can run concurrently with a lock-holding periodic instance — but that also
  meant both instances could pass the idle check for the same window and
  double-send the Escape + `/exit` + Enter sequence, the late copy landing
  in the post-exit shell pane where the literal `exit` kills the user's
  shell. The fast path now only re-renders the status bar; the periodic
  instance owns all maintenance side effects.
- Hooks no longer use the bash-4-only `${var,,}` lowercase expansion in
  `_ccm_instant_notify`. On stock macOS `/bin/bash` (3.2.57) it aborted the
  whole notification path with "Bad substitution" under
  `set -euo pipefail`; replaced with a `tr`-based lowercase that bash 3.2
  accepts.

- `ccm send` re-checks the delivery pane's raw state (`detect_pane_state`)
  immediately before typing. The state gate runs on a `build_project_list`
  snapshot, and the interactive confirmation prompt can block for any length
  of time — a target that transitioned to PERMIT while the operator read the
  preview would have received the message body *inside the permission dialog*,
  breaking the "PERMIT never receives keystrokes" safety story. The send now
  aborts when the re-check sees PERMIT (even with `--force`), SHELL (Claude
  exited — the body would land in a bare shell), or BUSY without `--force`.
  A pane that can no longer be enumerated (tmux hiccup) fails open, matching
  the delivery-pane resolution fallback.
- `ccm stop --all` works from the CLI again. The bash wrapper forwarded the
  flag, but the `stop` subparser used a plain positional, so argparse rejected
  `--all` as an unknown option (exit 2) before the handler ever ran — the
  documented stop-with-`_autosave`-snapshot path was unreachable. `stop` now
  uses the same raw-argv passthrough as `capture`/`send`/`errors`;
  `ccm stop <name>` behaviour is unchanged.
- The dashboard no longer crashes with `IndexError` when `p`/`n`/`r`/`i` is
  pressed while a background-session row is selected (or the selection went
  stale). These keys now guard `0 <= selected < len(projects)` like Enter
  already did.
- The dashboard's exit-all and the attach-path auto-start no longer type into
  the window's *active* pane. `send-keys -t <window>` lands on whatever pane
  has focus — a shell pane would receive the literal `/exit`, an editor would
  receive the launch command as text. Exit-all now resolves the Claude pane
  via `enumerate_window_panes` (skipping windows where none resolves), and
  `auto_start_claude` resolves a shell pane (`SHELL_FOREGROUND_COMMANDS`,
  refusing to send when ambiguous) before typing `CLAUDE_CMD` — the same
  delivery-pane policy as `ccm send`.
- `ccm doctor` no longer crashes with `FileNotFoundError` when a probed
  binary (tmux, claude, jq, fzf) is missing — exactly the situation doctor
  exists to diagnose. Probe failures now render as "not found" rows.
- `ccm attach` detects Claude on *any* pane of the window, not just the first
  (a second-pane Claude no longer triggers a spurious auto-start), and treats
  a failed/non-zero `ps` probe as "Claude may be running" instead of
  auto-starting a duplicate.
- Windows sharing a project directory are no longer clobbered. The second
  window of a same-directory project was absent from `project_states`, so
  `update_window_names` renamed it to a bogus `● IDLE` every cycle and
  `ccm send <its-name>` was refused as unregistered. Untracked windows now
  inherit state from their tracked same-directory sibling (matched by
  canonical realpath), are skipped when no sibling exists, and `send`
  resolves them through the sibling for gating while still delivering to the
  addressed window's own pane.
- Digit-only project names are rejected by `validate_name` (`ccm add` /
  `register` / `rename`), since they collide with window-index addressing in
  `ccm send`/`attach`. Name matching now also takes precedence over numeric
  index interpretation, so an existing digit-named project stays reachable by
  name.
- `signal_age_suffix` no longer spawns a `tmux list-windows -a` subprocess
  per project per render cycle. The bulk-fetched `@ccm_session_id` is now
  carried on `Project.cached_session_id` and passed through from the
  dashboard annotation loop, `inject_status`, and `print_status`; SHELL
  windows and the `read_events_tail` path pass an authoritative empty id
  instead of re-querying tmux every scan. (This closes the last gaps in the
  documented no-N+1 design.)
- `ccm status` columns no longer shift with the per-state ANSI color-code
  length (256-color sequences are longer than single-color ones). Padding is
  now display-width based, and over-long project names are truncated with
  `truncate_to_width` instead of pushing the table out.
- `$HOME` → `~` shortening now only rewrites a leading `$HOME` prefix (new
  `shorten_home` helper). The previous global `str.replace` corrupted paths
  like `/Users/x2/work` (→ `~2/work`, breaking snapshot restore) and
  misrendered paths containing the home string mid-path.
- `acquire_pidfile` verifies the recorded PID still belongs to a dashboard
  process (`ps -p <pid> -o command=`) before SIGTERM/SIGKILL. A PID recycled
  after an unclean dashboard exit no longer gets an unrelated process killed;
  probe failure fails safe (leaves the process alone).
- A delayed `idle_prompt` notification no longer deletes a fresh BUSY signal.
  The old guard only spared signals timestamped in the same second or the
  future, so an idle_prompt arriving 10–60s late (its documented delay) could
  flip a working session to IDLE — a path into idle auto-exit killing an
  active session. Deletion is now skipped when the BUSY signal is younger
  than `CCM_IDLE_PROMPT_GUARD_SEC` (default 60; `0` restores the old
  behaviour).

### Changed
- The dashboard preview now shows the tracked Claude pane, not just the focused
  pane. `capture-pane -t <window>` grabs the window's active pane, so in a split
  window with Claude in a non-active pane the preview showed a shell, an editor,
  or a `CCM_IGNORE`'d sidekick instead of the session ccm tracks. The preview
  target is now resolved like `ccm send`'s delivery pane — the active pane if it
  hosts a non-ignored Claude, else the single/first non-ignored Claude pane,
  else the window (unchanged for single-pane projects) — and never a hidden
  sidekick.
- The dashboard now holds its row order stable while open. Previously
  `build_project_list` re-sorted by state on every refresh, so a project
  changing state (BUSY→IDLE, a new PERMIT, …) reshuffled the rows — and
  because the selection is a positional index, the highlight could jump to a
  *different* project mid-interaction. The order is now frozen at open
  (state-sorted, as before), later refreshes follow it, projects opened while
  the dashboard is up append at the end, and the selection is pinned to its
  project by identity so it never lands on the wrong one. A state change still
  updates a row's icon/color in place; it just no longer moves the row. Each
  popup open is a fresh process, so reopening re-decides the order from
  current state.

### Added
- Presence badge for external agent CLIs. A window with a pane running an
  external agent CLI (a small allowlist of known commands, matched on
  `pane_current_command` from the existing bulk panes cache — zero extra
  tmux subprocesses) now shows a dim `⚙<name>` badge next to the `[N]`/`⊘`
  annotations in the dashboard, `ccm status`, and the mode-2 status lines.
  A window hosting only an external agent (no Claude) keeps its honest
  `SHELL` state and gets a dim `(name)` note instead. Display-only: no
  state detection, hooks, or send integration for those panes.
- `CCM_IGNORE` — hide a Claude Code session from ccm. Launch with
  `CCM_IGNORE=1 claude`, or toggle at runtime with `ccm ignore [project]` /
  `ccm unignore [project]` (dashboard `i` key). An ignored pane is dropped
  from window state aggregation, session tracking, `ccm send` delivery, and
  idle auto-exit, and its hooks fire no signals, events, or desktop
  notifications. The intended use is running a second Claude Code session as
  a manual sidekick in a split pane of the same window (a main pane plus a
  second session launched with `CCM_IGNORE=1` alongside) without the sidekick
  confusing ccm's tracking of the primary session. A dim `⊘` marks the row in the
  dashboard and `ccm status`; opt into a per-pane label with
  `tmux set -g @ccm-ignore-pane-border on`. (Because a process's environment
  can't be read from outside on macOS, the ignored session marks its own
  pane via its hooks — `$TMUX_PANE` pane option for detection plus a
  per-session marker file for hook suppression.) Note: ignore stops ccm from
  tracking the sidekick, but cannot immunize the primary session's JSONL from
  the upstream same-cwd task-notification leak (anthropics/claude-code#48112)
  if the sidekick runs background tasks concurrently.

### Added (internal)
- Test-suite isolation guard: an autouse `block_live_subprocess` fixture in
  `tests/conftest.py` makes any real `tmux`/`ps`/`jq`/`osascript`/
  `terminal-notifier`/`notify-send` invocation from a test fail fast
  (`@pytest.mark.live_subprocess` opts out). Previously the suite issued
  hundreds of real tmux calls and even renamed the developer's live windows.
- Test coverage for previously untested paths: `cmd_attach` / `cmd_capture` /
  `cmd_debug_trace`, `notify()` dispatch and AppleScript escaping, the
  dashboard's interactive handlers (rename/remove/ignore/add/register/
  search), pidfile identity checks, and bats cases for the idle_prompt BUSY
  guard.
- Documentation drift: README's claim that the dashboard/status bar show only
  the current session's projects (they show all sessions), the default status
  bar mode (2, not 0), the `--start` wait (polls up to
  `CCM_START_WAIT_SEC` = 10s, not a fixed 2s), the auto-lowered
  `status-interval` (previously documented as a manual step; the previously
  undocumented `CCM_STATUS_INTERVAL` is now covered), the opt-in nature of
  the `T`/`C` key bindings, the stale `status-right-original` troubleshooting
  step, and the "BUSY is trusted while the process lives" note (it times out
  after `CCM_BUSY_HOOK_JSONL_WINDOW`). CLAUDE.md's subcommand list now
  includes `rename`/`search`/`tree-interactive`/`inject-status`/
  `reset-window`/`clear-notifications`. English and Japanese docs updated in
  sync.
- The zsh completion caught up with the CLI: `send`/`ignore`/`unignore`/
  `reset`/`reset-window`/`search`/`debug`/`clear-notifications`/`version`
  added (with project-name completion where applicable), and snapshot-name
  completion no longer breaks on names containing spaces.

## [0.6.0] - 2026-07-22

### Added
- Permission mode visibility across projects. Hook scripts now copy the
  `permission_mode` field from Claude Code's hook payload onto each event
  record (`{"ts":…,"type":…,"mode":"acceptEdits"}`), and the newest value is
  surfaced as a per-project badge: a `MODE` column in `ccm status` and a
  `{label}` annotation in the dashboard (suppressed for the everyday `manual`
  mode to keep rows quiet; `bypassPermissions` renders in warning color).
  Rationale: modes that auto-resolve dialogs (auto / dontAsk /
  bypassPermissions — and acceptEdits for file operations) never fire
  PermissionRequest, so "this project never shows PERMIT" is normal there and
  easy to misdiagnose as broken detection. The badge is display-only — state
  detection never reads it, and records without the field (pre-upgrade hook
  scripts) degrade to no badge. Payload value `default` renders as `manual`,
  matching the CLI vocabulary (`--permission-mode manual`).
- Hook-silence canary firing log. Each `@ccm-hook-silence` warning now also
  appends one JSON evidence record (project / state / jsonl_age / gap /
  timestamp) to `~/.local/share/ccm/state/hook-silence.log`, rate-limited to
  one record per project per 10 minutes (`CCM_HOOK_SILENCE_LOG_INTERVAL`).
  `ccm doctor` reports the recorded firing count. This turns the
  observe-first dogfood into a reviewable dataset for the canary's future
  default-on promotion ("zero false fires" / "caught a real silence" are now
  checkable claims instead of recollections).

### Added (internal)
- Regression tests for executable bits in the git index
  (`tests/test_permissions.bats`): plugin entry points (`ccm`, `ccm.tmux`),
  every `hooks/on-*.sh`, and dev scripts must be recorded as 100755, and
  `hooks/lib.sh` must stay source-only 644. Guards against the
  Dropbox-normalizes-local-permissions accident where a newly added script's
  forgotten `chmod +x` ships as 644 via TPM and breaks user installs while
  the local checkout keeps working (same incident class as the 2026-07 gem
  releases, engtagger#20 et al.; ccm's git-based distribution is immune to
  the 0600 variant, leaving the exec bit as the one remaining exposure).

### Changed
- Sub-second state updates for the dashboard and status bar. The dashboard's
  refresh loop is now hybrid: full detection still runs every 2 seconds, but a
  lightweight fast tick (4×/s, one `list-windows` call ≈10 ms) overlays
  *transitions* of the hook-written `@ccm_prev_state` channel in between, so
  hook-driven changes appear in ~0.3 s instead of up to ~2.3 s. The overlay is
  transition-gated on purpose: reacting to the absolute pushed value would
  re-fight legitimate divergences (HOLD_NO_WRITE displays like the startup
  transient) and flicker on a 2 s cycle. On the status-bar side,
  `ccm_write_signal` now spawns `ccm inject-status --fast` (backgrounded,
  ≈0.3 s, read-only cached-state render) on real state transitions, so mode-0
  and mode-2 surfaces — whose content is baked text that a plain
  refresh-client cannot update — re-render immediately instead of lagging one
  status-interval. BUSY→BUSY hook bursts spawn nothing.

### Fixed
- The `⊘` sidekick marker no longer lingers after a hidden sidekick's Claude
  Code exits. `@ccm_ignore` lives on the tmux pane, but the ignored session's
  own SessionEnd hook early-exits, so the marker was never cleared when the
  sidekick closed (its pane survives as a shell) — the `⊘` stayed on the row,
  and a new Claude later launched in that pane would have been silently
  ignored. The `⊘` count now requires the ignored pane to actually host a
  claude process, and a stale `@ccm_ignore` (marked but no claude) is unset
  during detection, so the marker disappears when the sidekick exits and the
  pane is tracked normally again.
- The opt-in hook-silence canary no longer misfires when a `CCM_IGNORE`'d
  sidekick shares the window's directory. The canary read the newest JSONL in
  the cwd's slug directory (newest-by-mtime), so an active sidekick's fresh
  writes were paired against the tracked session's own (possibly idle) event
  log — two different sessions — and it warned "hooks appear silent" on every
  sidekick turn. The JSONL read is now scoped to the tracked session
  (`read_jsonl_tail_info_for_session`), so both sides of the comparison belong
  to the same session and only a genuine silence fires.
- Active-work spinner detection now matches token counts below 1000. The
  spinner footer renders "(1m 39s · ↓ 557 tokens)" — no `k` suffix — until
  the count crosses 1000, and `PATTERN_ACTIVE_SPINNER` required the `k`, so
  every young turn's spinner was invisible to raw detection. In accept-edits
  mode (composer visible while tools run) that meant false IDLE for the
  opening stretch of a turn whenever the hook/JSONL layers had nothing to
  promote with — observed 2026-07-22 on a session suffering session-long
  upstream hook silence, where the spinner was the only live BUSY signal on
  screen and the mandatory `k` discarded it.
- Idle auto-exit no longer treats a parked editor or pager in a sibling pane
  as background work. The background-work guard (added 2026-07-11 after a
  sibling-pane batch job was interrupted) counted any non-shell foreground —
  including an idle nvim — as live work, so a split-editor workflow silently
  disabled `@ccm-idle-timeout` entirely (observed: sessions idle for 3-4 days
  with the timeout set to 10 minutes). Editors/pagers
  (`PARKED_FOREGROUND_COMMANDS`: vim, nvim, emacs, less, man, …) are now
  exempt. This is safe on both fronts: actively using an editor refreshes
  `window_activity` and resets the idle timer on its own, and exiting Claude
  leaves the sibling pane untouched. Autonomous work (batch jobs, dev
  servers, `tail -f`, live Bash tool shells under Claude) stays guarded.

## [0.5.3] - 2026-07-17

### Fixed
- `ccm send` now delivers keystrokes to the pane that actually hosts the claude process instead of the window target. Window state is a pane aggregation (PERMIT > BUSY > IDLE > SHELL), but `send-keys -t <session>:<idx>` lands in the window's ACTIVE pane — in a split window the two can disagree. Reported 2026-07-16 by the ringi session: a two-pane window (Claude idle in the side pane, active pane a bare zsh) aggregated to IDLE, so a `ccm send --start` concluded no launch was needed and typed the entire message into zsh, producing a flood of `command not found` (and, with a less lucky body, arbitrary shell execution — the message content ran AS COMMANDS). The user-visible symptom reads as "ccm misdetected a shell pane as IDLE", but detection was correct for the window; the bug was that delivery and state referred to different panes. Fix: `_resolve_delivery_pane` enumerates the window's panes before sending and targets the claude-hosting pane id directly (active pane wins when it hosts claude; a single claude pane wins otherwise; multiple claude panes with a non-claude active pane refuse as ambiguous — an Agent Teams split where guessing could inject a prompt into the wrong teammate's conversation). The SHELL + `--start` launch path now also verifies the active pane's foreground is really a shell before typing the launch command (a claude-less pane reads SHELL even when vim is in the foreground — typing `claude --continue` into vim would edit text, not start Claude). All capture-pane reads in the send path (PERMIT classification tail, agents-TUI guard, delivery verification) follow the same pane target, so the checks inspect the pane that will receive the keys. Six regression tests replay the incident topology (claude in non-active pane → body goes to the claude pane, never the active zsh), the launch-into-active-shell case, the editor-foreground refusal, both multi-claude branches, and the enumeration-failure fallback to the window target.
- `ccm send` no longer silently drops lines that start with a dash. Each body line is typed with `send-keys -l <line>`; tmux parses any argument beginning with `-` as a flag cluster, so a Markdown bullet (`- item`) or a `--flag` example failed the whole send-keys call with "invalid flag" — swallowed by the tmux wrapper — while the surrounding M-Enter newlines still landed. The receiver got the message with every bullet line missing and a blank line where each had been. This mangled three real cross-project briefs before diagnosis (design replies from a delegated session arriving 2026-07-10/11 with empty 設計/実装 sections, and a contract-knowledge brief arriving 2026-07-14 with its most important section empty — the receiving session flagged it, which is what finally exposed the pattern: dash-led lines vanished, numbered lines survived). The v0.5.0 delivery verification did not catch it because the signature it checks survived in the non-bullet lines. Fix: terminate option parsing with `--` before the literal line (`send-keys -l -- <line>`). A regression test pins bullets, `--flag`-style lines, and asserts NO literal-line send anywhere lacks the terminator.
- Status-bar layout math now measures CJK names in terminal columns, not characters. Both the mode-1 width budget and the mode-2 entries-per-line estimate used `len()` on the stripped entry text; CJK characters occupy two columns each, so a CJK project or branch name was undercounted by half, overestimating the remaining space and overflowing/wrapping the bar rows. Project names may legally be CJK (`validate_name` only strips shell metacharacters), so this was reachable with a single `ccm add <dir> 日本語名`. Found in a 2026-07-13 audit prompted by the non-ASCII slug bug below; both sites now use the existing `display_width` helper (dashboard and `ccm status` rendering already did). A regression test proves the layout allocates more lines for CJK names than for same-character-count ASCII names.
- Projects whose directory path contains non-ASCII characters (e.g. Japanese) no longer hold an indefinite false BUSY. Claude Code's JSONL slug replaces EVERY non-alphanumeric character with `-` (`/ほげ/ふが2000` → `------2000`, one dash per CJK character; `test_project` → `test-project`), but ccm's `_project_slug` replaced only `/` — so for any non-ASCII project path the session JSONL was simply never found (`jsonl_age=-1`, no stop_reason). That blinded every JSONL-dependent release path at once: a trailing `stop` event could not be confirmed terminal (the pause-class branch stays conservative on missing data), the recap-phantom guard's freshness bound never applied, and even the combined-stale expiry could not fire (it requires a valid `jsonl_age`) — observed 2026-07-13 on gc-gakkai, stuck at BUSY indefinitely after a completed turn. Both slug computation sites now apply the full `[^A-Za-z0-9]` → `-` sanitisation, verified against every existing slug directory in `~/.claude/projects` (ASCII paths are unaffected — `/` and `-` both map to `-`). Fix confirmed live: gc-gakkai's JSONL resolved immediately and the window released to IDLE. Regression tests pin the CJK slug (verbatim from the incident), underscore, and dot/space handling.

### Changed
- Auto-exit now skips windows with live background work, and announces itself when it does fire. Reported 2026-07-11: a monadic-chat window hosting a long batch job in a split pane went quiet, the 10-minute idle timer expired, and ccm exited the Claude session out from under a project the user considered active — the exit arrived unannounced and read as a mystery timeout. The `window_activity` extension only protects a job while it PRODUCES OUTPUT; a silent job, or the waiting period after output stops, was unprotected. Two changes, both governed by cost asymmetry (wrongly exiting interrupts real work; wrongly keeping costs one idle Claude process): (1) before exiting, `auto_exit_idle` checks the whole window for live work — any sibling pane whose foreground is a non-shell command (batch job, dev server, `tail -f`, an editor), or a live shell child under the Claude process itself (Claude spawns a shell per Bash tool job, foreground or `run_in_background`, and MCP/LSP workers are never bare shells, so the standing worker set does not false-positive) — and skips the window if found. A window permanently hosting a dev server will effectively never auto-exit; that is the intended trade-off. (2) A completed auto-exit now sends a desktop notification ("auto-exited after Nm idle — the conversation restores on next attach") fired only after the shell-foreground gate confirms the exit actually landed; it bypasses the `@ccm-notify` per-state opt-in list (announcing an autonomous destructive-looking action must not depend on the user having predicted it) but honors the global `off`. Check runs only inside the already-rare timeout branch, so the steady-state poll cycle pays nothing. Eleven regression tests cover the guard's boundaries (sibling job / idle-shell split / login-shell dash prefix / Bash-tool shell child / MCP children don't block), the notification (fires on confirmed exit only), and the gating bypass.

## [0.5.2] - 2026-07-11

### Added
- Hook-silence canary (opt-in, observe-first). Detects the upstream failure mode where a session's hook event log stops updating while the conversation is demonstrably still active — the #16047-class regression where hooks silently stop firing mid-session (the 2026-07-04 jwriter incident: hooks silent through a whole real turn). When that happens ccm's precise event-log detection goes blind and falls back to the coarser raw+JSONL heuristics, where false BUSY/IDLE become possible; the canary surfaces "detection is degraded for project X, and it's upstream, not ccm" in `ccm status`, `ccm doctor`, and the dashboard footer. The signature is fresh JSONL real-activity whose timestamp leads the newest hook event by a wide margin (default: activity within 90 s, event log 120 s+ behind it) — real work the hook log never recorded. Requiring the event log to EXIST and to lag by a wide margin keeps it clear of the benign look-alikes: a long tool run freezes the JSONL too (so it fails the freshness gate), a slash command is already filtered out of JSONL activity, and startup has no event log yet. It is **default OFF** during the observe-first phase — opt in with `tmux set -g @ccm-hook-silence on` — so a mis-tuned threshold can only ever mislead an operator who explicitly asked to watch it, never a default user. Thresholds are env-tunable (`CCM_HOOK_SILENCE_FRESH` / `CCM_HOOK_SILENCE_GAP`). The detector is a pure predicate (`hook_silence_suspect`) with full unit coverage; the wiring reuses the same mtime+size caches the detection cycle just populated (no extra file reads) and lives entirely off the detection hot path. To be promoted to default-on in a later release once real-session dogfooding confirms zero false fires.

### Fixed
- Reloading `.tmux.conf` (or re-running TPM) no longer stacks a duplicate focus-refresh hook per reload. `ccm.tmux` registered its `session-window-changed` hook with a blind `set-hook -ga`, so every re-source appended an identical copy — observed live 2026-07-11 with two entries firing a double `--fast` render on every window switch (harmless but wasteful, and unbounded over a long-lived server). Registration is now guarded by an append-once check that matches the distinctive `inject-status --fast` command substring, so a path change (e.g. a symlinked plugin dir) still counts as already-registered instead of accumulating a second variant.
- The mode-2 status bar no longer freezes solid when projects outgrow tmux's status-line limit. tmux's `status` option accepts at most 5 lines — with 27 registered projects the mode-2 layout computed 4 entry lines and issued `set -g status 6`, which tmux rejected ("unknown value: 6"); and because the whole mode-2 render is a single `;`-chained `tmux_batch`, the rejection aborted every subsequent `status-format` write in the chain. Every render path — the periodic 1 s tick, the focus-switch hook, manual `ccm inject-status` — failed identically and silently (stderr swallowed), so the bar froze at the last good bake: stale states (a BUSY project shown as SHELL), a stale focus highlight stuck on a window the user had long left, and newly registered projects missing from the bar entirely. Reported 2026-07-11 as "the focus mechanism broke"; live instrumentation of the server-context render revealed the rejected batch. Two fixes: (1) the layout now clamps entry lines to tmux's ceiling (`_TMUX_STATUS_MAX_LINES` = 5 total, so at most 3 entry lines after the main bar and gutter), packing overflow into the last line via the existing pack-don't-drop logic — verified live: the bar revived immediately and the highlight tracks window switches again; (2) `tmux_batch` now records any non-zero tmux exit to the silent-exception log, so the next chain-aborting bad value surfaces as an `errors.log` burst (which the existing burst canary flags in `ccm status`/dashboard within minutes) instead of a days-long silent freeze. Regression tests pin the ceiling (27-project incident shape, a 60-project degenerate case, no-projects-dropped packing, and the small-count layout unchanged) and the batch-failure logging.
- A recap-moment phantom `SubagentStart` no longer sticks the dashboard at BUSY while the pane sits idle. Observed live 2026-07-07 on monadic-chat: a real turn ended around 21:25 WITHOUT a Stop event (hook silence), leaving a permit-class event as the newest entry — a false PERMIT for ~15 min while the `❯` composer sat idle. Then at 21:40:53, when the user returned to the session, a recap `system/away_summary` JSONL record landed and a phantom `SubagentStart` hook fired at the same instant; that lone subagent became the newest event, so `derive_state_from_events` resolved BUSY and the dashboard stuck there (raw=IDLE — ❯ visible, no spinner — the whole time). This is a new variant of the jwriter phantom fixed below in this release: here the subagent is preceded by a permit sequence, not a rest marker, so neither the rest-marker phantom strip nor the all-subagent guard applies. Fix: a stale-BUSY-vs-idle-screen guard in `map_activity_to_state` — when the event log resolves to BUSY but raw=IDLE (a genuinely working session always shows a spinner → raw=BUSY) and the JSONL has recorded no real activity for longer than the long-tool window (`BUSY_HOOK_JSONL_WINDOW`, 600 s), the event log is stale, so derive defers and legacy commits raw=IDLE → IDLE. PERMIT is deliberately excluded from the guard: permit-latest + raw=IDLE + stale JSONL is genuinely ambiguous with an interactive choice menu whose `❯` selector matches the input prompt (the 2026-05-08 case), which must stay PERMIT — there is no spinner to disambiguate as there is for BUSY. Every long-tool / menu corpus probe carries fresh JSONL (tool_use within minutes), well inside the window, so none is affected; the incident is pinned by a new trace-replay fixture plus map-layer unit tests for both the guard and its boundaries. Note: this shares a root cause with the hook-silence canary added above — a turn ending without a Stop event — but the canary correctly abstained here (it fires on fresh-JSONL-with-frozen-events, and this session's JSONL was itself frozen), confirming the two mechanisms address distinct failure shapes.
- A lone phantom `SubagentStart` event no longer holds a false BUSY for the full 10-minute staleness window. Claude Code sometimes fires a phantom SubagentStart outside any turn (observed at the recap moment when the user returns to an idle session), and in the 2026-07-04 jwriter incident this combined with a second upstream anomaly — hooks going completely silent through a real turn (no prompt / pretool / stop events at all, #16047 class) — to produce an events log containing NOTHING but one subagent event. The existing phantom-subagent strip requires a preceding rest marker (`notify_idle` / `stop` / `session_end`) to anchor against, so it could not fire; the lone subagent then classified as start-class → IN_PROGRESS → BUSY until the combined-stale window (~10 min) expired, while the user looked at an idle composer. The `_strip_phantom_subagents` all-subagent branch even documented the intended behavior ("the classifier will fall through to UNKNOWN") — but no such guard existed. `classify_activity` now implements it: an events log consisting only of subagent events returns UNKNOWN, deferring to the legacy raw+JSONL path (which correctly resolved IDLE within seconds in the incident). Real subagent work is unaffected — a SubagentStart always happens inside a turn, so with healthy hooks its prompt/pretool context precedes it (covered by a new no-over-trigger test), and even with silent hooks real work still surfaces as BUSY via the spinner/process-tree raw signal or fresh JSONL `tool_use`. Regression tests replay the exact incident at three timestamps.
- `ccm debug trace` now runs the real two-path detection (event-log derive primary, legacy fallback) instead of the legacy rule table alone, and prints the derive result in a new `ev=` column. The trace's own docstring warns that observing a different detection path "defeats the trace tool" — yet it called `evaluate_rules()` directly, skipping `derive_state_from_events` entirely. During the jwriter incident above it printed `default → IDLE` while the live pipeline was resolving derive=BUSY, sending the investigation down the wrong path (the observer itself was the blind spot). Still read-only: the trace calls `resolve_state_from_context`, which has no side effects.

## [0.5.1] - 2026-07-04

### Changed
- The status bar reflects the focused project immediately on a window switch instead of waiting up to `status-interval` for the next tick. The mode-1/2 status bakes the "current window" highlight into a static status string when `inject-status` runs, so the highlight only moves when `inject-status` re-runs. A `session-window-changed` hook now re-runs `inject-status --fast` on every window switch. Two pieces make it instant: (1) `--fast` skips the full detection pass (~250 ms) and re-renders from the cached `@ccm_prev_state` (~10 ms), since a switch changes only WHICH window is current, not any project's state; (2) the fast path issues an explicit `refresh-client -S` afterward, because — unlike the periodic path driven by the status-right `#(...)` whose re-run inherently redraws — setting `status-format` from a hook does not force a screen redraw on its own, so without it the new highlight still waited for the next tick (the exact lag being removed). The hook is appended with `-ga` so a theme/user hook on the same event is preserved, runs with `-b` so the switch stays snappy. The focus refresh also bypasses the inject lockfile (it is read-only on `@ccm_prev_state`, so it cannot cause the state flicker the lock guards against): otherwise a switch landing while a periodic full-detection tick held the lock — about a quarter of the time at `status-interval` 1 — would silently drop the refresh and the highlight would stall until the next tick, the intermittent "dashboard select didn't move the status bar" symptom. New `ccm inject-status --fast` flag forces the cached-state render.

### Fixed
- The dashboard now self-heals when tmux draws a background pane's output over the popup. With a window actively streaming a response (double-width CJK output especially) behind the open dashboard, tmux's popup overlay clipping (observed on 3.7b; upstream has been churning in this area — PR #4920 fixed one shape in 3.7, PR #4997 another) sometimes paints the underlying pane's rows INSIDE the popup region, garbling the project list and preview. Worse, the damage was permanent: curses diffs each refresh against its own model of the physical screen, so it believed the clobbered cells were still correct and never rewrote them. `_render_current` and `_render_search` now call `stdscr.redrawwin()` before drawing, marking the whole window corrupted so every render re-emits every cell — renders run on each keypress and each 2 s refresh tick, so corruption heals within ~2 s instead of sticking until the row's content happened to change. Full re-emit of an 80%×60% popup over the local tmux socket is negligible. The root cause is upstream tmux overlay clipping; this is a defensive repaint, not a workaround that could mask other bugs (it changes no state, only forces honest redraws). Regression test asserts all three modes force the full repaint.
- Running a local slash command (`/model`, `/status`, `/clear`, …) at an idle prompt no longer flips the window to BUSY for up to ~10 minutes. A slash command writes up to three `user` records to the session JSONL — `<command-name>…` and `<local-command-stdout>…` (no `isMeta` flag) plus a `<local-command-caveat>…` record (`isMeta: true`) — none of which triggers an assistant turn. When hooks are silent for that session (slash commands fire no `UserPromptSubmit`/`PreToolUse`), detection falls back to the JSONL bridge, which saw the fresh `user` record as the tail after a terminal assistant and synthesized `user_pending`; the `jsonl_user_prompt_pending` rule then read that as "Claude is thinking about a new prompt" and showed BUSY until the `BUSY_HOOK_JSONL_WINDOW` (~10 min) elapsed. Confirmed live 2026-07-02: switching jwriter's model with `/model` left the dashboard and status bar at ◉ BUSY while Claude was plainly idle at the composer. Fix: `_parse_jsonl_tail` now skips `user` records that are local-command wrappers — identified by `isMeta: true` OR a leading content tag in `JSONL_LOCAL_COMMAND_PREFIXES` (`<command-name>`, `<local-command-stdout>`, `<local-command-caveat>`, …) — so they neither count as real activity nor drive the `user_pending` promotion. `isMeta` alone was insufficient because only the caveat record carries it; the two content-tagged records do not. Genuine prompts (no such flag or prefix) still promote normally, and the skip only ever makes the JSONL look *less* fresh, so it can never manufacture a false BUSY. Two regression tests cover the full three-record `/model` sequence staying at `end_turn` (→ IDLE) and a real prompt after slash-command records still promoting to `user_pending`.
- The dashboard no longer sticks at PERMIT while a background subagent's tool runs. When a permit event is the latest hook signal and `raw=BUSY` (capture-pane shows an active-work spinner, or children running with no prompt), the state now promotes to BUSY unconditionally — previously it also required the main session's JSONL to show a fresh `tool_use`, which a background subagent's WebFetch does not: its `tool_use` lands in the SUBAGENT's JSONL, so the main session showed `end_turn`/none while the fetch ran for minutes, and the dashboard stayed at ⚠ PERMIT with a spinner visibly running (2026-06-30 monadic-chat incident). `raw=BUSY` is authoritative on its own: a real permission wait BLOCKS execution (no spinner) and its options make raw PERMIT or IDLE, never BUSY, so promoting it cannot hide a genuine pending permit. The JSONL-freshness gate now governs only the `raw=IDLE` promotion (where the JSONL is the sole activity evidence), which keeps the interactive-menu case correctly at PERMIT. Two tests that pinned the old JSONL-gated behavior were updated and a subagent-WebFetch regression test added.
- Footer-less permission dialogs are now detected as PERMIT. Some permission prompts (observed 2026-06-26 on a WebFetch / web-content request raised by a background subagent: `Do you want to allow Claude to fetch this content?`) carry no separate `Esc to cancel · Tab to amend` footer — the `(esc)` is inline on the deny option (`N. No, and tell Claude what to do differently (esc)`). `PATTERN_PERMIT_FOOTER` matched none of its existing alternatives, so the dashboard showed IDLE for a blocking dialog the user had to answer. The deny-option line is now a third PERMIT signature, anchored both by a leading numbered-option prefix (`\d+.`) and a trailing inline `(esc)`; the `(esc)` requirement keeps a Claude response that merely quotes the option text in a numbered list (including a conversation about this very detector) from false-triggering PERMIT, and footer'd dialogs are unaffected since they match the `Esc to cancel · …` alternative. `PATTERN_PERMISSION_DIALOG` also gained the `Do you want to allow Claude to …` question and the same deny-option line so `ccm send` classifies these as the dangerous permission-request kind rather than a safe confirmation modal. Verified live against the paused dialog (`detect_window_raw → PERMIT`). Known limitation: the underlying cause is that a subagent's `PermissionRequest` hook fires under the subagent's own session_id, which ccm's main-session hook lookup misses, so this footer fallback only catches the dialog while it is visible in the pane (see memory `project_known_limitations.md`).

## [0.5.0] - 2026-06-25

### Added
- Trace-replay regression corpus (`tests/test_trace_replay.py` + `tests/fixtures/traces/`). Real captured event logs from production incidents are replayed through `derive_state_from_events` with probe points asserting the expected state along the timeline. Every detection bug ccm has had shares one shape — the upstream hook vocabulary has holes (no "permission resolved" event, no tool heartbeat, Stop missing on Esc) and ccm bridges them with heuristics over noisy side channels, where each bridge trades off against the others. The corpus makes those trade-offs executable: a fix for one incident can no longer silently regress another. First two fixtures capture the two sides of one such trade-off — an approved permission followed by a 12-minute tool run (dashboard wrongly showed PERMIT) and an `AskUserQuestion` menu wait (PERMIT is correct) — which are provably indistinguishable on all three detection signals (event-log tail, JSONL stop_reason, capture-pane raw), so a naive "promote tool_use to BUSY" fix for the first would regress the second. That conflict is now pinned by tests rather than discovered in production; the desired-but-unimplemented fix for the long-tool case is recorded as a `strict` xfail that will flip to an error the moment a real fix lands.
- `CCM_SEND_TRACE=1` opt-in trace mode for `ccm send`. When set, every `tmux send-keys` call made during a cross-project send is appended to `$CCM_TMP_DIR/send-trace.log` as a tab-separated row: `<unix-ts>\t<win_target>\t<label>\t<keys-repr>`. Labels cover the `-X cancel` pre-step, a `send-start` marker with project / line / byte counts, one `line:N` row per non-empty line, `newline:N` between lines, the closing `final-submit`, and a `send-end` marker. Designed for after-the-fact diff against the receiver's perceived content when an operator reports a `ccm send` drop — captures exactly what `tmux send-keys` saw so the ccm layer can be ruled in or out without round-tripping diagnostic test sends. Zero overhead when the env var is unset (one dict lookup per send-keys call on the happy path) and zero side effects in the receiving pane. Falsy spellings (`0` / `false` / `off` / `no` / empty) are treated as off, so an exported env var left enabled by accident does not silently keep logging across sessions. Write failures (read-only $TMPDIR, disk full) are swallowed so a non-writable log directory cannot block message delivery.

### Fixed
- `ccm send --start` no longer silently drops the message into a not-yet-ready Claude. The `--start` path launches `claude --continue` into a SHELL pane and waits for IDLE before sending — but the `❯` composer becomes visible (so detection reads IDLE) a moment before Claude's input handler actually accepts keystrokes. A send during that window is eaten, the body never lands, yet ccm printed "Sent". Confirmed live 2026-06-24: a `--start` delegation to a freshly-launched target showed "Sent" while the input box held only its placeholder and the body was zero; re-sending after IDLE settled delivered the full text. The earlier `134986d` poll-until-IDLE fix closed the old fixed-2s race but not this premature-IDLE window. Fix: after typing the body but BEFORE the committing Enter, verify a signature of the message actually appears in the target's composer; if not, clear it (`C-u`) and re-type up to twice with a short settle, then refuse honestly (`ccm_die`, no false "Sent") if it still hasn't landed. Verifying before Enter means a retry can never double-submit, and clearing before re-type prevents a partial landing from duplicating. Scoped to the launch path (an already-IDLE target was genuinely ready and stays on the fast path) and skipped for messages too short to match without false positives. Four regression tests cover verified-delivery-then-submit, premature-IDLE-refuses-without-false-Sent (asserts no committing Enter + retried + cleared), retry-then-succeeds, and short-message-skips-verification.
- The dashboard no longer sticks at PERMIT while an approved tool runs (the most common false-state users hit in accept-edits mode). When a Bash command (or any tool) prompts for permission and is approved, Claude Code does not re-fire PreToolUse, so the latest hook event stays `permit_req`/`notify_permit` until the tool finishes — which on a long command (e.g. a 13-minute `rake spec`) is many minutes later. During that window, accept-edits mode keeps the `❯` composer on screen, so `detect_pane_state` read raw=IDLE, and the event-log layer (which cannot tell an approved-running tool from a menu wait — both show permit-latest + JSONL `stop_reason=tool_use` + raw=IDLE) resolved to a stuck PERMIT. The dashboard showed ⚠ for a session that was actively executing. Fix: `detect_pane_state` now returns raw=BUSY when an active-work spinner footer (`<glyph> <verb>… (<elapsed> · <arrow> <N>k tokens)`, e.g. `✻ Building… (27m 26s · ↓ 28.5k tokens)`) is visible alongside the `❯` prompt. That spinner is rendered only while Claude is actively generating or running a tool; it is absent at a true idle prompt and during an `AskUserQuestion` menu / permission wait (Claude has stopped generating to ask) — empirically verified live 2026-06-11 across running panes, idle panes, and a menu wait. With raw=BUSY the existing permit-branch promotion in `classify_activity` yields BUSY, while menu/idle (no spinner → raw=IDLE) correctly stay PERMIT/IDLE. The matched pattern is the structural `(elapsed · arrow Nk tokens)` tail, not the cycling glyph or the localised verb, so it survives glyph/wording churn; the token-arrow counter is effectively absent from normal conversation text. The discriminator lives at the raw layer because that is the only layer with the spinner signal — the trace-replay corpus pins that boundary (the long-tool incident now resolves to BUSY with the spinner-aware raw; `classify_activity` alone with raw=IDLE is documented as still returning PERMIT, so a future refactor that moves the discriminator must be deliberate). Resolves the 2026-06-10/11 monadic-chat and tcse-dev incidents; closes the corpus xfail.
- Adversarial-verification pass (two independent reviewers re-checked this session's own fixes) caught three residual issues:
  - **Snapshot load still crashed on a top-level non-dict JSON.** The malformed-JSON guard added earlier checked `projects` but not `data` itself; `json.load` accepts a top-level array/scalar/null without raising, so a hand-edited `[1,2,3]` reached `data.get("projects")` and threw `AttributeError` — exactly the traceback the guard was meant to replace. Now dies with a readable message; regression tests for both top-level-non-dict and non-list-`projects`.
  - **Auto-exit declared SHELL even when `/exit` hadn't completed.** The SHELL state write and the autosave fired unconditionally after the `/exit` send, including on the race path where Claude is still the foreground process (heavy session, confirmation modal). That was a one-cycle false SHELL the next detection pass had to undo, plus a spurious autosave. All three side effects (`clear`, SHELL write, autosave) are now gated on positive evidence that the pane returned to a shell foreground — a ccm-launched Claude always runs as a shell child, so a completed exit always shows a shell. Multi-Claude (Agent Teams) behavior documented inline.
  - **Auto-exit's focused-window protection vanished if `display-message` returned empty.** With an empty session/window the focused target became ":", matching no real window, so the focused Claude could be auto-exited. Now bails the cycle when the focused window can't be resolved (pre-existing latent issue, hardened while in the area).
- Codebase-wide review (4 parallel audits over detection / runtime / UI / bash layers) fixed a batch of latent issues:
  - **Test runs no longer pollute the user's real error log.** `CCM_ERRORS_LOG` is computed at import time, so tests that monkeypatched `CCM_TMP_DIR` still wrote silent-exception records to the real `$TMPDIR/ccm-$UID/errors.log` — `ccm errors` then showed test scaffolding bugs as if they were production failures. A new autouse conftest fixture redirects the log (and its rotation sibling) to a per-test tmp path. This was discovered the hard way: a `dashboard._refresh_loop AttributeError: bg_visible` entry in the user's real log turned out to be a stale `Dashboard.__new__` scaffold in test_silent_exceptions.py that was never updated when the bg-section feature added the attribute.
  - **The dashboard refresh-loop smoke test now actually fails on swallowed exceptions.** Its stated mission was "the loop must complete without NameError/AttributeError", but the loop's own `except Exception: log_caught_exception(...)` caught exactly that class of bug, so the test passed while its scaffold was broken. It now records `log_caught_exception` calls and asserts none happened, and the scaffold includes `bg_visible` / `bg_sessions`.
  - **`_refresh_loop` reads `bg_visible` under the lock.** The main thread toggles it (with the lock held) while the background thread read it unlocked — snapshot it under the lock on both the initial and steady-state paths.
  - **`classify_permit_modal`'s footer fallback now works.** `PATTERN_PERMIT_FOOTER` lacked `re.MULTILINE`, so its `^` anchor never matched a footer on the last line of a multi-line captured tail — the fallback branch silently never fired and not-yet-cataloged confirm modals fell through to `unknown-permit`, surfacing the scary "Treat as dangerous" guidance for harmless dialogs. Per-line `.match()` use in detect_pane_state is unaffected by the flag.
  - **`acquire_lockfile` no longer leaks the lock fd** if the diagnostic pid write fails (disk full): the flock would have been held for the process lifetime, silently skipping every subsequent status update.
  - **Snapshot load/delete are TOCTOU-safe and malformed-JSON-safe**: open/unlink directly instead of exists-then-act (the file can vanish between the calls under concurrent delete or Dropbox sync), die with a readable message on truncated/hand-edited JSON, and skip non-dict entries in `projects` with a warning instead of crashing.
  - **`ccm debug trace` parses `ps_snapshot()` consistently** (`.strip()` before `.split` — same as every other call site) and **`cmd_doctor`'s `which`/`tmux -V` subprocess calls have timeouts** like the rest of the module.
  - **`on-notification.sh` extracts the signal state from the correct field.** `${EXISTING##* }` takes the LAST whitespace-separated token, which is only correct while BUSY signals carry no detail text; the parse now strips the timestamp and reads the second field explicitly, so a future detail-bearing BUSY write cannot silently break the same-second concurrency guard.
  - **`_ccm_instant_notify`'s dedup validates marker timestamps are numeric** before arithmetic — a corrupt/truncated marker previously evaluated as 0, making the age look huge and silently disabling dedup (duplicate notifications) instead of failing visibly.
- Auto-exit no longer silently kills user-split shell panes. `auto_exit_idle` was using a window-level send-keys target (`win:idx`), which routes to whichever pane is currently active in the window. If the user split off a shell pane (e.g., to run `ccm update`) and left tmux focus on it while working in another window, the 10-minute idle timer would fire the Escape + `/exit` + Enter sequence at the shell instead of at Claude: in emacs mode `Escape + /` becomes `Meta-/`, a no-op completion on an empty prompt; the literal characters `exit` then fill the buffer, and the trailing Enter submits `exit` to the shell, terminating it. tmux closes the pane when its foreground process exits, so the user came back hours later to find the shell pane gone with no obvious cause. Fix: enumerate `list-panes -t <win> -F "#{pane_index}\\t#{pane_pid}"`, run `find_claude_pid` (the same process-tree lookup the rest of state detection uses) against each pane's pid, and address every keystroke plus the post-`/exit` `display-message` check to the matching pane (`win:idx.pane_idx`). If no pane is currently hosting a Claude process (window transitioning, mid-exit race, user manually exited), defensively skip the cycle and re-evaluate on the next poll. The process-tree path matters because tmux's `#{pane_current_command}` returns the binary basename — for standard claude.ai installs the binary lives at `.../versions/<X.Y.Z>/` with `claude` as a symlink, so macOS's `proc_pidinfo` reports e.g. `2.1.167` instead of `claude`; `ps`'s `comm` field is still `claude` though, so the ps walk stays correct across installs while string-matching the foreground command would silently mis-fire. Three regression tests cover the bug path (shell-active + Claude-on-another-pane → keystrokes still target the Claude pane), the defensive skip (no Claude pane → no send-keys at all), and the version-named binary case (`pane_current_command` is `2.1.167` → ps walk still finds Claude). Reported and observed on the ccm-dev window itself, 2026-06-09.
- Auto-exit cleanup no longer leaks `clear` (or any other follow-up text) into a still-running Claude session. `auto_exit_idle` sends `/exit` then waits 0.5 s before sending `clear` to wipe the screen for the next attach. On sessions with heavy conversation history Claude's shutdown can take longer than that — when it does, the `clear\nEnter` lands as literal text in Claude's input box and is submitted as a stray user prompt (a confused "clear" message in the chat, followed by Claude's response to it). Empirically observed on a long-running blog project session on 2026-05-31, with the same race mechanism explaining an earlier "test" one-word injection. Fix: query `tmux display-message #{pane_current_command}` before the `clear` step; only send `clear` when the pane's foreground command is in `SHELL_FOREGROUND_COMMANDS` (zsh/bash/sh/fish/ksh/csh/tcsh/dash/ash). Empty / unknown / non-shell responses (e.g. `claude` still alive, tmux query timeout, exotic foreground wrapper) all fail-safe to "skip the clear" — the cosmetic cost is a slightly stale screen on the next attach; the safety win is no stray keystrokes can reach a live Claude. The `/exit` step itself is unchanged (it correctly targets the running Claude). Audited every other `send-keys` site for the same shape and found none: all other CLAUDE_CMD sends are either to a freshly-created tmux window (no Claude possible) or pre-gated on `state == "SHELL"`; the dashboard's "Exit all sessions" `/exit` does not follow with `clear`. Three regression tests cover happy path (zsh → clear sent), bug path (claude → clear skipped), and failure path (empty → clear skipped).
- `/model` picker is detected as PERMIT again on Claude Code v2.1.153+. Upstream reworded the picker's footer from `Enter to confirm · … · Esc to cancel` (the prefix `PATTERN_PERMIT_FOOTER` matched verbatim) to `Enter to set as default · s to use this session only · Esc to cancel` — the Enter verb is no longer literally `confirm`, so the pre-fix regex silently dropped the footer and ccm classified the open picker as IDLE. The dashboard showed `●` instead of `⚠`, no PERMIT notification fired, and `ccm send` would have happily delivered keystrokes into the open modal. The regex now matches the structural shape `Enter to <verb> · … · Esc to <verb>` (separator-anchored: `·` or `|`, not `,`), which keeps free-navigation slash menus (/skills, /resume v2.1.144+) correctly excluded while absorbing any future upstream wording changes to the Enter verb. The content-level modal classifier (`PATTERN_MODEL_PICKER`) was already wording-agnostic, so once the footer matches the modal still resolves to `confirmation-modal` and `ccm send` produces the correct guidance.

## [0.4.0] - 2026-05-24

### Added
- `ccm add` and the dashboard's `a` action now offer to create the target directory when the path doesn't exist but its parent does — one-level `mkdir` only, no recursive `mkdir -p`. Dashboard prompts inline; CLI prompts when stdin is a tty (scripts and snapshot-restore keep the original "Directory does not exist" die, so automation contracts are unchanged). The parent-must-exist gate is deliberate: a typo'd deep path is a far more common failure mode than legitimate deep-tree creation, and refusing forces the caller to spell intent. `cmd_add(create_dir=False)` default preserves every existing caller (notably `cmd_snapshot_load`, which must keep skipping with a warning rather than silently re-creating an empty directory the user no longer expects to be there).
- Read-only view of Claude Code's agent-view background sessions (Claude Code 2.1.139's `claude agents` / `claude --bg` / `claude attach`). The per-user supervisor daemon's `~/.claude/daemon/roster.json` plus each session's `~/.claude/jobs/<short>/state.json` are joined and surfaced as a "Background sessions" section in the ccm dashboard. Off by default — window-as-project workflows are unaffected. Toggle on demand with the `b` key, or set `@ccm-bg-section always` for persistent visibility (also reachable via the dashboard menu). Lifecycle (dispatch / stop) stays with `claude` itself; ccm only observes. New `ccm bg list` CLI subcommand prints the same data outside the dashboard. The reader (`lib/ccm_agentview.py`) is strictly read-only and tolerant of missing / malformed daemon files.
- Dashboard `Enter` on a background-session row opens a fresh tmux window and dispatches `claude attach <short>` into it. The new window is intentionally NOT registered as a ccm project (no `@ccm_project` / `@ccm_dir` tags), so ccm's `auto_start_claude` cannot race the attach by injecting `claude --continue` into a SHELL pane first — the structural workaround for the agent-view/ccm auto-start conflict that previously made attach-from-tmux impractical. The window inherits the bg session's working directory when available. Close it with `prefix + &` after detaching from claude.

### Fixed
- Dashboard path column no longer drifts horizontally as the `* elapsed` marker ticks or appears/disappears. Previously the marker lived inline between project name and path, which pushed `COL_DIR` (and therefore every project's path) right whenever the marker was shown, and again every time the counter crossed a 1↔2 digit boundary (`9s` → `10s`, `9m` → `10m`, …). Two-part fix: `format_elapsed` now returns a fixed 3-character right-padded string (`" 5s"` / `"10s"` / `" 1m"` / …), and the dashboard renders the marker in a right-anchored slot at the row's right edge instead of inside the inline annotation cluster. The path's right-clip width is reduced by `ELAPSED_RIGHT_SLOT` (6 cols) constant so a long path never overlaps the marker. New `test_elapsed_marker_does_not_perturb_path_column` regression test pins this — it renders the same project list with and without elapsed and asserts every `format_dir` X-position matches across the two renders.
- `ccm send` now refuses targets that are showing the `claude agents` TUI (Claude Code 2.1.139+, opened via `claude agents` or `← ←` detach). The TUI displays an `❯` input prompt that ccm's state detector reads as IDLE, so the target appears send-able — but keystrokes typed into the agents TUI **dispatch a brand-new agent-view session** rather than landing in any existing Claude conversation. Previously a casual `ccm send <project> "..."` to such a pane would silently spawn an unintended session. The new `PATTERN_AGENTS_FOOTER` regex matches the TUI's footer signature (`enter to open · … · ? for shortcuts`); when seen, the send is refused unconditionally (even with `--force`, mirroring the PERMIT guard's "no safe override" semantics), and the refusal message surfaces the captured pane tail so the operator can confirm classification. Issue 5 from the agent-view findings (2026-05-12) is now closed.
- COMPLETED desktop notifications no longer fire during `/goal`, `/loop`, and `/bg` workflows when Claude Code reports outstanding background work. The Stop hook fires at every turn boundary in multi-turn auto-loops; previously the grace-period sentinel caught some but not all (a `/loop` sleeping past `CCM_COMPLETION_GRACE_SEC` would surface a false "done" alert every iteration). `on-stop.sh` now reads the `background_tasks` and `session_crons` fields added to the Stop / SubagentStop payload in Claude Code v2.1.145+ and skips the `.pending` sentinel entirely when either is non-empty — the user's intent isn't "done" while bg work is pending or a cron is scheduled. Legacy payloads without these fields (older Claude Code) keep the original notify behaviour via `// []` defaulting to empty arrays.
- `PATTERN_PERMIT_FOOTER` now tolerates extra action keys between `Enter to confirm` and `Esc to <verb>`, so Claude Code 2.1.144's new `/model` footer (`Enter to confirm · d to set as default for new sessions · Esc to cancel`) is detected as PERMIT again. Before this fix the regex required the two phrases adjacent, silently dropping the new footer; ccm would have classified the open `/model` picker as IDLE, and `ccm send` would have happily delivered keystrokes that could accidentally confirm a model change in another pane. The intermediate-segment tolerance is opaque (`[^\n]*?`) rather than enumerating known middle texts, so future upstream additions to the same footer shape (e.g. another action key) keep matching without further changes. Verified empirically against Claude Code v2.1.144's `/model` picker.
- Dashboard `* elapsed` completion marker no longer flickers visibly during multi-turn auto-loop commands (`/goal`, and similar shapes). Empirical trace of a 3-turn `/goal` run showed the marker briefly surfacing in ~2 s IDLE windows between auto-fired turns, misleadingly implying the work had just finished when the loop was still mid-flight. State detection was already correct (each gap *is* truly idle); only the visual marker was over-eager. `format_elapsed()` now suppresses the marker for the first `MIN_ELAPSED_DISPLAY_SEC` (3 s) after a BUSY→IDLE transition — conceptually paired with the existing `CCM_COMPLETION_GRACE_SEC=3 s` window that already absorbs the same oscillations on the notification path. Long-form rationale and audit guide (when this code becomes unnecessary, how to verify it's still load-bearing) is inlined as a comment on the constant in `lib/ccm_render.py`.
- `ccm send <name> --start <msg>` no longer silently loses the message when `claude --continue` resumes into an auto-action. The previous fixed two-second wait could deliver keystrokes mid-`/compact` (long-session resume) or into a session-resume picker — the operator saw `Sent to <name>` while the message never reached the prompt. The launch path now polls the target's detected state every 0.5 s and proceeds only when state is `IDLE`. `PERMIT` short-circuits the wait (modal needs operator action; sending keystrokes there could approve/deny tools); the timeout (default 10 s, `CCM_START_WAIT_SEC` overrides) refuses the send with the captured pane tail so the operator can finish by hand. Progress is printed once per second when run interactively.
- Interactive choice menus (Claude Code's option-list UI surfaced as a permit-class hook) showed false `BUSY` for up to `CCM_BUSY_HOOK_JSONL_WINDOW` (10 min) instead of `PERMIT`. The previous heuristic promoted any permit-class event with recent `tool_use` JSONL to BUSY, intending to keep the dashboard responsive during the brief post-accept extended-thinking phase. But the same input shape is also produced by interactive menus where the user is genuinely awaiting selection, so the dashboard claimed activity for the entire reading time. The promotion now requires positive evidence — `raw=BUSY` (capture-pane sees tool output) or JSONL strictly fresher than the permit event (a new `tool_use` record landed post-accept). Anything else surfaces as `PERMIT` immediately. The cosmetic cost is a few-second window of `PERMIT` after a real accept before the next `PreToolUse` fires; the previous behavior's user-visible cost was multiple minutes of misleading `BUSY` on every menu.

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
