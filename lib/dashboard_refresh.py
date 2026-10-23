"""How the dashboard keeps its project list current: the background
refresh thread, its fast tick on pushed states, and its pace."""

import os
import time

import ccm_agentview
from ccm_constants import CCM_TMP_DIR
from ccm_core import build_project_list, get_session, log_caught_exception, tmux_cmd

REFRESH_INTERVAL = 2
# Fast-tick cadence for the hybrid refresh loop. Between full
# detection passes (REFRESH_INTERVAL), the refresh thread samples the
# PUSHED state channel — `@ccm_prev_state`, written instantly by the
# Claude Code hooks (and by the slow path's own commits) — with one
# `list-windows` subprocess per tick (~10 ms). A hook-driven state
# change therefore reaches the dashboard in ≤ ~0.3 s instead of
# waiting out the 2 s poll (worst case ~2.3 s), closing most of the
# latency gap vs purely event-driven consumers of the same hooks.
FAST_TICK_INTERVAL = 0.25
# A dashboard left open (a dock, typically) and not used for a while
# refreshes at a slower pace: the pushed channel is still checked every
# IDLE_FAST_TICK_INTERVAL, and full detection,
# which catches what no hook reports, runs every IDLE_REFRESH_INTERVAL.
# The first key press restores the normal pace.
IDLE_AFTER_SEC = 30
IDLE_FAST_TICK_INTERVAL = 0.5
IDLE_REFRESH_INTERVAL = 6


def popup_dashboard_alive():
    """True while a popup dashboard (or menu / tree / filter) runs."""
    try:
        pid = int(open(os.path.join(CCM_TMP_DIR, "dashboard.pid"), encoding="utf-8").read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


class RefreshMixin:

    def _select_current_project(self):
        """Select the opening window once, after the initial display order is set."""
        self.selected = 0
        try:
            session = get_session()
            index = tmux_cmd("display-message", "-p", "#{window_index}")
            if not session or not index:
                return
            target = f"{session}:{index}"
            self.selected = next(
                (i for i, p in enumerate(self.projects) if p.win_target == target),
                0,
            )
        except Exception:
            log_caught_exception("dashboard._select_current_project")

    def _set_projects_stable(self, projects):
        """Assign `self.projects` while (a) holding the row order stable
        for the dashboard's lifetime and (b) pinning the selection to
        the same project across refreshes.

        `build_project_list` re-sorts by state every refresh, so a
        project changing state (e.g. BUSY→IDLE, or a new PERMIT) would
        otherwise move rows under the cursor — and since `self.selected`
        is a positional index, the highlight would jump to a *different*
        project mid-interaction. Here the order is frozen at first build
        (state-sorted, as `build_project_list` returns it): later
        refreshes follow that frozen order, projects opened while the
        dashboard is up append at the end, and vanished ones drop out.
        Each popup open is a fresh process, so reopening re-decides the
        order from current state — matching "decide order on show, keep
        it while shown". A state change still updates a row's icon/color
        in place; it just doesn't reshuffle.

        Caller holds `self.lock` (except the single-threaded initial
        build in `__init__`)."""
        # Remember the cursor's project by identity (None if the
        # selection is on a background-session row, past the projects).
        prev_target = None
        if 0 <= self.selected < len(self.projects):
            prev_target = self.projects[self.selected].win_target

        if not self._display_order:
            # First build establishes the frozen order.
            self._display_order = [p.win_target for p in projects]
            ordered = list(projects)
        else:
            rank = {t: i for i, t in enumerate(self._display_order)}
            known = sorted((p for p in projects if p.win_target in rank),
                           key=lambda p: rank[p.win_target])
            # Projects opened since the freeze: keep their (state-sorted)
            # relative order and append to the frozen order so they stay
            # put on subsequent refreshes. Stale entries in
            # `_display_order` (a project that vanished) are harmless —
            # they simply match nothing, and a project that flickers out
            # and back returns to its original slot.
            new = [p for p in projects if p.win_target not in rank]
            self._display_order.extend(p.win_target for p in new)
            ordered = known + new

        self.projects = ordered

        # Re-pin the cursor: the project's index may have shifted if a
        # project above it appeared or vanished.
        if prev_target is not None:
            for i, p in enumerate(ordered):
                if p.win_target == prev_target:
                    self.selected = i
                    break

    def _trigger_rebuild(self):
        """Force a rebuild in the background thread."""
        projects = build_project_list(fast=True)
        with self.lock:
            self._set_projects_stable(projects)
            self.data_dirty = True

    def _refresh_loop(self):
        """Background thread: periodic state refresh."""
        # First refresh: fast (no git/port)
        time.sleep(0.3)
        try:
            projects = build_project_list(fast=False)
            # Skip the bg-section roster read when the section is
            # hidden — users who never use agent view should pay zero
            # I/O for it. When the user toggles bg on with `b` (or via
            # the menu), the toggle handler does an immediate
            # synchronous fetch so the first render after toggle is
            # already populated, no flicker.
            #
            # `bg_visible` is toggled by the main thread (under the
            # lock); snapshot it under the lock here too so this
            # thread never reads a torn / mid-toggle value.
            with self.lock:
                bg_visible = self.bg_visible
            bg_sessions = self._fetch_bg_sessions() if bg_visible else []
            handoff_warnings = self._fetch_handoff_warnings(
                projects, bg_sessions if bg_visible else None)
            with self.lock:
                self._set_projects_stable(projects)
                self.bg_sessions = bg_sessions
                self.handoff_warnings = handoff_warnings
                self.initial_load = False
                self.data_dirty = True
        except Exception:
            log_caught_exception("dashboard._refresh_loop:initial")
            self.initial_load = False

        # Subsequent refreshes: hybrid loop. Fast ticks sample the
        # pushed-state channel between full (slow-path) detection
        # passes — see FAST_TICK_INTERVAL for the design rationale.
        last_full = time.monotonic()
        while self.running:
            tick = self._refresh_pace()[0]
            time.sleep(tick)
            if not self.running:
                return
            self._fast_tick()
            if time.monotonic() - last_full < self._refresh_pace()[1]:
                continue
            last_full = time.monotonic()
            try:
                # While a popup runs its own detection, a docked dashboard
                # reads the states it writes rather than detecting twice.
                projects = build_project_list(
                    fast=self.docked and popup_dashboard_alive())
                with self.lock:
                    bg_visible = self.bg_visible
                bg_sessions = (
                    self._fetch_bg_sessions() if bg_visible else []
                )
                handoff_warnings = self._fetch_handoff_warnings(
                    projects, bg_sessions if bg_visible else None)
                with self.lock:
                    self._set_projects_stable(projects)
                    self.bg_sessions = bg_sessions
                    self.handoff_warnings = handoff_warnings
                    self.data_dirty = True
                # Refresh preview content if enabled
                if self.preview_enabled and self.mode == "dashboard":
                    self._last_preview_target = ""  # Force refresh
            except Exception:
                log_caught_exception("dashboard._refresh_loop")

    def _note_input(self):
        self._last_input = time.monotonic()

    def _idle(self):
        return time.monotonic() - getattr(self, "_last_input", 0.0) > IDLE_AFTER_SEC

    def _refresh_pace(self):
        """(fast tick, full refresh) intervals: slower once nobody has
        pressed a key for IDLE_AFTER_SEC."""
        if self._idle():
            return IDLE_FAST_TICK_INTERVAL, IDLE_REFRESH_INTERVAL
        return FAST_TICK_INTERVAL, REFRESH_INTERVAL

    def _scan_pushed_states(self):
        """One `list-windows` subprocess reading the pushed-state
        channel: `{win_target: @ccm_prev_state}` for every ccm
        window. `@ccm_prev_state` is written instantly by the Claude
        Code hooks (`ccm_write_signal`) and by the slow path's own
        commits, so its TRANSITIONS are exactly the events worth
        reacting to between full detection passes."""
        raw = tmux_cmd(
            "list-windows", "-a", "-F",
            "#{session_name}:#{window_index}\t#{@ccm_project}\t#{@ccm_prev_state}",
        )
        out = {}
        if not raw:
            return out
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or not parts[1]:
                continue
            out[parts[0]] = parts[2]
        return out

    def _fast_tick(self):
        """Overlay pushed-state TRANSITIONS onto the displayed list.

        Transition-gated on purpose: the overlay applies only to
        windows whose pushed value CHANGED since the previous tick —
        a fresh hook write or a slow-path commit. Reacting to the
        absolute pushed value instead would re-fight the slow path
        wherever the two legitimately diverge (HOLD_NO_WRITE rules
        like the startup transient deliberately display a state
        WITHOUT committing it to @ccm_prev_state), producing a
        2-second flicker cycle. Steady divergence is the slow path's
        call to make; this tick only relays fresh events.

        Runs on the refresh thread (never concurrent with the slow
        refresh); mutates project states under the lock. Errors are
        swallowed-but-logged like every other refresh-path failure —
        a broken fast tick must degrade to plain 2 s polling, not
        kill the dashboard.
        """
        try:
            pushed = self._scan_pushed_states()
        except Exception:
            log_caught_exception("dashboard._fast_tick")
            return
        prev = self._pushed_states
        self._pushed_states = pushed
        if not prev:
            return  # first sample is baseline only
        changed = {t: s for t, s in pushed.items()
                   if s and prev.get(t) != s}
        if not changed:
            return
        with self.lock:
            dirty = False
            for p in self.projects:
                s = changed.get(p.win_target)
                if s and s != p.state:
                    p.state = s
                    dirty = True
            if dirty:
                self.data_dirty = True

    def _fetch_handoff_warnings(self, projects, bg_sessions=None):
        """Hand-off notices for the project list, read on the refresh
        thread. Independent of the bg-section toggle: the reader is
        about to attach, and the empty session that would follow has
        no other explanation on this screen. When the bg section is
        shown, its roster snapshot from this same cycle is reused
        rather than read again. Never raises."""
        try:
            return ccm_agentview.continue_blocker_warnings(projects, bg_sessions)
        except Exception:
            log_caught_exception("dashboard._fetch_handoff_warnings")
            return []

    def _fetch_bg_sessions(self):
        """Read the agent-view roster. Returns `[]` on any failure
        (missing daemon, malformed file, etc.). Bounded I/O cost —
        9 small JSON files at most in practice."""
        try:
            return ccm_agentview.list_bg_sessions()
        except Exception:
            log_caught_exception("dashboard._fetch_bg_sessions")
            return []
