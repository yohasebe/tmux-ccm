# ccm User Guide

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

This creates a new tmux window, changes to the project directory, and launches Claude Code with `claude --continue` (so you can pick up the most recent conversation in that directory if one exists).

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
> When you switch to a project window where Claude Code isn't running, ccm automatically starts it with `claude --continue` to resume your last conversation.

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
| `r` | Remove | Choose [u]nregister (keep window) or [d]elete (kill window) |
| `i` | Ignore | Toggle CCM_IGNORE on the selected project (hide/restore it — see "Running a second model" below) |
| `x` | Exit all | Exit all idle Claude Code sessions to free resources |
| `/` | Filter | Live incremental search: type to narrow, `↑↓`/`C-p`/`C-n` to select, `Enter` to attach, `C-u` to clear, `Esc` to cancel. Unicode-safe — Japanese project names match on Japanese substrings |
| `t` | Tree | Switch to tree view |
| `m` | Menu | Switch to interactive menu |
| `q` / `Esc` | Quit | Close the dashboard |

The dashboard refreshes on a hybrid cadence: full state detection runs every 2 seconds, and in between, a lightweight fast tick (4×/second) watches the state channel the Claude Code hooks write to — so a hook-driven change (a permission prompt appearing, a prompt submitted) shows up in ~0.3 seconds rather than waiting out the full poll. The status bar gets the same treatment: on a state transition, the hook re-renders the bar immediately instead of waiting for the next `status-interval` tick. Navigation keys (`↑↓/jk`) respond instantly without waiting for any refresh.

The row order is decided when the dashboard opens (projects needing attention first) and then held stable while it stays open — a project changing state updates its icon in place but does not jump to a new position, so your selection never lands on the wrong project mid-interaction. Close and reopen the dashboard to re-sort by current state.

### Direct-to-filter shortcut

If you find yourself reaching for `/` right after opening the dashboard, bind `@ccm-key-search` in your `~/.tmux.conf` to open the dashboard already in live-filter mode:

```tmux
set -g @ccm-key-search "/"   # prefix + / → dashboard opens in filter mode
```

You can also run `ccm search` (or `ccm dashboard --search`) from a shell or another tmux binding for the same effect. This is handy when you have many projects — type a few characters to jump straight to the one you want, instead of hunting through the full list.

### Prefix-less dashboard hotkey

If you prefer a top-row function key over `prefix + Tab`, bind one with `@ccm-key-dashboard-noprefix`:

```tmux
# Must come BEFORE the ccm plugin load line — see the
# IMPORTANT block in README.md#keybindings for the full rule.
set -g @ccm-key-dashboard-noprefix "F1"   # F1 alone (no prefix) → dashboard
```

Goes through the same `display-popup` invocation as the prefix binding, so the coloured ccm logo on the popup title is preserved. Writing your own `bind-key -n F1 display-popup …` instead works mechanically but won't carry the logo unless you replicate the full `-T` format string.

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

```bash
# Simple positional message (confirmed interactively if run from a TTY)
ccm send blog "Summarize the last review cycle."

# From a file
ccm send research --file /tmp/brief.md

# From a pipe — perfect for wiring up an MCP server (Gmail, GitHub, etc.)
echo "Please investigate issue #42 in the parser repo" | ccm send fzf-workflow --stdin -y

# Multi-line body — \n is converted to Claude's "newline without submit" key,
# so the body lands as a single multi-line prompt
printf 'context:\nbug: NPE on line 120\nplease fix' | ccm send api-server --stdin -y

# Type text without submitting (user finishes editing in the target pane)
ccm send blog --no-enter "TODO: "
```

### State policy

| Target state | Default | `--force` | `--start` |
|---|---|---|---|
| **IDLE** | Send immediately | — | — |
| **BUSY** | Refused (avoid mixing with active turn) | Queued into input buffer | — |
| **SHELL** (Claude not running) | Refused | — | Launches Claude, polls for IDLE (up to `CCM_START_WAIT_SEC`, default 10s), then sends |
| **PERMIT** (permission dialog open) | **Hard refused** | **Still refused** — typing into a permission dialog could accidentally approve/deny a tool call | — |

### Flags

| Flag | Purpose |
|---|---|
| `--file <path>` | Read message from a file |
| `--stdin` (or bare `-`) | Read message from stdin |
| `--no-enter` | Send the text without the final Enter (useful for prefilling a prompt) |
| `--force` | Allow sending to a BUSY target (queues into Claude's input buffer) |
| `--start` | Auto-launch Claude if the target is in SHELL state |
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

This adds hooks to `~/.claude/settings.json`:

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
> Hook signals are written to `$TMPDIR/ccm-$UID/hooks/`. BUSY is cleared by `Stop`/`SessionEnd` or by process exit; if both the BUSY hook and the JSONL stay silent beyond `CCM_BUSY_HOOK_JSONL_WINDOW` (default 10 min), ccm stops trusting the stale signal and lets the state fall back to IDLE, so a missed `Stop` cannot strand a project in BUSY. PERMIT is released the same way: resolving a permission fires no hook upstream, so if a permit event is the newest one, the pane shows no modal, and the session log stays frozen beyond `CCM_PERMIT_MAX_TIMEOUT` (default 10 min), ccm stops trusting it and falls back to IDLE. A dialog still on screen is read from the pane directly and stays PERMIT no matter how long it waits.

Hook status is shown in the dashboard footer and `ccm status` output (Hooks: ON/OFF). If hooks are already installed, `ccm setup-hooks` will skip re-installation. If you reinstall ccm to a different path, it will automatically update hook paths.

To remove: `ccm remove-hooks`

### How each state is detected

| State | Method | Details |
|-------|--------|---------|
| **SHELL** | Process check | No `claude` process found among window's child processes |
| **BUSY** | Event log + JSONL stop_reason | Primary: the per-session event log (`hooks/<sessionId>.events.jsonl`, keyed on Claude Code's session UUID) appended by every BUSY-class hook (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStart`/`Stop`, `PreCompact`/`PostCompact`). `derive_state_from_events` evaluates the tail as a pure function and returns BUSY while the most recent entry is start-class. Hook silence is bridged by JSONL `stop_reason`: a fresh `tool_use` keeps BUSY across the tool-turn Stop boundary; an `end_turn` / `max_tokens` / `stop_sequence` newer than the latest event releases to IDLE within seconds. Claude Code housekeeping records (`system/away_summary`, `turn_duration`, `attachment/task_reminder`, `permission-mode`, `file-history-snapshot`, `last-prompt`) are filtered from JSONL activity so recap and startup housekeeping do not register as fresh activity |
| **IDLE** | Event log + capture-pane | The event log's most recent entry is end-class (`stop`, `notify_idle`, `notify_permit`-resolved), the input prompt `❯ ` is visible, and no PERMIT footer matches. With hooks disabled the legacy fallback uses process tree + prompt visibility only |
| **PERMIT** | Hook + capture-pane fallback | Primary: `PermissionRequest` / `PermissionDenied` / `Notification` (permission_prompt) hooks. Fallback: capture-pane match on the modal footer (`Esc to cancel · Tab to amend` for permission dialogs; `Enter to confirm · Esc to <verb>` for confirmation modals, including the v2.1.144 `/model` form `Enter to confirm · d to set as default for new sessions · Esc to cancel` where intermediate `· <action key>` segments are tolerated) — catches sessions where hooks have stopped firing |
| **Completion (`* elapsed`)** | Display layer | Transient marker: shown for 30s after BUSY/PERMIT → IDLE transition, then clears. Asterisk renders green (drawing the eye to the just-completed transition); the elapsed time is dim |
| **Multi-pane (`[N]`)** | Window inspection | Marker on every renderer (dashboard, status bar, `ccm status`) when a window holds more than one tmux pane (Agent Teams, casual splits, leftover orphan panes). Brackets dim, digit cyan. Lets you spot windows whose aggregated state may belong to a non-active pane. See "Using with Agent Teams" below for related details (sliver protection and PERMIT auto-focus) |
| **Permission mode (`{mode}`)** | Hook payload | Display-only badge from the `permission_mode` field Claude Code attaches to hook payloads; the newest value is shown as the `MODE` column in `ccm status` and a `{mode}` badge after the project name in the dashboard (`{accept}`, `{plan}`, `{auto}`, `{dontAsk}`, `{bypass}`). The everyday `manual` mode is suppressed in the dashboard. `{bypass}` renders in warning color. Modes that auto-resolve dialogs never produce PERMIT — the badge preempts misreading that silence as broken detection. Never consulted by state detection |
| **Ignored (`⊘`)** | Pane option | Dim `⊘` on the dashboard and `ccm status` row when the window has a `CCM_IGNORE`'d pane — a session ccm deliberately does not track (see "Running a second model as a sidekick" below). Present-but-untracked; not a state |

### Detection without hooks

Without hooks, ccm falls back to process tree inspection with prompt pattern matching. This means:
- Text generation (no tool use) appears as IDLE, not BUSY
- Completion detection relies on BUSY→IDLE transition heuristics

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

Save your project layout to restore it later.

### Save

```bash
ccm snapshot save my-workspace
```

### Restore

```bash
ccm start my-workspace
```

### Auto-save

The `_autosave` snapshot is updated automatically every 2 minutes while ccm projects exist, and is also written when you run `ccm stop --all`:

```bash
# Stop all projects (auto-saves)
ccm stop --all

# Next day, restore
ccm start _autosave
```

#### Auto-restore on tmux start

To automatically restore the last `_autosave` snapshot when tmux starts, add to `~/.tmux.conf`:

```tmux
set -g @ccm-auto-restore "on"    # default: off
```

> [!NOTE]
> This loads `_autosave` via TPM on startup. If ccm projects are already loaded, the restore is skipped.

### Manage snapshots

```bash
ccm snapshot list          # see all snapshots
ccm snapshot delete old    # remove a snapshot
```

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

### Every project frozen at the same state

If **all** projects are stuck at the same state (e.g. all BUSY, no longer updating after refresh), the detection cycle itself may have hit a silent exception. Check the log:

```bash
ccm errors
```

Each line is a previously-swallowed exception with timestamp, scope, and traceback. An empty log (`No silent-caught errors logged.`) means the cycle is healthy. If entries keep accumulating, the most recent traceback identifies the failing call site. `ccm errors --clear` removes both the active log and the rotated `errors.log.1`.

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

**Make the ignore visible on the pane itself** (optional): ccm sets a `⊘ ccm-ignored` pane title, which tmux shows only if you enable pane borders. Opt in with `tmux set -g @ccm-ignore-pane-border on` — ccm then turns on `pane-border-status` when a session is ignored (a global tmux change, so it happens only with this explicit opt-in). Without it, the dashboard `⊘` remains the cue.

**Caveat — same directory.** Running two Claude Code sessions in the same working directory hits an upstream bug (anthropics/claude-code#48112) where one session's background-task notifications can leak into the other's session log. `CCM_IGNORE` stops ccm from *tracking* the sidekick, but it cannot stop that leak from polluting your main session's log if the sidekick runs background tasks concurrently. Keep the sidekick for interactive consultation (avoid concurrent `run_in_background` work and simultaneous edits to the same files), or give each model its own directory via a git worktree if you need two co-equal agents.

## Relaying with a second agent CLI

You can run an agent CLI other than Claude Code in a split pane of a project window and let the two agents exchange messages without a human relaying text. ccm stays Claude-centric: it only shows a dim `⚙<name>` presence badge when a known external agent CLI runs in a pane (display only — it does not track that agent's state), and `ccm capture` shows every pane so either side can read the other.

The conventions that make the relay work:

- **Other agent → Claude**: the other agent runs `ccm send <project> "<message>"`. State gating applies (never into PERMIT), and the message lands as Claude's next turn — no one needs to watch.
- **Claude → other agent**: check the peer is ready with `ccm capture <project>` first (ccm cannot state-gate a non-Claude pane), then send the body and the submit key separately:
  ```bash
  tmux send-keys -t <pane> -l -- "<message>"   # -l: literal, do not resolve as key names
  tmux send-keys -t <pane> Enter
  ```
  The `-l` matters: without it tmux resolves arguments as key *names*, so a message containing a word like `Space` or `Enter` silently turns into that keystroke. (ccm's own delivery uses `-l --` for the same reason.) Newline keys differ between CLIs — Claude uses `M-Enter`, many others accept `C-j` — so for multi-line bodies send each line literally with the peer's newline key between them.
- **Report, don't poll**: neither side can observe the other's progress. When you finish a request, report back with `ccm send` — the reply arrives as a new turn on its own.
- **Long results**: write them to a file and send a one-line pointer; this also sidesteps the differing newline keys.

`ccm setup-claude-md` writes these conventions into `~/.claude/CLAUDE.md` so every Claude session knows them; putting the equivalent in the other CLI's own instructions file completes the loop.

## Using with agent view (background sessions)

Claude Code 2.1.139 introduced an [agent view](https://claude.com/blog/agent-view-in-claude-code): `claude agents` (TUI), `claude --bg <prompt>` (background dispatch), and `claude attach <short>` (foreground attach). All three run sessions as workers under a per-user supervisor daemon, completely outside tmux. ccm reads the daemon's state and surfaces those sessions in a read-only dashboard section so a single view shows both ccm-managed project windows and out-of-tmux background sessions.

### Enabling the section

Off by default — agent-view non-users see no clutter. There are three ways to make it visible:

- Press `b` inside the dashboard — toggles for the current popup only, no config persistence.
- Set `@ccm-bg-section "always"` in `~/.tmux.conf` — keeps it visible across opens.
- Toggle the `Background sessions: …` row in the dashboard menu (`m`) — writes the same option back to `~/.tmux.conf`.

The section appears below the project list and lists each active worker with its short ID, normalised state (`✽ WORKING` / `✻ NEEDS` / `● IDLE` / `✓ DONE` / `✕ FAILED`), human-readable name, age, and working directory.

### Attaching from the dashboard

Navigate to a bg row with `↑/↓` (selection moves seamlessly between projects and bg) and press `Enter`. ccm opens a new tmux window in the current session and runs `claude attach <short>` into it. The window's working directory matches the bg session when available, and its name is `bg-<short>` so it's easy to find with `prefix + w` (choose-tree).

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

### Data sources

The reader joins two files, both written by the daemon (read-only on the ccm side):

- `~/.claude/daemon/roster.json` — currently-active workers (pid, sessionId, cwd, cliVersion, dispatch metadata). Sessions are removed from this file after ~1 hour idle (`settled (done)`), matching what `claude agents` itself shows.
- `~/.claude/jobs/<short>/state.json` — per-session live state (`working` / `needs_input` / `idle` / `done` / `failed`), tempo, in-flight task counts, and an auto-generated name.

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
| `CCM_IDLE_EXIT_TIMEOUT` | `600` (seconds) | How long a Claude Code session can be IDLE before `x` (exit all) targets it, and how long before auto-exit triggers |
| `CCM_IDLE_PROMPT_GUARD_SEC` | `60` (seconds) | Guard in `on-notification.sh` for the `idle_prompt` Notification: idle_prompt arrives 10–60+ s late (anthropics/claude-code#5186), so a BUSY signal younger than this may have been written by work that started AFTER the notification was generated — deleting it would drop an actively working session to IDLE (and feed auto-exit's kill path). Signals younger than the guard are kept; older ones are cleared as before. Set to `0` to opt out and restore the old always-delete behaviour |
| `CCM_IGNORE` | unset | Launch-time flag, not a tunable: `CCM_IGNORE=1 claude` starts a session that ccm ignores entirely (see "Running a second model as a sidekick"). Toggle an already-running session with `ccm ignore` / `ccm unignore` instead |
| `CCM_STARTUP_GRACE_SEC` | `60` (seconds) | Window during which the legacy `startup_transient_raw_busy` rule demotes raw=BUSY to IDLE when no hook signal is present — covers Claude's MCP-loading phase after `claude --continue`, which typically completes in 10–30 s |
| `CCM_SLIVER_HEIGHT_THRESHOLD` | `4` (rows) | Minimum tmux pane height for a pane to participate in window-state aggregation. Panes shorter than this cannot render Claude's `❯` prompt, so capture-pane–based detection cannot tell them apart from a genuinely BUSY pane. Raise if you have legitimate small Agent Teams panes that should still count; lower (down to 1) to disable the filter entirely |
| `CCM_HOOK_CMD_TIMEOUT` | `5000` (ms) | Timeout Claude Code applies to each ccm hook invocation. ccm's hooks each do one signal-file write — comfortably within any reasonable value. Lower if you are debugging a hook hang; the default is generous enough that you will only notice it during a Claude Code or filesystem stall |
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
| `CCM_SHELL_CLUSTER_COUNT` | `3` | How many SHELL transitions within the window triggers the silent-exit canary (anthropics/claude-code#48069) |
| `CCM_SHELL_CLUSTER_WINDOW` | `600` (seconds) | Time window for counting SHELL transitions |
| `CCM_ERRORS_BURST_THRESHOLD` | `20` | How many `errors.log` records within the burst window triggers the silent-fail-loop canary. A poll-cycle bug (e.g. an exception fired by every `inject_status` refresh) accumulates roughly 30 records/min, so this threshold reliably distinguishes a runaway loop from one-off noise |
| `CCM_ERRORS_BURST_WINDOW` | `300` (seconds) | Time window for counting silent-fail records |

### Debug tracing

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_DEBUG_TRACE` | (unset) | Path to a JSONL trace file. When set, every slow-path detection scan (`inject-status`, dashboard, `ccm status`) appends a record with the full `DetectionContext`, matched rule, and resolved state. See [Detection-behaviour debugging](#detection-behaviour-debugging). Remember to set it via `tmux set-environment -g`, not shell `export`, so the tmux-spawned subprocesses see it |
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
| `CCM_AMBIGUOUS_WIDTH` | `1` | Terminal column count for East Asian Ambiguous characters (e.g. the IDLE icon `●`, SHELL icon `■`). Set to `2` on CJK locale terminals where Ambiguous chars render as 2 columns, so dashboard / `ccm status` columns stay aligned. Read at module load — restart inject-status / dashboard to pick up a change |
| `CCM_ERRORS_LOG_MAX_BYTES` | `1048576` (1 MB) | Size cap for `$TMPDIR/ccm-$UID/errors.log` (the silent-exception log). At the cap, the active log rotates to `errors.log.1` and a fresh log starts (total disk use ~2 × cap). View with `ccm errors`; clear with `ccm errors --clear` |
| `CCM_SESSION_INFO_AGE_DRIFT_SEC` | `10` (seconds) | Drift tolerance for the session_info pid-reuse check. When `read_session_info` is given a `ps` snapshot, it cross-checks Claude Code's recorded `startedAt` against the live process's etime-derived start time; a discrepancy beyond this tolerance means the json file is from a recycled pid's prior session and is rejected (caller falls through to legacy detection). 10 s comfortably covers normal clock drift / NTP corrections / the few-second gap between fork and Claude writing session_info |
| `CCM_STATUS_INTERVAL` | `5` (seconds) | Target tmux `status-interval` — how often the status bar re-renders. On plugin load, ccm lowers `status-interval` to this value if your current setting is higher (it never raises it). Set via `tmux set-environment -g` before the plugin loads, not shell `export` — see [Status refresh interval](#status-refresh-interval) |

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

### tmux-resurrect / tmux-continuum

ccm's window options (`@ccm_project`, `@ccm_dir`) are not automatically preserved by session restoration plugins. After a tmux restore, use `ccm start _autosave` to re-register projects from the last autosave snapshot. Alternatively, enable `@ccm-auto-restore "on"` to handle this automatically on tmux startup.

### Status refresh interval

ccm's status bar updates are driven by tmux's `status-interval`. On load, the plugin automatically lowers it to 5 seconds (from tmux's default of 15) if your current setting is higher — it only ever lowers the value, never raises it. To use a different target, set `CCM_STATUS_INTERVAL` in tmux's environment before the plugin loads:

```bash
tmux set-environment -g CCM_STATUS_INTERVAL 10   # poll every 10 seconds instead
```

Lower values increase CPU usage slightly.

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

ccm manages projects as windows within a single tmux session. The dashboard and status bar show projects from all sessions, but `ccm add` creates windows in your current session. If you need separate project sets, use named snapshots (`ccm snapshot save work`, `ccm snapshot save personal`).

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

### What is the difference between `_autosave` and named snapshots?

| | `_autosave` | Named snapshots |
|---|---|---|
| **Created by** | Automatically every 2 minutes | Manually via dashboard `s` key |
| **Content** | Always mirrors the current project list | Frozen at the time of save |
| **Overwritten** | Yes, every 2 minutes | Never (unique date-based name) |
| **Used by auto-restore** | Yes | No (must load manually with `ccm start <name>`) |

**Tip:** If you're about to shut down and want to ensure all projects are preserved, save a named snapshot from the dashboard (`s` key). This creates a checkpoint like `save-20260331-1230` that won't be overwritten. You can restore it later with `ccm start save-20260331-1230`.
