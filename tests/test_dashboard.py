"""Tests for dashboard.py — static/pure helpers that don't require curses."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import dashboard  # noqa: E402 — verify module imports cleanly
from dashboard import Dashboard


# ─── Module imports ───

class TestDashboardImports:
    """Smoke test: dashboard.py must import all the canary helpers
    it surfaces in the footer. Catches typos in the `from ccm_core
    import ...` block."""

    def test_canary_helpers_imported(self):
        assert hasattr(dashboard, "hooks_log_warning")
        assert hasattr(dashboard, "disable_all_hooks_warning")
        assert hasattr(dashboard, "managed_hooks_only_warning")
        assert hasattr(dashboard, "hooks_configured")


# ─── _strip_last_grapheme ───

class TestStripLastGrapheme:
    """Backspace should delete one user-perceived character (grapheme cluster)."""

    def test_ascii(self):
        assert Dashboard._strip_last_grapheme("abc") == "ab"

    def test_cjk(self):
        assert Dashboard._strip_last_grapheme("日本語") == "日本"

    def test_combining_mark(self):
        # é as e + combining acute accent (U+0301)
        assert Dashboard._strip_last_grapheme("cafe\u0301") == "caf"

    def test_multiple_combining_marks(self):
        # a + combining tilde + combining acute
        assert Dashboard._strip_last_grapheme("xa\u0303\u0301") == "x"

    def test_single_char(self):
        assert Dashboard._strip_last_grapheme("a") == ""

    def test_empty(self):
        assert Dashboard._strip_last_grapheme("") == ""

    def test_zwj_sequence(self):
        # 👨‍💻 = 👨 + ZWJ + 💻
        assert Dashboard._strip_last_grapheme("\U0001f468\u200d\U0001f4bb") == ""

    def test_zwj_after_text(self):
        assert Dashboard._strip_last_grapheme("x\U0001f468\u200d\U0001f4bb") == "x"


# ─── _display_width ───

class TestDisplayWidth:
    """Display width must account for wide (CJK) characters."""

    def test_ascii(self):
        assert Dashboard._display_width("hello") == 5

    def test_cjk(self):
        # Each CJK character is 2 columns
        assert Dashboard._display_width("日本語") == 6

    def test_mixed(self):
        assert Dashboard._display_width("ab日c") == 5  # 1+1+2+1

    def test_empty(self):
        assert Dashboard._display_width("") == 0


# ─── _truncate_to_width ───

class TestTruncateToWidth:
    def test_no_truncation_needed(self):
        assert Dashboard._truncate_to_width("abc", 10) == "abc"

    def test_truncate_ascii(self):
        assert Dashboard._truncate_to_width("abcdef", 3) == "abc"

    def test_truncate_cjk_boundary(self):
        # "日本語" = 6 cols; truncating to 5 should keep "日本" (4 cols)
        # because "日本語" needs 6 and "日本" + partial 語 won't fit
        result = Dashboard._truncate_to_width("日本語", 5)
        assert result == "日本"
        assert Dashboard._display_width(result) <= 5

    def test_truncate_cjk_exact(self):
        assert Dashboard._truncate_to_width("日本語", 6) == "日本語"
