"""
core/scheduler.py
─────────────────
Follow-up timing logic. Pure date/state reasoning — no network, no sheet
access — so it is fully unit-testable.

Rules (from the spec)
---------------------
* Follow-up 1 is due FOLLOWUP_1_DAYS after the initial send (default 4).
* Follow-up 2 is due FOLLOWUP_2_DAYS after the initial send (default 10).
* A follow-up is sent only if: it is due, it hasn't already been sent, and no
  reply has been received. A reply cancels all remaining follow-ups.

The orchestrator reads Campaign rows, builds a `FollowupState` for each, and
asks this module `due_followups(...)` to decide what (if anything) to send now.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from dateutil import parser as dateparser

from config.settings import settings


@dataclass
class FollowupState:
    """A Campaign row's follow-up-relevant fields, parsed."""

    sent_timestamp: str          # when the initial email went out
    followup_1_sent: bool
    followup_2_sent: bool
    reply_received: bool

    def initial_date(self) -> dt.date | None:
        return _parse_date(self.sent_timestamp)


def followup_due_dates(sent_timestamp: str) -> dict[str, dt.date | None]:
    """Compute the due dates for FU1 and FU2 from the initial send date."""
    base = _parse_date(sent_timestamp)
    if base is None:
        return {"followup_1": None, "followup_2": None}
    return {
        "followup_1": base + dt.timedelta(days=settings.behaviour.followup_1_days),
        "followup_2": base + dt.timedelta(days=settings.behaviour.followup_2_days),
    }


def due_followups(state: FollowupState, today: dt.date | None = None) -> list[str]:
    """
    Return which follow-ups should be sent right now: a subset of
    ["followup_1", "followup_2"]. Empty if a reply exists, nothing is due yet,
    or everything's already been sent.
    """
    if state.reply_received:
        return []  # reply cancels all follow-ups

    today = today or dt.date.today()
    due_dates = followup_due_dates(state.sent_timestamp)
    out: list[str] = []

    if not state.followup_1_sent and due_dates["followup_1"] and today >= due_dates["followup_1"]:
        out.append("followup_1")

    # FU2 only after FU1 has gone out (keeps the sequence coherent).
    fu1_done = state.followup_1_sent or "followup_1" in out
    if fu1_done and not state.followup_2_sent and due_dates["followup_2"] and today >= due_dates["followup_2"]:
        out.append("followup_2")

    return out


# ── helpers ─────────────────────────────────────────────────────────
def _parse_date(value: str) -> dt.date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return dateparser.parse(value).date()
    except (ValueError, OverflowError):
        return None
