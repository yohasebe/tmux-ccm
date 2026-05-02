"""Project-state persistence: save / load / list / delete snapshots.

A snapshot is a JSON manifest at
`$CCM_SNAPSHOT_DIR/<name>.json` recording every ccm-tagged tmux
window: `(name, dir, auto_start_claude)`. The `_autosave` snapshot
written by `cmd_snapshot_save("_autosave", quiet=True)` is the
restore point for `ccm start _autosave`; lifecycle commands
trigger it via `ccm_commands._autosave_trigger`.

Names are sanitized through `_sanitize_snapshot_name` to prevent
path traversal — the only legal forms are basename strings without
`..` components. The classic safety story for any user-supplied
filename hitting `os.path.join`.
"""

import glob
import json
import os
import time

import ccm_core  # late-bound for tmux_cmd / ccm_die / fzf_select / etc.
import ccm_render


def _sanitize_snapshot_name(name):
    """Sanitize a snapshot name to prevent path traversal. Strips
    path components and leading/trailing dots; aborts on empty
    result. The only legal forms are basename strings."""
    name = os.path.basename(name)
    name = name.strip(".")
    if not name:
        ccm_core.ccm_die(
            "Invalid snapshot name (alphanumerics / hyphens / underscores "
            "only; no path components)"
        )
    return name


def cmd_snapshot_save(name="", quiet=False):
    """Save the current set of ccm-tagged tmux windows to a snapshot
    JSON file. Empty `name` prompts interactively. The `_autosave`
    name is reserved for lifecycle-trigger autosaves and is what
    `ccm start _autosave` restores."""
    if not name:
        try:
            name = input("Snapshot name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not name:
        ccm_core.ccm_die("Snapshot name is required")
    name = _sanitize_snapshot_name(name)

    ccm_core.init_dirs()

    raw = ccm_core.tmux_cmd(
        "list-windows", "-a", "-F",
        "#{window_index}\t#{window_name}\t#{@ccm_project}\t#{@ccm_dir}",
    )
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
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, file_path)

    if not quiet:
        ccm_core.ccm_info(f"Snapshot saved: {name} ({file_path})")


def cmd_snapshot_load(name=""):
    """Restore a snapshot. Empty `name` opens an fzf picker over
    the existing snapshots."""
    # Deferred to avoid the circular `ccm_snapshot ↔ ccm_commands`
    # dep (cmd_snapshot_load creates project windows; cmd_add
    # triggers _autosave_trigger which calls back into snapshot
    # save).
    from ccm_commands import cmd_add

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

    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)

    snap_projects = data.get("projects", [])
    print(f"Loading snapshot: {name} ({len(snap_projects)} projects)")

    session = ccm_core.get_session()
    if not session:
        ccm_core.ccm_die("Not inside a tmux session — start one with "
                         "`tmux new-session` first")

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
            ccm_core.ccm_warn(
                f"Project window already exists, skipping: {proj_name}"
            )
            continue
        if not os.path.isdir(proj_dir):
            ccm_core.ccm_warn(
                f"Directory not found, skipping: {proj_name} ({proj_dir})"
            )
            continue

        # Don't auto-start Claude on restore — saves resources.
        cmd_add(proj_dir, proj_name, start_claude=False, _loading=True)

    # Save autosave after all projects loaded.
    try:
        cmd_snapshot_save("_autosave", quiet=True)
    except Exception:
        ccm_core.ccm_warn("Failed to save autosave snapshot after load")

    ccm_core.ccm_info(f"Snapshot loaded: {name}")


def cmd_snapshot_list():
    """Print a table of available snapshots: `NAME / CREATED /
    PROJECTS-COUNT`."""
    ccm_core.init_dirs()
    files = sorted(glob.glob(os.path.join(ccm_core.CCM_SNAPSHOT_DIR, "*.json")))
    if not files:
        print("No snapshots.")
        return

    print(f"{ccm_core._C_BOLD}{'NAME':<20} {'CREATED':<24} "
          f"{'PROJECTS'}{ccm_core._C_RESET}")
    print(f"{'----':<20} {'-------':<24} {'--------'}")

    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name", os.path.splitext(os.path.basename(fp))[0])
            created = data.get("created", "-")
            count = len(data.get("projects", []))
            print(
                f"{ccm_render.pad_to_width(name, 20)} "
                f"{ccm_render.pad_to_width(created, 24)} {count}"
            )
        except (json.JSONDecodeError, OSError):
            pass


def cmd_snapshot_delete(name=""):
    """Delete a snapshot. Empty `name` opens an fzf picker."""
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
