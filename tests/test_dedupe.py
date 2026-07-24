"""
tests/test_dedupe.py
────────────────────
Offline unit tests for duplicate detection. No network, no Google Sheets — pure
logic, so you can run this immediately to prove Module 2's dedupe works.

Run:
    python -m unittest tests.test_dedupe -v
"""

from __future__ import annotations

import unittest

from core.dedupe import (
    DuplicateIndex,
    normalize_company,
    normalize_email,
    normalize_website,
)


class TestNormalization(unittest.TestCase):
    def test_email(self):
        self.assertEqual(normalize_email("  Hello@Acme.COM "), "hello@acme.com")

    def test_website(self):
        self.assertEqual(normalize_website("https://www.Acme.com/"), "acme.com")
        self.assertEqual(normalize_website("http://acme.com"), "acme.com")

    def test_company(self):
        self.assertEqual(normalize_company("Acme, Inc."), "acme")
        self.assertEqual(normalize_company("ACME  Incorporated"), "acme")


class TestDuplicateIndex(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"Email": "jane@acme.com", "Company": "Acme, Inc.", "Website": "https://acme.com"},
            {"Email": "", "Company": "Globex LLC", "Website": "www.globex.com"},
        ]
        self.idx = DuplicateIndex.from_rows(self.rows)

    def test_email_duplicate(self):
        v = self.idx.classify("JANE@acme.com", "Different Name", "other.com")
        self.assertTrue(v.is_duplicate)
        self.assertEqual(v.reason, "email")

    def test_company_website_duplicate(self):
        # Same company + site, no matching email -> caught by second rule.
        v = self.idx.classify("newperson@acme.com".replace("acme", "zzz"), "acme inc", "http://www.acme.com/")
        self.assertTrue(v.is_duplicate)
        self.assertEqual(v.reason, "company+website")

    def test_not_duplicate(self):
        v = self.idx.classify("brand@new.com", "Fresh Co", "fresh.io")
        self.assertFalse(v.is_duplicate)

    def test_within_batch_dedup(self):
        # A lead accepted mid-run should block its own re-appearance.
        self.assertFalse(self.idx.classify("x@x.com", "New Biz", "newbiz.com").is_duplicate)
        self.idx.add("x@x.com", "New Biz", "newbiz.com")
        self.assertTrue(self.idx.classify("x@x.com", "New Biz", "newbiz.com").is_duplicate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
