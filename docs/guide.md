# ccm User Guide

[Getting started](#getting-started) · [Troubleshooting](#troubleshooting) · [Diagnostics](diagnostics.md)

## How ccm fits into tmux

tmux organizes your terminal into a hierarchy. ccm works within this structure:

```
Terminal (Ghostty, iTerm2, etc.)
 └── tmux server
      └── Session          ← your working context
           ├── Window 0    ← Project A (managed by ccm)
           ├── Window 1    ← Project B (managed by ccm)
           ├── Window 2    ← Project C (managed by ccm)
           └── Window 3    ← your shell (not managed by ccm)
```

**Key concept:** ccm manages Claude Code sessions as **tmux windows** within your existing session. Each project gets its own window. You can switch between projects just like switching tmux windows.

### What ccm does NOT do

- ccm does not create separate tmux sessions per project
- ccm does not modify your terminal emulator settings
- ccm does not require a specific terminal emulator

## Getting started

### 0. Authenticate Claude Code

If you haven't used Claude Code before, run it once to complete the initial setup:

```bash
claude
```

Follow the interactive prompts to choose your plan (subscription or API key) and authenticate via browser. Once done, you're ready to use ccm.

### 1. Start tmux

```bash
tmux new-session -s work
```

### 2. Add your first project

```bash
ccm add ~/code/my-project
```

This creates a new tmux window, changes to the project directory, and launches Claude Code — with `claude --continue` (so you pick up the most recent conversation in that directory), or plain `claude` when ccm can see that the directory has no conversation yet: the CLI's config home is the default, no custom transcript-directory name (`CLAUDE_CODE_PROJECT_DIR_NAME`) is set in ccm's environment or in the tmux environment the new window's shell inherits, and the transcript directory for the path is absent or empty. Whatever ccm cannot check resolves to `--continue`. The check covers what ccm's and tmux's environments show; a shell startup file that moves the transcript location is outside it, so with such a file `ccm add` can open a new conversation for a directory whose conversation lives at that other location.

### 3. Add more projects

```bash
ccm add ~/code/another-project
ccm add ~/code/third-project api-server   # custom name
```

### 4. Switch between projects

Use the dashboard (`prefix + Tab`) or:

```bash
ccm attach my-project    # by name
ccm attach 2             # by number
```

> [!TIP]
> When you switch to a project window where Claude Code isn't running, ccm automatically starts it with `claude --continue` — always, for a shell that already exists: that shell may keep its transcripts somewhere ccm cannot see (its own exports are invisible from outside), so ccm never claims it has nothing to resume. Only `ccm add`, whose window ccm creates itself, types plain `claude` for a directory with no conversation yet. There is no fallback chained behind `--continue`: if it cannot resume — there was nothing to resume after all (a project added with auto-start off, say), or its newest conversation was handed to a live background session — its message stays on screen and the pane returns to the shell, so you decide what to open next.

### 5. Check status

```bash
ccm status
```

```
STATUS       PROJECT              MODE     BRANCH           PORTS        DIRECTORY
------       -------              ----     ------           -----        ---------
◉ BUSY       my-project           manual   main*            3000         ~/code/my-project
● IDLE       another-project      accept   feature-x        -            ~/code/another-project
⚠ PERMIT     api-server           manual   main             8080         ~/code/api-server
```

The `MODE` column shows each project's Claude Code permission mode
(`manual` / `accept` / `plan` / `auto` / `dontAsk` / `bypass`), taken from
the newest hook event. This matters for multi-project work because modes
that auto-resolve permission dialogs (`auto`, `dontAsk`, `bypass` — and
`accept` for file operations) never produce a PERMIT state: if a project
seems to "never ask for permission", check its mode before suspecting
detection. `bypass` is shown in warning color — every guardrail is off.
`-` means no mode is known yet (Claude not running, hooks not installed,
or no hook has fired since startup). The dashboard shows the same
information as a `{mode}` badge after the project name, omitted for the
everyday `manual` mode to keep rows quiet. A mid-session mode change
(shift+tab) updates on the next hook firing.

## The Dashboard

Open with `prefix + Tab`. This is the primary interface for managing projects. You can also bind a single key (e.g. `F1`) for prefix-free toggle — see the [Keybindings section in README](../README.md#keybindings) for details.

> ```
> ── ccm Dashboard ──────────────
>   6 project(s)
>
> ▶ #5  ⚠ PERMIT  ml-pipeline                ~/code/ml-pipeline
>   #4  ● IDLE    auth-service     * 2s      ~/code/auth-service
>   #2  ◉ BUSY    api-gateway                ~/code/api-gateway
>   #3  ● IDLE    web-dashboard {accept}     ~/code/web-dashboard
>   #6  ● IDLE    mobile-app                 ~/code/mobile-app
>   #7  ■ SHELL   docs-site                  ~/code/docs-site
>
> [↑↓/jk] select [Enter] attach [p]review [a]dd [n]ame [r]emove
> e[x]it all [s]ave [t]ree [m]enu [q] quit
> Hooks: ON
> ```

### Dashboard actions

| Key | Action | When to use |
|-----|--------|-------------|
| `↑↓` or `jk` | Move selection | Navigate between projects |
| `Enter` | Switch to project | Jump to the selected project window |
| `s` | Save | Save snapshot (enter name or default `_autosave`) |
| `p` | Preview | See what's on the project's screen (press `c` to copy) |
| `a` | Add | Register a new project directory |
| `n` | Rename | Change the selected project's name |
| `g` | Register | Tag an existing tmux window as a ccm project |
| `r` | Remove | Choose [u]nregister (keep window) or [d]elete (kill window; asks y/N). Listed in the menu (`m` / `?`) too |
| `i` | Ignore | Toggle CCM_IGNORE on the selected project. Hiding requires `y` / `Y` + Enter; Enter alone, Esc or any other answer cancels. Restoring needs no confirmation (see "Running a second model" below) |
| `x` | Exit all | Review names and states, then exit Claude after screen checks; keep windows |
| `/` | Filter | Live incremental search: type to narrow, `↑↓`/`C-p`/`C-n` to select, `Enter` to attach, `C-u` to clear, `Esc` to cancel. Unicode-safe — Japanese project names match on Japanese substrings |
| `t` | Tree | Switch to tree view |
| `m` / `?` | Menu | Logout preparation, continue restore, checkpoints, reset and Claude exit |
| `u` | Undelivered messages | Open expired / held records; also available in the menu (`m` / `?`) |
| `q` / `Esc` | Quit | Close the dashboard |

The dashboard refreshes on a hybrid cadence: full state detection runs every 2 seconds, and in between, a lightweight fast tick (4×/second) watches the state channel the Claude Code hooks write to — so a hook-driven change (a permission prompt appearing, a prompt submitted) shows up in ~0.3 seconds rather than waiting out the full poll. The status bar gets the same treatment: on a state transition, the hook re-renders the bar immediately instead of waiting for the next `status-interval` tick. Navigation keys (`↑↓/jk`) respond instantly without waiting for any refresh.

The row order is decided when the dashboard opens (projects needing attention first) and then held stable while it stays open — a project changing state updates its icon in place but does not jump to a new position, so your selection never lands on the wrong project mid-interaction. Close and reopen the dashboard to re-sort by current state.

### Menu help preview

In the `m` / `?` menu, an enabled preview shows help for the selected item. Right placement requires at least 80 columns; bottom placement requires at least 20 rows. It uses the same dimensions as the project preview. When disabled or too small, only the menu appears. Descriptions wrap within the panel and follow selection changes; setting items also show their current values. Set `set -g @ccm-lang "ja"` in `~/.tmux.conf` for Japanese menu descriptions (`en` is the default; unsupported values use English). Reload the configuration and reopen the menu to apply it. Only the description body changes; labels and other screens remain English. Matching dashboard shortcuts appear as dim `[key]` hints at the right edge; narrow menus omit hints to preserve item names.

Items are grouped under Actions, Settings and Navigate. Setting values are colored (`on` green, `off` dim, others cyan), and the preview highlights the equivalent CLI command and the current value. Press `/` in the menu to filter items as you type, as in the project list: matching is case-insensitive on item names and on the first line of each description in the current `@ccm-lang` language, so `ログアウト` finds the logout items when descriptions are Japanese. `↑↓` / `C-p` / `C-n` select, `Enter` runs the selected item, `C-u` clears and `Esc` returns to the full menu with that item still selected.

The following items complement the checkpoint, logout and recovery actions described in the next section.

| Menu item | Purpose |
|---|---|
| Add project | Choose a directory and name for a new project. |
| Unregister project | Keep its window while removing ccm management. |
| Delete project | Choose a project and confirm closing its window. |
| Ignore / unignore project | Hide or restore a project in tracking. |
| Undelivered messages | Review messages that expired or need inspection. |
| Save snapshot | Choose a name and save the current layout. |
| Status bar mode | Choose how much the status bar displays. |
| Auto-restore | Choose whether tmux startup restores the autosave. |
| Idle timeout | Set the wait before idle Claude sessions exit. |
| Preview panel | Show or hide project previews and menu help. |
| Preview position | Place the preview on the right or below the list. |
| Dashboard display | Show the dashboard as a popup, or docked at the top or bottom. |
| Close dock after opening a project | When docked, choose whether opening a project closes the dock. |
| Background sessions | Show or hide background sessions on the dashboard. |
| Auto-start Claude | Choose whether opening a shell project starts Claude. |
| Notifications | Choose which changes generate a notification. |
| Notification sound | Enable or disable sounds (macOS). |
| Sound name | Choose and preview a sound (macOS). |
| Dashboard | Return to the project list. |
| Tree view | Open the session/window/pane hierarchy. |
| Quit | Close the dashboard. |

### Logout, restoration and recovery

Open the menu with `m` or `?`. These actions have no new single-key shortcut.

| Menu item | Action |
|-----------|--------|
| Prepare for logout | Save and protect `_autosave` through `ccm prepare-logout`. If projects are BUSY or PERMIT, their names appear and `[y/N]` defaults to No |
| Cancel logout protection | Confirm releasing protection while keeping the saved checkpoint |
| Continue restore | Continue the checkpoint recorded by an incomplete restoration. If it cannot be identified, choose from saved checkpoints |
| Saved checkpoints (load / delete) | Select by name, creation time, project count, v1/v2 and protection status. Enter loads; `d` asks `[y/N]` before deleting; Esc returns. `_autosave.prev` is excluded, and protected `_autosave` cannot be deleted |
| Reset selected project's runtime state | Confirm clearing its runtime signals and caches using `ccm reset`. Conversation files, running processes, windows and checkpoints remain |
| Exit Claude in selected project | Confirm `ccm exit <name>`. Its window, shell and sidekicks remain |

Preparation and restoration temporarily leave the curses screen so progress and prompts are visible. On return, the full result, failure reason and sidekick guidance stay in a scrollable view: `↑↓` / `jk` / `PgUp` / `PgDn` scroll; Enter / Esc / `q` returns. An incomplete restoration or protected checkpoint also shows an actionable line above the project list. The menu scrolls when the terminal is small.

Confirm with `y` or `Y` and Enter. `N`, Enter alone and Esc cancel. `x` now shows the projects and states before confirming a batch exit, using the same checks as a single exit. In PERMIT, Escape rejects the pending tool call; this is stated before confirmation. A project that becomes BUSY/PERMIT after an IDLE confirmation is refused and can be reviewed again.

`ccm exit <name>` exits Claude while retaining the window; `ccm stop <name>` closes the window. CLI exit refuses BUSY/PERMIT unless `-y` / `--yes` is supplied. Exit checks the Claude pane, excludes ignored panes and requires a shell underneath Claude. Ambiguous Claude panes need a reserved primary pane. It sends no keys when the agent view is open or the screen cannot be read, and reports failure unless the foreground returns to a shell. If exit detaches into agent view, it reports that result without sending further keys. These screen checks are shared with idle auto-exit.

### Handling undelivered messages

Press `u` to open expired records (never delivered) and held records (last seen in the recipient's input box). Each row shows the sender, destination, age and beginning of the message.

| Key | Action in the list |
|-----|--------------------|
| `↑↓` / `jk` | Select a record |
| `Enter` | Read the full text. Long lines wrap; scroll with `↑↓` / `jk` / `PgUp` / `PgDn`. Return to the list with `q` / `Esc` / `Enter` |
| `r` | Resend an expired record as a new message, after confirmation. If Claude is stopped, asks whether to start it and send. Clears the original record only on success; on failure, shows the reason and keeps it |
| `d` | Confirm deletion of the selected record only. Deleting a held record does not change the recipient's input box |
| `o` | Open the destination's existing project without typing keys or automatically starting Claude. Use it to inspect the input box or start Claude manually |
| `q` / `Esc` | Return to the dashboard |

Held records have no resend action: use `o` to inspect the recipient's input box first, avoiding a duplicate copy. Resend and discard default to No; cancelling does nothing. The list is read on opening and after an action, preserving the order of existing records and the selected record when it still exists. New records are appended. Reopen the list to see external changes.

### Direct-to-filter shortcut

If you find yourself reaching for `/` right after opening the dashboard, bind `@ccm-key-search` in your `~/.tmux.conf` to open the dashboard already in live-filter mode:

```tmux
set -g @ccm-key-search "/"   # prefix + / → dashboard opens in filter mode
```

Check the key is free before you take it — `prefix + /` is tmux-copycat's search, for one:

```bash
tmux list-keys -T prefix | grep -w '/'
```

Prefix keys are a namespace shared with every other plugin you run, and a second binding silently replaces the first. The symptom is that some other plugin's key stops working, which is hard to trace back to a line you added elsewhere. This applies to `@ccm-key-tree`, `@ccm-key-menu`, and `@ccm-key-dashboard` equally.

You can also run `ccm search` (or `ccm dashboard --search`) from a shell or another tmux binding for the same effect. This is handy when you have many projects — type a few characters to jump straight to the one you want, instead of hunting through the full list.

### Prefix-less dashboard hotkey

If you prefer a top-row function key over `prefix + Tab`, bind one with `@ccm-key-dashboard-noprefix`:

```tmux
# Must come BEFORE the ccm plugin load line — see the
# IMPORTANT block in README.md#keybindings for the full rule.
set -g @ccm-key-dashboard-noprefix "F1"   # F1 alone (no prefix) → dashboard
```

Goes through the same `display-popup` invocation as the prefix binding, so the coloured ccm logo on the popup title is preserved. Writing your own `bind-key -n F1 display-popup …` instead works mechanically but won't carry the logo unless you replicate the full `-T` format string.

### Docked dashboard

```tmux
set -g @ccm-dashboard-dock top        # top or bottom; off (default) keeps the popup
set -g @ccm-dashboard-dock-size 40    # percent of the window height, 10-80 (default 40)
set -g @ccm-dashboard-dock-close-on-open on   # close the dock after opening a project (default off)
set -g @ccm-dashboard-dock-edge 179    # colour of the line on the side facing your work: a 256-colour number, or off
```

Both can also be changed from the dashboard menu (`Dashboard display`, and `Close dock after opening a project` while docked); the menu saves them to `~/.tmux.conf`.

With docking on, `prefix + Tab` (and `@ccm-key-dashboard-noprefix`) opens the dashboard as a full-width pane in the current window instead of a popup, so it stays in view while you work in the panes beside it. The option is read on every key press; no reload is needed.

- The same key closes it. Pressed in another window, it brings the pane there.
- Switching windows by any means moves the pane to the window you switch to. The window it leaves gets its previous layout back.
- Opening a project from the dashboard keeps it open and it moves along with you, unless `@ccm-dashboard-dock-close-on-open` is on. `q` or `Esc` closes it.
- A docked dashboard shows the ccm logo on its first line, as the popup does in its title (on its second when docked at the bottom, below the edge line).
- It draws a coloured line along the side that faces the panes you work in (its last row when docked at the top, its first when at the bottom), so the boundary stays clear whatever your tmux border settings. `@ccm-dashboard-dock-edge off` gives that row back to the dashboard.
- The pane is never less than 10 rows high, the smallest the dashboard draws in, plus one for the edge line when it is on. A pane shrunk below that drops the line rather than the list.
- The pane is ccm's own: the logout checkpoint records each window as it is without it, and Claude is never started in it. Menu, tree and filter keys still open popups.

## The Tree View

Open with `prefix + T`. Shows the full tmux hierarchy:

> ```
> work ◀
>   ◉ my-project (main*) ~/code/my-project ◀
>   ● another-project (feature-x) ~/code/another-project
>   ⚠ api-server (main) [:8080] ~/code/api-server
>   ■ bash ~/home
> other-session
>   ■ bash ~/home
>
> [↑↓/jk] select  [Enter] attach  [q/Esc] quit
> ```

- `◀` marks your current session/window
- Only windows (not sessions or panes) are selectable
- Panes are shown only when a window has multiple panes

## Sending Prompts Between Projects

`ccm send` dispatches a prompt to another project's Claude Code session, so you can hand off work between projects without leaving your current pane.

When calling `ccm send` from a sidekick or a sandboxed tool, run it outside
the sandbox so it can reach tmux's Unix socket. Network restrictions can
block that socket even when `TMUX` is set. A connection failure includes
tmux's error and guidance to retry outside the sandbox; it does not mean
the project is missing or that the caller is outside tmux.

Queued messages use the caller pane's registered project when the pane's
process is an ancestor of the sending command. Changing directories, even
into another project, preserves that sender and pane identity. If ancestry
cannot be confirmed (including when process inspection fails), ccm checks
whether the pane's registered directory contains the command's working
directory. Both paths are resolved through symlinks; subdirectories count,
sibling name prefixes do not. If that check fails, ccm uses the working
directory only if exactly one registered window contains it. Nested or duplicate
registrations are ambiguous; linked copies of the same window count once.
Otherwise the sender is `unknown`, and the delivered header asks the recipient
to confirm the reply destination instead of suggesting `ccm send unknown`.

The unique-window fallback identifies a project, not a specific caller pane.
Self-send checks and the `--start` pane exclusion use a verified pane hint.
`sidekick-send` and `ignore` / `unignore` without a project argument refuse an
unverified hint; run those commands from a registered project pane. When
ancestry cannot be confirmed, the directory check cannot distinguish stale
hints between panes registered to the same directory.

```bash
# Simple positional message (confirmed interactively if run from a TTY)
ccm send demo "Summarize the last review cycle."

# From a file
ccm send research --file /tmp/brief.md

# From a pipe — perfect for wiring up an MCP server (Gmail, GitHub, etc.)
echo "Please investigate issue #42 in the parser repo" | ccm send parser --stdin -y

# Multi-line body — \n is converted to Claude's "newline without submit" key,
# so the body lands as a single multi-line prompt
printf 'context:\nbug: NPE on line 120\nplease fix' | ccm send api-server --stdin -y

# Type text without submitting (user finishes editing in the target pane)
ccm send demo --no-enter "TODO: "
```

### State policy

| Target state | Default | `--now` | `--force` | `--start` |
|---|---|---|---|---|
| **IDLE** | Send immediately | Send immediately | — | — |
| **BUSY** | Queued for later delivery | Refused | Sent now into the input buffer (mixes with the current turn) | — |
| **SHELL** (Claude not running) | Queued — delivered once Claude is running and idle | Refused | — | Launches Claude, polls for IDLE and a ready, empty input box (`CCM_START_WAIT_SEC` polling deadline, default 10s), then sends |
| **PERMIT** (permission dialog open) | Queued — delivered after the dialog is resolved | **Refused** | Still queued — a dialog is never typed into, even with `--force` | — |
| **IGNORED** (every Claude pane hidden) | **Refused** — never queued | Refused | Refused | Refused — Claude is never launched into a window ccm cannot see. The refusal points at `ccm unignore <project>` |

`--start` waits for an empty input box in the launch pane, with that condition and IDLE persisting for one second, before typing the body. If readiness is not established by the polling deadline, it reports failure with exit code 1 without typing the body or its submit Enter. This also applies when the launch command exits straight back to the shell. State detection and screen capture time can extend beyond the configured polling deadline.

After launching, ccm types the body once and sends the submit Enter only if the whole body is visible in the input box. It checks immediately, then reads again every 0.1 seconds for up to 2 seconds without sending any keys, proceeding as soon as the whole body appears. Screen capture time can extend beyond this polling deadline. If only part appears, nothing appears, or the input box cannot be read by the deadline, it stops with exit code 1 without clearing, retyping, or submitting. Check the recipient's input box and clear any leftover text before deciding whether to send again. The same check applies to short messages and `--no-enter`. A body that looks taller than the input box can show (about half the pane height less five rows, counting wrapped lines; an estimate, so emoji sequences or tabs can tip it either way) could not be seen whole, so ccm does not type it: it stops with exit code 1 before typing, leaving Claude running. Send it again without `--start`, or put the text in a file and send a short line pointing to it. An empty input box and IDLE do not guarantee that a newly launched application accepts every keystroke; screen confirmation is not proof of model receipt.

After the submit Enter, ccm checks the input box again. A hold notice, or the submitted message's beginning staying at the start of the input box throughout the check, produces an unsent error with exit code 1 instead of `Sent`. ccm does not press Enter again automatically. Inspect the recipient's input box before deciding what to do. This is a screen-based check: it cannot fully distinguish cases such as an unreadable screen or a new draft with the same beginning typed immediately after acceptance.

### The spool (store-and-forward)

When the target cannot take a message right now, `ccm send` no longer fails by default: the message is written to `$CCM_DATA_DIR/spool/<project>/` and delivered by the periodic status pass once the project reads IDLE again. You get an explicit `Queued for <project>` line with the queue length and a message id — a send that queued is not a send that arrived.

Delivery is deliberately conservative:

- **One message per project per pass.** A second message sent back-to-back would land in the input buffer of the turn the first just started, so the queue drains one per idle transition.
- **Re-checked at delivery time.** The pane's raw state is re-detected right before typing, and a composer holding a draft defers the message rather than merging into it.
- **TTL.** A queued instruction goes stale — delivered an hour late it is no longer the instruction you meant. Messages older than `CCM_SPOOL_TTL_SEC` (default 60 min) move to `expired/` and surface in `ccm status` / `ccm doctor` instead of delivering. Queued messages never start a Claude session on their own.
- **At-least-once.** A crash mid-delivery re-delivers on the next pass rather than losing the message; the envelope line lets the receiver tell a duplicate apart.

Delivered messages arrive under an envelope header — `[from: <project> · queued 14:03 · delivered 15:02 — reply with `ccm send <project> "…"`]` — so the receiver knows who sent it, how long it waited, and how to answer.

Queued counts show on `ccm status`, `ccm doctor`, and the dashboard (`✉N` by the project name). To inspect or withdraw:

```bash
ccm spool list                   # pending, held and expired messages with previews
ccm spool clear-expired           # acknowledge messages that never arrived
ccm spool clear-held [project]    # acknowledge messages a session was holding
ccm spool cancel <id> <project>   # withdraw one (a queued mis-send is cancellable)
ccm spool cancel --all <project>  # clear the project's queue
ccm spool show <expired|held> <id> <project>
ccm spool discard <expired|held> <id> <project> [--yes]
ccm spool resend expired <id> <project> [--start] [--yes]
```

Expired messages were never delivered. `ccm spool list [project]` shows each record's id, saved sender label, time since it was queued, and the first nonblank line (up to 60 characters). Unknown filenames show unknown sender/time; unreadable bodies show `(unreadable)` without hiding the record. For each expired record, the list prints a command to read the full text and a `ccm send --file …` command to use **only after reviewing whether the request is still needed**. If it needs updating, send revised text instead. These displayed commands do not run automatically.

Using the listed `ccm send --file …` command is a **new send**: if queued, it gets a new id and queue time, with the current sender in the delivery envelope. A ready target receives it directly through the usual send path. A SHELL target stays queued; the suggested command does not use `--start`. The old expired record remains until the normal seven-day retention expires or you acknowledge it. After reviewing **all** expired records for a project, `ccm spool clear-expired <project>` deletes those records only; it sends nothing. Without a project argument, it clears expired records across all projects. Neither listing nor clearing records retries delivery.

To handle one record directly, use `show` to read its full text or `discard` to delete only that record. `resend expired` sends the saved body now as a **new send**, clearing the original record only after the send check succeeds. If the target cannot receive it now, for example while BUSY, it fails without queueing a new copy and keeps the original record. Add `--start` to start Claude before sending if it is stopped. To revise the body, send the updated text separately with `ccm send`.

`discard` and `resend` ask for confirmation. After reviewing the record, use `--yes` (`-y`) to skip that prompt; this explicit flag is required in non-interactive use. Held records cannot be resent with `resend`. `discard held` deletes only the record, leaving the recipient's input box untouched. The dashboard's `u` view uses these same operations.

The dashboard's `u` list and full-text titles distinguish **Message** from
**Completion notice**. They share these labels with `ccm doctor`'s nonzero
undelivered counts; stored kinds, IDs and CLI selectors stay unchanged:

| Label | Meaning / next action |
|---|---|
| `Expired before delivery` | Read the record. Only ordinary expired messages can be sent anew after review. |
| `Waiting in input box` | Check the recipient's input box; do not send another copy. |
| `Delivery unconfirmed` | Check the conversation and input box. Automatic notices are never resent. |
| `Not sent: hourly limit` | Read the sidekick's result; the notice was not delivered. |
| `Cancelled before delivery` | Check the sidekick's result if still needed. |
| `Queued; not delivered yet` | Still waiting to deliver; not a delivery confirmation. |

Queued automatic notices appear only in detailed doctor output. Unrecognized
record kinds show `Unrecognized record; review diagnostic details.` in `u`,
with no record operations offered; the record is kept. Discarding a known
record leaves the conversation and input box unchanged. Opening a project
from `u` does not type or resend anything.

A message the target session says it did not take — Claude Code holds a prompt it rewrote until its user confirms it, and says so above the input box — is not queued again: once that user presses Enter on the copy in the composer, or clears it away, the box looks the same either way, so retrying would type the whole message a second time. The same applies when the submitted message's beginning stays in the input box without a hold notice. These messages are recorded as held, `ccm spool list` shows them, and you deal with them in the recipient's window. `ccm spool clear-held` then says so; it sends and withdraws nothing.

The state check is not the only gate. State detection cannot see a half-typed draft in the target's composer (an `❯` prompt holding text still reads IDLE), so immediately before typing, `ccm send` reads the composer line itself and — while a draft is present — queues the message like any other undeliverable state (`--now` refuses instead). Otherwise the message would merge into text you are still writing, and the Enter would submit the garbled mix. Claude Code's own next-prompt suggestion (drawn dim in the composer when a turn ends) is distinguished from a real draft via the capture's SGR attributes and does not block a send — it vanishes on the first keystroke, so there is nothing to protect.

### Flags

| Flag | Purpose |
|---|---|
| `--file <path>` | Read message from a file |
| `--stdin` (or bare `-`) | Read message from stdin |
| `--no-enter` | Send the text without the final Enter (useful for prefilling a prompt) |
| `--now` | Fail instead of queueing when the target cannot take the message now |
| `--force` | Allow sending to a BUSY target (queues into Claude's input buffer) |
| `--start` | Auto-launch Claude if the target is in SHELL state. The launch re-reads the window first: nothing is typed when a pane already hosts Claude (the message is then delivered to it) or when no pane can be verified as a shell prompt (the send is refused with the reason) |
| `-y`, `--yes` | Skip the interactive confirmation prompt |
| `--` | End of flag parsing (for messages that start with `-`) |

Confirmation is automatically skipped when stdin or stdout is not a TTY, so piped use (`echo "..." \| ccm send ...`) works without `-y`.

Targets can be specified by project name, `#<idx>`, or a bare window index.

### Delivery pane in split windows

The project state is aggregated across all panes of the window, but keystrokes must land in one specific pane. `ccm send` resolves the pane that actually hosts the claude process and types into it directly — even when a plain shell pane happens to be the active (focused) one. If several panes host claude (an Agent Teams split) and the active pane is not one of them, the send is refused as ambiguous: focus the pane that should receive the message, then retry. With `--start` on a SHELL window, Claude is launched in the active pane after verifying its foreground is really a shell (never into an editor or pager).

## State Detection

ccm uses a hybrid approach: Claude Code hooks (recommended) combined with process tree inspection as fallback.

### Claude Code Hooks (Recommended)

Install hooks for the best detection accuracy:

```bash
ccm setup-hooks
```

See [hook diagnostics](diagnostics.md#claude-code-hooks-recommended) for events, signal lifetimes, and settings checks.

Hook status is shown in the dashboard footer (Hooks: ON/OFF). `ccm status` shows Hooks: OFF when hooks are unavailable and retains diagnostic warnings; the normal Hooks: ON line is omitted. If hooks are already installed, `ccm setup-hooks` does not reinstall them; it only brings the timeout of ccm's own hooks in line with `CCM_HOOK_CMD_TIMEOUT`. If you reinstall ccm to a different path, `ccm setup-hooks` installs hooks from the new path and names the ones at the old path, which you delete by hand (see [hook diagnostics](diagnostics.md#claude-code-hooks-recommended)).

Run `ccm doctor` to see issues and next steps.

To remove: `ccm remove-hooks` (removes ccm's own hooks only)

### How each state is detected

See the [diagnostics reference](diagnostics.md#how-each-state-is-detected).

### Detection without hooks

Without hooks, ccm falls back to process tree inspection with prompt pattern matching. This means:
- Text generation (no tool use) appears as IDLE, not BUSY
- Completion detection relies on BUSY→IDLE transition heuristics

### Which pane answered last (unread mark)

With several agents in split panes, it stops being obvious which one replied last. Opt in with:

```bash
set -g @ccm-pane-labels on     # in ~/.tmux.conf, before ccm is loaded
```

ccm then marks a pane's border with `◆ new` when a reply completes in it while you are looking elsewhere, and removes the mark once you have been looking at that pane for a few seconds, or when you send it the next prompt. The few seconds are `@ccm-unread-linger` (default `5`): tmux reports focus to a pane the moment you switch to its window or bring the terminal back to the front, which is how you go and look, so a mark cleared on that event would be gone before you saw it. A pane you only pass through keeps its mark. "Looking at it" means a terminal that has focus is showing the pane as its active one — tmux reports focus only with `set -g focus-events on`. While ccm's dashboard popup is open nothing under it counts as looked at; a popup from another tool cannot be seen from here, so a reply that finishes under one goes unmarked. Focus held for that long is the only stand-in ccm has for reading.

The mark follows the completion notice: it appears once the reply has stayed finished for `CCM_COMPLETION_GRACE_SEC` (a tool boundary in a multi-step turn does not count), and not at all while background tasks are still running. It needs the hooks, so it covers Claude Code sessions; a sidekick CLI without hooks never gets one.

This turns on tmux's pane border line (`pane-border-status top`) and puts the mark in front of your `pane-border-format`. If your own configuration sets `pane-border-format` after ccm is loaded, it replaces ccm's; add the mark to your format yourself:

```
#{?@ccm_unread,#[fg=colour209]◆ new#[default] ,}
```

Turning the option off stops new marks at once, but what was set up while it was on — the border line, the format, the focus hook — stays until tmux restarts or you reset it.

### Completion tracking

When Claude Code finishes processing, ccm:
1. Records a completion timestamp (the project transitions to IDLE)
2. Shows `* <elapsed>` after the project name in the dashboard, status bar, and `ccm status` as a "recently completed" marker (asterisk green, time dim)
3. Sends a desktop notification (if configured)

The `* <elapsed>` marker clears when:
- 30 seconds elapse (auto-clear)
- You switch to the window (via dashboard, tree, or `ccm attach`)
- You send a new prompt (Claude goes BUSY, clearing the marker)

## Status Bar Modes

Configure with `set -g @ccm-status-line` in your `~/.tmux.conf`. See the [README Status Bar section](../README.md#status-bar) for configuration details and screenshots.

### Narrowing and placing the list

Two options change what modes 1 and 2 show and where mode 1 puts it. Both are
off by default and documented with examples in the
[README Status Bar section](../README.md#status-bar).

`@ccm-status-line-hide-shell on` drops SHELL projects, leaving only windows
that host a Claude session. On a machine with many registered projects most of
them are SHELL most of the time, and the one needing attention is easy to
miss. IDLE stays: the session is alive and waiting, and that is where the
`* elapsed` marker appears when a turn finishes.

Worth knowing: idle sessions auto-exit after `CCM_IDLE_EXIT_TIMEOUT`, so a
project you leave alone drops off the bar when its session ends, and returns
when you attach and Claude restarts. The dashboard and `ccm status` always
list every project.

`@ccm-status-line-position left` moves mode 1's entries to the far side of the
bar, highest priority first, filling the space the blanked window list leaves
empty. It writes nothing to `status-left` — the padding that moves them lives
in `status-right`. When the bar is too narrow it keeps the right-hand layout,
because sliding the entries under `status-left` would have tmux clip the
highest-priority one first.

### Mode 0 — Single icon

Appends one icon to your existing status-right. The icon shows the highest-priority state:

> ```
> 5: PERMIT ⚠   13:30
> ```

Priority order: `⚠` PERMIT (yellow) > `◉` BUSY (orange) > `≡` all idle (gray)

- Best for: users who want the most conservative integration with their existing tmux theme
- Trade-off: no per-project detail (use the dashboard for that)

### Mode 1 — Full (ccm-style window list)

Replaces the standard tmux window list with ccm-style colored entries. Your existing status-right is preserved.

> ```
> myapp:● | sideproject:◉ | docs:● | 21:30 12-25
> ```

- Best for: users who want colored project status in the main bar
- Trade-off: replaces the standard tmux window list

### Mode 2 — Dedicated line

Adds a second status bar line below the main bar, showing all projects including idle ones with git branch and port details.

> ```
> Main bar:  0:bash  1:my-project  2:api-server     21/03  07:30:00
> ccm line:  my-project:◉(main*) | another-project:●(dev) | api-server:⚠(main)[:8080]
> ```

| Icon | State | Color |
|------|-------|-------|
| `⚠` | PERMIT | Yellow |
| `◉` | BUSY | Orange |
| `●` | IDLE | Blue |
| `* <elapsed>` (after project name) | IDLE (recently completed) | Asterisk green, time dim |
| `■` | SHELL | Dark gray |

- The `* <elapsed>` marker appears for 30 seconds after completion, then clears (project remains `●` IDLE)
- Best for: users who want full visibility without losing their status-right
- Trade-off: uses one extra screen line (auto-expands to more if needed)

## Snapshots

To carry windows, splits, working directories and pane roles across logout:

1. Before logout, run `ccm prepare-logout`. If projects are PERMIT or BUSY, review their names and confirm with `y` and Enter to save.
2. After login, open tmux and run `ccm start _autosave`. Windows and panes return as inactive shells.
3. Open the windows you need with `ccm attach <name>` or the dashboard. When `@ccm-auto-start` is on, Claude resumes in the reserved primary pane, or an eligible shell if readable reservations contain no primary. Unreadable or conflicting roles prevent automatic launch.
4. Resume sidekicks manually using the restore output or `ccm roles`. For Codex, choose a conversation with `codex resume` in that cwd. ccm does not automatically select a particular conversation.

Running conversation state, approval waits and agent processes are not restored. Interrupted projects appear after restoring a sealed checkpoint; cached BUSY entries in ordinary autosaves do not. Do not layer tmux-resurrect restoration onto the same environment.

### Save

```bash
ccm snapshot save my-workspace
```

Saving supports one managed session. v2 retains windows, horizontal/vertical splits, cwd, roles, ignore intent, selected panes and zoom.

### Restore

```bash
ccm start my-workspace
ccm roles                    # Current pane's role and guidance
ccm roles @7                 # All panes in a window
ccm roles %12 --clear        # Release a repurposed pane's role reservation
ccm unignore                 # Clear current pane's ignore intent and live marker
```

Smaller screens scale the layout proportionally; if the panes cannot fit, that window is held back and the others are restored. When roles are readable and there is no primary reservation, opening a window chooses the active eligible shell, or the only eligible shell if the active pane is not eligible. Sidekick, reserved-ignore and explicitly cleared panes are excluded. Multiple eligible shells with no eligible active shell are left for manual selection. A unique primary reservation is never replaced by another pane if its pane cannot be used. Unreadable, malformed or conflicting reservations block automatic launch and exit; inspect `ccm roles`. Clearing a reservation records a manual-only role, retained by snapshots and restores; automatic launch and exit leave that pane alone. `roles --clear` does not change live ignore markers; use `unignore` to release those too.

A successfully read empty role means **no reservation**. This can be a newly split pane or a pane whose reservation an older version removed; `ccm roles` shows `reserved`, `no reservation`, or `invalid role` without guessing its origin. An unreserved pane does not block the window. While a known external agent such as Codex is running there, it is excluded from launch, exit and idle-exit candidates. Without a primary, an unreserved shell follows normal shell selection and an unreserved Claude follows normal exit rules. With a unique primary, only that primary can be launched or exited. Live ignore markers still exclude panes. Observation does not write a reservation into an unreserved pane: after the external agent stops, its shell can be eligible again. Saving a snapshot while the external agent is running records it as a sidekick.

Existing registered windows with matching names and base cwd are retained without changing splits or roles. Overlapping unregistered window names/cwd and missing saved directories stop restoration before creation. Inspect and explicitly register or resolve the overlap, then retry. An unregistered control window in a saved project's cwd also counts as a conflict.

Each window is published (named, registered and released for Claude) as soon as its own checks pass, so a problem with one project never keeps the others from starting. A window that cannot be restored now (its directory is missing, it conflicts with another window, its shell startup does not settle, or it was moved, linked or changed outside the restore) is held back and named with the reason; the rest are restored. While any window is held back, the restore is incomplete: the checkpoint stays protected and autosave paused, so the windows not yet restored are not dropped from it. The dashboard shows `Restore incomplete: N window(s) need attention`. Resolve the cause and rerun the same `ccm start <name>` (or Menu → Continue restore) to retry only the windows held back; windows already published are yours and are not changed again. A problem that is not one window's (the session is gone, tmux or the process list cannot be read, the progress record cannot be saved) still stops the whole restore, and windows published before it stay as they are.

Restoration starts your login shell: tmux's `default-shell`, then `$SHELL`, then `/bin/sh`, accepting only executable absolute paths with a recognized shell name. It bypasses `default-command`. Shell startup files run normally. At each layout step ccm waits up to 10 seconds for a shell foreground to settle. If rc starts an agent or another persistent foreground process, restoration pauses without killing or restarting it; inspect the pane and retry after it returns to the shell. Already completed panes are not restarted on retry.

### Auto-save

`_autosave` is checked every 2 minutes and after project operations. Identical or empty inventories, protected checkpoints and incomplete restorations leave it intact. Restored shells retain their roles and ignore intent in the next save.

### Protect a checkpoint

```bash
ccm prepare-logout
ccm prepare-logout -y          # --yes: skip PERMIT/BUSY confirmation
ccm doctor --verbose
ccm prepare-logout --cancel    # Unseal without deleting the checkpoint
```

The prompt is `Save anyway? [y/N]`. `y` and Enter save; N, Enter alone, Esc, EOF and Ctrl-C exit nonzero without saving. Non-interactive use with PERMIT/BUSY requires `-y`. Changes to configuration or states during confirmation also refuse the save and ask for a retry.

Windows and agents keep running after protection. Run prepare again after configuration changes. Protection survives logout and is released automatically only after a complete restore. Prepare/cancel refuse while restoration is incomplete; finish the same snapshot restore first. `ccm stop --all` closes windows even if saving fails, so confirm a successful prepare first when you need a confirmed save.

#### Auto-restore on tmux start

```tmux
set -g @ccm-auto-restore "on"    # default: off
```

TPM startup loads `_autosave`, skipping when managed windows already exist. The location follows `CCM_SNAPSHOT_DIR`, otherwise under `CCM_DATA_DIR`. The run's output, including any failure reason, is kept in `state/auto-restore.log` under `CCM_DATA_DIR`.

While a restore runs, the tmux status area shows it too, without opening the dashboard: a `⟳ ccm restore` badge with `12/45: alpha · about 50s left` for each window, then `✔ ccm restore` with `restored 45/45 window(s) · …`, `⚠ ccm restore` with `restored 43/45 · 2 need attention · dashboard menu: Continue restore` when windows were held back, or `✖ ccm restore` with `stopped at 12/45 · dashboard menu: Continue restore · <reason>`. Missing windows are all created first, so their shells start together; meanwhile the notice reads `0/45: creating alpha`. The badge is blue, green or red in muted tones, followed by a dark band so the notice stands apart from the status bar. These are temporary messages for every client attached to the restored session, renewed per window; panes keep updating while they show, and a key press dismisses one early. They need tmux 3.6 or later (`display-message -C`); older tmux shows no notice, and nothing is shown while no client is attached to the restored session. The dashboard shows the same progress, for example `Restoring 12/45: alpha · about 50s left`; the estimate appears after three windows have been rebuilt. If it stops, the dashboard shows `Restore stopped:` with the reason; retry with Menu → Continue restore or the same snapshot. Starting a restore while another is running waits for it, prints its progress and then its result, and does not restore again. When that run's result cannot be confirmed, the waiting restore continues itself; windows already restored are kept.

### Manage snapshots

```bash
ccm snapshot list
ccm snapshot delete old
```

The fixed `_autosave.prev` backup is absent from the list. To use it, copy it in the snapshot directory to an unused name such as `recovery.json`, set its mode to 0600, then run `ccm start recovery`. Inspect an existing incomplete restore before switching checkpoints. See [format, protection, backup and conflict details](diagnostics.md#snapshot-checkpoints).

## Tips

### Register existing windows

If you already have a tmux window running Claude Code, you can register it without restarting:

1. Open the dashboard (`prefix + Tab`)
2. Press `g` (register)
3. Select the unregistered window
4. Give it a name

Or from the command line:

```bash
ccm register 3 my-project    # register window index 3
```

### Capture and copy

Preview a project's screen without switching to it:

```bash
ccm capture my-project              # print to terminal
ccm capture --copy my-project       # copy to clipboard
```

Or from the dashboard: press `p` to preview, then `c` to copy. In a split window the dashboard preview (and the live preview panel) shows the pane running Claude, not whichever pane happens to be focused, and never a `CCM_IGNORE`'d sidekick — so you always preview the session ccm is tracking.

**Split windows are captured pane by pane.** `ccm capture` labels each pane with its id and what is running in it, so nothing is hidden behind whichever pane happens to be focused:

```
=== ccm capture: my-project ===
--- pane %1 [claude] (active) ---
...
--- pane %7 [other-agent] ---
...
=== end ===
```

Single-pane windows print exactly as before, with no headers. Panes hidden with `CCM_IGNORE` **are** included and marked `(ignored)` — hiding a pane means ccm does not track or type into it, not that it disappears from a capture you explicitly asked for.

This also makes the sidekick pane readable from Claude itself: running `ccm capture <this project>` from one pane shows what the other agent in the same window is doing, which is useful when you run a second agent CLI alongside Claude.

> [!IMPORTANT]
> A project's **state** describes its Claude pane — not a second agent sharing the window. ccm tracks Claude sessions; a pane running some other agent CLI has no Claude in it and contributes nothing to the state.
>
> So a Claude session must not read its own project's state to decide whether the agent beside it is free. While it is the one asking, the state it reads is its own, and a session running a command is BUSY by definition — which looks like "the other agent is busy" when nothing of the sort is true. Judge a sidekick pane only from its captured content.
>
> For the same reason `ccm send <this project>` refuses outright: delivery resolves to the Claude pane, which is the caller itself.

### Git integration

ccm shows the git branch and dirty status for each project:

- `main` — clean working tree
- `main*` — uncommitted changes (staged or unstaged)

This information appears in the dashboard, tree view, and `ccm status`.

### Port detection

ccm detects TCP ports that processes in your project directory are listening on. This is useful for web development projects:

```
 my-app:◉ [:3000]    api:● [:8080,8443]
```

Port detection results are cached for 30 seconds to minimize overhead.

## Troubleshooting

Start with `ccm doctor` for issues and next steps. Use the [diagnostics reference](diagnostics.md) for a detailed investigation.

### Dashboard won't open

If the dashboard appears and immediately closes:

```bash
# Remove stale PID file
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/dashboard.pid"
```

### Status bar shows old data

```bash
# Clear all caches
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/status-cache"
tmux source-file ~/.tmux/plugins/tmux-ccm/ccm.tmux.conf

# The pre-ccm status-right is kept in a tmux option (not a file):
tmux show-option -gqv @ccm-orig-status-right   # inspect the saved original
```

### Wrong session context

If projects appear in the wrong session, the popup session file may be stale:

```bash
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/popup-session"
```

### State stuck on BUSY

If a project shows BUSY but Claude Code is actually idle, it may have orphaned child processes:

```bash
ccm capture my-project    # check what's on screen
```

The state will correct itself on the next 2-second refresh cycle once the child processes exit.

If the state persists with a `(Nm)` suffix (e.g. `BUSY (5m)`), ccm has detected its own signals are stale but cannot prove the project is actually idle. This usually indicates an upstream double silent fail (Stop hook missed AND JSONL didn't record completion). As a last resort:

```bash
ccm reset my-project      # clears hook signals, event log, and cached state options
```

`ccm reset` does not touch the conversation, snapshots, or the running `claude` process — it only wipes the ephemeral runtime artefacts that detection reads. The next scan re-resolves state from scratch. For ordinary "Claude is hung" situations, `/exit` inside the pane is still the right answer.

### Settings that stop hooks

Start with `ccm doctor` and Claude Code's `/status`; ask your administrator to correct managed settings.

See the [diagnostics reference](diagnostics.md#settings-that-stop-hooks).

### Detection has gone quiet (hook-silence canary)

Restarting the affected Claude session restores hooks that have stopped firing.

See the [diagnostics reference](diagnostics.md#detection-has-gone-quiet-hook-silence-canary).

### Every project frozen at the same state

Start by checking the records with `ccm errors`.

See the [diagnostics reference](diagnostics.md#every-project-frozen-at-the-same-state).

## Using with Agent Teams

ccm works alongside Claude Code's [Agent Teams](https://code.claude.com/docs/en/agent-teams). The two operate at different levels and complement each other:

- **ccm** manages projects as tmux **windows** (one Claude Code per project)
- **Agent Teams** runs parallel agents as tmux **panes** within a single window

### How they work together

When you run Agent Teams inside a ccm-managed project window, ccm's state detection automatically aggregates across all panes. For example:

- If any teammate pane is in PERMIT state → the project shows `⚠ PERMIT` in ccm
- If any teammate is BUSY → the project shows `◉ BUSY`
- When all teammates are idle → the project shows `● IDLE`

This means ccm's dashboard and status bar give you visibility into Agent Teams activity without any extra configuration. Multi-pane windows additionally carry a `[N]` marker (brackets dim, digit cyan) immediately after the project name in every renderer, so you can spot which projects have parallel teammates at a glance.

**Sliver protection.** Panes shorter than `SLIVER_HEIGHT_THRESHOLD` (4 rows by default — see Environment Variables below) are excluded from state aggregation. Tiny pseudo-panes — typically a leftover 1-row strip from an earlier split — cannot render Claude's `❯` prompt, so capture-pane–based detection cannot tell them apart from a busy pane and they false-read BUSY. Excluding them prevents an invisible sliver from infecting the whole window's reported state. If you have a legitimate small Agent Teams pane and want it to count, raise the threshold via `CCM_SLIVER_HEIGHT_THRESHOLD`.

**Auto-focus on attach.** When you attach to a window via ccm (dashboard or `ccm attach`), if any teammate pane is waiting on a permission modal (`⚠ PERMIT`) and the active pane is not, ccm automatically switches focus to the PERMIT pane. Saves a manual `prefix + arrow` after every attach to a project that needs your input. PERMIT only — BUSY teammates are interesting to monitor but do not require user input, so focus is not stolen.

### No conflicts

| Feature | Agent Teams | ccm | Conflict |
|---------|------------|-----|----------|
| Keyboard shortcuts | `Shift+↓`, `Ctrl+T` (inside Claude Code) | `prefix + Tab` (tmux level; T/C are opt-in) | None |
| Pane management | Splits panes within window | Manages windows | None |
| Window naming | Does not rename windows | Sets icon + name | None |

### Typical workflow

1. Use `ccm add` to register multiple projects
2. Switch to a project with the dashboard (`prefix + Tab`)
3. Inside that project, tell Claude Code to create an Agent Team
4. Agent Teams splits the window into panes for each teammate
5. ccm's dashboard shows the aggregated state of all teammates
6. Switch to another project with `prefix + Tab` while the team works

## Running a second model as a sidekick (CCM_IGNORE)

You can run a second Claude Code session in a split pane of the same window — a sidekick to consult next to your main session. By default ccm would aggregate both panes into one window state and couldn't tell which session `ccm send` should reach, so `CCM_IGNORE` makes the sidekick invisible to ccm while the main session stays cleanly tracked:

```bash
# main pane: your primary session, tracked by ccm as usual
claude --continue

# split pane (prefix %): a sidekick session, hidden from ccm
CCM_IGNORE=1 claude
```

An ignored session is dropped from window-state aggregation, session tracking, `ccm send` delivery, and idle auto-exit, and its hooks fire no signals, events, or desktop notifications. The window's state, badges, and `ccm send` routing therefore reflect only your main session. A dim `⊘` on the dashboard / `ccm status` row reminds you a hidden sidekick is running. (The sidekick can be a different model, if you point `claude` at another Anthropic-compatible endpoint via `ANTHROPIC_BASE_URL` — ccm treats it the same either way.)

You can also toggle it on an already-running session:

```bash
ccm ignore              # hide the pane you're in
ccm ignore <project>    # hide every claude pane in a project's window
ccm unignore            # restore the current pane
ccm unignore <project>  # restore a project
```

or press `i` on a project in the dashboard.

**If you forget.** Nothing goes wrong silently. A window with two visible Claude sessions aggregates both, and `ccm send` refuses to guess between them rather than typing into the wrong conversation — the refusal points back at `CCM_IGNORE` and the `i` key. `ccm doctor --verbose` also lists such windows under *multi-claude windows*. Neither is phrased as a warning, because the same window shape is a normal Agent Teams split, where both sessions must stay visible so that either one's PERMIT reaches you. They name both readings and leave the choice where it belongs.

**Make the ignore visible on the pane itself** (optional): ccm sets a `⊘ ccm-ignored` pane title, which tmux shows only if you enable pane borders. Opt in with `tmux set -g @ccm-ignore-pane-border on` — ccm then turns on `pane-border-status` when a session is ignored (a global tmux change, so it happens only with this explicit opt-in). Without it, the dashboard `⊘` remains the cue.

**Caveat — same directory.** Running two Claude Code sessions in the same working directory hits an upstream bug (anthropics/claude-code#48112) where one session's background-task notifications can leak into the other's session log. `CCM_IGNORE` stops ccm from *tracking* the sidekick, but it cannot stop that leak from polluting your main session's log if the sidekick runs background tasks concurrently. Keep the sidekick for interactive consultation (avoid concurrent `run_in_background` work and simultaneous edits to the same files), or give each model its own directory via a git worktree if you need two co-equal agents.

## Relaying with a second agent CLI

![How ccm sees a window with a sidekick: the Claude Code pane is tracked and its state becomes the window's, while a second agent CLI in a split pane gets only a presence badge. The two exchange messages directly — the sidekick reports back with ccm send, and Claude messages it with ccm sidekick-send after checking it with ccm capture.](../assets/sidekick-model.svg)

You can run an agent CLI other than Claude Code in a split pane of a project window and let the two agents exchange messages without a human relaying text. ccm stays Claude-centric: it only shows a dim `▸<name>` presence badge when a known external agent CLI runs in a pane (display only — it does not track that agent's state), and `ccm capture` shows every pane so either side can read the other.

The conventions that make the relay work:

- **Other agent → Claude**: the other agent runs `ccm send <project> "<message>"`. State gating applies (never into PERMIT), and the message lands as Claude's next turn — no one needs to watch.
- **Claude → the sidekick in its own window**: run `ccm sidekick-send "<message>"` from a pane of the project window. ccm cannot state-gate a non-Claude pane, so check the peer is ready with `ccm capture <its own project>` first. The command then does what the hand procedure used to leave to your attention: it finds the sidekick pane from tmux metadata (a known external-agent CLI whose working directory belongs to this project), refuses when there is no sidekick or more than one, types the body literally (`send-keys -l --`), waits 0.3 s before sending `Enter` separately, and captures the pane afterwards to confirm a fragment of the message actually landed — exiting non-zero when it cannot confirm.

  The pieces it packages still matter if you ever type into a foreign TUI by hand. `-l` sends the text literally; without it tmux resolves arguments as key *names*, so a word like `Space` or `Enter` inside the message silently becomes that keystroke. Newline keys differ between CLIs (Claude uses `M-Enter`), so multi-line bodies are typed line by line with the peer's newline key between them — `ccm sidekick-send` uses `M-Enter`.

  **The pause before `Enter` is load-bearing, and it is the failure you are most likely to hit by hand.** Chain the body and the `Enter` with `&&` and the peer's TUI can still be digesting the inserted text when `Enter` arrives, and take it as a *newline* instead of a submit. The body then sits in the composer, unsent, looking exactly like a message that went through. Measured against Kimi K3: no gap fails every time, 0.3 s and 1 s both submit. Claude Code's own composer tolerates a zero gap — which is why `ccm send` needs no pause and why this bites only when the peer is something else.

  **Check the conversation and input box.** `sidekick-send` only checks whether a message fragment is visible; it cannot confirm acceptance. Short messages report delivery unconfirmed. `--no-enter` reports that submit Enter was not sent (also for `ccm send`); other sidekick sends report that Enter was attempted. When the report says delivery is unconfirmed, or before relying on the reply, check with `ccm capture`: do not resend if received, and resolve leftover text before deciding whether to resend. An empty input box alone does not prove receipt.
  **It does not read the peer's composer, so check it yourself.** `ccm send`
  refuses to type into a Claude pane whose composer already holds a half-typed
  draft, because the committing Enter would submit the mixture. There is no
  equivalent here: locating another vendor's input box means matching that
  vendor's screen, which is the coupling this lane exists without. Run `ccm
  capture <this project>` first and look at the peer's input box — the same
  glance the procedure always asked for.

- **A sidekick answers to its own window.** `ccm send` reaches a project's *Claude* — never a sidekick, which is dropped from delivery precisely so it cannot intercept one. `ccm sidekick-send` holds the same boundary from the other side: it only ever targets the sidekick sharing the caller's window, and it verifies the pane's directory belongs to the project before typing. When you want something from another project's sidekick, ask that project's Claude to relay it — that session knows whether its peer is idle and which keys its TUI takes, and it stays aware of what its own sidekick is doing. Reach in from elsewhere (raw `tmux send-keys` across windows) and two senders can land in one composer, interleaved into a single garbled prompt.
- **Report, don't poll**: neither side can observe the other's progress. When you finish a request, report back with `ccm send` — the reply arrives as a new turn on its own.
- **Long results**: write them to a file and send a one-line pointer; this also sidesteps the differing newline keys.

`ccm setup-claude-md` writes these conventions into `~/.claude/CLAUDE.md` so every Claude session knows them; putting the equivalent in the other CLI's own instructions file completes the loop.

**The sidekick can be another Claude Code.** Nothing above depends on the peer being a different product — a second `claude` in the split pane relays exactly the same way, with one exception: `ccm sidekick-send` only targets known *non-Claude* CLIs (a second `claude` is deliberately absent from that set — it is the tracked session's own binary), so reaching a Claude sidekick still means `ccm capture` to check, then `tmux send-keys` by hand. What changes with a Claude sidekick is how ccm sees it: a second Claude Code is a session ccm *would* track, so you hide it deliberately with `CCM_IGNORE` (see the section above) and it carries `⊘` rather than the `▸` a non-Claude CLI gets. In practice it is the easier pairing — the submit and newline keys match, and the peer already knows the conventions from your `CLAUDE.md`.

## Sidekick attention: knowing when it needs you

A sidekick's approval dialog is easy to miss: ccm deliberately reads no state from a non-Claude pane, so a Kimi blocked on *"Run this command?"* looks exactly like a Kimi working — a dim `▸kimi` either way. Rather than parse each product's screen (formats differ per CLI and change without notice), ccm lets the sidekick *report itself*, through the hook system the sidekick's own vendor ships:

```bash
ccm setup-sidekick-hooks kimi     # writes [[hooks]] entries into ~/.kimi-code/config.toml
ccm remove-sidekick-hooks kimi    # removes them (backup kept either way)
```

Takes effect in **new** Kimi sessions only — Kimi loads its config at session start, so restart the sidekick pane's `kimi` once after installing.

From then on, when the sidekick hits a permission prompt its hook drops an attention marker keyed to its tmux pane, and ccm reacts on every surface at once: the `▸kimi` badge turns PERMIT-yellow on the dashboard, `ccm status` and the status bar, and a desktop notification fires with the tool it is asking about. Answer approval dialogs yourself in the original sidekick pane; do not ask Claude to approve on your behalf. When you answer the dialog, the resolution hook clears the wait and the badge dims again. The window's own state never changes — PERMIT still means *Claude* needs you; a yellow `▸` means the *sidekick* does.

Toggle it with `w` in the dashboard, or persistently with `tmux set -g @ccm-sidekick-attention off` (markers are still written when off — only ccm's display and notification go quiet, so other local consumers of the marker directory keep working).

**A second Claude Code needs no installer at all.** An ignored Claude (`CCM_IGNORE=1 claude`, the documented Claude-as-sidekick arrangement) already runs ccm's hooks — they were just exiting silently. Its permission events now route to the same attention channel: the `⊘` marker turns PERMIT-yellow while the hidden Claude waits, and dims again when the dialog is answered. The ignore contract is unchanged — the session still contributes nothing to window state, `ccm send` delivery, or auto-exit. One honest caveat: Claude Code has no resolution event (its `PermissionResult` gap), so the wait is closed by the *next* hook activity — an approved tool fires `PostToolUse`, a denial's feedback round ends in `Stop` — which covers everything except a dialog dismissed with Esc followed by pure silence, where the marker ages out on its TTL instead.

> [!NOTE]
> Support for non-Claude agents is **exploratory**, and `ccm setup-sidekick-hooks` is deliberately absent from the CLI table for now. Each vendor's hook contract is young and still moving — measuring three of them turned up an undocumented event type, a platform-suffixed binary name, and a product whose hooks load but never fire. Expect this section to change; the Claude-sidekick path above depends on none of it.

Installable for **Kimi Code** and **Grok Build**, both verified against a running pane, and for **Codex** (described below). Kimi is the precise one — its hook set has `PermissionRequest` *and* `PermissionResult`, so waits open and close exactly. Grok has neither: its permission wait arrives as `Notification` with `notificationType: "permission_prompt"`, carries no tool details (the summary falls back to Grok's own "Tool permission requested"), and closes on the next activity event.

**Antigravity CLI** (Gemini CLI's successor) loads hooks without ever firing them — measured against 1.1.10, where a real approval dialog produced nothing from any of its six events. `ccm setup-sidekick-hooks` refuses an unsupported agent by name and says which case applies.

**Codex attention and completion notifications.** Install the adapter separately
from enabling Claude notifications:

```bash
ccm setup-sidekick-hooks codex
# Review and trust the installed hooks in Codex /hooks.
# Optional per-window override; prefer the persistent global setting below.
tmux set-option -w -t <window> @ccm-sidekick-notify on
# Optional settings (defaults: 20 deliveries/hour, excerpt on):
tmux set-option -w -t <window> @ccm-sidekick-notify-limit 20
tmux set-option -w -t <window> @ccm-sidekick-notify-excerpt off
# Stop completion notifications:
tmux set-option -w -t <window> @ccm-sidekick-notify off
ccm remove-sidekick-hooks codex
```

For all sidekick windows, put `set -g @ccm-sidekick-notify on` in `~/.tmux.conf` (recommended), then reload the configuration. A command entered only into the running server is not persistent. This enables delivery when matching Codex hooks are installed and trusted; it does not install or trust hooks.

All three notification options resolve in this order: explicit window value (`set -w`), global window value (`set -gw`), global session value (`set -g`), built-in default. A window's `off` overrides global `on`. If both global tables are set, `-gw` wins; prefer using only `-g` in the configuration. Empty values act as unset. Use `tmux set -wu -t <window> @ccm-sidekick-notify` to remove a window override. A failed query stops resolution and disables notification/excerpt delivery for that read.

`ccm doctor --verbose` shows the effective notification, hourly limit and excerpt values with their sources (`window`, `global (-gw)`, `global (-g)`, `default`, or `unavailable` on query failure). Enabled windows without observed Codex hooks still produce a warning. Hook creation, delivery, diagnostics and dashboard notice previews share the same resolver.

Snapshots v1/v2 are unchanged: they do not save notification overrides. Global configuration applies to newly restored windows. A window-only override is lost when its window is recreated; reapply exceptions before starting sidekicks. If notifications or excerpts must stay disabled after restart, set the corresponding global option to `off` in `~/.tmux.conf`.


The installer merges only this installation's hook entries into Codex's
`hooks.json` (under `CODEX_HOME`, or `~/.codex`), keeps a `.ccm-bak`, and preserves
other hooks. Invalid JSON and installation paths containing spaces are refused.
It does not edit trust records or `config.toml`. New or changed hooks need the
user's trust review; installation alone does not establish that they run.
Removal disables loaded ccm hooks and cancels pending notices; restart/review
Codex as appropriate. Reinstalling restores the adapter; window preferences
remain as configured. Refresh `ccm setup-claude-md` for the recipient guidance.

Only the hook payload's cwd identifies the project: exactly one registered
window must contain it, and that window must host exactly one Codex pane.
Linked copies of a window count once; nested or duplicate registrations are
ambiguous. Ignored Codex panes still count as physical sidekicks. The first
session observed for that live Codex process is bound automatically. An event
from a new session, or a changed process identity, rebinds it, whether or not a
SessionStart arrived; late events from a session the process already left are
ignored. The internal `@ccm-sidekick-binding` window option records this
association; do not edit it. Another session using the same cwd (for example
Codex running outside tmux) is indistinguishable and takes over the binding
when it sends events. This is a heuristic, so read the named pane before acting.
SessionStart may not arrive until the first turn. The hook's own pane variable
and process ancestry are not used for routing.

Permission requests use the existing attention marker, badge and desktop
notification, controlled by `@ccm-sidekick-attention`. Desktop notices run on
the next full refresh and omit waits already resolved. They do **not** send a
message to Claude. Only a uniquely matched tool completion or a matching turn
interrupt/end clears the wait; an unrelated tool does not. Neither ccm nor
Claude answers approval dialogs, returns hook approval decisions, changes
permissions, or bypasses hook trust. The user handles the original approval UI.

With completion notifications enabled, Stop queues a short automatic notice
for the window's sole non-ignored Claude pane. It waits for IDLE, an empty
composer and a readable capture; ordinary queued messages take priority.
There is no auto-launch or forced delivery. Unless excerpts are off, each event
quotes the final message's first line, limited to 400 characters after redacting
the full message. A cut line is marked as truncated; read the source pane for the
full text and location. Sanitization removes
terminal/control/bidirectional formatting; it cannot guarantee removal of all
secrets or malicious instructions. Excerpts are data, not authorization.
Notifications do not suggest a `ccm send` reply. Do not acknowledge merely to
acknowledge or automatically delegate another task.

Unexpired pending completions for the same session/binding retain their event IDs
and first lines in order when combined. Each notice holds up to 8 events and
4 KiB of UTF-8 text, including JSON quoting and fixed instructions; overflow stays
in separate pending notices. Only combined notices show a count. They use the
normal spool TTL (60 minutes by default, `CCM_SPOOL_TTL_SEC`); a combined notice
keeps the earliest expiry, so later events never extend it. Expired notices remain visible. The rolling hourly limit
is per window; excess notices are recorded rather than delivered, and capacity
returns as earlier attempts age out. There is no minimum interval, permanent
pause or lifetime quota. Each delivered notice can consume a Claude turn.
An explicit `ccm send` does not suppress the automatic completion notice.

A changed source/recipient, opt-out or session end cancels pending delivery.
If delivery might have begun, ccm records an uncertain result and never retries
automatically; a crash just before typing can therefore lose a notice. Held
input is also not resent. Dashboard `u` and `ccm doctor` expose expired,
rate-limited, cancelled, held and uncertain notices. Read or discard them there;
automatic notices cannot be resent. `ccm doctor --verbose` also shows installation,
reception, per-window preferences and binding. Reception is not proof of current
hook trust. Evidence and duplicate IDs are retained for seven days.

Enabling notifications sends excerpts to Claude’s conversation history and outside your machine. Redaction is best-effort. Disable excerpts with `tmux set -w @ccm-sidekick-notify-excerpt off`; Claude can still read the source pane, and that content also enters its conversation history and is sent outside your machine.

This completion path currently supports Codex only. Kimi, Grok and ignored
Claude sidekicks retain their existing attention behavior. Approval-wait and
resolution messages to Claude, and `codex queue` transport, are not included.


Grok Build gets its own hook file (`~/.grok/hooks/ccm-sidekick-attention.json`) instead of an edit to your config, so removing it is an unlink and nothing of yours is ever merged with.

<a id="sidekick-completion-v1"></a>
### Completion reporting (`sidekick-completion v1`)

When assigning work to a sidekick, the requester specifies the completion route
(automatic notification or explicit send) and where to leave the result.
For automatic notification, do not add a `ccm send` just to announce completion:
start the final message with one line containing the request number, completion,
and result location. For explicit send, report to the specified recipient as
usual, and keep the result location in the final message. If the instructions
are unclear, ask the requester; do not guess that reporting can be omitted.
Approval dialogs are always handled by the user in the original UI.

Combined notifications preserve each event's first line, including earlier result
locations; overflow waits in separate notices. Existing records that already lost
an excerpt cannot recover it. Reporting can still miss a location when hooks do
not run, a notice expires or cannot be delivered, or redaction or the 400-character
line limit removes it. Truncated locations require checking the source pane;
when excerpts are off, locations are not included. Specify
explicit send when notifications are off, the CLI is unsupported, or the route
is uncertain. ccm does not track outstanding completion reports. Check the
sidekick's result when a report is missing; expired, limited, held or uncertain
notices must not trigger an automatic resend through another route.

## Using with agent view (background sessions)

Claude Code 2.1.139 introduced an [agent view](https://claude.com/blog/agent-view-in-claude-code): `claude agents` (TUI), `claude --bg <prompt>` (background dispatch), and `claude attach <short>` (foreground attach). All three run sessions as workers under a per-user supervisor daemon, completely outside tmux. ccm reads the daemon's state and surfaces those sessions in a read-only dashboard section so a single view shows both ccm-managed project windows and out-of-tmux background sessions.

### Enabling the section

Off by default — agent-view non-users see no clutter. There are three ways to make it visible:

- Press `b` inside the dashboard — toggles for the current popup only, no config persistence.
- Set `@ccm-bg-section "always"` in `~/.tmux.conf` — keeps it visible across opens.
- Toggle the `Background sessions: …` row in the dashboard menu (`m`) — writes the same option back to `~/.tmux.conf`.

The section appears below the project list and lists each active worker with its short ID, normalised state (`✽ WORKING` / `✻ NEEDS` / `● IDLE` / `✓ DONE` / `✕ FAILED` / `■ STOPPED`), human-readable name, age, and working directory. A blocked session — the CLI's word for one whose next step belongs to you: a reply or an approval it waits for, or a condition to act on or wait out (a login, a usage limit, a rate limit) — reads `✻ NEEDS`, and what it waits for is shown in place of the directory. A session ended with `claude stop` reads `■ STOPPED`: neither finished nor failed.

### Attaching from the dashboard

Navigate to a bg row with `↑/↓` (selection moves seamlessly between projects and bg) and press `Enter`. ccm opens a new tmux window in the current session and runs `claude attach <short>` into it. The window's working directory matches the bg session when available, and its name is `bg-<short>` so it's easy to find with `prefix + w` (choose-tree). The window is also tagged with the session's short id (`@ccm_bg_short`), so pressing `Enter` on the same row again switches to that window instead of opening another one — every extra `claude attach` is another client drawing the same conversation, and enough of them garble it. The tag is what is matched, not the name, and only while the window still hosts claude; once the attach has ended, the next `Enter` opens a fresh window. If two such windows are live, or ccm cannot tell (the window list, the process table, or a window's panes could not be read), no window is opened and the message says why. Only the current tmux session is searched.

The new window is **not** registered as a ccm project — it has no `@ccm_project` / `@ccm_dir` tags, so ccm's `auto_start_claude` never races your attach with a `claude --continue` injection. This is the structural workaround for the attach/auto-start conflict; without it, attaching to a bg session from inside a ccm window would deliver `claude attach <short>` as a user message to the already-running `claude --continue` instead of as a shell command. Close the window with `prefix + &` after you detach from claude.

### Lifecycle stays with `claude`

ccm only **observes** the daemon — it never writes to `~/.claude/daemon/` or sends signals. Dispatch and termination remain the `claude` CLI's responsibility:

```bash
claude agents                 # interactive TUI
claude --bg "<prompt>"        # fire-and-forget background job
claude attach <short>         # foreground attach to an existing session
claude stop <short>           # terminate a session
```

Outside the dashboard, `ccm bg list` prints the same data as a coloured table for shell use.

### When `claude --continue` starts fresh

Sending a session to the background with `/bg` leaves a hand-off record at the end of its transcript. `claude --continue` walks a directory's transcripts newest first, and when the newest one hands off to a session the CLI still counts as live, it resumes nothing: a notice names the background session and a fresh session starts. The CLI can still count a background worker as live after its task is `done` so the launch command ccm types on attach can produce an empty session for a project whose conversation is intact on disk. (`claude --resume`'s picker hides the handed-off transcript too; `claude --resume <full session id>` still opens it.)

ccm checks three things, cheapest first: the daemon's roster must list a worker for the project (necessary, not sufficient — the roster outlives the daemon, so after a crash or reboot every worker is still listed with a dead pid), the newest transcript's tail must hand off to that worker's session, and then the CLI's own session registry (`~/.claude/sessions/<pid>.json`, one record per running process — the list `claude --continue` consults) must hold a live, non-interactive record for that session — live on the CLI's own terms: the process exists and its start time still matches the record's `procStart`. Where the CLI would not commit to a set (a record it cannot read, or a live record missing its kind or session id), `claude --continue` walks past the hand-off, and so the notice is withheld; it is also withheld when ccm cannot verify what the CLI verifies (no readable start time, a record from another pid domain), and when `CLAUDE_CONFIG_DIR` moves the CLI's home away from `~/.claude`, where ccm reads transcripts and registry. The notice appears wherever the launch is about to happen or has just happened:

- a `⚠` line on the dashboard and in `ccm status`, naming the project, the background session, its state, and the exits — for projects in SHELL state only, since a window already running Claude (often the background session itself, attached) has no launch coming;
- a `bg hand-off` row in `ccm doctor`;
- on the tmux message line right after `ccm attach` or a dashboard attach types the launch command, and in the output of `ccm attach` and `ccm send --start`.

The exits are the CLI's own: `claude attach <short>` opens the background conversation (it holds the work done since the hand-off), `claude stop <short>` releases the hand-off so `--continue` / `--resume` move on, `claude rm <short>` removes a finished session from the roster. This notice never runs any of them for you — the check is read-only (the dashboard's `Enter` on a bg row, which types `claude attach`, is a separate, explicit action). A newer interactive session in the directory clears the notice on the next refresh.

### Data sources

See the [diagnostics reference](diagnostics.md#data-sources).

## Environment Variables

See the [diagnostics reference](diagnostics.md#environment-variables).

### Detection timing

See the [diagnostics reference](diagnostics.md#detection-timing).

### Runtime directories

See the [diagnostics reference](diagnostics.md#runtime-directories).

### Canary thresholds

See the [diagnostics reference](diagnostics.md#canary-thresholds).

### Debug tracing

See the [diagnostics reference](diagnostics.md#debug-tracing).

### Cache TTLs

See the [diagnostics reference](diagnostics.md#cache-ttls).

### Display and observability

See the [diagnostics reference](diagnostics.md#display-and-observability).

### Tuning examples

See the [diagnostics reference](diagnostics.md#tuning-examples).

### Interactions with Claude Code's own environment variables

See the [diagnostics reference](diagnostics.md#interactions-with-claude-codes-own-environment-variables).

## Known Limitations

### tmux-resurrect / tmux-continuum

ccm's window options (`@ccm_project`, `@ccm_dir`) are not preserved by session restoration plugins. Do not restore the same environment with both tmux-resurrect and ccm. If windows that ccm does not manage share a project's name or directory, `ccm start` stops before creating anything; close those windows or register them with `ccm register`, then run it again.

### Status refresh interval

See the [diagnostics reference](diagnostics.md#status-refresh-interval).

### Narrow panes, long thinking, reduced motion

Narrow panes or reduced motion can make active work appear IDLE and lead to auto-exit. Keep hooks installed and raise `@ccm-idle-timeout` or set it to `0` to disable auto-exit.

See the [diagnostics reference](diagnostics.md#narrow-panes-long-thinking-reduced-motion).

### Ambiguous glyph width

See the [diagnostics reference](diagnostics.md#ambiguous-glyph-width).

### Debugging

See the [diagnostics reference](diagnostics.md#debugging).

#### Detection-behaviour debugging

See the [diagnostics reference](diagnostics.md#detection-behaviour-debugging).

## FAQ

### Do I lose my projects if I close my terminal app?

No. tmux runs as a background server process, independent of your terminal emulator (Ghostty, iTerm2, etc.). Closing or quitting the terminal only disconnects the display — all tmux sessions, windows, and ccm projects continue running. Just reopen your terminal and run `tmux attach` to reconnect.

> [!TIP]
> If you have multiple tmux sessions, use `tmux attach -t work` (replacing `work` with your session name) to reconnect to a specific one.

### Do I lose my projects when my Mac goes to sleep?

No. Sleep suspends all processes but does not terminate them. When you wake your Mac, tmux and all ccm projects resume exactly where they left off.

### When do I need to load a snapshot?

Only when the tmux server itself is terminated. This happens when:

- Your computer restarts or shuts down
- The machine crashes or loses power
- You manually run `tmux kill-server`

In these cases, run `ccm start _autosave` to restore your previous workspace. Tip: set `@ccm-auto-restore on` in your `.tmux.conf` to restore automatically on tmux start.

### What is the difference between `ccm start` and `ccm snapshot load`?

They are identical. `ccm start <name>` is a short alias for `ccm snapshot load <name>`. Similarly, `ccm stop --all` is the counterpart that saves an `_autosave` snapshot and closes all project windows.

### Do I need to set up Claude Code before using ccm?

Yes. Run `claude` once in a regular terminal to complete the initial authentication (subscription or API key setup). After that, ccm can launch Claude Code automatically in each project window.

### Can I use ccm across multiple tmux sessions?

ccm manages projects as windows within a single tmux session. The dashboard and status bar show projects from all sessions, but `ccm add` creates windows in your current session. Snapshot saving supports managed projects in one session and refuses inventories spanning multiple sessions.

### Can I view two projects side by side?

ccm manages one project per tmux window, so tmux pane splitting is not recommended — running two Claude Code instances in the same window interferes with state detection and hook signals.

**Recommended approach:** Open a separate terminal window (e.g., a new Ghostty window) **without tmux**, navigate to the project directory, and start Claude Code directly:

```bash
cd ~/code/other-project
claude --continue
```

This gives you a fully independent Claude Code session alongside your ccm-managed projects. Both sessions can work on the same project directory without conflict.

**Syncing back to ccm:** When you finish working in the separate window, the ccm-managed session will catch up automatically — idle auto-exit closes the stale session after 10 minutes, and switching to that window restarts Claude Code with `--continue`, loading the latest conversation. For immediate catch-up, type `/exit` in the ccm window and switch away then back.

### How should I stop Claude Code in a project?

Use `/exit` in the Claude Code prompt. This exits Claude Code but **keeps the tmux window and project registration**. The project shows as SHELL state and auto-restarts when you switch to it.

Do **not** close the tmux window directly (e.g., `prefix + &` or `exit` in the shell). This removes the window and its ccm registration, and the project will be missing from the next autosave.

In most cases, you don't need to manually stop Claude Code at all — idle auto-exit handles it automatically after 10 minutes.

**Auto-exit skips windows with live background work.** Before exiting, ccm checks the whole window: if any sibling pane is running an autonomous non-shell command (a batch job, a dev server, `tail -f`), or Claude itself still has a running Bash job (foreground or background task), the window is left alone no matter how long the conversation has been idle. The trade-off is deliberate: a window that permanently hosts a dev server will effectively never auto-exit — wrongly exiting interrupts real work, while wrongly keeping costs one idle process. Parked editors and pagers (vim/nvim/emacs/less/man/…) are exempt from this guard: actively using them refreshes the idle timer on its own, and exiting Claude leaves the sibling pane untouched — so a split-editor workflow does not disable auto-exit. When auto-exit does fire, a desktop notification announces it (silenced only by `@ccm-notify off`), so an exited session never reads as a mystery crash; the conversation always restores on the next attach via `claude --continue`.

**Auto-exit leaves a record.** Claude Code reports it as `SessionEnd` with reason `prompt_input_exit` — the same value a person typing `/exit` produces — so afterwards nothing distinguishes the two. ccm therefore writes one line per auto-exit to `$CCM_DATA_DIR/state/auto-exit.log` (timestamp, project, session id, idle seconds), and `ccm doctor --verbose` shows the count. If you ever wonder whether something has been closing your sessions, that file answers it; the session id makes the record joinable with whatever else observed the same session end.

**Auto-exit looks before it types.** IDLE says the conversation is at rest; it does not say the screen takes a slash command. A session sent to the background (`/background`, `←` twice, or `/exit` inside an attached background session, which detaches rather than exits) leaves the pane on the agent view, where text typed into the input box can start a new session and Escape moves between views rather than cancelling input. So auto-exit captures the target pane first and types nothing when it shows the agent view or cannot be read; and when the pane shows the agent view after `/exit` — what an attached background session does when told to exit: it detaches, and the worker keeps running — it does not treat that as an exit: no SHELL state, no autosave, no completion notice, no further keys. Only the screen is observed, so the record says what was seen, not why. Both are recorded in `$CCM_DATA_DIR/state/auto-exit-declined.log` (outcome and identifiers only, rate-limited per project and outcome), kept apart from `auto-exit.log` so that file keeps counting only real exits. `ccm doctor --verbose` shows both counts. Attached background sessions are, in short, not something auto-exit ends: the worker keeps running either way, and stopping it is `claude stop <short>`'s job.

### What is the difference between `_autosave` and named snapshots?

| | `_autosave` | Named snapshots |
|---|---|---|
| **Created by** | Automatically every 2 minutes | Manually via dashboard `s` key |
| **Content** | Last complete capture | Configuration at save time |
| **Overwritten** | When changed, unless protected | When saved again under the same name |
| **Used by auto-restore** | Yes | No (must load manually with `ccm start <name>`) |

**Tip:** Use `ccm prepare-logout` to protect a checkpoint before shutting down. Release protection with `ccm prepare-logout --cancel`.
