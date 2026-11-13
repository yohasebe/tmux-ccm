"""Snapshot and restore exceptions.

Kept apart from ccm_snapshot_store so they exist before any import cycle
through ccm_core reaches a module that subclasses them at import time.
"""


class SnapshotError(RuntimeError):
    pass


class EmptySnapshot(SnapshotError):
    pass


class TmuxFailure(SnapshotError):
    """A tmux command or listing failed. A restore asks a fresh listing
    whether the window it was working on is gone before blaming it alone."""


class WindowProblem(SnapshotError):
    """A problem confined to one project's window. A restore records it and
    goes on with the others; any other SnapshotError stops the restore."""
