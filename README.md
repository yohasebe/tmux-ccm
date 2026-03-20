# ccm - Claude Code Manager

**[日本語版 README はこちら](README.ja.md)**

A tmux-based multi-project manager for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Manage multiple Claude Code sessions as tmux windows with an interactive dashboard, status detection, and snapshot support.

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
- jq
- fzf
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

## Installation

### With TPM (recommended)

Add to your `~/.tmux.conf`:

```tmux
set -g @plugin 'yohasebe/ccm'
```

Reload tmux and press `prefix + I` to install.

### Manual

```bash
git clone https://github.com/yohasebe/ccm.git ~/.tmux/plugins/ccm
```

Add to your `~/.tmux.conf`:

```tmux
source-file ~/.tmux/plugins/ccm/ccm.tmux.conf
```

### Add to PATH

For CLI usage (`ccm add`, `ccm status`, etc.), add the plugin directory to your PATH:

```bash
# In your .zshrc or .bashrc
export PATH="$HOME/.tmux/plugins/ccm:$PATH"
```

### Zsh Completion (optional)

```bash
# In your .zshrc (before compinit)
fpath=($HOME/.tmux/plugins/ccm/completions $fpath)
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
set -g @ccm-status-line 1     # default
```

| Value | Mode | Description |
|-------|------|-------------|
| `0` | Full | Shows all active projects with name and icon in status-right |
| `1` | Icon (default) | Single priority icon appended to status-right |
| `2` | Dedicated line | Adds status line(s) showing all projects including idle |

#### Mode 0 — Full details in status-right

Replaces status-right with project name + icon for each active (BUSY/PERMIT/DONE) project. When all projects are idle, shows `≡`. This provides the most information but overwrites your existing status-right.

#### Mode 1 — Single icon (default)

Appends a single icon to your existing status-right. Your clock, battery, etc. are preserved. The icon reflects the highest-priority state across all projects:

| Priority | Condition | Icon | Color |
|----------|-----------|------|-------|
| 1 (highest) | Any project has PERMIT | `⚠` | Yellow |
| 2 | Any project has BUSY | `◉` | Cyan |
| 3 | Any project has DONE | `✔` | Green |
| 4 (lowest) | All projects are IDLE | `≡` | Gray |

Click the icon to open the dashboard for full details.

#### Mode 2 — Dedicated status line

Adds a second (or more) tmux status line below the main bar. Shows all projects including idle ones. The main status bar is not modified.

| State | Icon | Color |
|-------|------|-------|
| PERMIT | `⚠` | Yellow |
| BUSY | `◉` | Cyan |
| DONE | `✔` | Green |
| IDLE | `●` | Gray |
| SHELL | `■` | Dark gray |

Lines auto-expand based on terminal width and project count.

### Dashboard Controls

| Key | Action |
|-----|--------|
| `↑↓` / `jk` | Navigate projects |
| `Enter` | Attach to selected project |
| `s` | Split (open project side-by-side) |
| `p` | Preview pane content (`c` to copy) |
| `a` | Add new project |
| `g` | Register existing window |
| `r` | Remove project |
| `/` | Search projects |
| `q` / `Esc` | Close |

### CLI Commands

```
ccm add <dir> [name]              Add project (creates window + starts Claude)
ccm open <dir> [name]             Start Claude in current pane (split-pane use)
ccm register <window> [name]      Register existing window as ccm project
ccm remove <name>                 Remove project window
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

## Documentation

- **[User Guide](docs/guide.md)** — Tutorial, workflows, state detection, status bar modes, snapshots, tips, and troubleshooting
- **[ユーザーガイド（日本語）](docs/guide.ja.md)** — 同内容の日本語版

## License

MIT
