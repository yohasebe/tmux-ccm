"""`ccm send` — cross-pane prompt delivery to another project's
Claude Code session.

This is the most state-aware of the ccm commands and the only one
that mutates a *different* project's pane via `send-keys`. The
state-based gating policy is the safety story:

  - **IDLE / DONE**: send immediately
  - **BUSY**: refuse without `--force`. With `--force`, the message
    queues into Claude's input buffer (mixes with the current turn,
    user must want this)
  - **PERMIT**: ALWAYS refuse — typing into a permission dialog
    could accidentally approve or deny a tool call. The classifier
    in `ccm_constants.classify_permit_modal` distinguishes
    permission dialogs from safer modals (session-resume, /model
    picker) but the refusal is still unconditional; the
    classification only shapes the guidance message
  - **SHELL**: refuse without `--start`. With `--start`, launch
    Claude (`claude --continue ...`) and poll until the target
    reaches IDLE before sending. A fixed wait would mis-deliver
    the message when `claude --continue` triggers a follow-on
    action — most commonly an auto `/compact` on resume of a
    long session, or a session-resume picker (PERMIT modal) —
    because the keystrokes would land mid-modal and be eaten or
    queued into a dialog. Polling refuses with the captured pane
    tail so the operator can see exactly what happened and finish
    by hand

The gating snapshot can go stale while the interactive confirmation
prompt blocks, so the delivery pane's raw state is re-checked via
`detect_pane_state` immediately before typing (`_recheck_delivery_state`)
— a target that transitioned to PERMIT / SHELL / BUSY in the meantime
aborts the send instead of receiving the body.

Multi-line messages (`\\n` in body) are converted to `M-Enter`
between lines + a final `Enter`, matching Claude Code's "newline
without submit" key convention so a multi-line prompt arrives as
one turn rather than several.

Delivery-pane resolution: the project state is a WINDOW-level
aggregation across panes (PERMIT > BUSY > IDLE > SHELL), but
`send-keys -t <session>:<idx>` delivers to the window's ACTIVE
pane — which in a split window may be a plain shell sitting next
to the Claude pane. incident: a two-pane window
(Claude idle in pane A, active zsh in pane B) aggregated to IDLE,
so a `ccm send --start` decided no launch was needed and typed the
whole message into zsh. `_resolve_delivery_pane` closes the gap by
locating the pane that actually hosts the claude process and
targeting keystrokes (and captures) at that pane id instead of the
window.

The companion `ccm sidekick-send` (bottom of this file) delivers to a
NON-Claude sidekick pane of the caller's own window instead. There is
no state to gate on (ccm deliberately tracks no state for an external
agent), so its safety story is identity: it finds the sidekick pane
from tmux metadata, refuses every ambiguity, and confirms delivery by
capture. Both commands share the typing helpers (`_type_body`,
`_send_keys`) so the literal-send details exist exactly once.
"""

import os
import sys
import time

import ccm_core  # late-bound for tmux_cmd / build_project_list / die / etc.
import ccm_spool  # store-and-forward queue for undeliverable sends
from ccm_constants import (
    CLAUDE_CMD,
    composer_draft_fragment,
    SHELL_FOREGROUND_COMMANDS,
    external_agent_name,
)
from ccm_pane_state import detect_pane_state, enumerate_window_panes


# ─── Opt-in send trace ───
# `CCM_SEND_TRACE=1 ccm send ...` appends a per-keystroke log to
# `$CCM_TMP_DIR/send-trace.log`. Used to diff sender vs receiver
# when an operator reports "ccm send dropped content" — the trace
# captures exactly what tmux send-keys saw, so we can rule the ccm
# layer in or out without round-tripping diagnostic round-trips.
#
# Zero overhead when the env var is unset: a single dict lookup
# per call on the message-delivery loop. The trace file is best-
# effort (silent OSError) so a non-writable log directory cannot
# block a send.
def _trace_enabled():
    return os.environ.get("CCM_SEND_TRACE", "").lower() in ("1", "true", "yes", "on")


def _trace_record(win_target, label, keys):
    """Append one send-keys event. Format is tab-separated:
        <unix-ts>\t<win_target>\t<label>\t<key1> <key2> ...
    Keys are repr'd so embedded whitespace / control chars survive
    a later `cut`/`awk` pass."""
    try:
        path = os.path.join(ccm_core.CCM_TMP_DIR, "send-trace.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        keys_repr = " ".join(repr(k) for k in keys)
        with open(path, "a") as f:
            f.write(f"{time.time():.3f}\t{win_target}\t{label}\t{keys_repr}\n")
    except OSError:
        pass  # diagnostic logging must never block the send


def _send_keys(win_target, *keys, label=""):
    """tmux send-keys wrapper with optional CCM_SEND_TRACE logging.
    When trace is disabled (the default), this is equivalent to a
    direct `ccm_core.tmux_cmd("send-keys", ...)` call with one extra
    dict lookup. When enabled, the call is recorded first, then
    delegated to tmux normally."""
    if _trace_enabled():
        _trace_record(win_target, label, keys)
    ccm_core.tmux_cmd("send-keys", "-t", win_target, *keys)


# Maximum seconds to wait for a `--start`-launched target to
# reach IDLE before refusing. Tuned for two real scenarios:
#
#   - Normal resume (no auto-action): 1-5 s typical to IDLE.
#   - Resume into auto-`/compact` (long session): 10-60+ s in
#     BUSY. No reasonable wait gets the message delivered, so we
#     refuse early and let the operator verify by hand.
#
# 10 s comfortably covers the first case while refusing the
# second within a useful response time. Override with
# CCM_START_WAIT_SEC if your environment routinely needs more.
START_WAIT_SEC = int(os.environ.get("CCM_START_WAIT_SEC", "10"))

# How often the wait loop reports progress. The loop polls more
# frequently (so IDLE is caught quickly) but only prints when the
# tick interval has elapsed, so the operator gets a sign-of-life
# every second without log spam.
_WAIT_PROGRESS_TICK_SEC = 1.0


def _wait_for_target_idle(project_name, timeout_sec=None,
                          progress=False):
    """Poll the named project until its detected state is IDLE,
    or return the last observed non-IDLE state at timeout.

    Used by the `--start` path so the message-send only proceeds
    once the target is genuinely at the input prompt — not still
    initialising MCP servers, not in an auto-`/compact`, not on a
    session-resume picker. Polls every 0.5 s up to `timeout_sec`
    (default `START_WAIT_SEC`).

    Returns either the string `"IDLE"` (success), or the last
    non-IDLE state seen (one of `BUSY` / `PERMIT` / `SHELL` /
    `DOWN`). PERMIT and DOWN short-circuit the wait — they will
    not transition to IDLE without operator action, so further
    polling is wasted time and delays the refusal message that
    the operator needs.

    With `progress=True` the function prints one short status line
    per second while waiting, so an operator running an
    interactive `ccm send --start` knows something is happening
    rather than staring at a frozen terminal until the timeout.
    """
    if timeout_sec is None:
        timeout_sec = START_WAIT_SEC
    started = time.time()
    deadline = started + timeout_sec
    last_state = None
    last_progress = started
    while time.time() < deadline:
        time.sleep(0.5)
        projects = ccm_core.build_project_list(fast=False)
        target = next((p for p in projects if p.name == project_name), None)
        if target is None:
            return "DOWN"
        last_state = target.state
        if last_state in ("IDLE", "PERMIT", "DOWN"):
            return last_state
        now = time.time()
        if progress and now - last_progress >= _WAIT_PROGRESS_TICK_SEC:
            elapsed = now - started
            ccm_core.ccm_info(
                f"  [waiting {elapsed:.1f}s] state={last_state}"
            )
            last_progress = now
    return last_state or "BUSY"


# ─── Post-launch delivery verification (--start path) ───
# `--start` launches `claude --continue` into a SHELL pane, then
# waits for IDLE before sending. But the `❯` composer becomes
# visible (so detection reads IDLE) a moment BEFORE Claude's input
# handler actually accepts keystrokes — during that window a
# send-keys is silently eaten, the body never lands, yet ccm
# printed "Sent". Confirmed (a project project):
# target SHELL → --start → "Sent" shown but the input box held only
# its placeholder, body zero; re-sending after IDLE settled
# delivered the full text.
#
# Fix: after typing the body (but BEFORE the committing Enter),
# verify a signature of the message actually appears in the pane.
# If not, clear the composer and re-type, up to a few times with a
# short settle. Only send Enter once the body is confirmed present,
# so a premature-IDLE drop is caught and retried instead of
# silently lost — and Enter-after-verify means a retry can never
# double-submit. Scoped to the launch path; an already-IDLE target
# (normal send) was genuinely ready and is left on the fast path.
_DELIVERY_VERIFY_RETRIES = 2
_DELIVERY_VERIFY_SETTLE_SEC = 1.0
# Minimum signature length to verify against. A very short message
# (< this) cannot be matched in the pane without false positives,
# so verification is skipped for it (rare for delegation messages).
_DELIVERY_SIG_MIN_LEN = 8


def _message_signature(message):
    """Return distinctive substrings of `message` to look for in the
    target's input box, or None when the message is too short to
    verify reliably.

    Returns a tuple, and a match on ANY of them counts, because a
    composer holding a long body shows only part of it. Claude's grows
    upward and keeps the leading row; a body that outgrows the pane
    scrolls instead and keeps the trailing row (observed
    against Kimi K3: a 30-line message rendered as `↑ 24 more` with the
    head cut off). Checking one end only would report "did not land"
    for a message sitting right there, which on the --start path means
    re-typing a body that already arrived and then refusing the send.
    So take a candidate from each end."""
    candidates = [ln.strip() for ln in message.split("\n") if ln.strip()]
    if not candidates:
        return None
    # Cap each signature so a long line that wraps in the composer
    # still matches on its visible head.
    sigs = []
    for group in (candidates[:5], candidates[-5:]):
        sig = max(group, key=len)[:40]
        if len(sig) >= _DELIVERY_SIG_MIN_LEN and sig not in sigs:
            sigs.append(sig)
    return tuple(sigs) or None


def _body_landed(win_target, signatures):
    """True iff ANY of `signatures` appears in the target pane's
    visible text — i.e. the typed body reached the `❯` composer.
    Captures the whole visible area (the composer grows upward for
    multi-line input, so a tail-only read can miss the leading row).
    See `_message_signature` for why either end may be the one on
    screen."""
    cap = ccm_core.tmux_cmd("capture-pane", "-t", win_target, "-p") or ""
    if not cap.strip():
        cap = ccm_core.tmux_cmd(
            "capture-pane", "-a", "-t", win_target, "-p"
        ) or ""
    # Compare with whitespace removed from both sides. A composer
    # wraps the body to its own width, and the break lands wherever
    # the width falls — mid-word, or mid-sentence in a language that
    # writes without spaces. A signature that straddles the break is
    # then absent as a substring while sitting there in plain view,
    # and the send reports a failure that did not happen. A caller
    # who is told "not delivered" about a delivered message either
    # sends it twice or stops believing the check.
    flat = "".join(cap.split())
    return any("".join(sig.split()) in flat for sig in signatures)


def _type_body(win_target, lines):
    """Type the message body into the target's composer: each
    non-empty line literally, with `M-Enter` (newline-without-submit)
    between lines. Does NOT send the committing Enter — the caller
    decides when (and whether) to submit.

    The `--` terminator before the line text is load-bearing: tmux's
    argument parser treats any argument starting with `-` as a flag
    cluster, so without it every line beginning with a dash — most
    commonly a Markdown bullet (`- item`) — failed the whole
    send-keys call with "invalid flag" and was SILENTLY DROPPED
    (stderr is swallowed by tmux_cmd), while the surrounding
    M-Enters still landed. The receiver saw the message with all
    its bullet lines missing and a blank line where each had been.
    This mangled three real cross-project briefs before being
    diagnosed (design replies/11 arriving
    with empty design/implementation sections; the contract brief
 arriving with an empty slug section) — the delivery
    verification did not catch it because the signature it checks
    survived in the non-bullet lines."""
    for line_i, line in enumerate(lines):
        if line:
            _send_keys(win_target, "-l", "--", line, label=f"line:{line_i}")
        if line_i < len(lines) - 1:
            _send_keys(win_target, "M-Enter", label=f"newline:{line_i}")


# ─── Delivery-pane resolution ───
# Window state aggregates across panes, but send-keys to a window
# target lands in the ACTIVE pane. In a split window those can
# disagree: Claude idle in a side pane makes the window IDLE while
# the active pane is a bare zsh — and the message would be typed
# into the shell (incident: `ccm send --start` saw
# IDLE, skipped the launch, and flooded zsh with the body as shell
# commands). Resolving the actual claude-hosting pane and targeting
# it directly makes state and delivery refer to the same pane.

def _resolve_delivery_pane(win_target):
    """Return `(pane_target, active_cmd)` for the pane that should
    receive the keystrokes.

      - When one or more panes host a claude process: the active
        pane if it hosts claude, else the single claude pane.
        Multiple claude panes with a non-claude active pane is
        ambiguous (Agent Teams split) → refuse via ccm_die.
      - When NO pane hosts claude (SHELL window): the active pane —
        that is where `--start` will launch Claude.

    `active_cmd` is the active pane's `pane_current_command` (used
    by the --start path to verify it is really a shell before
    typing the launch command), or None when pane enumeration
    failed and we fell back to the window target."""
    ps_lines = ccm_core.ps_snapshot().strip().split("\n")
    panes = enumerate_window_panes(win_target, ps_lines)
    if not panes:
        # Defensive fallback: pane enumeration failed (tmux error).
        # Window target preserves pre-resolution behaviour.
        return win_target, None

    # A CCM_IGNORE'd pane is not a delivery candidate: never the claude
    # target, and never the "active" pane either (so a hidden sidekick
    # can't capture the send).
    live = [p for p in panes if not p.ignored]
    active = next((p for p in live if p.active), None)
    active_pane = active.pane_id if active else None
    active_cmd = active.current_command if active else None
    claude_panes = [p.pane_id for p in live if p.claude_pid]

    if not claude_panes:
        return (active_pane or win_target), active_cmd
    if active_pane in claude_panes:
        return active_pane, active_cmd
    if len(claude_panes) == 1:
        return claude_panes[0], active_cmd
    # The second line is the only place ccm volunteers CCM_IGNORE, and
    # deliberately so. A standing hint ("this window has two claude
    # panes — hide one") would fire for Agent Teams too, where hiding a
    # teammate costs you its PERMIT: advice that harms if followed.
    # Here the reader has already hit the ambiguity, and hiding really
    # does resolve it — an ignored pane drops out of `live` above, so
    # the remaining claude pane becomes the unique target.
    ccm_core.ccm_die(
        f"Multiple panes in {win_target} host a claude process "
        f"({', '.join(claude_panes)}) and the active pane is not one "
        "of them — the delivery target is ambiguous.\n"
        "  Switch focus to the pane that should receive the message, "
        "then retry.\n"
        "  If one of them is a sidekick rather than a teammate, hiding "
        "it from ccm resolves this for good — start it with "
        "`CCM_IGNORE=1`, or press `i` on it in the dashboard."
    )


def _recheck_delivery_state(win_target, pane_target):
    """Re-detect the delivery pane's raw state immediately before
    typing (TOCTOU guard).

    The state gate in `cmd_send` runs on a `build_project_list`
    snapshot, and the interactive confirmation prompt can block for
    any length of time — the target may transition to PERMIT (or
    exit to SHELL) while the operator reads the preview. Typing on
    the stale verdict would inject the message into a permission
    dialog, breaking the "never type into PERMIT" safety story.
    Re-running `detect_pane_state` on the delivery pane closes the
    window with the same detection logic the rest of ccm uses; the
    cost is one ps snapshot + one list-panes + up to two capture-pane
    calls, negligible for a user-invoked command.

    Returns the raw state, or None when the pane can no longer be
    enumerated (tmux hiccup — includes the case where delivery
    resolution already fell back to the window target). None fails
    OPEN: refusing on a transient tmux error would break sends that
    worked before this guard existed."""
    ps_lines = ccm_core.ps_snapshot().strip().split("\n")
    pane = next(
        (p for p in enumerate_window_panes(win_target, ps_lines)
         if p.pane_id == pane_target),
        None,
    )
    if pane is None:
        return None
    return detect_pane_state(
        pane.pane_pid, pane.pane_id, ps_lines, str(os.getpgrp()),
        current_command=pane.current_command,
    )


# ─── Composer-draft guard ───
# State detection cannot see a half-typed draft: the raw IDLE check
# matches `^❯\s`, which a composer already holding text satisfies too.
# So a send that arrives while the user is mid-sentence merges into
# their draft, and the committing Enter submits the garbled mix.
# The delivery path therefore reads the composer line itself and
# refuses while a draft is present. This shrinks the mixing race from
# "the whole IDLE period" to the capture→send-keys gap (~100 ms);
# that residual TOCTOU cannot be closed from outside the TUI and is
# accepted. Fail-OPEN when no composer line is visible at all: a
# transient capture error must not break sends that worked before
# this guard existed — the same call `_recheck_delivery_state` makes.
def _composer_draft_fragment(pane_target):
    """Return a one-line fragment of the draft in the target pane's
    composer, or None when the composer is bare (or unreadable).

    Which line is the composer is the whole question — see
    `composer_draft_fragment`, which both this and the spool's
    delivery check call so they cannot answer it differently."""
    cap = ccm_core.tmux_cmd("capture-pane", "-t", pane_target, "-p") or ""
    if not cap.strip():
        cap = ccm_core.tmux_cmd(
            "capture-pane", "-a", "-t", pane_target, "-p"
        ) or ""
    return composer_draft_fragment(cap)


_SEND_USAGE = (
    "Usage: ccm send <name|#idx> <message> "
    "[--file path] [--stdin|-] [--force] [--start] [--now] [--no-enter] [-y|--yes]\n"
    # `--` is the only way to send a message that starts with a dash,
    # and a user who needs it is by definition looking at a message
    # ccm just tried to parse as flags — so it belongs in the usage
    # line they are about to be shown, not only in the guide.
    "       ccm send <name> -- <message starting with a dash>"
)


# ─── Spool hand-off ───
# A target that cannot take the message right now (BUSY / PERMIT /
# SHELL / agents-TUI / a composer holding a draft) no longer fails
# the send by default: the message is queued for the reconciler's
# delivery pass (see ccm_spool) and the sender is told. `--now`
# restores the old fail-fast behaviour for callers that would rather
# retry themselves. Permanent errors — ambiguous target, self-send,
# unregistered window — still refuse immediately: they do not heal
# with time, so queueing them would only hide the mistake.

def _sender_label():
    """Best-effort identity of the calling window's project, for the
    spool envelope's `from:` — which is also the receiver's reply
    route, so the project name is the useful value."""
    caller = os.environ.get("TMUX_PANE", "")
    if not caller:
        return "unknown"
    win = (ccm_core.tmux_cmd(
        "display-message", "-p", "-t", caller, "#{window_id}") or "").strip()
    if not win:
        return "unknown"
    name = (ccm_core.tmux_cmd(
        "show-option", "-w", "-t", win, "-qv", "@ccm_project") or "").strip()
    if name:
        return name
    name = (ccm_core.tmux_cmd(
        "display-message", "-p", "-t", win, "#{window_name}") or "").strip()
    return name or "unknown"


def _queue_message(project_name, message, reason):
    """Spool the message for the reconciler's delivery pass and say
    so — never a silent exit 0, since "queued" and "delivered" are
    different facts and the sender planned around one of them."""
    sender = _sender_label()
    msg_id, n = ccm_spool.enqueue(project_name, sender, message)
    ttl_min = max(1, ccm_spool.SPOOL_TTL_SEC // 60)
    ccm_core.ccm_info(
        f"Queued for {project_name} ({reason}; {n} pending, "
        f"TTL {ttl_min}m, id {msg_id}).\n"
        f"  Inspect: `ccm spool list` · withdraw: "
        f"`ccm spool cancel {msg_id} {project_name}`"
    )


def cmd_send(args):
    """Send a prompt to a project's Claude Code session.

    Usage:
      ccm send <name|#idx> <message>       Send literal message + Enter
      ccm send <name> --file <path>        Read message from file
      ccm send <name> --stdin              Read message from stdin
      ccm send <name> --no-enter <msg>     Send without submitting
      ccm send <name> --force <msg>        Send to a BUSY project (queued)
      ccm send <name> --start <msg>        Auto-launch Claude if SHELL
      ccm send <name> --now <msg>          Fail instead of spooling when
                                           the target cannot take it now
      ccm send -y <name> <msg>             Skip confirmation prompt
      ccm send <name> -- "--literal"       `--` ends flag parsing
    """
    if any(a in ("-h", "--help") for a in args):
        print(_SEND_USAGE)
        return
    target = None
    positional_parts = []
    message_file = None
    use_stdin = False
    no_enter = False
    force = False
    auto_start = False
    now = False
    skip_confirm = False

    stop_flags = False
    i = 0
    while i < len(args):
        arg = args[i]
        if not stop_flags and arg == "--":
            stop_flags = True
            i += 1
            continue
        if not stop_flags and arg.startswith("-") and arg != "-":
            if arg == "--file":
                i += 1
                if i >= len(args):
                    ccm_core.ccm_die("--file requires a path argument")
                message_file = args[i]
            elif arg == "--stdin":
                use_stdin = True
            elif arg == "--no-enter":
                no_enter = True
            elif arg == "--force":
                force = True
            elif arg == "--start":
                auto_start = True
            elif arg == "--now":
                now = True
            elif arg in ("-y", "--yes"):
                skip_confirm = True
            else:
                ccm_core.ccm_die(f"Unknown flag: {arg}\n{_SEND_USAGE}")
        else:
            if arg == "-":  # conventional stdin alias
                use_stdin = True
            elif target is None:
                target = arg
            else:
                positional_parts.append(arg)
        i += 1

    if not target:
        ccm_core.ccm_die(_SEND_USAGE)

    # Resolve message source (exactly one of the three)
    positional_message = " ".join(positional_parts) if positional_parts else None
    source_count = sum(x is not None and x is not False for x in
                       (positional_message, message_file, use_stdin or None))
    if source_count == 0:
        ccm_core.ccm_die("No message provided (positional, --file, or --stdin)")
    if source_count > 1:
        ccm_core.ccm_die(
            "Provide exactly one of: positional message, --file, or --stdin"
        )

    if message_file:
        try:
            with open(message_file, encoding="utf-8") as f:
                message = f.read()
        except OSError as e:
            ccm_core.ccm_die(f"Failed to read message file: {e}")
    elif use_stdin:
        message = sys.stdin.read()
        # Once we have consumed stdin, the interactive confirmation
        # prompt can no longer read from it (EOFError). Force-skip
        # confirmation so a TTY user running `ccm send demo --stdin`
        # and typing a body terminated by Ctrl-D is not silently
        # cancelled.
        skip_confirm = True
    else:
        message = positional_message

    if not message.strip() and not no_enter:
        ccm_core.ccm_die(
            "Empty message (use --no-enter to send only Enter suppression)"
        )

    # Resolve target window
    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die(
            "Not inside a tmux session — start one with `tmux new-session` first"
        )

    if target.startswith("#"):
        idx = target[1:]
    else:
        # Name match wins over index interpretation, even for a
        # digit-only target: validate_name now rejects digit-only
        # names at creation, but a project named e.g. "123" from
        # before that guard must stay reachable by name. Explicit
        # `#123` above remains the way to force index semantics.
        idx = ccm_core.find_window(session, target)
        if idx is None:
            if target.isdigit():
                idx = target
            else:
                ccm_core.ccm_die(f"Project not found: {target}")

    win_target = f"{session}:{idx}"

    # Look up project state from the current ccm scan
    projects = ccm_core.build_project_list(fast=False)
    matched = next((p for p in projects if p.win_target == win_target), None)
    project_name = matched.name if matched else None
    if matched is None:
        # A second window registered against the SAME directory is
        # dropped from `projects` by build_project_list's seen_dirs
        # dedup, so the win_target lookup misses it and the send
        # used to die with "not a registered ccm project" even
        # though the window is a legitimate ccm project. Fall back
        # to the same-directory sibling that IS tracked: delivery
        # still targets this window's own pane (resolved from
        # win_target below via _resolve_delivery_pane); only the
        # gating state is borrowed from the sibling — an
        # approximation, but far better than refusing outright.
        # The approximation is order-dependent: seen_dirs keeps the
        # FIRST window per directory, so with two same-dir windows
        # where window 1 is SHELL and window 2 hosts the running
        # claude, a send to window 2 borrows state=SHELL and is
        # refused without --start (pinned by
        # test_send_same_dir_second_window_sibling_shell_refused).
        proj_tag = ccm_core.tmux_cmd(
            "show-option", "-w", "-t", win_target, "-qv", "@ccm_project")
        dir_tag = ccm_core.tmux_cmd(
            "show-option", "-w", "-t", win_target, "-qv", "@ccm_dir")
        if proj_tag and dir_tag:
            canonical = ccm_core.canonical_dir(dir_tag)
            matched = next(
                (p for p in projects
                 if p.dir
                 and ccm_core.canonical_dir(p.dir) == canonical),
                None,
            )
            if matched is not None:
                # Display the target window's own name, not the
                # sibling's, so gating messages identify the pane
                # the user actually addressed.
                project_name = proj_tag
    if matched is None:
        ccm_core.ccm_die(f"Window is not a registered ccm project: {win_target}")

    state = matched.state

    # Resolve the pane that actually receives the keystrokes. The
    # gating state above is a window aggregate; delivery must go to
    # the claude-hosting pane (or, for SHELL windows, the active
    # pane where --start will launch). See _resolve_delivery_pane.
    pane_target, active_cmd = _resolve_delivery_pane(win_target)

    # Self-delivery guard. `ccm send` resolves delivery to the pane
    # hosting Claude, so a Claude session addressing its OWN project
    # resolves to the pane it is running in. Typing the body there
    # would inject it into the caller's own composer, and the state
    # gate below would consult a state the caller itself is producing
    # — a session is BUSY precisely because it is running this
    # command, so the gate reports BUSY and blames the target. That
    # self-reference reads as a spurious "the other agent is busy",
    # which is how it was reported. Refuse explicitly
    # instead: the honest answer is not a state verdict at all.
    #
    # `$TMUX_PANE` is set by tmux for any process started inside a
    # pane, so it identifies the caller without a lookup. Absent
    # (invoked outside tmux) the guard simply does not apply.
    caller_pane = os.environ.get("TMUX_PANE", "")
    if caller_pane and caller_pane == pane_target:
        ccm_core.ccm_die(
            f"{project_name}'s Claude pane IS this pane ({pane_target}) — "
            "refusing to send a message to yourself.\n"
            "  Note: the state ccm would gate on here is your own, so a "
            "session checking it always sees itself BUSY.\n"
            "  To reach a second agent running in another pane of this "
            "window, `ccm send` is not the route — it only delivers to "
            "Claude panes. Read that pane with "
            f"`ccm capture {project_name}`, which prints every pane "
            "separately."
        )

    # State-based gating
    if state == "PERMIT":
        if not now:
            _queue_message(project_name, message,
                           "target is in PERMIT state")
            return
        # Give the caller (human or another Claude) enough information
        # to understand what the target pane is blocked on. The refusal
        # itself is unconditional — PERMIT is never auto-dismissed from
        # another pane even when the modal is safe, because
        # misclassification of a real permission dialog could
        # accidentally approve a tool call.
        raw_tail = ccm_core.tmux_cmd(
            "capture-pane", "-t", pane_target, "-p", "-S", "-10"
        ) or ""
        if not raw_tail.strip():
            raw_tail = ccm_core.tmux_cmd(
                "capture-pane", "-a", "-t", pane_target, "-p", "-S", "-10"
            ) or ""
        tail_lines = [l for l in raw_tail.split("\n") if l.strip()][-8:]
        category, guidance = ccm_core.classify_permit_modal(raw_tail)
        lines = [
            f"{project_name} is in PERMIT state — send refused.",
            f"  Classification: {category}",
            "  Guidance:",
        ]
        lines.extend(f"    {g}" for g in guidance.split("\n"))
        if tail_lines:
            lines.append("  Pane tail:")
            lines.extend(f"    {l}" for l in tail_lines)
        ccm_core.ccm_die("\n".join(lines))

    # True once we actually launched Claude this invocation (SHELL +
    # --start). Gates post-send delivery verification: only a
    # freshly-launched target can be in the premature-IDLE window
    # where the `❯` composer shows before input is accepted.
    did_launch = False
    if state == "SHELL":
        if not auto_start:
            if not now:
                _queue_message(
                    project_name, message,
                    "target is in SHELL state (Claude not running)")
                return
            ccm_core.ccm_die(
                f"{project_name} is in SHELL state (Claude not running). "
                "Use --start to auto-launch Claude before sending."
            )
        # Guard the launch target. A SHELL window means "no claude
        # in any pane", but the active pane's foreground could still
        # be an editor / pager (per-pane detection maps a claude-less
        # pane to SHELL regardless of what runs in it). Typing the
        # launch command into vim would edit text, not start Claude —
        # refuse instead. `active_cmd is None` (pane enumeration
        # failed) skips the guard to preserve the defensive fallback.
        if active_cmd is not None and active_cmd not in SHELL_FOREGROUND_COMMANDS:
            ccm_core.ccm_die(
                f"{project_name} is in SHELL state but its active pane "
                f"is running `{active_cmd}`, not a shell — refusing to "
                "type the Claude launch command into it.\n"
                "  Switch to the target window, return the pane to a "
                "shell prompt (or focus a shell pane), then retry."
            )
        ccm_core.ccm_info(f"Starting Claude in {project_name}...")
        ccm_core.tmux_cmd("send-keys", "-t", pane_target, "-X", "cancel")
        ccm_core.tmux_cmd("send-keys", "-t", pane_target, CLAUDE_CMD, "Enter")
        did_launch = True
        # Wait for the target to reach the input prompt. A fixed
        # sleep would mis-deliver the message when `claude
        # --continue` triggers a follow-on action — most commonly
        # an auto-`/compact` on resume of a long session, or a
        # session-resume picker (PERMIT modal). Refuse with the
        # captured pane tail so the operator sees exactly what
        # the target is doing and finishes by hand. Progress is
        # printed when run interactively so the operator does not
        # see a frozen terminal during the wait.
        interactive_wait = sys.stdout.isatty()
        ready_state = _wait_for_target_idle(
            project_name, progress=interactive_wait,
        )
        if ready_state != "IDLE":
            tail = ccm_core.tmux_cmd(
                "capture-pane", "-t", pane_target, "-p", "-S", "-15",
            )
            tail_lines = [l for l in tail.split("\n") if l.strip()]
            lines = [
                f"{project_name} did not reach IDLE within "
                f"{START_WAIT_SEC}s after `claude --continue`.",
                f"  Last observed state: {ready_state}",
                "  Likely cause: Claude resumed into an auto-action",
                "    (e.g. `/compact` on a long session, or a",
                "    session-resume picker). The message would land",
                "    in the middle of that action and be eaten,",
                "    so the send is refused.",
                "  Switch to the target window, let the action",
                "    finish (or dismiss the modal), then retry.",
            ]
            if tail_lines:
                lines.append("  Pane tail:")
                lines.extend(f"    {l}" for l in tail_lines[-15:])
            ccm_core.ccm_die("\n".join(lines))

    if state == "BUSY" and not force:
        if not now:
            _queue_message(project_name, message, "target is BUSY")
            return
        ccm_core.ccm_die(
            f"{project_name} is BUSY. The message would queue in the "
            "input buffer and mix with Claude's current turn. Use --force "
            "if that is what you want."
        )

    # `claude agents` TUI guard. The TUI shows an `❯` input prompt that
    # ccm reads as IDLE, but typing into it dispatches a BRAND-NEW
    # agent-view session rather than landing in an existing
    # conversation — a `ccm send` would silently spawn a session the
    # operator didn't ask for. Refused unconditionally (even with
    # `--force`): the BUSY-style "queue it anyway" semantic does not
    # map onto "dispatch a new agent". Check is gated on `state ==
    # "IDLE"` because the PERMIT / BUSY / SHELL branches above already
    # refuse or handle their own paths, so we don't pay the
    # capture-pane cost twice.
    if state == "IDLE":
        raw_tail = ccm_core.tmux_cmd(
            "capture-pane", "-t", pane_target, "-p", "-S", "-10"
        ) or ""
        if not raw_tail.strip():
            raw_tail = ccm_core.tmux_cmd(
                "capture-pane", "-a", "-t", pane_target, "-p", "-S", "-10"
            ) or ""
        if ccm_core.is_agents_tui(raw_tail):
            if not now:
                _queue_message(
                    project_name, message,
                    "target is showing the `claude agents` TUI")
                return
            tail_lines = [l for l in raw_tail.split("\n") if l.strip()][-8:]
            lines = [
                f"{project_name} is showing the `claude agents` TUI — send refused.",
                "  Reason: keystrokes typed into the agents TUI dispatch a NEW",
                "    agent-view session rather than landing in an existing",
                "    Claude conversation.",
                "  User action: switch to the target pane and use the TUI's",
                "    own input (Enter to open a session, ? for shortcuts), or",
                "    detach from `claude agents` before retrying.",
            ]
            if tail_lines:
                lines.append("  Pane tail:")
                lines.extend(f"    {l}" for l in tail_lines)
            ccm_core.ccm_die("\n".join(lines))

    # Confirmation prompt (skip when piping or --yes)
    interactive = (
        not skip_confirm
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if interactive:
        preview = message.strip().replace("\n", " ")[:80]
        if len(message.strip()) > 80:
            preview += "..."
        tag = " (force)" if state == "BUSY" else ""
        print(f"Send to {project_name} ({state}{tag}): {preview}")
        try:
            ans = input("Proceed? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ccm_core.ccm_info("Cancelled")
            return
        if ans not in ("y", "yes"):
            ccm_core.ccm_info("Cancelled")
            return

    # TOCTOU guard: re-check the delivery pane's raw state after the
    # (potentially long-blocking) confirmation prompt, immediately
    # before any keystrokes. The gate above ran on a
    # build_project_list snapshot that can go stale while the
    # operator reads the preview — a target that transitioned to
    # PERMIT in the meantime must never receive the body (it would
    # be typed into the permission dialog). A None result (pane no
    # longer enumerable) fails open, matching the delivery-pane
    # resolution fallback.
    rechecked = _recheck_delivery_state(win_target, pane_target)
    if rechecked == "PERMIT":
        if not now:
            _queue_message(project_name, message,
                           "target transitioned to PERMIT mid-send")
            return
        ccm_core.ccm_die(
            f"{project_name} transitioned to PERMIT after the initial "
            "state check — send refused. PERMIT never receives "
            "keystrokes; resolve the dialog in the target pane, then "
            "retry."
        )
    if rechecked == "SHELL" and not did_launch:
        # SHELL means no claude under the pane — the body would be
        # typed into a bare shell (the same shape
        # class). The --start path is exempt: it just launched Claude
        # and the wait loop confirmed IDLE a moment ago, so a raw
        # SHELL reading there is detection lag, not a dead session.
        if not now:
            _queue_message(project_name, message,
                           "target no longer hosts a running Claude")
            return
        ccm_core.ccm_die(
            f"{project_name} no longer hosts a running Claude (state "
            "changed to SHELL after the initial check) — refusing to "
            "type the message into a bare shell."
        )
    if rechecked == "BUSY" and not force:
        if not now:
            _queue_message(project_name, message,
                           "target became BUSY mid-send")
            return
        ccm_core.ccm_die(
            f"{project_name} became BUSY after the initial state "
            "check. The message would queue in the input buffer and "
            "mix with Claude's current turn. Use --force if that is "
            "what you want."
        )

    # Defensively exit any tmux mode on the target pane. Without this,
    # a pane stuck in copy-mode would interpret the message characters
    # as copy-mode bindings rather than typed input.
    _send_keys(pane_target, "-X", "cancel", label="pre-cancel")

    # Composer-draft guard (see the note above
    # `_composer_draft_fragment`). Runs after the mode cancel so the
    # capture reads the live composer, and before any payload
    # keystrokes. Applies to every delivery path — including `--force`
    # (queueing into a BUSY turn must not also merge into a draft) and
    # `--start` (the user may have typed into the freshly launched
    # composer during the IDLE wait).
    draft = _composer_draft_fragment(pane_target)
    if draft is not None:
        if not now:
            _queue_message(
                project_name, message,
                "target's composer holds a half-typed draft")
            return
        ccm_core.ccm_die(
            f"{project_name}'s composer holds a half-typed draft — "
            "send refused: the message would merge into text being "
            "written right now.\n"
            f"  Draft: \"{draft}\"\n"
            "  Finish or clear the draft in the target pane, then retry."
        )

    # Literal send, converting `\n` into M-Enter (Claude Code's
    # "newline without submit" key) so the body is delivered as a
    # single multi-line prompt rather than multiple submitted turns.
    lines = message.split("\n")
    if _trace_enabled():
        _trace_record(pane_target, "send-start",
                      (f"project={project_name}", f"lines={len(lines)}",
                       f"bytes={len(message)}"))
    _type_body(pane_target, lines)

    # Post-launch delivery verification. On the --start path the
    # body can be eaten by a not-yet-ready input handler even though
    # detection saw IDLE (see the _DELIVERY_* note above). Verify the
    # body actually reached the composer BEFORE committing the Enter;
    # if not, clear and re-type a few times, then refuse honestly
    # rather than print a false "Sent". Verifying before Enter means
    # a retry can never double-submit. Skipped when we didn't launch
    # (an already-IDLE target was genuinely ready) or when the
    # message is too short to match without false positives.
    signature = _message_signature(message) if did_launch else None
    if signature is not None and not _body_landed(pane_target, signature):
        landed = False
        for _ in range(_DELIVERY_VERIFY_RETRIES):
            time.sleep(_DELIVERY_VERIFY_SETTLE_SEC)
            # Clear the composer (C-u) first so a partial landing from
            # the previous attempt cannot duplicate text on re-type.
            _send_keys(pane_target, "C-u", label="retry-clear")
            _type_body(pane_target, lines)
            if _body_landed(pane_target, signature):
                landed = True
                break
        if not landed:
            if _trace_enabled():
                _trace_record(pane_target, "send-unverified",
                              (f"project={project_name}",))
            ccm_core.ccm_die(
                f"Delivery to {project_name} could not be confirmed: the "
                f"message did not appear in its input box after "
                f"{_DELIVERY_VERIFY_RETRIES} retries.\n"
                "  Likely cause: Claude had just launched (--start) and its "
                "input handler was not yet accepting keystrokes — the body "
                "was eaten.\n"
                "  The send was NOT completed (no Enter submitted). Switch "
                "to the target window, confirm it is idle at the prompt, "
                "then resend (without --start, since Claude is now running)."
            )

    # Final submit (unless --no-enter)
    if not no_enter:
        _send_keys(pane_target, "Enter", label="final-submit")
    if _trace_enabled():
        _trace_record(pane_target, "send-end", (f"project={project_name}",))

    ccm_core.ccm_info(f"Sent to {project_name}")


# ─── ccm sidekick-send ───
# Delivers a prompt to the sidekick agent CLI (Kimi, Codex, …) in a
# split pane of the CALLER'S window. `ccm send` deliberately never
# reaches a sidekick — it only targets tracked Claude panes — so this
# is the other lane of the relay. What it enforces that the manual
# `tmux send-keys` procedure could only ask for:
#
#   - identity: the target is found from tmux metadata — the single
#     pane in the caller's own window whose foreground command is a
#     known external agent (EXTERNAL_AGENT_COMMANDS), with a working
#     directory inside this project. A mis-targeted send cannot
#     happen silently.
#   - procedure: literal `-l --` typing, the settle pause before the
#     committing Enter, and a post-send capture confirming a fragment
#     of the message actually landed.
#
# What it deliberately does NOT do is read the sidekick's SCREEN
# state: ccm tracks no state for a non-Claude pane, and matching
# another TUI's prompt text would inherit every vendor's redesign.
# Readiness stays the caller's judgment (`ccm capture` first).

_SIDEKICK_SEND_USAGE = (
    "Usage: ccm sidekick-send <message> "
    "[--file path] [--stdin|-] [--no-enter]\n"
    "       ccm sidekick-send -- <message starting with a dash>\n"
    "Delivers to the sidekick agent CLI in a split pane of THIS window."
)

# Pause between the typed body and the committing Enter. A peer TUI
# still digesting the inserted text when Enter arrives can take it as
# a newline instead of a submit; the body then sits in the composer
# unsent, looking exactly like a delivered message. Measured against
# Kimi K3: no gap fails every time, 0.3 s submits. Claude Code's own
# composer tolerates a zero gap, which is why `ccm send` needs none.
_SIDEKICK_SUBMIT_SETTLE_SEC = 0.3

# Post-send confirmation polls the pane for a message fragment.
# Presence can pass spuriously when the same text was already on
# screen (resending an identical message) — accepted: the check
# exists to catch text that never arrived, not to prove ordering.
_SIDEKICK_VERIFY_TIMEOUT_SEC = 2.0
_SIDEKICK_VERIFY_POLL_SEC = 0.5


def _resolve_sidekick_pane(caller_pane):
    """Return `(pane_id, agent_name)` of THE sidekick pane in the
    caller's window, refusing (ccm_die) on every ambiguity.

    Identity comes from tmux metadata only — the pane's foreground
    command against `external_agent_name`, and its working directory
    against the project directory. The sidekick's screen is never
    read."""
    fmt = "#{window_id}\t#{pane_current_command}\t#{pane_current_path}"
    info = (ccm_core.tmux_cmd(
        "display-message", "-p", "-t", caller_pane, fmt) or "").strip()
    parts = info.split("\t")
    if len(parts) < 3 or not parts[0]:
        ccm_core.ccm_die(
            f"Cannot identify the caller's window from {caller_pane} — "
            "is $TMUX_PANE stale? Run `ccm sidekick-send` from a live "
            "pane of the project window."
        )
    win_id, caller_cmd, caller_cwd = parts[0], parts[1], parts[2]

    agent = external_agent_name(caller_cmd)
    if agent:
        ccm_core.ccm_die(
            f"This pane IS the sidekick ({agent}) — refusing the "
            "reverse lane. To reach the window's Claude session, use "
            "`ccm send <project>`."
        )

    out = ccm_core.tmux_cmd(
        "list-panes", "-t", win_id, "-F",
        "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}") or ""
    sidekicks = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 3:
            continue
        name = external_agent_name(f[1])
        if name and f[0] != caller_pane:
            sidekicks.append((f[0], name, f[2]))

    if not sidekicks:
        ccm_core.ccm_die(
            "No sidekick pane in this window — no pane is running a "
            "known external agent CLI.\n"
            "  Start the sidekick in a split pane of this window first. "
            "To message another project's Claude session, use "
            "`ccm send <project>`."
        )
    if len(sidekicks) > 1:
        listing = ", ".join(f"{pid} ({name})" for pid, name, _ in sidekicks)
        ccm_core.ccm_die(
            f"{len(sidekicks)} sidekick panes in this window "
            f"({listing}) — the target is ambiguous, refusing.\n"
            "  Keep exactly one sidekick pane in the window, or type "
            "into it by hand with tmux if you really run several."
        )

    pane_id, agent_name, pane_cwd = sidekicks[0]

    # The pane must belong to THIS project. The window's @ccm_dir tag
    # is the registered identity; fall back to the caller pane's own
    # cwd when the window is untagged. No reference at all fails
    # closed — the point of the check is that a mis-targeted send
    # cannot happen silently.
    ref = (ccm_core.tmux_cmd(
        "show-option", "-w", "-t", win_id, "-qv", "@ccm_dir") or "").strip()
    ref = ref or caller_cwd
    if not ref:
        ccm_core.ccm_die(
            "Cannot establish this window's project directory (no "
            "@ccm_dir tag and the caller's cwd is unreadable) — "
            "refusing rather than guessing at the target."
        )
    ref_c = ccm_core.canonical_dir(ref)
    pane_c = ccm_core.canonical_dir(pane_cwd)
    if pane_c != ref_c and not pane_c.startswith(ref_c + os.sep):
        ccm_core.ccm_die(
            f"The sidekick pane {pane_id} runs in {pane_cwd}, outside "
            f"this project ({ref}) — refusing: the pane does not "
            "belong to this window's project."
        )
    return pane_id, agent_name


def cmd_sidekick_send(args):
    """Send a prompt to the sidekick agent CLI in the caller's window.

    Usage:
      ccm sidekick-send <message>            Send literal message + Enter
      ccm sidekick-send --file <path>        Read message from a file
      ccm sidekick-send --stdin              Read message from stdin
      ccm sidekick-send --no-enter <msg>     Send without submitting
      ccm sidekick-send -- "--literal"       `--` ends flag parsing
    """
    if any(a in ("-h", "--help") for a in args):
        print(_SIDEKICK_SEND_USAGE)
        return
    positional_parts = []
    message_file = None
    use_stdin = False
    no_enter = False

    stop_flags = False
    i = 0
    while i < len(args):
        arg = args[i]
        if not stop_flags and arg == "--":
            stop_flags = True
            i += 1
            continue
        if not stop_flags and arg.startswith("-") and arg != "-":
            if arg == "--file":
                i += 1
                if i >= len(args):
                    ccm_core.ccm_die("--file requires a path argument")
                message_file = args[i]
            elif arg == "--stdin":
                use_stdin = True
            elif arg == "--no-enter":
                no_enter = True
            else:
                ccm_core.ccm_die(
                    f"Unknown flag: {arg}\n{_SIDEKICK_SEND_USAGE}")
        else:
            if arg == "-":  # conventional stdin alias
                use_stdin = True
            else:
                positional_parts.append(arg)
        i += 1

    # Resolve message source (exactly one of the three), mirroring
    # cmd_send's conventions.
    positional_message = " ".join(positional_parts) if positional_parts else None
    source_count = sum(x is not None and x is not False for x in
                       (positional_message, message_file, use_stdin or None))
    if source_count == 0:
        ccm_core.ccm_die("No message provided (positional, --file, or --stdin)")
    if source_count > 1:
        ccm_core.ccm_die(
            "Provide exactly one of: positional message, --file, or --stdin"
        )
    if message_file:
        try:
            with open(message_file, encoding="utf-8") as f:
                message = f.read()
        except OSError as e:
            ccm_core.ccm_die(f"Failed to read message file: {e}")
    elif use_stdin:
        message = sys.stdin.read()
    else:
        message = positional_message
    if not message.strip() and not no_enter:
        ccm_core.ccm_die(
            "Empty message (use --no-enter to send only Enter suppression)"
        )

    caller_pane = os.environ.get("TMUX_PANE", "")
    if not caller_pane:
        ccm_core.ccm_die(
            "ccm sidekick-send must run inside tmux ($TMUX_PANE is "
            "unset) — it delivers to the sidekick pane of the caller's "
            "own window."
        )

    pane_id, agent = _resolve_sidekick_pane(caller_pane)

    # TOCTOU: re-resolve immediately before typing. The pane found a
    # moment ago may have exited or been replaced while the message
    # was being read (a slow --file, a human at the keys) — typing
    # into its successor would be the mis-send this command exists to
    # prevent.
    again = _resolve_sidekick_pane(caller_pane)
    if again != (pane_id, agent):
        ccm_core.ccm_die(
            f"The sidekick pane changed while preparing the send "
            f"({pane_id} → {again[0]}) — refusing. Retry the command."
        )

    # Defensively exit any tmux mode on the target pane, then type
    # the body literally (shared helpers — see cmd_send).
    _send_keys(pane_id, "-X", "cancel", label="sidekick-pre-cancel")
    lines = message.split("\n")
    if _trace_enabled():
        _trace_record(pane_id, "sidekick-send-start",
                      (f"agent={agent}", f"lines={len(lines)}",
                       f"bytes={len(message)}"))
    _type_body(pane_id, lines)

    if not no_enter:
        # The settle pause is load-bearing — see the constant's note.
        time.sleep(_SIDEKICK_SUBMIT_SETTLE_SEC)
        _send_keys(pane_id, "Enter", label="sidekick-submit")

    # Post-send delivery confirmation: a fragment of the message must
    # be visible in the pane (in the composer for --no-enter, in the
    # conversation echo after Enter). Absence means the text never
    # arrived — report failure honestly instead of a false "Sent".
    signature = _message_signature(message)
    if signature is None:
        ccm_core.ccm_info(
            f"Sent to sidekick {agent} ({pane_id}) — message too short "
            "to auto-verify; confirm with `ccm capture`."
        )
        return
    deadline = time.time() + _SIDEKICK_VERIFY_TIMEOUT_SEC
    while True:
        if _body_landed(pane_id, signature):
            break
        if time.time() >= deadline:
            if _trace_enabled():
                _trace_record(pane_id, "sidekick-send-unverified",
                              (f"agent={agent}",))
            ccm_core.ccm_die(
                f"Delivery to the sidekick ({agent}, {pane_id}) could "
                "not be confirmed: no fragment of the message appeared "
                "in the pane after sending.\n"
                "  The send may have been eaten (a TUI still digesting "
                "the text, or a dialog open over the composer). Check "
                "the pane with `ccm capture`, then resend."
            )
        time.sleep(_SIDEKICK_VERIFY_POLL_SEC)
    if _trace_enabled():
        _trace_record(pane_id, "sidekick-send-end", (f"agent={agent}",))
    ccm_core.ccm_info(f"Sent to sidekick {agent} ({pane_id})")
