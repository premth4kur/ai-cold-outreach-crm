"""
tests/test_research.py
───────────────────────
Offline tests for research parsing + official-email discovery. Uses fixed HTML,
no network. Proves the "official emails only, never personal, never guessed"
rule and the confidence routing.

Run:
    python -m unittest tests.test_research -v
"""

from __future__ import annotations

import unittest

from core.research import (
    choose_primary_email,
    discover_internal_links,
    extract_emails,
    is_free_email,
    is_junk_email,
    registered_domain,
    score_confidence,
)

HOME_HTML = """
<html><body>
  <nav><a href="/about">About us</a><a href="/contact">Contact</a>
       <a href="https://twitter.com/acme">Twitter</a></nav>
  <h1>Acme builds bespoke furniture</h1>
  <footer>
    Email us at <a href="mailto:hello@acme.com">hello@acme.com</a>
    or the founder personally: jane.personal@gmail.com
    <script>var x = 'noreply@acme.com';</script>
  </footer>
</body></html>
"""


class TestEmailRules(unittest.TestCase):
    def test_registered_domain(self):
        self.assertEqual(registered_domain("https://shop.acme.co.uk/x"), "acme.co.uk")
        self.assertEqual(registered_domain("hello@acme.com"), "acme.com")

    def test_free_and_junk(self):
        self.assertTrue(is_free_email("jane@gmail.com"))
        self.assertFalse(is_free_email("hello@acme.com"))
        self.assertTrue(is_junk_email("noreply@acme.com"))

    def test_extract_drops_junk(self):
        emails = extract_emails(HOME_HTML)
        self.assertIn("hello@acme.com", emails)
        self.assertIn("jane.personal@gmail.com", emails)
        self.assertNotIn("noreply@acme.com", emails)  # junk prefix removed

    def test_primary_prefers_domain_role_inbox(self):
        emails = extract_emails(HOME_HTML)
        primary = choose_primary_email(emails, "https://acme.com")
        self.assertEqual(primary, "hello@acme.com")  # never the gmail address

    def test_no_official_email_returns_empty(self):
        # Only a personal address published -> no official email -> Social Outreach.
        emails = ["jane.personal@gmail.com"]
        self.assertEqual(choose_primary_email(emails, "https://acme.com"), "")


class TestLinkDiscovery(unittest.TestCase):
    def test_internal_links_same_domain_only(self):
        links = discover_internal_links("https://acme.com", HOME_HTML)
        self.assertTrue(any(l.endswith("/about") for l in links))
        self.assertTrue(any(l.endswith("/contact") for l in links))
        self.assertFalse(any("twitter.com" in l for l in links))


class TestConfidence(unittest.TestCase):
    def test_unreachable_is_zero(self):
        self.assertEqual(score_confidence(reachable=False, text_len=999, num_pages=3, has_email=True), 0.0)

    def test_rich_site_scores_high(self):
        c = score_confidence(reachable=True, text_len=1200, num_pages=3, has_email=True)
        self.assertGreaterEqual(c, 0.55)

    def test_thin_site_scores_low(self):
        c = score_confidence(reachable=True, text_len=50, num_pages=1, has_email=False)
        self.assertLess(c, 0.55)


if __name__ == "__main__":
    unittest.main(verbosity=2)
