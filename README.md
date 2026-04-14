<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.png">
  <img alt="ccm — Claude Code Manager for tmux" src="assets/logo-light.png" width="380" align="left">
</picture>
<br clear="left">
<br>

Run multiple [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions in parallel. Switch between projects instantly, see which ones need your attention, and never lose your workspace.

ccm is a tmux plugin that manages Claude Code sessions as tmux windows — with a live dashboard, state detection, and snapshot restore.

**Dashboard** (`prefix + Tab`):

![ccm dashboard](assets/dashboard.png)

**Status bar** (mode 2 shown):

![ccm status bar mode 2](assets/statusbar-mode2.png)

## Features

- **Resource Management** — Idle Claude Code sessions auto-exit after 10 minutes to free memory and CPU; auto-restart with `--continue` when you switch back
- **Dashboard** — Interactive popup with real-time Claude Code status (BUSY/IDLE/PERMIT/DONE)
- **Tree View** — Hierarchical session/window/pane display with navigation
- **Git Integration** — Branch name and dirty status (`main*`) per project
- **Port Detection** — Listening TCP ports per project (with caching)
- **Snapshots** — Save and restore project layouts as JSON
- **Auto-start** — Claude Code auto-launches when switching to a SHELL-state window
- **Status Line** — Inject active project status into tmux status bar
- **Agent Teams Compatible** — Works alongside Claude Code's [Agent Teams](https://code.claude.com/docs/en/agent-teams): manage projects with ccm while running parallel agents within each project

## Requirements

- tmux 3.2+ (popup support required)
- Python 3.9+ (standard on macOS and most Linux distributions)
- [TPM](https://github.com/tmux-plugins/tpm) (for plugin installation; or use manual install)
- jq
- fzf
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — **v2.1.107 or later recommended**. ccm registers the `elicitation_dialog` Notification matcher (added in v2.1.107) for MCP elicitation prompts; on older releases this matcher is automatically skipped at install time (detected via `claude --version`), so v2.1.101–v2.1.106 clients still work with the remaining 13 hook events — you just do not get MCP elicitation detection until you update. v2.1.100 and earlier are not supported because ccm requires the `PostToolUseFailure` event that landed in v2.1.101

## Installation

### With TPM (recommended)

Add to your `~/.tmux.conf`:

```tmux
set -g @plugin 'yohasebe/tmux-ccm'
```

Reload tmux and press `prefix + I` to install.

### Manual

```bash
git clone https://github.com/yohasebe/tmux-ccm.git ~/.tmux/plugins/tmux-ccm
```

Add to your `~/.tmux.conf`:

```tmux
source-file ~/.tmux/plugins/tmux-ccm/ccm.tmux.conf
```

### Add to PATH

For CLI usage (`ccm add`, `ccm status`, etc.), add the plugin directory to your PATH:

```bash
# In your .zshrc or .bashrc
export PATH="$HOME/.tmux/plugins/tmux-ccm:$PATH"
```

### Zsh Completion (optional)

```bash
# In your .zshrc (before compinit)
fpath=($HOME/.tmux/plugins/tmux-ccm/completions $fpath)
```

## First-Time Setup

If you haven't used Claude Code before, complete the initial authentication first:

```bash
claude
```

This opens an interactive setup where you choose your plan (subscription or API key) and authenticate via browser. Once authenticated, run the setup wizard:

```bash
ccm init
```

This guides you through hooks installation, auto-restore, and status bar configuration in one step.

> [!NOTE]
> Hooks are automatically installed on plugin load (via TPM) and kept up to date on plugin updates. If you prefer manual control, use `ccm setup-hooks` and `ccm remove-hooks`.

## Usage

### Keybindings

| Key | Action | Default |
|-----|--------|---------|
| `prefix + Tab` | Toggle dashboard popup | Enabled |
| `prefix + T` | Toggle tree view popup | Disabled (opt-in) |
| `prefix + C` | Open ccm menu | Disabled (opt-in) |

Only the dashboard keybinding is enabled by default to avoid conflicts with other plugins. Tree view and menu are also accessible from within the dashboard by pressing `t` or `m`.

To additionally bind dedicated keys, add to `~/.tmux.conf`:

```tmux
set -g @ccm-key-menu "C"        # optional: enable prefix + C for menu
set -g @ccm-key-tree "T"        # optional: enable prefix + T for tree view
```

> [!TIP]
> For even quicker access, you can bind a single key (no prefix) to toggle the dashboard. For example, to use `F1`:
>
> ```tmux
> bind-key -T root F1 run-shell 'mkdir -p "${TMPDIR:-/tmp}/ccm-$(id -u)" && printf "#{session_name}" > "${TMPDIR:-/tmp}/ccm-$(id -u)/popup-session"' \; display-popup -E -w 80% -h 60% -T " ccm Dashboard " "~/.tmux/plugins/tmux-ccm/ccm dashboard"
> ```
>
> Place this **after** the ccm plugin loads. Press `F1` to open, `F1` again to close. Adjust the path if you installed ccm manually (e.g. `~/path/to/ccm/ccm dashboard`).

> [!IMPORTANT]
> All `set -g @ccm-*` options must be placed **before** the ccm plugin loads in `~/.tmux.conf` — that means before both the `source-file` line (manual install) and the TPM `run` line (TPM install). The plugin reads these options at load time, so settings placed after will not take effect.

### Desktop Notifications

ccm can send desktop notifications (macOS and Linux) when project states change:

```tmux
set -g @ccm-notify "permit,done"     # notify on PERMIT and DONE
```

| Value | Behavior |
|-------|----------|
| `permit,done` (default) | Notify on permission prompt and response completion |
| `permit` | Notify when permission is needed |
| `done` | Notify when response completes |
| `all` | All state changes |
| `off` | No notifications |

Enable notification sound:

```tmux
set -g @ccm-notify-sound "on"     # default: off (plays "Glass" sound on macOS)
set -g @ccm-notify-sound-name "Ping"  # optional: customize sound (macOS only)
```

### Status Bar

ccm shows active project status in the tmux status bar. Configure the display mode:

```tmux
set -g @ccm-status-line 0     # default
```

| Value | Mode | Description |
|-------|------|-------------|
| `0` | Icon (default) | Priority icon with window indices appended to status-right |
| `1` | Full | Replaces window list with ccm-style colored entries |
| `2` | Dedicated line | Adds status line(s) with branch/port details for all projects |

#### Mode 0 — Icon with indices (default)

Appends a priority icon with window indices to your existing status-right. Your clock, battery, etc. are preserved. When active projects exist, window numbers are shown (e.g., `5: PERMIT ⚠`). When all are IDLE, a single `≡` icon is shown:

![status bar mode 0](assets/statusbar-mode0.png)

| Priority | Condition | Icon | Color |
|----------|-----------|------|-------|
| 1 (highest) | Any project has PERMIT | `⚠` | Yellow |
| 2 | Any project has BUSY | `◉` | Orange |
| 3 | Any project has DONE | `✔` | Green |
| 4 (lowest) | All projects are IDLE | `≡` | Gray |

Click the icon to open the dashboard for full details.

#### Mode 1 — Full (ccm-style window list)

Replaces the standard tmux window list with ccm-style colored entries showing project name and status icon. Your existing status-right (clock, etc.) is preserved.

![status bar mode 1](assets/statusbar-mode1.png)

#### Mode 2 — Dedicated status line

Adds one or more tmux status lines below the main bar. Shows all projects including idle ones, with git branch and port details when available. The main status bar is not modified.

![status bar mode 2](assets/statusbar-mode2.png)

| State | Icon | Color |
|-------|------|-------|
| PERMIT | `⚠` | Yellow |
| BUSY | `◉` | Orange |
| DONE | `✔` | Green |
| IDLE | `●` | Blue |
| SHELL | `■` | Dark gray |

Lines auto-expand based on terminal width and project count.

> [!NOTE]
> Mode 2 uses additional `status-format` lines (up to `status-format[5]` depending on project count). If other plugins also use these indices, conflicts may occur.

### Dashboard Controls

| Key | Action |
|-----|--------|
| `↑↓` / `jk` | Navigate projects |
| `Enter` | Attach to selected project |
| `s` | Save snapshot |
| `p` | Preview pane content (`c` to copy) |
| `a` | Add new project |
| `n` | Rename selected project |
| `g` | Register existing window |
| `r` | Remove — choose [u]nregister (keep window) or [d]elete |
| `x` | Exit all idle Claude Code sessions |
| `/` | Search projects |
| `t` | Switch to tree view |
| `m` | Switch to menu |
| `q` / `Esc` / `F1` | Close |

### CLI Commands

```
ccm add <dir> [name]              Add project (creates window + starts Claude)
ccm open <dir> [name]             Start Claude in current pane (split-pane use)
ccm register <window> [name]      Register existing window as ccm project
ccm unregister <name>             Unregister window from ccm (keep window)
ccm rename <name> <new_name>      Rename a project
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
ccm init                          Interactive setup wizard (hooks, restore, status bar)
ccm setup-claude-md               Add ccm section to ~/.claude/CLAUDE.md
ccm remove-claude-md              Remove ccm section from ~/.claude/CLAUDE.md
ccm statusline                    Print one-line status (used by tmux status bar)
ccm inject-status                 Update tmux status bar (called internally)
```

> [!TIP]
> Most commands have short aliases: `ls` (list), `st` (status), `a` (attach), `rm` (remove), `d` (dashboard), `reg` (register), `unreg` (unregister), `mv` (rename), `cap` (capture), `snap` (snapshot), `sl` (statusline).

### Status Icons

| Icon | State | Description |
|------|-------|-------------|
| ⚠ | PERMIT | Waiting for user permission |
| ◉ | BUSY | Claude is processing |
| ✔ | DONE | Response complete (auto-clears after 30s) |
| ● | IDLE | Waiting for input |
| ■ | SHELL | Shell active, Claude not running |
| ○ | DOWN | Window not available |

### Claude Code Hooks (Recommended)

For more accurate state detection, install Claude Code hooks:

```bash
ccm setup-hooks
```

This adds hooks to `~/.claude/settings.json` that signal state changes:
- **UserPromptSubmit** → BUSY when you submit a prompt (detects text generation)
- **PreToolUse / PostToolUse / PostToolUseFailure** → BUSY across tool execution (PostToolUseFailure is a Claude Code v2.1.101+ event for tool errors)
- **SubagentStart / SubagentStop** → BUSY around subagent execution (the parent agent is still working)
- **PreCompact / PostCompact** → BUSY (context compaction is busy work)
- **Stop / StopFailure** → DONE when Claude finishes responding
- **PermissionRequest** → PERMIT when a tool requires permission
- **Notification** → PERMIT (permission_prompt / elicitation_dialog) or DONE (idle_prompt)
- **SessionEnd** → SHELL when Claude Code session ends (/exit, Ctrl+D, etc.)
- **PermissionDenied** → PERMIT when auto mode denies an action (check `/permissions` to retry)

ccm has multiple hook-independent fallbacks so detection still works when Claude Code stops firing hooks mid-session ([anthropics/claude-code#16047](https://github.com/anthropics/claude-code/issues/16047), [#25655](https://github.com/anthropics/claude-code/issues/25655)):

- **JSONL session log heartbeat**: ccm polls the mtime of the newest `~/.claude/projects/<slug>/<sessionId>.jsonl` file. Claude Code appends a record at every conversation turn boundary, so a fresh mtime is positive evidence the session is active.
- **Process grandchild detection**: A grandchild process under `claude` (e.g. `claude → bash → xcodebuild`) is unambiguous evidence that a foreground tool is running, even if the input prompt is visible (the v2.1+ "ctrl+b ctrl+b to background" UI).
- **Permission dialog footer match**: ccm recognizes the v2.1.101+ permission footer (`Esc to cancel · Tab to amend · ctrl+e to explain`) directly from the visible pane.
- **`~/.claude/hooks.log` size canary**: ccm warns in `ccm status` and the dashboard footer when this file exceeds 100 MB — the documented root cause of #16047 is silent hook failure due to log bloat. The fix is `: > ~/.claude/hooks.log`.

Hook status is shown in the dashboard footer and `ccm status` output (Hooks: ON/OFF). If hooks are already installed, `ccm setup-hooks` will skip re-installation. If you reinstall ccm to a different path, it will automatically update hook paths.

To remove: `ccm remove-hooks`

### Snapshots

Save your workspace layout and restore it later:

```bash
ccm snapshot save my-workspace
ccm snapshot list
ccm start my-workspace
```

The `_autosave` snapshot is also updated automatically every 2 minutes while projects are active. When you run `ccm stop --all`, it is saved as well:

```bash
ccm start _autosave   # restore previous session
```

#### Auto-Restore on tmux Start

Automatically restore the last `_autosave` snapshot when tmux starts:

```tmux
set -g @ccm-auto-restore "on"    # default: off
```

When enabled, ccm loads the `_autosave` snapshot via TPM on tmux startup (only if no ccm projects are already loaded).

### Idle Auto-Exit

Claude Code sessions that remain idle are automatically exited to free system resources. When you switch to an exited window, Claude Code restarts with `--continue` and resumes the conversation.

```tmux
set -g @ccm-idle-timeout "10"    # minutes (default: 10, 0 to disable)
```

### Dashboard Preview Panel

Show a live preview of the selected project's terminal content alongside the project list:

```tmux
set -g @ccm-preview "on"              # default: off
set -g @ccm-preview-position "right"  # or "bottom"
```

The preview updates when you move the cursor and refreshes automatically. ANSI colors (256-color and RGB) are rendered. Requires terminal width ≥ 80 columns (right position) or height ≥ 20 rows (bottom position). Can also be toggled from the dashboard menu (`m`).

### Auto-Start Claude Code

When you switch to a project window where Claude Code has exited (SHELL state), ccm automatically restarts it with `--continue` to resume the conversation.

```tmux
set -g @ccm-auto-start "on"     # default: on (set to "off" to disable)
```

Also configurable from the dashboard menu (`m`).

### Anti-Flicker

ccm automatically sets `CLAUDE_CODE_NO_FLICKER=1` to reduce UI flicker when running Claude Code inside tmux. No user configuration needed.

## Tips

### Make Claude Code Aware of Other Projects

By default, each Claude Code session is isolated and unaware of your other projects. You can change this by adding ccm commands to your global Claude Code instructions (`~/.claude/CLAUDE.md`):

```markdown
## Multi-Project Environment

This user manages multiple projects with ccm (Claude Code Manager for tmux).
Use the following commands to discover and inspect other projects:

- `ccm list` — List all managed projects (names and directories)
- `ccm status` — Show all project states (branch, port, Claude status)
- `ccm capture <name>` — Capture visible terminal output from another project
```

This lets every Claude Code session discover sibling projects and inspect their state — for example, checking how a library is used in another project, or reading another project's `CLAUDE.md`.

To set this up automatically:

```bash
ccm setup-claude-md     # add ccm section to ~/.claude/CLAUDE.md
ccm remove-claude-md    # remove it
```

### Organize Projects by Category with tmux Sessions

Use separate tmux sessions to group projects by context (e.g., work, OSS, research). ccm manages each session independently — dashboard and status bar only show that session's projects.

```bash
tmux new-session -s work       # Work projects
tmux new-session -s oss        # Open source projects

# In each session, add projects as usual:
ccm add ~/code/auth-service
ccm add ~/code/dashboard-ui

# Switch between sessions:
tmux switch-client -t oss      # Standard tmux session switching
```

> [!TIP]
> This pairs well with snapshots. Each session can save and restore its own project layout independently with `ccm snapshot save` / `ccm start`.

## How It Works

- Projects are tmux windows tagged with `@ccm_project` and `@ccm_dir`
- Claude Code state is detected via hook signals + process tree inspection (with prompt pattern matching as supplement)
- DONE state is auto-detected on BUSY/PERMIT → IDLE transitions (auto-clears after 30s)
- Works with any tmux theme — ccm auto-detects theme changes to status-right
- Git branch and port info are cached (30s) to minimize overhead
- Popup session context is passed via temp file (`$TMPDIR/ccm-$UID/`)

## Uninstall

1. Remove from `~/.tmux.conf`:
   ```tmux
   # Delete this line:
   set -g @plugin 'yohasebe/tmux-ccm'
   # Or if using source-file:
   # source-file ~/.tmux/plugins/tmux-ccm/ccm.tmux.conf
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

- **[User Guide](docs/guide.md)**
- **[日本語版 README](README.ja.md)** / **[ユーザーガイド](docs/guide.ja.md)**

## License

MIT
