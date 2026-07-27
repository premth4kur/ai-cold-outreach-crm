"""
main.py — one-click orchestrator
════════════════════════════════
Run the whole outreach pipeline with a single command:

    python main.py

What one run does
-----------------
1. Loads + validates config, opens the CRM, ensures all tabs exist.
2. Scans the mailbox (IMAP) and marks any leads who replied — so we never chase
   someone who already answered.
3. Processes every lead whose Status is exactly "New":
      dedupe → research → grounded analysis → write emails → validate → send →
      update CRM → log → random 4–10 min delay → next lead.
   Duplicates, missing-email (Social Outreach) and low-confidence (Needs Review)
   leads are routed aside without sending. Runs are resumable: state lives in
   the sheet, so a crash or a per-run send cap simply continues next time.
4. Sends any follow-ups that are now due (FU1 after 4 days, FU2 after 10),
   skipping anyone who replied.
5. Recomputes the Dashboard metrics.

Nothing here is destructive: it only advances leads forward through their
lifecycle. Set DRY_RUN=true in .env to rehearse everything without sending.
"""

from __future__ import annotations

import datetime as dt

from config.schema import (
    CAMPAIGN_COLUMNS,
    EMAILS_COLUMNS,
    LEADS_COLUMNS,
    LeadStatus,
    Tab,
)
from config.settings import settings
from core.ai_client import AIError
from core.dedupe import DuplicateIndex
from core.email_generator import EmailGenerator
from core.imap_client import IMAPClient
from core.research import WebsiteResearcher
from core.scheduler import FollowupState, due_followups, followup_due_dates
from core.sheets import LeadRow, SheetsClient
from core.smtp_client import SMTPClient
from core.validator import validate_before_send
from utils.delays import wait_between_sends
from utils.logger import activity_log, get_logger, setup_logging, smtp_log

log = get_logger("main")

_YES = "Yes"
_NO = "No"


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"yes", "true", "1", "y"}


# ════════════════════════════════════════════════════════════════════
#  Orchestrator
# ════════════════════════════════════════════════════════════════════
class Orchestrator:
    def __init__(self) -> None:
        self.sheets = SheetsClient()
        self.researcher = WebsiteResearcher()
        self.generator = EmailGenerator()
        self.smtp = SMTPClient()
        self.imap = IMAPClient()
        self.sends_this_run = 0

    # ── Entry ───────────────────────────────────────────────────────
    def run(self) -> None:
        setup_logging()
        log.info("=== AI Cold Outreach run starting ===")
        settings.validate_for_run()

        self.sheets.connect()
        self.sheets.ensure_schema()
        log.info("CRM connected and schema verified.")

        # Preflight: creds must actually work before we spend AI tokens.
        if not settings.behaviour.dry_run:
            if not self.smtp.verify_connection():
                raise SystemExit("SMTP connection failed — check Hostinger credentials in .env.")
            if not self.imap.verify_connection():
                log.warning("IMAP connection failed — reply detection will be skipped this run.")
        else:
            log.info("DRY_RUN enabled: no emails will actually be sent.")

        self._sync_replies()
        self.smtp.open() if not settings.behaviour.dry_run else None
        try:
            self._process_new_leads()
            self._process_due_followups()
        finally:
            self.smtp.close()

        self._update_dashboard()
        log.info("=== Run complete. Emails sent this run: %d ===", self.sends_this_run)

    # ── Step: reply detection ───────────────────────────────────────
    def _sync_replies(self) -> None:
        if settings.behaviour.dry_run:
            return
        try:
            signals = self.imap.fetch_reply_signals(since_days=30)
        except Exception as exc:  # never let mail scanning abort the run
            log.warning("Reply scan failed: %s", exc)
            return

        rows = self.sheets.get_rows(Tab.CAMPAIGN, CAMPAIGN_COLUMNS)
        marked = 0
        for _, rec in rows:
            lead_id = str(rec.get("Lead ID", "")).strip()
            email = str(rec.get("Email", "")).strip()
            msg_id = str(rec.get("Message ID", "")).strip()
            if not lead_id or _truthy(rec.get("Reply Received")):
                continue
            if signals.has_reply_from(email) or signals.has_reply_to_message_id(msg_id):
                self.sheets.upsert_by_lead_id(
                    Tab.CAMPAIGN, lead_id,
                    {"Reply Received": _YES, "Reply Date": _today()},
                )
                self.sheets.log_activity(lead_id, rec.get("Company", ""), "Reply detected", "Reply", "")
                marked += 1
        if marked:
            log.info("Marked %d lead(s) as replied.", marked)

    # ── Step: new leads ─────────────────────────────────────────────
    def _process_new_leads(self) -> None:
        all_leads = self.sheets.get_all_leads()

        # Build duplicate index from already-processed leads only; new leads add
        # themselves as they're accepted so same-batch dupes are also caught.
        processed = [l for l in all_leads if l.status not in LeadStatus.processable()]
        dup_index = DuplicateIndex.from_rows([l.data for l in processed])

        existing_ids = [l.lead_id for l in all_leads if l.lead_id]
        new_leads = [l for l in all_leads if l.status in LeadStatus.processable()]
        log.info("Found %d new lead(s) to process.", len(new_leads))

        cap = settings.behaviour.max_sends_per_run
        for lead in new_leads:
            if cap and self.sends_this_run >= cap:
                log.info("Per-run send cap (%d) reached; remaining new leads left for next run.", cap)
                break
            try:
                self._process_single_lead(lead, dup_index, existing_ids)
            except AIError as exc:
                log.error("AI error on %s: %s", lead.company, exc)
                self.sheets.set_lead_status(lead.row_number, LeadStatus.NEEDS_REVIEW)
                self.sheets.log_activity(lead.lead_id, lead.company, "AI generation", "Error", str(exc))
            except Exception as exc:  # keep the run alive; isolate per-lead failures
                log.exception("Unexpected error on %s", lead.company)
                self.sheets.set_lead_status(lead.row_number, LeadStatus.NEEDS_REVIEW)
                self.sheets.log_activity(lead.lead_id, lead.company, "Processing", "Error", str(exc))

    def _process_single_lead(self, lead: LeadRow, dup_index: DuplicateIndex, existing_ids: list[str]) -> None:
        # 1. Ensure a Lead ID.
        lead_id = lead.lead_id
        if not lead_id:
            lead_id = self.sheets.next_lead_id(existing_ids)
            existing_ids.append(lead_id)
            self.sheets.assign_lead_id(lead.row_number, lead_id)
            lead.data["Lead ID"] = lead_id

        company = lead.company
        log.info("Processing %s (%s)…", company or "(no company)", lead_id)

        # 2. Duplicate check.
        verdict = dup_index.classify(lead.email, company, lead.website)
        if verdict.is_duplicate:
            self.sheets.set_lead_status(lead.row_number, LeadStatus.DUPLICATE, Duplicate=_YES)
            self.sheets.log_activity(lead_id, company, "Dedupe", "Duplicate", verdict.reason)
            log.info("  → duplicate (%s), skipped.", verdict.reason)
            return
        dup_index.add(lead.email, company, lead.website)

        # 3. A website is where we research + discover the official email.
        if not lead.website:
            hold = LeadStatus.SOCIAL_OUTREACH if settings.behaviour.auto_send else LeadStatus.NEEDS_REVIEW
            self.sheets.set_lead_status(lead.row_number, hold)
            self.sheets.log_activity(lead_id, company, "Research", "Skipped", "No website")
            log.info("  → no website (%s).", hold.value)
            return

        # 4. Research the site.
        evidence = self.researcher.research(lead.website)
        if not evidence.reachable:
            self.sheets.upsert_by_lead_id(Tab.RESEARCH, lead_id, {
                "Company": company, "Website Summary": "", "Observation": "",
                "Research Timestamp": evidence.timestamp, "Research Confidence": evidence.confidence,
                "Personalization Evidence": evidence.error,
            })
            hold = LeadStatus.SOCIAL_OUTREACH if settings.behaviour.auto_send else LeadStatus.NEEDS_REVIEW
            self.sheets.set_lead_status(lead.row_number, hold, Confidence=evidence.confidence)
            self.sheets.log_activity(lead_id, company, "Research", "Unreachable", evidence.error)
            log.info("  → website unreachable (%s).", hold.value)
            return

        # 5. Decide recipient email (official only). Prefer an imported address,
        #    else the officially published one discovered on the site.
        recipient = lead.email or evidence.primary_email
        if not recipient:
            self.sheets.upsert_by_lead_id(Tab.RESEARCH, lead_id, {
                "Company": company, "Website Summary": evidence.evidence_text[:500],
                "Research Timestamp": evidence.timestamp, "Research Confidence": evidence.confidence,
            })
            self.sheets.set_lead_status(lead.row_number, LeadStatus.SOCIAL_OUTREACH, Confidence=evidence.confidence)
            self.sheets.log_activity(lead_id, company, "Email discovery", "No official email", "→ Social Outreach")
            log.info("  → no official email, routed to Social Outreach.")
            return

        # 6. Grounded AI analysis.
        analysis = self.generator.analyze_research(evidence, company)
        self.sheets.upsert_by_lead_id(Tab.RESEARCH, lead_id, {
            "Company": company,
            "Website Summary": analysis.website_summary,
            "Observation": analysis.observation,
            "Opportunity": analysis.opportunity,
            "Personalization Evidence": analysis.personalization_evidence,
            "Research Timestamp": evidence.timestamp,
            "Research Confidence": evidence.confidence,
        })

        threshold = settings.behaviour.research_confidence_threshold
        combined_conf = round(min(evidence.confidence, max(analysis.model_confidence, 0.0)), 2)
        low = (analysis.model_confidence < 0.5 or evidence.confidence < threshold or not analysis.observation)
        # In auto-send mode we never park low-confidence leads for review — the AI
        # writes the best email it can from whatever it found and proceeds to send.
        if low and not settings.behaviour.auto_send:
            self.sheets.set_lead_status(lead.row_number, LeadStatus.NEEDS_REVIEW, Confidence=combined_conf)
            self.sheets.log_activity(lead_id, company, "Research", "Low confidence", analysis.note or "below threshold")
            log.info("  → low research confidence (%.2f), needs review.", combined_conf)
            return

        self.sheets.set_lead_status(lead.row_number, LeadStatus.RESEARCH_COMPLETE, Confidence=combined_conf)

        # 7. Generate copy.
        emails = self.generator.write_emails(lead.data, analysis)
        wc = emails.word_counts
        self.sheets.upsert_by_lead_id(Tab.EMAILS, lead_id, {
            "Company": company,
            "Subject": emails.subject,
            "Email 1": emails.email_1,
            "Follow-up 1": emails.followup_1,
            "Follow-up 2": emails.followup_2,
            "Word Count": f"E1:{wc.get('email_1',0)} F1:{wc.get('followup_1',0)} F2:{wc.get('followup_2',0)}",
            "Validation": "Pending",
        })

        # 8. Validate. In auto-send mode we relax the "must have a grounded
        #    observation" checks (the AI still writes a real email), but keep the
        #    hard gates: recipient, subject, links, word count, no banned terms.
        result = validate_before_send(recipient_email=recipient, analysis=analysis, emails=emails,
                                      require_grounding=not settings.behaviour.auto_send)
        if not result.ok:
            self.sheets.upsert_by_lead_id(Tab.EMAILS, lead_id, {"Validation": f"FAILED: {result.reason()}"})
            hold = LeadStatus.FAILED if settings.behaviour.auto_send else LeadStatus.NEEDS_REVIEW
            self.sheets.set_lead_status(lead.row_number, hold)
            self.sheets.log_activity(lead_id, company, "Validation", "Failed", result.reason())
            log.info("  → validation failed: %s", result.reason())
            return
        self.sheets.upsert_by_lead_id(Tab.EMAILS, lead_id, {"Validation": "Passed"})
        self.sheets.set_lead_status(lead.row_number, LeadStatus.EMAIL_READY)

        # 9. Send + record.
        self._send_initial(lead, lead_id, recipient, emails)

    def _send_initial(self, lead: LeadRow, lead_id: str, recipient: str, emails) -> None:
        company = lead.company
        due = followup_due_dates(_now())  # due dates computed from now
        res = self.smtp.send(to_email=recipient, subject=emails.subject, body=emails.email_1)
        smtp_log(f"[{lead_id}] initial -> {recipient}: {res.smtp_status} {res.error}")

        # Dry run is a non-destructive rehearsal: everything is prepared and the
        # draft is validated, but the lead is left as New so a later REAL run
        # actually sends it. Nothing is marked Sent.
        if res.dry_run:
            self.sheets.log_activity(lead_id, company, "Send initial", "Dry run", f"would send to {recipient}")
            self.sheets.set_lead_status(lead.row_number, LeadStatus.NEW)
            log.info("  → DRY RUN: would send to %s (left as New)", recipient)
            return

        campaign = {
            "Company": company,
            "Email": recipient,
            "Email Sent": _YES if res.ok else _NO,
            "Sent Timestamp": _now() if res.ok else "",
            "SMTP Status": res.smtp_status,
            "Message ID": res.message_id,
            "Follow-up 1 Due": _fmt_date(due["followup_1"]),
            "Follow-up 2 Due": _fmt_date(due["followup_2"]),
            "Follow-up 1 Sent": _NO,
            "Follow-up 2 Sent": _NO,
            "Reply Received": _NO,
        }
        self.sheets.upsert_by_lead_id(Tab.CAMPAIGN, lead_id, campaign)

        if res.ok:
            self.sheets.set_lead_status(lead.row_number, LeadStatus.SENT)
            self.sheets.log_activity(lead_id, company, "Send initial", "Sent", res.smtp_status)
            self.sends_this_run += 1
            log.info("  → sent to %s (%s)", recipient, res.smtp_status)
            wait_between_sends()
        else:
            self.sheets.set_lead_status(lead.row_number, LeadStatus.FAILED)
            self.sheets.log_activity(lead_id, company, "Send initial", "Failed", res.error)
            log.warning("  → send failed: %s", res.error)

    # ── Step: due follow-ups ────────────────────────────────────────
    def _process_due_followups(self) -> None:
        cap = settings.behaviour.max_sends_per_run
        campaign_rows = self.sheets.get_rows(Tab.CAMPAIGN, CAMPAIGN_COLUMNS)
        for _, rec in campaign_rows:
            if cap and self.sends_this_run >= cap:
                log.info("Send cap reached during follow-ups; remainder next run.")
                break
            lead_id = str(rec.get("Lead ID", "")).strip()
            if not lead_id or not _truthy(rec.get("Email Sent")):
                continue

            state = FollowupState(
                sent_timestamp=str(rec.get("Sent Timestamp", "")),
                followup_1_sent=_truthy(rec.get("Follow-up 1 Sent")),
                followup_2_sent=_truthy(rec.get("Follow-up 2 Sent")),
                reply_received=_truthy(rec.get("Reply Received")),
            )
            due = due_followups(state)
            if not due:
                continue

            email_row = self.sheets.get_row_by_lead_id(Tab.EMAILS, lead_id)
            if not email_row:
                continue

            for fu in due:
                if cap and self.sends_this_run >= cap:
                    break
                self._send_followup(rec, email_row, lead_id, fu)

    def _send_followup(self, campaign_rec: dict, email_row: dict, lead_id: str, which: str) -> None:
        company = campaign_rec.get("Company", "")
        recipient = str(campaign_rec.get("Email", "")).strip()
        base_subject = str(email_row.get("Subject", "")).strip()
        subject = base_subject if base_subject.lower().startswith("re:") else f"Re: {base_subject}"
        body = email_row.get("Follow-up 1" if which == "followup_1" else "Follow-up 2", "")
        if not body.strip() or not recipient:
            return

        msg_id = str(campaign_rec.get("Message ID", "")).strip() or None
        res = self.smtp.send(to_email=recipient, subject=subject, body=body,
                             in_reply_to=msg_id, references=msg_id)
        smtp_log(f"[{lead_id}] {which} -> {recipient}: {res.smtp_status} {res.error}")

        label_sent = "Follow-up 1 Sent" if which == "followup_1" else "Follow-up 2 Sent"
        label_ts = "Follow-up 1 Timestamp" if which == "followup_1" else "Follow-up 2 Timestamp"
        if res.ok:
            self.sheets.upsert_by_lead_id(Tab.CAMPAIGN, lead_id, {label_sent: _YES, label_ts: _now()})
            self.sheets.log_activity(lead_id, company, f"Send {which}", "Sent", res.smtp_status)
            self.sends_this_run += 1
            log.info("Follow-up (%s) sent to %s.", which, recipient)
            wait_between_sends()
        else:
            self.sheets.log_activity(lead_id, company, f"Send {which}", "Failed", res.error)
            log.warning("Follow-up (%s) failed: %s", which, res.error)

    # ── Step: dashboard ─────────────────────────────────────────────
    def _update_dashboard(self) -> None:
        try:
            metrics = self._compute_metrics()
            self.sheets.update_dashboard(metrics)
            log.info("Dashboard updated.")
        except Exception as exc:
            log.warning("Dashboard update failed: %s", exc)

    def _compute_metrics(self) -> dict:
        leads = self.sheets.get_all_leads()
        campaign = [rec for _, rec in self.sheets.get_rows(Tab.CAMPAIGN, CAMPAIGN_COLUMNS)]

        def count_status(s: LeadStatus) -> int:
            return sum(1 for l in leads if l.status == s.value)

        sent = sum(1 for c in campaign if _truthy(c.get("Email Sent")))
        replies = sum(1 for c in campaign if _truthy(c.get("Reply Received")))
        failed = count_status(LeadStatus.FAILED)
        fu1_sent = sum(1 for c in campaign if _truthy(c.get("Follow-up 1 Sent")))
        fu2_sent = sum(1 for c in campaign if _truthy(c.get("Follow-up 2 Sent")))
        meetings = sum(1 for c in campaign if _truthy(c.get("Meeting Booked")))

        today = dt.date.today()
        def sent_on_or_after(days: int) -> int:
            cutoff = today - dt.timedelta(days=days)
            n = 0
            for c in campaign:
                d = _safe_date(c.get("Sent Timestamp", ""))
                if d and d >= cutoff:
                    n += 1
            return n

        pending = sum(1 for l in leads if l.status in {
            LeadStatus.NEW.value, LeadStatus.RESEARCH_COMPLETE.value,
            LeadStatus.EMAIL_READY.value, LeadStatus.NEEDS_REVIEW.value,
        })

        return {
            "Total Leads": len(leads),
            "New Leads": count_status(LeadStatus.NEW),
            "Research Complete": count_status(LeadStatus.RESEARCH_COMPLETE),
            "Email Ready": count_status(LeadStatus.EMAIL_READY),
            "Emails Sent": sent,
            "Pending": pending,
            "Failed": failed,
            "Social Outreach": count_status(LeadStatus.SOCIAL_OUTREACH),
            "Follow-up 1 Sent": fu1_sent,
            "Follow-up 2 Sent": fu2_sent,
            "Follow-up 1 Pending": max(sent - fu1_sent - replies, 0),
            "Follow-up 2 Pending": max(fu1_sent - fu2_sent - replies, 0),
            "Replies": replies,
            "Meetings Booked": meetings,
            "Bounce Rate": f"{(failed / sent * 100):.1f}%" if sent else "0%",
            "Reply Rate": f"{(replies / sent * 100):.1f}%" if sent else "0%",
            "Today's Emails": sent_on_or_after(0),
            "This Week": sent_on_or_after(7),
            "This Month": sent_on_or_after(30),
        }


# ── module-level date helpers ───────────────────────────────────────
def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return dt.date.today().isoformat()


def _fmt_date(d) -> str:
    return d.isoformat() if d else ""


def _safe_date(value: str):
    from core.scheduler import _parse_date
    return _parse_date(value)


def main() -> None:
    Orchestrator().run()


if __name__ == "__main__":
    main()
