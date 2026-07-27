"""
core/validator.py
─────────────────
The hard gate before any email is sent. If validation fails, the orchestrator
does NOT send — the lead is held (Needs Review) with the reasons logged.

Checks (from the spec)
----------------------
- an official email exists to send to
- personalization evidence exists
- a genuine observation exists
- a subject line exists
- every email body is < the configured word cap (default 130)
- the portfolio link is present
- the booking link is present
- no banned agency terms / obvious fake-stat language
- (duplicate + SMTP-connected are enforced elsewhere: dedupe at intake, SMTP at
  send time — this validator focuses on content correctness.)

Pure and side-effect free: give it the pieces, get back a pass/fail with a list
of human-readable reasons. Trivial to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import settings
from core.email_generator import GeneratedEmails, ResearchAnalysis, contains_banned_terms, word_count


@dataclass
class ValidationResult:
    ok: bool
    failures: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    def reason(self) -> str:
        return "; ".join(self.failures)


def validate_before_send(
    *,
    recipient_email: str,
    analysis: ResearchAnalysis,
    emails: GeneratedEmails,
    require_grounding: bool = True,
) -> ValidationResult:
    """
    `require_grounding=True` (strict) enforces that a genuine observation and
    personalization evidence exist. In auto-send mode it's set False, so the
    email still ships as long as the hard gates pass (recipient, subject, links,
    word count, no banned terms).
    """
    failures: list[str] = []
    cap = settings.behaviour.max_email_words
    portfolio = settings.outreach.portfolio_url
    booking = settings.outreach.booking_url

    # 1. deliverable target
    if not recipient_email.strip():
        failures.append("No official recipient email.")

    # 2. grounding (skipped in auto-send mode)
    if require_grounding:
        if not analysis.observation.strip():
            failures.append("Missing genuine observation.")
        if not analysis.personalization_evidence.strip():
            failures.append("Missing personalization evidence.")

    # 3. subject
    if not emails.subject.strip():
        failures.append("Missing subject line.")

    # 4. per-email content checks
    for label, body in (
        ("Email 1", emails.email_1),
        ("Follow-up 1", emails.followup_1),
        ("Follow-up 2", emails.followup_2),
    ):
        if not body.strip():
            failures.append(f"{label} is empty.")
            continue
        wc = word_count(body)
        if wc >= cap:
            failures.append(f"{label} is {wc} words (max {cap - 1}).")
        if portfolio not in body:
            failures.append(f"{label} missing portfolio link.")
        if booking not in body:
            failures.append(f"{label} missing booking link.")
        banned = contains_banned_terms(body)
        if banned:
            failures.append(f"{label} contains banned term(s): {', '.join(banned)}.")

    return ValidationResult(ok=not failures, failures=failures)
