"""Which hook entries in Claude Code's settings are ccm's, and the two
edits ccm makes to them.

One definition of ownership, used everywhere that reads or rewrites
ccm's hooks: `ccm setup-hooks`, `ccm remove-hooks` (both through
lib/common.sh) and `ccm doctor`. They used to answer the question
separately — the removal by matching a script name anywhere in a
command and dropping the whole matcher entry that held it — and the
answers disagreed, which let a repair meant to touch only ccm's hooks
take another tool's with it.

A hook is ccm's when its command is a bare path (nothing but the path)
to one of ccm's hook scripts in this ccm's hooks directory. The script
name must match exactly, case included, as ccm writes it. The directory
is compared by identity on the filesystem, not by spelling, so a path
written through a symlink, with `.` or `..`, or — on a filesystem that
ignores case — in another case still counts; a path that cannot be
looked up does not.

Nothing else is ccm's to edit — not even hooks that look like those of
a ccm installed somewhere else, or of one since deleted. From the
settings alone such a directory cannot be told apart from another tool
that names its scripts the same way, and guessing wrong would delete
that tool's hooks. Every hook named like a ccm script outside this
directory is reported back as a look-alike, so the caller can name it
for removal by hand, but it is never edited.

Edits work on individual hooks: a matcher entry that also holds another
tool's hook keeps that hook and its matcher; an entry is dropped only
when removing ccm's hooks empties it, and an event only when its list
is emptied that way.

CLI (settings JSON on stdin):
    ccm_hook_owner.py strip <hooks_dir>            -> settings on stdout
    ccm_hook_owner.py sync <hooks_dir> <timeout>   -> settings on stdout
    ccm_hook_owner.py lookalikes <hooks_dir>       -> one command per line
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ccm_constants import HOOK_SCRIPTS  # noqa: E402

def _hooks(settings):
    """Every hook dict with a string command, wherever the settings keep
    one; tolerant of any shape it does not expect."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks")
            if not isinstance(inner, list):
                continue
            for hook in inner:
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    yield hook


#: What a command must look like to be read as a path at all: absolute,
#: and free of anything a shell would split, expand or interpret. ccm
#: writes its hook commands as a bare absolute path, which Claude Code
#: runs through a shell, so a path that needed quoting would not have
#: worked as a hook anyway. Anything else — `bash /x/on-stop.sh`, a
#: quoted path, arguments, a variable — is not read as a path and is
#: never ccm's, however its last component is spelled.
_BARE_PATH = re.compile(r"""/[^\s'"`$;&|<>()\\*?\[\]{}!#]+""")


def _split(command):
    """(directory, script) when `command` is a bare absolute path to a
    file named like a ccm hook script, else None."""
    if not isinstance(command, str) or not _BARE_PATH.fullmatch(command):
        return None
    directory, script = command.rsplit("/", 1)
    if not directory or script not in HOOK_SCRIPTS:
        return None
    return directory, script


def classify(settings, hooks_dir):
    """(owned, lookalikes): the sorted command strings that are ccm's,
    and those named like a ccm script that are not."""
    try:
        own_exists = os.path.isdir(hooks_dir)
    except (OSError, ValueError):
        own_exists = False
    owned, lookalikes = [], []
    for command in sorted({h["command"] for h in _hooks(settings)}):
        parts = _split(command)
        if not parts:
            continue
        try:
            mine = own_exists and os.path.samefile(parts[0], hooks_dir)
        except (OSError, ValueError):
            mine = False
        (owned if mine else lookalikes).append(command)
    return owned, lookalikes


def strip(settings, owned):
    """`settings` with ccm's hooks removed, hook by hook."""
    owned = set(owned)
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        return settings
    hooks = settings["hooks"]
    had_events = bool(hooks)

    def is_owned(hook):
        return (isinstance(hook, dict) and isinstance(hook.get("command"), str)
                and hook["command"] in owned)

    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept_entries = []
        for entry in entries:
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if isinstance(inner, list) and any(is_owned(h) for h in inner):
                remaining = [h for h in inner if not is_owned(h)]
                if remaining:
                    kept_entries.append({**entry, "hooks": remaining})
            else:
                kept_entries.append(entry)
        if entries and not kept_entries:
            del hooks[event]
        else:
            hooks[event] = kept_entries
    if had_events and not hooks:
        del settings["hooks"]
    return settings


def sync_timeouts(settings, owned, timeout):
    """`settings` with ccm's hooks carrying `timeout`; nothing else changes."""
    owned = set(owned)
    for hook in _hooks(settings):
        if hook["command"] in owned:
            hook["timeout"] = timeout
    return settings


def _main(argv):
    if len(argv) < 3 or argv[1] not in ("strip", "sync", "lookalikes"):
        print(__doc__, file=sys.stderr)
        return 2
    action, hooks_dir = argv[1], argv[2]
    try:
        settings = json.load(sys.stdin)
    except ValueError as exc:
        print(f"settings are not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        # `[]`, `123`, `null`: valid JSON, not settings. Passing them
        # through would let a caller back the file up over a good copy
        # and write it back as if it had been edited — and jq builds an
        # object out of `null` without complaint.
        print(f"settings are not a JSON object (found "
              f"{type(settings).__name__})", file=sys.stderr)
        return 1
    owned, lookalikes = classify(settings, hooks_dir)
    if action == "lookalikes":
        for command in lookalikes:
            print(command)
        return 0
    if action == "strip":
        result = strip(settings, owned)
    else:
        if len(argv) < 4:
            print("sync needs a timeout", file=sys.stderr)
            return 2
        try:
            timeout = json.loads(argv[3])
        except ValueError:
            timeout = None
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            print(f"timeout must be a number of seconds, got {argv[3]!r}", file=sys.stderr)
            return 2
        result = sync_timeouts(settings, owned, timeout)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
