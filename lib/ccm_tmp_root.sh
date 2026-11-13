# shellcheck shell=bash
# The per-user temp root (`${TMPDIR:-/tmp}/ccm-$UID`) holds hook signals,
# locks and pid files. Where TMPDIR is unset (common on Linux) it sits in
# the shared /tmp, so it must be a real directory owned by this user, and
# nobody else may ever have been able to write into it, before anything
# is written there. One definition, sourced by every shell entry point.

# Create or check `$1`. Returns non-zero when it cannot be trusted: a
# symlink, not a directory, or owned by someone else. A root of ours that
# others could write into (an older ccm made it so under umask 002) may
# hold anything, so it is moved aside unopened, to `<root>.untrusted-<pid>`,
# and a fresh one is made. A root others can only read is closed to 0700.
# Hooks call this on every event, so the usual path forks once (ls).
ccm_secure_tmp_root() {
    local root="$1" mode
    # `link/` and `link/.` resolve through the link, so test the name
    # itself: drop trailing slashes, refuse a final `.` or `..`.
    while [[ "$root" == */ && "$root" != / ]]; do root="${root%/}"; done
    case "${root##*/}" in ''|.|..) return 1 ;; esac
    [[ -e "$root" || -L "$root" ]] || (umask 077 && mkdir -p "$root") 2>/dev/null
    [[ ! -L "$root" && -d "$root" && -O "$root" ]] || return 1
    mode=$(ls -ld "$root" 2>/dev/null) || return 1
    if [[ "${mode:5:1}" == w || "${mode:8:1}" == w ]]; then
        mv "$root" "${root}.untrusted-$$" 2>/dev/null
        (umask 077 && mkdir "$root") 2>/dev/null
        [[ ! -L "$root" && -d "$root" && -O "$root" ]] || return 1
        mode=$(ls -ld "$root" 2>/dev/null) || return 1
        [[ "${mode:5:1}" != w && "${mode:8:1}" != w ]] || return 1
    fi
    [[ "${mode:4:6}" == ------ ]] || chmod 700 "$root" 2>/dev/null || return 1
}
