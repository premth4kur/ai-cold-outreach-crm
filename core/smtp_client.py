"""
core/smtp_client.py
───────────────────
Sending via Hostinger SMTP. Credentials come only from settings (.env), never
hard-coded.

Features
--------
* SSL (465) or STARTTLS (587), chosen by SMTP_USE_SSL.
* A generated, RFC-compliant Message-ID per email, returned so the CRM can store
  it and later thread follow-ups to the same conversation (In-Reply-To /
  References), which is what lets replies land in the same thread.
* Bounded retry on transient failures; the raw SMTP response/exception is
  captured and returned for logging.
* DRY_RUN short-circuits the actual send (everything else runs) so you can test
  the full pipeline without emailing anyone.
* One connection can send many messages (`open()` / `close()`), or use
  `send()` as a one-shot.
"""

from __future__ import annotations

import smtplib
import ssl
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import settings


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""
    smtp_status: str = ""      # human-readable status for the CRM
    error: str = ""
    dry_run: bool = False


class SMTPClient:
    def __init__(self) -> None:
        self._cfg = settings.smtp
        self._conn: smtplib.SMTP | smtplib.SMTP_SSL | None = None

    # ── Connection lifecycle ────────────────────────────────────────
    @retry(
        reraise=True,
        retry=retry_if_exception_type((smtplib.SMTPException, OSError)),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        stop=stop_after_attempt(3),
    )
    def open(self) -> None:
        """Open + authenticate a reusable connection."""
        if self._conn is not None:
            return
        cfg = self._cfg
        if cfg.use_ssl:
            ctx = ssl.create_default_context()
            conn = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=ctx)
        else:
            conn = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
            conn.ehlo()
            conn.starttls(context=ssl.create_default_context())
            conn.ehlo()
        conn.login(cfg.user, cfg.password)
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:
                pass
            finally:
                self._conn = None

    def __enter__(self) -> "SMTPClient":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── Verify (used by validation / preflight) ─────────────────────
    def verify_connection(self) -> bool:
        """Open then close a connection to prove SMTP creds work. Never raises."""
        try:
            self.open()
            self.close()
            return True
        except Exception:
            return False

    # ── Sending ─────────────────────────────────────────────────────
    def send(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> SendResult:
        """
        Send one plain-text email. If in_reply_to/references are supplied, the
        message threads under the original (used for follow-ups).
        """
        cfg = self._cfg
        msg = EmailMessage()
        msg["From"] = formataddr((cfg.sender_name, cfg.sender_email))
        msg["To"] = to_email
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        message_id = make_msgid(domain=_domain_of(cfg.sender_email))
        msg["Message-ID"] = message_id
        # Monitoring copy: every outgoing email is BCC'd to the test address so
        # you get a copy of exactly what each lead receives. Recipients never see
        # this. Set TEST_EMAIL="" to turn it off.
        if cfg.test_email:
            msg["Bcc"] = cfg.test_email
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
        msg.set_content(body)

        # DRY_RUN: simulate success without touching the network.
        if settings.behaviour.dry_run:
            return SendResult(
                ok=True,
                message_id=message_id,
                smtp_status="DRY_RUN (not actually sent)",
                dry_run=True,
            )

        try:
            self._deliver(msg, to_email)
            return SendResult(ok=True, message_id=message_id, smtp_status="250 Sent")
        except smtplib.SMTPException as exc:
            return SendResult(ok=False, message_id=message_id, smtp_status="SMTP error", error=str(exc))
        except OSError as exc:
            return SendResult(ok=False, message_id=message_id, smtp_status="Network error", error=str(exc))

    @retry(
        reraise=True,
        retry=retry_if_exception_type((smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError)),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        stop=stop_after_attempt(3),
    )
    def _deliver(self, msg: EmailMessage, to_email: str) -> None:
        """Ensure a live connection (reconnect if dropped) and send."""
        if self._conn is None:
            self.open()
        assert self._conn is not None
        self._conn.send_message(msg)


# ── helpers ─────────────────────────────────────────────────────────
def _domain_of(email: str) -> str:
    return email.split("@")[-1] if "@" in email else "localhost"


def new_message_id(email: str) -> str:
    """Standalone Message-ID generator (used if ever needed outside a send)."""
    return f"<{uuid.uuid4().hex}@{_domain_of(email)}>"
