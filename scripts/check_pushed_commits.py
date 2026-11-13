#!/usr/bin/env python3
"""Check every commit a push publishes against its own allow list.

Run from a pre-push hook with the hook's stdin ("<local ref> <local sha>
<remote ref> <remote sha>" lines). Each commit in each pushed range is
checked with `check_tracked_paths.py --tree`, not just the tip: a file
added in one commit and removed in the next is gone from the tip but still
published in the history.

Fails when the ranges cannot be determined (nothing read is not clean),
including a pushed ref that is not a commit.
A commit without an allow list passes only when a remote branch already
contains it, i.e. it was published before the list existed.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, 'check_tracked_paths.py')
ZERO = '0' * 40
TAG = '[lint:pushed_commits]'


def git(*args):
    result = subprocess.run(['git', *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def main(stdin):
    pairs = [line.split() for line in stdin.splitlines() if line.strip()]
    if not pairs or any(len(p) != 4 for p in pairs):
        print(f'{TAG} no ref pairs on stdin; refusing to pass.')
        return 1
    failed = False
    for _local_ref, local, _remote_ref, remote in pairs:
        if local == ZERO:
            continue  # branch deletion publishes nothing
        # A tag may point at a tree or blob: rev-list would list no
        # commits and nothing would be checked.
        if git('rev-parse', '--verify', '--quiet', f'{local}^{{commit}}') is None:
            print(f'{TAG} {local} is not a commit; refusing to pass.')
            return 1
        spec = [local, '--not', '--remotes'] if remote == ZERO else [f'{remote}..{local}']
        listing = git('rev-list', '--reverse', *spec)
        if listing is None:
            print(f'{TAG} cannot list the commits of {local}; refusing to pass.')
            return 1
        for commit in listing.split():
            run = subprocess.run([sys.executable, CHECKER, '--tree', commit],
                                 capture_output=True, text=True)
            if run.returncode == 0:
                continue
            if run.returncode == 2 and git('branch', '-r', '--contains', commit):
                continue  # published before the allow list existed
            print(f'{TAG} commit {commit[:12]}:')
            sys.stdout.write(run.stdout)
            failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.stdin.read()))
