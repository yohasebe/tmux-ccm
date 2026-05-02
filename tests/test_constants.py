"""Tests for ccm_constants — pure constants and the PERMIT-modal
classifier."""

import pytest

import ccm_constants


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
        assert "confirm" in guidance.lower()

    def test_confirmation_modal_footer_only(self):
        """No content signature matches but footer is the confirm
        shape — classify as a generic confirmation-modal (not unknown).
        Permission dialogs are caught by their own footer above, so
        what remains here is safe."""
        text = "Enter to confirm · Esc to quit"
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
