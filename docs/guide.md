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

When you switch to a project window where Claude Code isn't running, ccm automatically starts it with `claude --continue` to resume your last conversation.

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

Open with `prefix + Tab`. This is the primary interface for managing projects.

> ```
> -- ccm Dashboard --
>
> > #1 ◉ BUSY    my-project (main*) ~/code/my-project
>   #2 ● IDLE    another-project (feature-x) ~/code/another-project
>   #3 ⚠ PERMIT  api-server (main) [:8080] ~/code/api-server
>
> [↑↓/jk] select [Enter] attach [p]review [a]dd [s]ave [q] quit
> Last saved: 10:30:45
> ```

### Dashboard actions

| Key | Action | When to use |
|-----|--------|-------------|
| `↑↓` or `jk` | Move selection | Navigate between projects |
| `Enter` | Switch to project | Jump to the selected project window |
| `s` | Save | Save snapshot (enter name or default `_autosave`) |
| `p` | Preview | See what's on the project's screen (press `c` to copy) |
| `a` | Add | Register a new project directory |
| `g` | Register | Tag an existing tmux window as a ccm project |
| `r` | Remove | Choose [u]nregister (keep window) or [d]elete (kill window) |
| `/` | Search | Filter projects by name |
| `q` or `Esc` | Quit | Close the dashboard |

The dashboard auto-refreshes every 2 seconds to keep status icons up to date. Navigation keys (`↑↓/jk`) respond instantly without waiting for a refresh.

## The Tree View

Open with `prefix + T`. Shows the full tmux hierarchy:

> ```
> work <
>   ◉ my-project (main*) ~/code/my-project <
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
| `SubagentStart` | BUSY | Subagent spawned (Agent tool) |
| `Stop` | DONE | Claude finished responding |
| `Notification` | PERMIT / DONE | Permission prompt shown / idle notification |

Hook signals are written to `$TMPDIR/ccm-$UID/hooks/` and automatically expire (BUSY: 5 min, DONE/PERMIT: 30s).

Hook status is shown in the dashboard footer and `ccm status` output (Hooks: ON/OFF). If hooks are already installed, `ccm setup-hooks` will skip re-installation. If you reinstall ccm to a different path, it will automatically update hook paths.

To remove: `ccm remove-hooks`

### How each state is detected

| State | Method | Details |
|-------|--------|---------|
| **SHELL** | Process check | No `claude` process found among window's child processes |
| **BUSY** | Hook / Process tree | Hooks: UserPromptSubmit, PreToolUse, SubagentStart. Fallback: `claude` has child processes |
| **IDLE** | Process tree | `claude` process exists but has no children, no fresh hook signal |
| **PERMIT** | Hook only | Notification hook (permission_prompt). Requires `ccm setup-hooks` |
| **DONE** | Hook signal / State transition | Hook: Stop fired. Fallback: BUSY/PERMIT → IDLE transition |

### Detection without hooks

Without hooks, ccm falls back to process tree inspection only. This means:
- Text generation (no tool use) appears as IDLE, not BUSY
- DONE detection relies on BUSY→IDLE transition heuristics

### DONE tracking

When Claude Code finishes processing, ccm:
1. Sets the state to DONE
2. Shows `✔` in the window name and status bar
3. Sends a desktop notification (if configured)

The DONE flag clears when:
- 30 seconds elapse (auto-clear)
- You switch to the window (via dashboard, tree, or `ccm attach`)
- You send a new prompt (Claude goes BUSY, clearing the flag)

## Status Bar Modes

Configure with `set -g @ccm-status-line` in your `~/.tmux.conf`.

### Mode 0 — Single icon (default)

Appends one icon to your existing status-right. The icon shows the highest-priority state:

> ```
> 0:◉ my-project  1:⚠ api*  2:✔ web  3:● docs      07:30  ⚠ PERMIT
> ```

Priority order: `⚠` PERMIT (yellow) > `◉` BUSY (cyan) > `✔` DONE (green) > `≡` all idle (gray)

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
| `◉` | BUSY | Cyan |
| `✔` | DONE | Green |
| `●` | IDLE | Gray |
| `■` | SHELL | Dark gray |

- DONE auto-clears after 30 seconds and reverts to IDLE
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

This loads `_autosave` via TPM on startup. If ccm projects are already loaded, the restore is skipped.

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
tmux source-file ~/.tmux/plugins/tmux-claude-code-manager/ccm.tmux.conf
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

To reset ccm state completely:

```bash
rm -rf "${TMPDIR:-/tmp}/ccm-$(id -u)"
tmux source-file ~/.tmux.conf
```

## FAQ

### Do I lose my projects if I close my terminal app?

No. tmux runs as a background server process, independent of your terminal emulator (Ghostty, iTerm2, etc.). Closing or quitting the terminal only disconnects the display — all tmux sessions, windows, and ccm projects continue running. Just reopen your terminal and run `tmux attach` to reconnect.

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

**Syncing back to ccm:** When you finish working in the separate window, the ccm-managed session will catch up automatically — idle auto-exit closes the stale session after 5 minutes, and switching to that window restarts Claude Code with `--continue`, loading the latest conversation. For immediate catch-up, type `/exit` in the ccm window and switch away then back.

### How should I stop Claude Code in a project?

Use `/exit` in the Claude Code prompt. This exits Claude Code but **keeps the tmux window and project registration**. The project shows as SHELL state and auto-restarts when you switch to it.

Do **not** close the tmux window directly (e.g., `prefix + &` or `exit` in the shell). This removes the window and its ccm registration, and the project will be missing from the next autosave.

In most cases, you don't need to manually stop Claude Code at all — idle auto-exit handles it automatically after 5 minutes.

### What is the difference between `_autosave` and named snapshots?

| | `_autosave` | Named snapshots |
|---|---|---|
| **Created by** | Automatically every 2 minutes | Manually via dashboard `s` key |
| **Content** | Always mirrors the current project list | Frozen at the time of save |
| **Overwritten** | Yes, every 2 minutes | Never (unique date-based name) |
| **Used by auto-restore** | Yes | No (must load manually with `ccm start <name>`) |

**Tip:** If you're about to shut down and want to ensure all projects are preserved, save a named snapshot from the dashboard (`s` key). This creates a checkpoint like `save-20260331-1230` that won't be overwritten. You can restore it later with `ccm start save-20260331-1230`.
