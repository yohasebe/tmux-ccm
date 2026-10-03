"""The tracked-path allow list checker, run as it is shipped: a copy of the
script inside a throwaway repository, so it judges that repository."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('check_tracked_paths', SCRIPTS / 'check_tracked_paths.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.mark.parametrize('glob,path,matches', [
    ('lib/*.py', 'lib/a.py', True),
    ('lib/*.py', 'lib/sub/a.py', False),
    ('lib/*.py', 'lib/.hidden.py', True),
    ('*', 'notes/plan.md', False),
    ('docs/**/*.md', 'docs/a.md', True),
    ('docs/**/*.md', 'docs/x/y/a.md', True),
    ('docs/**/*.md', 'docsx/a.md', False),
    ('a?.txt', 'ab.txt', True),
    ('a?.txt', 'a/.txt', False),
    ('README.md', 'README_md', False),
])
def test_glob_semantics(glob, path, matches):
    assert bool(checker.to_regex(glob).match(path)) is matches


def test_braces_expand_to_every_alternative():
    assert checker.expand_braces('a/{b,c}/*.{x,y}') == ['a/b/*.x', 'a/b/*.y', 'a/c/*.x', 'a/c/*.y']
    assert checker.expand_braces('compose{,.dev}') == ['compose', 'compose.dev']


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.com',
                           *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'scripts').mkdir()
    shutil.copy(SCRIPTS / 'check_tracked_paths.py', tmp_path / 'scripts')
    (tmp_path / 'lib').mkdir()
    (tmp_path / 'lib' / 'a.py').write_text('x\n')
    (tmp_path / 'scripts' / 'tracked_paths.allow').write_text(
        '# comment\nscripts/check_tracked_paths.py\nscripts/tracked_paths.allow\nlib/*.py\n')
    git(tmp_path, 'add', '.')
    return tmp_path


def check(repo, *args):
    run = subprocess.run([sys.executable, str(repo / 'scripts' / 'check_tracked_paths.py'), *args],
                         capture_output=True, text=True)
    return run.returncode, run.stdout


def test_the_index_passes_when_every_file_and_line_is_used(repo):
    assert check(repo)[0] == 0


@pytest.mark.parametrize('name', ['notes/plan.md', 'lib/PLAN.md', 'debug.log', '.env', 'data.sqlite3', 'dump.json'])
def test_an_unlisted_path_fails_and_only_its_path_is_printed(repo, name):
    path = repo / name
    path.parent.mkdir(exist_ok=True)
    path.write_text('file body\n')
    git(repo, 'add', '-f', name)
    code, out = check(repo)
    assert code == 1 and f'  {name}' in out and 'file body' not in out


def test_an_unused_alternative_fails(repo):
    allow = repo / 'scripts' / 'tracked_paths.allow'
    allow.write_text(allow.read_text().replace('lib/*.py', 'lib/*.{py,rb}'))
    git(repo, 'add', '.')
    code, out = check(repo)
    assert code == 1 and 'lib/*.rb  (from lib/*.{py,rb})' in out


def test_an_unstaged_allow_line_changes_nothing_until_staged(repo):
    (repo / 'notes').mkdir()
    (repo / 'notes' / 'plan.md').write_text('x\n')
    git(repo, 'add', 'notes/plan.md')
    allow = repo / 'scripts' / 'tracked_paths.allow'
    allow.write_text(allow.read_text() + 'notes/*.md\n')
    assert check(repo)[0] == 1
    git(repo, 'add', str(allow))
    assert check(repo)[0] == 0


def test_tree_mode_judges_a_commit_by_its_own_list(repo):
    git(repo, 'commit', '-q', '-m', 'one')
    (repo / 'debug.log').write_text('x\n')
    git(repo, 'add', '-f', 'debug.log')
    assert check(repo, '--tree', 'HEAD')[0] == 0
    assert check(repo)[0] == 1


@pytest.mark.parametrize('args', [['--tree'], ['--tree', '--x']])
def test_tree_without_a_revision_refuses(repo, args):
    assert check(repo, *args)[0] == 1


def test_missing_list_or_files_refuse(repo, tmp_path_factory):
    git(repo, 'rm', '-q', '--cached', 'scripts/tracked_paths.allow')
    assert check(repo)[0] == 2
    empty = tmp_path_factory.mktemp('empty')
    git(empty, 'init', '-q')
    (empty / 'scripts').mkdir()
    shutil.copy(SCRIPTS / 'check_tracked_paths.py', empty / 'scripts')
    assert check(empty)[0] == 1



ZERO = '0' * 40


def pushed(repo, stdin):
    shutil.copy(SCRIPTS / 'check_pushed_commits.py', repo / 'scripts')
    run = subprocess.run([sys.executable, str(repo / 'scripts' / 'check_pushed_commits.py')],
                         input=stdin, capture_output=True, text=True, cwd=repo)
    return run.returncode, run.stdout


@pytest.fixture
def published(repo, tmp_path_factory):
    """A repository whose first commit, with the allow list, is on a remote."""
    git(repo, 'commit', '-q', '-m', 'base')
    remote = tmp_path_factory.mktemp('remote')
    git(remote, 'init', '-q', '--bare')
    git(repo, 'remote', 'add', 'pub', str(remote))
    git(repo, 'push', '-q', '--no-verify', 'pub', 'HEAD:main')
    git(repo, 'fetch', '-q', 'pub')
    return repo, git(repo, 'rev-parse', 'HEAD').strip()


def commit_file(repo, name, message, remove=False):
    if remove:
        git(repo, 'rm', '-q', name)
    else:
        (repo / name).write_text('x\n')
        git(repo, 'add', '-f', name)
    git(repo, 'commit', '-q', '--no-verify', '-m', message)
    return git(repo, 'rev-parse', 'HEAD').strip()


def test_a_path_only_in_a_middle_commit_stops_the_push(published):
    repo, base = published
    middle = commit_file(repo, 'debug.log', 'add')
    tip = commit_file(repo, 'debug.log', 'remove', remove=True)
    assert check(repo, '--tree', tip)[0] == 0
    code, out = pushed(repo, f'refs/heads/main {tip} refs/heads/main {base}\n')
    assert code == 1 and middle[:12] in out and 'debug.log' in out


def test_a_clean_range_and_a_deletion_pass(published):
    repo, base = published
    tip = commit_file(repo, 'lib/b.py', 'add')
    assert pushed(repo, f'refs/heads/main {tip} refs/heads/main {base}\n')[0] == 0
    assert pushed(repo, f'(delete) {ZERO} refs/heads/old {base}\n')[0] == 0


@pytest.mark.parametrize('stdin', ['', 'garbage\n', f'refs/heads/main {"1" * 40} refs/heads/main {"2" * 40}\n'])
def test_an_undeterminable_range_refuses(published, stdin):
    repo, _ = published
    assert pushed(repo, stdin)[0] == 1


def test_a_commit_without_a_list_passes_only_when_already_published(published):
    repo, base = published
    git(repo, 'checkout', '-q', '--orphan', 'bare')
    git(repo, 'rm', '-q', '-r', '--cached', '.')
    unpublished = commit_file(repo, 'notes.txt', 'no list')
    code, out = pushed(repo, f'refs/heads/bare {unpublished} refs/heads/bare {ZERO}\n')
    assert code == 1 and 'has no scripts/tracked_paths.allow' in out
    git(repo, 'push', '-q', '--no-verify', 'pub', 'bare')
    git(repo, 'fetch', '-q', 'pub')
    assert pushed(repo, f'refs/heads/other {unpublished} refs/heads/other {base}\n')[0] == 0


@pytest.mark.parametrize('kind', ['tree', 'blob', 'annotated-tree'])
def test_a_tag_that_is_not_a_commit_refuses(published, kind):
    repo, _ = published
    bad = commit_file(repo, 'debug.log', 'add')
    target = git(repo, 'rev-parse', f'{bad}^{{tree}}' if kind != 'blob' else f'{bad}:debug.log').strip()
    if kind == 'annotated-tree':
        git(repo, 'tag', '-a', '-m', 't', 'check', target)
        target = git(repo, 'rev-parse', 'check').strip()
    code, out = pushed(repo, f'refs/tags/check {target} refs/tags/check {ZERO}\n')
    assert code == 1 and 'is not a commit' in out


def test_an_annotated_tag_on_a_commit_is_checked(published):
    repo, _ = published
    commit_file(repo, 'debug.log', 'add')
    git(repo, 'tag', '-a', '-m', 't', 'release')
    tag = git(repo, 'rev-parse', 'release').strip()
    code, out = pushed(repo, f'refs/tags/release {tag} refs/tags/release {ZERO}\n')
    assert code == 1 and 'debug.log' in out
