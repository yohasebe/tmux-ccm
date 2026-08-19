"""Tests for ccm_constants — pure constants and the PERMIT-modal
classifier."""

import os
import re

import pytest

import ccm_constants


class TestCCMVersionConsistency:
    """`CCM_VERSION` is hand-mirrored across three files (bash
    wrapper, Python constants, CHANGELOG header). Drift between
    them would mean `ccm --version` and `ccm doctor` report
    different things, or the released version doesn't match what
    CHANGELOG documents. This test fails fast if any pair
    disagrees, so the only way to ship a release is to bump all
    three together."""

    def test_python_matches_bash(self):
        ccm_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bash_path = os.path.join(ccm_root, "ccm")
        with open(bash_path, encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'^CCM_VERSION="([^"]+)"', content, re.MULTILINE)
        assert m is not None, "CCM_VERSION not found in ccm bash wrapper"
        assert m.group(1) == ccm_constants.CCM_VERSION, (
            f"bash CCM_VERSION={m.group(1)} but Python CCM_VERSION="
            f"{ccm_constants.CCM_VERSION} — bump both together"
        )

    def test_python_matches_changelog_top_entry(self):
        # The top released entry — `[Unreleased]` is the staging
        # ground for fixes accumulating toward the next patch and
        # is skipped here.
        ccm_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        changelog_path = os.path.join(ccm_root, "CHANGELOG.md")
        top_version = None
        with open(changelog_path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^## \[([^\]]+)\]", line)
                if m and m.group(1) != "Unreleased":
                    top_version = m.group(1)
                    break
        if top_version is None:
            pytest.fail("No released version entry found in CHANGELOG.md")
        assert top_version == ccm_constants.CCM_VERSION, (
            f"CHANGELOG top released entry [{top_version}] but Python "
            f"CCM_VERSION={ccm_constants.CCM_VERSION} — bump both together"
        )


class TestClassifyPermitModal:
    """Unit tests for the pure PERMIT-modal classifier.

    Each case feeds a minimal captured-pane excerpt and asserts the
    returned category. The guidance string is checked only for shape
    (non-empty, mentions the category's key noun) to avoid locking
    tests to exact wording.
    """

    def test_session_resume_modal(self):
        text = (
            "This session is 2h 15m old and 43k tokens.\n"
            "1. Resume from summary (recommended)\n"
            "2. Resume full session as-is\n"
            "3. Don't ask me again\n"
            "Enter to confirm · Esc to cancel"
        )
        cat, guidance = ccm_constants.classify_permit_modal(text)
        assert cat == "session-resume"
        assert "Resume" in guidance or "resume" in guidance

    @pytest.mark.parametrize("age", ["45m", "2h 15m", "2d 4h", "1d"])
    def test_session_resume_age_takes_any_units(self, age):
        """The age line carries whatever units it needs. Requiring a
        fixed `\\d+h \\d+m` pair excluded every session under the hour,
        and `--continue` also resumes ones days old. Found while fixing
        the same mistake in PATTERN_ACTIVE_SPINNER, where
        an hours component silently stopped the match.

        The age line alone must classify: the recommended-summary line
        that would otherwise carry it is dropped here on purpose."""
        text = (
            f"This session is {age} old and 43k tokens.\n"
            "1. Resume where it left off\n"
            "Enter to confirm · Esc to cancel"
        )
        cat, _guidance = ccm_constants.classify_permit_modal(text)
        assert cat == "session-resume"

    def test_permission_request_tab_to_amend(self):
        text = (
            "Do you want to proceed?\n"
            "Run `rm -rf /tmp/x`\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "Esc to cancel · Tab to amend"
        )
        cat, guidance = ccm_constants.classify_permit_modal(text)
        assert cat == "permission-request"
        assert "DANGEROUS" in guidance or "dangerous" in guidance

    def test_permission_request_ctrl_e_footer_only(self):
        """Footer alone (no 'Do you want to proceed?' line) should still
        be classified as permission-request — the alt ctrl+e footer is
        a tool-permission signature that must never fall through as a
        safe confirmation-modal."""
        text = "Esc to cancel · ctrl+e to explain"
        cat, _ = ccm_constants.classify_permit_modal(text)
        assert cat == "permission-request"

    def test_confirmation_modal_model_picker(self):
        text = (
            "Switch between Claude models\n"
            "1. claude-sonnet-4-6\n"
            "2. claude-opus-4-7\n"
            "Enter to confirm · Esc to exit"
        )
        cat, guidance = ccm_constants.classify_permit_modal(text)
        assert cat == "confirmation-modal"
        assert "confirm" in guidance.lower() or "modal" in guidance.lower()

    def test_confirmation_modal_model_picker_v2_1_153(self):
        """v2.1.153 reworded the `/model` footer (`Enter to set as
        default` instead of `Enter to confirm`). PATTERN_MODEL_PICKER
        still matches the `Select model` / `Switch between Claude
        models` content lines, so as long as PATTERN_PERMIT_FOOTER
        accepts the new verb the classifier resolves correctly."""
        text = (
            "Select model\n"
            "Switch between Claude models. Your pick becomes the default for new sessions.\n"
            "❯ 1. Default (recommended) ✔  Opus 4.7 with 1M context\n"
            "  2. Sonnet                   Sonnet 4.6\n"
            "Enter to set as default · s to use this session only · Esc to cancel"
        )
        cat, _ = ccm_constants.classify_permit_modal(text)
        assert cat == "confirmation-modal"

    def test_ask_user_question_menu_footer_measured_2026_07_26(self):
        """Verbatim footer of a live `AskUserQuestion` choice menu,
        captured from a real pane on (v2.1.220).

        This one is load-bearing beyond classification: the PERMIT
        staleness guard in `ccm_activity.map_activity_to_state`
        releases a stale permit only when `raw == "IDLE"`, and its
        safety argument is that a menu still awaiting a selection
        shows this footer and therefore arrives as raw=PERMIT. If
        upstream rewords it past `PATTERN_PERMIT_FOOTER`, a menu left
        open longer than `PERMIT_MAX_TIMEOUT` would silently read as
        IDLE — the exact false-IDLE this test exists to catch. Note
        the verb is `select`, not `confirm`: the pattern must stay on
        its structural `Enter to \\S… · …Esc to \\w+` branch."""
        footer = ("Enter to select · ↑/↓ to navigate · n to add notes "
                  "· Esc to cancel")
        assert ccm_constants.PATTERN_PERMIT_FOOTER.search(footer)
        # As it appears in a captured pane: options above, footer last.
        text = (
            "  Which approach?\n"
            "❯ 1. First option\n"
            "  2. Second option\n"
            "  Chat about this\n"
            + footer
        )
        assert ccm_constants.PATTERN_PERMIT_FOOTER.search(text)

    def test_idle_composer_is_not_a_permit_footer(self):
        """The other half of the measurement: the same pane with no
        modal (empty `❯` composer + status line) must NOT match, or
        every idle session would read as PERMIT. Captured alongside
        the menu above."""
        text = (
            "  ⎿  Tip: Use Plan Mode to prepare for a complex request.\n"
            "❯ \n"
            "  ~/code/ccm  main  Opus 5  ctx ████░░░░░░ 42%\n"
            "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents"
        )
        assert not ccm_constants.PATTERN_PERMIT_FOOTER.search(text)

    def test_footerless_webfetch_permission_is_permission_request(self):
        """Footer-less WebFetch / web-content permission dialog
        (observed, raised by a background subagent). It
        must classify as the DANGEROUS permission-request kind, not a
        safe confirmation-modal — `ccm send` warns the operator not
        to dismiss a permission prompt from another pane, and that
        guidance would be wrong if this were called a confirmation
        modal. Both the `Do you want to allow Claude to …` question
        and the deny-option line carry it to permission-request."""
        text = (
            "Do you want to allow Claude to fetch this content?\n"
            "❯ 1. Yes\n"
            "  2. Yes, and don't ask again for www.example.com\n"
            "  3. No, and tell Claude what to do differently (esc)"
        )
        cat, guidance = ccm_constants.classify_permit_modal(text)
        assert cat == "permission-request"
        assert "DANGEROUS" in guidance or "dangerous" in guidance

    def test_browser_dialog_is_a_permission_request(self):
        """The Claude-in-Chrome dialog carries no `Do you want to …`
        question, so the deny line alone carries its classification —
        and the classifier uses its own regex object. The shared
        constant keeps the two in lockstep; this pins it with the full
        dialog, cursor included."""
        text = (
            "Claude in Chrome wants to navigate on www.example.org\n"
            "❯ 1. Allow\n"
            "  2. Allow all actions on www.example.org for this session\n"
            "  3. Deny (esc)"
        )
        cat, guidance = ccm_constants.classify_permit_modal(text)
        assert cat == "permission-request"

    def test_folder_trust_prompt_is_a_permission_request(self):
        """The first-run folder-trust prompt carries the same
        `Enter to confirm · Esc to cancel` footer a safe picker uses,
        so without a content signature it read as harmless — and the
        guidance would have invited dismissing it from another pane.
        Answering it grants read, edit and execute in that
        directory."""
        text = (
            " Quick safety check: Is this a project you created or one "
            "you trust?\n"
            " \u276f 1. Yes, I trust this folder\n"
            "   2. No, exit\n"
            " Enter to confirm \u00b7 Esc to cancel"
        )
        cat, guidance = ccm_constants.classify_permit_modal(text)
        assert cat == "permission-request"

    def test_trust_signature_does_not_catch_the_safe_pickers(self):
        for text in ("Switch between Claude models\nEnter to confirm \u00b7 Esc to cancel",
                     "This session is 3h 11m old\nEnter to confirm \u00b7 Esc to cancel"):
            assert ccm_constants.classify_permit_modal(text)[0] != "permission-request"

    def test_confirmation_modal_footer_only(self):
        """No content signature matches but footer is the confirm
        shape — classify as a generic confirmation-modal (not unknown).
        Permission dialogs are caught by their own footer above, so
        what remains here is safe."""
        text = "Enter to confirm · Esc to quit"
        cat, _ = ccm_constants.classify_permit_modal(text)
        assert cat == "confirmation-modal"

    def test_confirmation_modal_footer_on_last_line_of_tail(self):
        """The realistic shape: classify_permit_modal receives the
        whole captured tail (multiple lines joined with newlines) and
        the footer sits on the LAST line, not at position 0. Without
        re.MULTILINE on PATTERN_PERMIT_FOOTER the `^` anchor only
        matched the start of the joined string, so this fallback
        branch silently never fired and a not-yet-cataloged confirm
        modal fell through to unknown-permit — surfacing the scary
        "Treat as dangerous" guidance for a harmless dialog."""
        text = (
            "Some new modal ccm has no content signature for\n"
            "❯ 1. Option A\n"
            "  2. Option B\n"
            "Enter to select · Esc to dismiss"
        )
        cat, _ = ccm_constants.classify_permit_modal(text)
        assert cat == "confirmation-modal"

    def test_unknown_permit_no_signature(self):
        """PERMIT was detected by the state engine but none of our
        known modal signatures are present — could be a new Claude
        Code modal. Classifier must flag it for conservative handling."""
        text = "some unrelated screen\nfoo bar baz"
        cat, guidance = ccm_constants.classify_permit_modal(text)
        assert cat == "unknown-permit"
        assert guidance  # non-empty guidance

    def test_permission_dialog_wins_over_resume_signature(self):
        """Defensive: if both a resume-like and a permission-like line
        happen to appear in the tail (unlikely, but the classifier
        should prefer the more dangerous classification)."""
        text = (
            "Resume from summary (recommended)\n"
            "Do you want to proceed?\n"
            "Esc to cancel · Tab to amend"
        )
        cat, _ = ccm_constants.classify_permit_modal(text)
        assert cat == "permission-request"


class TestPermitFooterPattern:
    """Direct regex tests for `PATTERN_PERMIT_FOOTER`. This is the
    primary gate that promotes a captured pane to PERMIT state, so
    every observed Claude Code modal footer shape must match here —
    and every slash-menu / non-blocking footer must NOT, since
    matching would cause `ccm send` to refuse and the user to see
    spurious PERMIT indicators on a screen they can freely close."""

    def _matches(self, text):
        return bool(ccm_constants.PATTERN_PERMIT_FOOTER.match(text))

    # ── must match (decision-blocking modals) ──

    def test_permission_dialog_tab_to_amend(self):
        assert self._matches("Esc to cancel · Tab to amend")

    def test_permission_dialog_ctrl_e_to_explain(self):
        assert self._matches("Esc to cancel · ctrl+e to explain")

    def test_confirm_modal_pre_v2_1_144(self):
        """Original `/model` / session-resume confirm footer."""
        assert self._matches("Enter to confirm · Esc to cancel")
        assert self._matches("Enter to confirm · Esc to exit")

    def test_confirm_modal_v2_1_144_model_picker(self):
        """v2.1.144 added a `d to set as default` action key between
        `Enter to confirm` and `Esc to cancel` on `/model`. Without
        the intermediate-segment tolerance the pre-existing regex
        would have silently dropped this footer — `ccm send` would
        then deliver keystrokes into the open picker and could
        accidentally confirm a model change. Verified empirically
 against Claude Code v2.1.144."""
        assert self._matches(
            "Enter to confirm · d to set as default for new sessions · Esc to cancel"
        )

    def test_confirm_modal_v2_1_153_model_picker(self):
        """v2.1.153 reworded the `/model` footer: the Enter verb is
        now `set as default` (formerly `confirm`) and the alternate
        action is `s to use this session only`. The pre-v2.1.153
        regex hard-coded `Enter to confirm` and would silently
        drop this footer, leaving `/model` undetected (IDLE) while
        the user is actually blocked at the picker. Verified
        empirically against Claude Code v2.1.153."""
        assert self._matches(
            "Enter to set as default · s to use this session only · Esc to cancel"
        )

    def test_confirm_modal_with_pipe_separator(self):
        """`|` separator variant — observed historically as an
        alternative to `·` on some terminals/themes."""
        assert self._matches("Enter to confirm | Esc to cancel")

    def test_footerless_permission_dialog_deny_option(self):
        """Footer-less permission dialog (observed on a
        WebFetch permission raised by a background subagent):
            Do you want to allow Claude to fetch this content?
            ❯ 1. Yes
              2. Yes, and don't ask again for www.example.com
              3. No, and tell Claude what to do differently (esc)
        There is NO `Esc to cancel · Tab to amend` footer here — the
        `(esc)` is inline on the deny option. The deny-option line is
        the PERMIT signal. ccm showed IDLE for this blocking dialog
        until the deny-option alternative was added; verified live
        against the paused dialog."""
        assert self._matches(
            "  3. No, and tell Claude what to do differently (esc)"
        )

    def test_deny_option_other_numbers_and_spacing(self):
        """The deny option is not always number 3, and spacing
        varies — match any `<n>. No, and tell Claude what to do
        differently … (esc)` line. The trailing `(esc)` is required
        (it disambiguates the live dialog from prose; see
        test_deny_phrase_in_prose_not_matched)."""
        assert self._matches("2. No, and tell Claude what to do differently (esc)")
        assert self._matches("   4.  No,  and tell Claude what to do differently  (esc)")

    def test_browser_dialog_deny_option(self):
        """The Claude-in-Chrome navigation dialog is footer-less and
        its deny line is just `Deny (esc)`:
            Claude in Chrome wants to navigate on www.example.org
            ❯ 1. Allow
              2. Allow all actions on www.example.org for this session
              3. Deny (esc)
        The old alternative fixed the deny label to one dialog's
        wording, so this one read as no-match — hook path only, and
        BUSY/IDLE whenever hooks are silent. Verified against the live
        dialog. The label is matched as a negative word, not a
        wording."""
        assert self._matches("  3. Deny (esc)")

    def test_decline_label_also_matches(self):
        """Same class: any negative-word deny label with the inline
        `(esc)` is the signal, before upstream renames it again."""
        assert self._matches("  2. Decline (esc)")

    def test_cursor_on_the_deny_line_still_matches(self):
        """Arrowing onto the deny option rewrites the line as
        `❯ 3. Deny (esc)`. Without the optional cursor prefix the
        footer match dropped out at exactly that moment — and the line
        fell through to PATTERN_INPUT_PROMPT, reading as an idle
        prompt while the dialog was open. Review finding."""
        assert self._matches("❯ 3. Deny (esc)")
        assert self._matches("❯ 2. No, and tell Claude what to do differently (esc)")

    def test_cursor_on_an_accept_line_does_not_match(self):
        assert not self._matches("❯ 1. Allow")
        assert not self._matches("❯ 1. Yes")

    def test_negative_word_needs_its_own_boundary(self):
        """`Note …` and `Denying …` must not ride on the No/Deny
        prefixes."""
        assert not self._matches("  3. Note the setting (esc)")
        assert not self._matches("  3. Denying access (esc)")

    def test_deny_option_without_inline_esc_not_matched(self):
        """A numbered deny line WITHOUT the inline `(esc)` does not
        match this alternative — a footer-less dialog always carries
        the inline `(esc)`, and requiring it is what keeps a Claude
        response that quotes the option text (a numbered list in
        prose) from false-triggering PERMIT. Footer'd dialogs, whose
        deny option lacks the inline `(esc)`, are matched by the
        `Esc to cancel · …` alternative instead, so nothing is lost."""
        assert not self._matches("3. No, and tell Claude what to do differently")

    # ── must NOT match (free-navigation menus) ──

    def test_bare_esc_to_cancel(self):
        """Slash menus (`/hooks`, `/config`, etc.) with footer
        `Esc to cancel` alone are free navigation — matching them
        would trap the user in PERMIT until they Esc'd."""
        assert not self._matches("Esc to cancel")

    def test_slash_menu_with_type_to_search(self):
        """`/skills` v2.1.121+ — multiple action keys + Esc, free nav."""
        assert not self._matches(
            "Enter to use, / to search, t to sort, Esc to close"
        )

    def test_resume_picker_v2_1_144(self):
        """v2.1.144 reformatted `/resume` from a confirm modal into
        a slash-menu-style picker. The footer has neither `Enter to
        confirm` prefix nor the permission-dialog keys, so it
        correctly does NOT match — the picker is browseable without
        committing, and treating it as PERMIT would block `ccm send`
        whenever a user opened it in another pane."""
        assert not self._matches(
            "Ctrl+A to show all projects · Ctrl+B to only show current "
            "branch · Space to preview · Ctrl+R to rename · Type to "
            "search · Esc to cancel"
        )

    def test_response_body_text_not_at_line_start(self):
        """Defensive: long-form Claude responses may legitimately
        contain the phrases `Enter to confirm` and `Esc to exit` in
        explanatory text. The line-start anchor (`^\\s*`) keeps
        these from triggering false PERMITs."""
        assert not self._matches(
            "documented: press Enter to confirm and then Esc to exit"
        )

    def test_deny_phrase_in_prose_not_matched(self):
        """Defensive for the deny-option signature: Claude could
        mention the phrase in answer text (e.g. explaining the
        permission UI). Two guards keep prose from false-triggering
        PERMIT: the leading `<n>.` numbered-option prefix AND the
        trailing inline `(esc)`. A numbered list in prose that quotes
        the option but continues past it (no `(esc)` at the end) is
        the realistic false-positive vector — including THIS very
        conversation about the detector — and must not match."""
        assert not self._matches(
            "you can choose No, and tell Claude what to do differently"
        )
        assert not self._matches(
            "the option reads 'No, and tell Claude what to do differently'"
        )
        # A numbered list line in prose: has the `<n>.` prefix and the
        # phrase, but trails off in explanation rather than ending at
        # an inline `(esc)`. Must stay IDLE.
        assert not self._matches(
            "3. No, and tell Claude what to do differently is the deny option"
        )
        assert not self._matches(
            "  3. No, and tell Claude what to do differently — this dismisses it"
        )


class TestAgentsTUIDetection:
    """`is_agents_tui` decides whether `ccm send` should refuse the
    target pane. Misclassification has real safety cost on either
    side — false positive blocks legitimate sends, false negative
    silently dispatches an unintended `claude agents` session."""

    def _detect(self, text):
        return ccm_constants.is_agents_tui(text)

    def test_canonical_agents_footer(self):
        """The v2.1.139+ TUI footer ccm refuses against."""
        text = (
            "  session a · idle  · 2m\n"
            "  session b · working · 14s\n"
            "❯ \n"
            "enter to open · space to reply · ctrl+x to delete · ? for shortcuts"
        )
        assert self._detect(text)

    def test_case_insensitive(self):
        """Upstream tweaks capitalization periodically; the matcher
        is IGNORECASE so we don't have to chase wording drift."""
        assert self._detect("Enter to open · ? for shortcuts")
        assert self._detect("ENTER TO OPEN something FOR SHORTCUTS")

    def test_empty_and_none(self):
        assert not self._detect("")
        assert not self._detect(None)

    def test_regular_claude_repl_not_matched(self):
        """A normal claude --continue REPL with an input prompt must
        NOT be classified as TUI — otherwise `ccm send` would refuse
        for every project, breaking the headline cross-project
        messaging feature."""
        text = (
            "Some response text from claude.\n"
            "❯ \n"
            "──────────────────────────────────────────\n"
            "  ~/code/foo  main  Opus 4.7  ██░░░░ 22%\n"
            "  ⏵⏵ accept edits on (shift+tab to cycle)"
        )
        assert not self._detect(text)

    def test_permit_footer_not_matched(self):
        """PERMIT footers (`Esc to cancel · Tab to amend` etc.) are
        a separate concept handled by PATTERN_PERMIT_FOOTER. Make
        sure they don't accidentally classify as agents TUI."""
        assert not self._detect("Esc to cancel · Tab to amend")
        assert not self._detect("Enter to confirm · Esc to cancel")

    def test_body_text_with_phrases_not_at_line_start(self):
        """Defensive: claude could mention these phrases in answer
        text. The line-start MULTILINE anchor keeps body text from
        false-tripping the matcher — only the actual footer line
        at column 0 (after optional whitespace) qualifies."""
        text = (
            "I see you have `enter to open` mapped in your TUI; the "
            "documentation says `? for shortcuts` shows the help."
        )
        assert not self._detect(text)


class TestStaleReleaseWindows:
    """The BUSY and PERMIT stale-release windows were split apart so
    they could be tuned independently. The behaviour
    tests around them are written boundary-relative — `W ± 1` — which
    correctly exercises the mechanism but adapts to whatever the
    constant says, so none of them would notice the BUSY window being
    put back to 600 s and the Esc-interrupt false BUSY returning.
    These pin the relationships the split exists to express."""

    def test_busy_release_is_shorter_than_the_shared_window(self):
        """The whole point of the split: an Esc-interrupted turn must
        escape a false BUSY well before the 10-minute window that
        still governs the promotion and combined-stale paths."""
        assert (ccm_constants.BUSY_STALE_RELEASE_SEC
                < ccm_constants.BUSY_HOOK_JSONL_WINDOW)

    def test_busy_release_is_far_below_the_auto_exit_timeout(self):
        """The release window is only flicker prevention; the real
        safety net against acting on a wrong IDLE is auto-exit's
        requirement of sustained IDLE. Keeping a wide margin between
        them is what makes a short release window defensible — worst
        case, a silent tool with broken spinner detection still needs
        `BUSY_STALE_RELEASE_SEC + IDLE_EXIT_TIMEOUT` of silence before
        anything is killed."""
        assert (ccm_constants.BUSY_STALE_RELEASE_SEC * 5
                <= ccm_constants.IDLE_EXIT_TIMEOUT)

    def test_permit_window_is_unchanged_by_the_split(self):
        """PERMIT keeps the 10-minute window: a modal on screen is
        re-committed by the raw override regardless of age, so the
        window only governs the already-resolved case, where being
        slow is harmless."""
        assert ccm_constants.PERMIT_MAX_TIMEOUT == 600
