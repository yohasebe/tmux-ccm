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

### 1. Start tmux

```bash
tmux new-session -s work
```

### 2. Add your first project

```bash
ccm add ~/code/my-project
```

This creates a new tmux window, changes to the project directory, and launches Claude Code with `claude --resume` (so you can pick up a previous conversation if one exists).

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

```
  ► #1 ◉ BUSY   my-project (main*) ~/code/my-project
    #2 ● IDLE   another-project (feature-x) ~/code/another-project
    #3 ⚠ PERMIT api-server (main) [:8080] ~/code/api-server

  [↑↓/jk] select  [Enter] attach  [s]plit  [p]review  [a]dd  [g] register
  [r]emove  [/] search  [q/Esc] quit
```

### Dashboard actions

| Key | Action | When to use |
|-----|--------|-------------|
| `↑↓` or `jk` | Move selection | Navigate between projects |
| `Enter` | Switch to project | Jump to the selected project window |
| `s` | Split | Open project in a side-by-side pane |
| `p` | Preview | See what's on the project's screen (press `c` to copy) |
| `a` | Add | Register a new project directory |
| `g` | Register | Tag an existing tmux window as a ccm project |
| `r` | Remove | Remove a project window |
| `/` | Search | Filter projects by name |
| `q` or `Esc` | Quit | Close the dashboard |

The dashboard auto-refreshes every 2 seconds to keep status icons up to date. Navigation keys (`↑↓/jk`) respond instantly without waiting for a refresh.

## The Tree View

Open with `prefix + T`. Shows the full tmux hierarchy:

```
  ├── work ◀
  │   ├── ◉ my-project (main*) ~/code/my-project ◀
  │   ├── ● another-project (feature-x) ~/code/another-project
  │   ├── ⚠ api-server (main) [:8080] ~/code/api-server
  │   └── ■ bash ~/home
  └── other-session
      └── ■ bash ~/home

  [↑↓/jk] select  [Enter] attach  [q/Esc] quit
```

- `◀` marks your current session/window
- Only windows (not sessions or panes) are selectable
- Panes are shown only when a window has multiple panes

## State Detection

ccm detects Claude Code's state without parsing screen output (except for PERMIT). This makes it resilient to UI changes.

### How each state is detected

| State | Method | Details |
|-------|--------|---------|
| **SHELL** | Process check | No `claude` process found among window's child processes |
| **BUSY** | Process tree | `claude` process has child processes (e.g., running tools) |
| **IDLE** | Process tree | `claude` process exists but has no children |
| **PERMIT** | Screen capture | Last 8 lines contain permission keywords ("Do you want", "Allow", etc.) |
| **DONE** | State transition | Detected when BUSY/PERMIT transitions to IDLE |

### DONE tracking

When Claude Code finishes processing (BUSY → IDLE), ccm:
1. Sets the state to DONE
2. Shows `✔` in the window name and status bar
3. Displays a tmux message: `✔ project-name: response complete`

The DONE flag clears automatically when you switch to that window.

## Status Bar Modes

Configure with `set -g @ccm-status-line` in your `~/.tmux.conf`.

### Mode 0 — Full details

Shows all active project names with status icons in the status-right area.

```
 project-a:◉ │ project-b:⚠ │ ≡
```

- Best for: users who want maximum visibility
- Trade-off: replaces your existing status-right content

### Mode 1 — Single icon (default)

Appends one icon to your existing status-right. The icon shows the highest-priority state:

```
 21/03  07:30:00  ⚠
```

Priority order: `⚠` PERMIT (yellow) > `◉` BUSY (cyan) > `✔` DONE (green) > `≡` all idle (gray)

- Best for: users who want minimal status bar impact
- Trade-off: no per-project detail (use dashboard for that)

### Mode 2 — Dedicated line

Adds a second status bar line below the main bar, showing all projects including idle ones.

```
 Main bar:  0:bash  1:my-project  2:api-server     21/03  07:30:00
 ccm line:  my-project:◉ │ another-project:● │ api-server:⚠
```

| Icon | State | Color |
|------|-------|-------|
| `⚠` | PERMIT | Yellow |
| `◉` | BUSY | Cyan |
| `✔` | DONE | Green |
| `●` | IDLE | Gray |
| `■` | SHELL | Dark gray |

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
tmux source-file ~/.tmux/plugins/ccm/ccm.tmux.conf
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
