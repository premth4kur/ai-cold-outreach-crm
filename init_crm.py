"""
init_crm.py
───────────
One-off bootstrap: connect to your Google Sheet and create every CRM tab with
the correct headers. Safe to run repeatedly — existing data is never touched.

Usage:
    python init_crm.py

Requires a valid .env and credentials/service_account.json (see README).
"""

from __future__ import annotations

from config.settings import settings
from core.sheets import SheetsClient


def main() -> None:
    # Minimal config check: we only need Sheets creds for bootstrap.
    if not settings.sheets.sheet_id:
        raise SystemExit("GOOGLE_SHEET_ID is not set in .env")

    client = SheetsClient()
    print("Connecting to Google Sheets…")
    client.connect()

    print("Ensuring all CRM tabs and headers exist…")
    client.ensure_schema()

    titles = [ws.title for ws in client.spreadsheet.worksheets()]
    print("Done. Tabs present:")
    for t in titles:
        print("  -", t)
    print("\nCRM is ready. Import your leads into the 'Leads' tab with Status = New.")


if __name__ == "__main__":
    main()
