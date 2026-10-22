"""Reading Claude Code's input box from a pane capture — pure, no I/O.

Where the composer is (the rules around it), what is typed in it (a
draft, or the dim next-prompt suggestion), whether it shows the start
of a message ccm typed, and whether the line above it says Claude Code
is holding a prompt for the sender to confirm. The send path and the
spool both ask these questions, and they are answered here only.
"""

import re

from ccm_constants import (
    PATTERN_INPUT_PROMPT, _PROMPT_CHARS, _SGR_PARAM_RE, strip_sgr,
)

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
#
# A named session draws its name into the upper rule
# (`\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 <name> \u2500`, seen with 2.1.304). Missing that rule moves
# the bracket up to an older rule in the transcript, where the
# just-submitted prompt then reads as a draft that never left. The
# name is accepted on either side of a run of at least ten.
PATTERN_COMPOSER_RULE = re.compile(
    r"^\s*(?:\u2500{10,}"
    r"|\u2500{10,}[ \u00a0]+\S.*?[ \u00a0]+\u2500+"
    r"|\u2500+[ \u00a0]+\S.*?[ \u00a0]+\u2500{10,})\s*$")

# How far above the bottom the closing rule may sit. Below it Claude
# Code draws only the status lines, so a rule further up than this
# belongs to the transcript (or to a dialog covering the composer)
# and brackets something that is not an input box.
COMPOSER_TAIL_WINDOW = 6

# The composer's first row: the prompt glyph at the left edge, then a
# space or nothing.
PATTERN_COMPOSER_FIRST_ROW = re.compile(rf"^[{_PROMPT_CHARS}](?:\s|$)")


def composer_draft_fragment(pane_text, pane_text_attributed=None):
    """A one-line fragment of the half-typed draft in the pane's
    composer, or None when the composer is bare — or absent.

    Absent means a dialog is covering it; None there is deliberate.
    The caller decides what an open dialog means from the detected
    state, and answering that question twice, in two places, is how
    the two answers start to disagree.

    `pane_text` is derived by stripping SGR from `pane_text_attributed`,
    a single `capture-pane -e` snapshot (SGR attributes kept). Claude Code draws a
    next-prompt SUGGESTION into the composer when a turn ends — the
    exact moment a send is meant to land — with the same `❯` line
    shape as a half-typed draft, but entirely in dim (SGR 2). A plain
    capture drops that attribute, so the suggestion read as a draft
    and refused (or spooled) the send; the suggestion vanishes on the
    first real keystroke, so refusing on it never protected anything.
    A fragment whose text is entirely dim is the suggestion, not a
    draft → None. Any non-dim character keeps the draft verdict (the
    safe side), and a missing / misaligned attribute capture falls
    back to the plain-text answer, unchanged from before this
    discriminator existed.
    """
    lines = _composer_lines(pane_text, pane_text_attributed)
    if not lines:
        return None
    fragment = lines[0].strip()
    return fragment[:60] + "..." if len(fragment) > 60 else fragment


def composer_visible(pane_text):
    """Positive evidence of an input prompt between the bottom rules."""
    lines = pane_text.rstrip().split("\n")
    edges = _composer_edges(lines)
    return (edges is not None
            and any(PATTERN_INPUT_PROMPT.match(line)
                    for line in lines[edges[0] + 1:edges[1]]))


def composer_has_message_prefix(pane_text, attributed, message, *, complete=False):
    """Match the unchanged beginning, never an arbitrary subsequence.

    Whitespace alone is folded to accommodate terminal wrapping. Short
    messages require equality; longer ones use 32 visible characters.
    With complete=True, require equality of the whole visible body.
    Sanitized prompts are handled separately by prompt_held_notice.
    """
    lines = _composer_lines(pane_text, attributed)
    if not lines or not message:
        return False
    first = lines[0].split(maxsplit=1)
    if len(first) != 2:
        return False
    draft = "".join((first[1] + "\n" + "\n".join(lines[1:])).split())
    body = "".join(message.split())
    if complete or len(body) < 8:
        return draft == body and bool(body)
    prefix = body[:32]
    return draft.startswith(prefix)


def _composer_edges(lines):
    """`(upper, lower)` rows of the rules around the composer, or None.

    The lower rule is the last one, near the bottom. The upper one is
    the nearest rule above it whose next row starts with the prompt
    glyph: the composer's first row always does, and a continuation
    row of a draft is indented, so a rule-shaped line typed into a
    draft is passed over. Without such a rule the second-last one is
    used."""
    rules = [i for i, ln in enumerate(lines) if PATTERN_COMPOSER_RULE.match(ln)]
    if len(rules) < 2 or len(lines) - rules[-1] > COMPOSER_TAIL_WINDOW:
        return None
    bottom = rules[-1]
    for top in reversed(rules[:-1]):
        if PATTERN_COMPOSER_FIRST_ROW.match(lines[top + 1]):
            return top, bottom
    return rules[-2], bottom


def _composer_lines(pane_text, pane_text_attributed=None):
    """The composer's own lines, from its draft line to the closing
    rule, or [] when the composer is bare, absent or showing the
    next-prompt suggestion. What a caller does with more than the
    first line is its business; finding the region is done here
    once."""
    lines = pane_text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    edges = _composer_edges(lines)
    if edges is None:
        return []
    top, bottom = edges
    for idx in range(top + 1, bottom):
        ln = lines[idx]
        if PATTERN_COMPOSER_DRAFT.match(ln):
            if pane_text_attributed is not None:
                att_lines = pane_text_attributed.split("\n")
                if (idx < len(att_lines)
                        and strip_sgr(att_lines[idx]) == ln
                        and _fragment_text_is_dim(att_lines[idx])):
                    return []
            return lines[idx:bottom]
    return []


# What Claude Code says when it has NOT taken a prompt it was given.
# Since 2.1.277 a prompt carrying characters it strips is rewritten
# without them and held for the sender to confirm, and the line
# saying so is drawn directly above the composer:
#
#   Removed 9 invisible characters · review and press Enter to send
#
# That line is the one thing on screen that says what happened.
# Reading it beats comparing the composer's text with the message,
# which cannot separate a prompt held back from a draft typed after
# the send landed: `reply only` is the message `Please reply only
# OK.` minus six characters, and a prompt Claude Code held after
# removing nine characters is no less held than one it removed one
# from. Both of those were sent as evidence by a reviewer, in
# opposite directions, against a comparison that tried.
#
# The count and the reason belong to upstream's wording and are left
# opaque; what is matched is the shape of the status line — a reason,
# the `·` separator, and the instruction to the sender closing the
# line. The instruction alone is not enough: a reply that happens to
# say `press Enter to send` lands in the same rows as the notice
# while it streams (measured: 46 of 100 samples of such a reply), and
# read as the notice it failed a send the session had taken. A
# rewording upstream, or a pane too narrow to show the line whole
# (at 60 columns 2.1.278 cuts it before the instruction), means ccm
# stops seeing the hold and reports as it did before this existed —
# the same direction every other missed reading takes here.
PATTERN_PROMPT_HELD = re.compile(
    r"\S.*\u00b7[^\u00b7\n]*\bpress Enter (?:again )?to send\s*$",
    re.IGNORECASE)

#: How far above the composer's opening rule the notice may sit.
#: Bounded because the same words in a transcript — a reply quoting
#: them, this paragraph on screen — are not the CLI saying it is
#: holding something.
HELD_NOTICE_LOOKBACK = 2


def prompt_held_notice(pane_text, pane_text_attributed=None):
    """The line where Claude Code says it is holding a prompt for the
    sender to confirm, or None when it is not saying that.

    A held prompt is IN the composer — that is what holding it means
    — so an empty composer is never a hold, whatever the rows above
    it say. The two conditions are asked together here, in one
    place, so that no caller can ask only one.

    A sender that reports success on having pressed Enter reports a
    delivery that did not happen. This is what the pane shows when
    that has happened, and nothing else on screen states it: the
    composer's own text says only that something is in it.

    None when the composer cannot be found (a dialog covers it, the
    capture failed), which leaves the caller reporting what it
    reported before.
    """
    lines = pane_text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    edges = _composer_edges(lines)
    if edges is None:
        return None
    if not _composer_lines(pane_text, pane_text_attributed):
        return None
    top = edges[0]
    for idx in range(max(0, top - HELD_NOTICE_LOOKBACK), top):
        if PATTERN_PROMPT_HELD.search(lines[idx]):
            return lines[idx].strip()
    return None


def _fragment_text_is_dim(attributed_line):
    """True when every visible character after the composer prompt
    glyph carries the dim attribute (SGR 2) — the signature of Claude
    Code's next-prompt suggestion, e.g.
    `\\x1b[39m❯ \\x1b[2mnow try …\\x1b[0m`. The glyph itself is chrome
    (not dim), so it and the whitespace after it are skipped first.
    Any non-dim visible character — a genuine draft, or one with a dim
    fragment mixed in — answers False: the safe side keeps calling it
    a draft."""
    dim = False
    past_prefix = False
    saw_text = False
    pos = 0

    def _scan(chunk):
        nonlocal past_prefix, saw_text
        for ch in chunk:
            if not past_prefix:
                if ch in _PROMPT_CHARS or ch.isspace():
                    continue
                past_prefix = True
            if not ch.isspace():
                saw_text = True
                if not dim:
                    return False
        return True

    for m in _SGR_PARAM_RE.finditer(attributed_line):
        if not _scan(attributed_line[pos:m.start()]):
            return False
        params = m.group(1).split(";")
        i = 0
        while i < len(params):
            p = params[i]
            i += 1
            if p in ("38", "48"):
                # Extended colour operands are not independent SGR attributes.
                if i >= len(params) or params[i] not in ("5", "2"):
                    return False
                count = 2 if params[i] == "5" else 4
                operands = params[i:i + count]
                if len(operands) != count or not all(v.isdigit() for v in operands):
                    return False
                i += count
                continue
            if p in ("", "0"):
                dim = False
            elif p == "2":
                dim = True
            elif p == "22":
                dim = False
        pos = m.end()
    if not _scan(attributed_line[pos:]):
        return False
    return saw_text
