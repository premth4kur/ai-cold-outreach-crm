"""
core/dedupe.py
──────────────
Duplicate detection. A lead is a duplicate if either:

  1. its email already exists on another lead, OR
  2. its (company + website) pair already exists on another lead.

Why both rules
--------------
Email is the strongest signal, but many imported leads arrive without an email
(those go to Social Outreach). Company+website catches the same organisation
imported twice under different contacts or on different days, so we never email
the same business twice.

The matcher builds a normalized in-memory index from the leads already in the
CRM, then classifies each candidate. Normalization (lowercasing, trimming,
stripping URL scheme/`www`/trailing slash) means "Acme, Inc." at
`https://Acme.com/` matches "acme inc" at `acme.com`.

This module is pure logic — it never touches the network or the sheet. The
orchestrator feeds it lead data and acts on the verdict, which keeps it trivial
to unit-test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_website(website: str) -> str:
    """
    Reduce a URL to a comparable host+path key:
    strip scheme, leading www, and trailing slash; lowercase.
    'https://www.Acme.com/' -> 'acme.com'
    """
    w = (website or "").strip().lower()
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    w = w.rstrip("/")
    return w


def normalize_company(company: str) -> str:
    """
    Lowercase, collapse whitespace, and drop common legal suffixes and
    punctuation so 'Acme, Inc.' == 'acme inc' == 'ACME  Incorporated'.
    """
    c = (company or "").strip().lower()
    c = re.sub(r"[.,]", " ", c)
    c = re.sub(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|gmbh|pvt|private)\b", " ", c)
    c = re.sub(r"\s+", " ", c).strip()
    return c


def _company_website_key(company: str, website: str) -> str:
    return f"{normalize_company(company)}|{normalize_website(website)}"


@dataclass
class DuplicateVerdict:
    """Result of classifying one candidate lead."""

    is_duplicate: bool
    reason: str = ""  # "email" | "company+website" | ""

    def __bool__(self) -> bool:  # allow `if verdict:`
        return self.is_duplicate


@dataclass
class DuplicateIndex:
    """
    In-memory index of leads already present in the CRM. Build it once per run
    from the existing sheet rows, then classify each candidate against it.

    As you accept new (non-duplicate) leads during a run, call `add()` so that a
    duplicate appearing twice *within the same import batch* is also caught.
    """

    _emails: set[str] = field(default_factory=set)
    _company_sites: set[str] = field(default_factory=set)

    @classmethod
    def from_rows(cls, rows: list[dict]) -> "DuplicateIndex":
        """
        Build from raw lead dicts (keys: 'Email', 'Company', 'Website').
        Rows already marked Duplicate are still indexed — they represent real
        prior occurrences, so re-imports keep matching them.
        """
        idx = cls()
        for r in rows:
            idx.add(
                email=str(r.get("Email", "")),
                company=str(r.get("Company", "")),
                website=str(r.get("Website", "")),
            )
        return idx

    def add(self, email: str, company: str, website: str) -> None:
        e = normalize_email(email)
        if e:
            self._emails.add(e)
        key = _company_website_key(company, website)
        if key.strip("|"):  # ignore empty|empty
            self._company_sites.add(key)

    def classify(self, email: str, company: str, website: str) -> DuplicateVerdict:
        """
        Decide whether a candidate is a duplicate of anything already indexed.
        Email match takes precedence (stronger signal) in the reported reason.
        """
        e = normalize_email(email)
        if e and e in self._emails:
            return DuplicateVerdict(True, "email")

        key = _company_website_key(company, website)
        if key.strip("|") and key in self._company_sites:
            return DuplicateVerdict(True, "company+website")

        return DuplicateVerdict(False)
