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
        2026-05-13 against Claude Code v2.1.144."""
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
