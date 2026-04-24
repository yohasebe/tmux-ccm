"""Subcommand handlers for the ccm CLI.

Extracted from `ccm_core` in R2 Stage B so the core module can stay
focused on constants, data model, tmux helpers, and state detection.
Every public `cmd_*` function here corresponds to a `ccm <subcommand>`
invocation routed through the CLI dispatch in `ccm_core.__main__`.

Cross-module discipline (mirrors `ccm_detection.py`):
  - Truly immutable constants (`CLAUDE_CMD`, ANSI color strings) are
    pulled in with `from ccm_core import ...` for readability.
  - Everything else — helpers that tests mock (`tmux_cmd`, `get_session`,
    `find_window`, `build_project_list`, `hooks_configured`,
    `_autosave_trigger`, ...) and constants that tests mutate
    (`CCM_SNAPSHOT_DIR`) — is accessed via `ccm_core.foo()` so that
    `unittest.mock.patch("ccm_core.foo")` and
    `monkeypatch.setattr(ccm_core, "foo", ...)` keep reaching the
    callsites inside this module. A direct from-import would freeze
    the binding at import time and bypass the mock.

`ccm_core` re-exports every public symbol from this file at the bottom
of its module, so existing `from ccm_core import cmd_add` callers
(dashboard, tests, CLI dispatch) do not change.
"""

import glob
import json
import os
import shlex
import subprocess
import sys
import time

# `ccm_core` is imported for its (mockable) helpers AND for the runtime
# constants the test suite mutates. See module docstring.
import ccm_core  # noqa: F401 (used for late-bound attribute access)
from ccm_core import (
    CLAUDE_CMD,
    _C_BOLD,
    _C_RESET,
)


# ─── Snapshot commands ───


def _sanitize_snapshot_name(name):
    """Sanitize snapshot name to prevent path traversal."""
    # Strip path components — only keep the basename
    name = os.path.basename(name)
    # Remove any remaining dots that could cause issues (e.g., ".." left over)
    name = name.strip(".")
    if not name:
        ccm_core.ccm_die("Invalid snapshot name")
    return name


def cmd_snapshot_save(name="", quiet=False):
    """Save current projects as a snapshot."""
    if not name:
        try:
            name = input("Snapshot name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not name:
        ccm_core.ccm_die("Snapshot name is required")
    name = _sanitize_snapshot_name(name)

    ccm_core.init_dirs()

    # Scan ALL sessions for ccm-tagged windows
    raw = ccm_core.tmux_cmd("list-windows", "-a", "-F",
                            "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}")
    if not raw:
        if not quiet:
            ccm_core.ccm_die("No active projects to save")
        return

    projects_list = []
    for line in raw.split("\n"):
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        project, proj_dir = parts[2], parts[3]
        if not project or not proj_dir:
            continue
        # Replace $HOME with ~ for portability
        proj_dir = proj_dir.replace(os.path.expanduser("~"), "~")
        projects_list.append({
            "name": project,
            "dir": proj_dir,
            "auto_start_claude": True,
        })

    if not projects_list:
        if not quiet:
            ccm_core.ccm_die("No active projects to save")
        return

    snapshot = {
        "version": 1,
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "projects": projects_list,
    }

    file_path = os.path.join(ccm_core.CCM_SNAPSHOT_DIR, f"{name}.json")
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, file_path)

    if not quiet:
        ccm_core.ccm_info(f"Snapshot saved: {name} ({file_path})")


def cmd_snapshot_load(name=""):
    """Load and restore a snapshot."""
    ccm_core.init_dirs()
    if not name:
        files = sorted(glob.glob(os.path.join(ccm_core.CCM_SNAPSHOT_DIR, "*.json")))
        if not files:
            ccm_core.ccm_die("No snapshots found")
        items = [os.path.splitext(os.path.basename(f))[0] for f in files]
        name = ccm_core.fzf_select(items, "Select snapshot: ")
        if not name:
            return

    name = _sanitize_snapshot_name(name)
    file_path = os.path.join(ccm_core.CCM_SNAPSHOT_DIR, f"{name}.json")
    if not os.path.exists(file_path):
        ccm_core.ccm_die(f"Snapshot not found: {name}")

    with open(file_path) as f:
        data = json.load(f)

    snap_projects = data.get("projects", [])
    print(f"Loading snapshot: {name} ({len(snap_projects)} projects)")

    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die("Not inside a tmux session")

    for proj in snap_projects:
        proj_name = proj.get("name", "")
        proj_dir = proj.get("dir", "")
        if not proj_name or proj_name == "null":
            continue
        if not proj_dir or proj_dir == "null":
            continue
        proj_dir = os.path.expanduser(proj_dir)
        try:
            proj_dir = os.path.realpath(proj_dir)
        except OSError:
            pass

        if ccm_core.project_exists(session, proj_name):
            ccm_core.ccm_warn(f"Project window already exists, skipping: {proj_name}")
            continue
        if not os.path.isdir(proj_dir):
            ccm_core.ccm_warn(f"Directory not found, skipping: {proj_name} ({proj_dir})")
            continue

        # Don't auto-start Claude on restore — saves resources
        cmd_add(proj_dir, proj_name, start_claude=False, _loading=True)

    # Save autosave after all projects loaded
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        ccm_core.ccm_warn("Failed to save autosave snapshot after load")

    ccm_core.ccm_info(f"Snapshot loaded: {name}")


def cmd_snapshot_list():
    """List available snapshots."""
    ccm_core.init_dirs()
    files = sorted(glob.glob(os.path.join(ccm_core.CCM_SNAPSHOT_DIR, "*.json")))
    if not files:
        print("No snapshots.")
        return

    print(f"{_C_BOLD}{'NAME':<20} {'CREATED':<24} {'PROJECTS'}{_C_RESET}")
    print(f"{'----':<20} {'-------':<24} {'--------'}")

    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            name = data.get("name", os.path.splitext(os.path.basename(fp))[0])
            created = data.get("created", "-")
            count = len(data.get("projects", []))
            print(f"{name:<20} {created:<24} {count}")
        except (json.JSONDecodeError, OSError):
            pass


def cmd_snapshot_delete(name=""):
    """Delete a snapshot."""
    ccm_core.init_dirs()
    if not name:
        files = sorted(glob.glob(os.path.join(ccm_core.CCM_SNAPSHOT_DIR, "*.json")))
        if not files:
            ccm_core.ccm_die("No snapshots found")
        items = [os.path.splitext(os.path.basename(f))[0] for f in files]
        name = ccm_core.fzf_select(items, "Delete snapshot: ")
        if not name:
            return

    name = _sanitize_snapshot_name(name)
    file_path = os.path.join(ccm_core.CCM_SNAPSHOT_DIR, f"{name}.json")
    if not os.path.exists(file_path):
        ccm_core.ccm_die(f"Snapshot not found: {name}")

    os.unlink(file_path)
    ccm_core.ccm_info(f"Snapshot deleted: {name}")


# ─── Session commands ───


def _autosave_trigger():
    """Trigger autosave in background (non-blocking)."""
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        pass


def cmd_add(directory, name="", start_claude=True, _loading=False):
    """Add a new ccm project window."""
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
    name = ccm_core.validate_name(name)
    if not name:
        ccm_core.ccm_die("Invalid project name")

    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die("Not inside a tmux session")

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
        ccm_core.ccm_die("Failed to create window")

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
        # Tests patch `ccm_core._autosave_trigger` — go through the
        # module attribute so the mock is observed.
        ccm_core._autosave_trigger()


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
        ccm_core.ccm_die("Not inside a tmux session")

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
        ccm_core.ccm_die("Invalid project name")

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
    ccm_core._autosave_trigger()


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

    # Remove all ccm tags. Legacy tags (`@ccm_done`, `@ccm_last_done`)
    # from the pre-4-state model are included so v0.1.0 installs that
    # have lingering tmux options get a clean unregister after upgrade.
    tags = ["automatic-rename", "@ccm_project", "@ccm_dir", "@ccm_orig_name",
            "@ccm_prev_state", "@ccm_completed_at",
            "@ccm_state_icon", "@ccm_state_color",
            "@ccm_shell_history",
            "@ccm_done", "@ccm_last_done"]
    cmds = [("set-option", "-wt", win_target, "-u", tag) for tag in tags]
    ccm_core.tmux_batch(*cmds)

    ccm_core.cleanup_project_runtime_files(proj_dir)

    ccm_core.ccm_info(f"Unregistered: {name} (window kept)")
    ccm_core._autosave_trigger()


def cmd_rename(old_name, new_name):
    """Rename a ccm project."""
    if not old_name:
        ccm_core.ccm_die("Usage: ccm rename <current_name> <new_name>")
    if not new_name:
        ccm_core.ccm_die("New name is required")

    new_name = ccm_core.validate_name(new_name)
    if not new_name:
        ccm_core.ccm_die("Invalid project name")

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
    ccm_core._autosave_trigger()


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
    ccm_core.cleanup_project_runtime_files(proj_dir)

    ccm_core.ccm_info(f"Removed project: {name}")
    ccm_core._autosave_trigger()


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
        print(f"{project:<20} {proj_dir}")


def cmd_attach(target):
    """Switch to a ccm project window."""
    if not target:
        ccm_core.ccm_die("Project name or number is required")

    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die("Not inside a tmux session")

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

    # Auto-start Claude if SHELL state
    pane_pid = ccm_core.tmux_cmd("list-panes", "-t", win_target, "-F", "#{pane_pid}")
    if pane_pid:
        pane_pid = pane_pid.split("\n")[0]
        # Check if claude is running as child
        try:
            ps_out = subprocess.run(["ps", "-eo", "ppid,comm"],
                                    capture_output=True, text=True, timeout=5)
            has_claude = False
            for line in ps_out.stdout.strip().split("\n"):
                fields = line.split()
                if len(fields) >= 2 and fields[0] == pane_pid and fields[1] == "claude":
                    has_claude = True
                    break
        except (subprocess.TimeoutExpired, OSError):
            has_claude = True  # Assume running on error

        if not has_claude:
            ccm_core.auto_start_claude(win_target)

    ccm_core.reset_window_after_attach(win_target)
    ccm_core.tmux_cmd("select-window", "-t", f"{session}:{idx}")


def cmd_capture(args):
    """Capture visible content of a project window."""
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

    output = ccm_core.tmux_cmd("capture-pane", "-t", f"{session}:{idx}", "-p", "-S", "-50")

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

        # Auto-save before stopping
        ccm_core.init_dirs()
        try:
            cmd_snapshot_save("_autosave", quiet=True)
            ccm_core.ccm_info("Auto-saved snapshot: _autosave")
        except Exception:
            pass

        for w_idx, _, project, _ in windows:
            ccm_core.tmux_cmd("kill-window", "-t", f"{session}:{w_idx}")
            ccm_core.ccm_info(f"Stopped: {project}")
    elif target:
        cmd_remove(target)
    else:
        ccm_core.ccm_die("Usage: ccm stop [--all|<name>]")


def cmd_send(args):
    """Send a prompt to a project's Claude Code session.

    Usage:
      ccm send <name|#idx> <message>       Send literal message + Enter
      ccm send <name> --file <path>        Read message from file
      ccm send <name> --stdin              Read message from stdin
      ccm send <name> --no-enter <msg>     Send without submitting
      ccm send <name> --force <msg>        Send to a BUSY project (queued)
      ccm send <name> --start <msg>        Auto-launch Claude if SHELL
      ccm send -y <name> <msg>             Skip confirmation prompt
      ccm send <name> -- "--literal"       `--` ends flag parsing

    State policy:
      IDLE         → send immediately
      BUSY         → refuse without --force; queue into buffer with --force
      PERMIT       → ALWAYS refuse (hard guard — typing into a permission
                     dialog could accidentally approve or deny a tool call)
      SHELL        → refuse without --start; launch Claude + 2s wait with --start

    Multi-line messages (`\\n` in content) are converted to M-Enter between
    lines + a final Enter, matching Claude Code's "newline without submit"
    convention.
    """
    target = None
    positional_parts = []
    message_file = None
    use_stdin = False
    no_enter = False
    force = False
    auto_start = False
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
            elif arg in ("-y", "--yes"):
                skip_confirm = True
            else:
                ccm_core.ccm_die(
                    f"Unknown flag: {arg}\n"
                    "Usage: ccm send <name> <message> "
                    "[--file path] [--stdin] [--force] [--start] "
                    "[--no-enter] [-y]"
                )
        else:
            if arg == "-":  # conventional stdin alias
                use_stdin = True
            elif target is None:
                target = arg
            else:
                positional_parts.append(arg)
        i += 1

    if not target:
        ccm_core.ccm_die(
            "Usage: ccm send <name> <message> "
            "[--file path] [--stdin] [--force] [--start] "
            "[--no-enter] [-y]"
        )

    # Resolve message source (exactly one of the three)
    positional_message = " ".join(positional_parts) if positional_parts else None
    source_count = sum(x is not None and x is not False for x in
                       (positional_message, message_file, use_stdin or None))
    if source_count == 0:
        ccm_core.ccm_die("No message provided (positional, --file, or --stdin)")
    if source_count > 1:
        ccm_core.ccm_die("Provide exactly one of: positional message, --file, or --stdin")

    if message_file:
        try:
            with open(message_file) as f:
                message = f.read()
        except OSError as e:
            ccm_core.ccm_die(f"Failed to read message file: {e}")
    elif use_stdin:
        message = sys.stdin.read()
        # Once we have consumed stdin, the interactive confirmation
        # prompt can no longer read from it (EOFError). Force-skip
        # confirmation so a TTY user running `ccm send blog --stdin`
        # and typing a body terminated by Ctrl-D is not silently
        # cancelled.
        skip_confirm = True
    else:
        message = positional_message

    if not message.strip() and not no_enter:
        ccm_core.ccm_die("Empty message (use --no-enter to send only Enter suppression)")

    # Resolve target window
    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die("Not inside a tmux session")

    if target.startswith("#"):
        idx = target[1:]
    elif target.isdigit():
        idx = target
    else:
        idx = ccm_core.find_window(session, target)
        if idx is None:
            ccm_core.ccm_die(f"Project not found: {target}")

    win_target = f"{session}:{idx}"

    # Look up project state from the current ccm scan
    projects = ccm_core.build_project_list(fast=False)
    matched = next((p for p in projects if p.win_target == win_target), None)
    if matched is None:
        ccm_core.ccm_die(f"Window is not a registered ccm project: {win_target}")

    project_name = matched.name
    state = matched.state

    # State-based gating
    if state == "PERMIT":
        # Give the caller (human or another Claude) enough information
        # to understand what the target pane is blocked on. The
        # refusal itself is unconditional — PERMIT is never auto-
        # dismissed from another pane even when the modal is safe,
        # because misclassification of a real permission dialog could
        # accidentally approve a tool call.
        raw_tail = ccm_core.tmux_cmd(
            "capture-pane", "-t", win_target, "-p", "-S", "-10"
        ) or ""
        if not raw_tail.strip():
            raw_tail = ccm_core.tmux_cmd(
                "capture-pane", "-a", "-t", win_target, "-p", "-S", "-10"
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

    if state == "SHELL":
        if not auto_start:
            ccm_core.ccm_die(
                f"{project_name} is in SHELL state (Claude not running). "
                "Use --start to auto-launch Claude before sending."
            )
        ccm_core.ccm_info(f"Starting Claude in {project_name}...")
        ccm_core.tmux_cmd("send-keys", "-t", win_target, "-X", "cancel")
        ccm_core.tmux_cmd("send-keys", "-t", win_target, CLAUDE_CMD, "Enter")
        # Crude wait for Claude to initialize. Longer would block ccm
        # pipelines; shorter risks sending before the input prompt is
        # ready. 2 seconds is a reasonable compromise on modern hardware.
        time.sleep(2)

    if state == "BUSY" and not force:
        ccm_core.ccm_die(
            f"{project_name} is BUSY. The message would queue in the "
            "input buffer and mix with Claude's current turn. Use --force "
            "if that is what you want."
        )

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

    # Defensively exit any tmux mode on the target pane. Without this,
    # a pane stuck in copy-mode would interpret the message characters
    # as copy-mode bindings (same class of bug as the dashboard attach
    # fix in d1ca09b).
    ccm_core.tmux_cmd("send-keys", "-t", win_target, "-X", "cancel")

    # Literal send, converting `\n` into M-Enter (Claude Code's
    # "newline without submit" key) so the body is delivered as a
    # single multi-line prompt rather than multiple submitted turns.
    lines = message.split("\n")
    for line_i, line in enumerate(lines):
        if line:
            ccm_core.tmux_cmd("send-keys", "-t", win_target, "-l", line)
        if line_i < len(lines) - 1:
            ccm_core.tmux_cmd("send-keys", "-t", win_target, "M-Enter")

    # Final submit (unless --no-enter)
    if not no_enter:
        ccm_core.tmux_cmd("send-keys", "-t", win_target, "Enter")

    ccm_core.ccm_info(f"Sent to {project_name}")


def cmd_reset_window():
    """CLI handler for `ccm reset-window` — runs the post-attach reset
    on the current window. Internal plumbing used by the bash wrapper
    for attach paths that cannot call `reset_window_after_attach`
    directly; not user-facing."""
    session_name = ccm_core.tmux_cmd("display-message", "-p", "#{session_name}")
    win_idx = ccm_core.tmux_cmd("display-message", "-p", "#{window_index}")
    if session_name and win_idx:
        ccm_core.reset_window_after_attach(f"{session_name}:{win_idx}")


def cmd_debug_trace(project_match, interval=0.3):
    """Print one line per scan showing every DetectionContext input,
    the rule that would match, and the resolved state. Read-only — this
    does NOT mutate @ccm_prev_state or any runtime file, so it can be
    run alongside the live ccm dashboard without interfering.

    Useful for answering "why did this project flicker to BUSY for 10
    seconds after attach?" by correlating the rule-firing sequence
    with the user's observed event timeline. See
    `.claude/projects/.../memory/feedback_detection_debug_playbook.md`
    for the full usage recipe.

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
        f"# columns: time  raw  prev  hook(state,age)  pid_age  jsonl(age,stop)  rule[phase]  →  state[action]\n"
    )
    sys.stderr.flush()

    while not stop["flag"]:
        t0 = _time.monotonic()

        # Fresh ps + pane snapshots each tick. No caching — we want to
        # observe real kernel/tmux state, not anything ccm has
        # memoized.
        ps_lines = ccm_core.ps_snapshot().split("\n")
        panes_raw = ccm_core.tmux_cmd(
            "list-panes", "-a", "-F",
            "#{session_name}:#{window_index}\t#{pane_pid}\t#{pane_id}",
        )
        panes_cache = []
        for line in panes_raw.split("\n"):
            parts = line.split("\t")
            if len(parts) == 3:
                panes_cache.append(tuple(parts))
        own_pgid = str(os.getpgrp())

        prev_state = ccm_core.tmux_cmd(
            "show-option", "-wqv", "-t", win_target, "@ccm_prev_state",
        ) or ""

        # Invalidate the JSONL activity cache so each tick is a fresh
        # read — without this, trace would show stale ages across
        # successive ticks while the JSONL file actually changes.
        ccm_core._jsonl_activity_cache.clear()

        ctx = ccm_core.build_detection_context(
            win_target, proj_dir, prev_state,
            panes_cache, ps_lines, own_pgid,
        )
        rule, state = ccm_core.evaluate_rules(ctx)

        hook_str = f"{ctx.hook_state or '-'},{ctx.hook_age if ctx.hook_age >= 0 else '-'}"
        pid_age = ctx.claude_pid_age if ctx.claude_pid_age >= 0 else "-"
        jsonl_str = f"{ctx.jsonl_age if ctx.jsonl_age >= 0 else '-'},{ctx.jsonl_last_stop_reason or '-'}"
        action_short = "WRITE" if rule.action == ccm_core.Action.DEFAULT else "HOLD"
        # Phase annotation is metadata only (Step 1 of phase-machine
        # roadmap) — show it next to the rule name so "why fired?"
        # investigations include the intended session-lifecycle scope.
        rule_label = f"{rule.name}[{rule.phase or '-'}]"

        sys.stdout.write(
            f"{_time.strftime('%H:%M:%S')}  "
            f"raw={ctx.raw:5}  prev={prev_state or '-':5}  "
            f"hook={hook_str:10}  pid_age={str(pid_age):4}  "
            f"jsonl={jsonl_str:20}  "
            f"{rule_label:42} → {state:5} [{action_short}]\n"
        )
        sys.stdout.flush()

        # Sleep the remaining budget so the loop runs at a steady
        # cadence even when a scan takes varying time.
        elapsed = _time.monotonic() - t0
        remaining = interval - elapsed
        if remaining > 0:
            _time.sleep(remaining)
