"""Read Claude settings without waiting for a FIFO writer.

No ccm imports: both core diagnostics and settings canaries use this
reader without introducing a circular dependency.
"""

import json
import os
import stat


def read_settings(path):
    """Return a settings object, or None if absent or unreadable.

    Open nonblocking, then check the opened descriptor rather than the
    path: a settings file can be replaced between stat and open. Follow
    symlinks to regular files, as with ordinary settings reads, but never
    read a pipe or device. Invalid JSON, encoding, and non-object roots
    are unknown settings, not evidence that hooks are enabled.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            with os.fdopen(fd, encoding="utf-8") as stream:
                fd = None  # stream owns the descriptor from here
                data = json.load(stream)
        finally:
            if fd is not None:
                os.close(fd)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
