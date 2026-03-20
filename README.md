# ccm - Claude Code Manager

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
ccm remove <name>                 Remove project window
ccm attach <name|number>          Switch to project window
ccm list                          List managed projects
ccm status                        Show projects with status, branch, ports
ccm tree                          Show session/window/pane hierarchy
ccm ports                         Show listening ports per project
ccm capture [--copy] <name|#id>   Capture pane content (--copy: clipboard)
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

## License

MIT
