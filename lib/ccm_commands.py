"""Subcommand handlers for the ccm CLI.

Every public `cmd_*` function here corresponds to a `ccm <subcommand>`
invocation routed through the argparse dispatch in
`ccm_core.__main__`. Snapshot and `ccm send` handlers live in
`ccm_snapshot` / `ccm_send` because their bodies are large enough
to deserve their own modules; this file owns the lifecycle
commands (`add` / `open` / `register` / `unregister` / `rename` /
`remove` / `attach` / `list` / `capture` / `stop` / `reset_window`)
plus the diagnostic commands (`doctor` / `errors` / `debug trace`).

Cross-module discipline:
  - Immutable constants (`CLAUDE_CMD`, ANSI color strings) are
    pulled in via direct `from ccm_constants import …` /
    `from ccm_core import …`.
  - Mockable helpers (`tmux_cmd`, `get_session`, `find_window`,
    `build_project_list`, `hooks_configured`, …) and constants
    that tests mutate (`CCM_SNAPSHOT_DIR`) are accessed via
    `ccm_core.foo()` so that `unittest.mock.patch("ccm_core.foo")`
    and `monkeypatch.setattr(ccm_core, "foo", ...)` reach the
    callsites here. A direct from-import would freeze the binding
    at import time and bypass the mock.
"""

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime

# `ccm_core` is imported for its (mockable) helpers AND for the runtime
# constants the test suite mutates. See module docstring.
import ccm_core  # late-bound for tmux_cmd / ccm_die / build_project_list / etc.
import ccm_window
import ccm_canaries
import ccm_commands
import ccm_detection
import ccm_jsonl
import ccm_pane_state
import ccm_render
import ccm_rules
import ccm_signals
import ccm_snapshot
from ccm_constants import (CCM_VERSION, CLAUDE_CMD, CLAUDE_CONFIG_DIR,
                           external_agent_name)
from ccm_core import _C_BOLD, _C_RESET


# ─── Session commands ───


def _autosave_trigger():
    """Trigger autosave in background (non-blocking).

    Autosave is best-effort — its failure should not crash the
    caller. We do, however, surface the failure as a warning so
    the user is not left silently believing a snapshot exists.
    """
    try:
        ccm_snapshot.cmd_snapshot_save("_autosave", quiet=True)
    except Exception as exc:
        ccm_core.ccm_warn(f"Autosave failed: {exc}")


def cmd_add(directory, name="", start_claude=True, _loading=False,
            create_dir=False):
    """Add a new ccm project window.

    When `create_dir=True` and the directory does not exist, ccm
    will `mkdir` it provided the immediate parent already exists
    — one-level creation only, never recursive `mkdir -p`. The
    rationale: a typo in the path is a much more common failure
    mode than "I really want the full parent tree", so refusing
    when the parent is missing forces the caller to spell the
    intent explicitly. Default False keeps every existing caller
    (notably `cmd_snapshot_load`) on the original strict
    behavior — a stale snapshot whose dir was deleted should
    skip with a warning, not silently re-create an empty
    directory the user no longer expects to be there.
    """
    if not directory:
        ccm_core.ccm_die("Directory is required")

    directory = os.path.expanduser(directory)
    if os.path.exists(directory):
        try:
            directory = os.path.realpath(directory)
        except OSError:
            pass
    else:
        # Don't realpath a non-existent path — that would silently
        # resolve through any symlinks in the parent chain BEFORE
        # we've validated the leaf. Normalize to absolute so the
        # mkdir target / error messages are stable.
        directory = os.path.abspath(directory)

    if not os.path.isdir(directory):
        if not create_dir:
            ccm_core.ccm_die(f"Directory does not exist: {directory}")
        # Refuse if a non-directory already sits at the path; the
        # mkdir would error out anyway, but a tailored message
        # makes the cause obvious.
        if os.path.exists(directory):
            ccm_core.ccm_die(
                f"Path exists but is not a directory: {directory}"
            )
        parent = os.path.dirname(directory) or "/"
        if not os.path.isdir(parent):
            ccm_core.ccm_die(
                f"Cannot create directory: parent does not exist: {parent}"
            )
        try:
            os.mkdir(directory)
        except FileExistsError:
            # Raced with another process; only proceed if what's
            # there is actually a directory now.
            if not os.path.isdir(directory):
                ccm_core.ccm_die(
                    f"Path exists but is not a directory: {directory}"
                )
        except PermissionError:
            ccm_core.ccm_die(
                f"Permission denied creating directory: {directory}"
            )
        except OSError as exc:
            ccm_core.ccm_die(
                f"Failed to create directory: {directory} ({exc})"
            )
        # Now that the leaf exists, resolve symlinks (in case the
        # parent chain included any) so the tagged @ccm_dir is the
        # canonical form — matches the realpath() path taken when
        # the directory existed at entry.
        try:
            directory = os.path.realpath(directory)
        except OSError:
            pass
        ccm_core.ccm_info(f"Created directory: {directory}")

    if not name:
        name = os.path.basename(directory)
    name = ccm_core.validate_name(name)
    if not name:
        ccm_core.ccm_die(
            "Invalid project name (alphanumerics / hyphens / underscores only; "
            "shell metachars and whitespace are stripped; digit-only names "
            "are not allowed — they collide with window-index addressing)"
        )

    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die(
            "Not inside a tmux session — start one with `tmux new-session` first"
        )

    if ccm_core.project_exists(session, name):
        ccm_core.ccm_die(f"Project window already exists: {name}")

    # Check for duplicate directory
    for _idx, _wn, _proj, existing_dir in ccm_core.list_windows_raw(session):
        try:
            real_existing = os.path.realpath(os.path.expanduser(existing_dir))
        except OSError:
            real_existing = existing_dir
        if directory == real_existing:
            ccm_core.ccm_die(f"Directory already registered as project '{_proj}': {existing_dir}")

    # Create new window
    win_idx = ccm_core.tmux_cmd("new-window", "-P", "-F", "#{window_index}",
                                "-t", f"{session}:", "-n", name, "-c", directory)
    if not win_idx:
        ccm_core.ccm_die("Failed to create tmux window — check `tmux info` for server status")

    win_target = f"{session}:{win_idx}"

    # Tag the window with ccm metadata
    orig_name = ccm_core.tmux_cmd("display-message", "-t", win_target, "-p", "#{window_name}") or name
    ccm_core.tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_orig_name", orig_name),
        ("set-option", "-wt", win_target, "@ccm_project", name),
        ("set-option", "-wt", win_target, "@ccm_dir", directory),
        ("set-option", "-wt", win_target, "automatic-rename", "off"),
    )

    if start_claude:
        ccm_core.tmux_cmd("send-keys", "-t", win_target, CLAUDE_CMD, "Enter")

    ccm_core.ccm_info(f"Added project: {name} ({directory})")

    if not ccm_core.hooks_configured():
        ccm_core.ccm_warn("Hooks not installed. Run 'ccm setup-hooks' for accurate state detection.")

    if not _loading:
        # Tests patch `ccm_commands._autosave_trigger` — go through the
        # module attribute so the mock is observed.
        ccm_commands._autosave_trigger()


def cmd_open(directory, name=""):
    """Start Claude in the current pane (for split-pane use)."""
    if not directory:
        ccm_core.ccm_die("Directory is required")

    directory = os.path.expanduser(directory)
    try:
        directory = os.path.realpath(directory)
    except OSError:
        pass

    if not os.path.isdir(directory):
        ccm_core.ccm_die(f"Directory does not exist: {directory}")

    if not name:
        name = os.path.basename(directory)

    # shlex.quote for safety
    ccm_core.tmux_cmd("send-keys",
                      f"cd {shlex.quote(directory)} && (claude --continue 2>/dev/null || claude)",
                      "Enter")


def cmd_register(source_target, new_name=""):
    """Register an existing tmux window as a ccm project."""
    if not source_target:
        ccm_core.ccm_die("Usage: ccm register <window_name|window_index> [name]")

    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die("Not inside a tmux session — start one with `tmux new-session` first")

    # Find window by index or name
    if source_target.isdigit():
        win_idx = source_target
        win_name = ccm_core.tmux_cmd("display-message", "-t", f"{session}:{win_idx}",
                                     "-p", "#{window_name}")
        if not win_name:
            ccm_core.ccm_die(f"Window not found at index: {source_target}")
    else:
        raw = ccm_core.tmux_cmd("list-windows", "-t", session, "-F",
                                "#{window_index}\t#{window_name}")
        win_idx = None
        win_name = source_target
        if raw:
            for line in raw.split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1] == source_target:
                    win_idx = parts[0]
                    break
        if win_idx is None:
            ccm_core.ccm_die(f"Window not found: {source_target}")

    win_target = f"{session}:{win_idx}"

    # Check if already tagged
    existing = ccm_core.tmux_cmd("show-option", "-wt", win_target, "-qv", "@ccm_project")
    if existing:
        ccm_core.ccm_die(f"Already a ccm project: {existing}")

    name = new_name or win_name
    name = ccm_core.validate_name(name)
    if not name:
        ccm_core.ccm_die(
            "Invalid project name (alphanumerics / hyphens / underscores only; "
            "digit-only names are not allowed — they collide with "
            "window-index addressing)"
        )

    if ccm_core.project_exists(session, name):
        ccm_core.ccm_die(f"Project name already in use: {name}")

    # Get directory from pane
    pane_dir = ccm_core.tmux_cmd("display-message", "-t", win_target, "-p", "#{pane_current_path}")

    ccm_core.tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_orig_name", win_name),
        ("set-option", "-wt", win_target, "@ccm_project", name),
        ("set-option", "-wt", win_target, "@ccm_dir", pane_dir or ""),
        ("set-option", "-wt", win_target, "automatic-rename", "off"),
        ("rename-window", "-t", win_target, name),
    )

    ccm_core.ccm_info(f"Registered: {win_name} → {name}")
    ccm_commands._autosave_trigger()


def cmd_unregister(name):
    """Unregister window from ccm (keep window alive)."""
    if not name:
        ccm_core.ccm_die("Project name is required")

    session = ccm_core.get_session()
    idx = ccm_core.find_window(session, name)
    if idx is None:
        ccm_core.ccm_die(f"Project window not found: {name}")

    win_target = f"{session}:{idx}"

    # Capture @ccm_dir BEFORE we clear it — needed for runtime-file
    # cleanup below.
    proj_dir = ccm_core.tmux_cmd("show-option", "-wt", win_target, "-qv", "@ccm_dir")

    # Restore original name
    orig_name = ccm_core.tmux_cmd("show-option", "-wt", win_target, "-qv", "@ccm_orig_name")
    if orig_name:
        ccm_core.tmux_cmd("rename-window", "-t", win_target, orig_name)

    # Remove all ccm tags.
    tags = ["automatic-rename", "@ccm_project", "@ccm_dir", "@ccm_orig_name",
            "@ccm_prev_state", "@ccm_completed_at",
            "@ccm_state_icon", "@ccm_state_color",
            "@ccm_shell_history"]
    cmds = [("set-option", "-wt", win_target, "-u", tag) for tag in tags]
    ccm_core.tmux_batch(*cmds)

    ccm_signals.cleanup_project_runtime_files(proj_dir)

    ccm_core.ccm_info(f"Unregistered: {name} (window kept)")
    ccm_commands._autosave_trigger()


def cmd_rename(old_name, new_name):
    """Rename a ccm project."""
    if not old_name:
        ccm_core.ccm_die("Usage: ccm rename <current_name> <new_name>")
    if not new_name:
        ccm_core.ccm_die("New name is required")

    new_name = ccm_core.validate_name(new_name)
    if not new_name:
        ccm_core.ccm_die(
            "Invalid project name (alphanumerics / hyphens / underscores only; "
            "digit-only names are not allowed — they collide with "
            "window-index addressing)"
        )

    session = ccm_core.get_session()
    idx = ccm_core.find_window(session, old_name)
    if idx is None:
        ccm_core.ccm_die(f"Project not found: {old_name}")

    if ccm_core.project_exists(session, new_name):
        ccm_core.ccm_die(f"Project name already in use: {new_name}")

    win_target = f"{session}:{idx}"
    ccm_core.tmux_batch(
        ("set-option", "-wt", win_target, "@ccm_project", new_name),
        ("rename-window", "-t", win_target, new_name),
    )

    ccm_core.ccm_info(f"Renamed: {old_name} → {new_name}")
    ccm_commands._autosave_trigger()


def cmd_remove(name):
    """Remove a ccm project window (kill window)."""
    if not name:
        ccm_core.ccm_die("Project name is required")

    session = ccm_core.get_session()
    idx = ccm_core.find_window(session, name)
    if idx is None:
        ccm_core.ccm_die(f"Project window not found: {name}")

    win_target = f"{session}:{idx}"
    # Capture @ccm_dir BEFORE killing the window — kill-window removes
    # the tmux options along with it.
    proj_dir = ccm_core.tmux_cmd("show-option", "-wt", win_target, "-qv", "@ccm_dir")

    ccm_core.tmux_cmd("kill-window", "-t", win_target)
    ccm_signals.cleanup_project_runtime_files(proj_dir)

    ccm_core.ccm_info(f"Removed project: {name}")
    ccm_commands._autosave_trigger()


def cmd_list():
    """List all ccm-managed project windows."""
    session = ccm_core.get_session()
    if not session:
        print("No active projects.")
        return

    windows = ccm_core.list_windows_raw(session)
    if not windows:
        print("No active projects.")
        return

    print(f"{_C_BOLD}{'PROJECT':<20} {'DIRECTORY'}{_C_RESET}")
    print(f"{'-------':<20} {'---------'}")

    for _idx, _wn, project, proj_dir in windows:
        print(f"{ccm_render.pad_to_width(project, 20)} {proj_dir}")


def cmd_attach(target):
    """Switch to a ccm project window."""
    if not target:
        ccm_core.ccm_die("Project name or number is required")

    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die("Not inside a tmux session — start one with `tmux new-session` first")

    idx = None
    if target.isdigit():
        # By window index
        windows = ccm_core.list_windows_raw(session)
        for w_idx, _, _, _ in windows:
            if w_idx == target:
                idx = w_idx
                break
        if idx is None:
            ccm_core.ccm_die(f"No ccm project at window index: {target}")
    else:
        idx = ccm_core.find_window(session, target)
        if idx is None:
            # Try by window name
            raw = ccm_core.tmux_cmd("list-windows", "-t", session, "-F",
                                    "#{window_index}\t#{window_name}")
            if raw:
                for line in raw.split("\n"):
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[1] == target:
                        idx = parts[0]
                        break
            if idx is None:
                ccm_core.ccm_die(f"Project not found: {target}")

    # Check if already on this window
    current_idx = ccm_core.tmux_cmd("display-message", "-t", session, "-p", "#{window_index}")
    if current_idx == idx:
        ccm_core.ccm_info("Already in this window")
        return

    win_target = f"{session}:{idx}"

    # Auto-start Claude if SHELL state. Claude may live in ANY pane of
    # the window (split-pane layouts), not just the first — check all
    # pane pids before deciding to auto-start.
    pane_pids_raw = ccm_core.tmux_cmd("list-panes", "-t", win_target, "-F", "#{pane_pid}")
    if pane_pids_raw:
        pane_pids = {p.strip() for p in pane_pids_raw.split("\n") if p.strip()}
        # Check if claude is running as child of any pane
        try:
            ps_out = subprocess.run(["ps", "-eo", "ppid,comm"],
                                    capture_output=True, timeout=5)
            if ps_out.returncode != 0:
                # ps exited non-zero: stdout is untrustworthy. Assume
                # claude is running rather than risk a duplicate
                # auto-start into a live session.
                has_claude = True
            else:
                # macOS truncates `comm` mid-codepoint for apps with
                # multi-byte names — see ccm_core.ps_snapshot for the
                # full rationale. Decode permissively so the truncated
                # row does not abort the scan.
                ps_text = ps_out.stdout.decode("utf-8", errors="replace")
                has_claude = False
                for line in ps_text.strip().split("\n"):
                    fields = line.split()
                    if len(fields) >= 2 and fields[0] in pane_pids and fields[1] == "claude":
                        has_claude = True
                        break
        except (subprocess.TimeoutExpired, OSError):
            has_claude = True  # Assume running on error

        if not has_claude:
            ccm_window.auto_start_claude(win_target)

    ccm_window.reset_window_after_attach(win_target)
    ccm_core.tmux_cmd("select-window", "-t", f"{session}:{idx}")


def _capture_pane_label(pane):
    """Human-readable role for a captured pane's section header.

    `pane_current_command` alone is not enough: a claude pane
    reports the versioned launcher name (e.g. `2_1_220`), which
    reads as noise. Resolve the role instead — claude first (via the
    process-tree walk `enumerate_window_panes` already did), then a
    known external agent CLI, then the raw foreground command."""
    if pane.claude_pid:
        role = "claude"
    elif external_agent_name(pane.current_command):
        role = pane.current_command
    else:
        role = pane.current_command or "?"
    marks = []
    if pane.active:
        marks.append("active")
    if pane.ignored:
        # Ignored panes ARE captured: `CCM_IGNORE` means "ccm does not
        # track or write to this pane", not "hide it from an explicit
        # read the user asked for" — the sidekick is often exactly
        # what the operator wants to inspect. Marked so the output
        # still says which pane ccm keeps its hands off.
        marks.append("ignored")
    suffix = f" ({', '.join(marks)})" if marks else ""
    return f"{pane.pane_id} [{role}]{suffix}"


def _capture_window_text(session, idx):
    """Visible text of a window, pane by pane.

    `capture-pane -t <window>` delivers only the window's ACTIVE
    pane, so on a split window this silently returned one pane and
    dropped the rest — from inside the window that is usually the
    caller's own pane, and from outside it is whichever pane happened
    to hold focus. (Same window-vs-pane flaw the dashboard preview
    had before `_resolve_preview_pane`.) Enumerate the panes and
    capture each, labelled, so a split window is fully visible and
    the result does not depend on focus.

    Single-pane windows keep the previous output byte-for-byte (no
    headers). Enumeration failure falls back to the window target,
    preserving the old behaviour rather than returning nothing.

    The pane count is probed with a bare `list-panes` before the full
    enumeration so the common single-pane case never pays for a `ps`
    snapshot (`enumerate_window_panes` walks the process tree to
    resolve claude, which is only needed once there are labels to
    print)."""
    import ccm_pane_state

    win_target = f"{session}:{idx}"
    listed = ccm_core.tmux_cmd("list-panes", "-t", win_target, "-F", "#{pane_id}")
    pane_ids = [p for p in (listed or "").split("\n") if p.strip()]
    if len(pane_ids) <= 1:
        return ccm_core.tmux_cmd(
            "capture-pane", "-t", win_target, "-p", "-S", "-50") or ""

    ps_lines = ccm_core.ps_snapshot().strip().split("\n")
    panes = ccm_pane_state.enumerate_window_panes(win_target, ps_lines)
    if not panes:
        return ccm_core.tmux_cmd(
            "capture-pane", "-t", win_target, "-p", "-S", "-50") or ""

    sections = []
    for pane in panes:
        body = ccm_core.tmux_cmd(
            "capture-pane", "-t", pane.pane_id, "-p", "-S", "-50") or ""
        sections.append(f"--- pane {_capture_pane_label(pane)} ---\n{body}")
    return "\n".join(sections)


def cmd_capture(args):
    """Capture visible content of a project window."""
    if any(a in ("-h", "--help") for a in args):
        print("Usage: ccm capture [--copy|-c] <name|#id|window_index>")
        return
    copy_mode = False
    target = ""
    for arg in args:
        if arg in ("--copy", "-c"):
            copy_mode = True
        else:
            target = arg

    if not target:
        ccm_core.ccm_die("Usage: ccm capture [--copy] <name|#id>")

    session = ccm_core.get_session()

    # Resolve target to window index
    if target.startswith("#"):
        num = target[1:]
    elif target.isdigit():
        num = target
    else:
        num = None

    if num is not None:
        windows = ccm_core.list_windows_raw(session)
        idx = None
        proj_name = None
        for w_idx, _, proj, _ in windows:
            if w_idx == num:
                idx = w_idx
                proj_name = proj
                break
        if idx is None:
            ccm_core.ccm_die(f"No ccm project at window index: {num}")
    else:
        proj_name = target
        idx = ccm_core.find_window(session, target)
        if idx is None:
            ccm_core.ccm_die(f"Project not found: {target}")

    output = _capture_window_text(session, idx)

    if copy_mode:
        if ccm_core.clipboard_copy(output):
            ccm_core.ccm_info(f"Captured {proj_name} → clipboard")
        else:
            ccm_core.ccm_warn("No clipboard tool available (install pbcopy, xclip, or xsel)")
    else:
        print(f"=== ccm capture: {proj_name} ===")
        print(output)
        print("=== end ===")


def cmd_stop(target):
    """Stop project window(s)."""
    if target == "--all":
        session = ccm_core.get_session()
        windows = ccm_core.list_windows_raw(session)
        if not windows:
            print("No active projects.")
            return

        # Auto-save before stopping. Best-effort: if it fails, warn
        # the user but proceed with the stop. Silent failure is
        # explicitly avoided here so users do not believe a snapshot
        # exists when it does not.
        ccm_core.init_dirs()
        try:
            ccm_snapshot.cmd_snapshot_save("_autosave", quiet=True)
            ccm_core.ccm_info("Auto-saved snapshot: _autosave")
        except Exception as exc:
            ccm_core.ccm_warn(f"Autosave failed: {exc} — proceeding with stop")

        for w_idx, _, project, _ in windows:
            ccm_core.tmux_cmd("kill-window", "-t", f"{session}:{w_idx}")
            ccm_core.ccm_info(f"Stopped: {project}")
    elif target:
        cmd_remove(target)
    else:
        ccm_core.ccm_die("Usage: ccm stop [--all|<name>]")


def cmd_reset_window():
    """CLI handler for `ccm reset-window` — runs the post-attach reset
    on the current window. Internal plumbing used by the bash wrapper
    for attach paths that cannot call `reset_window_after_attach`
    directly; not user-facing."""
    session_name = ccm_core.tmux_cmd("display-message", "-p", "#{session_name}")
    win_idx = ccm_core.tmux_cmd("display-message", "-p", "#{window_index}")
    if session_name and win_idx:
        ccm_window.reset_window_after_attach(f"{session_name}:{win_idx}")


# Per-window tmux options that `ccm reset` unsets. Keep this list
# narrow: it must touch only ccm-owned ephemeral state, never
# `@ccm_project` / `@ccm_dir` (the source of truth that identifies
# the window as a ccm project) or anything tmux-built-in.
_RESET_WINDOW_OPTIONS = (
    "@ccm_prev_state",
    "@ccm_session_id",
    "@ccm_completed_at",
    "@ccm_shell_history",
    "@ccm_bg_active",
)


def cmd_reset(name):
    """`ccm reset <name>` — clear runtime state for a stuck project.

    Removes the project's ephemeral runtime artefacts:
      - hook signal / events / pending sentinel under `$HOOK_DIR/`
      - notify marker, git-branch and port caches under `$TMPDIR`
      - per-window tmux options that cache resolved state
        (`@ccm_prev_state`, `@ccm_session_id`, `@ccm_completed_at`,
        `@ccm_shell_history`, `@ccm_bg_active`)

    Does NOT touch:
      - the conversation JSONL (`~/.claude/projects/<slug>/...`)
      - Claude Code's session info (`~/.claude/sessions/<pid>.json`)
      - the running `claude` process
      - the tmux window itself (`@ccm_project`, `@ccm_dir`)
      - snapshots

    Intended as a recovery hatch for the rare cases where a stuck
    `(Nm)` suffix won't clear (e.g. the upstream double silent fail
    described in `memory/project_known_limitations.md`). For normal
    "Claude got stuck" situations, `/exit` inside the pane is still
    the right answer."""
    if not name:
        ccm_core.ccm_die("Usage: ccm reset <name>")
    session = ccm_core.get_session()
    win_idx = ccm_core.find_window(session, name)
    if not win_idx:
        ccm_core.ccm_die(f"Project not found: {name}")
    win_target = f"{session}:{win_idx}"

    # Resolve @ccm_dir BEFORE we wipe options, so we can find the
    # cwd-keyed runtime files.
    proj_dir = ccm_core.tmux_cmd(
        "show-option", "-w", "-t", win_target, "-qv", "@ccm_dir",
    )
    if proj_dir:
        ccm_signals.cleanup_project_runtime_files(proj_dir)

    # Unset the cached state options. Use `-u` (unset) rather than
    # writing an empty string so detection treats the project as
    # "fresh" on the next scan.
    for opt in _RESET_WINDOW_OPTIONS:
        ccm_core.tmux_cmd("set-option", "-w", "-t", win_target, "-u", opt)

    ccm_core.ccm_info(f"Reset runtime state for: {name}")


# ─── ignore / unignore ───
# `CCM_IGNORE=1 claude` makes a session invisible to ccm from launch
# (handled by hooks/lib.sh). These commands do the same at runtime for
# an already-running session, and reverse it. Both markers are set:
#   - the `@ccm_ignore` tmux PANE option (detection reads it from the
#     bulk list-panes query and drops the pane from aggregation,
#     session tracking, `ccm send`, and idle auto-exit);
#   - a `$HOOK_DIR/<sessionId>.ignore` file so the session's hooks
#     early-exit (no signals, no events, no desktop notifications).
# Scope: no arg → the caller's pane ($TMUX_PANE); a project name →
# every claude-hosting pane in that project's window (hide the whole
# project).

_IGNORE_PANE_TITLE = "⊘ ccm-ignored"


def _resolve_ignore_targets(name):
    """Return the list of tmux pane ids to (un)ignore.

    No name → the current pane from $TMUX_PANE (the pane `ccm ignore`
    was typed in). A project name → all panes of that project's
    window. Dies with a clear message when the target cannot be
    resolved."""
    if not name:
        pane = os.environ.get("TMUX_PANE", "").strip()
        if not pane:
            ccm_core.ccm_die(
                "ccm ignore: no pane context (run inside a tmux pane, "
                "or pass a project name)")
        return [pane]
    session = ccm_core.get_session()
    win_idx = ccm_core.find_window(session, name)
    if not win_idx:
        ccm_core.ccm_die(f"Project not found: {name}")
    raw = ccm_core.tmux_cmd(
        "list-panes", "-t", f"{session}:{win_idx}", "-F", "#{pane_id}")
    panes = [ln.strip() for ln in (raw or "").split("\n") if ln.strip()]
    if not panes:
        ccm_core.ccm_die(f"No panes found for project: {name}")
    return panes


def _pane_session_id(pane_id, ps_lines):
    """Resolve the claude session_id hosted in a pane, or None. Used to
    key the hook-suppression marker file on the session, matching how
    the hooks key their own artefacts."""
    import ccm_pane_state
    pane_pid = ccm_core.tmux_cmd(
        "display-message", "-p", "-t", pane_id, "#{pane_pid}")
    if not pane_pid:
        return None
    claude_pid = ccm_pane_state.find_claude_pid(pane_pid.strip(), ps_lines)
    if not claude_pid:
        return None
    info = ccm_jsonl.read_session_info(claude_pid, ps_lines)
    return info.get("sessionId") if info else None


def _ignore_marker_path(session_id):
    return os.path.join(ccm_core.CCM_HOOK_DIR, f"{session_id}.ignore")


def cmd_ignore(name=""):
    """`ccm ignore [project]` — hide a pane (default: current) or a
    whole project's window from ccm."""
    targets = _resolve_ignore_targets(name)
    ps_lines = ccm_core.ps_snapshot().strip().split("\n")
    border_optin = (ccm_core.tmux_cmd(
        "show-option", "-gqv", "@ccm-ignore-pane-border") == "on")
    n_panes = 0
    n_sessions = 0
    for pane in targets:
        ccm_core.tmux_cmd("set-option", "-p", "-t", pane, "@ccm_ignore", "1")
        ccm_core.tmux_cmd("select-pane", "-t", pane, "-T", _IGNORE_PANE_TITLE)
        n_panes += 1
        sid = _pane_session_id(pane, ps_lines)
        if sid:
            try:
                with open(_ignore_marker_path(sid), "w", encoding="utf-8"):
                    pass
                n_sessions += 1
            except OSError:
                pass
    if border_optin:
        ccm_core.tmux_cmd("set-option", "-g", "pane-border-status", "top")
    label = name or "current pane"
    ccm_core.ccm_info(
        f"Ignoring {label} ({n_panes} pane(s), {n_sessions} claude "
        f"session(s) silenced). Undo with `ccm unignore"
        f"{' ' + name if name else ''}`.")


def cmd_unignore(name=""):
    """`ccm unignore [project]` — restore a pane/project to ccm."""
    targets = _resolve_ignore_targets(name)
    ps_lines = ccm_core.ps_snapshot().strip().split("\n")
    for pane in targets:
        ccm_core.tmux_cmd("set-option", "-p", "-t", pane, "-u", "@ccm_ignore")
        ccm_core.tmux_cmd("select-pane", "-t", pane, "-T", "")
        sid = _pane_session_id(pane, ps_lines)
        if sid:
            try:
                os.unlink(_ignore_marker_path(sid))
            except OSError:
                pass
    # The global pane-border stays as the user left it — other ignored
    # panes may still rely on it, and it was an explicit opt-in.
    ccm_core.ccm_info(f"Restored {name or 'current pane'} to ccm.")


def cmd_doctor():
    """`ccm doctor` — single self-check command. Aggregates dependency
    versions, hook installation state, runtime canaries, project
    inventory, and a tail of the silent-exception log. Designed as
    "first thing to run when something feels wrong" and as a
    drop-in artefact for bug reports."""
    OK = f"{ccm_core._C_GREEN}✓{ccm_core._C_RESET}"
    WARN = f"{ccm_core._C_YELLOW}⚠{ccm_core._C_RESET}"
    FAIL = f"{ccm_core._C_RED}✗{ccm_core._C_RESET}"

    def section(title):
        print(f"\n{ccm_core._C_BOLD}{title}{ccm_core._C_RESET}")

    def row(mark, label, detail=""):
        print(f"  {mark} {label}{('  ' + detail) if detail else ''}")

    def _probe(args):
        """Run a dependency probe, returning stdout or "" when the
        probe itself fails. doctor is the dependency-check command —
        a missing `which`/`tmux` binary must surface as a "not found"
        row, not crash the whole report with FileNotFoundError."""
        try:
            return subprocess.run(
                args, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""

    section("Environment")
    # ccm itself — surfaces the running version up front so bug
    # reports include it without anyone having to remember to run
    # `ccm --version` separately.
    row(OK, "ccm", CCM_VERSION)
    # claude binary
    claude_path = _probe(["which", "claude"])
    if not claude_path:
        row(FAIL, "claude",
            "binary not found — install from https://docs.anthropic.com/en/docs/claude-code")
    else:
        version_out = _probe([claude_path, "--version"])
        row(OK, "claude", version_out or claude_path)
    # tmux
    tmux_ver = _probe(["tmux", "-V"])
    row(OK if tmux_ver else FAIL, "tmux", tmux_ver or "not found")
    # jq, fzf
    for tool in ("jq", "fzf"):
        path = _probe(["which", tool])
        row(OK if path else WARN, tool, path or "not found (recommended)")

    section("Setup")
    if ccm_core.hooks_configured():
        row(OK, "Hooks installed")
    else:
        row(WARN, "Hooks not installed",
            "run `ccm setup-hooks` for full state detection")
    claude_md = os.path.join(CLAUDE_CONFIG_DIR, "CLAUDE.md")
    if os.path.exists(claude_md):
        with open(claude_md, encoding="utf-8") as f:
            has_ccm = "ccm" in f.read().lower()
        if has_ccm:
            row(OK, "~/.claude/CLAUDE.md", "ccm section present")
        else:
            row(WARN, "~/.claude/CLAUDE.md",
                "ccm section absent — run `ccm setup-claude-md`")
    else:
        row(WARN, "~/.claude/CLAUDE.md", "missing")

    section("Runtime canaries")
    hooks_warn = ccm_canaries.hooks_log_warning()
    log_size = ccm_canaries.hooks_log_size()
    if hooks_warn:
        row(WARN, "hooks.log size", hooks_warn)
    elif log_size < 0:
        row(OK, "hooks.log size", "(absent)")
    else:
        row(OK, "hooks.log size", f"{log_size / (1024*1024):.1f} MB")
    dah = ccm_canaries.disable_all_hooks_warning()
    row(WARN if dah else OK,
        "disableAllHooks",
        dah or "not set")
    mho = ccm_canaries.managed_hooks_only_warning()
    row(WARN if mho else OK,
        "allowManagedHooksOnly",
        mho or "not set")

    projects = ccm_core.build_project_list(fast=False)
    cluster_msgs = ccm_canaries.shell_cluster_warnings(projects)
    if cluster_msgs:
        for msg in cluster_msgs:
            row(WARN, "cluster-SHELL transitions", msg)
    else:
        row(OK, "cluster-SHELL transitions",
            f"none in last {ccm_canaries.SHELL_CLUSTER_WINDOW // 60} min")

    # Hook-silence canary is opt-in (observe-first). Report its state
    # so `ccm doctor` explains why it is or isn't watching, then list
    # any live suspects (empty unless opted in).
    if ccm_canaries.hook_silence_enabled():
        silence_msgs = ccm_canaries.hook_silence_warnings(projects)
        if silence_msgs:
            for msg in silence_msgs:
                row(WARN, "hook-silence", msg)
        else:
            row(OK, "hook-silence", "on — no silent sessions")
        # Firing-log evidence count (default-on promotion review):
        # each past firing is one JSON line in the log; zero across a
        # long dogfood window is the "no false fires" evidence.
        fired = ccm_canaries.hook_silence_log_count()
        if fired:
            row(OK, "hook-silence log",
                f"{fired} firing(s) recorded — inspect "
                f"{ccm_canaries.hook_silence_log_path()}")
        else:
            row(OK, "hook-silence log", "no firings recorded")
    else:
        row(OK, "hook-silence",
            "off (opt in with `tmux set -g @ccm-hook-silence on`)")

    section(f"Active projects ({len(projects)})")
    if not projects:
        row(WARN, "(none registered)", "run `ccm add <dir>` to start")
    # Map sessionId → Claude Code version from the per-session
    # JSON files Claude writes to ~/.claude/sessions. Surfaces
    # mixed-version setups (one window on an old `claude` binary,
    # another auto-updated mid-day) without an extra subprocess
    # per session.
    version_map = ccm_jsonl.read_session_versions()
    for p in projects:
        state_color = {
            "PERMIT": ccm_core._C_YELLOW,
            "BUSY": ccm_core._C_GREEN,
            "IDLE": ccm_core._C_DIM,
            "SHELL": ccm_core._C_DIM,
            "DOWN": ccm_core._C_RED,
        }.get(p.state, "")
        state_label = f"{state_color}{p.state:<6}{ccm_core._C_RESET}"
        sid = ccm_core.tmux_cmd(
            "show-option", "-w", "-t", p.win_target,
            "-qv", "@ccm_session_id",
        ) or "(no session)"
        ver = version_map.get(sid, "")
        sid_label = f"{sid} v{ver}" if ver else sid
        row(OK, f"{p.name:<20}", f"{state_label}  {sid_label}")

    # Windows where more than one *visible* pane hosts claude. Two
    # readings are equally legitimate — Agent Teams teammates, or a
    # sidekick nobody hid — and they want opposite things, so this
    # states the fact and names both. It must not read as "hide one":
    # for the teammate reading that is advice which costs the reader a
    # PERMIT. Same reason the standing dashboard hint was dropped in
    # favour of the `ccm send` refusal line, which fires only once the
    # ambiguity has actually bitten.
    #
    # Computed here rather than carried on `Project`: only doctor wants
    # it, and doctor is on-demand, whereas `build_project_list` runs on
    # the 2-second detection cycle.
    #
    # One bulk `list-panes -a` for every window, not
    # `enumerate_window_panes` per project — that helper forks once per
    # window, which measured +40% on a 34-project doctor. Same trade
    # `_resolve_ignored_panes` already makes for the `⊘` count, and the
    # same reason: this is a whole-inventory question, so ask it once.
    multi_claude = []
    scan_failed = False
    try:
        # `"".split("\n")` is `[""]` — truthy. Filter so the guard
        # below means what it says instead of waving empty data through.
        doctor_ps = [ln for ln in ccm_core.ps_snapshot().split("\n") if ln.strip()]
        panes_cache = ccm_core._build_panes_cache()
    except Exception:
        doctor_ps, panes_cache, scan_failed = [], [], True
    for p in projects if (doctor_ps and panes_cache) else ():
        visible = sum(
            1 for pc in panes_cache
            if pc[0] == p.win_target
            and not ccm_core._pane_is_ignored(pc)
            and ccm_pane_state.find_claude_pid(pc[1], doctor_ps)
        )
        if visible >= 2:
            multi_claude.append(f"{p.name} ({visible})")
    if multi_claude:
        row(OK, "multi-claude windows",
            f"{', '.join(multi_claude)} — normal for Agent Teams, where "
            "each teammate's PERMIT has to stay visible. If one is a "
            "sidekick instead, `CCM_IGNORE=1` or the dashboard's `i` "
            "keeps it out of the window's state.")
    elif scan_failed:
        # A silently missing check reads as a passed check. doctor is
        # the command you run when something is already wrong, so say
        # which question went unanswered rather than dropping the row.
        row(WARN, "multi-claude windows",
            "not checked — reading panes or the process list failed")

    section("Silent-exception log")
    log_count = 0
    try:
        if os.path.exists(ccm_core.CCM_ERRORS_LOG):
            with open(ccm_core.CCM_ERRORS_LOG, encoding="utf-8") as f:
                log_count = sum(1 for _ in f)
    except OSError:
        pass
    if log_count == 0:
        row(OK, "errors.log", "empty")
    else:
        row(WARN, "errors.log",
            f"{log_count} record(s) — view with `ccm errors`")
    burst = ccm_canaries.errors_log_burst_warning()
    if burst:
        row(WARN, "errors burst", burst)

    section("Configuration")
    for key in ("CCM_ROOT", "CCM_TMP_DIR", "CCM_DATA_DIR", "CCM_HOOK_DIR"):
        val = getattr(ccm_core, key, None)
        if val:
            print(f"  {key:<20} {val}")
    print()


def cmd_errors(args):
    """`ccm errors [--clear]` — show or clear the silent-exception log.

    Reads `$TMPDIR/ccm-$UID/errors.log` (and `errors.log.1` if a
    rotation occurred), prints one human-readable line per record
    plus the traceback. With `--clear`, deletes both files.

    The log itself is written by `ccm_core.log_caught_exception`
    every time an inject_status / dashboard / build_project_list
    silent-catch barrier fires. An empty log means detection has
    been running cleanly.
    """
    if args and args[0] in ("-h", "--help"):
        print("Usage: ccm errors [--clear]")
        return
    if args and args[0] == "--clear":
        cleared = 0
        for path in (ccm_core.CCM_ERRORS_LOG, ccm_core.CCM_ERRORS_LOG_PREV):
            try:
                os.unlink(path)
                cleared += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                ccm_core.ccm_warn(f"Failed to remove {path}: {e}")
        ccm_core.ccm_info(
            f"Errors log cleared ({cleared} file(s) removed)"
        )
        return

    # Read previous epoch first, then current — chronological order.
    paths = [ccm_core.CCM_ERRORS_LOG_PREV, ccm_core.CCM_ERRORS_LOG]
    found_any = False
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            ccm_core.ccm_warn(f"Failed to read {path}: {e}")
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # malformed line; skip
            found_any = True
            ts = rec.get("ts", 0)
            try:
                when = (datetime.fromtimestamp(ts).isoformat(timespec="seconds")
                        if ts else "?")
            except (ValueError, OSError):
                when = "?"
            print(f"{when}  [{rec.get('scope', '?')}]  "
                  f"{rec.get('type', '')}: {rec.get('msg', '')}")
            tb = rec.get("traceback", "")
            for tb_line in tb.splitlines():
                print(f"    {tb_line}")
    if not found_any:
        print("No silent-caught errors logged.")


def cmd_debug_trace(project_match, interval=0.3):
    """Print one line per scan showing every DetectionContext input,
    the rule that would match, and the resolved state. Read-only — this
    does NOT mutate @ccm_prev_state or any runtime file, so it can be
    run alongside the live ccm dashboard without interfering.

    Useful for answering "why did this project flicker to BUSY for 10
    seconds after attach?" by correlating the rule-firing sequence
    with the user's observed event timeline.

    project_match: substring match against @ccm_project or @ccm_dir
    (basename). The first matching ccm window wins.

    interval: seconds between scans. Smaller values catch faster
    transients but cost more CPU; 0.3 s is enough to observe the
    sub-second ordering that matters for detection bugs.
    """
    import signal as _signal
    import time as _time

    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die("No tmux session detected — run inside tmux")

    # Resolve the project. Accept an exact @ccm_project match first,
    # then fall back to substring on project name or dir basename.
    raw = ccm_core.tmux_cmd(
        "list-windows", "-a", "-F",
        "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_dir}",
    )
    win_target = None
    proj_name = None
    proj_dir = None
    if raw:
        rows = []
        for line in raw.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1]:
                rows.append((parts[0], parts[1], parts[2]))
        # Exact match on name first
        for wt, name, d in rows:
            if name == project_match:
                win_target, proj_name, proj_dir = wt, name, d
                break
        if win_target is None:
            # Substring match on name or dir basename
            needle = project_match.lower()
            for wt, name, d in rows:
                basename = os.path.basename(d) if d else ""
                if needle in name.lower() or needle in basename.lower():
                    win_target, proj_name, proj_dir = wt, name, d
                    break
    if win_target is None:
        ccm_core.ccm_die(f"No ccm project matches: {project_match!r}")

    # Graceful Ctrl-C.
    stop = {"flag": False}
    def _sigint(_sig, _frame):
        stop["flag"] = True
    _signal.signal(_signal.SIGINT, _sigint)

    sys.stderr.write(
        f"# ccm debug trace: {proj_name} ({win_target}) — {proj_dir}\n"
        f"# interval={interval}s  Ctrl-C to stop\n"
        f"# columns: time  raw  prev  hook(state,age)  pid_age  jsonl(age,stop)  ev(derive)  rule[phase]  →  state[action]\n"
    )
    sys.stderr.flush()

    while not stop["flag"]:
        t0 = _time.monotonic()

        # Fresh ps + pane snapshots each tick. No caching — we want to
        # observe real kernel/tmux state, not anything ccm has
        # memoized.
        ps_lines = ccm_core.ps_snapshot().strip().split("\n")
        # Match build_project_list's 6-field format so detect_window_raw
        # has the same sliver-exclusion / aggregation inputs during
        # trace runs as in production. Truncated tuples would fall
        # through legacy fallbacks and silently observe a different
        # detection path, defeating the trace tool.
        panes_raw = ccm_core.tmux_cmd(
            "list-panes", "-a", "-F",
            "#{session_name}:#{window_index}\t#{pane_pid}\t#{pane_id}\t#{pane_current_command}\t#{pane_active}\t#{pane_height}",
        )
        panes_cache = []
        for line in panes_raw.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 6:
                panes_cache.append(tuple(parts[:6]))
        own_pgid = str(os.getpgrp())

        prev_state = ccm_core.tmux_cmd(
            "show-option", "-wqv", "-t", win_target, "@ccm_prev_state",
        ) or ""

        # Invalidate the JSONL activity cache so each tick is a fresh
        # read — without this, trace would show stale ages across
        # successive ticks while the JSONL file actually changes.
        ccm_jsonl._jsonl_activity_cache.clear()

        ctx = ccm_detection.build_detection_context(
            win_target, proj_dir, prev_state,
            panes_cache, ps_lines, own_pgid,
        )
        # Use the REAL two-path merge (event-log derive primary,
        # legacy fallback), not evaluate_rules alone. The trace used
        # to run only the legacy table, which silently observed a
        # different detection path than production — during the
        # 2026-07-04 jwriter phantom-subagent incident it printed
        # "default → IDLE" while the live pipeline was resolving
        # derive=BUSY, misdirecting the investigation. Still
        # read-only: resolve_state_from_context has no side effects
        # (apply_actions is what writes, and we don't call it).
        state, rule, event_log_state = (
            ccm_detection.resolve_state_from_context(ctx, proj_dir))

        hook_str = f"{ctx.hook_state or '-'},{ctx.hook_age if ctx.hook_age >= 0 else '-'}"
        pid_age = ctx.claude_pid_age if ctx.claude_pid_age >= 0 else "-"
        jsonl_str = f"{ctx.jsonl_age if ctx.jsonl_age >= 0 else '-'},{ctx.jsonl_last_stop_reason or '-'}"
        action_short = "WRITE" if rule.action == ccm_rules.Action.DEFAULT else "HOLD"
        # Phase annotation is metadata only (Step 1 of phase-machine
        # roadmap) — show it next to the rule name so "why fired?"
        # investigations include the intended session-lifecycle scope.
        rule_label = f"{rule.name}[{rule.phase or '-'}]"

        sys.stdout.write(
            f"{_time.strftime('%H:%M:%S')}  "
            f"raw={ctx.raw:5}  prev={prev_state or '-':5}  "
            f"hook={hook_str:10}  pid_age={str(pid_age):4}  "
            f"jsonl={jsonl_str:20}  "
            f"ev={event_log_state or '-':5}  "
            f"{rule_label:42} → {state:5} [{action_short}]\n"
        )
        sys.stdout.flush()

        # Sleep the remaining budget so the loop runs at a steady
        # cadence even when a scan takes varying time.
        elapsed = _time.monotonic() - t0
        remaining = interval - elapsed
        if remaining > 0:
            _time.sleep(remaining)


# ─── Sidekick attention hooks (per-CLI adapters) ───

# Registry of sidekick CLIs whose own hook system can run ccm's
# attention adapter. Each entry knows where the CLI's hook config
# lives and how to render the managed block. Only measured
# integrations belong here: Kimi's was verified live (kimi 0.31.1,
# 2026-08-05 — PermissionRequest/PermissionResult fire, $TMUX_PANE is
# inherited, config loads at session start). Gemini / Grok Build have
# hook systems but their permission-wait events are unverified;
# Codex has no approval-time hook at all (openai/codex#11808).
_SIDEKICK_BLOCK_BEGIN = "# ccm:sidekick-attention begin (managed by ccm)"
_SIDEKICK_BLOCK_END = "# ccm:sidekick-attention end"

# Events that open a wait vs. events that end one — the adapter
# script dispatches on the payload's hook_event_name, so every event
# routes to the same command line.
_KIMI_ATTENTION_EVENTS = (
    "PermissionRequest", "PermissionResult", "Interrupt",
    "Stop", "StopFailure", "SessionEnd",
)


def _kimi_config_path():
    return os.environ.get(
        "CCM_KIMI_CONFIG",
        os.path.expanduser("~/.kimi-code/config.toml"))


def _kimi_attention_block():
    """The managed `[[hooks]]` block for Kimi's config.toml.

    Kimi's TOML hook parser is strict — an unknown field inside a
    `[[hooks]]` entry fails the WHOLE config load (measured constraint)
    — so entries carry exactly `event` / `command` / `timeout` and
    nothing else. The script path is single-quoted inside the TOML
    string so a plugin directory containing spaces survives the shell."""
    script = os.path.join(ccm_core.CCM_ROOT, "hooks", "sidekick-attention.sh")
    lines = [_SIDEKICK_BLOCK_BEGIN]
    for event in _KIMI_ATTENTION_EVENTS:
        lines += [
            "[[hooks]]",
            f'event = "{event}"',
            f"command = \"'{script}' kimi\"",
            "timeout = 5",
        ]
    lines.append(_SIDEKICK_BLOCK_END)
    return "\n".join(lines) + "\n"


def _strip_sidekick_block(text):
    """Remove a previously-installed managed block (idempotency)."""
    out, skipping = [], False
    for line in text.split("\n"):
        if line.strip() == _SIDEKICK_BLOCK_BEGIN:
            skipping = True
            continue
        if line.strip() == _SIDEKICK_BLOCK_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    # Collapse the blank-line seam the removal leaves behind.
    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def _grok_hook_path():
    return os.path.join(
        os.environ.get("CCM_GROK_HOME", os.path.expanduser("~/.grok")),
        "hooks", "ccm-sidekick-attention.json")


# Grok Build's permission-wait signal (measured 2026-08-05, grok
# 0.2.118): it has no PermissionRequest event — the wait arrives as
# `Notification` with `notificationType: "permission_prompt"`. There
# is no resolution event either, so the next activity event closes
# the wait, exactly as for a Claude sidekick.
_GROK_ATTENTION_EVENTS = (
    "Notification", "PostToolUse", "PostToolUseFailure",
    "PreToolUse", "Stop", "StopFailure", "SessionEnd",
    "PermissionDenied",
)


def _grok_attention_hooks():
    script = os.path.join(ccm_core.CCM_ROOT, "hooks", "sidekick-attention.sh")
    command = f"{shlex.quote(script)} grok"
    return {
        "hooks": {
            event: [{"hooks": [{"type": "command",
                                "command": command,
                                "timeout": 5}]}]
            for event in _GROK_ATTENTION_EVENTS
        }
    }


def _setup_grok_hooks():
    """Grok Build reads `~/.grok/hooks/*.json`, so ccm ships a file of
    its own instead of editing the user's config — nothing of theirs
    to back up, break, or merge with, and removal is an unlink."""
    path = _grok_hook_path()
    if not os.path.isdir(os.path.dirname(os.path.dirname(path))):
        ccm_core.ccm_die(
            f"{os.path.dirname(os.path.dirname(path))} not found — is "
            "Grok Build installed?")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_grok_attention_hooks(), f, indent=2)
        f.write("\n")
    ccm_core.ccm_info(f"attention hooks installed at {path}")
    print("  Takes effect in NEW Grok sessions. Grok has no permission-"
          "resolution event, so\n  a wait is cleared by the next activity "
          "event (or its TTL after an Esc).")


def cmd_setup_sidekick_hooks(agent):
    """`ccm setup-sidekick-hooks <agent>` — install ccm's attention
    adapter into the sidekick CLI's own hook config, so the sidekick
    self-reports "waiting on a decision" without ccm parsing its
    screen."""
    if agent == "grok":
        _setup_grok_hooks()
        return
    if agent != "kimi":
        ccm_core.ccm_die(
            f"unsupported sidekick agent: {agent!r}. Supported: kimi, grok.\n"
            "  Codex has no approval-time hook (openai/codex#11808), so a "
            "wait cannot be observed there at all. Antigravity CLI is not "
            "yet measured.")
    config = _kimi_config_path()
    if not os.path.isdir(os.path.dirname(config)):
        ccm_core.ccm_die(
            f"{os.path.dirname(config)} not found — is Kimi Code "
            "installed? (Its installer creates the directory.)")
    text = ""
    if os.path.exists(config):
        with open(config, encoding="utf-8") as f:
            text = f.read()
        with open(config + ".ccm-bak", "w", encoding="utf-8") as f:
            f.write(text)
    text = _strip_sidekick_block(text)
    if text and not text.endswith("\n"):
        text += "\n"
    if text.strip():
        text += "\n"
    text += _kimi_attention_block()
    with open(config, "w", encoding="utf-8") as f:
        f.write(text)
    ccm_core.ccm_info(f"attention hooks installed into {config}")
    print("  Takes effect in NEW Kimi sessions only — the config is "
          "loaded at session start,\n  so restart the sidekick pane's "
          "kimi to activate. Backup: config.toml.ccm-bak")


def cmd_remove_sidekick_hooks(agent):
    """`ccm remove-sidekick-hooks <agent>` — uninstall the adapter."""
    if agent == "grok":
        path = _grok_hook_path()
        if not os.path.exists(path):
            ccm_core.ccm_info("nothing to remove (no ccm hook file found)")
            return
        os.unlink(path)
        ccm_core.ccm_info(f"attention hooks removed: {path}")
        return
    if agent != "kimi":
        ccm_core.ccm_die(
            f"unsupported sidekick agent: {agent!r}. Supported: kimi, grok.")
    config = _kimi_config_path()
    if not os.path.exists(config):
        ccm_core.ccm_info("nothing to remove (no Kimi config found)")
        return
    with open(config, encoding="utf-8") as f:
        text = f.read()
    if _SIDEKICK_BLOCK_BEGIN not in text:
        ccm_core.ccm_info("nothing to remove (no ccm block in config)")
        return
    with open(config + ".ccm-bak", "w", encoding="utf-8") as f:
        f.write(text)
    with open(config, "w", encoding="utf-8") as f:
        f.write(_strip_sidekick_block(text))
    ccm_core.ccm_info(f"attention hooks removed from {config}")
    print("  Takes effect in NEW Kimi sessions (running ones loaded "
          "the old config).")
