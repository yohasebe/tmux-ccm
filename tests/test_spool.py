"""Tests for ccm_spool — the store-and-forward queue behind
`ccm send`'s undeliverable states, plus the `ccm spool` CLI and the
reconciler delivery pass.

The semantics under test are the load-bearing ones (see the module
docstring): one message per project per pass, claim-by-rename,
at-least-once, TTL expiry, and fail-closed readiness for the
unattended delivery path."""

import os
import time

from types import SimpleNamespace

import pytest

import ccm_constants
import ccm_core
import ccm_notify
import ccm_send
import ccm_spool

# Alias without the dotted decorator spelling on purpose: the local
# pre-commit hook's email-shaped-string check misfires on
# `@word.word` in an added line (the diff's own "+" prefix plays the
# local part). Same fixture, same semantics.


@pytest.fixture
def spool_root(tmp_path, monkeypatch):
    root = str(tmp_path / "spool")
    monkeypatch.setattr(ccm_spool, "SPOOL_ROOT", root)
    return root


def _project(name="demo", state="IDLE", win_target="0:5"):
    return SimpleNamespace(name=name, state=state, win_target=win_target)


def _pending(root, project="demo"):
    return ccm_spool._pending(os.path.join(root, project))


def composer_screen(*composer_rows, scrollback=True):
    """A pane capture shaped like Claude Code's real screen.

    The composer sits between two horizontal rules with the status
    lines under them, and — crucially — submitted prompts are drawn
    into the transcript above with the SAME glyph. A fixture without
    that scrollback cannot tell a draft from a message already sent,
    which is how the guard shipped refusing every send while any
    prompt was still on screen.
    """
    rule = "\u2500" * 80
    out = []
    if scrollback:
        out += ["\u276f a prompt sent earlier", "\u23fa and its response", ""]
    out += [rule] + list(composer_rows) + [
        rule,
        "  /tmp/demo  main  Opus 5  ctx 42%",
        "  \u23f5\u23f5 auto mode on (shift+tab to cycle)",
    ]
    return "\n".join(out) + "\n"


class TestSuiteIsolation:
    def test_spool_root_is_redirected_for_every_test(self):
        """Positive control for the autouse fixture: no test may write
        into the user's real spool. This one asks for no fixture at
        all — exactly the shape of test that polluted the data
        directory before the redirect moved into conftest."""
        import ccm_spool
        real = os.path.join(ccm_constants.CCM_DATA_DIR, "spool")
        assert ccm_spool.SPOOL_ROOT != real

    def test_enqueue_without_a_fixture_stays_out_of_the_real_spool(self):
        import ccm_spool
        ccm_spool.enqueue("audit-probe", "tester", "body")
        real = os.path.join(ccm_constants.CCM_DATA_DIR, "spool")
        assert not os.path.exists(os.path.join(real, "audit-probe"))


class TestEnqueue:
    def test_enqueue_writes_body_and_counts(self, spool_root):
        msg_id, n = ccm_spool.enqueue("demo", "tester", "hello spool")
        assert n == 1
        names = _pending(spool_root)
        assert names == [msg_id + ".msg"]
        with open(os.path.join(spool_root, "demo", names[0])) as f:
            assert f.read() == "hello spool"

    def test_enqueue_same_millisecond_does_not_overwrite(
            self, spool_root, monkeypatch):
        monkeypatch.setattr(ccm_spool.time, "time", lambda: 1700000000.0)
        id1, _ = ccm_spool.enqueue("demo", "tester", "first")
        id2, n = ccm_spool.enqueue("demo", "tester", "second")
        assert id1 != id2 and n == 2
        assert len(_pending(spool_root)) == 2

    def test_sender_is_filename_sanitised(self, spool_root):
        msg_id, _ = ccm_spool.enqueue("demo", "we ird/sender", "x")
        assert "/" not in msg_id and " " not in msg_id


class TestExpiredVisibility:
    """A message that expired without arriving must be visible in the
    places a reader actually looks, not only in `ccm doctor`. The
    sender was told "queued", so nothing else records the loss."""

    def test_expired_counts_are_reported_per_project(self, spool_root):
        edir = os.path.join(spool_root, "demo", "expired")
        os.makedirs(edir)
        open(os.path.join(edir, "1-tester.msg"), "w").close()
        assert ccm_spool.expired_counts() == {"demo": 1}

    def test_expired_counts_ignore_delivered_evidence(self, spool_root):
        ddir = os.path.join(spool_root, "demo", "delivered")
        os.makedirs(ddir)
        open(os.path.join(ddir, "1-tester.msg"), "w").close()
        assert ccm_spool.expired_counts() == {}

    def test_expired_warning_names_the_project(self, spool_root):
        """The line `ccm status` and the dashboard both print. They
        used to report the queue length alone, so a project whose
        queue had emptied by expiry looked clean in both."""
        import ccm_render
        edir = os.path.join(spool_root, "demo", "expired")
        os.makedirs(edir)
        open(os.path.join(edir, "1-tester.msg"), "w").close()
        msg = ccm_render.spool_expired_warning()
        assert "expired undelivered" in msg and "demo:1" in msg
        assert "`ccm spool list`" in msg and "clear-expired" not in msg

    def test_expired_warning_is_empty_when_nothing_expired(self, spool_root):
        import ccm_render
        assert ccm_render.spool_expired_warning() == ""

    def test_expired_warning_caps_a_long_project_list(self, spool_root):
        """Warning lines share the terminal with everything else; a
        long list of names must not push the rest off screen."""
        import ccm_render
        for i in range(20):
            edir = os.path.join(spool_root, f"project-number-{i}", "expired")
            os.makedirs(edir)
            open(os.path.join(edir, "1-tester.msg"), "w").close()
        msg = ccm_render.spool_expired_warning()
        assert len(msg) < 120 and msg.count("...") == 1


class TestSpoolCli:
    def test_list_empty(self, spool_root, capsys):
        ccm_spool.cmd_spool(["list"])
        assert "No queued messages" in capsys.readouterr().out

    def test_list_shows_id_age_and_preview(self, spool_root, capsys):
        ccm_spool.enqueue("demo", "tester", "first line of body\nsecond")
        ccm_spool.cmd_spool(["list"])
        out = capsys.readouterr().out
        assert "demo:" in out and "first line of body" in out
        assert "ago" in out

    def test_expired_only_project_is_still_listed(self, spool_root, capsys):
        """A project whose queue is empty can still hold the record of
        a message that never arrived. `ccm doctor` counts it and sends
        the reader to this command, so hiding it here is how a lost
        message stays lost."""
        edir = os.path.join(spool_root, "demo", "expired")
        os.makedirs(edir)
        open(os.path.join(edir, "1-tester.msg"), "w").close()
        ccm_spool.cmd_spool(["list"])
        out = capsys.readouterr().out
        assert "demo:" in out
        assert "1 expired" in out
        assert "No queued messages" not in out

    def test_truly_empty_still_says_so(self, spool_root, capsys):
        os.makedirs(os.path.join(spool_root, "demo"))
        ccm_spool.cmd_spool(["list"])
        assert "No queued messages" in capsys.readouterr().out

    def test_list_scoped_to_project(self, spool_root, capsys):
        ccm_spool.enqueue("demo", "a", "for demo")
        ccm_spool.enqueue("other", "a", "for other")
        ccm_spool.cmd_spool(["list", "demo"])
        out = capsys.readouterr().out
        assert "for demo" in out and "for other" not in out

    def test_cancel_by_id(self, spool_root, capsys):
        msg_id, _ = ccm_spool.enqueue("demo", "tester", "withdraw me")
        ccm_spool.cmd_spool(["cancel", msg_id, "demo"])
        assert _pending(spool_root) == []
        assert "Cancelled" in capsys.readouterr().out

    def test_cancel_unknown_id_dies(self, spool_root):
        with pytest.raises(SystemExit):
            ccm_spool.cmd_spool(["cancel", "9999999999999-nobody"])

    def test_cancel_ambiguous_id_across_projects_dies(self, spool_root):
        """Without a project argument the id must resolve uniquely —
        a mis-cancel is the wrong-direction error."""
        pdir1 = os.path.join(spool_root, "a")
        pdir2 = os.path.join(spool_root, "b")
        os.makedirs(pdir1)
        os.makedirs(pdir2)
        shared = "1700000000000-tester.msg"
        for d in (pdir1, pdir2):
            with open(os.path.join(d, shared), "w") as f:
                f.write("x")
        with pytest.raises(SystemExit):
            ccm_spool.cmd_spool(["cancel", shared[:-4]])

    def test_cancel_all(self, spool_root, capsys):
        ccm_spool.enqueue("demo", "a", "one")
        ccm_spool.enqueue("demo", "a", "two")
        ccm_spool.enqueue("other", "a", "three")
        ccm_spool.cmd_spool(["cancel", "--all", "demo"])
        assert _pending(spool_root) == []
        assert len(ccm_spool._pending(os.path.join(spool_root, "other"))) == 1
        assert "2" in capsys.readouterr().out

    def test_unknown_subcommand_dies(self, spool_root):
        with pytest.raises(SystemExit):
            ccm_spool.cmd_spool(["flush"])


class TestClearExpired:
    """A warning that cannot be answered is one the reader learns to
    scroll past, so the evidence of an undelivered message can be
    acknowledged — the same reason `ccm errors` has `--clear`."""

    def _expired(self, spool_root, project="demo", n=1):
        edir = os.path.join(spool_root, project, "expired")
        os.makedirs(edir, exist_ok=True)
        for i in range(n):
            open(os.path.join(edir, f"{i}-tester.msg"), "w").close()

    def test_clears_and_reports(self, spool_root, capsys):
        self._expired(spool_root, n=2)
        ccm_spool.cmd_spool(["clear-expired"])
        assert "Cleared 2" in capsys.readouterr().out
        assert ccm_spool.expired_counts() == {}

    def test_scoped_to_one_project(self, spool_root):
        self._expired(spool_root, "demo")
        self._expired(spool_root, "other")
        ccm_spool.cmd_spool(["clear-expired", "demo"])
        assert ccm_spool.expired_counts() == {"other": 1}

    def test_leaves_queued_messages_alone(self, spool_root):
        self._expired(spool_root)
        ccm_spool.enqueue("demo", "tester", "still waiting")
        ccm_spool.cmd_spool(["clear-expired"])
        assert len(_pending(spool_root)) == 1

    def test_says_so_when_there_is_nothing(self, spool_root, capsys):
        ccm_spool.cmd_spool(["clear-expired"])
        assert "No expired" in capsys.readouterr().out


class TestExpiry:
    def _enqueue_aged(self, spool_root, body, age_sec, project="demo"):
        msg_id, _ = ccm_spool.enqueue(project, "tester", body)
        # Age the message by rewriting its filename timestamp (the
        # timestamp of record, not the mtime).
        pdir = os.path.join(spool_root, project)
        old_ms = int((time.time() - age_sec) * 1000)
        new_name = f"{old_ms}-tester.msg"
        os.rename(os.path.join(pdir, msg_id + ".msg"),
                  os.path.join(pdir, new_name))
        return new_name

    def test_stale_message_expires_instead_of_delivering(
            self, spool_root, monkeypatch):
        monkeypatch.setattr(ccm_spool, "SPOOL_TTL_SEC", 3600)
        monkeypatch.setattr(ccm_notify, "notify", lambda *a, **k: None)
        name = self._enqueue_aged(spool_root, "stale instruction", 3700)
        delivered = []

        monkeypatch.setattr(ccm_spool, "_deliverable_pane",
                            lambda w: ("%51", None))
        monkeypatch.setattr(ccm_send, "_type_body",
                            lambda t, lines: delivered.append(lines))
        monkeypatch.setattr(ccm_send, "_send_keys", lambda *a, **k: None)
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert not delivered, "expired message must not be delivered"
        assert _pending(spool_root) == []
        expired = os.listdir(os.path.join(spool_root, "demo", "expired"))
        assert expired == [name]

    def test_expiry_notifies_the_sender(self, spool_root, monkeypatch):
        """`ccm send` reported the message as queued, and queued is
        not delivered. Without a notification the only trace of the
        loss is a count nobody reads until a reply goes missing —
        which is how a report was lost for a whole TTL."""
        monkeypatch.setattr(ccm_spool, "SPOOL_TTL_SEC", 3600)
        seen = []
        monkeypatch.setattr(ccm_notify, "notify",
                            lambda state, project, detail="": seen.append(
                                (state, project, detail)))
        self._enqueue_aged(spool_root, "stale instruction", 3700)
        ccm_spool._expire_and_prune(os.path.join(spool_root, "demo"),
                                    time.time())
        assert seen == [("SPOOLEXPIRED", "demo", "tester")]

    def test_fresh_message_does_not_notify(self, spool_root, monkeypatch):
        monkeypatch.setattr(ccm_spool, "SPOOL_TTL_SEC", 3600)
        seen = []
        monkeypatch.setattr(ccm_notify, "notify",
                            lambda *a, **k: seen.append(a))
        self._enqueue_aged(spool_root, "fresh", 10)
        ccm_spool._expire_and_prune(os.path.join(spool_root, "demo"),
                                    time.time())
        assert seen == []

    def test_fresh_message_is_not_expired(self, spool_root, monkeypatch):
        monkeypatch.setattr(ccm_spool, "SPOOL_TTL_SEC", 3600)
        self._enqueue_aged(spool_root, "fresh", 3500)
        ccm_spool.reconcile_spools([])  # no projects → no delivery
        assert len(_pending(spool_root)) == 1

    def test_evidence_is_pruned_past_the_keep_window(
            self, spool_root, monkeypatch):
        monkeypatch.setattr(ccm_spool, "_EVIDENCE_KEEP_SEC", 100)
        ddir = os.path.join(spool_root, "demo", "delivered")
        os.makedirs(ddir)
        old = os.path.join(ddir, "1700000000000-tester.msg")
        with open(old, "w") as f:
            f.write("x")
        old_ts = time.time() - 200
        os.utime(old, (old_ts, old_ts))
        ccm_spool.reconcile_spools([])
        assert not os.path.exists(old)


class TestReconcileDelivery:
    def _stub_delivery(self, monkeypatch, deliverable=("%51", None),
                       held=None):
        """Stub the readiness check, the typing helpers and the
        session's answer to the submit; returns (typed_lines, keys).

        `held` is what the target does with the submitted prompt:
        None takes it, and a notice line is what it says when it
        keeps the prompt for its user to confirm."""
        typed, keys = [], []
        monkeypatch.setattr(ccm_spool, "_deliverable_pane",
                            lambda w: deliverable)
        monkeypatch.setattr(ccm_send, "_type_body",
                            lambda t, lines: typed.extend(lines))
        monkeypatch.setattr(ccm_send, "_send_keys",
                            lambda *a, **k: keys.append(a))
        monkeypatch.setattr(ccm_send, "held_after_submit",
                            lambda pane, **kw: held)
        return typed, keys

    def test_a_message_the_session_does_not_take_leaves_the_queue_held(
            self, spool_root, monkeypatch):
        """Typed and submitted is not delivered: Claude Code holds a
        prompt it rewrote for its user to confirm. Recording it as
        delivered would lose it — and putting it back in the queue
        would type it again once the user confirms the copy already
        in the composer, since an empty composer then looks like a
        message that never arrived. It goes to `held/` instead, where
        `ccm spool list` and `ccm doctor` name it."""
        typed, keys = self._stub_delivery(monkeypatch, held="Removed 1 invisible character · review and press Enter to send")
        ccm_spool.enqueue("demo", "tester", "please review\u200b now")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert any("please review" in ln for ln in typed)
        assert any(a == ("%51", "Enter") for a in keys)
        assert _pending(spool_root) == []
        assert not os.path.isdir(os.path.join(spool_root, "demo", "delivered"))
        held_dir = os.path.join(spool_root, "demo", "held")
        assert len(os.listdir(held_dir)) == 1
        assert ccm_spool.spool_summary()["held"] == 1

    def test_a_held_record_is_listed_and_can_be_acknowledged(
            self, spool_root, monkeypatch, capsys):
        """ccm cannot see how a held message ended — pressing Enter on
        the copy in the composer and clearing it away both leave the
        box empty — so the record stays until the reader says it is
        dealt with. Without that there is a warning nobody can answer."""
        self._stub_delivery(monkeypatch, held="Removed 1 invisible character · review and press Enter to send")
        ccm_spool.enqueue("demo", "tester", "please review\u200b now")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        capsys.readouterr()

        ccm_spool.cmd_spool(["list"])
        listed = capsys.readouterr().out
        assert "(held)" in listed and "please review" in listed
        assert "clear-held" in listed

        ccm_spool.cmd_spool(["clear-held", "demo"])
        assert "Cleared 1 held" in capsys.readouterr().out
        assert ccm_spool.spool_summary()["held"] == 0

    def test_clearing_held_records_sends_nothing(self, spool_root, monkeypatch):
        """Acknowledging a record is bookkeeping. Whatever is in the
        other session's input box is its user's to deal with."""
        _typed, keys = self._stub_delivery(monkeypatch, held="Removed 1 invisible character · review and press Enter to send")
        ccm_spool.enqueue("demo", "tester", "please review\u200b now")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        before = len(keys)
        ccm_spool.cmd_spool(["clear-held", "demo"])
        assert len(keys) == before

    def test_a_held_message_is_never_typed_again(self, spool_root, monkeypatch):
        """The pass after it was held types nothing — not even once
        the user has confirmed or cleared the copy in the composer,
        which ccm cannot tell apart."""
        self._stub_delivery(monkeypatch, held="Removed 1 invisible character · review and press Enter to send")
        ccm_spool.enqueue("demo", "tester", "please review\u200b now")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        typed, keys = self._stub_delivery(monkeypatch)   # target now takes prompts
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert typed == [] and keys == []
        assert ccm_spool.spool_summary()["held"] == 1

    def test_idle_project_receives_the_message(self, spool_root,
                                               monkeypatch):
        typed, keys = self._stub_delivery(monkeypatch)
        ccm_spool.enqueue("demo", "tester", "please review")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert _pending(spool_root) == []
        assert any("please review" in ln for ln in typed)
        assert any(a == ("%51", "Enter") for a in keys)
        delivered = os.listdir(os.path.join(spool_root, "demo", "delivered"))
        assert len(delivered) == 1

    def test_envelope_carries_provenance_and_reply_route(
            self, spool_root, monkeypatch):
        typed, _keys = self._stub_delivery(monkeypatch)
        ccm_spool.enqueue("demo", "origin-proj", "body text")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        header = typed[0]
        assert header.startswith("[from: origin-proj · queued ")
        assert "delivered" in header
        assert 'ccm send origin-proj' in header

    def test_busy_project_stays_queued(self, spool_root, monkeypatch):
        typed, keys = self._stub_delivery(monkeypatch)
        ccm_spool.enqueue("demo", "tester", "wait for idle")
        ccm_spool.reconcile_spools([_project(state="BUSY")])
        assert not typed and not keys
        assert len(_pending(spool_root)) == 1

    def test_unlisted_project_stays_queued(self, spool_root, monkeypatch):
        typed, _ = self._stub_delivery(monkeypatch)
        ccm_spool.enqueue("gone", "tester", "window closed")
        ccm_spool.reconcile_spools([_project(name="demo")])
        assert not typed
        assert len(ccm_spool._pending(os.path.join(spool_root, "gone"))) == 1

    def test_one_message_per_project_per_pass(self, spool_root,
                                              monkeypatch):
        """Two queued messages deliver one per pass: the second would
        land in the input buffer of the turn the first just started
        — the mixing the BUSY refusal exists to prevent."""
        typed, _ = self._stub_delivery(monkeypatch)
        ccm_spool.enqueue("demo", "tester", "first message")
        ccm_spool.enqueue("demo", "tester", "second message")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert any("first message" in ln for ln in typed)
        assert not any("second message" in ln for ln in typed)
        assert len(_pending(spool_root)) == 1

    def test_oldest_message_delivers_first(self, spool_root, monkeypatch):
        typed, _ = self._stub_delivery(monkeypatch)
        pdir = os.path.join(spool_root, "demo")
        os.makedirs(pdir)
        base_ms = int(time.time() * 1000)
        for ms, body in ((base_ms, "newer"), (base_ms - 1000, "older")):
            with open(os.path.join(pdir, f"{ms}-tester.msg"), "w") as f:
                f.write(body)
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert any("older" in ln for ln in typed)
        assert not any("newer" in ln for ln in typed)

    def test_held_lock_skips_the_project(self, spool_root, monkeypatch):
        typed, _ = self._stub_delivery(monkeypatch)
        ccm_spool.enqueue("demo", "tester", "body")
        os.mkdir(os.path.join(spool_root, "demo.lock"))
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert not typed
        assert len(_pending(spool_root)) == 1

    def test_stale_lock_is_reclaimed(self, spool_root, monkeypatch):
        typed, _ = self._stub_delivery(monkeypatch)
        ccm_spool.enqueue("demo", "tester", "body")
        lock = os.path.join(spool_root, "demo.lock")
        os.mkdir(lock)
        old = time.time() - (ccm_spool._LOCK_STALE_SEC + 10)
        os.utime(lock, (old, old))
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert any("body" in ln for ln in typed)
        # The lock is released after the pass.
        assert not os.path.exists(lock)

    def test_unready_target_defers_and_keeps_message(
            self, spool_root, monkeypatch):
        typed, _ = self._stub_delivery(monkeypatch,
                                       deliverable=(None, "raw state BUSY"))
        ccm_spool.enqueue("demo", "tester", "not now")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert not typed
        assert len(_pending(spool_root)) == 1

    def test_claim_race_loses_quietly(self, spool_root, monkeypatch):
        """A message already claimed (renamed away) by a concurrent
        pass is not delivered twice: the loser's rename fails and it
        walks off."""
        typed, _ = self._stub_delivery(monkeypatch)
        pdir = os.path.join(spool_root, "demo")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "1000-tester.msg"), "w") as f:
            f.write("claimed already")
        # Simulate the race: the claim rename finds no source file.
        ccm_spool._deliver_one(_project(), pdir, "9999-tester.msg")
        assert not typed

    def test_typing_failure_restores_the_message(
            self, spool_root, monkeypatch):
        """At-least-once: a crash mid-delivery returns the message to
        pending (a duplicate on retry beats silent loss)."""
        monkeypatch.setattr(ccm_spool, "_deliverable_pane",
                            lambda w: ("%51", None))
        monkeypatch.setattr(ccm_send, "_send_keys", lambda *a, **k: None)

        def boom(_t, _lines):
            raise RuntimeError("simulated crash mid-typing")
        monkeypatch.setattr(ccm_send, "_type_body", boom)
        monkeypatch.setattr(ccm_core, "log_caught_exception",
                            lambda scope: None)
        ccm_spool.enqueue("demo", "tester", "retry me")
        ccm_spool.reconcile_spools([_project(state="IDLE")])
        assert len(_pending(spool_root)) == 1


class TestDeliverablePane:
    """The unattended readiness check. Unlike interactive `ccm send`
    (capture hiccups fail open there), this path fails CLOSED: any
    doubt defers the message to the next pass."""

    def _stub_panes(self, monkeypatch, raw="IDLE", capture="❯ \n",
                    claude=True, active_claude=True):
        from ccm_pane_state import PaneInfo
        pane = PaneInfo("%51", "100", True, "claude", False,
                        101 if claude else None)
        panes = [pane] if active_claude else []
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "x")
        monkeypatch.setattr(ccm_spool, "enumerate_window_panes",
                            lambda w, ps: panes)
        monkeypatch.setattr(ccm_spool, "detect_pane_state",
                            lambda *a, **k: raw)
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            lambda *a: capture if a[0] == "capture-pane"
                            else "")
        monkeypatch.setattr(ccm_core, "is_agents_tui", lambda t: False)
        return pane

    def test_happy(self, monkeypatch):
        self._stub_panes(monkeypatch)
        pane_id, reason = ccm_spool._deliverable_pane("0:5")
        assert pane_id == "%51" and reason is None

    def test_raw_not_idle_defers(self, monkeypatch):
        self._stub_panes(monkeypatch, raw="PERMIT")
        pane_id, reason = ccm_spool._deliverable_pane("0:5")
        assert pane_id is None and "PERMIT" in reason

    def test_composer_draft_defers(self, monkeypatch):
        self._stub_panes(monkeypatch,
                         capture=composer_screen("❯ half typed"))
        pane_id, reason = ccm_spool._deliverable_pane("0:5")
        assert pane_id is None and "draft" in reason

    def test_dim_suggestion_does_not_defer(self, monkeypatch):
        """Claude Code's dim-rendered next-prompt suggestion is not a
        draft — the delivery check must see through it via the `-e`
        capture, answering through the same shared function as
        `ccm send`'s guard."""
        ghost = "\x1b[39m❯ \x1b[2mnow try that\x1b[0m"
        self._stub_panes(monkeypatch)
        monkeypatch.setattr(
            ccm_core, "tmux_cmd",
            lambda *a: (composer_screen(ghost) if "-e" in a
                        else composer_screen("❯ now try that"))
            if a[0] == "capture-pane" else "")
        pane_id, reason = ccm_spool._deliverable_pane("0:5")
        assert pane_id == "%51" and reason is None

    def test_agents_tui_defers(self, monkeypatch):
        self._stub_panes(monkeypatch)
        monkeypatch.setattr(ccm_core, "is_agents_tui", lambda t: True)
        pane_id, reason = ccm_spool._deliverable_pane("0:5")
        assert pane_id is None and "agents" in reason

    def test_unreadable_capture_defers(self, monkeypatch):
        """The asymmetry with cmd_send, pinned: unattended delivery
        with no readable screen must not type blind."""
        self._stub_panes(monkeypatch, capture="")
        pane_id, reason = ccm_spool._deliverable_pane("0:5")
        assert pane_id is None and "capture" in reason

    def test_ambiguous_claude_panes_defer(self, monkeypatch):
        from ccm_pane_state import PaneInfo
        panes = [
            PaneInfo("%51", "100", False, "claude", False, 101),
            PaneInfo("%52", "102", False, "claude", False, 103),
        ]
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "x")
        monkeypatch.setattr(ccm_spool, "enumerate_window_panes",
                            lambda w, ps: panes)
        pane_id, reason = ccm_spool._deliverable_pane("0:5")
        assert pane_id is None and "ambiguous" in reason


class TestCmdSendSpooling:
    """`ccm send`'s default flip: an undeliverable target queues
    instead of refusing (the refusal lives on behind --now, pinned in
    tests/test_send.py)."""

    def _patch_resolution(self, monkeypatch, spool_root, state):
        project = ccm_core.Project(
            win_target="0:5", win_idx="5", name="demo",
            directory="/tmp/demo", state=state,
        )
        monkeypatch.setattr(ccm_core, "get_session", lambda: "0")
        monkeypatch.setattr(ccm_core, "require_session", lambda: "0")
        monkeypatch.setattr(
            ccm_core, "find_window",
            lambda sess, name: project.win_idx if name == project.name else None,
        )
        monkeypatch.setattr(
            ccm_core, "build_project_list", lambda fast=False: [project],
        )
        monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setattr(ccm_core, "caller_context", lambda **kw: ("unknown", ""))
        return project

    def _tmux(self, calls, capture=""):
        def tmux(*args):
            calls.append(args)
            if args[0] == "capture-pane":
                return capture
            return ""
        return tmux

    def test_busy_target_queues_instead_of_refusing(
            self, monkeypatch, spool_root, capsys):
        self._patch_resolution(monkeypatch, spool_root, "BUSY")
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd", self._tmux(calls))
        ccm_send.cmd_send(["demo", "handle this later"])
        assert not [c for c in calls
                    if c[0] == "send-keys" and "-l" in c]
        pending = _pending(spool_root)
        assert len(pending) == 1
        out = capsys.readouterr().out
        assert "Queued for demo" in out and "1 pending" in out
        assert "TTL" in out

    def test_permit_target_queues(self, monkeypatch, spool_root):
        self._patch_resolution(monkeypatch, spool_root, "PERMIT")
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd", self._tmux(calls))
        ccm_send.cmd_send(["demo", "after the dialog"])
        assert not [c for c in calls if c[0] == "send-keys"]
        assert len(_pending(spool_root)) == 1

    def test_shell_target_queues_without_start(
            self, monkeypatch, spool_root):
        self._patch_resolution(monkeypatch, spool_root, "SHELL")
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd", self._tmux(calls))
        ccm_send.cmd_send(["demo", "when claude is up"])
        assert len(_pending(spool_root)) == 1

    def test_draft_target_queues(self, monkeypatch, spool_root):
        self._patch_resolution(monkeypatch, spool_root, "IDLE")
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd",
                            self._tmux(calls, capture=composer_screen("❯ user typing")))
        ccm_send.cmd_send(["demo", "queue behind the draft"])
        assert len(_pending(spool_root)) == 1

    def test_idle_target_still_sends_immediately(
            self, monkeypatch, spool_root):
        """The fast path is untouched: no spool write, straight to
        send-keys."""
        self._patch_resolution(monkeypatch, spool_root, "IDLE")
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd", self._tmux(calls))
        ccm_send.cmd_send(["demo", "right away"])
        assert ("send-keys", "-t", "0:5", "-l", "--", "right away") in calls
        assert not os.path.exists(spool_root)

    def test_sender_label_from_window_tag(self, monkeypatch, spool_root):
        monkeypatch.setenv("TMUX_PANE", "%41")
        monkeypatch.setattr(ccm_core, "tmux_query", lambda *a, **kw:
                            f"%41\torigin\t{os.getcwd()}\t0")
        assert ccm_send._sender_label() == "origin"

    def test_sender_label_unknown_without_registered_cwd(self, monkeypatch):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setattr(ccm_core, "tmux_query", lambda *a, **kw: "")
        assert ccm_send._sender_label() == "unknown"


class TestExpiredDetails:
    """Inspection and acknowledgement must never perform delivery."""

    def _record(self, root, project="demo", filename="1000000-origin.msg",
                body="Review release\nFull details"):
        from pathlib import Path
        path = Path(root) / project / "expired" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_details_and_actions(self, spool_root, monkeypatch, capsys):
        import shlex
        monkeypatch.setattr(ccm_spool.time, "time", lambda: 4660)
        path = self._record(spool_root)
        ccm_spool.cmd_spool(["list"])
        out = capsys.readouterr().out
        assert "1000000-origin  (expired; from: origin; queued: 1h01m ago)  Review release" in out
        assert "never delivered" in out
        assert "Review full text: " + shlex.join(["cat", "--", str(path)]) in out
        assert "If still needed, send as NEW: " + shlex.join(
            ["ccm", "send", "--file", str(path), "--", "demo"]) in out
        assert "After reviewing ALL expired records above: ccm spool clear-expired demo" in out
        assert "deletes this project's expired records only; sends nothing" in out
        assert "Full details" not in out
        assert path.read_text() == "Review release\nFull details"
        assert _pending(spool_root) == []

    @pytest.mark.parametrize("scope", [[], ["beta"]])
    def test_multiple_projects_and_records(self, spool_root, capsys, scope):
        self._record(spool_root, "alpha", "1000-a.msg", "alpha one")
        self._record(spool_root, "alpha", "2000-b.msg", "alpha two")
        self._record(spool_root, "beta", "3000-c.msg", "beta one")
        ccm_spool.cmd_spool(["list", *scope])
        out = capsys.readouterr().out
        assert "beta:" in out and "beta one" in out
        if scope:
            assert "alpha" not in out
            assert out.count("send as NEW:") == 1
        else:
            assert "2 expired" in out and "1 expired" in out
            assert out.index("alpha one") < out.index("alpha two") < out.index("beta one")
            assert out.count("send as NEW:") == 3

    @pytest.mark.parametrize("failure", ["oserror", "unicode"])
    def test_unreadable_body_does_not_hide_other_records(
            self, spool_root, monkeypatch, capsys, failure):
        import builtins
        path = self._record(spool_root)
        self._record(spool_root, filename="2000000-next.msg", body="still visible")
        if failure == "unicode":
            path.write_bytes(b"\xff")
        else:
            original = builtins.open
            def unreadable(name, *a, **kw):
                if os.fspath(name) == str(path):
                    raise PermissionError("denied")
                return original(name, *a, **kw)
            monkeypatch.setattr(builtins, "open", unreadable)
        ccm_spool.cmd_spool(["list"])
        out = capsys.readouterr().out
        assert "from: origin" in out and "(unreadable)" in out
        assert "still visible" in out and out.count("Review full text:") == 2

    def test_nonstandard_filename_is_not_dropped(self, spool_root, capsys):
        self._record(spool_root, filename="unexpected.msg", body="retained evidence")
        ccm_spool.cmd_spool(["list"])
        out = capsys.readouterr().out
        assert "unexpected  (expired; from: unknown; queued: unknown)  retained evidence" in out
        assert "send as NEW:" in out

    def test_preview_skips_blanks_and_truncates(self, spool_root, capsys):
        self._record(spool_root, body="\n   \n" + "x" * 75 + "\nunshown tail")
        ccm_spool.cmd_spool(["list"])
        out = capsys.readouterr().out
        assert "x" * 60 + "..." in out
        assert "x" * 61 not in out and "unshown tail" not in out

    def test_quoted_commands_preserve_arguments(self, spool_root, monkeypatch, capsys):
        import shlex
        root = spool_root + " space's"
        monkeypatch.setattr(ccm_spool, "SPOOL_ROOT", root)
        project = "team's work"
        path = self._record(root, project, filename="odd ' name.msg")
        ccm_spool.cmd_spool(["list"])
        lines = capsys.readouterr().out.splitlines()
        review = next(line.split(": ", 1)[1] for line in lines if "Review full text:" in line)
        send = next(line.split(": ", 1)[1] for line in lines if "send as NEW:" in line)
        assert shlex.split(review) == ["cat", "--", str(path)]
        assert shlex.split(send) == ["ccm", "send", "--file", str(path), "--", project]

    def test_list_and_clear_do_not_send_or_change_other_records(
            self, spool_root, monkeypatch, capsys):
        from pathlib import Path
        def forbidden(*a, **kw):
            pytest.fail("inspection/acknowledgement attempted delivery")
        for module, names in (
                (ccm_send, ["cmd_send", "_type_body", "_send_keys"]),
                (ccm_spool, ["enqueue", "reconcile_spools"]),
                (ccm_core, ["tmux_cmd"])):
            for name in names:
                monkeypatch.setattr(module, name, forbidden)
        expired = self._record(spool_root)
        self._record(spool_root, "other")
        for sub in ("", "held", "delivered"):
            path = Path(spool_root) / "demo" / sub / "2000000-kept.msg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep me")
        def snapshot():
            return {str(p): p.read_bytes() for p in Path(spool_root).rglob("*") if p.is_file()}
        before = snapshot()
        ccm_spool.cmd_spool(["list"])
        out = capsys.readouterr().out
        assert "last seen waiting in the session's input box" in out
        assert "expired — never delivered" in out
        assert snapshot() == before
        ccm_spool.cmd_spool(["clear-expired", "demo"])
        del before[str(expired)]
        assert snapshot() == before

    def test_suggested_send_to_shell_is_new_and_preserves_evidence(
            self, spool_root, monkeypatch, capsys):
        import shlex
        path = self._record(spool_root)
        monkeypatch.setattr(ccm_spool.time, "time", lambda: 4660)
        ccm_spool.cmd_spool(["list"])
        line = next(l for l in capsys.readouterr().out.splitlines() if "send as NEW:" in l)
        args = shlex.split(line.split(": ", 1)[1])[2:]
        assert "--start" not in args and "--force" not in args and "-y" not in args
        helper = TestCmdSendSpooling()
        helper._patch_resolution(monkeypatch, spool_root, "SHELL")
        calls = []
        monkeypatch.setattr(ccm_core, "tmux_cmd", helper._tmux(calls))
        monkeypatch.setattr(ccm_send, "_sender_label", lambda: "current-sender")
        ccm_send.cmd_send(args)
        names = _pending(spool_root)
        assert names == ["4660000-current-sender.msg"]
        queued_path = os.path.join(spool_root, "demo", names[0])
        with open(queued_path) as f:
            assert f.read() == path.read_text()
        assert path.exists()
        assert not [call for call in calls if call[0] == "send-keys"]
        queued, sender = ccm_spool._parse_msg_name(names[0])
        envelope = ccm_spool._envelope(sender, queued, 4700)
        assert "current-sender" in envelope and "origin" not in envelope
