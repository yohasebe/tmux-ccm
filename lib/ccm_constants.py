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
CCM_VERSION = "0.11.0"


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
# Sidekick attention markers, one JSON file per tmux pane
# (`<pane_id>.json`), written by `hooks/sidekick-attention.sh` from a
# sidekick CLI's OWN hook config (Kimi's `[[hooks]]`, etc.) — the
# CCM_IGNORE-style self-report bridge, keyed by the `$TMUX_PANE` the
# hook process inherits. A marker is OVERWRITTEN with
# `state: "resolved"` rather than deleted when the wait ends, so a
# consumer can tell "resolved" from "stale file"; ccm's reader
# is the garbage collector. Contract v1 fields:
#   agent / state / id / cwd / ts  (required)
#   session / summary / pane / tool / resolved_ts / expires (optional)
CCM_ATTENTION_DIR = os.path.join(CCM_TMP_DIR, "attention")


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
# Stale-BUSY release window, split from BUSY_HOOK_JSONL_WINDOW's 600 s.
# This is a FLICKER-PREVENTION window, not an estimate of the longest
# silent tool: the real safety net against reporting IDLE for a session
# that is still working lives on the auto-exit side (IDLE_EXIT_TIMEOUT
# requires 600 s of *sustained* IDLE before anything is killed), so
# 600 -> 60 only moves the worst-case kill threshold for a silent tool
# from 1200 s of silence to 660 s. What it buys is large: an
# Esc-interrupted turn fires no Stop hook and writes no further JSONL,
# so it used to sit in a false BUSY for ten minutes; it now clears in
# one. The release requires the CONJUNCTION of raw=IDLE (no ticking
# work clock on screen — a static footer does not count) and a frozen
# JSONL — neither alone is trusted, because a single long silent tool
# freezes the JSONL too, and spinner detection has broken before on
# upstream reworks (the accept-edits marker, the /model footer verb).
BUSY_STALE_RELEASE_SEC = int(
    os.environ.get("CCM_BUSY_STALE_RELEASE_SEC", "60"))
# How long an on-screen work clock (the spinner's elapsed-time
# footer) may stand still before its claim to a running turn stops
# being believed. The claim is what lets a childless pane — claude
# thinking or generating, nothing spawned — read BUSY, and raw=BUSY
# has no timeout anywhere in the pipeline, so the claim must limit
# itself: a frozen frame (claude hung after rendering the footer)
# and a transcript line merely quoting a footer are static, and past
# this window they stop holding the window busy. The clock has
# 1-second resolution and ticks every second while live, so 30 s of
# stillness (~15 consecutive identical reads at the dashboard's 2 s
# cadence) is unambiguous; the safety net for the false-IDLE
# direction remains auto-exit's IDLE_EXIT_TIMEOUT (600 s sustained).
SPINNER_STALE_RELEASE_SEC = int(
    os.environ.get("CCM_SPINNER_STALE_RELEASE_SEC", "30"))
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
# Synthetic `stop_reason` for an Esc-interrupted turn. Claude Code
# fires no Stop hook on a user interrupt (documented upstream), which
# is why detection long treated Esc as leaving no trace at all — but
# it DOES write a transcript record saying so, and that record is the
# only positive evidence the turn ended (measured: 8
# occurrences in one session). Treated as terminal so the existing
# "terminal stop_reason newer than the latest event → release"
# path in `ccm_activity` picks it up, instead of the session waiting
# out BUSY_STALE_RELEASE_SEC with an idle screen.
JSONL_INTERRUPTED = "interrupted"
# Anchored to the WHOLE record text, with the trailing clause left
# open. Three spellings are known — "[Request interrupted by user]",
# "[Request interrupted by user for tool use]" and a rare bare
# "[Request interrupted]" (the last from a consumer's corpus of ~165) — so
# the clause is exactly the detail that gets reworded and must not be
# pinned. What must NOT be loose is the anchoring: a substring test
# fires on any message that merely mentions the phrase, and a session
# discussing interrupts then reads as interrupted — a false IDLE,
# the dangerous direction, since `ccm send` would deliver into a
# working session and auto-exit could eventually kill it. Claude's
# own note is the entire content of its record, so requiring that
# separates it from every quotation of it. (Measured: a
# naive substring scan of this very session's transcript returned 41
# hits for 7 real interrupts. a consumer hit the same contamination.)
JSONL_INTERRUPT_RE = re.compile(r"^\[Request interrupted[^\]]*\]$")
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
# Attention-marker garbage collection. A `resolved` marker is kept
# this long so a slow consumer (a consumer's poll) can still see the
# resolution, then unlinked. A `waiting` marker is dropped past this
# hard TTL even when nothing resolved it — the safety net for an
# agent that died mid-wait without firing its resolution hook. Both
# are reader-side: the writers are per-CLI hook scripts that ccm does
# not control, so the ONE reader ccm owns is where cleanup can live.
ATTENTION_RESOLVED_GC_SEC = int(
    os.environ.get("CCM_ATTENTION_RESOLVED_GC_SEC", "300"))
ATTENTION_WAITING_TTL_SEC = int(
    os.environ.get("CCM_ATTENTION_WAITING_TTL_SEC", "3600"))


# ─── Claude Code UI patterns ───
# These are the ONLY place where Claude Code's terminal output is
# matched. If detection breaks after a Claude Code update, check
# these first. See: https://github.com/anthropics/claude-code

# Input prompt characters. The composer renders a NO-BREAK SPACE
# after the glyph, not an ordinary one, which is why
# patterns below match whitespace rather than a literal space.
# Where a wrapped line must not count, horizontal whitespace is
# spelled out instead (`[ \t\xa0]`).
_PROMPT_CHARS = "❯"
# Accept-edits prompt characters (doubled = accept-edits mode)
_ACCEPT_CHARS = "❯⏵"
PATTERN_INPUT_PROMPT = re.compile(rf"^[{_PROMPT_CHARS}]\s")
PATTERN_ACCEPT_EDITS = re.compile(rf"^\s*[{_ACCEPT_CHARS}]{{2}}")

# A composer line that carries text after the prompt character is a
# half-typed draft. State detection cannot see this: raw IDLE only
# requires `^❯\s`, which a composer holding text also satisfies. The
# send path matches this against the pane bottom and refuses to merge
# a message into a draft the user is still writing. Claude-only on
# purpose — it is the one TUI ccm tracks; sidekick TUIs are never
# pattern-matched (see EXTERNAL_AGENT_COMMANDS).
# `\s+`, not `\s*`: the composer always renders a space after the
# prompt glyph, and requiring it keeps the doubled-glyph mode line of
# older builds (`❯❯ accept edits on`) from reading as a draft.
PATTERN_COMPOSER_DRAFT = re.compile(rf"^[{_PROMPT_CHARS}]\s+\S")

# The rules Claude Code draws above and below its input box. The
# composer is what sits BETWEEN the last two of them, and finding it
# that way is the whole point: a submitted prompt is rendered into
# the transcript carrying the same `❯` glyph, so scanning the pane
# top-down and taking the first hit reads the user's previous message
# as a draft — and refuses every send for as long as one is on
# screen. That shipped, and cost a queued message its whole TTL.
PATTERN_COMPOSER_RULE = re.compile(r"^\s*\u2500{10,}\s*$")

# How far above the bottom the closing rule may sit. Below it Claude
# Code draws only the status lines, so a rule further up than this
# belongs to the transcript (or to a dialog covering the composer)
# and brackets something that is not an input box.
COMPOSER_TAIL_WINDOW = 6


def composer_draft_fragment(pane_text):
    """A one-line fragment of the half-typed draft in the pane's
    composer, or None when the composer is bare — or absent.

    Absent means a dialog is covering it; None there is deliberate.
    The caller decides what an open dialog means from the detected
    state, and answering that question twice, in two places, is how
    the two answers start to disagree.
    """
    lines = pane_text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    rules = [i for i, ln in enumerate(lines) if PATTERN_COMPOSER_RULE.match(ln)]
    if len(rules) < 2 or len(lines) - rules[-1] > COMPOSER_TAIL_WINDOW:
        return None
    for ln in lines[rules[-2] + 1:rules[-1]]:
        if PATTERN_COMPOSER_DRAFT.match(ln):
            fragment = ln.strip()
            return fragment[:60] + "..." if len(fragment) > 60 else fragment
    return None

# Active-work spinner footer. Claude Code renders a status line of
# the shape `<glyph> <verb>… (<elapsed> · <arrow> <N>k tokens)` ONLY
# while it is actively generating or running a tool — e.g.
#   "✻ 処理中… (27m 26s · ↓ 28.5k tokens)"
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
# Empirically verified: running panes show the spinner,
# AskUserQuestion menu waits do not. See memory
# project_false_idle_long_tool.md.
#
# Elapsed forms: "59s", "2m 2s", or "3h 11m 16s". Token forms: "8.0k",
# "8k", or — below 1000 — a bare count with NO k suffix ("557 tokens";
# observed, a project: a fresh turn streamed for 1m39s showing
# "(1m 39s · ↓ 557 tokens)" and the then-mandatory `k` made the
# spinner invisible to raw detection, so an accept-edits pane sat at
# false IDLE through the whole sub-1k window).
#
# The hour component is the same lesson a second time: a turn past
# the hour mark rendered
# "(3h 11m 16s · ↓ 8.8k tokens)" and the minutes-only form stopped
# matching, so the ONE piece of direct on-screen evidence that Claude
# is working went dark for the rest of that turn). Each unit is
# independently optional rather than a fixed "h m s" shape, because
# nothing guarantees Claude Code keeps printing a zero-valued unit.
# The segments after the elapsed time are read as opaque. Within one
# turn the footer passes through several shapes — a bare verb, then
# `(2s · thinking with max effort)`, then
# `(5s · ↓ 25 tokens · thought for 3s)`, then `(7s · ↓ 380 tokens)` —
# and a pattern anchored on `tokens)` matched only the last of them,
# leaving the pane reading IDLE while Claude was plainly working.
# What separates a running turn from the finished line beside it
# (`Crunched for 8s`) is the parenthesised elapsed time, not what
# follows it, so that is all this asks for.
#
# The closing paren is optional: a narrow pane clips the footer at
# the pane's right edge (`(5s · ↓ 25 tok`), and ccm's own sidekick
# layout splits windows into panes narrow enough to do that.
# End of line therefore closes the match as well as `)`. The
# finished line still fails — it has no opening paren at all.
PATTERN_ACTIVE_SPINNER = re.compile(
    r"\((?:\d+h\s+)?(?:\d+m\s+)?\d+s\s*·[^)\n]*(?:\)|$)"
)
# Connection / rate-limit retries REPLACE the spinner footer with a
# line of their own — measured against an unreachable endpoint:
#
#   ✻ Connection refused — a firewall or proxy may be blocking it (ConnectionRefused) · Retrying in 3s · attempt 8/10
#
# (75 samples over 188 s and 10 attempts: zero matches for the
# spinner form; the footer does not come back between attempts.)
# While it waits, the session has no child process, writes no JSONL,
# and shows nothing else that moves — so without this line a long
# backoff reads as idle, the reading auto-exit acts on.
#
# Only the countdown is asked for: the tail clips in narrow panes
# (measured at 100 columns the line ended `attemp…`), and the
# countdown is also the clock — it ticks once a second, which is
# what lets the staleness check treat the line like the spinner.
#
# The units are optional the same way the spinner's elapsed units
# are: backoff grows with every attempt, so a seconds-only pattern
# would stop matching exactly the long backoffs this exists for.
# (`Retrying in 1m 5s` is unobserved as yet — the risk is in the
# shape, and the duration formatter is shared with the footer,
# which does render minutes.) Decimal seconds are not matched:
# unobserved anywhere upstream, and the footer's own elapsed is
# integer seconds.
PATTERN_RETRY_BACKOFF = re.compile(
    r"Retrying in (?:\d+h\s+)?(?:\d+m\s+)?\d+s"
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
# on a WebFetch permission raised by a background
# subagent, and again on the Claude-in-Chrome navigation dialog,
# whose deny line is just `Deny (esc)`). The deny label varies by
# dialog, so it is matched as a negative word (`No,` / `Deny` /
# `Decline`) rather than any one wording — fixing the exact string
# is how the browser dialog was missed.
# Two anchors keep this specific to a real
# dialog: the leading `\d+\.` (an actual numbered option line) AND
# a trailing inline `(esc)`. The `(esc)` is what disambiguates the
# live dialog from PROSE that merely quotes the option text — e.g.
# a Claude response (including this very conversation about the
# detector) writing "3. No, and tell Claude what to do differently
# is the deny option" would otherwise false-trigger PERMIT. The
# footer'd permission dialogs are already matched by the
# "Esc to cancel · …" alternative above, so requiring `(esc)` here
# costs nothing for them and only tightens the footer-less case.
# The deny option of a footer-less permission dialog, shared by the
# footer match and the modal classifier so the two cannot drift
# apart. A numbered line, optionally carrying the selection cursor
# (arrowing onto the deny line rewrites it as `❯ 3. Deny (esc)`,
# which must not flip the match off — nor fall through to
# PATTERN_INPUT_PROMPT and read as an idle prompt), starting with a
# negative word and ending in the inline `(esc)`. `Decline` is
# unobserved — cheap insurance, not a measurement.
_DENY_OPTION_LINE = (
    r"(?:❯\s*)?\d+\.\s*(?:No\b|Deny\b|Decline\b)[^\n]*\(esc\)"
)

PATTERN_PERMIT_FOOTER = re.compile(
    r"^\s*(?:"
    r"Esc to cancel\s*(?:·|\|)\s*(?:Tab to amend|ctrl\+e to explain)"
    r"|Enter to \S[^\n]*?\s*(?:·|\|)\s*[^\n]*?\bEsc to \w+"
    r"|" + _DENY_OPTION_LINE +
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
# The age expression takes whatever units it needs — `45m old` under the
# hour, `3h 11m old` over it, `2d 4h old` for a session resumed days
# later, which `--continue` invites. Spelled as a repeated unit rather
# than a fixed pair after the same mistake was found in
# PATTERN_ACTIVE_SPINNER: requiring `\d+h \d+m` silently
# excluded every session younger than an hour. Days are worth allowing
# here even though the spinner ignores them — a single turn does not
# span days, but a resumable session easily does.
PATTERN_RESUME_MODAL = re.compile(
    r"This session is (?:\d+[dhm]\s+)+old"
    r"|Resume from summary \(recommended\)"
)
PATTERN_PERMISSION_DIALOG = re.compile(
    r"Do you want to proceed\?"
    # Horizontal whitespace only: a wrapped transcript line that ends
    # in this phrase is a quotation, not a live dialog, and must not
    # classify as one.
    r"|Do you want to allow Claude to[ \t\xa0]"
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
    r"|" + _DENY_OPTION_LINE
)
# The folder-trust prompt. Its footer is the same `Enter to confirm ·
# Esc to cancel` a safe picker uses, so without a content signature it
# classified as a harmless confirmation — and the guidance `ccm send`
# prints would have invited an operator to dismiss it from another
# pane. Answering it grants read, edit and execute in that directory,
# which is a permission grant wearing a confirmation's clothes.
PATTERN_TRUST_MODAL = re.compile(
    r"Yes, I trust this folder"
    r"|Is this a project you created or one you trust\?"
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
        "Permission dialog (DANGEROUS —\n"
        "do NOT attempt to dismiss from another pane).\n"
        "User action required: switch to the target pane and\n"
        "respond to the prompt yourself. ccm refuses to send\n"
        "keystrokes here because they could accidentally approve\n"
        "or deny the requested action."
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
    elif PATTERN_TRUST_MODAL.search(pane_text):
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

# Foreground commands (`tmux #{pane_current_command}`) of external
# agent CLIs — non-claude agent sessions a user runs in a sidekick
# pane of a registered window. Display-only presence signal: panes
# matching this set surface a dim `⚙<name>` badge (dashboard /
# `ccm status` / status bar mode 2) so the session is visible
# without ccm tracking its state. Deliberately an allowlist rather
# than "any non-shell foreground": parked editors / pagers (vim,
# less, …) would be pure noise. Detection, the state machine, and
# hooks never read this — a matching pane still aggregates as
# SHELL (no claude), which is honest: SHELL means "no claude".
#
# `claude` must never appear here. This set marks panes ccm shows but
# does NOT track; the Claude pane is the one it does track, and listing
# it would have a single pane claim both at once — the exact asymmetry
# assets/sidekick-model.svg exists to draw.
EXTERNAL_AGENT_COMMANDS = frozenset({
    # Measured: this is what tmux reports for a running Kimi Code pane.
    "kimi", "kimi-code",
    # Assumed from each CLI's binary name — not verified against a running
    # pane, because none of them are installed here. The asymmetry makes
    # guessing safe: a name that never appears simply never matches, while
    # a correct guess starts working the day the user installs the tool.
    # If one of these turns out to report something else, the fix is to add
    # the real name, not to remove the guess.
    # Codex installed through npm may run behind a node shim, in which
    # case tmux reports `node` and no badge appears; a brew/installer
    # build reports `codex`. `node` is far too broad to allowlist, so
    # this is a debugging note rather than something to fix here.
    "codex",    # OpenAI Codex CLI
    "gemini",   # Google Gemini CLI
    "grok",     # xAI Grok Build — see the prefix note below
})

# Some CLIs are a launcher symlink pointing at a platform-suffixed
# binary, and tmux reports the RESOLVED name (truncated): Grok Build's
# `grok` resolves to `grok-macos-aarch64` and arrives as
# `grok-macos-aarc` (measured). Enumerating every
# platform/arch spelling — and guessing tmux's truncation width — is
# the fixed-shape mistake this project keeps paying for, so a matching
# prefix stands in for the whole family. Prefixes must stay specific
# enough that no unrelated program collides: `grok-` qualifies,
# a bare `grok` would also swallow `grokking-notes`.
EXTERNAL_AGENT_PREFIXES = ("grok-",)


def external_agent_name(command):
    """Canonical agent name for a pane's foreground command, or "" if
    it is not a known external agent CLI.

    Returns the SHORT name, so display and marker records stay stable
    across a platform-suffixed binary: `grok-macos-aarc` → `grok`.
    Single source of truth for "is this pane an agent?" — the badge,
    the SHELL-row note and the attention reader all route through it,
    so a new spelling is fixed in exactly one place."""
    if not command:
        return ""
    if command in EXTERNAL_AGENT_COMMANDS:
        return command
    for prefix in EXTERNAL_AGENT_PREFIXES:
        if command.startswith(prefix):
            return prefix.rstrip("-")
    return ""

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
#
# `JSONL_INTERRUPTED` is ccm's own synthesis rather than an upstream
# value, and belongs here for the same reason the real ones do: an
# Esc-interrupted turn has ended just as definitively as one that ran
# to `end_turn`. Including it means the release paths that already
# key on "the transcript says this turn is over" cover Esc too,
# instead of each needing its own interrupt branch.
TERMINAL_STOP_REASONS = frozenset({
    "end_turn", "max_tokens", "stop_sequence", JSONL_INTERRUPTED,
})
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
