#!/usr/bin/env bats
# Regression guard for executable bits in the GIT INDEX.
#
# ccm is distributed via git/TPM, so what users receive is decided by
# `git ls-files -s` modes — NOT by local filesystem permissions. This
# matters because the development checkout lives under Dropbox, which
# can normalize local permissions to 0600: a newly added hook/script
# whose +x was forgotten before `git add` gets recorded as 100644 and
# silently ships broken (the local checkout keeps working, so nothing
# surfaces until a TPM user hits it). A 644 `ccm.tmux` would disable
# the entire plugin at tmux startup; a 644 hook script means Claude
# Code cannot exec it and the hook never fires.
#
# Same incident class as the 2026-07 gem releases (a project#20 et
# al.), where Dropbox-normalized 0600 files were baked into published
# gems. git-based distribution is structurally immune to the 0600
# variant (git records only the exec bit), so the exec bit is the one
# thing left to guard — and the index is the only place to check it.

CCM_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

# Print the index mode (e.g. 100755) of one tracked path.
_index_mode() {
    git -C "$CCM_ROOT" ls-files -s -- "$1" | awk '{print $1}'
}

@test "plugin entry points are executable in the git index" {
    # ccm: the CLI users invoke directly. ccm.tmux: TPM runs this at
    # tmux startup — 644 here disables the whole plugin.
    [[ "$(_index_mode ccm)" == "100755" ]]
    [[ "$(_index_mode ccm.tmux)" == "100755" ]]
}

@test "every hook script is executable in the git index" {
    # Claude Code executes the configured hook path directly, so each
    # on-*.sh must carry the exec bit. Globbing the index (not a
    # hard-coded list) makes a future hook added without +x fail here
    # automatically.
    local count=0 mode path
    while read -r mode _ _ path; do
        count=$((count + 1))
        [[ "$mode" == "100755" ]] || {
            echo "hook not executable in index: $path ($mode)" >&2
            return 1
        }
    done < <(git -C "$CCM_ROOT" ls-files -s -- 'hooks/on-*.sh')
    # Guard the glob itself: if the naming scheme changes and matches
    # nothing, the loop above would pass vacuously.
    [[ "$count" -ge 7 ]]
}

@test "hooks/lib.sh stays non-executable (source-only contract)" {
    # lib.sh is sourced by every hook, never exec'd. Keeping it 644
    # documents that contract in the index; if someone flips it to
    # 755, either they are about to exec it (wrong — it has no main)
    # or the change is accidental. Both deserve a failing test.
    [[ "$(_index_mode hooks/lib.sh)" == "100644" ]]
}

@test "dev scripts are executable in the git index" {
    # scripts/*.sh are maintainer tooling run directly (./scripts/…).
    # Not user-facing, but the same forgotten-+x accident applies.
    local count=0 mode path
    while read -r mode _ _ path; do
        count=$((count + 1))
        [[ "$mode" == "100755" ]] || {
            echo "script not executable in index: $path ($mode)" >&2
            return 1
        }
    done < <(git -C "$CCM_ROOT" ls-files -s -- 'scripts/*.sh')
    [[ "$count" -ge 1 ]]
}
