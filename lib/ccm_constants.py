"""Pure constants and pattern utilities — no I/O, no subprocess.

Submodules import values directly from here without pulling in
`ccm_core` (and its import-time work — sys.modules registration,
body definitions). Keeping pure constants in their own module
breaks the circular dependency between `ccm_core` and the
detection / command submodules.

Contents:
  - Runtime path constants (`CCM_TMP_DIR`, `CCM_HOOK_DIR`, …)
  - Detection thresholds (`HOOK_FRESH_THRESHOLD`,
    `BUSY_HOOK_JSONL_WINDOW`, `STARTUP_GRACE_SEC`, …)
  - Claude Code UI patterns (`PATTERN_INPUT_PROMPT`,
    `PATTERN_PERMIT_FOOTER`, …)
  - PERMIT modal classification + the `classify_permit_modal`
    pure function
  - Process-tree constants (`CLAUDE_PROCESS_NAME`,
    `IGNORED_CHILDREN`, `SHELL_FOREGROUND_COMMANDS`)
  - Display / state metadata (`STATE_ICONS`, `STATE_PRIORITY`)
  - Hook script filenames + Claude launch command

When changing detection patterns or hook script names, this is the
single source of truth — `lib/state_meta.sh` mirrors the
state-icon table for the bash hot path, but everything else lives
here.
"""

import os
import re


# Version string. Keep in sync with the `CCM_VERSION` constant in
# the bash `ccm` wrapper and with CHANGELOG.md's top entry.
CCM_VERSION = "0.6.0"


# ─── Runtime paths ───

CCM_ROOT = os.environ.get(
    "CCM_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CCM_TMP_DIR = os.environ.get(
    "CCM_TMP_DIR",
    os.path.join(os.environ.get("TMPDIR", "/tmp"), f"ccm-{os.getuid()}"),
)
CCM_HOOK_DIR = os.environ.get("CCM_HOOK_DIR", os.path.join(CCM_TMP_DIR, "hooks"))
_default_data_dir = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "ccm",
)
CCM_DATA_DIR = os.environ.get("CCM_DATA_DIR", _default_data_dir)
CCM_SNAPSHOT_DIR = os.environ.get(
    "CCM_SNAPSHOT_DIR", os.path.join(CCM_DATA_DIR, "snapshots")
)
CCM_GIT_CACHE_DIR = os.path.join(CCM_TMP_DIR, "git-cache")
CCM_PORT_CACHE_DIR = os.path.join(CCM_TMP_DIR, "port-cache")
# Per-project markers (md5-of-cwd keyed) written by
# `hooks/lib.sh::_ccm_instant_notify` and read by inject_status
# polling so the Python side does not re-fire a notification the
# bash hook already sent. Must stay in sync with the `marker_dir`
# resolver in `hooks/lib.sh`.
CCM_NOTIFY_MARKER_DIR = os.path.join(CCM_TMP_DIR, "notified")


# ─── Detection thresholds (tunable via env vars) ───

# Display-layer "recently completed" marker timeout.
COMPLETED_AT_TIMEOUT = int(os.environ.get("CCM_COMPLETED_AT_TIMEOUT", "30"))
# Hook signal age (seconds) below which a BUSY signal is treated as
# "fresh" and trusted unconditionally — bypasses the slower pipeline
# when multiple projects contend for evaluation time.
HOOK_FRESH_THRESHOLD = 2
# Window for trusting a BUSY hook signal without JSONL corroboration.
# A BUSY hook older than this AND a JSONL silent for the same
# duration is almost certainly left over from a turn that completed
# without a Stop hook firing (anthropics/claude-code#25655 class).
# Past this window, the rule table stops trusting the hook and falls
# through to the raw=IDLE fallback path so the state can eventually
# drop out of BUSY. Default 10 minutes — long enough to cover real
# thinking phases that legitimately lack tool activity, short enough
# that a missed Stop does not strand the project in BUSY indefinitely.
BUSY_HOOK_JSONL_WINDOW = int(os.environ.get("CCM_BUSY_HOOK_JSONL_WINDOW", "600"))
PERMIT_MAX_TIMEOUT = int(os.environ.get("CCM_PERMIT_MAX_TIMEOUT", "600"))
IDLE_EXIT_TIMEOUT = int(os.environ.get("CCM_IDLE_EXIT_TIMEOUT", "600"))
CACHE_TTL = int(os.environ.get("CCM_CACHE_TTL", "30"))  # git/port cache seconds
# Hook-vs-real-activity gap discriminator. A BUSY hook fired more
# than this many seconds AFTER the last real conversation activity
# is treated as a phantom hook (no surrounding real work) — the
# upstream `away_summary` recap fires a BUSY-class hook with no
# corresponding Stop, and this guard rejects it. In real long-
# thinking, hook_age and real_activity_age grow together (gap ~0);
# in recap, the hook is brand new while real_activity is minutes
# old (gap >> threshold). Lives here (not in `ccm_jsonl`) so
# `ccm_activity` and `ccm_render` can read it without forming an
# import cycle through the `ccm_jsonl → ccm_core → ccm_commands →
# ccm_detection → ccm_activity` chain.
JSONL_HOOK_GAP_TOLERANCE = int(
    os.environ.get("CCM_JSONL_HOOK_GAP_TOLERANCE", "60")
)
# Synthetic `stop_reason` value emitted by `ccm_jsonl.read_jsonl_tail_info`
# when the latest JSONL record is a user prompt newer than the previous
# assistant's terminal stop_reason — i.e. "user submitted, claude has
# not yet started writing assistant tokens". Lives here (not in
# `ccm_jsonl`) so `ccm_rules` can reference it at module-load time
# without forming an import cycle.
JSONL_USER_PENDING = "user_pending"
# How long after the `claude` process starts a `raw=BUSY` reading
# is treated as MCP-loading startup rather than real work, when no
# hook signal has been written yet. MCP server initialization
# typically finishes within 10–30 s, so 60 s is a conservative cap.
# After the grace expires the startup_transient rule stops firing
# and detection falls back to the `raw_busy_passthrough` rule — so
# a Claude that actually hangs during startup will surface as BUSY.
STARTUP_GRACE_SEC = int(os.environ.get("CCM_STARTUP_GRACE_SEC", "60"))
# Minimum pane height (in tmux rows) for a pane to contribute to
# the window's aggregated state. Panes shorter than this cannot
# reliably render Claude's `❯` prompt + accept-edits indicator +
# footer; capture-pane–based prompt detection silently fails and
# the pane falsely reads BUSY (has children, no prompt visible).
SLIVER_HEIGHT_THRESHOLD = int(os.environ.get("CCM_SLIVER_HEIGHT_THRESHOLD", "4"))


# ─── Claude Code UI patterns ───
# These are the ONLY place where Claude Code's terminal output is
# matched. If detection breaks after a Claude Code update, check
# these first. See: https://github.com/anthropics/claude-code

# Input prompt characters (single character followed by space = idle prompt)
_PROMPT_CHARS = "❯"
# Accept-edits prompt characters (doubled = accept-edits mode)
_ACCEPT_CHARS = "❯⏵"
PATTERN_INPUT_PROMPT = re.compile(rf"^[{_PROMPT_CHARS}]\s")
PATTERN_ACCEPT_EDITS = re.compile(rf"^\s*[{_ACCEPT_CHARS}]{{2}}")

# Active-work spinner footer. Claude Code renders a status line of
# the shape `<glyph> <verb>… (<elapsed> · <arrow> <N>k tokens)` ONLY
# while it is actively generating or running a tool — e.g.
#   "✻ フェーズ7仕上げ中… (27m 26s · ↓ 28.5k tokens)"
#   "✶ Spelunking… (59s · ↑ 3.5k tokens)"
# The animating glyph and the incrementing elapsed timer are what
# the user sees "blinking". We match the structural tail — the
# parenthesised `(elapsed · arrow Nk tokens)` group — NOT the glyph
# (which cycles through many characters and changes between
# releases) and NOT the verb (localised / arbitrary). The arrow +
# `Nk tokens` counter is effectively never present in normal
# conversation text, so the false-positive risk is limited to a
# response that literally quotes this exact footer format (e.g. a
# conversation about this very detector); such a case self-corrects
# on the next turn and only matters in the permit-stuck window.
#
# WHY this signal exists: in accept-edits mode the `❯` composer
# stays on screen WHILE a tool runs, so `detect_pane_state` would
# read raw=IDLE during active execution. When an approved
# permission is the latest hook event, that false IDLE makes the
# event-log derive return a stuck PERMIT (the dashboard shows ⚠ for
# a session that is actively working). This spinner is the ONLY
# signal that distinguishes "approved tool running" (spinner
# present) from "menu / permission wait" (spinner absent — Claude
# has stopped generating to ask) and "true idle" (spinner absent).
# Empirically verified 2026-06-11: running panes show the spinner,
# AskUserQuestion menu waits do not. See memory
# project_false_idle_long_tool.md.
#
# Elapsed forms: "59s" or "2m 2s". Token forms: "8.0k", "8k", or —
# below 1000 — a bare count with NO k suffix ("557 tokens"; observed
# 2026-07-22, wp2txt: a fresh turn streamed for 1m39s showing
# "(1m 39s · ↓ 557 tokens)" and the then-mandatory `k` made the
# spinner invisible to raw detection, so an accept-edits pane sat at
# false IDLE through the whole sub-1k window).
PATTERN_ACTIVE_SPINNER = re.compile(
    r"\((?:\d+m\s+)?\d+s\s*·\s*[↑↓]\s*[\d.]+k?\s+tokens\)"
)
# Modal-dialog footer markers. Matches any Claude Code UI that is
# blocked awaiting a user keypress response. Observed forms:
#
#   - "Esc to cancel · Tab to amend"          (permission dialog)
#   - "Esc to cancel · ctrl+e to explain"     (permission dialog alt)
#   - "Enter to confirm · Esc to cancel"      (session-resume modal)
#   - "Enter to confirm · Esc to exit"        (/model picker, pre-v2.1.144)
#   - "Enter to confirm · d to set as default for new sessions · Esc to cancel"
#                                             (/model picker, v2.1.144–v2.1.152)
#   - "Enter to set as default · s to use this session only · Esc to cancel"
#                                             (/model picker, v2.1.153+)
#   - "3. No, and tell Claude what to do differently (esc)"
#                                             (footer-less permission dialog —
#                                              WebFetch / web-content prompts,
#                                              subagent permission requests:
#                                              the deny option carries an inline
#                                              `(esc)` instead of a separate
#                                              "Esc to cancel · …" footer line)
#
# All map to the PERMIT state because semantically Claude is
# blocked pending a single user action — the UX is the same as a
# permission prompt. A fifth "MODAL" state would split hairs
# without benefit.
#
# The verb after "Enter to" varies per modal AND per version
# (confirm / set as default / ... — Claude Code upstream renames
# freely between releases). The discriminator that actually
# matters is the `·` (or `|`) separator structure: blocking
# modals describe each key with `Key to <action>` segments joined
# by `·`, while free-navigation slash menus (/skills, /resume from
# v2.1.144+, ...) join their hints with commas. Matching on that
# structure instead of a literal verb keeps the regex resilient
# against upstream wording drift without enumerating every
# possible verb. Esc-verb after `Esc to` is also permissive
# (`\w+`) for the same reason — cancel / exit / close / quit /
# dismiss have all been observed.
#
# Anchored at line start (after optional whitespace) so the same
# words inside a Claude response — e.g. "use ctrl+e to explain" in
# answer text, or a code example containing "Enter to confirm" —
# do not falsely trigger PERMIT. The bare "Esc to cancel" line
# used by slash menus (/hooks, /config, /skills, /resume from
# v2.1.144 onwards, ...) deliberately does NOT match: those menus
# are free navigation with type-to-search / preview keys, not a
# blocked single-decision modal.
#
# re.MULTILINE: the pattern is consumed two ways — per-line
# `.match(line)` in detect_pane_state (unaffected by the flag) and
# whole-tail `.search(pane_text)` in classify_permit_modal, where
# the footer sits on the LAST line of a multi-line capture. Without
# MULTILINE the `^` anchor only matches at position 0 of the joined
# string, so the classifier's footer fallback silently never fired
# and unrecognized confirm modals fell through to "unknown-permit"
# instead of "confirmation-modal".
# The third alternative matches the deny option of a permission
# prompt that has no separate "Esc to cancel · …" footer (observed
# 2026-06-26 on a WebFetch permission raised by a background
# subagent: `Do you want to allow Claude to fetch this content?`
# with the `(esc)` carried inline on `No, and tell Claude what to
# do differently (esc)`). Two anchors keep this specific to a real
# dialog: the leading `\d+\.` (an actual numbered option line) AND
# a trailing inline `(esc)`. The `(esc)` is what disambiguates the
# live dialog from PROSE that merely quotes the option text — e.g.
# a Claude response (including this very conversation about the
# detector) writing "3. No, and tell Claude what to do differently
# is the deny option" would otherwise false-trigger PERMIT. The
# footer'd permission dialogs are already matched by the
# "Esc to cancel · …" alternative above, so requiring `(esc)` here
# costs nothing for them and only tightens the footer-less case.
PATTERN_PERMIT_FOOTER = re.compile(
    r"^\s*(?:"
    r"Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
    r"|Enter to \S[^\n]*?\s*(?:·|\|)\s*[^\n]*?\bEsc to \w+"
    r"|\d+\.\s*No,\s*and tell Claude what to do differently[^\n]*\(esc\)"
    r")",
    re.MULTILINE,
)


# ─── PERMIT modal classification ───
# Content-level signatures (not just the footer) used by
# `classify_permit_modal()` to distinguish safe modals
# (session-resume, /model picker, /exit confirmation) from
# dangerous permission dialogs. Order inside
# `classify_permit_modal()` matters — check the most specific
# signatures first.
#
# We match these against the full captured tail, not just the
# footer line, because the footer alone is ambiguous: both
# session-resume and /model use `Enter to confirm · Esc to …`.
PATTERN_RESUME_MODAL = re.compile(
    r"This session is \d+h \d+m old"
    r"|Resume from summary \(recommended\)"
)
PATTERN_PERMISSION_DIALOG = re.compile(
    r"Do you want to proceed\?"
    r"|Do you want to allow Claude to "
    r"|Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
    # Footer-less permission dialogs (WebFetch / web-content,
    # subagent permission requests) ask "Do you want to allow
    # Claude to <fetch this content|run …|…>?" and carry the deny
    # option with an inline `(esc)`. Classify these as the
    # dangerous permission-request kind (not a safe confirmation
    # modal) so `ccm send` warns the operator not to dismiss them
    # from another pane. The trailing `(esc)` is required for the
    # same reason as in PATTERN_PERMIT_FOOTER — it keeps prose that
    # merely quotes the option text from mis-classifying.
    r"|\d+\.\s*No,\s*and tell Claude what to do differently[^\n]*\(esc\)"
)
PATTERN_MODEL_PICKER = re.compile(
    r"Switch between Claude models"
    r"|Select (?:a )?model"
)


# Guidance strings are kept close to the classifier so they evolve
# together when Claude Code adds a new modal kind. Multi-line so
# `cmd_send` can print them verbatim under the refusal header.
_PERMIT_GUIDANCE = {
    "session-resume": (
        "claude --continue resume picker (safe — no side effects).\n"
        "User action required: switch to the target pane, then:\n"
        "  - Enter         → Resume from summary (recommended)\n"
        "  - ↓, Enter      → Resume full session as-is\n"
        "  - ↓×2, Enter    → Don't ask me again\n"
        "  - Esc           → Cancel resume (session won't start)"
    ),
    "permission-request": (
        "Permission dialog for a tool invocation (DANGEROUS —\n"
        "do NOT attempt to dismiss from another pane).\n"
        "User action required: switch to the target pane and\n"
        "respond to the prompt yourself. ccm refuses to send\n"
        "keystrokes here because they could accidentally approve\n"
        "or deny a tool call."
    ),
    "confirmation-modal": (
        "Confirmation modal (e.g., /model picker, /exit).\n"
        "Safe to dismiss but requires a user decision.\n"
        "User action required: switch to the target pane and\n"
        "respond per the footer keys — Enter and Esc are always\n"
        "present, plus any per-modal extras (e.g. v2.1.153+\n"
        "/model picker offers `s` to set the model for the\n"
        "current session only)."
    ),
    "unknown-permit": (
        "Unrecognized PERMIT modal. Treat as dangerous by default.\n"
        "User action required: switch to the target pane and\n"
        "inspect the dialog before responding. If this is a new\n"
        "Claude Code modal, the classifier in ccm_constants.py\n"
        "needs an additional signature pattern."
    ),
}


# ─── `claude agents` TUI signature ───
# The agent view TUI (Claude Code 2.1.139+, opened via `claude agents`
# or `← ←` detach from a session) shows an `❯`-style input prompt at
# the bottom — which ccm's `PATTERN_INPUT_PROMPT` reads as IDLE, so
# the surrounding window appears send-able. But unlike a regular
# Claude REPL, typing into the agents TUI dispatches a BRAND-NEW
# agent-view session rather than landing in an existing conversation.
# A `ccm send` to that pane would silently spawn an unintended session
# (Issue 5 in `project_agent_view_findings_2026_05_12`).
#
# `cmd_send` checks `PATTERN_AGENTS_FOOTER` against the captured pane
# tail and refuses on match, with a tailored guidance message.
#
# Footer shape observed on v2.1.139+:
#   "enter to open · space to reply · ctrl+x to delete · ? for shortcuts"
#
# The "enter to open" prefix + "? for shortcuts" suffix together are
# specific enough to the TUI that a permissive `.*?` between them is
# safe — Claude response text containing those phrases at line start
# is implausible, and the footer line lives at the captured-pane tail
# (the only place `cmd_send` looks). IGNORECASE absorbs upstream
# wording case shifts; MULTILINE lets `^` match any line so multi-row
# capture works without splitting per-line.
PATTERN_AGENTS_FOOTER = re.compile(
    r"^\s*enter to open\b.*?\bfor shortcuts\b",
    re.IGNORECASE | re.MULTILINE,
)


def is_agents_tui(pane_text) -> bool:
    """True when the captured pane shows the `claude agents` TUI
    footer. `cmd_send` uses this to refuse sending into the TUI,
    because keystrokes there spawn a fresh agent-view session
    instead of landing in an existing conversation.

    Defensively reject non-string input (None, MagicMock from
    mocked-without-return_value tests, etc.) — the matcher would
    otherwise raise TypeError on the first regex call and the
    safest interpretation of "I cannot read the pane" is False
    (the same as "pane doesn't look like TUI"), which lets the
    send proceed and matches the legacy behaviour."""
    if not isinstance(pane_text, str) or not pane_text:
        return False
    return bool(PATTERN_AGENTS_FOOTER.search(pane_text))


def classify_permit_modal(pane_text: str):
    """Classify a PERMIT-state pane by content signature.

    Returns `(category, guidance)` where category is one of:
      - "session-resume"     — claude --continue resume picker
      - "permission-request" — tool permission dialog (dangerous)
      - "confirmation-modal" — /model picker, /exit, … (safe)
      - "unknown-permit"     — none of the above matched

    `pane_text` is the full captured tail (lines joined with
    newlines). Order matters: the more specific the signature, the
    earlier we check. Permission dialog is checked before the
    generic confirm footer so a permission dialog never falls
    through as a safe confirmation-modal.
    """
    if PATTERN_PERMISSION_DIALOG.search(pane_text):
        cat = "permission-request"
    elif PATTERN_RESUME_MODAL.search(pane_text):
        cat = "session-resume"
    elif PATTERN_MODEL_PICKER.search(pane_text):
        cat = "confirmation-modal"
    elif PATTERN_PERMIT_FOOTER.search(pane_text):
        # Footer has the "Enter to … · Esc to …" blocking-modal
        # shape but no content-level signature matched — treat as
        # a generic safe confirmation rather than unknown.
        # Permission dialogs are caught above, so what remains is
        # almost always a /<slash> confirm or a not-yet-cataloged
        # confirm modal.
        cat = "confirmation-modal"
    else:
        cat = "unknown-permit"
    return cat, _PERMIT_GUIDANCE[cat]


# ─── Process-tree constants ───

CLAUDE_PROCESS_NAME = "claude"
# Processes that are always children of Claude Code and should be
# ignored when checking for meaningful child processes (tool
# execution).
IGNORED_CHILDREN = {"caffeinate"}
# Foreground commands (`tmux #{pane_current_command}`) that
# indicate the pane is at a shell prompt — claude may exist
# somewhere in the process tree but is not the active foreground
# process (e.g. user did Ctrl-Z + new shell). Used to override the
# process-tree heuristic (which would otherwise return BUSY for
# the lingering claude pid). Editors / pagers (vim, less, etc.)
# are intentionally NOT in this set — those mean the user is
# actively doing something, even if not in claude, and ccm's
# auto-start should not fire over them.
SHELL_FOREGROUND_COMMANDS = frozenset({
    "zsh", "bash", "sh", "fish", "ksh", "csh", "tcsh", "dash", "ash",
})

# Interactive programs that sit idle indefinitely without doing work —
# editors and pagers a user parks in a split pane next to Claude.
# Auto-exit's background-work guard treats a non-shell sibling-pane
# foreground as "live work"; these are the exception. Two facts make
# the exemption safe:
#   1. ACTIVE use of an editor/pager produces pane output (screen
#      redraws), which refreshes `window_activity` and resets the
#      idle timer — so the guard is never needed to protect a pane
#      the user is actually touching.
#   2. Exiting Claude leaves the sibling pane untouched (separate
#      process in a separate pane) — unlike the batch-job case, there
#      is nothing to orphan or interrupt.
# Autonomous work (batch jobs, dev servers, `tail -f`) stays guarded:
# it can be silent for >timeout while still mattering. Editors with a
# long-running internal job (`:make`) are an accepted edge — the job's
# output refreshes window_activity in practice.
PARKED_FOREGROUND_COMMANDS = frozenset({
    "vim", "nvim", "vi", "view", "emacs", "nano", "pico",
    "less", "more", "man", "bat",
})

CLAUDE_CMD = "claude --continue 2>/dev/null || claude"


# ─── Hook scripts + state metadata ───

# Hook script filenames (single source of truth for
# `hooks_configured` checks).
HOOK_SCRIPTS = [
    "on-prompt-submit.sh",
    "on-stop.sh",
    "on-pre-tool-use.sh",
    "on-notification.sh",
    "on-permission-request.sh",
    "on-permission-denied.sh",
    "on-session-end.sh",
]

STATE_PRIORITY = {"PERMIT": 0, "BUSY": 1, "IDLE": 2, "SHELL": 3, "DOWN": 4}

# JSONL `stop_reason` values that mean "Claude's response truly ended"
# (versus `"tool_use"` which means "paused mid-response for a tool").
# Single source of truth for both the legacy rule table
# (`ccm_rules.DETECTION_RULES`, via the tuple form for declarative
# fields) and the event-log derive path (`ccm_activity`, via the
# frozenset for fast `in` checks).
TERMINAL_STOP_REASONS = frozenset({"end_turn", "max_tokens", "stop_sequence"})
TERMINAL_STOP_REASONS_TUPLE = tuple(sorted(TERMINAL_STOP_REASONS))
# Detection state icons. Keep in sync with `lib/state_meta.sh` —
# bash hooks pay a ~50 ms cost per Python cold start, so we cannot
# just shell out to Python to resolve icons; the bash side has its
# own copy in `ccm_state_icon`. Update BOTH when adding / changing
# a state icon. The extra "COMPLETED" key used by notification
# paths is bash-only (not a detection state).
STATE_ICONS = {
    "PERMIT": "⚠", "BUSY": "◉", "IDLE": "●", "SHELL": "■", "DOWN": "○",
}


# ─── Permission mode display ───
# Claude Code sends `permission_mode` as an optional common field on
# every hook payload; `hooks/lib.sh` copies it onto each event record
# and the display layer surfaces the newest value as a per-project
# badge. Modes that auto-resolve dialogs (auto / dontAsk /
# bypassPermissions) never fire PermissionRequest, so "no PERMIT ever
# shows up" is normal for them — the badge exists to preempt that
# misdiagnosis. Display-only: the state model never reads the mode.
#
# Payload value "default" is what the CLI calls `manual`
# (`--permission-mode manual`; the payload keeps the legacy name), so
# the badge renders the CLI vocabulary users actually type and see in
# Claude Code's own footer ("manual mode on").
PERMISSION_MODE_LABELS = {
    "default": "manual",
    "acceptEdits": "accept",
    "plan": "plan",
    "auto": "auto",
    "dontAsk": "dontAsk",
    "bypassPermissions": "bypass",
}
# Modes rendered in warning colour — every guardrail is off.
PERMISSION_MODE_WARN = frozenset({"bypassPermissions"})


def permission_mode_label(mode: str) -> str:
    """Short badge label for a payload `permission_mode` value.
    Unknown (future) modes pass through length-capped so a new
    upstream mode stays visible without waiting for a ccm release."""
    if not mode:
        return ""
    return PERMISSION_MODE_LABELS.get(mode, mode[:10])
