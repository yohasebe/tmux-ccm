"""Machine-readable output for agents (`--json`).

One JSON object on stdout per run, success or failure, so a caller
never has to read prose. Within a schema version, fields and enum
values are only ever added; a reader ignores fields it does not know,
treats an enum value it does not know as unknown, and never acts or
retries on one.

`ccm status --json [<name>|self]` lives here; the text `ccm status` is
`ccm_render.print_status`, built from the same project records.
"""
from __future__ import annotations

import json
import os
import sys
import time

import ccm_core
import ccm_render
import ccm_spool
from ccm_constants import CCM_VERSION

SCHEMA = 1

STATUS_USAGE = """\
Usage: ccm status [--json [<name> | self]]
  --json         One JSON object for agents, every registered window
  --json <name>  Only the window registered as <name>
  --json self    Only the window this command runs in"""


def emit(payload, code=0):
    """Print one JSON object and exit with `code`."""
    sys.stdout.write(json.dumps({"schema": SCHEMA, **payload},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def error(code, message):
    emit({"error": {"code": code, "message": message}}, 1)


def cmd_status(args):
    """`ccm status` with its arguments: text without `--json`."""
    if "--json" not in args:
        if args in (["-h"], ["--help"]):
            print(STATUS_USAGE)
            return
        if args:
            ccm_core.ccm_die(STATUS_USAGE)
        ccm_render.print_status()
        return
    rest = list(args)
    rest.remove("--json")
    if any(a in ("-h", "--help") for a in rest):
        emit({"help": STATUS_USAGE})
    if any(a.startswith("-") for a in rest) or len(rest) > 1:
        error("usage", STATUS_USAGE)
    # One listing serves the selection and the records: a second one
    # could fail on its own and read as no windows.
    windows = ccm_core.tmux_query("list-windows", "-a", "-F",
                                  ccm_core._WINDOW_FORMAT)
    if windows is None:
        error("tmux_unavailable", "Could not list the tmux windows.")
    if not rest:
        projects = ccm_core.build_project_list(by_window=True, windows=windows)
    else:
        window_id = (_self_window() if rest[0] == "self"
                     else _named_window(rest[0], _registered(windows)))
        projects = ccm_core.build_project_list(window_id=window_id,
                                               windows=windows)
        if not projects:
            error("no_match", f"The window {window_id} is not registered.")
    emit(status_payload(projects))


def _registered(windows):
    """{window id: name} of the registered windows in a listing."""
    rows = {}
    for line in windows.split("\n"):
        row = ccm_core._parse_window_line(line)
        if row and row["window_id"]:
            rows[row["window_id"]] = row["project"]
    return rows


def _named_window(name, rows):
    matches = sorted(wid for wid, project in rows.items() if project == name)
    if not matches:
        error("no_match", f"No registered window is named {name}.")
    if len(matches) > 1:
        error("ambiguous", f"{len(matches)} windows are named {name} "
              f"({', '.join(matches)}); none is chosen.")
    return matches[0]


def _self_window():
    """The window this command runs in: the verified caller pane's, or
    the one registered window whose directory holds the cwd."""
    _, pane = ccm_core.caller_context(resolve_project=False)
    if pane:
        window_id = ccm_core.tmux_query("display-message", "-p", "-t", pane,
                                        "#{window_id}")
        if window_id:
            return window_id
    else:
        try:
            match = ccm_core.unique_registered_window(os.getcwd())
        except OSError:
            match = None
        if match:
            return match[0]
    error("self_unresolved", "Could not tell which registered window this "
          "command runs in.")


def status_payload(projects):
    """The `ccm status --json` object for these projects."""
    summary = ccm_spool.spool_summary()
    return {
        "ccm_version": CCM_VERSION,
        "generated_at": int(time.time()),
        "hooks": {"configured": ccm_core.hooks_configured()},
        "warnings": ccm_render.status_warnings(projects),
        "spool": {"pending": summary["pending"], "held": summary["held"],
                  "expired": summary["expired"]},
        "projects": [_project(p) for p in projects],
    }


def _project(p):
    return {
        "window_id": p.window_id or None,
        "name": p.name,
        "dir": ccm_core._canonical_directory(p.dir),
        "dir_raw": p.dir,
        "state": p.state,
        "bg": p.bg_active,
        "permission_mode": p.permission_mode or None,
        "completed_at": p.completed_raw,
        "branch": p.branch or None,
        "sidekicks": [
            {"agent": o.agent, "pane": o.pane, "state": o.state,
             "state_source": o.state_source, "keys": o.keys}
            for o in p.sidekicks],
    }
