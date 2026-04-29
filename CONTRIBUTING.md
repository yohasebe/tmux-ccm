# Contributing to ccm

Thanks for considering a contribution. This is a small project with a focused scope; the notes below describe how to get a change in efficiently.

## Getting set up

```bash
git clone https://github.com/yohasebe/tmux-ccm.git
cd tmux-ccm
```

Test prerequisites:

```bash
# Python — pytest is the only runtime dep
pip install pytest

# bash — bats-core for shell-side tests
brew install bats-core   # or: apt-get install bats
```

## Running tests

```bash
# Python (detection logic, dashboard, status bar — 600+ cases)
python3 -m pytest tests/ -v

# bash (hook setup + notify parity)
bats tests/
```

The full suite runs in under a second on modern hardware. PRs must keep both green.

## Code organisation

- `ccm` — bash dispatcher CLI
- `lib/ccm_core.py` — constants, tmux/ps helpers, data model, dispatch, JSONL/hook signal I/O
- `lib/ccm_detection.py` — state-detection engine (event-log path + minimal legacy fallback)
- `lib/ccm_commands.py` — `cmd_*` subcommand handlers
- `lib/ccm_render.py` — terminal-output formatters (`print_*`, ANSI colours)
- `lib/dashboard.py` — curses TUI
- `lib/inject_status.py` — tmux status bar updater
- `lib/state_meta.sh` — bash icon table (kept in sync with Python `STATE_ICONS`)
- `hooks/` — Claude Code hook scripts that write event log + signal files
- `tests/` — pytest + bats suites

`docs/state-machine.md` is the formal reference for the detection pipeline (state model, event-log decision tree, legacy fallback table, time-window heuristics, lifecycle walk-throughs). Update it when changing detection.

## Documentation

- `README.md` (English) and `README.ja.md` (Japanese) must stay in sync — same structure, same content.
- `docs/guide.md` (English) and `docs/guide.ja.md` (Japanese) likewise.
- Add an entry to `CHANGELOG.md` under `[Unreleased]` for any user-visible change.

## Claude Code release tracking

Each new Claude Code release can shift detection assumptions (modal footer wording, hook payload shape, JSONL record schema). When you update Claude Code locally:

1. Pull the verbatim CHANGELOG bullets for the new version (do not rely on a summary — footer wording changes are easy to miss in summaries).
2. Sample-check the most common modals — `/skills`, `/model`, `/hooks`, the permission prompt — with `tmux capture-pane -p` and verify they still classify correctly.
3. If a modal's footer wording changed, update `PATTERN_PERMIT_FOOTER` in `lib/ccm_core.py` and the corresponding fixture in `tests/test_ccm_core.py`.
4. If a JSONL housekeeping record type was added, no code change is required — the activity-type whitelist (`JSONL_ACTIVITY_TYPES`) in `lib/ccm_core.py` rejects unknown types by default.

## Dev-only flags

These are not user-facing — they exist for screenshots, debugging, and test instrumentation. Do not document them in user-facing READMEs:

- **`@ccm-mock-state`** (tmux option) / **`CCM_MOCK_STATE=1`** (env var) — forces fast-path detection that reads only `@ccm_prev_state`. Useful for screenshots where you want a stable BUSY/PERMIT/IDLE icon mix without running real Claude Code sessions. Skips ps/capture-pane entirely.
- **`CCM_DEBUG_TRACE=<path>`** — JSONL trace of every slow-path detection scan. Set via `tmux set-environment -g`, not shell `export`, so the tmux-spawned subprocesses see it.
- **`CCM_TRACE_ONLY_DIFF=1`** — restricts the above to scans where the legacy and event-log derivations disagree. Lets long-running traces stay small.
- **`CCM_USE_EVENT_LOG=off`** — diagnostic kill-switch for the event-log path; forces legacy `DETECTION_RULES` only.

## Pull requests

- Keep PRs focused on one change. Refactors and behaviour changes go in separate PRs.
- Include a test case for any detection change.
- Match existing comment style: state what the code does and why; don't narrate the change history (that's what the commit log is for).

## Reporting issues

Useful information to include:

- ccm version (`git log -1 --oneline`)
- Claude Code version (`claude --version`)
- tmux version (`tmux -V`)
- macOS / Linux distribution
- For state-detection bugs: a `ccm debug trace <project>` capture covering the misbehaviour
