"""
core/sheets.py
──────────────
The ONLY module that talks to Google Sheets. Everything else asks this layer
for leads and hands it updates; nobody else knows a cell address.

What it guarantees
------------------
* Google Sheets is the single master CRM. This layer never reads Excel.
* Bootstrap is idempotent: `ensure_schema()` creates any missing tab and writes
  the header row, but never wipes existing data. Safe to call every run.
* Reads are status-driven: `fetch_new_leads()` returns only rows whose Status is
  "New", each carrying its real sheet row number so writes are precise.
* Writes are addressed by Lead ID, not by row position, so concurrent manual
  edits in the sheet don't corrupt updates. The Research/Emails/Campaign tabs
  are upserted (row created on first write, updated thereafter).
* Every Google API call is wrapped with bounded retry/backoff for transient
  quota (429) and 5xx errors.

Design choice: this layer is deliberately "chatty but correct". Cold outreach
runs process a modest number of leads with long random delays between sends, so
clarity and correctness matter far more than shaving API calls. Where it's cheap
to batch (header writes, dashboard), we do.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound
from gspread.utils import rowcol_to_a1
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.schema import (
    ACTIVITY_LOG_COLUMNS,
    DASHBOARD_METRICS,
    DATA_TABS,
    LEAD_ID,
    LEADS_COLUMNS,
    LeadStatus,
    Tab,
    column_index,
)
from config.settings import settings

# Google APIs we need: Sheets (read/write cells) + Drive (open by key/share).
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# gspread raises APIError for 429/5xx; retry those a few times with backoff.
_retry_api = retry(
    reraise=True,
    retry=retry_if_exception_type(APIError),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(5),
)


class LeadRow:
    """
    A single lead read from the LEADS tab.

    Carries the parsed field dict plus `row_number` — the 1-based sheet row the
    data came from — so status/field updates target the exact row even if other
    rows change between read and write.
    """

    __slots__ = ("row_number", "data")

    def __init__(self, row_number: int, data: dict[str, Any]) -> None:
        self.row_number = row_number
        self.data = data

    # Convenience accessors used throughout the pipeline.
    @property
    def lead_id(self) -> str:
        return str(self.data.get(LEAD_ID, "")).strip()

    @property
    def email(self) -> str:
        return str(self.data.get("Email", "")).strip()

    @property
    def company(self) -> str:
        return str(self.data.get("Company", "")).strip()

    @property
    def website(self) -> str:
        return str(self.data.get("Website", "")).strip()

    @property
    def status(self) -> str:
        return str(self.data.get("Status", "")).strip()

    def get(self, key: str, default: str = "") -> str:
        return str(self.data.get(key, default)).strip()

    def __repr__(self) -> str:
        return f"LeadRow(row={self.row_number}, id={self.lead_id!r}, company={self.company!r}, status={self.status!r})"


class SheetsClient:
    """Thin, well-behaved wrapper around one CRM spreadsheet."""

    def __init__(self) -> None:
        self._gc: gspread.Client | None = None
        self._ss: gspread.Spreadsheet | None = None
        # cache: tab title -> {header name: 1-based column index}
        self._header_cache: dict[str, dict[str, int]] = {}

    # ── Connection ──────────────────────────────────────────────────
    @_retry_api
    def connect(self) -> None:
        """Authenticate and open the spreadsheet. Call once at startup."""
        creds = Credentials.from_service_account_file(
            str(settings.service_account_path()), scopes=_SCOPES
        )
        self._gc = gspread.authorize(creds)
        self._ss = self._gc.open_by_key(settings.sheets.sheet_id)

    @property
    def spreadsheet(self) -> gspread.Spreadsheet:
        if self._ss is None:
            raise RuntimeError("SheetsClient.connect() must be called first.")
        return self._ss

    # ── Bootstrap ───────────────────────────────────────────────────
    @_retry_api
    def ensure_schema(self) -> None:
        """
        Make sure every tab exists and has the correct header row. Idempotent:
        existing data is never touched; only missing tabs/headers are created.
        """
        existing = {ws.title: ws for ws in self.spreadsheet.worksheets()}

        # Data tabs (header + rows).
        for title, columns in DATA_TABS.items():
            ws = existing.get(title)
            if ws is None:
                ws = self.spreadsheet.add_worksheet(
                    title=title, rows=1000, cols=max(len(columns), 12)
                )
            self._ensure_header(ws, columns)

        # Dashboard is a computed layout, seeded with metric labels in column A.
        dash = existing.get(Tab.DASHBOARD)
        if dash is None:
            dash = self.spreadsheet.add_worksheet(title=Tab.DASHBOARD, rows=60, cols=8)
        self._ensure_dashboard_labels(dash)

        # Keep tabs in a sensible order (Dashboard first).
        self._reorder_tabs()

    def _ensure_header(self, ws: gspread.Worksheet, columns: list[str]) -> None:
        current = ws.row_values(1)
        if current[: len(columns)] != columns:
            end = rowcol_to_a1(1, len(columns))
            ws.update(f"A1:{end}", [columns], value_input_option="RAW")
        # refresh cache for this tab
        self._header_cache[ws.title] = {name: i + 1 for i, name in enumerate(columns)}

    def _ensure_dashboard_labels(self, ws: gspread.Worksheet) -> None:
        # Title + metric labels down column A, values go in column B (filled by
        # the dashboard builder in a later module).
        want_a = ["AI Cold Outreach — Dashboard", ""] + DASHBOARD_METRICS
        current_a = ws.col_values(1)
        if current_a[: len(want_a)] != want_a:
            cells = [[label] for label in want_a]
            end = rowcol_to_a1(len(want_a), 1)
            ws.update(f"A1:{end}", cells, value_input_option="RAW")

    @_retry_api
    def _reorder_tabs(self) -> None:
        desired = [Tab.DASHBOARD, Tab.LEADS, Tab.RESEARCH, Tab.EMAILS, Tab.CAMPAIGN, Tab.ACTIVITY_LOG]
        by_title = {ws.title: ws for ws in self.spreadsheet.worksheets()}
        ordered = [by_title[t] for t in desired if t in by_title]
        if ordered:
            self.spreadsheet.reorder_worksheets(ordered)

    # ── Header index helpers ────────────────────────────────────────
    def _headers(self, tab: str) -> dict[str, int]:
        if tab not in self._header_cache:
            ws = self.spreadsheet.worksheet(tab)
            names = ws.row_values(1)
            self._header_cache[tab] = {name: i + 1 for i, name in enumerate(names)}
        return self._header_cache[tab]

    # ── Reads ───────────────────────────────────────────────────────
    @_retry_api
    def get_all_leads(self) -> list[LeadRow]:
        """Every lead on the LEADS tab, with real row numbers (header = row 1)."""
        ws = self.spreadsheet.worksheet(Tab.LEADS)
        records = ws.get_all_records(expected_headers=LEADS_COLUMNS)
        return [LeadRow(row_number=i + 2, data=rec) for i, rec in enumerate(records)]

    def fetch_new_leads(self) -> list[LeadRow]:
        """
        Only leads whose Status is exactly "New". This is the resumability
        contract: completed/duplicate/failed rows are ignored, so every run
        continues from the next unprocessed lead instead of restarting.
        """
        processable = LeadStatus.processable()
        return [lead for lead in self.get_all_leads() if lead.status in processable]

    # ── Lead ID assignment ──────────────────────────────────────────
    def next_lead_id(self, existing_ids: list[str] | None = None) -> str:
        """
        Generate the next sequential Lead ID like "L0007". Pulls existing IDs if
        not supplied. Used when a freshly imported row has no ID yet.
        """
        if existing_ids is None:
            existing_ids = [lead.lead_id for lead in self.get_all_leads()]
        max_n = 0
        for lid in existing_ids:
            if lid.upper().startswith("L") and lid[1:].isdigit():
                max_n = max(max_n, int(lid[1:]))
        return f"L{max_n + 1:04d}"

    # ── Writes: LEADS tab ───────────────────────────────────────────
    @_retry_api
    def update_lead_fields(self, row_number: int, fields: dict[str, Any]) -> None:
        """
        Update named columns on a specific LEADS row. Always stamps
        "Last Updated". Batched into one API call.
        """
        headers = self._headers(Tab.LEADS)
        ws = self.spreadsheet.worksheet(Tab.LEADS)
        fields = {**fields, "Last Updated": _now_iso()}
        updates = []
        for name, value in fields.items():
            if name not in headers:
                continue
            a1 = rowcol_to_a1(row_number, headers[name])
            updates.append({"range": a1, "values": [[value]]})
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")

    def set_lead_status(self, row_number: int, status: LeadStatus | str, **extra: Any) -> None:
        """Convenience: set Status (+ any extra fields) on a LEADS row."""
        value = status.value if isinstance(status, LeadStatus) else status
        self.update_lead_fields(row_number, {"Status": value, **extra})

    def assign_lead_id(self, row_number: int, lead_id: str) -> None:
        self.update_lead_fields(row_number, {LEAD_ID: lead_id})

    # ── Writes: lead-scoped tabs (Research / Emails / Campaign) ──────
    @_retry_api
    def upsert_by_lead_id(self, tab: str, lead_id: str, fields: dict[str, Any]) -> None:
        """
        Insert or update the row for `lead_id` on a lead-scoped tab. If no row
        with that Lead ID exists yet, one is appended; otherwise the named
        columns are updated in place. Keeps Research/Emails/Campaign in sync
        with LEADS without duplicating rows.
        """
        ws = self.spreadsheet.worksheet(tab)
        headers = self._headers(tab)
        id_col = headers[LEAD_ID]

        # Locate existing row for this lead id (search column A only).
        col_values = ws.col_values(id_col)
        target_row: int | None = None
        for idx, val in enumerate(col_values[1:], start=2):  # skip header
            if str(val).strip() == lead_id:
                target_row = idx
                break

        payload = {LEAD_ID: lead_id, **fields}

        if target_row is None:
            # Build a full-width row in header order and append.
            width = max(headers.values())
            row = [""] * width
            for name, value in payload.items():
                if name in headers:
                    row[headers[name] - 1] = value
            ws.append_row(row, value_input_option="USER_ENTERED")
        else:
            updates = []
            for name, value in payload.items():
                if name not in headers:
                    continue
                a1 = rowcol_to_a1(target_row, headers[name])
                updates.append({"range": a1, "values": [[value]]})
            if updates:
                ws.batch_update(updates, value_input_option="USER_ENTERED")

    # ── Generic reads for other tabs ────────────────────────────────
    @_retry_api
    def get_rows(self, tab: str, expected_headers: list[str] | None = None) -> list[tuple[int, dict[str, Any]]]:
        """
        Return (row_number, record) pairs for a tab. Row numbers are 1-based and
        account for the header row, so callers can update the exact row.
        """
        ws = self.spreadsheet.worksheet(tab)
        records = ws.get_all_records(expected_headers=expected_headers) if expected_headers \
            else ws.get_all_records()
        return [(i + 2, rec) for i, rec in enumerate(records)]

    def get_row_by_lead_id(self, tab: str, lead_id: str) -> dict[str, Any] | None:
        """Fetch a single lead-scoped row's data by Lead ID, or None."""
        for _, rec in self.get_rows(tab):
            if str(rec.get(LEAD_ID, "")).strip() == lead_id:
                return rec
        return None

    # ── Dashboard ───────────────────────────────────────────────────
    @_retry_api
    def update_dashboard(self, metrics: dict[str, Any]) -> None:
        """
        Write metric values into column B next to their labels. Labels were
        seeded in column A by ensure_schema (title in row 1, blank row 2, then
        the metrics), so value for metric i sits at B{3+i}.
        """
        ws = self.spreadsheet.worksheet(Tab.DASHBOARD)
        updates = []
        for i, label in enumerate(DASHBOARD_METRICS):
            if label in metrics:
                a1 = rowcol_to_a1(3 + i, 2)  # column B
                updates.append({"range": a1, "values": [[metrics[label]]]})
        # Stamp last-updated at B1.
        updates.append({"range": "B1", "values": [[f"Updated {_now_iso()} UTC"]]})
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")

    # ── Activity log ────────────────────────────────────────────────
    @_retry_api
    def log_activity(
        self, lead: str, company: str, action: str, result: str, notes: str = ""
    ) -> None:
        """Append one audit row to the Activity Log. Append-only by design."""
        ws = self.spreadsheet.worksheet(Tab.ACTIVITY_LOG)
        row = [_now_iso(), lead, company, action, result, notes]
        # Guard against schema drift: trim/pad to the header width.
        width = len(ACTIVITY_LOG_COLUMNS)
        row = (row + [""] * width)[:width]
        ws.append_row(row, value_input_option="USER_ENTERED")


# ── module-level helpers ────────────────────────────────────────────
def _now_iso() -> str:
    """UTC timestamp, second precision, sortable."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
