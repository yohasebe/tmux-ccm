"""`ccm spool` — store-and-forward queue for cross-project messages.

When `ccm send` finds the target BUSY / PERMIT / SHELL (or otherwise
momentarily undeliverable), the message used to be refused and the
sender owned the retry loop. The spool takes over that loop: the
message is written to `$CCM_DATA_DIR/spool/<project>/` and the
periodic reconciler (the `inject-status` full pass, under its flock)
delivers it once the target is genuinely ready. No daemon, no LLM
broker — the mechanism is a directory, a rename, and the detection
ccm already runs.

Layout per project (`<project>` is the registered project name):

    <project>/<ms>-<sender>.msg     pending, sorted oldest-first
    <project>/<ms>-<sender>.msg.delivering   claimed by a delivery pass
    <project>/delivered/            evidence of what went out
    <project>/held/                 typed, submitted, not taken
    <project>/expired/              evidence of what outlived the TTL
    <project>.lock                  per-project mkdir lock (atomic)

Delivery semantics, deliberately chosen and pinned by tests:

  - **One message per project per pass.** Two back-to-back sends
    would land the second in the input buffer of the turn the first
    just started — the mixing the BUSY refusal existed to prevent.
    The queue drains one per IDLE observation instead.
  - **Claim-by-rename** (`msg` → `msg.delivering`) before typing, so
    a concurrent pass cannot pick the same message up.
  - **At-least-once.** A crash between the send-keys and the move to
    `delivered/` re-delivers the message on the next pass. Duplicates
    are tolerable (the envelope's `queued` timestamp betrays them);
    silent loss is not.
  - **TTL.** A queued instruction goes stale: delivered hours late,
    it executes out of context. Messages older than
    `CCM_SPOOL_TTL_SEC` (default 60 min) move to `expired/` and are
    surfaced in `ccm status` / `ccm doctor` instead of delivering.
  - **Fail-closed readiness.** Unlike the interactive `ccm send`
    (whose capture hiccups fail open so as not to break working
    sends), the unattended pass defers whenever readiness is in
    doubt: raw state not IDLE, an agents-TUI screen, a composer
    holding a draft, or an unreadable capture all leave the message
    queued for the next pass. The TTL is the backstop for a target
    that never becomes ready.
  - **No auto-launch.** A SHELL target stays queued until Claude is
    started by other means; the spool never starts sessions.
"""

import os
import re
import shlex
import time

import ccm_constants
import ccm_core
import ccm_notify
import ccm_send  # shared typing helpers (_send_keys / _type_body)
from ccm_pane_state import (
    capture_composer_snapshot, detect_pane_state, enumerate_window_panes,
    read_work_clock,
)

SPOOL_ROOT = os.path.join(ccm_constants.CCM_DATA_DIR, "spool")

# How long a queued message stays deliverable. An instruction that
# arrives an hour late is not the instruction the sender meant — see
# the module docstring. Env-overridable for deployments whose messages
# legitimately wait longer.
SPOOL_TTL_SEC = int(os.environ.get("CCM_SPOOL_TTL_SEC", "3600"))

# A delivery pass holding a lock for longer than this died mid-pass
# (the reconciler crashed between claim and release). Reclaim rather
# than strand the project forever.
_LOCK_STALE_SEC = 120

# Evidence dirs are trimmed past this age — they exist so a delivery
# (or an expiry) can be accounted for, not as an archive.
_EVIDENCE_KEEP_SEC = 7 * 86400

_SPOOL_USAGE = (
    "Usage: ccm spool list [project]          Inspect pending, held and expired messages\n"
    "       ccm spool cancel <id> [project]   Withdraw a queued message\n"
    "       ccm spool cancel --all [project]  Withdraw all of them\n"
    "       ccm spool clear-expired [project] Delete expired records only; sends nothing\n"
    "       ccm spool clear-held [project]    Acknowledge held ones\n"
    "       ccm spool show <expired|held> <id> <project>\n"
    "       ccm spool discard <expired|held> <id> <project> [--yes]\n"
    "       ccm spool resend expired <id> <project> [--start] [--yes]"
)


def _project_dir(name):
    return os.path.join(SPOOL_ROOT, name)


def _safe_component(s):
    """Make an arbitrary sender label filename-safe."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")
    return cleaned or "unknown"


def _parse_msg_name(filename):
    """`(queued_ts, sender)` from `<ms>-<sender>.msg`, or None."""
    if not filename.endswith(".msg"):
        return None
    stem = filename[:-4]
    ms, sep, sender = stem.partition("-")
    if not sep or not ms.isdigit() or not sender:
        return None
    return int(ms) / 1000.0, sender


def _pending(pdir):
    """Pending message filenames in `pdir`, oldest first.

    `.delivering` (claimed) and `.tmp` (mid-write) names are excluded;
    a claimed message belongs to the pass that claimed it, even if
    that pass died (the lock staleness check recovers the project).
    """
    try:
        names = os.listdir(pdir)
    except OSError:
        return []
    return sorted(
        n for n in names
        if n.endswith(".msg") and os.path.isfile(os.path.join(pdir, n))
    )


def enqueue(project, sender, body):
    """Append `body` to `project`'s spool. Returns `(msg_id, n_pending)`.

    The file holds the raw body only; the envelope is prepended at
    delivery time so its `delivered` timestamp is real. The write is
    tmp-then-rename so a concurrent delivery pass never reads a
    half-written message.
    """
    pdir = _project_dir(project)
    os.makedirs(pdir, exist_ok=True)
    ms = int(time.time() * 1000)
    path = os.path.join(pdir, f"{ms}-{_safe_component(sender)}.msg")
    while os.path.exists(path):
        # Same-millisecond resend from the same sender: nudge the key
        # rather than overwrite. Ordering and the queued-at display
        # are unaffected at this granularity.
        ms += 1
        path = os.path.join(pdir, f"{ms}-{_safe_component(sender)}.msg")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)
    return os.path.basename(path)[:-4], len(_pending(pdir))


def pending_counts():
    """`{project: n_pending}` for projects with a non-empty queue."""
    counts = {}
    try:
        entries = os.listdir(SPOOL_ROOT)
    except OSError:
        return counts
    for entry in entries:
        if entry.endswith(".lock"):
            continue
        pdir = os.path.join(SPOOL_ROOT, entry)
        if os.path.isdir(pdir):
            n = len(_pending(pdir))
            if n:
                counts[entry] = n
    return counts


def expired_counts():
    """`{project: n_expired}` for projects holding a message that was
    never delivered.

    Separate from `pending_counts` because the two mean opposite
    things to the reader: a pending count is work still on its way, an
    expired one is work that never arrived. Surfacing only the first
    is how the record of a loss stays invisible in the places people
    actually look.
    """
    return _subdir_counts("expired")


def _in_subdir(pdir, sub):
    """The message files a project keeps in `sub`, oldest first."""
    try:
        return sorted(n for n in os.listdir(os.path.join(pdir, sub))
                      if n.endswith(".msg"))
    except OSError:
        return []


def _subdir_counts(sub):
    """`{project: n}` for the messages each project keeps in `sub`."""
    counts = {}
    try:
        entries = os.listdir(SPOOL_ROOT)
    except OSError:
        return counts
    for entry in entries:
        if entry.endswith(".lock"):
            continue
        d = os.path.join(SPOOL_ROOT, entry, sub)
        try:
            n = sum(1 for n_ in os.listdir(d) if n_.endswith(".msg"))
        except OSError:
            continue
        if n:
            counts[entry] = n
    return counts


def held_counts():
    """`{project: n_held}` for projects whose message was typed into a
    session that did not take it — it is sitting in that session's
    input box, waiting for its user to confirm or clear it. Neither
    pending (ccm will not send it again) nor delivered."""
    return _subdir_counts("held")


def spool_summary():
    """{"pending": N, "expired": M, "held": H} across all projects,
    for doctor."""
    return {"pending": sum(pending_counts().values()),
            "expired": sum(expired_counts().values()),
            "held": sum(held_counts().values())}


def _expire_and_prune(pdir, now):
    """Move stale pending messages to expired/, and trim evidence."""
    edir = os.path.join(pdir, "expired")
    for name in _pending(pdir):
        parsed = _parse_msg_name(name)
        if parsed is None:
            continue
        queued, _sender = parsed
        if now - queued > SPOOL_TTL_SEC:
            os.makedirs(edir, exist_ok=True)
            try:
                os.rename(os.path.join(pdir, name),
                          os.path.join(edir, name))
            except OSError:
                continue
            # Tell somebody. `ccm send` reported the message as
            # queued, and queued is not delivered — without this the
            # only trace of the loss is a count in `ccm spool list`,
            # which nobody reads until they notice a reply missing.
            try:
                ccm_notify.notify("SPOOLEXPIRED", os.path.basename(pdir),
                                  _sender)
            except Exception:
                ccm_core.log_caught_exception("spool-expire-notify")
    for sub in ("delivered", "expired"):
        d = os.path.join(pdir, sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            try:
                if now - os.path.getmtime(path) > _EVIDENCE_KEEP_SEC:
                    os.unlink(path)
            except OSError:
                pass


# ─── per-project delivery lock ───

def _acquire_lock(pdir):
    """mkdir lockdir: atomic across the (already flock-serialised)
    reconciler and any future second driver. A stale lock belongs to
    a crashed pass — reclaim it."""
    lock = pdir + ".lock"
    try:
        os.mkdir(lock)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lock) > _LOCK_STALE_SEC:
                os.rmdir(lock)
                os.mkdir(lock)
                return True
        except OSError:
            pass
        return False
    except OSError:
        return False


def _release_lock(pdir):
    try:
        os.rmdir(pdir + ".lock")
    except OSError:
        pass


# ─── delivery ───

def _deliverable_pane(win_target, expected_pane=None):
    """`(pane_id, None)` when the window can take a message right now,
    else `(None, reason)`. Fail-closed: any doubt defers the message
    to the next pass (the TTL bounds how long that can go on)."""
    ps_lines = ccm_core.ps_snapshot().strip().split("\n")
    panes = [p for p in enumerate_window_panes(win_target, ps_lines)
             if not p.ignored]
    if not panes:
        return None, "no panes"
    claude_panes = [p for p in panes if p.claude_pid]
    active = next((p for p in panes if p.active), None)
    if expected_pane is not None:
        if len(claude_panes) != 1 or claude_panes[0].pane_id != expected_pane:
            return None, "notification target changed"
        pane = claude_panes[0]
    elif active is not None and active.claude_pid:
        pane = active
    elif len(claude_panes) == 1:
        pane = claude_panes[0]
    else:
        return None, "delivery pane ambiguous"
    raw = detect_pane_state(pane.pane_pid, pane.pane_id, ps_lines,
                            str(os.getpgrp()),
                            current_command=pane.current_command,
                            stored_clock=read_work_clock(win_target))
    if raw != "IDLE":
        return None, f"raw state {raw}"
    cap, attributed = capture_composer_snapshot(pane.pane_id)
    if not cap.strip():
        return None, "capture unreadable"
    if ccm_core.is_agents_tui(cap):
        return None, "agents TUI"
    if ccm_constants.composer_draft_fragment(
            cap, pane_text_attributed=attributed) is not None:
        return None, "composer holds a draft"
    return pane.pane_id, None


def _envelope(sender, queued_ts, now):
    """The header a queued message is delivered under: provenance, so
    the receiver can tell a stale or duplicated delivery from a fresh
    one, and the reply route, so nobody relays by hand."""
    q = (time.strftime("%H:%M", time.localtime(queued_ts))
         if queued_ts else "?")
    d = time.strftime("%H:%M", time.localtime(now))
    reply = ("sender unavailable; confirm the reply destination"
             if sender == "unknown" else f'reply with `ccm send {sender} "…"`')
    return f"[from: {sender} · queued {q} · delivered {d} — {reply}]"


def _deliver_one(project, pdir, msg_name):
    """Claim, deliver, and file one message. Returns True on delivery.

    On any failure the message is renamed back to pending and retried
    on a later pass — at-least-once (see the module docstring)."""
    src = os.path.join(pdir, msg_name)
    delivering = src + ".delivering"
    try:
        os.rename(src, delivering)
    except OSError:
        return False  # claimed by a concurrent pass
    try:
        pane_id, _reason = _deliverable_pane(project.win_target)
        if pane_id is None:
            os.rename(delivering, src)
            return False
        parsed = _parse_msg_name(msg_name)
        queued, sender = parsed if parsed else (None, "unknown")
        with open(delivering, encoding="utf-8") as f:
            body = f.read()
        text = _envelope(sender, queued, time.time()) + "\n\n" + body
        ccm_send._send_keys(pane_id, "-X", "cancel", label="spool-pre-cancel")
        ccm_send._type_body(pane_id, text.split("\n"))
        ccm_send._send_keys(pane_id, "Enter", label="spool-submit")
        if ccm_send.held_after_submit(pane_id, message=text):
            # Typed and submitted, and the session is holding it for
            # its user to confirm (a prompt it rewrote). Recording it
            # as delivered would lose it while saying it arrived, and
            # putting it back in the queue would type it a second
            # time: once the user confirms the copy in the composer,
            # the next pass finds an empty composer and cannot tell
            # that from a message never delivered. So it leaves the
            # queue for `held/`, where `ccm spool list` and
            # `ccm doctor` name it and the reader decides.
            hdir = os.path.join(pdir, "held")
            os.makedirs(hdir, exist_ok=True)
            os.rename(delivering, os.path.join(hdir, msg_name))
            return False
        ddir = os.path.join(pdir, "delivered")
        os.makedirs(ddir, exist_ok=True)
        os.rename(delivering, os.path.join(ddir, msg_name))
        return True
    except Exception:
        ccm_core.log_caught_exception("spool-deliver")
        try:
            os.rename(delivering, src)
        except OSError:
            pass
        return False


def reconcile_spools(projects):
    """One delivery pass over every project spool.

    Called from the `inject-status` full pass (periodic, under its
    flock) — the single delivery driver in v1. Runs BEFORE
    auto_exit_idle so a session with a pending message is used, not
    closed. Per project: expire first, then deliver at most one
    message, and only to a project that currently reads IDLE.
    """
    root = SPOOL_ROOT
    if not os.path.isdir(root):
        return
    regular_pending = set(pending_counts())
    by_name = {}
    for p in projects:
        by_name.setdefault(p.name, p)
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return
    now = time.time()
    for entry in entries:
        if entry.endswith(".lock"):
            continue
        pdir = os.path.join(root, entry)
        if not os.path.isdir(pdir):
            continue
        try:
            _expire_and_prune(pdir, now)
        except Exception:
            ccm_core.log_caught_exception("spool-expire")
        pending = _pending(pdir)
        if not pending:
            continue
        project = by_name.get(entry)
        if project is None or project.state != "IDLE":
            continue
        if not _acquire_lock(pdir):
            continue
        try:
            # Re-list inside the lock; the first pass's view may be
            # stale by the time the lock is taken.
            pending = _pending(pdir)
            if pending:
                _deliver_one(project, pdir, pending[0])
        finally:
            _release_lock(pdir)

    import ccm_sidekick_notify
    ccm_sidekick_notify.reconcile(projects, regular_pending)


# ─── ccm spool list / cancel ───

def _msg_age(now, queued_ts):
    minutes = int((now - queued_ts) // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    return f"{minutes // 60}h{minutes % 60:02d}m ago"


def _preview(path, limit=60):
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read()
    except (OSError, UnicodeError):
        return "(unreadable)"
    first = next((ln.strip() for ln in body.split("\n") if ln.strip()), "")
    if len(first) > limit:
        first = first[:limit] + "..."
    return first or "(empty)"


def _iter_project_dirs(only=None):
    try:
        entries = sorted(os.listdir(SPOOL_ROOT))
    except OSError:
        return
    for entry in entries:
        if entry.endswith(".lock"):
            continue
        if only is not None and entry != only:
            continue
        pdir = os.path.join(SPOOL_ROOT, entry)
        if os.path.isdir(pdir):
            yield entry, pdir


def _cmd_list(rest):
    project = rest[0] if rest else None
    now = time.time()
    shown = 0
    for name, pdir in _iter_project_dirs(only=project):
        pending = _pending(pdir)
        expired = _in_subdir(pdir, "expired")
        held = _in_subdir(pdir, "held")
        n_expired, n_held = len(expired), len(held)
        # A project whose queue is empty can still be the one holding
        # the record of a message that never arrived. Skipping it
        # because nothing is pending hides that record in the command
        # `ccm doctor` sends the reader to.
        if not pending and not n_expired and not n_held:
            continue
        print(f"{name}:")
        for fname in pending:
            parsed = _parse_msg_name(fname)
            queued, _sender = parsed if parsed else (None, None)
            age = _msg_age(now, queued) if queued else "?"
            msg_id = fname[:-4]
            print(f"  {msg_id}  ({age})  {_preview(os.path.join(pdir, fname))}")
        for fname in held:
            msg_id = fname[:-4]
            print(f"  {msg_id}  (held)  "
                  f"{_preview(os.path.join(pdir, 'held', fname))}")
        if n_held:
            print(f"  ({n_held} last seen waiting in the session's input "
                  f"box. Deal with it there — press Enter to send it or "
                  f"clear the box — then `ccm spool clear-held {name}`. "
                  f"ccm does not retype or withdraw them.)")
        if n_expired:
            print(f"  ({n_expired} expired — never delivered)")
            for fname in expired:
                parsed = _parse_msg_name(fname)
                queued, sender = parsed if parsed else (None, "unknown")
                age = _msg_age(now, queued) if queued is not None else "unknown"
                path = os.path.abspath(os.path.join(pdir, "expired", fname))
                print(f"  {fname[:-4]}  (expired; from: {sender}; queued: {age})  "
                      f"{_preview(path)}")
                print(f"    Review full text: {shlex.join(['cat', '--', path])}")
                print("    If still needed, send as NEW: "
                      + shlex.join(['ccm', 'send', '--file', path, '--', name]))
            print("  After reviewing ALL expired records above: "
                  + shlex.join(['ccm', 'spool', 'clear-expired', name])
                  + " — deletes this project's expired records only; sends nothing.")
        shown += 1
    if shown:
        return
    if project is not None:
        ccm_core.ccm_info(f"No queued messages for {project}.")
    else:
        ccm_core.ccm_info("No queued messages.")


def _cmd_cancel(rest):
    if not rest:
        ccm_core.ccm_die(_SPOOL_USAGE)
    target = rest[0]
    project = rest[1] if len(rest) > 1 else None

    if target == "--all":
        n = 0
        for name, pdir in _iter_project_dirs(only=project):
            for fname in _pending(pdir):
                try:
                    os.unlink(os.path.join(pdir, fname))
                    n += 1
                except OSError:
                    pass
        scope = project or "all projects"
        ccm_core.ccm_info(f"Cancelled {n} queued message(s) for {scope}.")
        return

    matches = []
    for name, pdir in _iter_project_dirs(only=project):
        path = os.path.join(pdir, target + ".msg")
        if os.path.isfile(path):
            matches.append((name, path))
    if not matches:
        ccm_core.ccm_die(
            f"No queued message with id {target}"
            + (f" for {project}." if project else ".")
            + "  `ccm spool list` shows live ids."
        )
    if len(matches) > 1:
        ccm_core.ccm_die(
            f"Id {target} exists in several projects "
            f"({', '.join(n for n, _ in matches)}) — repeat with the "
            "project name: `ccm spool cancel <id> <project>`."
        )
    name, path = matches[0]
    try:
        os.unlink(path)
    except OSError as e:
        ccm_core.ccm_die(f"Failed to cancel {target}: {e}")
    ccm_core.ccm_info(f"Cancelled {target} ({name}).")


def _cmd_clear_expired(rest):
    """Drop the records of messages that expired undelivered.

    They are kept as evidence and warned about until they age out, and
    until now there was no way to say "seen". A warning that cannot be
    answered is one the reader learns to scroll past — the same reason
    `ccm errors` has `--clear`. Deleting the evidence is the
    acknowledgement; nothing else is affected, since a queued message
    lives elsewhere.
    """
    project = rest[0] if rest else None
    removed = 0
    for name, pdir in _iter_project_dirs(only=project):
        edir = os.path.join(pdir, "expired")
        try:
            names = os.listdir(edir)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".msg"):
                continue
            try:
                os.unlink(os.path.join(edir, n))
                removed += 1
            except OSError:
                pass
    if removed:
        ccm_core.ccm_info(
            f"Cleared {removed} expired message record(s)."
            + (f" ({project})" if project else ""))
    else:
        ccm_core.ccm_info("No expired message records to clear.")


def _cmd_clear_held(rest):
    """Drop the records of messages a session was holding.

    ccm cannot see how one ended: whether its user pressed Enter on
    the copy in the composer or cleared it away, the composer is
    empty afterwards either way — which is why these are not queued
    again. The record says "this was last seen waiting"; saying
    "dealt with" is the reader's to do, and this is where they say
    it. Nothing is sent, withdrawn or typed here.
    """
    project = rest[0] if rest else None
    removed = 0
    for name, pdir in _iter_project_dirs(only=project):
        hdir = os.path.join(pdir, "held")
        for n in _in_subdir(pdir, "held"):
            try:
                os.unlink(os.path.join(hdir, n))
                removed += 1
            except OSError:
                pass
    if removed:
        ccm_core.ccm_info(f"Cleared {removed} held message record(s).")
    else:
        ccm_core.ccm_info("No held message records to clear.")


def attention_records():
    """Read-only, stable keys for the records that need a decision."""
    records = []
    now = time.time()
    for project, pdir in _iter_project_dirs():
        if os.path.islink(pdir):
            continue
        for kind in ("expired", "held"):
            for filename in _in_subdir(pdir, kind):
                parsed = _parse_msg_name(filename)
                if parsed is None:
                    continue
                queued, sender = parsed
                records.append({"project": project, "kind": kind,
                                "id": filename[:-4], "sender": sender,
                                "age": _msg_age(now, queued),
                                "preview": _preview(os.path.join(pdir, kind, filename))})
    import ccm_sidekick_notify
    return records + ccm_sidekick_notify.attention_records()


def record_actions(kind):
    """UI affordances only; record operations re-read and validate the target."""
    if kind == "expired":
        return ("read", "discard", "open", "resend")
    if kind == "held":
        return ("read", "discard", "open")
    if kind.startswith("notice-"):
        import ccm_sidekick_notify
        return ccm_sidekick_notify.record_actions(kind)
    return ()


def _record_path(kind, msg_id, project):
    if (kind not in ("expired", "held")
            or any(not value or value in (".", "..")
                   or os.path.basename(value) != value
                   for value in (msg_id, project))
            or _parse_msg_name(msg_id + ".msg") is None):
        ccm_core.ccm_die("Invalid spool record.")
    pdir = _project_dir(project)
    path = os.path.join(pdir, kind, msg_id + ".msg")
    if any(os.path.islink(p) for p in (pdir, os.path.dirname(path), path)):
        ccm_core.ccm_die("Refusing a linked spool record.")
    return path


def read_record(kind, msg_id, project):
    if kind.startswith("notice-"):
        import ccm_sidekick_notify
        return ccm_sidekick_notify.read_record(kind, msg_id, project)
    path = _record_path(kind, msg_id, project)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeError) as e:
        ccm_core.ccm_die(f"Cannot read spool record: {e}")


def _cmd_record(action, rest):
    yes = "--yes" in rest or "-y" in rest
    start = "--start" in rest
    args = [arg for arg in rest if arg not in ("--yes", "-y", "--start")]
    if len(args) != 3 or (start and action != "resend"):
        ccm_core.ccm_die(_SPOOL_USAGE)
    kind, msg_id, project = args
    if kind.startswith("notice-"):
        import ccm_sidekick_notify
        return ccm_sidekick_notify.record_action(action, kind, msg_id, project, yes)
    path = _record_path(kind, msg_id, project)
    if action == "resend" and kind != "expired":
        ccm_core.ccm_die("Held records cannot be resent; inspect the target input box.")
    if action == "show":
        print(read_record(kind, msg_id, project))
        return
    if not yes:
        import sys
        if not sys.stdin.isatty():
            ccm_core.ccm_die("Confirmation required; review the record and pass --yes.")
        verb = "Start Claude if needed and resend" if start else action.capitalize()
        try:
            confirmed = input(f"{verb} {kind} record {msg_id} for {project}? [y/N] ")
        except EOFError:
            confirmed = ""
        if confirmed.lower() != "y":
            ccm_core.ccm_die("Cancelled; record kept.")
    pdir = _project_dir(project)
    if not _acquire_lock(pdir):
        ccm_core.ccm_die("Spool is busy; record kept. Retry later.")
    try:
        body = read_record(kind, msg_id, project)
        before = os.stat(path)
        if action == "resend":
            # --now makes every deferred delivery an error. A normal
            # return means the shared send acceptance check passed.
            send_args = ["--now", "--yes", "--file", path]
            if start:
                send_args.append("--start")
            ccm_send.cmd_send(send_args + ["--", project])
        # Do not remove a replacement created while the send waited.
        after = os.stat(path)
        if ((before.st_ino, before.st_mtime_ns, before.st_size)
                != (after.st_ino, after.st_mtime_ns, after.st_size)
                or read_record(kind, msg_id, project) != body):
            ccm_core.ccm_die("Record changed; kept for review.")
        os.unlink(path)
    except OSError as e:
        ccm_core.ccm_die(f"Could not finish {action}: {e}. Review the record before retrying.")
    finally:
        _release_lock(pdir)
    ccm_core.ccm_info(f"{'Resent and cleared' if action == 'resend' else 'Discarded'} {msg_id} ({project}).")


def cmd_spool(args):
    """Inspect and withdraw queued (store-and-forward) messages.

    Usage:
      ccm spool list [project]          Inspect pending, held and expired messages
      ccm spool cancel <id> [project]   Withdraw one
      ccm spool cancel --all [project]  Withdraw all
      ccm spool clear-expired [project] Acknowledge the undelivered
      ccm spool clear-held [project]    Acknowledge the held
    """
    if not args or args[0] in ("-h", "--help"):
        print(_SPOOL_USAGE)
        return
    sub, rest = args[0], args[1:]
    if sub in ("show", "discard", "resend"):
        _cmd_record(sub, rest)
    elif sub == "list":
        _cmd_list(rest)
    elif sub == "cancel":
        _cmd_cancel(rest)
    elif sub == "clear-expired":
        _cmd_clear_expired(rest)
    elif sub == "clear-held":
        _cmd_clear_held(rest)
    else:
        ccm_core.ccm_die(_SPOOL_USAGE)
