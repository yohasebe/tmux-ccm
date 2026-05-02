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
# Modal-dialog footer markers. Matches any Claude Code UI that is
# blocked awaiting a user keypress response. Observed forms:
#
#   - "Esc to cancel · Tab to amend"          (permission dialog)
#   - "Esc to cancel · ctrl+e to explain"     (permission dialog alt)
#   - "Enter to confirm · Esc to cancel"      (session-resume modal)
#   - "Enter to confirm · Esc to exit"        (/model picker)
#
# All map to the PERMIT state because semantically Claude is
# blocked pending a single user action — the UX is the same as a
# permission prompt. A fifth "MODAL" state would split hairs
# without benefit.
#
# The Esc-verb after "Enter to confirm" varies per modal author
# (cancel / exit observed so far; Claude Code upstream is not
# consistent). `Esc to \w+` is intentionally permissive for the
# confirm-modal branch — the `Enter to confirm` prefix is strong
# enough that false-positive risk is negligible, and this future-
# proofs against new modals that pick yet another verb (close,
# quit, dismiss, ...).
#
# Anchored at line start (after optional whitespace) so the same
# words inside a Claude response — e.g. "use ctrl+e to explain" in
# answer text, or a code example containing "Enter to confirm" —
# do not falsely trigger PERMIT. The bare "Esc to cancel" line
# used by slash menus (/hooks, /config, /skills, ...) deliberately
# does NOT match: those menus are free navigation, not a blocked
# decision.
PATTERN_PERMIT_FOOTER = re.compile(
    r"^\s*(?:"
    r"Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
    r"|Enter to confirm\s*(?:·|\|)\s*Esc to \w+"
    r")"
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
    r"|Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
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
        "press Enter to confirm or Esc to cancel."
    ),
    "unknown-permit": (
        "Unrecognized PERMIT modal. Treat as dangerous by default.\n"
        "User action required: switch to the target pane and\n"
        "inspect the dialog before responding. If this is a new\n"
        "Claude Code modal, the classifier in ccm_constants.py\n"
        "needs an additional signature pattern."
    ),
}


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
        # Footer says "Enter to confirm · Esc to ..." but no
        # content-level signature matched — treat as a generic safe
        # confirmation rather than unknown. Permission dialogs are
        # caught above, so what remains is almost always a /<slash>
        # confirm or a not-yet-cataloged confirm modal.
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
