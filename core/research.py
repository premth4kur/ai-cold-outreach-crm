"""
core/research.py
────────────────
Website research + official email discovery.

Responsibilities (and hard limits)
-----------------------------------
* Visit the official website only. Fetch the homepage, then follow a few
  internal links that look like Contact / About / Team, plus scan the footer.
* Extract ONLY officially published business emails found on those pages
  (mailto: links or plain text). Never guess an address, never synthesise
  first.last@domain, never call Hunter / Apollo / ZoomInfo, never accept
  personal free-mail addresses (gmail/yahoo/…). If nothing official is
  published, the lead has no email and is routed to Social Outreach.
* Gather the cleaned page text as evidence for the AI observation step
  (Module 4). This module does NOT invent observations — it only collects
  verifiable material and scores how much of it we actually got.
* Score a research confidence in [0, 1]. Below the configured threshold the
  lead is routed to Needs Review so a human checks it before any send.
* Optional web-search context (SerpAPI) adds a few snippets of company
  background when enabled; it never substitutes for the real website.

The scraping helpers are written as pure functions so they can be unit-tested
against fixed HTML without hitting the network.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
import tldextract
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import settings

# ── Constants ───────────────────────────────────────────────────────
_USER_AGENT = (
    "Mozilla/5.0 (compatible; OutreachResearchBot/1.0; +https://premthakurr.framer.media/)"
)
_REQUEST_TIMEOUT = 15  # seconds
_MAX_INTERNAL_PAGES = 4  # homepage + up to this many discovered pages
_MAX_TEXT_CHARS = 6000   # cap evidence text handed to the AI (per lead)

# Internal links we care about, matched against href + link text.
_PAGE_KEYWORDS = ("contact", "about", "team", "company", "who-we-are", "get-in-touch")

# Free / personal mailbox providers — never treated as an official business email.
_FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "gmx.com", "zoho.com", "yandex.com",
    "mail.com", "msn.com",
}

# Emails that are almost always tracking/asset noise, not a contact address.
_JUNK_EMAIL_PREFIXES = ("noreply", "no-reply", "donotreply", "postmaster", "abuse", "mailer-daemon")
_JUNK_EMAIL_SUBSTRINGS = ("example.com", "domain.com", "sentry.io", "wixpress.com")

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


# ════════════════════════════════════════════════════════════════════
#  Result container
# ════════════════════════════════════════════════════════════════════
@dataclass
class ResearchEvidence:
    """
    Everything the scraper gathered for one lead. Consumed by the AI observation
    step (Module 4) and written to the RESEARCH tab by the orchestrator.
    """

    website: str
    reachable: bool = False
    pages_fetched: list[str] = field(default_factory=list)
    evidence_text: str = ""              # cleaned, truncated page text
    emails: list[str] = field(default_factory=list)   # all official emails found
    primary_email: str = ""              # best official email to contact
    search_context: str = ""             # optional web-search snippets
    confidence: float = 0.0
    timestamp: str = ""
    error: str = ""

    @property
    def has_official_email(self) -> bool:
        return bool(self.primary_email)


# ════════════════════════════════════════════════════════════════════
#  Pure helpers (unit-testable, no network)
# ════════════════════════════════════════════════════════════════════
def registered_domain(url_or_email: str) -> str:
    """Return the registrable domain, e.g. 'shop.acme.co.uk' -> 'acme.co.uk'."""
    host = url_or_email.split("@")[-1] if "@" in url_or_email else urlparse(
        url_or_email if "://" in url_or_email else "http://" + url_or_email
    ).netloc
    ext = tldextract.extract(host)
    return ".".join(p for p in (ext.domain, ext.suffix) if p).lower()


def is_free_email(addr: str) -> bool:
    return addr.split("@")[-1].lower() in _FREE_EMAIL_DOMAINS


def is_junk_email(addr: str) -> bool:
    local = addr.split("@")[0].lower()
    if any(local.startswith(p) for p in _JUNK_EMAIL_PREFIXES):
        return True
    return any(s in addr.lower() for s in _JUNK_EMAIL_SUBSTRINGS)


def extract_emails(html: str) -> list[str]:
    """
    Pull candidate emails from a page: mailto: hrefs first (highest signal),
    then any addresses appearing in visible text. De-duplicated, lower-cased,
    junk removed. Personal-provider filtering happens later against the site
    domain so we can still prefer domain-matched addresses.
    """
    found: list[str] = []
    soup = BeautifulSoup(html, "lxml")

    for a in soup.select('a[href^="mailto:"]'):
        href = a.get("href", "")
        addr = href[len("mailto:"):].split("?")[0].strip().lower()
        if _EMAIL_RE.fullmatch(addr):
            found.append(addr)

    for m in _EMAIL_RE.findall(soup.get_text(" ")):
        found.append(m.lower())

    # de-dup, drop obvious junk
    seen: list[str] = []
    for e in found:
        if e not in seen and not is_junk_email(e):
            seen.append(e)
    return seen


def choose_primary_email(emails: list[str], website: str) -> str:
    """
    Pick the best official business email:
      1. an address whose domain matches the website's registered domain,
         preferring role inboxes (contact@, hello@, info@, sales@),
      2. otherwise any non-free, domain-matched address,
      3. never a free/personal provider address.
    Returns "" if nothing qualifies (-> Social Outreach).
    """
    site_domain = registered_domain(website) if website else ""
    domain_matched = [e for e in emails if site_domain and registered_domain(e) == site_domain]

    role_order = ("contact@", "hello@", "info@", "sales@", "hi@", "team@", "office@")
    for role in role_order:
        for e in domain_matched:
            if e.startswith(role):
                return e

    if domain_matched:
        return domain_matched[0]

    # No domain match. Accept a non-free business address only if it isn't personal.
    non_free = [e for e in emails if not is_free_email(e)]
    return non_free[0] if non_free else ""


def discover_internal_links(base_url: str, html: str) -> list[str]:
    """Find same-domain Contact/About/Team style links to fetch next."""
    soup = BeautifulSoup(html, "lxml")
    base_domain = registered_domain(base_url)
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ").strip().lower()
        blob = (href + " " + text).lower()
        if not any(k in blob for k in _PAGE_KEYWORDS):
            continue
        absolute = urljoin(base_url, href)
        if not absolute.startswith("http"):
            continue
        if registered_domain(absolute) != base_domain:
            continue
        if absolute not in out and absolute.rstrip("/") != base_url.rstrip("/"):
            out.append(absolute)
    return out[:_MAX_INTERNAL_PAGES]


def clean_text(html: str) -> str:
    """Strip scripts/styles/nav noise and return readable page text."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def score_confidence(*, reachable: bool, text_len: int, num_pages: int, has_email: bool) -> float:
    """
    Heuristic confidence in [0, 1] for how much real material we gathered.
    Not a guess about content quality — a measure of evidence sufficiency, so a
    thin/JS-only site scores low and lands in Needs Review instead of getting a
    hallucinated observation.
    """
    if not reachable:
        return 0.0
    score = 0.0
    score += 0.30 if text_len >= 400 else (0.15 if text_len >= 120 else 0.0)
    score += min(num_pages, 3) * 0.12          # up to +0.36 for depth
    score += 0.22 if has_email else 0.0
    score += 0.10                               # base for a reachable site
    return round(min(score, 1.0), 2)


# ════════════════════════════════════════════════════════════════════
#  Researcher (network side)
# ════════════════════════════════════════════════════════════════════
class WebsiteResearcher:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    @retry(
        reraise=True,
        retry=retry_if_exception_type(requests.RequestException),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
    )
    def _fetch(self, url: str) -> str:
        resp = self._session.get(url, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            return ""
        return resp.text

    def research(self, website: str) -> ResearchEvidence:
        """Fetch + parse a lead's site and return gathered evidence."""
        ev = ResearchEvidence(website=website, timestamp=_now_iso())

        url = _normalize_url(website)
        if not url:
            ev.error = "No/invalid website"
            return ev

        try:
            home_html = self._fetch(url)
        except requests.RequestException as exc:
            ev.error = f"Homepage unreachable: {exc}"
            ev.confidence = 0.0
            return ev

        ev.reachable = True
        ev.pages_fetched.append(url)

        all_html = [home_html]
        all_emails = list(extract_emails(home_html))

        # Follow a few internal Contact/About/Team pages.
        for link in discover_internal_links(url, home_html):
            try:
                page_html = self._fetch(link)
            except requests.RequestException:
                continue
            if not page_html:
                continue
            ev.pages_fetched.append(link)
            all_html.append(page_html)
            for e in extract_emails(page_html):
                if e not in all_emails:
                    all_emails.append(e)

        # Build evidence text (truncated) and pick the contact email.
        combined = " ".join(clean_text(h) for h in all_html)
        ev.evidence_text = combined[:_MAX_TEXT_CHARS]
        ev.emails = all_emails
        ev.primary_email = choose_primary_email(all_emails, url)

        # Optional company background from web search.
        ev.search_context = self._search_context(website)

        ev.confidence = score_confidence(
            reachable=True,
            text_len=len(ev.evidence_text),
            num_pages=len(ev.pages_fetched),
            has_email=ev.has_official_email,
        )
        return ev

    # ── Optional web-search context (SerpAPI) ───────────────────────
    def _search_context(self, website: str) -> str:
        cfg = settings.web_search
        if not cfg.enabled:
            return ""
        try:
            domain = registered_domain(website)
            resp = self._session.get(
                "https://serpapi.com/search.json",
                params={"q": domain, "engine": "google", "num": 3, "api_key": cfg.serpapi_key},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            snippets = [
                r.get("snippet", "")
                for r in data.get("organic_results", [])[:3]
                if r.get("snippet")
            ]
            return " ".join(snippets)[:1500]
        except Exception:  # search is best-effort; never fail research over it
            return ""


# ── module-level helpers ────────────────────────────────────────────
def _normalize_url(website: str) -> str:
    w = (website or "").strip()
    if not w or "." not in w:
        return ""
    if not w.startswith(("http://", "https://")):
        w = "https://" + w
    return w


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
