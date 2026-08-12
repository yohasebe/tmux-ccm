"""Tests for ccm_runtime — autosave + idle auto-exit + window-name
update helpers. The silent-NameError class of bug (caught by
`log_caught_exception` in production) makes these paths easy to
break without anyone noticing, so the regression coverage here
focuses on `actually called the underlying function` rather than
just `did not raise`."""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

import ccm_runtime
import ccm_core
import ccm_snapshot


class TestUpdateWindowNames:
    """`update_window_names` rewrites tmux window names to carry the
    current state icon. Regression coverage for the same-directory
    dedup interaction: `build_project_list` drops every window after
    the first that shares a canonical directory (`seen_dirs`), so the
    second window is absent from `projects`. The old
    `project_states.get(win_target, "IDLE")` default then rewrote
    that window's name to the IDLE icon every poll cycle regardless
    of its real state. The fix inherits the state from the same-dir
    sibling that IS tracked, and skips windows with no state source
    at all instead of stamping a wrong icon."""

    def _project(self, win_target, name, directory, state):
        return ccm_core.Project(
            win_target=win_target,
            win_idx=win_target.split(":")[1],
            name=name,
            directory=directory,
            state=state,
        )

    def _run(self, projects, listing):
        """Run update_window_names with a stubbed list-windows;
        return the list of rename-window call tuples."""
        renames = []

        def fake_tmux(*args):
            if args[0] == "list-windows":
                return listing
            if args[0] == "rename-window":
                renames.append(args)
            return ""

        with patch("ccm_core.tmux_cmd", side_effect=fake_tmux):
            ccm_runtime.update_window_names(projects)
        return renames

    def test_renames_stale_name_to_state_icon(self):
        projects = [self._project("main:1", "demo", "/tmp/demo", "BUSY")]
        listing = "main:1\tdemo\told-name\t/tmp/demo"
        renames = self._run(projects, listing)
        assert renames == [("rename-window", "-t", "main:1", "◉ demo")]

    def test_noop_when_name_already_matches(self):
        projects = [self._project("main:1", "demo", "/tmp/demo", "IDLE")]
        listing = "main:1\tdemo\t● demo\t/tmp/demo"
        assert self._run(projects, listing) == []

    def test_same_dir_second_window_inherits_sibling_state(self):
        """The core regression: only the first same-dir window is in
        `projects` (seen_dirs dedup), but the second window must NOT
        be rewritten to the IDLE icon — it mirrors the sibling's
        state instead."""
        projects = [self._project("main:1", "demo", "/tmp/shared", "BUSY")]
        listing = (
            "main:1\tdemo\t◉ demo\t/tmp/shared\n"
            "main:2\tdemo2\t● demo2\t/tmp/shared"
        )
        renames = self._run(projects, listing)
        assert renames == [
            ("rename-window", "-t", "main:2", "◉ demo2")
        ], (
            "second same-dir window must inherit the tracked sibling's "
            "state, not be overwritten with the IDLE icon"
        )

    def test_same_dir_symlinked_paths_match(self):
        """The @ccm_dir tag and the project dir may differ textually
        (symlinked path); canonical_dir must align them the same way
        build_project_list's dedup key does."""
        projects = [self._project("main:1", "demo", "/tmp/shared", "PERMIT")]
        # /tmp vs /private/tmp style divergence (macOS /tmp symlink)
        import os
        linked = os.path.realpath("/tmp/shared")
        listing = (
            f"main:1\tdemo\t⚠ demo\t/tmp/shared\n"
            f"main:2\tdemo2\t● demo2\t{linked}"
        )
        renames = self._run(projects, listing)
        assert renames == [("rename-window", "-t", "main:2", "⚠ demo2")]

    def test_untracked_window_without_sibling_is_skipped(self):
        """A tagged window absent from `projects` with no same-dir
        sibling (e.g. tagged mid-cycle) keeps its current name rather
        than being stamped with a wrong IDLE icon; the next poll
        picks it up."""
        projects = [self._project("main:1", "demo", "/tmp/demo", "IDLE")]
        listing = (
            "main:1\tdemo\t● demo\t/tmp/demo\n"
            "main:2\tfresh\tfresh\t/tmp/elsewhere"
        )
        assert self._run(projects, listing) == []

    def test_untracked_window_with_empty_dir_is_skipped(self):
        """No @ccm_dir tag → no dir fallback possible → skip."""
        projects = [self._project("main:1", "demo", "/tmp/demo", "IDLE")]
        listing = "main:2\tghost\t● ghost\t"
        assert self._run(projects, listing) == []


class TestForceAutosave:
    """`_force_autosave` is called from `auto_exit_idle` after
    closing an idle window, so it must persist the snapshot or the
    user loses the project on the next `ccm start _autosave`."""

    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_calls_snapshot_save(self, mock_save):
        ccm_runtime._force_autosave()
        mock_save.assert_called_once_with("_autosave", quiet=True)

    @patch("ccm_core.log_caught_exception")
    @patch("ccm_snapshot.cmd_snapshot_save", side_effect=OSError("disk full"))
    def test_swallows_exception_and_logs(self, mock_save, mock_log):
        # Best-effort path: must not propagate, but must record.
        ccm_runtime._force_autosave()
        mock_log.assert_called_once_with("_force_autosave")


class TestPeriodicAutosave:
    """`periodic_autosave` runs every poll cycle (every ~2 s via
    `inject_status`). It must (a) be cheap to call repeatedly, (b)
    actually persist when 120 s have elapsed, (c) leave a stale
    snapshot alone when no projects exist."""

    @patch("ccm_core.tmux_cmd", return_value="")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_no_projects_skips_save(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_save.assert_not_called()

    @patch("ccm_core.tmux_cmd", return_value="proj1\nproj2\n")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_with_projects_and_no_marker_saves(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        # Fresh marker file (last_save=0) → 120 s threshold passed → save.
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_save.assert_called_once_with("_autosave", quiet=True)
        marker = os.path.join(tmp_path, "autosave-time")
        assert os.path.exists(marker)

    @patch("ccm_core.tmux_cmd", return_value="proj1\n")
    @patch("ccm_snapshot.cmd_snapshot_save")
    def test_recent_marker_skips_save(self, mock_save, mock_tmux, tmp_path, monkeypatch):
        import time
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        marker = os.path.join(tmp_path, "autosave-time")
        with open(marker, "w") as f:
            f.write(str(int(time.time())))  # just-saved marker
        ccm_runtime.periodic_autosave()
        mock_save.assert_not_called()

    @patch("ccm_core.log_caught_exception")
    @patch("ccm_core.tmux_cmd", return_value="proj1\n")
    @patch("ccm_snapshot.cmd_snapshot_save", side_effect=OSError("disk full"))
    def test_save_failure_logs_silently(
        self, mock_save, mock_tmux, mock_log, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(ccm_core, "CCM_TMP_DIR", str(tmp_path))
        ccm_runtime.periodic_autosave()
        mock_log.assert_called_once_with("periodic_autosave")


class TestAutoExitIdle:
    """`auto_exit_idle` walks ccm windows past the idle timeout, sends
    `/exit` to Claude, then `clear` to wipe the screen for the next
    attach.

    Two safety properties are pinned here:

      1. `clear` must only be sent after `/exit` has completed. On
         long sessions Claude's shutdown can exceed the 0.5 s wait
         window; sending `clear` while Claude is still foreground
         delivers literal text into its input box and submits it as
         an unintended user prompt (observed live, and
         retro-explains earlier "test" one-word injections).
      2. Every keystroke must be addressed to the Claude pane
         directly, not to the window's active pane. If the user
         split off a shell pane and left focus on it, `send-keys`
         to the window-level target lands the Escape + `/exit` +
         Enter sequence in the shell. In emacs mode that becomes
         `Meta-/` (no-op completion) followed by the literal
         characters `exit` plus Enter, silently killing the user's
         shell pane some hours after they last touched it (reported
         on the sample-proj window itself).

    Coverage:
      - Happy path: shell foreground (`zsh`) → `clear` IS sent.
      - Race path:  Claude still alive (`claude`) → `clear` skipped.
      - Failure path: tmux query returns "" → `clear` skipped.
      - Pane targeting: keystrokes go to the Claude pane, even when
        a non-Claude pane is active in the window.
      - Defensive skip: no Claude pane in window → nothing fires.
    """

    # `list-panes -F "#{pane_index}\t#{pane_pid}"` output for a
    # single-pane window with the Claude pane at index 0 hosting
    # a shell whose child is `claude`. Format: `<idx>\t<pane_pid>`.
    DEFAULT_PANES_LISTING = "0\t1000"

    # Stands in for the session id ccm caches on the window.
    SESSION_ID = "0123abcd-0000-0000-0000-000000000000"
    # The example project name this fixture listing carries.
    PROJECT_NAME = "demo"

    # `ps_snapshot()` returns the raw stdout string from
    # `ps -eo pid,ppid,pgid,comm,etime`. ccm_runtime splits it into
    # lines via `.strip().split("\n")`; tests therefore mock the
    # snapshot as a multi-line string, NOT a Python list. Earlier
    # versions of these tests provided a list directly, which masked
    # a production bug where ccm_runtime forgot to split the string
    # (`find_claude_pid` then iterated character-by-character and
    # returned None for every pane).
    DEFAULT_PS_OUTPUT = (
        "1000 999 1000 zsh 00:01:00\n"
        "1001 1000 1001 claude 00:00:30\n"
    )

    @staticmethod
    def _build_tmux_side_effect(post_exit_cmd, panes_listing,
                               self_session_id=SESSION_ID):
        """Wire `tmux_cmd` so it returns the right value for each
        query auto_exit_idle makes during a single past-timeout pass.

        - idle-timeout option lookup → ""  (use IDLE_EXIT_TIMEOUT)
        - display-message session / window → "main" / "0"
        - list-windows -a -F      → one expired window (non-focused)
        - list-panes -t <win>     → `panes_listing` (default: single
                                    pane at index 0 with pane_pid 1000)
        - display-message -t <pane> #{pane_current_command} →
                                    `post_exit_cmd` (parameterised:
                                    zsh / "claude" / empty / version)
        - send-keys / set-option  → ""  (side effects only)
        """
        def side_effect(*args):
            if args[:2] == ("show-option", "-gqv"):
                return ""
            if args[:2] == ("display-message", "-p"):
                fmt = args[2]
                if fmt == "#{session_name}":
                    return "main"
                if fmt == "#{window_index}":
                    return "0"
            if args[:2] == ("display-message", "-t"):
                fmt = args[-1]
                if fmt == "#{pane_current_command}":
                    return post_exit_cmd
            if args[0] == "list-panes":
                return panes_listing
            if args[0] == "list-windows":
                # Single ccm window at main:1 (NOT main:0 → not focused),
                # IDLE for 9999 s (well past the 600 s default timeout).
                old = "1"
                return (f"main:1\tdemo\tIDLE\t{old}\t{old}\t"
                        f"{self_session_id}")
            return ""
        return side_effect

    def _run(self, post_exit_cmd, panes_listing=None, ps_output=None,
             return_side_effects=False):
        if panes_listing is None:
            panes_listing = self.DEFAULT_PANES_LISTING
        if ps_output is None:
            ps_output = self.DEFAULT_PS_OUTPUT
        send_calls = []
        tmux_se = self._build_tmux_side_effect(post_exit_cmd, panes_listing)

        def tmux_recorder(*args):
            if args and args[0] == "send-keys":
                send_calls.append(args)
            return tmux_se(*args)

        with patch("ccm_core.tmux_cmd", side_effect=tmux_recorder), \
             patch("ccm_core.ps_snapshot", return_value=ps_output), \
             patch("ccm_detection._set_win_state") as mock_set_state, \
             patch("ccm_runtime._force_autosave") as mock_autosave, \
             patch("ccm_runtime.ccm_notify") as mock_notify, \
             patch("ccm_runtime.time.sleep"):
            ccm_runtime.auto_exit_idle([])
        if return_side_effects:
            return send_calls, mock_set_state, mock_autosave, mock_notify
        return send_calls

    def test_clear_sent_when_shell_foreground(self):
        """Happy path: `/exit` completed, pane returned to zsh, so
        `clear` is safe to send. This is the original intent of the
        auto-exit cleanup."""
        send_calls = self._run("zsh")
        sent_strings = [c[3] for c in send_calls if len(c) >= 4]
        assert "clear" in sent_strings, (
            f"expected `clear` to be sent to a shell pane, "
            f"got send-keys calls: {send_calls}"
        )

    def test_clear_skipped_when_claude_still_running(self):
        """Race path: `/exit` did not complete within the 0.5 s wait
        (heavy session, confirmation modal, slow shutdown). pane
        foreground is still `claude`. ccm MUST NOT send `clear` —
        otherwise the literal text leaks into Claude's input box as
        an unintended user prompt."""
        send_calls = self._run("claude")
        sent_strings = [c[3] for c in send_calls if len(c) >= 4]
        assert "clear" not in sent_strings, (
            f"`clear` was sent while Claude was still the foreground "
            f"process — would land as user input. send-keys: {send_calls}"
        )

    def test_clear_skipped_when_pane_query_fails(self):
        """Failure path: `tmux_cmd` returns "" on subprocess timeout
        or non-zero exit. We treat that as "unknown foreground" and
        skip `clear` rather than risk a stray keystroke. Cosmetic
        cost (stale screen on next attach) is acceptable; safety
        gain (no clear leak) is the priority."""
        send_calls = self._run("")
        sent_strings = [c[3] for c in send_calls if len(c) >= 4]
        assert "clear" not in sent_strings, (
            f"`clear` was sent despite a failed pane_current_command "
            f"query — should fail-safe to skip. send-keys: {send_calls}"
        )

    def test_no_auto_exit_when_focused_window_unresolved(self):
        """Safety guard (adversarial-review finding): if
        `display-message` returns empty for the focused session/window
        (query failed / no client context), current_target would be
        ":" and protect no window — letting the focused Claude be
        auto-exited. The function must bail the whole cycle instead."""
        send_calls = []

        def side_effect(*args):
            if args[:2] == ("show-option", "-gqv"):
                return ""
            if args[:2] == ("display-message", "-p"):
                return ""  # focused session/window unresolved
            if args[0] == "list-windows":
                return "main:1\tdemo\tIDLE\t1\t1"
            if args[0] == "list-panes":
                return self.DEFAULT_PANES_LISTING
            if args and args[0] == "send-keys":
                send_calls.append(args)
            return ""

        with patch("ccm_core.tmux_cmd", side_effect=side_effect), \
             patch("ccm_core.ps_snapshot", return_value=self.DEFAULT_PS_OUTPUT), \
             patch("ccm_detection._set_win_state") as mock_set_state, \
             patch("ccm_runtime._force_autosave") as mock_autosave, \
             patch("ccm_runtime.time.sleep"):
            ccm_runtime.auto_exit_idle([])

        assert send_calls == [], (
            f"auto-exit fired with the focused window unresolved — the "
            f"focused-window protection was bypassed. send-keys: {send_calls}"
        )
        mock_set_state.assert_not_called()
        mock_autosave.assert_not_called()

    def test_shell_transition_and_autosave_gated_on_exit_success(self):
        """Side-effect gating (adversarial-review finding):
        the SHELL state write and the autosave must fire ONLY when
        `/exit` actually completed (pane foreground back to a shell).
        On the race path (Claude still foreground) declaring SHELL
        would be a one-cycle state lie that the next detection pass
        has to undo, plus a spurious autosave against it.

        Happy path (zsh): SHELL write + autosave fire."""
        _, mock_set_state, mock_autosave, _notify = self._run(
            "zsh", return_side_effects=True
        )
        mock_set_state.assert_called_once()
        assert mock_set_state.call_args.args[1] == "SHELL"
        mock_autosave.assert_called_once()

    def test_no_shell_transition_when_exit_did_not_complete(self):
        """Race path (claude still foreground): NO SHELL write, NO
        autosave — the window keeps its real state for the next poll
        to re-derive. This is the regression the gating prevents."""
        _, mock_set_state, mock_autosave, _notify = self._run(
            "claude", return_side_effects=True
        )
        mock_set_state.assert_not_called()
        mock_autosave.assert_not_called()

    def test_no_shell_transition_when_pane_query_fails(self):
        """Query-failure path ("" foreground): treat as exit-not-
        confirmed, skip the SHELL write and autosave. Detection
        re-derives the true state next cycle."""
        _, mock_set_state, mock_autosave, _notify = self._run(
            "", return_side_effects=True
        )
        mock_set_state.assert_not_called()
        mock_autosave.assert_not_called()

    def test_send_keys_targets_claude_pane_not_active_pane(self):
        """Pane targeting bug fix: when the window's
        active pane is NOT the Claude pane (e.g., user split off a
        shell pane for `ccm update` and left focus on it), every
        send-keys must address the Claude pane directly via
        `win:idx.pane_idx`. Without this, the Escape + `/exit` +
        Enter sequence lands in the user's shell: Meta-/ on an
        empty prompt is a no-op completion, the literal characters
        `exit` fill the buffer, and Enter submits `exit` to the
        shell — silently killing the user's pane."""
        # Window has shell at pane 0 (pid 2000, no claude child) and
        # the Claude pane at index 1 (pid 1000 → claude child 1001).
        ps_output = (
            "2000 999 2000 zsh 00:01:00\n"           # pane 0 shell, no claude
            "1000 999 1000 zsh 00:01:00\n"           # pane 1 shell
            "1001 1000 1001 claude 00:00:30\n"       # claude under pane 1's shell
        )
        send_calls = self._run(
            "zsh",
            panes_listing="0\t2000\n1\t1000",
            ps_output=ps_output,
        )
        targets = [c[2] for c in send_calls if len(c) >= 3]
        assert targets, "no send-keys fired; expected the auto-exit sequence"
        assert all(t == "main:1.1" for t in targets), (
            f"send-keys leaked to a non-Claude pane: {send_calls}. "
            "Auto-exit must target `win:idx.pane_idx` for the Claude pane "
            "so a focused shell pane never receives the keystrokes."
        )

    def test_skipped_when_no_claude_pane_in_window(self):
        """Defensive skip: if no pane in the window currently has a
        `claude` descendant in its process tree (mid-transition,
        recently exited, detection race), auto-exit must not fire
        any keystrokes — there is no safe target. The next polling
        cycle will re-evaluate the window."""
        ps_output = (
            "1000 999 1000 zsh 00:01:00\n"           # plain shell
            "2000 999 2000 nvim 00:00:30\n"          # editor, not claude
        )
        send_calls = self._run(
            "zsh",
            panes_listing="0\t1000\n1\t2000",
            ps_output=ps_output,
        )
        assert send_calls == [], (
            f"auto-exit fired send-keys with no Claude pane present: "
            f"{send_calls}. Should defer to the next polling cycle."
        )

    def test_pane_that_is_claude_itself_is_never_exited(self):
        """`tmux new-window "claude …"` leaves no shell under the pane,
        so the pane pid IS claude. Exiting it ends the pane's only
        process and the pane closes, changing the window's layout —
        auto-exit reclaims an idle process, it does not close panes.

        These panes were unreachable by accident until this fix:
        the process walk only looked for a child, so they resolved to
        no-claude and the background-work guard read their versioned
        command name as live work. Teaching the walk about them
        removed that cover, so the exclusion is explicit now."""
        # pane pid 1000 is itself `claude` — no shell in between.
        ps_output = "1000 999 1000 claude 00:05:00\n"
        send_calls = self._run(
            "claude",
            panes_listing="0\t1000",
            ps_output=ps_output,
        )
        assert send_calls == [], (
            f"auto-exit targeted a pane whose own process is claude: "
            f"{send_calls}. Exiting it would close the pane."
        )

    def test_claude_pane_found_when_pane_current_command_is_version(self):
        """Real-world regression: on standard claude.ai
        installs the binary lives at `.../versions/<X.Y.Z>/`, with
        `claude` as a symlink. macOS's `proc_pidinfo` reports the
        version basename as `#{pane_current_command}` (e.g.
        "2.1.167"), so a fix that string-matched the foreground
        command would never identify the Claude pane and auto-exit
        would silently no-op forever. `ps`'s `comm` field is still
        "claude" though, so the process-tree path (`find_claude_pid`
        against `ps_snapshot`) keeps working — pin that here."""
        # post_exit_cmd is the VERSION string (what tmux returns):
        # `pane_current_command` shows "2.1.167" instead of "claude",
        # but the ps-based pane lookup still finds the Claude child.
        send_calls = self._run("2.1.167")
        targets = [c[2] for c in send_calls if len(c) >= 3]
        assert targets, (
            "auto-exit failed to fire even though ps shows a claude "
            "child of the pane shell. The process-tree lookup must "
            "not depend on `#{pane_current_command}` matching `claude`."
        )
        # All keystrokes go to the Claude pane (`main:1.0`); the
        # default scaffold puts Claude at pane index 0.
        assert all(t == "main:1.0" for t in targets), send_calls


class TestAutoExitBackgroundWorkGuard:
    """Auto-exit must leave a window alone while it hosts live
    background work, however long the Claude conversation has been
    idle. Cost asymmetry: wrongly exiting interrupts running work
    (incident — a sibling-pane batch job's
    window went quiet for 10 min and Claude was exited out from
    under an active project); wrongly keeping costs one idle Claude
    process. Two signals, either sufficient:

      - a non-Claude pane whose foreground is not a shell (batch,
        dev server, tail, editor);
      - the Claude process has a live shell child (a Bash tool job,
        foreground or run_in_background, still running).
    """

    # Window with Claude pane (idx 0) + sibling pane (idx 1).
    # 3-field listing: idx \t pane_pid \t pane_current_command.
    def _panes(self, sibling_cmd):
        return f"0\t1000\tzsh\n1\t2000\t{sibling_cmd}"

    PS_BASE = (
        "1000 999 1000 zsh 01:00:00\n"
        "1001 1000 1001 claude 00:30:00\n"
        "2000 999 2000 zsh 01:00:00\n"
    )

    def _run(self, panes_listing, ps_output):
        helper = TestAutoExitIdle()
        return helper._run("zsh", panes_listing=panes_listing,
                           ps_output=ps_output)

    def test_sibling_pane_running_job_skips_exit(self):
        """The incident shape: batch job (ruby) in the split pane.
        No keystroke may reach the window."""
        send_calls = self._run(self._panes("ruby"), self.PS_BASE)
        assert send_calls == [], (
            f"auto-exit fired despite a live sibling-pane job: "
            f"{send_calls}"
        )

    def test_sibling_pane_idle_shell_still_exits(self):
        """A plain idle zsh in the split pane is NOT background work
        — the guard must not block the exit (otherwise any split
        disables auto-exit entirely)."""
        send_calls = self._run(self._panes("zsh"), self.PS_BASE)
        sent = [c[3] for c in send_calls if len(c) >= 4]
        assert "/exit" in sent

    def test_sibling_login_shell_dash_prefix_still_exits(self):
        """Login shells report as '-zsh'; the dash must be stripped
        before the shell-set membership test."""
        send_calls = self._run(self._panes("-zsh"), self.PS_BASE)
        sent = [c[3] for c in send_calls if len(c) >= 4]
        assert "/exit" in sent

    def test_claude_shell_child_skips_exit(self):
        """A live shell child under the claude process = a Bash tool
        job (foreground or background task) still running. Exiting
        would orphan it."""
        ps = self.PS_BASE + "1002 1001 1002 zsh 00:05:00\n"
        send_calls = self._run(self._panes("zsh"), ps)
        assert send_calls == [], (
            f"auto-exit fired despite a live Bash tool shell under "
            f"claude: {send_calls}"
        )

    def test_parked_editor_sibling_still_exits(self):
        """A parked nvim in the split pane is ambient tooling, not
        background work. Active editing refreshes window_activity
        (screen output) and resets the idle timer on its own, and
        exiting Claude leaves the editor pane untouched — so the
        guard must not treat it as live work. Without this exemption
        a split-editor workflow silently disables auto-exit for
        every window (observed: three sessions idle 3-4
        days with @ccm-idle-timeout 10 set, each with a parked nvim
        in the second pane)."""
        for editor in ("nvim", "vim", "emacs", "less"):
            send_calls = self._run(self._panes(editor), self.PS_BASE)
            sent = [c[3] for c in send_calls if len(c) >= 4]
            assert "/exit" in sent, (
                f"parked {editor} wrongly blocked auto-exit"
            )

    def test_mcp_children_do_not_block_exit(self):
        """MCP servers / LSP workers are direct children of claude
        but never bare shells — the standing worker set must not
        suppress auto-exit."""
        ps = self.PS_BASE + (
            "1003 1001 1003 node 02:00:00\n"
            "1004 1001 1004 python3 02:00:00\n"
        )
        send_calls = self._run(self._panes("zsh"), ps)
        sent = [c[3] for c in send_calls if len(c) >= 4]
        assert "/exit" in sent


class TestAutoExitNotification:
    """A completed auto-exit must announce itself: an unannounced
    exit reads as a crash or a mystery timeout and sends the user
    hunting for a cause (report). Fired only
    after the shell-foreground gate confirmed the exit landed."""

    def test_notify_fired_on_confirmed_exit(self):
        helper = TestAutoExitIdle()
        _, _, _, mock_notify = helper._run(
            "zsh", return_side_effects=True)
        mock_notify.notify.assert_called_once()
        args, kwargs = mock_notify.notify.call_args
        assert args[0] == "AUTOEXIT"
        assert args[1] == "demo"          # project name from listing
        assert "m" in kwargs.get("detail", "")  # "10m" etc.

    def test_no_notify_when_exit_did_not_complete(self):
        """Claude still foreground → exit unconfirmed → no
        notification (it must never announce an exit that didn't
        happen)."""
        helper = TestAutoExitIdle()
        _, _, _, mock_notify = helper._run(
            "claude", return_side_effects=True)
        mock_notify.notify.assert_not_called()


class TestNotifyAutoExitGating:
    """AUTOEXIT bypasses the @ccm-notify per-state opt-in list (it
    announces an autonomous destructive-looking action) but still
    honors the global 'off' kill switch."""

    def _notify(self, setting, state="AUTOEXIT"):
        import ccm_notify
        sent = []
        def fake_tmux(*args):
            if "@ccm-notify" in args and "-gqv" in args:
                return setting
            return ""
        with patch("ccm_core.tmux_cmd", side_effect=fake_tmux), \
             patch.object(ccm_notify, "_terminal_notifier_path",
                          return_value=None), \
             patch("ccm_notify.subprocess.Popen",
                   side_effect=lambda *a, **k: sent.append(a) or MagicMock()):
            ccm_notify.notify(state, "proj", detail="10m")
        return sent

    def test_fires_with_default_setting(self):
        # default "permit,completed" does not contain "autoexit" —
        # AUTOEXIT must fire anyway.
        assert self._notify("permit,completed")

    def test_fires_with_empty_setting(self):
        assert self._notify("")

    def test_silenced_by_off(self):
        assert self._notify("off") == []

    def test_other_states_still_gated(self):
        # sanity: the bypass is AUTOEXIT-only; BUSY stays gated by
        # the setting list.
        assert self._notify("permit,completed", state="BUSY") == []


class TestAutoExitEvidenceLog:
    """A desktop notification tells whoever is looking at the screen.
    It tells nobody afterwards.

    Claude Code reports an auto-exit as `SessionEnd` with reason
    `prompt_input_exit` — the same value a person typing `/exit`
    produces. So the only place the difference can be recorded is
    here, and without it the question has no answer anywhere: a
    neighbouring tool read eight such endings in a row and concluded
    the sessions were crashing.
    """

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CCM_AUTO_EXIT_LOG",
                           str(tmp_path / "state" / "auto-exit.log"))

    def _records(self):
        try:
            with open(ccm_runtime.auto_exit_log_path(), encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except OSError:
            return []

    def test_a_confirmed_exit_is_recorded(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        TestAutoExitIdle()._run("zsh")
        records = self._records()
        assert len(records) == 1, records
        assert records[0]["project"] == TestAutoExitIdle.PROJECT_NAME

    def test_the_record_carries_the_session_id(self, monkeypatch, tmp_path):
        """The join key. Whatever else watched the session end knows it
        by this id and by nothing else ccm has."""
        self._isolate(monkeypatch, tmp_path)
        TestAutoExitIdle()._run("zsh")
        assert self._records()[0]["session"] == TestAutoExitIdle.SESSION_ID

    def test_an_exit_that_did_not_land_is_not_recorded(
            self, monkeypatch, tmp_path):
        """Same gate as the notification: the log must never claim an
        exit that did not happen."""
        self._isolate(monkeypatch, tmp_path)
        TestAutoExitIdle()._run("claude")
        assert self._records() == []

    def test_the_count_matches_what_was_written(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        assert ccm_runtime.auto_exit_log_count() == 0
        TestAutoExitIdle()._run("zsh")
        assert ccm_runtime.auto_exit_log_count() == 1

    def test_an_unwritable_log_does_not_break_the_exit(
            self, monkeypatch, tmp_path):
        """The session was closed cleanly; failing to write about it
        must not turn that into an error."""
        monkeypatch.setenv("CCM_AUTO_EXIT_LOG", "/proc/nonexistent/x.log")
        TestAutoExitIdle()._run("zsh")   # must not raise

    def test_the_log_rotates_at_the_cap(self, monkeypatch, tmp_path):
        log = tmp_path / "state" / "auto-exit.log"
        log.parent.mkdir(parents=True)
        log.write_text("x" * 64)
        monkeypatch.setenv("CCM_AUTO_EXIT_LOG", str(log))
        monkeypatch.setattr(ccm_runtime, "AUTO_EXIT_LOG_MAX_BYTES", 32)
        ccm_runtime._log_auto_exit("demo", "abc", 600, 1)
        assert (tmp_path / "state" / "auto-exit.log.1").exists()
        assert len(self._records()) == 1
