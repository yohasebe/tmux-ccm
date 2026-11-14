"""Claude Code's configuration home, found the way Claude Code finds it.

Claude Code keeps its user settings (hooks included) and the user
`CLAUDE.md` in `$CLAUDE_CONFIG_DIR` when that variable is set, and in
`~/.claude` otherwise. The variable is taken from the environment of
whoever asks: ccm's commands follow the shell they are run from.

A value that is empty or not an absolute path is refused rather than
resolved: Claude Code would read it relative to the directory it was
started in, which ccm cannot know.

CLI:
    ccm_claude_home.py   -> the home on stdout; exit 2 with the reason on
                            stderr when CLAUDE_CONFIG_DIR is unusable
"""
import os
import sys

VARIABLE = "CLAUDE_CONFIG_DIR"


def claude_home(environ=None):
    """The configuration home. Raises ValueError for an unusable
    CLAUDE_CONFIG_DIR."""
    environ = os.environ if environ is None else environ
    value = environ.get(VARIABLE)
    if value is None:
        return os.path.join(os.path.expanduser("~"), ".claude")
    if not os.path.isabs(value):
        raise ValueError(
            f"{VARIABLE} is set but is not an absolute path ({value!r}); "
            "set it to an absolute path, or unset it to use ~/.claude.")
    return value


def problem(environ=None):
    """Why CLAUDE_CONFIG_DIR cannot be used, or None."""
    try:
        claude_home(environ)
    except ValueError as e:
        return str(e)
    return None


def readable_home(environ=None):
    """The home to read from: `claude_home`, or `~/.claude` when the
    variable is unusable (`problem` says why, for `ccm doctor`)."""
    try:
        return claude_home(environ)
    except ValueError:
        return claude_home({})


def user_settings_file(environ=None):
    return os.path.join(readable_home(environ), "settings.json")


def user_claude_md(environ=None):
    return os.path.join(readable_home(environ), "CLAUDE.md")


if __name__ == "__main__":
    try:
        print(claude_home())
    except ValueError as e:
        print(f"ccm: {e}", file=sys.stderr)
        sys.exit(2)
