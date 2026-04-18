# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-04-18

Initial public release.

### Added
- Window-based project management (`ccm add/open/remove/attach/list/register/unregister/rename`)
- 4-state detection model (PERMIT/BUSY/IDLE/SHELL) via Claude Code hooks + multi-layer fallback:
  - 14 hook events (7 scripts): UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart/Stop, PreCompact/PostCompact, Stop/StopFailure, PermissionRequest, PermissionDenied, Notification (permission_prompt / elicitation_dialog / idle_prompt), SessionEnd
  - JSONL session-log heartbeat with system-record filtering (recap / away_summary safe)
  - Process grandchild detection (foreground tool execution)
  - Permission dialog footer match (capture-pane fallback)
  - Canaries: hooks.log bloat, disableAllHooks, allowManagedHooksOnly, cluster-SHELL (#48069)
- ✔ completion marker: display-layer "recently completed" icon (30s after BUSY/PERMIT→IDLE, cosmetic only — not a detection state)
- Interactive dashboard (`prefix + Tab`) with live status, preview panel, add/remove/rename/save
- Live incremental filter search (`/` in dashboard or `prefix + /` via `@ccm-key-search`). Unicode-safe
- Interactive tree view (`prefix + T`) with session/window/pane hierarchy
- Interactive menu (`prefix + C`) for settings management
- `ccm send` — cross-project prompt injection with state-gated safety (PERMIT hard guard)
- Three status bar modes (`@ccm-status-line` 0/1/2) with theme compatibility
- Desktop notifications (`@ccm-notify`) with instant hook delivery, sound options, per-project dedup markers (concurrent projects never suppress each other's notifications), and a grace window (`CCM_COMPLETION_GRACE_SEC`) that absorbs the Stop hooks Claude Code fires at multi-turn tool boundaries so the alert only arrives on a genuine completion
- Snapshot save/load/list/delete with `_autosave` on `ccm stop --all`
- Auto-start Claude Code on window switch; auto-exit idle sessions after 10 minutes
- Git branch (with dirty indicator) and listening TCP port detection per project
- Cluster-SHELL transition canary for macOS silent-exit regression (anthropics/claude-code#48069)
- `ccm setup-hooks` / `ccm remove-hooks` with version-gated elicitation_dialog matcher
- `ccm setup-claude-md` / `ccm remove-claude-md` for `~/.claude/CLAUDE.md` integration
- `ccm init` interactive setup wizard
- Zsh completion
- Bilingual documentation (English / Japanese)
- 274 pytest tests + bats hook tests
