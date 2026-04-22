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
STATUS       PROJECT              BRANCH           PORTS        DIRECTORY
------       -------              ------           -----        ---------
◉ BUSY       my-project           main*            3000         ~/code/my-project
● IDLE       another-project      feature-x        -            ~/code/another-project
⚠ PERMIT     api-server           main             8080         ~/code/api-server
```

## The Dashboard

Open with `prefix + Tab`. This is the primary interface for managing projects. You can also bind a single key (e.g. `F1`) for prefix-free toggle — see the [Keybindings section in README](../README.md#keybindings) for details.

> ```
> ── ccm Dashboard ──────────────
>   6 project(s)
>
> ▶ #5  ⚠PERMIT  ml-pipeline    ✔20s ~/code/ml-pipeline
>   #4  ✔IDLE    auth-service   ✔2s  ~/code/auth-service
>   #2  ◉BUSY    api-gateway    ✔6s  ~/code/api-gateway
>   #3  ●IDLE    web-dashboard  ✔1m  ~/code/web-dashboard
>   #6  ●IDLE    mobile-app     ✔5m  ~/code/mobile-app
>   #7  ■SHELL   docs-site      ✔1d  ~/code/docs-site
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
| `x` | Exit all | Exit all idle Claude Code sessions to free resources |
| `/` | Filter | Live incremental search: type to narrow, `↑↓`/`C-p`/`C-n` to select, `Enter` to attach, `C-u` to clear, `Esc` to cancel. Unicode-safe — Japanese project names match on Japanese substrings |
| `t` | Tree | Switch to tree view |
| `m` | Menu | Switch to interactive menu |
| `q` / `Esc` | Quit | Close the dashboard |

The dashboard auto-refreshes every 2 seconds to keep status icons up to date. Navigation keys (`↑↓/jk`) respond instantly without waiting for a refresh.

### Direct-to-filter shortcut

If you find yourself reaching for `/` right after opening the dashboard, bind `@ccm-key-search` in your `~/.tmux.conf` to open the dashboard already in live-filter mode:

```tmux
set -g @ccm-key-search "/"   # prefix + / → dashboard opens in filter mode
```

You can also run `ccm search` (or `ccm dashboard --search`) from a shell or another tmux binding for the same effect. This is handy when you have many projects — type a few characters to jump straight to the one you want, instead of hunting through the full list.

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
echo "Please investigate issue #42 in rsyntaxtree" | ccm send fzf-workflow --stdin -y

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
| **SHELL** (Claude not running) | Refused | — | Launches Claude, waits 2s, then sends |
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
| `PostToolUseFailure` | BUSY | Tool execution failed (Claude Code v2.1.101+ split from `PostToolUse`) |
| `SubagentStart` / `SubagentStop` | BUSY | Subagent execution start/end (parent agent still working) |
| `PreCompact` / `PostCompact` | BUSY | Context compaction is busy work |
| `Stop` / `StopFailure` | clears BUSY | Claude finished responding (signal file deleted) |
| `PermissionRequest` | PERMIT | Tool requires user permission |
| `Notification` | PERMIT / clears signal | Permission prompt or MCP elicitation dialog shown / idle notification (matchers: `permission_prompt`, `elicitation_dialog`, `idle_prompt`) |
| `SessionEnd` | SHELL | Claude Code session ended (/exit, Ctrl+D, etc.) |
| `PermissionDenied` | PERMIT | Auto mode denied an action (check `/permissions` to retry) |

> [!NOTE]
> Hook signals are written to `$TMPDIR/ccm-$UID/hooks/`. BUSY is trusted as long as the Claude Code process is alive (cleared by `Stop`/`SessionEnd` or by process exit); PERMIT auto-clears after 10 min as a safety net.

Hook status is shown in the dashboard footer and `ccm status` output (Hooks: ON/OFF). If hooks are already installed, `ccm setup-hooks` will skip re-installation. If you reinstall ccm to a different path, it will automatically update hook paths.

To remove: `ccm remove-hooks`

### How each state is detected

| State | Method | Details |
|-------|--------|---------|
| **SHELL** | Process check | No `claude` process found among window's child processes |
| **BUSY** | Hook / JSONL / Process tree | Primary: UserPromptSubmit / PreToolUse / SubagentStart hooks. Fallbacks (any one wins): (a) the project's newest `~/.claude/projects/<slug>/<sessionId>.jsonl` has a **user/assistant record** newer than `JSONL_FRESH_THRESHOLD` (5s) — Claude Code appends a record at every conversation turn boundary, so this is positive evidence the session is alive even when hooks are silent ([#16047](https://github.com/anthropics/claude-code/issues/16047), [#25655](https://github.com/anthropics/claude-code/issues/25655)). System metadata records (v2.1.108+ recap / `system/away_summary`, `turn_duration`, `attachment/task_reminder`, ...) are filtered out so recap generation does not register as fresh activity; (b) `claude` has a grandchild process (e.g. `bash → xcodebuild` from the Bash tool) — works around the v2.1+ UI showing an empty `❯ ` prompt above an active tool; (c) any non-MCP direct child of `claude` |
| **IDLE** | Process tree | `claude` exists with only direct children (MCP / language servers) and a visible input prompt, with no fresh BUSY hook |
| **PERMIT** | Hook + capture-pane fallback | Primary: `PermissionRequest` / `PermissionDenied` / `Notification` (permission_prompt) hooks. Fallback: capture-pane match on the v2.1.101+ footer `Esc to cancel · Tab to amend · ctrl+e to explain` — catches hung hook sessions ([#16047](https://github.com/anthropics/claude-code/issues/16047)) |
| **Completion (✔)** | Display layer | Transient marker: shown for 30s after BUSY/PERMIT → IDLE transition, then clears |

### Detection without hooks

Without hooks, ccm falls back to process tree inspection with prompt pattern matching. This means:
- Text generation (no tool use) appears as IDLE, not BUSY
- Completion detection relies on BUSY→IDLE transition heuristics

### Completion tracking

When Claude Code finishes processing, ccm:
1. Records a completion timestamp (the project transitions to IDLE)
2. Shows `✔` in the window name and status bar as a "recently completed" marker
3. Sends a desktop notification (if configured)

The `✔` marker clears when:
- 30 seconds elapse (auto-clear)
- You switch to the window (via dashboard, tree, or `ccm attach`)
- You send a new prompt (Claude goes BUSY, clearing the marker)

## Status Bar Modes

Configure with `set -g @ccm-status-line` in your `~/.tmux.conf`. See the [README Status Bar section](../README.md#status-bar) for configuration details and screenshots.

### Mode 0 — Single icon (default)

Appends one icon to your existing status-right. The icon shows the highest-priority state:

> ```
> 0:◉ my-project  1:⚠ api*  2:✔ web  3:● docs      07:30  ⚠ PERMIT
> ```

Priority order: `⚠` PERMIT (yellow) > `◉` BUSY (orange) > `✔` recently completed (green) > `≡` all idle (gray)

- Best for: users who want minimal status bar impact
- Trade-off: no per-project detail (use dashboard for that)

### Mode 1 — Full (ccm-style window list)

Replaces the standard tmux window list with ccm-style colored entries. Your existing status-right is preserved.

> ```
> openai-workflow:● | ccm:◉ | monadic-chat:● | 21:30 2026-03-21
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
| `✔` | IDLE (recently completed) | Green |
| `■` | SHELL | Dark gray |

- The `✔` marker appears for 30 seconds after completion, then reverts to `●` IDLE
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

When you run `ccm stop --all`, the current layout is automatically saved as `_autosave`:

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

Or from the dashboard: press `p` to preview, then `c` to copy.

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
rm -f "${TMPDIR:-/tmp}/ccm-$(id -u)/status-right-original"
tmux source-file ~/.tmux/plugins/tmux-ccm/ccm.tmux.conf
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

## Using with Agent Teams

ccm works alongside Claude Code's [Agent Teams](https://code.claude.com/docs/en/agent-teams). The two operate at different levels and complement each other:

- **ccm** manages projects as tmux **windows** (one Claude Code per project)
- **Agent Teams** runs parallel agents as tmux **panes** within a single window

### How they work together

When you run Agent Teams inside a ccm-managed project window, ccm's state detection automatically aggregates across all panes. For example:

- If any teammate pane is in PERMIT state → the project shows `⚠ PERMIT` in ccm
- If any teammate is BUSY → the project shows `◉ BUSY`
- When all teammates are idle → the project shows `● IDLE`

This means ccm's dashboard and status bar give you visibility into Agent Teams activity without any extra configuration.

### No conflicts

| Feature | Agent Teams | ccm | Conflict |
|---------|------------|-----|----------|
| Keyboard shortcuts | `Shift+↓`, `Ctrl+T` (inside Claude Code) | `prefix + Tab/T/C` (tmux level) | None |
| Pane management | Splits panes within window | Manages windows | None |
| Window naming | Does not rename windows | Sets icon + name | None |

### Typical workflow

1. Use `ccm add` to register multiple projects
2. Switch to a project with the dashboard (`prefix + Tab`)
3. Inside that project, tell Claude Code to create an Agent Team
4. Agent Teams splits the window into panes for each teammate
5. ccm's dashboard shows the aggregated state of all teammates
6. Switch to another project with `prefix + Tab` while the team works

## Environment Variables

ccm exposes several tuning knobs via environment variables. Defaults are chosen to work well for most users; adjust only if you observe a specific problem. Set them in your shell rc file (e.g. `~/.zshrc`) before tmux starts.

### Detection timing

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_JSONL_FRESH_THRESHOLD` | `5` (seconds) | JSONL write age under which `jsonl_fresh_activity` promotes raw=IDLE to BUSY. Lower = faster settle after a turn, but risk of flashing IDLE during multi-turn gaps |
| `CCM_JSONL_ACTIVE_THRESHOLD` | `15` (seconds) | Post-Stop BUSY hold window (`jsonl_holds_busy`). Bridges the gap between the fresh window expiring and the final IDLE transition. Raise if you see BUSY flashing to IDLE and back during multi-step tool use |
| `CCM_BUSY_HOOK_JSONL_WINDOW` | `600` (seconds) | Maximum age of a BUSY hook signal that ccm will trust when the project's JSONL is also being written. Beyond this, ccm assumes the Stop hook was missed (anthropics/claude-code#25655) and releases BUSY |
| `CCM_JSONL_HOOK_GAP_TOLERANCE` | `60` (seconds) | Recap-phantom discriminator. A BUSY hook that fired more than this many seconds AFTER the last real conversation activity is rejected as phantom (e.g. v2.1.108 `away_summary`). Lower = more aggressive rejection |
| `CCM_COMPLETED_AT_TIMEOUT` | `30` (seconds) | How long the ✔ "recently completed" marker stays visible after a BUSY/PERMIT → IDLE transition |
| `CCM_COMPLETION_GRACE_SEC` | `3` (seconds) | Grace period between a Stop hook firing and the COMPLETED desktop notification. Claude Code fires Stop at every turn boundary (including mid-tool-use); ccm waits this long before alerting so a subsequent PreToolUse / UserPromptSubmit can cancel the pending notification. Lower = faster alerts but higher risk of notifying mid-conversation; raise if you frequently see premature "completion" notifications during long multi-turn work |
| `CCM_PERMIT_MAX_TIMEOUT` | `600` (seconds) | Safety net: PERMIT state auto-clears after this if no hook signal resolves it (e.g. if Claude Code crashed while a permission dialog was open) |
| `CCM_IDLE_EXIT_TIMEOUT` | `600` (seconds) | How long a Claude Code session can be IDLE before `x` (exit all) targets it, and how long before auto-exit triggers |
| `CCM_STARTUP_GRACE_SEC` | `60` (seconds) | Window during which the `startup_transient_raw_busy` rule demotes raw=BUSY to IDLE when no hook signal is present — covers Claude's MCP-loading phase after `claude --continue`, which typically completes in 10–30 s. Raise if your MCP setup takes longer to come up than 60 s, lower if you want a genuinely hung startup to surface as BUSY sooner |

### Canary thresholds

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_HOOKS_LOG_WARN_BYTES` | `104857600` (100 MB) | Size threshold for the `~/.claude/hooks.log` bloat canary. Claude Code does not rotate this file and bloated logs silently disable hook firing (anthropics/claude-code#16047) |
| `CCM_SHELL_CLUSTER_COUNT` | `3` | How many SHELL transitions within the window triggers the silent-exit canary (anthropics/claude-code#48069) |
| `CCM_SHELL_CLUSTER_WINDOW` | `600` (seconds) | Time window for counting SHELL transitions |

### Debug tracing

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_DEBUG_TRACE` | (unset) | Path to a JSONL trace file. When set, every slow-path detection scan (`inject-status`, dashboard, `ccm status`) appends a record with the full `DetectionContext`, matched rule, and resolved state. See [Detection-behaviour debugging](#detection-behaviour-debugging). Remember to set it via `tmux set-environment -g`, not shell `export`, so the tmux-spawned subprocesses see it |
| `CCM_TRACE_MAX_BYTES` | `104857600` (100 MB) | Size cap for the `CCM_DEBUG_TRACE` log. Once exceeded, a single `{"event":"trace_cap_reached", ...}` sentinel is written and subsequent appends are skipped, so a forgotten trace cannot fill the disk |

### Cache TTLs

| Variable | Default | Purpose |
|----------|---------|---------|
| `CCM_CACHE_TTL` | `30` (seconds) | Git branch / port detection cache lifetime |
| `CCM_JSONL_CACHE_TTL` | `30` (seconds) | JSONL path resolution cache lifetime |

### Tuning examples

```bash
# Snappier post-Stop transition (shorter BUSY lingering)
export CCM_JSONL_ACTIVE_THRESHOLD=10

# Longer ✔ marker visibility after completion
export CCM_COMPLETED_AT_TIMEOUT=60

# More aggressive recap-phantom rejection
export CCM_JSONL_HOOK_GAP_TOLERANCE=30

# Earlier hooks.log bloat warning (10 MB)
export CCM_HOOKS_LOG_WARN_BYTES=10485760
```

### Interactions with Claude Code's own environment variables

A few undocumented Claude Code env vars overlap with ccm's behavior. If you set both, be aware of the interaction:

| Claude Code env | Interaction with ccm |
|-----------------|----------------------|
| `CLAUDE_CODE_EXIT_AFTER_STOP_DELAY` | Makes Claude Code exit itself some seconds after a Stop event. This duplicates `CCM_IDLE_EXIT_TIMEOUT` — pick one path. If both are set, whichever fires first wins, and the other becomes a no-op on a SHELL-state window |
| `CLAUDE_CODE_IDLE_THRESHOLD_MINUTES`, `CLAUDE_CODE_IDLE_TOKEN_THRESHOLD` | Claude Code's own idle detection. When it fires, your SessionEnd hook runs and ccm observes the window transition to SHELL (no conflict, just additional auto-exit paths you may not expect) |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | Upper bound Claude Code gives the SessionEnd hook (ccm's `on-session-end.sh`). The ccm hook is trivial (one signal-file write) and completes well within any reasonable value; listed here only so you know ccm's hook is not the bottleneck if you tune it |
| `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS` | Emits authoritative `session_state_changed` events (states: `idle` / `running` / `requires_action`) but only via `--print --output-format=stream-json` stdout. ccm cannot consume them in interactive mode; they are listed here because you may see ccm adopt them in the future if Claude Code exposes a file or hook channel |
| `CLAUDE_CODE_NO_FLICKER` | Already handled by ccm. Preview capture falls back to `tmux capture-pane -a` when the pane uses the alternate screen buffer |
| `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` | No conflict. If you dislike Claude Code rewriting your tmux window title, set this to `1` in your shell rc — ccm's own window naming (state icons) takes precedence either way |

These are not required for ccm to work. They are listed only so that users who customize Claude Code can predict overlaps.

## Known Limitations

### tmux-resurrect / tmux-continuum

ccm's window options (`@ccm_project`, `@ccm_dir`) are not automatically preserved by session restoration plugins. After a tmux restore, use `ccm start _autosave` to re-register projects from the last autosave snapshot. Alternatively, enable `@ccm-auto-restore "on"` to handle this automatically on tmux startup.

### Status refresh interval

ccm's status bar updates are triggered by tmux's `status-interval` setting (default: 15 seconds). To get faster updates:

```tmux
set -g status-interval 5    # update every 5 seconds
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
19:48:55  raw=IDLE  prev=IDLE  hook=-,-  pid_age=653  jsonl=6883,end_turn  default → IDLE [WRITE]
```

Ctrl-C to stop. Safe to run alongside the live dashboard.

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

### What is the difference between `_autosave` and named snapshots?

| | `_autosave` | Named snapshots |
|---|---|---|
| **Created by** | Automatically every 2 minutes | Manually via dashboard `s` key |
| **Content** | Always mirrors the current project list | Frozen at the time of save |
| **Overwritten** | Yes, every 2 minutes | Never (unique date-based name) |
| **Used by auto-restore** | Yes | No (must load manually with `ccm start <name>`) |

**Tip:** If you're about to shut down and want to ensure all projects are preserved, save a named snapshot from the dashboard (`s` key). This creates a checkpoint like `save-20260331-1230` that won't be overwritten. You can restore it later with `ccm start save-20260331-1230`.
