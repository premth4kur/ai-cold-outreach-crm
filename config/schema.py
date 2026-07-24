"""
config/schema.py
────────────────
Single source of truth for the Google Sheets CRM structure.

Every tab name, every column, every allowed Status value, and the follow-up /
due-date field names live here. No other module hard-codes a column name or a
tab title — they import from this file. That keeps the CRM layout in ONE place,
so changing a column is a one-line edit instead of a hunt across the codebase.

Design notes
------------
* Tabs are keyed by a stable internal key (e.g. "LEADS") that never changes,
  even if we rename the visible sheet title later.
* Columns are ordered lists. The Sheets bootstrap writes them as the header row
  (row 1) in exactly this order.
* Several tabs share the LEAD_ID column. That column is the join key linking a
  lead across LEADS / RESEARCH / EMAILS / CAMPAIGN. It is always column A.
"""

from __future__ import annotations

from enum import Enum


# ════════════════════════════════════════════════════════════════════
#  Lead lifecycle — the Status values that drive processing
# ════════════════════════════════════════════════════════════════════
class LeadStatus(str, Enum):
    """
    The Status column on the LEADS tab. The orchestrator only ever *picks up*
    leads whose status is NEW. Every other value is a terminal or holding state
    that the pipeline skips, which is what makes runs resumable and duplicate-safe.
    """

    NEW = "New"                       # freshly imported, not yet processed
    RESEARCH_COMPLETE = "Research Complete"
    NEEDS_REVIEW = "Needs Review"     # research confidence too low — human check
    EMAIL_READY = "Email Ready"       # drafted + validated, awaiting send
    SENT = "Sent"                     # initial email sent
    SOCIAL_OUTREACH = "Social Outreach"  # no official email — handled off-email
    DUPLICATE = "Duplicate"           # matched an existing lead — skipped
    FAILED = "Failed"                 # send failed after retries
    CLOSED = "Closed"                 # deal closed / lead retired

    @classmethod
    def processable(cls) -> set[str]:
        """Statuses the pipeline is allowed to pick up and act on."""
        return {cls.NEW.value}

    @classmethod
    def terminal(cls) -> set[str]:
        """Statuses the pipeline must never re-process."""
        return {
            cls.RESEARCH_COMPLETE.value,
            cls.SENT.value,
            cls.CLOSED.value,
            cls.DUPLICATE.value,
            cls.SOCIAL_OUTREACH.value,
            cls.FAILED.value,
        }


# ════════════════════════════════════════════════════════════════════
#  Shared join key
# ════════════════════════════════════════════════════════════════════
LEAD_ID = "Lead ID"  # column A on every lead-scoped tab


# ════════════════════════════════════════════════════════════════════
#  Tab titles (the visible sheet names inside the spreadsheet)
# ════════════════════════════════════════════════════════════════════
class Tab:
    DASHBOARD = "Dashboard"
    LEADS = "Leads"
    RESEARCH = "Research"
    EMAILS = "Emails"
    CAMPAIGN = "Campaign"
    ACTIVITY_LOG = "Activity Log"


# ════════════════════════════════════════════════════════════════════
#  Column definitions, per tab (order matters — this IS the header row)
# ════════════════════════════════════════════════════════════════════

LEADS_COLUMNS: list[str] = [
    LEAD_ID,
    "Added Date",
    "Campaign",
    "Import Batch",
    "First Name",
    "Last Name",
    "Company",
    "Website",
    "Email",
    "LinkedIn",
    "Instagram",
    "Industry",
    "Segment",
    "Country",
    "Priority",
    "Status",
    "Confidence",
    "Duplicate",
    "Last Updated",
]

RESEARCH_COLUMNS: list[str] = [
    LEAD_ID,
    "Company",
    "Website Summary",
    "Observation",
    "Opportunity",
    "Personalization Evidence",
    "Research Timestamp",
    "Research Confidence",
]

EMAILS_COLUMNS: list[str] = [
    LEAD_ID,
    "Company",
    "Subject",
    "Email 1",
    "Follow-up 1",
    "Follow-up 2",
    "Word Count",
    "Validation",
]

CAMPAIGN_COLUMNS: list[str] = [
    LEAD_ID,
    "Company",
    "Email",
    "Email Sent",
    "Sent Timestamp",
    "SMTP Status",
    "Message ID",
    "Follow-up 1 Due",
    "Follow-up 1 Sent",
    "Follow-up 1 Timestamp",
    "Follow-up 2 Due",
    "Follow-up 2 Sent",
    "Follow-up 2 Timestamp",
    "Reply Received",
    "Reply Date",
    "Meeting Booked",
    "Closed",
]

ACTIVITY_LOG_COLUMNS: list[str] = [
    "Timestamp",
    "Lead",
    "Company",
    "Action",
    "Result",
    "Notes",
]

# The Dashboard is a rendered/computed tab (metrics + charts), not a data table.
# We still define its metric labels here so the bootstrap can lay them out and
# the dashboard builder can address each cell by name.
DASHBOARD_METRICS: list[str] = [
    "Total Leads",
    "New Leads",
    "Research Complete",
    "Email Ready",
    "Emails Sent",
    "Pending",
    "Failed",
    "Social Outreach",
    "Follow-up 1 Pending",
    "Follow-up 1 Sent",
    "Follow-up 2 Pending",
    "Follow-up 2 Sent",
    "Replies",
    "Positive Replies",
    "Negative Replies",
    "Meetings Booked",
    "Bounce Rate",
    "Reply Rate",
    "Today's Emails",
    "This Week",
    "This Month",
]


# ════════════════════════════════════════════════════════════════════
#  Registry — lets the bootstrap iterate every data tab uniformly
# ════════════════════════════════════════════════════════════════════
# Note: Dashboard is intentionally NOT in this registry because it is a
# computed layout, not a header+rows data table. It is bootstrapped separately.
DATA_TABS: dict[str, list[str]] = {
    Tab.LEADS: LEADS_COLUMNS,
    Tab.RESEARCH: RESEARCH_COLUMNS,
    Tab.EMAILS: EMAILS_COLUMNS,
    Tab.CAMPAIGN: CAMPAIGN_COLUMNS,
    Tab.ACTIVITY_LOG: ACTIVITY_LOG_COLUMNS,
}


def column_index(columns: list[str], name: str) -> int:
    """
    Return the 1-based column index (A=1, B=2, …) of `name` within `columns`.
    Used by the Sheets layer to build A1 ranges without magic numbers.
    Raises KeyError with a clear message if the column is missing.
    """
    try:
        return columns.index(name) + 1
    except ValueError as exc:  # pragma: no cover - defensive
        raise KeyError(f"Column {name!r} is not defined in this tab") from exc
