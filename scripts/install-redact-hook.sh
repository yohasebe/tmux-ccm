#!/usr/bin/env bash
# Install a local pre-commit hook that refuses to commit real project
# names.
#
# The names live on the machine, never in the repository — the hook
# reads them from the running tmux server at commit time. That is the
# part `tests/test_no_local_leakage.py` cannot do: a test carrying the
# list would publish it, and no structural rule separates a project
# name from a failure name — both are lowercase hyphenated tokens.
#
# Install once per clone:
#     scripts/install-redact-hook.sh
#
# The hook is written to .git/hooks/, which git never tracks or
# distributes. Bypass a single commit with `git commit --no-verify`.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
hook="$repo_root/.git/hooks/pre-commit"

if [ -e "$hook" ] && ! grep -q "ccm-redact-hook" "$hook" 2>/dev/null; then
    echo "error: $hook exists and is not the ccm redact hook." >&2
    echo "       Merge it by hand, or move it aside and re-run." >&2
    exit 1
fi

cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# ccm-redact-hook — keep local project names out of the repository.
#
# Names come from the tmux server this machine runs, so nothing about
# the developer's environment is written down here. Generic words that
# are also ordinary English are skipped: they produce noise, and noise
# is how a check gets disabled.
set -uo pipefail

command -v tmux >/dev/null 2>&1 || exit 0
tmux info >/dev/null 2>&1 || exit 0   # no server → nothing to compare against

# Names to skip, one per line, in a file git never tracks. Some project
# names are ordinary English ("review", "personal") and would fire on
# every other commit; listing them here keeps the check quiet without
# writing a single real name into the repository. Starts empty — add a
# name the first time a false positive annoys you.
ignore_file="$(git rev-parse --git-dir)/ccm-redact-ignore"

names=$(tmux list-windows -a -F '#{@ccm_project}' 2>/dev/null | sort -u)
[ -n "$names" ] || exit 0

staged=$(git diff --cached --name-only --diff-filter=ACM)
[ -n "$staged" ] || exit 0

found=""
while IFS= read -r name; do
    [ -n "$name" ] || continue
    if [ -f "$ignore_file" ] && grep -qxF "$name" "$ignore_file" 2>/dev/null; then
        continue
    fi
    # Word-boundary match: a short project name can be a substring of
    # an ordinary word, and a plain grep would fire on every one.
    #
    # URLs are stripped before matching. A path segment of somebody
    # else's site is not a local name, and leaving them in means the
    # check cries wolf on any commit that touches a link — which is
    # how a check ends up switched off, or its subject ends up in the
    # ignore file.
    hits=$(git diff --cached -U0 -- $staged \
           | grep -nE "^\+" \
           | grep -vE "^\+\+\+" \
           | sed -E 's#https?://[^ )"'"'"']*##g' \
           | grep -E "(^|[^A-Za-z0-9_-])${name}([^A-Za-z0-9_-]|$)" || true)
    if [ -n "$hits" ]; then
        found="${found}
  ${name}:
$(printf '%s\n' "$hits" | head -3 | sed 's/^/    /')"
    fi
done <<< "$names"

if [ -n "$found" ]; then
    cat >&2 <<MSG
commit refused: staged changes name a real project.

The repository carries deliverables, tests, and documentation.
Describe the behaviour, not the session it turned up in: "hooks fell
silent partway through a turn" tells a reader everything they need,
and names nothing.
$found

Bypass with: git commit --no-verify
MSG
    exit 1
fi
exit 0
HOOK

chmod +x "$hook"

ignore_file="$repo_root/.git/ccm-redact-ignore"
if [ ! -e "$ignore_file" ]; then
    cat > "$ignore_file" <<'IGNORE'
# Project names the pre-commit redact hook should not flag, one per line.
# Some project names are ordinary English words and would fire constantly.
# This file lives under .git/, so nothing here is ever published.
IGNORE
    echo "created:   $ignore_file (empty)"
fi

echo "installed: $hook"
echo "reads project names from tmux at commit time; nothing is stored in the repo."
