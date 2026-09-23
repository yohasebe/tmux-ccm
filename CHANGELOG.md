# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- An unread mark on the pane border, for windows running several
  agents in split panes (`set -g @ccm-pane-labels on`, off by default).
  A pane where a reply completed while you were looking elsewhere
  shows `◆ new` until you have looked at it for a few seconds
  (`@ccm-unread-linger`, default 5) or send it the next prompt. The
  mark outlives the focus event on purpose: tmux delivers that event
  when you switch to the window or bring the terminal to the front,
  which is the act of going to look. It
  follows the completion notice's rules (grace period, nothing while
  background tasks remain) and needs the hooks. Turning it on enables
  tmux's pane border line; if your own configuration sets
  `pane-border-format` after ccm loads, add the mark to it yourself
  (see the guide).
- Auto-exit looks at the target pane before typing and types nothing
  when it shows the agent view (where a session lands after
  `/background`, `←` twice, or `/exit` inside an attached background
  session) or cannot be read. When the pane shows the agent view
  after `/exit` — an attached background session detaches instead of
  ending — that is recorded as such, with no SHELL state, autosave,
  completion notice, or further keys. Both outcomes go to a separate, rate-limited
  `auto-exit-declined.log` (`CCM_AUTO_EXIT_DECLINED_LOG`,
  `CCM_AUTO_EXIT_DECLINED_LOG_INTERVAL`), shown by `ccm doctor`, so
  `auto-exit.log` keeps counting only real exits.
- A hand-off notice for projects whose `claude --continue` will not
  resume their conversation. When a session is sent to the background
  with `/bg`, its transcript ends with a hand-off to the background
  session, and `claude --continue` starts a fresh session for as long
  as the CLI still counts that background session as live — including
  after its task is done. ccm reads the CLI's own session registry
  for that, on the CLI's own terms (process alive, start time
  matching the record), using the daemon's roster only to decide when
  to look, since the roster outlives the daemon; where the registry
  cannot be read as a whole, or `CLAUDE_CONFIG_DIR` moves the CLI's
  home, nothing is reported. ccm names the background session, its state,
  and the `claude attach` / `claude stop` exits: as a warning line on
  the dashboard and `ccm status`, a `bg hand-off` row in `ccm doctor`,
  and on the tmux message line (plus `ccm attach` / `ccm send --start`
  output) right after the launch command is typed. Shown for SHELL
  projects only — a window already running Claude has no launch
  coming. The notice itself never stops or attaches a background
  session; the check is read-only.

### Changed
- Pressing `Enter` on a dashboard background-session row whose attach
  window is already open switches to that window instead of opening
  another. The window is recognised by a tag (`@ccm_bg_short`) set
  when it is opened, and only while it still hosts claude. When two
  are live, or whether one is open cannot be told, none is opened and
  the message says so.
- A launch into an existing shell (attach, the dashboard,
  `ccm send --start`, `ccm open`) always types `claude --continue`.
  Only `ccm add` types plain `claude`, and only when ccm has looked
  where the CLI keeps the new window's transcripts and found none: default config
  home, no custom transcript-directory name in ccm's or tmux's
  environment, no transcript under the directory's slug. Whatever ccm
  cannot check resolves to `--continue`. The former
  `claude --continue 2>/dev/null || claude` chain is gone: `--continue` exits with status 1 not only when there is
  nothing to resume but also when the newest conversation was handed
  to a live background session, and the chain turned every such case
  into a silently new conversation with the CLI's explanation hidden.
  Now the message stays on screen and the pane returns to the shell.
- The hook-driven `inject-status --fast` no longer sweeps the temp
  directory's caches on every state transition; the sweep runs with
  the periodic poll only. Directory creation is unchanged.
- The hand-off check reads the daemon's roster only when a project is
  in SHELL state, and the dashboard reuses the roster it already read
  for its background-sessions section instead of reading it again.
- Every path that starts Claude in a project window — `ccm attach`, the
  dashboard, `ccm send --start` — now goes through one launch step that
  re-reads the window at that moment: it types nothing when any pane
  already hosts Claude (a second `claude --continue` would open the
  same conversation twice, as happens when Claude relaunches in place
  after an update), and nothing when no pane can be verified as a
  shell prompt, including when the panes or the process list cannot
  be read at all. `ccm send --start` never launches into the sender's
  own pane and re-applies its self-delivery guard to the pane the
  message will actually go to. The outcome is reported: `ccm attach`
  and the dashboard say when nothing was started, and `ccm send
  --start` refuses with the reason instead of typing the message into
  an unverified pane.
- The cluster-SHELL warning now says what it observed — Claude left the
  pane repeatedly — and lists the causes that look identical from ccm's
  side (an update relaunching in place, manual exits, unexpected
  exits), naming anthropics/claude-code#48069 as one known cause of
  the last rather than as the likely diagnosis. It no longer promises
  that `claude --continue` restores the conversation. Detection and
  thresholds are unchanged.
- The external-agent presence badge is now `▸<name>` instead of
  `⚙<name>` on the dashboard, `ccm status`, and the status bar. The
  gear read as "settings"; the triangle reads as "someone is next to
  this session". `▸` (U+25B8) is Neutral-width, so column budgets are
  unchanged. Colours are unchanged: dim for presence only, PERMIT
  yellow while the sidekick waits on a decision.

### Fixed
- Claude Code 2.1.280 adjustment dialogs (`/autocompact`, `/effort`,
  and the enabled `/fast` picker) now read PERMIT when a key hint
  precedes Enter in their footer. They previously fell through to
  IDLE or BUSY, allowing sends into a dialog. Existing Enter-first
  footers and free-navigation menus retain their classification.
- `ccm doctor` no longer ticks a settings flag it could not read. A
  `settings.json` that is present but does not parse — overlapping
  writers have left it cut off mid-object — reported
  `disableAllHooks ✓ not set` and "Hooks not installed"; it now
  reports the flag as unknown, naming the file, and says the settings
  cannot be read as JSON.
- `ccm setup-hooks` and `ccm remove-hooks` no longer overwrite the
  backup with a settings file they then refuse to touch. The copy to
  `settings.json.bak` was made before working out the new content, so
  a broken file replaced the last good backup. It is now made only
  when there is something to write. The write itself no longer
  depends on the shell's `pipefail`: a failed step ends the command
  there instead of going on to write an empty file. JSON that parses
  but is not an object (`[]`, `null`) and a settings path that is a
  broken symlink are refused the same way, where `null` used to get
  hooks written over it and a broken symlink was replaced by a file.
- `ccm send` no longer reports a message as sent when the session says
  it did not take it. Claude Code 2.1.277 removes characters it strips
  from a prompt and holds the cleaned text for the sender to confirm
  with another Enter, saying so in a line above the input box — so
  pressing Enter proved nothing. Both `ccm send` and the spool now
  read that line after submitting: `ccm send` fails, quoting what the
  session said and adding that the body is still in its input box,
  and the spool records the message as held instead of delivered,
  where `ccm spool list` and `ccm doctor` name it and
  `ccm spool clear-held` acknowledges it once you have dealt with it
  in that session. It is not queued for a second try: after its user
  confirms or clears the copy in the input box, the box looks the same
  either way, so retrying would type the message again. ccm neither
  presses Enter again nor rewrites what you sent. When the session
  does not say it is holding the prompt — including when the line has
  already gone, the wording changes, the pane is too narrow to show
  the line whole (2.1.278 cuts it at 60 columns), or the pane cannot
  be read — the send reports as before. The reading can also err the
  other way: a reply that prints a line shaped like that notice,
  while the session's user is typing a new draft, reads as a hold.
  Treat the failure as a prompt to look at the target window, not as
  grounds to resend automatically.
- Documented that under Claude Code's reduced-motion setting the
  spinner footer's elapsed time can stop updating during a running
  turn, so reading the pane — the fallback used when hooks are not
  firing — goes idle once `CCM_SPINNER_STALE_RELEASE_SEC` passes. The
  guides and the pattern's own note said the time was unaffected by
  reduced motion.
- `ccm remove-hooks`, and `ccm setup-hooks` when it rebuilds an install,
  no longer remove another tool's hooks. Both dropped any matcher entry
  holding a hook whose command merely contained a ccm script's name, so
  another tool's hook in the same entry, or a same-named script in
  another directory, went with it. ccm now edits only its own hooks, one
  hook at a time: a bare path to a ccm script in this ccm's hooks
  directory, however the directory is spelled (through a symlink, or in
  another case on a filesystem that ignores case). Every other hook
  named like a ccm script is left in place and named in the command's
  output and by `ccm doctor`, with the number of entries in each
  directory. This includes the hooks of a ccm at a previous path: after
  moving ccm, `ccm setup-hooks` installs hooks from the new path and
  leaves the old ones for you to delete by hand, since from the settings
  alone they cannot be told apart from another tool's. When `ccm doctor`
  can read the settings and finds none of the hooks in this ccm's hooks
  directory, it says so instead of reporting them as installed.
- `ccm setup-hooks` now writes a hook timeout of 5 seconds instead of
  5000. The field is in seconds, not milliseconds, so the old value
  gave a hook that hangs 83 minutes to do it in. At session end Claude
  Code waits for the largest SessionEnd hook timeout, capped at 60
  seconds, so the old value also held that wait at the cap. On an
  existing install, `ccm setup-hooks` now sets the timeout of ccm's own
  hook commands and changes nothing else, other tools' hooks included;
  `ccm doctor` names an install still carrying the old value. Change any
  `CCM_HOOK_CMD_TIMEOUT` you set in milliseconds before running it.
- A pane whose spinner footer shows only the elapsed time — the pane
  too narrow for the thinking hint beside it, or a tool phase that has
  produced no tokens yet — no longer reads as idle. That form is read
  on the spinner's own line only, since a bare `(2s)` is ordinary in
  prose and has nothing else to tell it apart.
- A spinner verb ending in an ASCII `...` rather than `…` is read the
  same way. Claude Code 2.1.273 leaves such a verb alone instead of
  appending an ellipsis of its own, and a configured `spinnerVerbs`
  entry or a task's own wording can end that way.
- A narrow pane in a long thinking phase no longer reads as idle: when
  the spinner footer drops its elapsed time to make room for the
  thinking hint (`(deep in thought)`), ccm captures the pane again
  half a second later (twice at most) and takes the spinner glyph
  moving, or an elapsed time that has come back, as the sign of a
  running turn. A frozen frame does not move; nor does the fixed
  glyph of Claude Code's reduced-motion setting, whose panes are read
  through their elapsed time instead (see the guide's known
  limitations).
- Background sessions the CLI marks `blocked` — waiting for a reply,
  an approval, or a condition to act on or wait out — now read
  `✻ NEEDS` in the dashboard and `ccm bg list` instead of `? UNKNOWN`,
  with what they wait for shown in place of the directory; a session
  ended with `claude stop` reads `■ STOPPED`.
- Status-line mode 1 no longer fails to write the bar when there is
  no project window to list; the idle marker is written and the
  `status-right-length` floor is applied as on every other write.
- The dashboard's background-session block now appears when `b` is
  pressed even with more projects than the popup has rows: the block's
  rows are reserved out of what is left below the header and any
  warning banners, a few project rows are always kept, and the
  project list scrolls within the rest. It used to be skipped for
  want of space, so the toggle showed nothing; on a popup too short
  for both, the project rows win.
- Detection reuses the session record it validated against the live
  process when resolving the transcript, instead of reading the file a
  second time without the check; a record rejected as belonging to a
  recycled pid can no longer come back through the transcript path,
  and a cached transcript path resolved from such a record is not
  reused either.
- The probe that reads a transcript's recorded directory is bounded in
  bytes: a single multi-megabyte record at the head was read in full
  past the 512 KiB limit.
- The hourly cleanup of the temp directory is limited to the disposable
  caches (git, ports, notification markers); it no longer deletes the
  dashboard's pid marker, whose absence let periodic polls run full
  detection alongside an open dashboard.
- The instant PERMIT flag names the full window (`session:index`), so a
  permission prompt in one tmux session no longer paints a window with
  the same index in another session.
- The dashboard initially selects the project in the opening window instead of
  the first state-sorted row. Unlisted windows retain the first-row fallback.
- Send and spool derive composer text and dim attributes from one capture,
  preventing screen changes between reads from hiding a real draft. Alternate
  screen reads also preserve attributes.
- Send, spool readiness, and auto-focus now share the periodic detector's saved
  work-clock history, so a frozen footer does not remain BUSY in those checks.
- Coloured composer drafts are no longer mistaken for dim prompt suggestions.
  Extended foreground/background colour arguments preserve the dim attribute.
- `ccm send` checks for the agents TUI immediately before delivery, including
  `--force`, `--start`, and transitions during confirmation, preventing messages
  from dispatching unintended sessions.
- `ccm send` is refused again when the `claude agents` view prefixes its
  footer with a mode chip. Backgrounding a session with `←` draws
  `⏵⏵ auto mode · enter to return · … · ? for shortcuts`, and the
  detector required `enter to` at line start — so it read the pane as
  not the TUI and the send flowed into the dispatch box, spawning a
  brand-new session. The pattern no longer anchors at line start; the
  invariants are `enter to <word>` and a closing `? for shortcuts` on
  the same line.
- The `claude agents` TUI is refused again on every row kind. The TUI
  draws a different footer per row kind (session rows and collapsed
  rows differ in the verb and middle segments), and the detector fixed
  the leading verb — so on a collapsed row it read the pane as not the
  TUI, lifting the refusal that keeps `ccm send` keystrokes from
  spawning an unintended agent-view session. The pattern now matches
  the stable skeleton: an `enter to <verb>` and a closing
  `? for shortcuts`.
- `ccm send` no longer refuses to deliver because of Claude Code's own
  next-prompt suggestion. Claude Code draws that suggestion into the composer
  when a turn ends — the moment a send is meant to land — with the same `❯`
  line shape as a half-typed draft but entirely dim, and the composer guard
  captured without the terminal's attributes, so it read as a draft and the
  message was refused (or queued until it expired). The guard now reads the
  attributes too: an entirely dim line is the suggestion, any non-dim
  character keeps the draft verdict, and an unreadable attribute capture
  falls back to refusing as before.
- `ccm doctor` no longer reports the managed settings tier when it has read
  one file from it. An organization can deliver `disableAllHooks` or
  `allowManagedHooksOnly` through MDM / OS policy or from the claude.ai
  console, and neither leaves a file to read — so a green "not set (managed
  settings)" claimed a scope the check does not have, in the one direction a
  canary must never fail in. Both rows now name `managed-settings.json` and
  point at `/status`, which is what knows the managed source in force.
- A window whose only Claude lives in an ignored pane no longer claims
  SHELL or DOWN. ccm deliberately does not see that Claude, so it has no
  basis for either claim — and SHELL made `ccm send` queue messages
  against a presumed-absent Claude, or offer `--start` to launch Claude
  into a visible pane, which can be a sidekick's. The window now reports
  a new IGNORED state (`⊘`, dim): not a rung of the
  PERMIT > BUSY > IDLE > SHELL ladder but a verdict that ccm cannot
  answer. Any Claude in a visible pane still answers normally, `ccm send`
  to an IGNORED project is refused (never queued, never auto-started)
  with a refusal that points at `ccm unignore <project>`, and auto-exit
  does not act on it.
- The ignore marker on a pane running a known external-agent CLI (the main
  use of `ccm ignore`) is no longer stripped as stale — it survived only
  while claude ran there, so a hidden sidekick agent reappeared after one
  detection pass. Liveness now asks the same question the attention reader
  asks: still hosting claude or a known agent.
- A turn cancelled with Esc before the answer began no longer holds the
  project busy for ten minutes. Nothing writes a terminal record in that
  case, so both detection paths had to fall back on the pane — and only one
  of them gave up when it stopped seeing work. They now use the same window.
- A connection or rate-limit retry reads as busy instead of idle. The retry
  line (`Retrying in Ns · attempt M/10`) replaces the spinner footer for the
  whole backoff, and the session spawns no child and writes no log while it
  waits — so a long backoff had no signal marking it as working, and idle is
  the reading auto-exit acts on. The countdown is matched without the tail,
  which a narrow pane clips, and in minutes as well as seconds — the backoff
  grows with every attempt, so the long ones are the ones that matter.
- A leftover child process (a dev server the session no longer owns) plus a
  static spinner-shaped string on screen no longer holds the window busy
  without expiry. The accept-edits disambiguation now asks the same question
  the childless path asks — is the pane's clock ticking? — instead of taking
  any footer-shaped string as yes.
- A pane is read as busy while Claude is thinking or generating with no
  child process spawned — the spinner's elapsed-time footer is on screen the
  whole while, and reading only the process table there called a working
  session idle, which is the reading auto-exit acts on. The claim holds only
  while that clock ticks: a frozen frame (a session hung after rendering the
  footer) or a transcript quoting a footer stops counting after
  `CCM_SPINNER_STALE_RELEASE_SEC` (default 30 s), because raw BUSY has no
  other way out. The comparison history lives on the window itself, so the
  release works from the periodic status pass — a fresh process every time —
  not only while the dashboard is open.
- The active-work footer is still recognised when a narrow pane clips it
  before the closing parenthesis.

## [0.11.0] - 2026-08-17

### Added
- `ccm doctor` reports tmux's `focus-events`, which Claude Code asks for in
  a startup banner that scrolls away.
- `ccm spool clear-expired [project]` acknowledges messages that expired
  without being delivered, clearing the warning they raise. A warning that
  cannot be answered is one the reader learns to scroll past.
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
- `ccm sidekick-send` does not read the peer's composer, so a message typed
  into a half-written draft still merges. Locating another vendor's input box
  means matching that vendor's screen, which is the coupling this lane exists
  without; capture the pane first, as the manual procedure always asked.
- `ccm send` no longer refuses when the target cannot take the message
  right now — it queues the message to the spool and reports
  `Queued for <project>` (queue length, TTL, message id). This flips the
  default for BUSY / PERMIT / SHELL targets and mid-send transitions:
  scripts that relied on a non-zero exit to detect a non-delivery should
  use `--now`, which keeps the old fail-fast behaviour (PERMIT with
  classification and guidance included).

### Fixed
- The spinner is recognised through the shapes its footer takes. Within one
  turn it carries an elapsed time with no token count, then one with a
  trailing segment, then the familiar form — and only the last was matched, so
  a pane with a tool running read as idle for the rest.
- The guide's example search binding says that `prefix + /` is tmux-copycat's
  too, and that a second binding replaces the first.
- A project whose transcript directory is shared with another finds its own
  session. The directory name turns every non-alphanumeric character into a
  dash, so `a/b`, `a-b` and `a_b` name one directory — and the newest file in
  it could belong to the other project, holding this one busy or releasing it
  early. A transcript records the directory it belongs to; that now decides.
- Background sessions list only workers a task has claimed. The daemon also
  keeps a worker warm for the next dispatch, and that appeared as a nameless
  row with nothing to attach to. A job whose state file is missing or corrupt
  still counts — existence, not readability.
- `ccm capture` says when it read nothing instead of printing an empty block.
  A pane hosting a session is never blank, so silence there means the read
  failed — and this output is what an agent reads to decide whether a message
  landed or whether a peer is still working.
- The folder-trust prompt is treated as a permission request. It carries the
  same footer a model picker does, so it classified as a harmless
  confirmation — and answering it grants read, edit and execute in that
  directory.
- The permission-dialog question matches whichever space Claude Code draws
  after it. The composer renders a no-break space, so requiring an ordinary
  one anywhere is an assumption with nothing behind it. The prefix now
  matches horizontal whitespace only, so a wrapped transcript quotation of
  the question does not read as a live dialog.
- A session's transcript is found by its session id when the directory it
  sits in is not the sanitised working directory. Losing the transcript loses
  the terminal stop reason that releases a held BUSY, and that failure has no
  timeout of its own.
- A delivered message is no longer reported as unconfirmed because the
  composer wrapped it. The check looked for a fragment as a contiguous
  substring, and the wrap lands wherever the width falls — mid-word, or
  mid-sentence in a language written without spaces.
- A prompt already sent no longer reads as a half-typed draft. Claude Code
  draws submitted prompts into the transcript with the same `❯` glyph the
  composer uses, and the guard scanned the pane from the top and took the
  first one — so a send was refused, and under the spool queued and then
  expired undelivered, for as long as any prompt was on screen. The composer
  is now located by the rules the TUI draws around it.
- A queued message that expired without arriving is reported wherever the
  queue is: a desktop notice when it happens, a named warning in `ccm
  status`, the dashboard and `ccm doctor`, and its own row in `ccm spool
  list`. `ccm spool clear-expired` acknowledges it. The sender had been
  told "queued", and queued is not delivered.
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
- The settings canaries read the administrator's settings and each
  project's as well as the user's own file, resolving them in the order
  Claude Code does, and name which one carries the flag.
  `allowManagedHooksOnly` is reported only from the administrator's
  settings, where it has effect.
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
