# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Store-and-forward spool for `ccm send`. When the target cannot take a
  message (BUSY / PERMIT / SHELL, an agents TUI, or a composer holding a
  draft), the message is written to `$CCM_DATA_DIR/spool/<project>/` and
  the periodic status pass delivers it once the project reads IDLE again.
  Delivery re-detects the pane's raw state and refuses to merge into a
  draft; one message per project per pass, claimed by rename under a
  per-project lock, at-least-once (a crash mid-delivery redelivers).
  Messages older than `CCM_SPOOL_TTL_SEC` (default 60 min) move to
  `expired/` and surface in `ccm status` / `ccm doctor` instead of
  arriving. Delivered messages carry an envelope header
  (`[from: … · queued … · delivered … — reply with `ccm send …`]`).
  New `ccm spool list` / `ccm spool cancel <id|--all> [project]` to
  inspect and withdraw — a queued mis-send is cancellable. Queued counts
  show on `ccm status`, `ccm doctor`, and the dashboard (`✉N`).
- `ccm sidekick-send "<message>"` delivers a prompt to the sidekick agent
  CLI in a split pane of the caller's own window — the relay lane
  `ccm send` deliberately never takes. The target is resolved from tmux
  metadata only (the single pane running a known external-agent CLI
  whose working directory belongs to the project); zero or multiple
  candidates refuse, a sidekick caller is refused (the reverse lane is
  `ccm send <project>`), and the pane identity is re-checked right
  before typing. The send types literally, waits out the peer's
  composer-settle pause before Enter, then captures the pane and exits
  non-zero when no message fragment appears. The guide and the
  `setup-claude-md` template now teach this command instead of the hand
  `tmux send-keys` procedure.
- Auto-exits are recorded in `$CCM_DATA_DIR/state/auto-exit.log` (timestamp,
  project, session id, idle seconds). `ccm doctor` shows the count.
- `@ccm-ambiguous-width 1|2` declares how your terminal draws glyphs whose
  width Unicode leaves open, so the status bar stops reserving room for the
  wider case. `CCM_AMBIGUOUS_WIDTH` remains as a fallback for running ccm
  outside tmux; the option wins when both are set.
- `@ccm-status-line-hide-shell on` lists only windows that host a Claude
  session in status-bar modes 1 and 2. Off by default; `IDLE` projects stay
  visible.
- `@ccm-status-line-position left` places mode 1's entries on the far side of
  the bar, highest priority first, instead of next to the clock. Default
  `right`. `status-left` is not written to.

### Changed
- `ccm send` no longer refuses when the target cannot take the message
  right now — it queues the message to the spool and reports
  `Queued for <project>` (queue length, TTL, message id). This flips the
  default for BUSY / PERMIT / SHELL targets and mid-send transitions:
  scripts that relied on a non-zero exit to detect a non-delivery should
  use `--now`, which keeps the old fail-fast behaviour (PERMIT with
  classification and guidance included).

### Fixed
- `ccm send` no longer types into a composer that already holds a
  half-typed draft. State detection cannot see one (an `❯` prompt
  holding text still reads IDLE), so the message used to merge into the
  user's in-progress text and the committing Enter would submit the
  garbled mix. The delivery path now reads the composer line
  immediately before typing and — while a draft is present — queues the
  message for later delivery (refuses with `--now`), quoting the draft's
  opening fragment.
- Arrowing onto a footer-less dialog's deny option no longer drops the
  detection. The cursor rewrites the line as `❯ 3. Deny (esc)`, which the
  match rejected and the idle-prompt pattern then claimed — an open
  permission dialog reading as idle. The deny-line shape is also a single
  definition shared by the footer match and the modal classifier, so the two
  cannot drift apart.
- The Claude-in-Chrome permission dialog is recognised without hooks. Its
  deny line is `Deny (esc)`, and the footer-less dialog match had fixed the
  deny label to another dialog's wording — so with hooks silent this dialog
  would have read as working or idle. The label is matched as a negative word
  now, and the dialog classifies as a permission request for `ccm send`'s
  warning.
- The instant status-bar update no longer trusts a working directory alone.
  Hooks are user-scope, so a Claude Desktop or VS Code session opened on the
  same directory as a managed project fires them too, and could paint the
  window PERMIT for a prompt that is not in it. The window's cached session
  id now has to agree before the fast path writes state or notifies.
- A window hosting an external agent is marked once, by the `⚙name` badge
  beside the project name. SHELL rows also repeated it as a `(name)` note in
  the state column, and fitting the note widened that column — which happened
  a beat after the dashboard opened, when full detection caught up, shifting
  every row sideways. Column positions no longer depend on what agents are
  present.
- Deleting a project from the dashboard asks for confirmation. It closes the
  window and ends the session in it, yet cost a single keypress while the
  menu route confirmed — the harsher path was the cheaper one.
- The dashboard menu (`m` / `?`) lists unregistering, deleting and ignoring a
  project. Adding one was listed there; the ways back out were reachable only
  behind `r`, which the help line rendered as "remove". The name prompt
  defaults to the selected project, so a plain Enter targets it.
- The settings canaries resolve files in the order Claude Code does, so the
  file they name is the one deciding. `allowManagedHooksOnly` is reported only
  from the administrator's settings, where it has effect.
- The example project name used across the README, guide and tests is a
  synthetic one.
- The `disableAllHooks` and `allowManagedHooksOnly` canaries read the
  administrator's settings and each project's settings as well as the user's
  own file, and name which one carries the flag.
- `stat` in the config-writer tests no longer assumes BSD flags, which failed
  the Linux CI job.
- The status bar re-lays itself when the terminal is resized. Its layout is
  baked from the width at render time, so a resize left it laid out for the
  old width until the next periodic pass — entries clipped on a narrower
  terminal, fewer entries than fit on a wider one. One resize gesture costs
  one render, at the size the gesture ended on.
- Declaring what the terminal draws now also stops the status bar reserving
  room for a glyph width it no longer has to guess at. At `2` the reservation
  was charged on top of a column count that already included it; at `1` the
  layout went on hedging against a case the user had ruled out, leaving
  around 12 columns of empty space after `status-left` in left placement.
  Leaving it unset keeps the reservation, since the width is then unknown.
- Mode 1's left-placed entries no longer sit in the middle of the bar. The
  width budget kept a flat margin from when the parts around it were
  approximate, and once those parts were measured exactly the same slack was
  being reserved twice — up to 20 columns of empty space after `status-left`.
  Every part of the budget is now measured from the strings it draws.
- The status bar reserves room for glyphs whose width the terminal decides —
  box drawing, geometric shapes, and Nerd Font icons all draw one column or
  two, and Unicode does not say which. A theme using them could paint over
  the first entry of the left-placed list.
- The status bar measures the status-right it preserves as it renders, not as
  it is written. A theme's `%T` and `%F` are four characters that draw as
  eighteen, and counting the template left mode 1's layout short enough to
  clip its first entry.
- Saving a tmux setting no longer deletes other settings whose name extends
  it, from either the dashboard or the setup wizard. `@ccm-status-line` and
  `@ccm-status-line-hide-shell` are separate options, and writing the first
  removed the second from `~/.tmux.conf`.
- `ccm init` reads the current status-bar mode from `~/.tmux.conf` again when
  run outside tmux. It matched the option name as a substring, so a config
  that also set `@ccm-status-line-position` reported the two values joined
  together as the current mode.
- A permission dialog dismissed with Esc no longer returns to PERMIT, which
  also blocked `ccm send` for that window.
- Auto-exit no longer closes panes that run `claude` directly, with no shell
  underneath.

## [0.10.0] - 2026-08-08

### Added
- `ccm debug trace` accepts a tmux pane, window, or `session:index` as well as
  a project name, so a session ccm does not manage can be traced.

### Fixed
- A pane that runs `claude` directly, with no shell underneath, is detected
  instead of reading as SHELL.
- `ccm debug trace` with an empty argument is refused instead of tracing an
  arbitrary project.
- Documented the dashboard's `w` key in the README control table, and fixed a
  broken guide link in the Japanese README.

## [0.9.0] - 2026-08-05

### Added
- Sidekick attention. When a sidekick agent blocks on a permission dialog, its
  badge turns PERMIT-yellow in the dashboard, `ccm status` and the status bar,
  and a desktop notification names what it is asking about. The window's own
  state never changes — PERMIT still means Claude needs you. Toggle with `w` in
  the dashboard or `@ccm-sidekick-attention off`.
- A second Claude Code running as a sidekick (`CCM_IGNORE=1 claude`) needs no
  setup; its permission events reach the same channel. Support for non-Claude
  agents is exploratory and documented in the guide.

### Fixed
- A session interrupted with Esc is released at once instead of after a minute.
- The stale-BUSY guard still releases when the session transcript cannot be
  read, instead of holding BUSY with no way out.
- ccm's hooks ignore payloads from other agent harnesses that read Claude
  Code's settings file.
- Agent CLIs launched through a platform-suffixed binary get their presence
  badge again.

## [0.8.2] - 2026-07-30

### Fixed
- A turn running past the hour mark is recognised as active work again.
- The session-resume modal is recognised for sessions of any age.
- `ccm send --start` no longer refuses a long message that did arrive.
- `ccm doctor`'s multi-claude scan reports when it cannot run.

## [0.8.1] - 2026-07-30

### Added
- A diagram of the sidekick arrangement (`assets/sidekick-model.svg`), embedded
  in the guide.
- The external-agent allowlist covers Codex, Gemini and Grok alongside Kimi.
- The guide states that the sidekick may be another Claude Code, which is the
  arrangement `CCM_IGNORE` was written for.
- `ccm doctor` lists windows hosting more than one visible Claude session.
- Documented that a sidekick answers only to the Claude session sharing its
  window; to reach another project's sidekick, ask that project's Claude.

### Fixed
- The documented procedure for reaching a sidekick pane now includes the pause
  its delivery needs, and ships in the `ccm setup-claude-md` template. Existing
  `~/.claude/CLAUDE.md` sections are not rewritten — re-run
  `ccm remove-claude-md && ccm setup-claude-md` to pick it up.

### Changed
- `ccm send`'s ambiguity refusal points at `CCM_IGNORE` and the dashboard's `i`
  key, which resolve it for good.
- Product names are capitalised consistently across the diagrams, README and
  guide.

## [0.8.0] - 2026-07-27

### Added
- Guide section on relaying work with a second agent CLI, with the same
  conventions written into the `ccm setup-claude-md` template.

### Fixed
- The status bar no longer runs a full detection pass every second; the
  periodic pass is rate-limited to `CCM_RECONCILE_INTERVAL` (20 s).
- A session interrupted with Esc mid-tool no longer shows BUSY for up to 10
  minutes. It is released after `CCM_BUSY_STALE_RELEASE_SEC` (default 60 s).

## [0.7.1] - 2026-07-27

### Added
- README documents `ccm search`, the `⊘` hidden-pane and `⚙<name>`
  external-agent markers, and sidekick support.
- README describes what `ccm setup-claude-md` writes into your global Claude
  Code instructions, including the command that types prompts into other
  sessions.

### Fixed
- Uninstall instructions detach ccm from Claude Code first, so no hooks are
  left pointing at a removed plugin directory.
- `ccm send`'s help outputs document `--` and `--yes`.
- `ccm send` addressed at its own project is refused with an explanation.
- A resolved permission no longer holds `⚠ PERMIT` indefinitely. A permit whose
  modal is not on screen is released once the session log has been frozen past
  `CCM_PERMIT_MAX_TIMEOUT` (default 10 min).

## [0.7.0] - 2026-07-26

### Added
- Presence badge for external agent CLIs: a window with a pane running one
  shows a dim `⚙<name>`. Display-only — no detection, hooks or send
  integration for those panes.
- `CCM_IGNORE` — hide a Claude Code session from ccm. Launch with
  `CCM_IGNORE=1 claude`, or toggle with `ccm ignore` / `ccm unignore` (dashboard
  `i`). An ignored pane is dropped from window state, session tracking, `ccm
  send` delivery and idle auto-exit, and its hooks fire nothing. A dim `⊘` marks
  the row.

### Fixed
- `ccm capture` captures every pane of a split window instead of only the
  focused one, each under a header naming what runs in it.
- `inject-status --fast` no longer runs periodic maintenance.
- Hooks no longer use a bash-4-only expansion that aborted the notification
  path on stock macOS bash.
- `ccm send` re-checks the target pane's state immediately before typing.
- `ccm stop --all` works from the CLI again.
- The dashboard no longer crashes when `p`/`n`/`r`/`i` is pressed on a
  background-session row.
- Exit-all and the attach-time auto-start type into the Claude pane rather than
  whichever pane has focus.
- `ccm doctor` no longer crashes when a probed binary is missing.
- `ccm attach` detects Claude on any pane of the window.
- Windows sharing a project directory are no longer renamed to a wrong state or
  refused by `ccm send`.
- Digit-only project names are rejected, since they collide with window-index
  addressing in `ccm send` and `ccm attach`.
- `ccm status` columns no longer shift with the per-state colour codes, and
  long names are truncated rather than pushing the table out.
- `$HOME` → `~` shortening no longer corrupts paths containing the home string
  mid-path.
- The dashboard pidfile verifies the recorded process before signalling it.
- A delayed `idle_prompt` notification no longer clears a fresh BUSY signal
  (`CCM_IDLE_PROMPT_GUARD_SEC`, default 60 s).

### Changed
- The dashboard preview shows the tracked Claude pane, not whichever pane has
  focus, and never a hidden sidekick.
- The dashboard holds its row order stable while open; reopening re-sorts by
  current state.

## [0.6.0] - 2026-07-22

### Added
- Permission mode visibility: a `MODE` column in `ccm status` and a badge in
  the dashboard. Display-only.
- The hook-silence canary records each firing to
  `~/.local/share/ccm/state/hook-silence.log`; `ccm doctor` reports the count.

### Fixed
- The `⊘` sidekick marker is cleared when a hidden sidekick exits.
- The hook-silence canary no longer misfires when a hidden sidekick shares the
  window's directory.
- Active-work spinner detection matches token counts below 1000.
- Idle auto-exit no longer treats a parked editor or pager in a sibling pane as
  background work.

### Changed
- Sub-second state updates for the dashboard and status bar: a lightweight tick
  overlays hook-driven transitions between full detection passes.

## [0.5.3] - 2026-07-17

### Fixed
- `ccm send` delivers keystrokes to the pane hosting Claude rather than the
  window's active pane, which in a split window could be a bare shell.
- `ccm send` no longer drops lines that start with a dash.
- Status-bar layout measures CJK names in terminal columns, not characters.
- Projects whose directory path contains non-ASCII characters no longer hold an
  indefinite false BUSY.

### Changed
- Auto-exit skips windows with live background work, and sends a desktop
  notification when it does fire.

## [0.5.2] - 2026-07-11

### Added
- Hook-silence canary (opt-in). Warns when a session's hook log stops updating
  while the conversation is active. Off by default; enable with
  `tmux set -g @ccm-hook-silence on`.

### Fixed
- Reloading `.tmux.conf` no longer stacks a duplicate focus-refresh hook per
  reload.
- The mode-2 status bar no longer freezes solid when projects outgrow tmux's
  five-line status limit. Entry lines are clamped and overflow is packed.
- A phantom `SubagentStart` no longer holds a false BUSY while the pane sits
  idle, in either of the two shapes it arrives in.
- `ccm debug trace` runs the real two-path detection instead of the legacy rule
  table alone, and prints the derived state in a new `ev=` column.

## [0.5.1] - 2026-07-04

### Fixed
- The dashboard repaints itself when tmux draws a background pane's output over
  the popup.
- Running a local slash command at an idle prompt no longer flips the window to
  BUSY for up to ten minutes.
- The dashboard no longer sticks at PERMIT while a background subagent's tool
  runs.
- Permission dialogs that carry no separate footer are detected as PERMIT.

### Changed
- The status bar reflects the focused project immediately on a window switch
  instead of waiting for the next tick. New `ccm inject-status --fast` renders
  from cached state.

## [0.5.0] - 2026-06-25

### Added
- Trace-replay regression corpus: recorded event sequences are replayed through
  detection with probe points asserting the expected state.
- `CCM_SEND_TRACE=1` opt-in trace for `ccm send`, logging every keystroke call
  it makes.

### Fixed
- `ccm send --start` verifies the body reached the composer before submitting,
  since a freshly launched Claude shows its prompt before it accepts input.
- The dashboard no longer sticks at PERMIT while an approved tool runs.
- Auto-exit no longer types into a user's shell pane, no longer leaks `clear`
  into a still-running session, declares SHELL only once the exit has landed,
  and bails when the focused window cannot be resolved.
- Snapshot load fails with a readable message on malformed JSON instead of a
  traceback.
- The `/model` picker is detected as PERMIT again after its footer was reworded
  upstream.
- Assorted robustness fixes across the detection, runtime, UI and hook layers.

## [0.4.0] - 2026-05-24

### Added
- `ccm add` offers to create the target directory when the path does not exist
  but its parent does.
- Read-only view of Claude Code's background sessions in the dashboard (`b`, or
  `@ccm-bg-section always`), plus a `ccm bg list` subcommand. Lifecycle stays
  with `claude` itself; ccm only observes.
- Dashboard `Enter` on a background-session row opens a fresh tmux window
  running `claude attach`.

### Fixed
- The dashboard path column no longer drifts as the `* elapsed` marker ticks.
- `ccm send` refuses targets showing the `claude agents` TUI, where keystrokes
  would dispatch a new session rather than reach a conversation.
- COMPLETED notifications no longer fire while Claude reports outstanding
  background work.
- The permit-footer pattern tolerates extra action keys, so the reworked
  `/model` footer is detected again.
- The `* elapsed` marker no longer flickers during multi-turn auto-loops.
- `ccm send --start` waits for the target to be ready instead of a fixed two
  seconds.
- Interactive choice menus show PERMIT instead of a false BUSY for the whole
  reading time.

## [0.3.0] - 2026-05-05

Initial public release. ccm manages Claude Code sessions as tmux windows, with
live state detection, an interactive dashboard, status-bar integration, and
snapshot save/restore.

### Project management
- Window-based project model: `ccm add` / `open` / `register` / `unregister` /
  `remove` / `attach` / `list` / `rename`. Each project is a tagged tmux window.
- `ccm send <project> <message>` for cross-project prompts, gated on state — a
  window showing a permission modal never receives keystrokes, including with
  `--force`.
- Snapshot save / load / list / delete, with an `_autosave` snapshot written
  every two minutes and on `ccm stop --all`.
- Claude Code auto-starts on attach to a shell window. Idle sessions auto-exit
  after `CCM_IDLE_EXIT_TIMEOUT` (10 min); the next attach resumes them.

### State detection
- Four states — PERMIT / BUSY / IDLE / SHELL — plus DOWN when tmux is not
  running.
- Detection reads Claude Code's hook events first, falls back to the session
  transcript when hooks fall silent, and reads the pane directly for modals, so
  a permission prompt is seen even when hooks stop firing.
- Window state aggregates its panes by priority, so a teammate or sidekick
  needing attention surfaces regardless of which pane has focus.
- `ccm debug trace <target>` prints one line per scan with every detection
  input and the resolved state.

### Dashboard
- Toggleable popup (`prefix + Tab`) showing each project's state, git branch,
  listening ports and pane count.
- A `* elapsed` marker after a session finishes, a `(bg)` note for leftover
  background activity, and a stale-signal age suffix when auto-release windows
  have lapsed.
- Live filter search (`/`), interactive tree view (`prefix + T`) and menu
  (`prefix + C`).
- Auto-focus to a pane waiting on a permission modal when attaching.

### Status bar
- Three modes via `@ccm-status-line`: an icon appended to your existing
  `status-right`, a replacement window list, or a dedicated row with branch and
  port details (default).
- Themable colours; polling cadence tunable via `CCM_STATUS_INTERVAL`.

### Notifications
- Desktop notifications via `@ccm-notify` (`permit` / `completed` / `all`), with
  per-project grouping on macOS and `ccm clear-notifications` to clear them.
- A grace window absorbs the Stop events Claude Code fires at tool boundaries,
  so a completion alert only arrives on a genuine completion.

### Robustness
- Canaries warn when `~/.claude/hooks.log` grows past 100 MB, when settings
  disable every hook, and when sessions exit repeatedly in a short window —
  each a documented upstream failure that would otherwise look like a ccm bug.
- `ccm doctor` aggregates dependency versions, hook installation, every canary,
  per-project state and the error log into one self-check.
- `ccm errors` prints the silent-exception log, so a swallowed failure in the
  refresh path stays debuggable.
- Multi-byte text is measured in terminal columns throughout, so CJK and emoji
  project names align.
- A per-project exception barrier keeps one project's detection bug from
  affecting the others.

### Setup / integration
- `ccm init` setup wizard; `ccm setup-hooks` / `remove-hooks` and
  `ccm setup-claude-md` / `remove-claude-md` to attach and detach ccm from
  Claude Code.
- Zsh completion. English and Japanese documentation kept in sync.

### Requirements
- tmux 3.2+, Claude Code v2.1.107+, jq, fzf, Python 3.
