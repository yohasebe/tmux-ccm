#!/usr/bin/env python3
"""Tracked-path allow list: every file git tracks must match a line of
tracked_paths.allow, and every line there must match a tracked file.

Other checks each forbid one known shape. This one works the other way
round: anything that is not an expected kind of file in an expected place
fails, including kinds nobody thought to forbid (.env, logs, databases,
notes). It matters because TPM installs ccm by cloning the repository, so
every tracked file is shipped.

Usage:
    python3 scripts/check_tracked_paths.py             # the index (next commit)
    python3 scripts/check_tracked_paths.py --tree REV  # a commit (e.g. before a push)

The allow list is read from the same place as the files: the index, or the
commit given to --tree. An unstaged edit to the list changes nothing until
it is staged.

Lines are globs: `*` and `?` stay within one directory (and match
dotfiles), `**/` spans any depth, and `{a,b}` is expanded before matching.
Each alternative of a brace must match a tracked file, so `{py,sh}` cannot
keep an unused `sh` alive.

Output is paths only. Exit status: 0 clean; 1 unallowed paths, unused
alternatives, or anything that could not be read (nothing seen is not
clean); 2 the tree has no allow list (pre-push decides whether that
commit is already public).
"""
import os
import re
import subprocess
import sys

ALLOW_PATH = 'scripts/tracked_paths.allow'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = '[lint:tracked_paths]'


def git_read(*args):
    try:
        result = subprocess.run(['git', '-C', ROOT, *args], capture_output=True)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def expand_braces(pattern):
    """"a/{b,c}/*.{x,y}" -> four patterns. Braces do not nest."""
    start = pattern.find('{')
    if start < 0:
        return [pattern]
    end = pattern.find('}', start)
    if end < 0:
        print(f'{TAG} unbalanced brace: {pattern}')
        sys.exit(1)
    head, tail = pattern[:start], pattern[end + 1:]
    return [alternative
            for choice in pattern[start + 1:end].split(',')
            for alternative in expand_braces(head + choice + tail)]


def to_regex(glob):
    out, i = [], 0
    while i < len(glob):
        if glob.startswith('**/', i):
            out.append('(?:[^/]*/)*')
            i += 3
        elif glob[i] == '*':
            out.append('[^/]*')
            i += 1
        elif glob[i] == '?':
            out.append('[^/]')
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile(''.join(out) + r'\Z')


def main(argv):
    tree = None
    if '--tree' in argv:
        at = argv.index('--tree')
        tree = argv[at + 1] if at + 1 < len(argv) else None
        if not tree or tree.startswith('-'):
            print(f'{TAG} --tree needs a revision; refusing to pass.')
            return 1
    source = tree or 'the index'
    listing = (git_read('ls-tree', '-r', '-z', '--name-only', tree) if tree
               else git_read('ls-files', '-z'))
    files = [p for p in listing.decode('utf-8', 'surrogateescape').split('\0') if p] if listing else []
    if not files:
        print(f'{TAG} could not list tracked files in {source}; refusing to pass.')
        return 1
    allow = git_read('show', f'{tree or ""}:{ALLOW_PATH}')
    if allow is None:
        print(f'{TAG} {source} has no {ALLOW_PATH}; refusing to pass.')
        return 2

    lines = [line.strip() for line in allow.decode('utf-8').splitlines()]
    lines = [line for line in lines if line and not line.startswith('#')]
    alternatives = [(line, alt, to_regex(alt)) for line in lines for alt in expand_braces(line)]
    used = [False] * len(alternatives)
    unallowed = []
    for path in files:
        hits = [i for i, (_, _, rx) in enumerate(alternatives) if rx.match(path)]
        for i in hits:
            used[i] = True
        if not hits:
            unallowed.append(path)
    unused = [(line, alt) for (line, alt, _), hit in zip(alternatives, used) if not hit]

    print(f'{TAG} checked {len(files)} tracked file(s) in {source} against {len(lines)} line(s)')
    if not unallowed and not unused:
        print(f'{TAG} OK - every tracked file is allowed and every alternative is in use.')
        return 0
    if unallowed:
        print(f'{TAG} {len(unallowed)} tracked file(s) not in {ALLOW_PATH}:')
        for path in unallowed:
            print(f'  {path}')
    if unused:
        print(f'{TAG} {len(unused)} alternative(s) match no tracked file (remove or narrow them):')
        for line, alt in unused:
            print(f'  {line}' if line == alt else f'  {alt}  (from {line})')
    print()
    print('If a new file belongs in the repository, add the narrowest line that')
    print('describes its place and kind, and stage the list. If it does not,')
    print('untrack it (git rm --cached).')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
