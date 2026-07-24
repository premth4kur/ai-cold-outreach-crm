"""
core/imap_client.py
───────────────────
Reply detection via Hostinger IMAP. Before sending any follow-ups, the
orchestrator asks this module "who has replied?" and skips those leads.

How it works
------------
* Connects over IMAP-SSL and scans the configured folder (INBOX) for messages
  received since a cutoff date (default: the last N days).
* For each message, extracts the sender address. It returns the SET of sender
  email addresses that have written to us, plus, when available, the
  In-Reply-To / References headers so a reply can be matched to the exact
  outbound Message-ID if you prefer thread-level precision.
* Read-only: it never deletes, moves, or marks messages. Purely observational.

Matching strategy (used by the scheduler/orchestrator)
------------------------------------------------------
Primary match: the reply's From address equals the lead's email → that lead has
replied. Secondary match: the reply's In-Reply-To/References contains a
Message-ID we stored in the Campaign tab. Either is treated as "reply received".
"""

from __future__ import annotations

import datetime as dt
import email
import imaplib
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import parseaddr

from config.settings import settings


@dataclass
class ReplySignals:
    """Everything we learned from one inbox scan."""

    sender_emails: set[str] = field(default_factory=set)   # normalized From addresses
    referenced_message_ids: set[str] = field(default_factory=set)  # In-Reply-To/References values
    scanned: int = 0

    def has_reply_from(self, lead_email: str) -> bool:
        return bool(lead_email) and lead_email.strip().lower() in self.sender_emails

    def has_reply_to_message_id(self, message_id: str) -> bool:
        if not message_id:
            return False
        mid = message_id.strip()
        return any(mid in ref for ref in self.referenced_message_ids)


class IMAPClient:
    def __init__(self) -> None:
        self._cfg = settings.imap

    def verify_connection(self) -> bool:
        """Prove IMAP creds work. Never raises."""
        try:
            conn = self._connect()
            conn.logout()
            return True
        except Exception:
            return False

    def _connect(self) -> imaplib.IMAP4_SSL:
        cfg = self._cfg
        conn = imaplib.IMAP4_SSL(cfg.host, cfg.port)
        conn.login(cfg.user, cfg.password)
        return conn

    def fetch_reply_signals(self, since_days: int = 30) -> ReplySignals:
        """
        Scan the mailbox for messages since `since_days` ago and collect sender
        addresses + referenced message-ids. Read-only.
        """
        signals = ReplySignals()
        conn = self._connect()
        try:
            conn.select(self._cfg.folder, readonly=True)
            since = (dt.date.today() - dt.timedelta(days=since_days)).strftime("%d-%b-%Y")
            typ, data = conn.search(None, f'(SINCE {since})')
            if typ != "OK" or not data or not data[0]:
                return signals

            ids = data[0].split()
            for num in ids:
                # Fetch only the headers we need (cheap).
                typ, msg_data = conn.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM IN-REPLY-TO REFERENCES)])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                from_addr = parseaddr(_decode(msg.get("From", "")))[1].strip().lower()
                if from_addr:
                    signals.sender_emails.add(from_addr)

                for hdr in ("In-Reply-To", "References"):
                    val = _decode(msg.get(hdr, "")).strip()
                    if val:
                        signals.referenced_message_ids.add(val)

                signals.scanned += 1
            return signals
        finally:
            try:
                conn.logout()
            except Exception:
                pass


def _decode(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value
