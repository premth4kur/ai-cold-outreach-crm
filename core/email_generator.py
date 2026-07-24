"""
core/email_generator.py
────────────────────────
Turns research evidence into (1) a grounded analysis and (2) the outreach copy.

Two AI steps, deliberately separated
------------------------------------
1. analyze_research(evidence) -> ResearchAnalysis
   Reads ONLY the scraped evidence text and produces a website summary, ONE
   genuine observation, one opportunity, and the exact personalization evidence
   it relied on. The prompt forbids inventing anything not present in the
   evidence; if the material is too thin, it must say so (low self-confidence),
   which the orchestrator treats as Needs Review.

2. write_emails(lead, analysis) -> GeneratedEmails
   Writes Subject + Email 1 + Follow-up 1 + Follow-up 2. Hard rules baked into
   the prompt: ≤130 words each, one observation + one opportunity, include the
   portfolio and booking links, position Prem as someone who builds connected
   brand systems, and NEVER mention an agency / "Ences Marketing" / fake stats /
   fake promises.

Determinism guardrails
----------------------
The model is strongly instructed, but we also enforce the non-negotiables in
code afterward: the portfolio and booking links are appended if the model
omitted them, and word counts are computed here (not trusted from the model)
for the validator to check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import settings
from core.ai_client import AIClient
from core.research import ResearchEvidence

# Banned terms — the positioning must never read as an agency pitch.
BANNED_TERMS = ("ences marketing", "agency", "marketing agency")


@dataclass
class ResearchAnalysis:
    website_summary: str = ""
    observation: str = ""
    opportunity: str = ""
    personalization_evidence: str = ""
    model_confidence: float = 0.0  # the model's own sufficiency rating [0,1]
    note: str = ""


@dataclass
class GeneratedEmails:
    subject: str = ""
    email_1: str = ""
    followup_1: str = ""
    followup_2: str = ""
    word_counts: dict[str, int] = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════
#  Prompts
# ════════════════════════════════════════════════════════════════════
_ANALYSIS_SYSTEM = """You are a meticulous B2B research analyst. You will be given
raw text scraped from a company's own website. Your job is to summarise it and
find ONE genuine, specific observation about their brand or digital presence.

Absolute rules:
- Use ONLY facts present in the provided evidence. Never invent products,
  clients, numbers, or claims. If it is not in the evidence, it does not exist.
- The observation must be specific to THIS company (something you could only say
  after visiting their site), not a generic statement that fits any business.
- The opportunity must connect to brand systems: visual consistency, messaging,
  brand experience, customer journey, conversion, or trust.
- If the evidence is too thin to say anything specific, set model_confidence low
  (below 0.5) and explain in note. Do not fabricate to fill the gap.

Return JSON with keys:
  website_summary  (2-3 sentences, factual),
  observation      (one specific sentence),
  opportunity      (one sentence, tied to connected brand systems),
  personalization_evidence (the exact phrase/detail from the site you used),
  model_confidence (0.0-1.0),
  note             (short; empty if all good)."""

_EMAIL_SYSTEM = """You write concise, human cold outreach emails for Prem, a
designer who builds CONNECTED BRAND SYSTEMS — aligning visual consistency,
messaging, brand experience, the customer journey, conversion, and trust into
one coherent system. Prem is an individual, not an agency.

Voice: warm, specific, peer-to-peer, no hype. Sound like a sharp person who
actually looked at their website, not a template.

Hard rules for EVERY email (initial + both follow-ups):
- Maximum 130 words. Shorter is better.
- Open from the genuine observation provided. Do not restate it generically.
- Name ONE opportunity tied to connected brand systems.
- Include the portfolio link and the booking link exactly as given.
- Do NOT mention any agency, "Ences Marketing", "marketing agency", retainers,
  social media management, random design work, fake statistics, or fake promises.
- No placeholders like [Name] — use the real fields provided, and if a field is
  missing, write naturally without it.

The two follow-ups are gentle nudges on the same thread: shorter, add one fresh
angle or reason to reply, never guilt-trip, never repeat the first email verbatim.

Return JSON with keys: subject, email_1, followup_1, followup_2.
Each email body should be plain text with line breaks as \\n."""


class EmailGenerator:
    def __init__(self, ai: AIClient | None = None) -> None:
        self._ai = ai or AIClient()

    # ── Step 1: grounded analysis ───────────────────────────────────
    def analyze_research(self, evidence: ResearchEvidence, company: str) -> ResearchAnalysis:
        if not evidence.reachable or not evidence.evidence_text:
            return ResearchAnalysis(model_confidence=0.0, note="No usable website evidence.")

        user = (
            f"Company: {company}\n"
            f"Website: {evidence.website}\n"
            f"--- WEBSITE EVIDENCE (scraped) ---\n{evidence.evidence_text}\n"
            f"--- ADDITIONAL SEARCH CONTEXT (optional) ---\n{evidence.search_context or '(none)'}\n"
        )
        data = self._ai.complete_json(_ANALYSIS_SYSTEM, user, max_tokens=700, temperature=0.4)
        return ResearchAnalysis(
            website_summary=str(data.get("website_summary", "")).strip(),
            observation=str(data.get("observation", "")).strip(),
            opportunity=str(data.get("opportunity", "")).strip(),
            personalization_evidence=str(data.get("personalization_evidence", "")).strip(),
            model_confidence=_as_float(data.get("model_confidence", 0.0)),
            note=str(data.get("note", "")).strip(),
        )

    # ── Step 2: copy ────────────────────────────────────────────────
    def write_emails(self, lead: dict, analysis: ResearchAnalysis) -> GeneratedEmails:
        first = (lead.get("First Name") or "").strip()
        company = (lead.get("Company") or "").strip()

        user = (
            f"Recipient first name: {first or '(unknown)'}\n"
            f"Company: {company}\n"
            f"Sender name: {settings.smtp.sender_name}\n"
            f"Portfolio link (use verbatim): {settings.outreach.portfolio_url}\n"
            f"Booking link (use verbatim): {settings.outreach.booking_url}\n\n"
            f"Genuine observation: {analysis.observation}\n"
            f"Opportunity: {analysis.opportunity}\n"
            f"Personalization evidence: {analysis.personalization_evidence}\n"
        )
        data = self._ai.complete_json(_EMAIL_SYSTEM, user, max_tokens=1100, temperature=0.75)

        emails = GeneratedEmails(
            subject=str(data.get("subject", "")).strip(),
            email_1=_enforce_links(str(data.get("email_1", "")).strip()),
            followup_1=_enforce_links(str(data.get("followup_1", "")).strip()),
            followup_2=_enforce_links(str(data.get("followup_2", "")).strip()),
        )
        emails.word_counts = {
            "email_1": word_count(emails.email_1),
            "followup_1": word_count(emails.followup_1),
            "followup_2": word_count(emails.followup_2),
        }
        return emails


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def word_count(text: str) -> int:
    return len([w for w in text.split() if w.strip()])


def contains_banned_terms(text: str) -> list[str]:
    low = text.lower()
    return [t for t in BANNED_TERMS if t in low]


def _enforce_links(body: str) -> str:
    """
    Guarantee the portfolio and booking links are present. If the model omitted
    one, append a short closing line rather than silently shipping an email that
    fails validation. Kept minimal to respect the word cap.
    """
    portfolio = settings.outreach.portfolio_url
    booking = settings.outreach.booking_url
    additions = []
    if portfolio not in body:
        additions.append(f"Portfolio: {portfolio}")
    if booking not in body:
        additions.append(f"Grab 15 min: {booking}")
    if additions:
        body = body.rstrip() + "\n\n" + "\n".join(additions)
    return body


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
