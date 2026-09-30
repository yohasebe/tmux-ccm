"""Project-state persistence: save / load / list / delete snapshots.

A snapshot is a JSON manifest at
`$CCM_SNAPSHOT_DIR/<name>.json` recording every ccm-tagged tmux
window and its observed pane layout (v2). The `_autosave` snapshot
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

import ccm_core  # late-bound for tmux_cmd / ccm_die / fzf_select / etc.
import ccm_render
import ccm_snapshot_store as store
import ccm_restore


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
    """Capture under the same lock used by prepare, cancel and recovery."""
    if not name:
        try:
            name = input("Snapshot name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    name = _sanitize_snapshot_name(name)
    try:
        with store.locked():
            if ccm_restore.paused():
                if not quiet:
                    ccm_core.ccm_warn("Restore incomplete; retry the same snapshot before saving")
                return False
            old = store.read(store.directory() / f"{name}.json")
            if name == "_autosave" and store.sealed(old):
                if not quiet:
                    ccm_core.ccm_info("Snapshot protected: _autosave; use ccm prepare-logout --cancel to unseal")
                return False
            data = store.collect(name)
            changed = store.write(name, data)
    except store.EmptySnapshot as exc:
        if quiet:
            return False
        ccm_core.ccm_die(str(exc))
    except (store.SnapshotError, OSError, ValueError) as exc:
        if quiet:
            raise
        ccm_core.ccm_die(str(exc))
    if not quiet:
        ccm_core.ccm_info(f"Snapshot {'saved' if changed else 'unchanged'}: {name}")
    return changed


def _confirmation():
    """Read a short terminal answer; Escape cancels without requiring Enter."""
    import sys
    import termios
    import tty
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    answer = bytearray()
    try:
        tty.setraw(fd)
        print("Save anyway? [y/N] ", end="", flush=True)
        while True:
            char = os.read(fd, 1)
            if char in (b'', b'\x1b', b'\x03', b'\x04'):
                return ''
            if char in (b'\r', b'\n'):
                return answer.decode('ascii', errors='replace')
            if char in (b'\x7f', b'\x08'):
                if answer:
                    answer.pop()
                    print('\b \b', end='', flush=True)
            elif len(answer) < 32:
                answer.extend(char)
                print(char.decode('ascii', errors='replace'), end='', flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, previous)
        print()


def cmd_prepare_logout(args):
    """Save and seal a checkpoint, or explicitly release its protection."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="ccm prepare-logout")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cancel", action="store_true", help="Unseal without deleting the checkpoint")
    group.add_argument("-y", "--yes", action="store_true", help="Save even when projects are PERMIT or BUSY")
    opts = parser.parse_args(args)
    try:
        with store.locked():
            if ccm_restore.paused():
                raise store.SnapshotError("Restore incomplete; retry the same snapshot before preparing or unsealing")
            path = store.directory() / "_autosave.json"
            if opts.cancel:
                data = store.read(path)
                if not data:
                    raise store.SnapshotError("No _autosave checkpoint to unseal")
                if store.sealed(data):
                    data['checkpoint']['sealed'] = False
                    store.write("_autosave", data, backup=False)
                ccm_core.ccm_info("Snapshot protection released: _autosave (checkpoint retained)")
                return
            data = store.collect("_autosave", sealed=True)
            interrupted = data['checkpoint']['interrupted']
            if interrupted:
                for project in interrupted:
                    print(f"{project['name']}: {project['state']}")
                if not opts.yes:
                    if not sys.stdin.isatty():
                        raise store.SnapshotError("PERMIT/BUSY projects: non-interactive save refused; use -y to save anyway")
                    try:
                        answer = _confirmation().strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = ''
                    if answer != 'y':
                        raise store.SnapshotError("Save cancelled; checkpoint unchanged")
                    fresh = store.collect("_autosave", sealed=True)
                    if store.comparable(fresh) != store.comparable(data):
                        raise store.SnapshotError("Projects changed during confirmation; checkpoint unchanged, retry")
                    data = fresh
            store.write("_autosave", data)
        ccm_core.ccm_info("Checkpoint saved and protected: _autosave (windows and agents are still running)")
    except (store.SnapshotError, OSError, ValueError) as exc:
        ccm_core.ccm_die(str(exc))


def snapshot_diagnostics():
    """Read only: doctor must not recover, create files or acquire a write lock."""
    rows = []
    try:
        if ccm_restore.paused():
            rows.append((True, 'restore incomplete; retry the same snapshot load to resume autosave'))
        if (store.directory() / '.snapshot-transaction').exists():
            rows.append((True, "snapshot transaction pending; next snapshot write will recover it"))
        data = store.read(store.directory() / '_autosave.json')
        if data:
            meta = data['checkpoint'] if data['version'] == 2 else {}
            rows.append((False, f"_autosave v{data['version']}: "
                         f"{'sealed' if store.sealed(data) else 'unsealed'}, {data.get('created', '?')}"))
            for p in meta.get('interrupted', []):
                rows.append((False, f"interrupted at save: {p['name']} ({p['state']})"))
        if store.read(store.directory() / '_autosave.prev'):
            rows.append((False, "previous checkpoint available: _autosave.prev"))
    except (store.SnapshotError, OSError, ValueError) as exc:
        rows.append((True, str(exc)))
    return rows


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
    # Open directly rather than exists-then-open: the file can vanish
    # between the two calls (concurrent delete, a sync client), and a
    # malformed snapshot (truncated write, hand-edit) must die with a
    # message instead of a traceback.
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        ccm_core.ccm_die(f"Snapshot not found: {name}")
    except (json.JSONDecodeError, OSError) as e:
        ccm_core.ccm_die(f"Snapshot unreadable: {name} ({e})")

    # json.load accepts a top-level array / scalar / null without
    # raising, so a hand-edited or truncated snapshot can parse to a
    # non-dict. Guard before `.get` or it crashes with AttributeError
    # instead of the readable die this function promises.
    if not isinstance(data, dict):
        ccm_core.ccm_die(f"Snapshot malformed: {name} (top level is not an object)")

    try:
        store.validate(data)
    except store.SnapshotError as exc:
        ccm_core.ccm_die(str(exc))
    if data['version'] == 2:
        return ccm_restore.load(name)
    if ccm_restore.paused():
        ccm_core.ccm_die('Restore incomplete; finish the same v2 snapshot before loading another snapshot')

    snap_projects = data.get("projects", [])
    if not isinstance(snap_projects, list):
        ccm_core.ccm_die(f"Snapshot malformed: {name} (projects is not a list)")
    print(f"Loading snapshot: {name} ({len(snap_projects)} projects)")

    session = ccm_core.require_session()

    for proj in snap_projects:
        if not isinstance(proj, dict):
            ccm_core.ccm_warn(f"Skipping malformed snapshot entry: {proj!r}")
            continue
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
    try:
        with store.locked():
            if ccm_restore.paused():
                raise store.SnapshotError("Restore incomplete; retry the same snapshot before deleting")
            if name == "_autosave" and store.sealed(store.read(store.directory() / f"{name}.json")):
                raise store.SnapshotError("Snapshot protected; run ccm prepare-logout --cancel before deleting")
            os.unlink(file_path)
    except (OSError, store.SnapshotError) as exc:
        ccm_core.ccm_die(str(exc))
    ccm_core.ccm_info(f"Snapshot deleted: {name}")
