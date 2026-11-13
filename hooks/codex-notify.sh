#!/usr/bin/env bash
# Codex invokes hooks from a shared process; never route by its TMUX_PANE.
CCM_CODEX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 0
python3 "$CCM_CODEX_ROOT/lib/ccm_sidekick_notify.py" >/dev/null 2>/dev/null || true
exit 0
