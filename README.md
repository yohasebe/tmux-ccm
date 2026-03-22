# ccm - Claude Code Manager for tmux

**[日本語版 README](README.ja.md)**

A tmux-based multi-project manager for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Manage multiple Claude Code sessions as tmux windows with an interactive dashboard, status detection, and snapshot support.

**Dashboard** (`prefix + Tab`):

> ```
> -- ccm Dashboard --
>
> > #1 ◉ BUSY    my-app (main*) [:3000] ~/code/my-app
>   #2 ⚠ PERMIT  api-server (dev) ~/code/api-server
>   #3 ✔ DONE    web-client (main) ~/code/web-client
>   #4 ● IDLE    docs (main) ~/code/docs
>
> [↑↓/jk] select [Enter] attach [p]review [a]dd [s]ave [q] quit
> Last saved: 10:30:45
> ```

**Status bar** (mode 0):

> ```
> 0:◉ my-app  1:⚠ api*  2:✔ web  3:● docs      10:30  ◉ BUSY
> ```

## Features

- **Dashboard** — Interactive popup with real-time Claude Code status (BUSY/IDLE/PERMIT/DONE)
- **Tree View** — Hierarchical session/window/pane display with navigation
- **Git Integration** — Branch name and dirty status (`main*`) per project
- **Port Detection** — Listening TCP ports per project (with caching)
- **Snapshots** — Save and restore project layouts as JSON
- **Auto-start** — Claude Code auto-launches when switching to a SHELL-state window
- **Status Line** — Inject active project status into tmux status bar

## Requirements

- tmux 3.2+ (popup support required)
- [TPM](https://github.com/tmux-plugins/tpm) (for plugin installation; or use manual install)
- jq
- fzf
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

## Installation

### With TPM (recommended)

Add to your `~/.tmux.conf`:

```tmux
set -g @plugin 'yohasebe/tmux-claude-code-manager'
```

Reload tmux and press `prefix + I` to install.

### Manual

```bash
git clone https://github.com/yohasebe/tmux-claude-code-manager.git ~/.tmux/plugins/tmux-claude-code-manager
```

Add to your `~/.tmux.conf`:

```tmux
source-file ~/.tmux/plugins/tmux-claude-code-manager/ccm.tmux.conf
```

### Add to PATH

For CLI usage (`ccm add`, `ccm status`, etc.), add the plugin directory to your PATH:

```bash
# In your .zshrc or .bashrc
export PATH="$HOME/.tmux/plugins/tmux-claude-code-manager:$PATH"
```

### Zsh Completion (optional)

```bash
# In your .zshrc (before compinit)
fpath=($HOME/.tmux/plugins/tmux-claude-code-manager/completions $fpath)
```

## Usage

### Keybindings

| Key | Action |
|-----|--------|
| `prefix + Tab` | Toggle dashboard popup |
| `prefix + T` | Toggle tree view popup |
| `prefix + C` | Open ccm menu |

Customize keys in `~/.tmux.conf` (before loading the plugin):

```tmux
set -g @ccm-key-dashboard "Tab"
set -g @ccm-key-menu "C"
set -g @ccm-key-tree "T"
```

### Status Bar

ccm shows active project status in the tmux status bar. Configure the display mode:

```tmux
set -g @ccm-status-line 0     # default
```

| Value | Mode | Description |
|-------|------|-------------|
| `0` | Icon (default) | Single priority icon appended to status-right |
| `1` | Full | Replaces window list with ccm-style colored entries |
| `2` | Dedicated line | Adds status line(s) with branch/port details for all projects |

#### Mode 0 — Single icon (default)

Appends a single icon to your existing status-right. Your clock, battery, etc. are preserved. The icon reflects the highest-priority state across all projects:

| Priority | Condition | Icon | Color |
|----------|-----------|------|-------|
| 1 (highest) | Any project has PERMIT | `⚠` | Yellow |
| 2 | Any project has BUSY | `◉` | Cyan |
| 3 | Any project has DONE | `✔` | Green |
| 4 (lowest) | All projects are IDLE | `≡` | Gray |

Click the icon to open the dashboard for full details.

#### Mode 1 — Full (ccm-style window list)

Replaces the standard tmux window list with ccm-style colored entries showing project name and status icon. Your existing status-right (clock, etc.) is preserved.

```
openai-workflow:● │ ccm:◉ │ monadic-chat:● │ 21:30 2026-03-21
```

#### Mode 2 — Dedicated status line

Adds a second (or more) tmux status line below the main bar. Shows all projects including idle ones with git branch and port details. The main status bar is not modified.

```
my-project:◉(main*) │ api:●(dev)[:8080] │ ccm:✔(main*)
```

| State | Icon | Color |
|-------|------|-------|
| PERMIT | `⚠` | Yellow |
| BUSY | `◉` | Cyan |
| DONE | `✔` | Green |
| IDLE | `●` | Gray |
| SHELL | `■` | Dark gray |

Lines auto-expand based on terminal width and project count.

> **Note:** Mode 2 uses `status-format[1]` through `status-format[5]`. If other plugins also use these indices, conflicts may occur.

### Dashboard Controls

| Key | Action |
|-----|--------|
| `↑↓` / `jk` | Navigate projects |
| `Enter` | Attach to selected project |
| `s` | Save snapshot |
| `p` | Preview pane content (`c` to copy) |
| `a` | Add new project |
| `g` | Register existing window |
| `r` | Remove — choose [u]nregister (keep window) or [d]elete |
| `/` | Search projects |
| `q` / `Esc` | Close |

### CLI Commands

```
ccm add <dir> [name]              Add project (creates window + starts Claude)
ccm open <dir> [name]             Start Claude in current pane (split-pane use)
ccm register <window> [name]      Register existing window as ccm project
ccm unregister <name>             Unregister window from ccm (keep window)
ccm remove <name>                 Remove project window (kill window)
ccm attach <name|number>          Switch to project window
ccm list                          List managed projects
ccm status                        Show projects with status, branch, ports
ccm tree                          Show session/window/pane hierarchy
ccm ports                         Show listening ports per project
ccm capture [--copy] <name|#id>   Capture pane content (--copy: clipboard)
ccm dashboard                     Open interactive dashboard popup
ccm menu                          Interactive menu (for keybinding)
ccm snapshot save|load|list|delete  Manage snapshots
ccm start <snapshot>              Restore from snapshot
ccm stop [--all|name]             Stop project (--all saves _autosave snapshot)
```

### Status Icons

| Icon | State | Description |
|------|-------|-------------|
| ⚠ | PERMIT | Waiting for user permission |
| ◉ | BUSY | Claude is processing |
| ✔ | DONE | Response complete (auto-detected) |
| ● | IDLE | Waiting for input |
| ■ | SHELL | Shell active, Claude not running |
| ○ | DOWN | Window not available |

### Snapshots

Save your workspace layout and restore it later:

```bash
ccm snapshot save my-workspace
ccm snapshot list
ccm start my-workspace
```

When you run `ccm stop --all`, the current layout is auto-saved as `_autosave`:

```bash
ccm start _autosave   # restore previous session
```

## How It Works

- Projects are tmux windows tagged with `@ccm_project` and `@ccm_dir`
- Claude Code state is detected via process tree inspection (not screen scraping)
- DONE state is auto-detected on BUSY/PERMIT → IDLE transitions
- Git branch and port info are cached (30s) to minimize overhead
- Popup session context is passed via temp file (`$TMPDIR/ccm-$UID/`)

## Uninstall

1. Remove from `~/.tmux.conf`:
   ```tmux
   # Delete this line:
   set -g @plugin 'yohasebe/tmux-claude-code-manager'
   # Or if using source-file:
   # source-file ~/.tmux/plugins/tmux-claude-code-manager/ccm.tmux.conf
   ```

2. Clean up tmux state:
   ```bash
   # Remove ccm options
   tmux set -g -u @ccm-orig-status-right 2>/dev/null
   tmux set -g -u @ccm-orig-sr-length 2>/dev/null
   tmux set -g -u @ccm-status-line 2>/dev/null
   tmux set -g -u window-status-format 2>/dev/null
   tmux set -g -u window-status-current-format 2>/dev/null

   # Remove temp files
   rm -rf "${TMPDIR:-/tmp}/ccm-$(id -u)"

   # Remove runtime data (optional — keeps snapshots)
   rm -rf ~/.local/share/ccm
   ```

3. Reload tmux: `tmux source-file ~/.tmux.conf`

## Documentation

- **[User Guide](docs/guide.md)** — Tutorial, workflows, state detection, status bar modes, snapshots, tips, and troubleshooting
- **[ユーザーガイド（日本語）](docs/guide.ja.md)** — 同内容の日本語版

## License

MIT
