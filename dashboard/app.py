"""
dashboard/app.py — local web control panel
═══════════════════════════════════════════
A user-friendly browser dashboard for the outreach system. Runs on your own PC,
so your SMTP password and API keys never leave your machine.

Start it:
    python -m dashboard.app
then open http://127.0.0.1:5000 in your browser.

What it gives you
-----------------
* Live metric cards (total leads, sent, replies, reply rate, follow-ups…).
* A leads table with statuses.
* Per-lead email preview (subject + Email 1 + both follow-ups).
* An activity feed (mirrors the CRM Activity Log).
* A big "Run campaign" button (with a Dry-run toggle) that launches the full
  pipeline in the background and streams live log output.

The backend reuses the exact same modules as `python main.py` — the dashboard is
just a friendly face over the same engine, so behaviour is identical.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import replace
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from config.schema import CAMPAIGN_COLUMNS, Tab
from config.settings import ConfigError, settings
from core.sheets import SheetsClient
from utils.logger import setup_logging

app = Flask(__name__)

# ── Shared, lazily-connected Sheets client ──────────────────────────
_sheets: SheetsClient | None = None
_sheets_lock = threading.Lock()


def get_sheets() -> SheetsClient:
    """Return a connected SheetsClient, connecting once and reusing it."""
    global _sheets
    with _sheets_lock:
        if _sheets is None:
            c = SheetsClient()
            c.connect()
            c.ensure_schema()
            _sheets = c
        return _sheets


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"yes", "true", "1", "y"}


# ════════════════════════════════════════════════════════════════════
#  Background run manager
# ════════════════════════════════════════════════════════════════════
class RunManager:
    """
    Runs the orchestrator in a background thread so the UI stays responsive.
    Only one run at a time. Exposes live state + a tail of today's log file.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.state = "idle"          # idle | running | done | error
        self.started_at: str = ""
        self.finished_at: str = ""
        self.message: str = ""
        self.dry_run = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, dry_run: bool) -> tuple[bool, str]:
        with self._lock:
            if self.is_running:
                return False, "A run is already in progress."
            self.dry_run = dry_run
            self.state = "running"
            self.started_at = _now()
            self.finished_at = ""
            self.message = "Run started."
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "Run started."

    def _run(self) -> None:
        # Import here to avoid a heavy import at module load.
        from main import Orchestrator

        # Apply the dry-run choice at runtime by swapping the (frozen) behaviour
        # config on the shared settings object. Every module reads it live.
        object.__setattr__(settings, "behaviour", replace(settings.behaviour, dry_run=self.dry_run))
        try:
            Orchestrator().run()
            self.state = "done"
            self.message = "Run complete."
        except SystemExit as exc:
            self.state = "error"
            self.message = str(exc)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self.state = "error"
            self.message = f"{type(exc).__name__}: {exc}"
        finally:
            self.finished_at = _now()

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "is_running": self.is_running,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "message": self.message,
            "log_tail": _read_log_tail(120),
        }


run_manager = RunManager()


# ════════════════════════════════════════════════════════════════════
#  Routes — pages
# ════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")


# ════════════════════════════════════════════════════════════════════
#  Routes — API
# ════════════════════════════════════════════════════════════════════
@app.get("/api/config-status")
def api_config_status():
    """Report whether the .env is valid enough to run. Secrets stay masked."""
    problems: list[str] = []
    try:
        settings.validate_for_run()
    except ConfigError as exc:
        problems = str(exc).replace("Configuration is invalid:", "").strip().split("\n  - ")
        problems = [p.strip("- ").strip() for p in problems if p.strip()]
    return jsonify({
        "ok": not problems,
        "problems": problems,
        "provider": settings.ai.provider,
        "sender": settings.smtp.sender_email,
        "dry_run_default": settings.behaviour.dry_run,
    })


@app.get("/api/metrics")
def api_metrics():
    try:
        metrics = _compute_metrics(get_sheets())
        return jsonify({"ok": True, "metrics": metrics})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _friendly(exc)}), 200


@app.get("/api/leads")
def api_leads():
    try:
        leads = get_sheets().get_all_leads()
        rows = [{
            "lead_id": l.lead_id,
            "company": l.company,
            "name": (l.get("First Name") + " " + l.get("Last Name")).strip(),
            "email": l.email,
            "website": l.website,
            "status": l.status,
            "confidence": l.get("Confidence"),
            "updated": l.get("Last Updated"),
        } for l in leads]
        return jsonify({"ok": True, "leads": rows})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _friendly(exc)}), 200


@app.get("/api/emails/<lead_id>")
def api_emails(lead_id: str):
    try:
        sheets = get_sheets()
        email_row = sheets.get_row_by_lead_id(Tab.EMAILS, lead_id) or {}
        research = sheets.get_row_by_lead_id(Tab.RESEARCH, lead_id) or {}
        return jsonify({"ok": True, "emails": email_row, "research": research})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _friendly(exc)}), 200


@app.get("/api/activity")
def api_activity():
    try:
        rows = [rec for _, rec in get_sheets().get_rows(Tab.ACTIVITY_LOG)]
        return jsonify({"ok": True, "activity": rows[-60:][::-1]})  # newest first
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _friendly(exc)}), 200


@app.post("/api/run")
def api_run():
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run", False))
    # Preflight config unless it's a dry run (dry run still needs Sheets + AI).
    try:
        settings.validate_for_run()
    except ConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    ok, msg = run_manager.start(dry_run=dry_run)
    return jsonify({"ok": ok, "message": msg})


@app.get("/api/run/status")
def api_run_status():
    return jsonify(run_manager.snapshot())


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _compute_metrics(sheets: SheetsClient) -> dict:
    from config.schema import LeadStatus

    leads = sheets.get_all_leads()
    campaign = [rec for _, rec in sheets.get_rows(Tab.CAMPAIGN, CAMPAIGN_COLUMNS)]

    def cs(s: LeadStatus) -> int:
        return sum(1 for l in leads if l.status == s.value)

    sent = sum(1 for c in campaign if _truthy(c.get("Email Sent")))
    replies = sum(1 for c in campaign if _truthy(c.get("Reply Received")))
    fu1 = sum(1 for c in campaign if _truthy(c.get("Follow-up 1 Sent")))
    fu2 = sum(1 for c in campaign if _truthy(c.get("Follow-up 2 Sent")))
    meetings = sum(1 for c in campaign if _truthy(c.get("Meeting Booked")))
    failed = cs(LeadStatus.FAILED)

    today = dt.date.today()

    def since(days: int) -> int:
        from core.scheduler import _parse_date
        cutoff = today - dt.timedelta(days=days)
        return sum(1 for c in campaign if (_parse_date(c.get("Sent Timestamp", "")) or dt.date.min) >= cutoff)

    return {
        "total": len(leads),
        "new": cs(LeadStatus.NEW),
        "research_complete": cs(LeadStatus.RESEARCH_COMPLETE),
        "email_ready": cs(LeadStatus.EMAIL_READY),
        "sent": sent,
        "needs_review": cs(LeadStatus.NEEDS_REVIEW),
        "social": cs(LeadStatus.SOCIAL_OUTREACH),
        "duplicate": cs(LeadStatus.DUPLICATE),
        "failed": failed,
        "fu1_sent": fu1,
        "fu2_sent": fu2,
        "replies": replies,
        "meetings": meetings,
        "reply_rate": f"{(replies / sent * 100):.1f}%" if sent else "0%",
        "bounce_rate": f"{(failed / sent * 100):.1f}%" if sent else "0%",
        "today": since(0),
        "week": since(7),
        "month": since(30),
    }


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    if "GOOGLE_SHEET_ID" in msg or "service account" in msg.lower():
        return "Google Sheets isn't configured yet. Fill in .env and credentials/service_account.json."
    return f"{type(exc).__name__}: {msg}"


def _read_log_tail(n: int) -> str:
    try:
        today = dt.date.today().isoformat()
        path = Path(settings.logs_dir) / f"daily-{today}.log"
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:  # noqa: BLE001
        return ""


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    setup_logging()
    host = "127.0.0.1"
    port = 5000
    print(f"\n  Outreach dashboard running →  http://{host}:{port}\n  (Press Ctrl+C to stop)\n")
    try:
        # Prefer waitress (production-grade) if available; fall back to Flask dev.
        from waitress import serve
        serve(app, host=host, port=port)
    except ImportError:
        app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
