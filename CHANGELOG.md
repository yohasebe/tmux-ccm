# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **`ccm status --json`.** Agents and scripts can read each window's state, its sidekicks' states and the queued messages as one JSON object, for all windows, one by name, or `self`.
- **`@ccm-notify-transport osascript`** sends macOS notifications through `osascript` even when `terminal-notifier` is installed, for a Mac where `terminal-notifier` cannot post.
- **`ccm sidekick-send --json`.** The result of a send comes back as one JSON object: refused before any key, or attempted, with whether a fragment of the message appeared and whether Enter was sent.

### Changed

- **`ccm sidekick-send` stops for more waits.** It refuses a sidekick whose wait it learns from Codex's hooks or a Kimi or Grok marker, not only from the screen, and one whose process it cannot identify or whose observation fails; it looks again before Enter and leaves Enter unsent if a wait appeared or the sidekick was restarted.

### Fixed

- **Auto-restore at tmux start skips when managed windows already exist.**
- **A Claude whose process is named by its path is found on macOS**, so its window no longer reads as SHELL.
- **A Claude session outside tmux on a project's directory no longer changes that window's state** or sends its completion notification.

### Security

- **Text that holds `#` reaches tmux literally:** a project directory opened by `ccm add`, v1 snapshot loading or the dashboard's attach window, a project name or branch in the status bar, and the plugin's install path.
- **A message holding a control character is refused before any key** by `ccm send`, the message queue and `ccm sidekick-send`; one already queued moves to `expired/` unsent. Windows line endings are folded and tabs become spaces.

## [0.13.0] - 2026-11-05

### Added

- **Docked dashboard.** The dashboard can sit as a full-width pane at the top or bottom of the window (`@ccm-dashboard-dock`, `@ccm-dashboard-dock-size`), follows you between windows, can close when you open a project (`@ccm-dashboard-dock-close-on-open`), draws a coloured edge toward your panes (`@ccm-dashboard-dock-edge`), and gives each window its layout back when it leaves.
- **Sidekicks working and waiting.** The `▸` badge shows a Codex or Antigravity CLI (`agy`) sidekick working (`◉`) or waiting for a key (`⚠`), and attaching through ccm focuses a waiting sidekick when no Claude pane waits. Hide these states with dashboard key `w` or `@ccm-sidekick-attention off`.
- The dashboard shows what keeps a project `(bg)` and for how long (`(bg 9h)`), with a line per task in the preview. Stop unwanted background tasks from that session's `/tasks`.
- `ccm finish-restore` (Menu → Finish restore) ends an incomplete restore with the projects restored so far and resumes autosave; the active projects replace `_autosave` (or it is removed if none remain), and the full checkpoint is kept as `_autosave-held-back-<id>`. Run the command again if finishing stops.
- The guide's Tips explain how to keep `←` on an empty prompt from opening the agent list, in Claude Code and in Codex.

### Changed

- **Faster and lighter.** Hooks, status updates, the dashboard (including with Python version managers) and restores of many windows use fewer resources and respond sooner.
- **Restores no longer stop at one bad window.** Ready windows become usable at once and others are held back with their reasons; autosave stays paused until the restore completes or you finish it. Use Menu → Continue restore to retry them, or `ccm finish-restore` to keep what has been restored.
- With several sidekicks in one window, each name on the `▸` badge carries its own state (`▸ codex ◉,agy ⚠`).
- Queued messages are delivered oldest first in batches limited by 5 messages, 16 KB and the input box, with an oversized oldest message sent alone; each message has its own envelope header.
- Time spent on a permission prompt or while the Mac sleeps no longer counts toward a queued message's expiry (`CCM_SPOOL_TTL_SEC`, default 60 minutes); `CCM_SPOOL_MAX_AGE_SEC` (default 24 hours) limits total age, and the sender of an expired message receives instructions for resending it.
- `(bg)` appears in BUSY's colour beside Claude's state, ahead of any sidekick badge, and no longer counts Claude Code's own upkeep.
- Status modes 1 and 2 highlight the current project (`@ccm-status-current-bg`).
- After a restore, the hint for a Codex sidekick suggests `codex resume --last`.
- A long error message ends with its first line again.
- `ccm doctor` reports when no Codex hook has arrived in 7 days. Complete a turn in Codex before checking the hook installation.

### Fixed

- A subagent's permission request stays PERMIT while you carry on in the main conversation, and `ccm send` and queued messages judge from the main conversation whether it can receive a message.
- A window no longer switches between IDLE and BUSY while background agents work after Claude's turn has ended.
- `ccm sidekick-send` refuses a Codex or Antigravity sidekick showing a dialog that waits for a key, or a Codex sidekick with text selected. Answer the dialog or clear the selection before retrying.
- Running `ccm setup-sidekick-hooks codex` again leaves other tools' hooks in Codex's `hooks.json` in place, so Codex does not ask to review them again.
- Checkpoints, auto-restore and the docked dashboard support tmux 3.8's JSON window layouts, and ccm's popups use the terminal's background again under tmux 3.8. Close floating panes before saving a checkpoint.
- Publishing an Artifact alone no longer leaves a session marked `IDLE (bg)` or prevents auto-exit.
- Auto-exit no longer exits a session on macOS while Claude runs a Bash command, and `IDLE (bg)` survives interrupting a turn with Esc.
- Named Claude Code sessions no longer cause false draft warnings or input-box timeouts when sending messages.
- A restore no longer holds a window back when the terminal is resized during it, and stops with the reason when it cannot record its progress.
- A checkpoint is no longer refused while a Kimi Code sidekick runs as `kimi-code`.
- A sidekick's approval wait survives a brief failure to list panes, and a Codex approval wait clears when its tool resolves.
- ccm's hooks no longer fail while no terminal is attached to tmux.
- The dashboard's key help stays on screen when the project list is longer than the popup.
- `ccm debug trace` stops after 12 hours by default, when its window is gone, or when the tmux server is replaced; `--max-time SECONDS` changes the limit and `--max-time 0` removes it.
- A command run in a pane uses its own pane's tmux session, not the one a popup was opened from.
- `ccm send --start` no longer types a message too tall for the started session's input box. Resend without `--start`, or send a short message pointing to a file.

### Security

- Fixed unsafe handling of Claude session ids.
- Secured ccm's temporary directory handling. ccm refuses unsafe runtime directories and may move an existing one to `ccm-<uid>.untrusted-<pid>`.

## [0.12.0] - 2026-10-03

### Added

- **Restore across logout.** `ccm prepare-logout` saves and protects the `_autosave` checkpoint, which records each window's pane splits, working directories, pane roles and zoom. Restoring it (`ccm start _autosave`, or at tmux start with `@ccm-auto-restore on`) rebuilds the panes and starts Claude only in its own pane; resume sidekicks by hand. Progress shows in the status area and the dashboard, and auto-restore output is kept in `state/auto-restore.log`.
- `ccm exit <name>` ends Claude in a project and keeps its window and sidekicks; `ccm roles` shows or clears pane role reservations.
- The dashboard menu adds logout preparation, continue restore, saved checkpoints, reset and exit, a help preview for the selected item (Japanese with `@ccm-lang ja`), a `/` live filter, grouped items and coloured setting values.
- Dashboard `u` and `ccm spool show / discard / resend` read, resend or delete expired and held messages.
- Opt-in Codex sidekick completion notices to the Claude in the same window (`@ccm-sidekick-notify on`), and Codex approval waits on the attention marker.
- Opt-in unread mark on pane borders for split-pane agents (`@ccm-pane-labels on`).
- A notice when `claude --continue` would not resume a project's conversation because it was handed to a live background session.

### Changed

- `ccm setup-hooks` writes a hook timeout of 5 seconds, and `ccm setup-hooks` and `ccm remove-hooks` no longer touch other tools' hooks; hooks left at a previous ccm path are named for you to remove. Run `ccm setup-hooks` once after upgrading.
- Starting Claude in an existing shell types `claude --continue` (only `ccm add` types a plain `claude`, when no transcript exists), a failed resume leaves its message on screen, and nothing starts where Claude already runs.
- `ccm doctor` shows actionable items by default; `--verbose` shows the details.
- The sidekick badge is `▸<name>` (was `⚙<name>`).

### Fixed

- `ccm send` and queued messages report a message as unsent when Claude Code holds it for confirmation, no longer mistake Claude's prompt suggestion or an earlier message for a draft, refuse the `claude agents` view, and with `--start` wait for an empty input box and never type into a shell.
- Thinking, retries and narrow or reduced-motion spinners read as BUSY, frozen screens are released, Esc before an answer no longer holds a project BUSY, parked background conversations stay tracked, and Claude Code 2.1.280 adjustment dialogs read as PERMIT.
- A window whose only Claude is in an ignored pane reads as IGNORED (`⊘`) instead of SHELL; ignored sidekick panes stay ignored.
- `ccm doctor` no longer reports settings it could not read as fine, and names managed-settings limits correctly.
- Status bar, dashboard and cleanup fixes: mode 1 with no projects, the background-session block on tall lists, per-session PERMIT flags, and the dashboard's marker no longer removed by temp-directory cleanup.

## [0.11.0] - 2026-08-17

### Added
- `ccm doctor` reports tmux's `focus-events` setting.
- `ccm spool clear-expired [project]` acknowledges messages that expired undelivered and clears their warning.
- Message queue for `ccm send`: a message the target cannot take (BUSY, PERMIT, SHELL, the agents view, or a draft in the input box) is queued under `$CCM_DATA_DIR/spool/<project>/` and delivered with an envelope header once the project is IDLE. Messages older than `CCM_SPOOL_TTL_SEC` (default 60 min) move to `expired/`. A crash during delivery can cause the same message to arrive again. `ccm spool list` and `ccm spool cancel <id|--all> [project]` inspect and withdraw queued messages, and counts show in `ccm status`, `ccm doctor` and the dashboard (`✉N`).
- `ccm sidekick-send "<message>"` sends a prompt to the sidekick agent CLI in a split pane of the caller's own window, refuses when there is no sidekick or more than one, and exits non-zero when it cannot confirm the message arrived. The guide and the `ccm setup-claude-md` template use it.
- Auto-exits are recorded in `$CCM_DATA_DIR/state/auto-exit.log`; `ccm doctor` shows the count.
- `@ccm-ambiguous-width 1|2` declares how your terminal draws ambiguous-width glyphs, so the status bar stops reserving room for the wider case. `CCM_AMBIGUOUS_WIDTH` remains as a fallback outside tmux; the option wins when both are set.
- `@ccm-status-line-hide-shell on` lists only windows hosting a Claude session in status-bar modes 1 and 2 (off by default; IDLE projects stay visible).
- `@ccm-status-line-position left` places mode 1's entries on the side of the bar away from the clock, highest priority first (default `right`).

### Changed
- `ccm sidekick-send` does not check the sidekick's input box for a draft; capture the pane first.
- `ccm send` queues a message the target cannot take right now and reports `Queued for <project>`. Scripts that rely on a non-zero exit for a non-delivery should use `--now`.

### Fixed
- A pane running a tool no longer reads as IDLE partway through a turn.
- The guide's example search binding notes that `prefix + /` is also tmux-copycat's, and that a second binding replaces the first.
- Projects whose paths differ only in punctuation (such as `a/b`, `a-b`, `a_b`) each track their own session.
- The background-session list no longer shows a nameless row with nothing to attach to.
- `ccm capture` says when it read nothing instead of printing an empty block.
- The folder-trust prompt reads as PERMIT.
- Permission dialogs are recognised whichever space Claude Code draws after the question, and a quoted question in the transcript no longer reads as a live dialog.
- A session whose transcript is stored outside the usual directory no longer stays BUSY.
- A delivered message is no longer reported as unconfirmed when the input box wraps it.
- A prompt already sent no longer reads as a draft that makes `ccm send` refuse or queue messages until they expire.
- An expired, undelivered message is reported by a desktop notice, a warning in `ccm status`, the dashboard and `ccm doctor`, and its own row in `ccm spool list`; `ccm spool clear-expired` acknowledges it.
- `ccm send` no longer types into an input box holding a draft; it queues the message (or refuses with `--now`) and quotes the draft's opening.
- Moving the cursor onto a permission dialog's deny option no longer makes it read as IDLE.
- The Claude-in-Chrome permission dialog reads as PERMIT without hooks.
- A Claude Desktop or VS Code session opened in a managed project's directory no longer marks the window PERMIT or notifies for it.
- An external agent is shown only by its `⚙name` badge, and dashboard columns no longer shift after the dashboard opens.
- Deleting a project from the dashboard asks for confirmation.
- The dashboard menu (`m` / `?`) lists unregistering, deleting and ignoring a project, and the name prompt defaults to the selected project.
- The settings canaries read the administrator's and each project's settings as well as the user's, and name which one carries the flag; `allowManagedHooksOnly` is reported only from the administrator's settings.
- The status bar re-lays itself when the terminal is resized.
- Status-bar layout: `@ccm-ambiguous-width` no longer leaves empty space, mode 1's left-placed entries no longer sit mid-bar, themes using box-drawing, geometric or Nerd Font glyphs no longer cover the first entry, and a `status-right` using `%T` or `%F` no longer clips it.
- Saving a setting from the dashboard or setup wizard no longer removes other settings whose name extends it from `~/.tmux.conf` (such as `@ccm-status-line-hide-shell` when writing `@ccm-status-line`).
- `ccm init` outside tmux reads the current status-bar mode correctly when `@ccm-status-line-position` is also set.
- A permission dialog dismissed with Esc no longer returns to PERMIT or blocks `ccm send`.
- Auto-exit no longer closes panes that run `claude` directly, with no shell underneath.

## [0.10.0] - 2026-08-08

### Added
- `ccm debug trace` accepts a tmux pane, window, or `session:index` as well as a project name, so sessions ccm does not manage can be traced.

### Fixed
- A pane that runs `claude` directly, with no shell underneath, is detected instead of reading as SHELL.
- `ccm debug trace` with an empty argument is refused.
- The README documents the dashboard's `w` key, and a broken guide link in the Japanese README is fixed.

## [0.9.0] - 2026-08-05

### Added
- Sidekick attention: when a sidekick waits on a permission dialog, its badge turns yellow in the dashboard, `ccm status` and the status bar, and a desktop notification names the request; the window's own state is unchanged. Toggle with dashboard `w` or `@ccm-sidekick-attention off`.
- A second Claude Code running as a sidekick (`CCM_IGNORE=1 claude`) needs no setup. Support for non-Claude agents is exploratory and documented in the guide.

### Fixed
- A session interrupted with Esc is released at once.
- BUSY is released even when the session transcript cannot be read.
- ccm's hooks ignore events from other agent CLIs that read Claude Code's settings file.
- Agent CLIs launched through a platform-suffixed binary get their presence badge again.

## [0.8.2] - 2026-07-30

### Fixed
- A turn running past the hour mark is recognised as active work again.
- The session-resume dialog is recognised for sessions of any age.
- `ccm send --start` no longer refuses a long message that did arrive.
- `ccm doctor`'s multi-claude scan reports when it cannot run.

## [0.8.1] - 2026-07-30

### Added
- The guide includes a diagram of the sidekick arrangement (`assets/sidekick-model.svg`), covers using another Claude Code as the sidekick (`CCM_IGNORE`), and notes that a sidekick answers only to the Claude session sharing its window.
- External-agent detection covers Codex, Gemini and Grok alongside Kimi.
- `ccm doctor` lists windows hosting more than one visible Claude session.

### Changed
- `ccm send`'s ambiguity refusal points at `CCM_IGNORE` and the dashboard's `i` key.
- Product names are capitalised consistently across the diagrams, README and guide.

### Fixed
- The `ccm setup-claude-md` template's procedure for reaching a sidekick pane includes the pause delivery needs. Run `ccm remove-claude-md && ccm setup-claude-md` to update an existing `~/.claude/CLAUDE.md` section.

## [0.8.0] - 2026-07-27

### Added
- The guide and the `ccm setup-claude-md` template cover relaying work with a second agent CLI.

### Fixed
- The status bar runs a full detection pass at most every `CCM_RECONCILE_INTERVAL` (20 s) instead of every second.
- A session interrupted with Esc mid-tool is released after `CCM_BUSY_STALE_RELEASE_SEC` (default 60 s) instead of showing BUSY for up to 10 minutes.

## [0.7.1] - 2026-07-27

### Added
- The README documents `ccm search`, the `⊘` and `⚙<name>` markers, sidekick support, and what `ccm setup-claude-md` writes into your global Claude Code instructions.

### Fixed
- Uninstall instructions detach ccm from Claude Code first, so no hooks point at a removed plugin directory.
- `ccm send`'s help documents `--` and `--yes`, and `ccm send` addressed at its own project is refused with an explanation.
- A `⚠ PERMIT` left after its dialog is gone and the session stops updating is released after `CCM_PERMIT_MAX_TIMEOUT` (default 10 min) instead of staying indefinitely.

## [0.7.0] - 2026-07-26

### Added
- Presence badge for external agent CLIs: a window with a pane running one shows a dim `⚙<name>` (display only).
- `CCM_IGNORE` hides a Claude Code session from ccm: launch with `CCM_IGNORE=1 claude`, or toggle with `ccm ignore` / `ccm unignore` (dashboard `i`). An ignored pane is left out of window state, `ccm send` and auto-exit, and its row is marked with a dim `⊘`.

### Changed
- The dashboard preview shows the tracked Claude pane, never the focused pane or a hidden sidekick.
- The dashboard keeps its row order while open; reopening re-sorts by current state.

### Fixed
- `ccm capture` captures every pane of a split window, each under a header naming what runs in it.
- `ccm inject-status --fast` no longer runs periodic maintenance.
- Notifications work with the stock macOS bash.
- `ccm send` re-checks the target pane's state immediately before typing.
- `ccm stop --all` works from the CLI again.
- The dashboard no longer crashes when `p`/`n`/`r`/`i` is pressed on a background-session row.
- Exit-all and auto-start on attach type into the Claude pane rather than the focused pane.
- `ccm doctor` no longer crashes when a checked binary is missing.
- `ccm attach` detects Claude on any pane of the window.
- Windows sharing a project directory are no longer shown with a wrong state or refused by `ccm send`.
- Digit-only project names are rejected.
- `ccm status` columns stay aligned, and long names are truncated.
- Shortening `$HOME` to `~` no longer corrupts paths that contain the home path mid-path.
- The dashboard no longer stops an unrelated process when its recorded process id is stale.
- A delayed idle notification no longer clears a fresh BUSY (`CCM_IDLE_PROMPT_GUARD_SEC`, default 60 s).

## [0.6.0] - 2026-07-22

### Added
- Permission mode is shown in a `MODE` column in `ccm status` and as a dashboard badge.
- The hook-silence canary logs each firing to `~/.local/share/ccm/state/hook-silence.log`; `ccm doctor` reports the count.

### Changed
- The dashboard and status bar show state changes within a second.

### Fixed
- The `⊘` sidekick marker clears when a hidden sidekick exits, and the hook-silence canary no longer misfires when a hidden sidekick shares the window's directory.
- Spinners with token counts below 1000 are recognised as active work.
- Idle auto-exit no longer treats an editor or pager in a sibling pane as background work.

## [0.5.3] - 2026-07-17

### Changed
- Auto-exit skips windows with live background work, and sends a desktop notification when it fires.

### Fixed
- `ccm send` types into the pane hosting Claude rather than the window's active pane.
- `ccm send` no longer drops lines that start with a dash.
- Status-bar layout measures CJK names in terminal columns.
- Projects whose path contains non-ASCII characters no longer stay BUSY indefinitely.

## [0.5.2] - 2026-07-11

### Added
- Hook-silence canary (opt-in): warns when a session's hook log stops updating while the conversation is active. Enable with `tmux set -g @ccm-hook-silence on`.

### Fixed
- Reloading `.tmux.conf` no longer adds a duplicate focus hook each time.
- The mode-2 status bar no longer freezes when projects exceed tmux's five-line status limit.
- A phantom `SubagentStart` no longer holds a false BUSY while the pane is idle.
- `ccm debug trace` uses the same detection as the status bar and prints the derived state in an `ev=` column.

## [0.5.1] - 2026-07-04

### Changed
- The status bar updates immediately on a window switch; new `ccm inject-status --fast` updates the status bar right away.

### Fixed
- The dashboard repaints when a background pane's output draws over the popup.
- A local slash command at an idle prompt no longer flips the window to BUSY.
- The dashboard no longer sticks at PERMIT while a background subagent's tool runs.
- Permission dialogs without a separate footer read as PERMIT.

## [0.5.0] - 2026-06-25

### Added
- `CCM_SEND_TRACE=1` logs every keystroke `ccm send` sends.

### Fixed
- `ccm send --start` checks that the message reached the input box before submitting it.
- The dashboard no longer sticks at PERMIT while an approved tool runs.
- Auto-exit no longer types into the wrong shell pane or sends `clear` before Claude exits, marks the window SHELL only after exit, and stops when the focused window cannot be found.
- Loading a malformed snapshot fails with a readable message.
- The `/model` picker reads as PERMIT again.
- Assorted robustness fixes in detection, runtime, UI and hooks.

## [0.4.0] - 2026-05-24

### Added
- `ccm add` offers to create a missing target directory when its parent exists.
- Read-only view of Claude Code's background sessions in the dashboard (`b`, or `@ccm-bg-section always`) and `ccm bg list`; `Enter` on a background-session row opens a new tmux window running `claude attach`.

### Fixed
- The dashboard path column no longer shifts as the `* elapsed` marker ticks, and the marker no longer flickers during multi-turn auto-loops.
- `ccm send` refuses targets showing the `claude agents` view.
- COMPLETED notifications no longer fire while Claude reports background work.
- The reworked `/model` footer reads as PERMIT again.
- `ccm send --start` waits for the target to be ready instead of a fixed two seconds.
- Interactive choice menus read as PERMIT instead of BUSY.

## [0.3.0] - 2026-05-05

Initial public release: Claude Code sessions as tmux windows, with live state detection, an interactive dashboard, status-bar integration, and snapshot save/restore.

### Project management
- Projects are tagged tmux windows, managed with `ccm add` / `open` / `register` / `unregister` / `remove` / `attach` / `list` / `rename`.
- `ccm send <project> <message>` sends prompts across projects and never types into a window showing a permission prompt, even with `--force`.
- Snapshot save / load / list / delete, with an `_autosave` snapshot every two minutes and on `ccm stop --all`.
- Claude Code starts on attach to a shell window; idle sessions exit after `CCM_IDLE_EXIT_TIMEOUT` (10 min) and resume on the next attach.

### State detection
- Four states (PERMIT / BUSY / IDLE / SHELL), plus DOWN when tmux is not running.
- Permission prompts are seen even when hooks stop firing.
- A window shows the state of its most urgent pane, whichever pane has focus.
- `ccm debug trace <target>` prints every detection input and the resolved state for each scan.

### Dashboard
- Popup (`prefix + Tab`) showing each project's state, git branch, listening ports and pane count.
- A `* elapsed` marker after a session finishes, a `(bg)` note for background activity, and an age suffix on stale signals.
- Live filter (`/`), tree view (`prefix + T`) and menu (`prefix + C`).
- Attaching focuses a pane waiting on a permission prompt.

### Status bar
- Three modes via `@ccm-status-line`: an icon in your `status-right`, a replacement window list, or a dedicated row with branch and port details (default).
- Themable colours; polling interval set by `CCM_STATUS_INTERVAL`.

### Notifications
- Desktop notifications via `@ccm-notify` (`permit` / `completed` / `all`), grouped per project on macOS; `ccm clear-notifications` clears them.
- A completion alert arrives only when a turn has really finished.

### Robustness
- Warnings when `~/.claude/hooks.log` grows past 100 MB, when settings disable every hook, and when sessions exit repeatedly in a short time.
- `ccm doctor` checks dependency versions, hook installation, every warning, per-project state and the error log.
- `ccm errors` prints the log of suppressed errors.
- CJK and emoji project names align.
- A detection error in one project does not affect the others.

### Setup / integration
- `ccm init` setup wizard; `ccm setup-hooks` / `remove-hooks` and `ccm setup-claude-md` / `remove-claude-md` attach and detach ccm from Claude Code.
- Zsh completion; English and Japanese documentation.

### Requirements
- tmux 3.2+, Claude Code v2.1.107+, jq, fzf, Python 3.
