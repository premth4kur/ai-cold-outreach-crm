# AI Cold Outreach CRM

One-click, resumable cold-outreach automation. Google Sheets is the master CRM;
the pipeline reads new leads, researches each company, writes personalized
emails plus two follow-ups, sends via Hostinger SMTP, detects replies via IMAP,
and writes every action back to the sheet.

> **Status:** all six modules complete. `python main.py` runs the full pipeline.

---

## How it runs

You import new leads into Google Sheets, then run one command:

```bash
python main.py
```

Each run: reads the CRM → finds only `New` leads → researches → generates email +
follow-ups → validates → sends (with random 4–10 min gaps) → updates the CRM →
logs everything → moves to the next lead. It also sends any follow-ups that are
due and skips anyone who has replied. Runs are resumable: state lives in the
sheet, so a crash never restarts from row 1 or double-sends.

---

## Project layout

```
outreach_crm/
├── main.py                 # one-click orchestrator            [✓]
├── init_crm.py             # one-off: create all CRM tabs       [✓]
├── requirements.txt
├── .env.example            # copy to .env and fill in
├── .gitignore
├── config/
│   ├── settings.py         # loads/validates .env (secrets masked)   [✓]
│   └── schema.py           # CRM tabs, columns, statuses             [✓]
├── core/
│   ├── sheets.py           # Google Sheets CRM read/write + bootstrap [✓]
│   ├── dedupe.py           # duplicate detection                      [✓]
│   ├── research.py         # website + official email discovery       [✓]
│   ├── ai_client.py        # Anthropic/OpenAI wrapper                 [✓]
│   ├── email_generator.py  # analysis + subject + email + FU1 + FU2   [✓]
│   ├── validator.py        # pre-send gate                            [✓]
│   ├── smtp_client.py      # Hostinger SMTP send + retry              [✓]
│   ├── imap_client.py      # reply detection                          [✓]
│   └── scheduler.py        # follow-up due dates + reply skip         [✓]
├── utils/
│   ├── logger.py           # daily/smtp/activity/error logs           [✓]
│   └── delays.py           # random 4–10 min send delays              [✓]
├── tests/                  # offline unit tests (no network)
├── credentials/            # service_account.json (git-ignored)
└── logs/                   # created at runtime (git-ignored)

## Running

**Friendly dashboard (recommended):** double-click `start_dashboard.bat`, then
open `http://127.0.0.1:5000`. Live metrics, leads table, per-lead email preview,
activity feed, and a **Run now** button with a **Dry run** toggle — all from the
browser, running on your own PC. See `DEPLOY.md` for GitHub + hosting options.

**Command line:**

```bash
python init_crm.py     # once: build the six CRM tabs
python main.py         # each day, after importing new leads
python -m dashboard.app        # or launch the dashboard directly

# rehearse safely without sending: set DRY_RUN=true in .env first
# run the offline tests:
python -m unittest discover -s tests -v
```
```

---

## Setup

### 1. Install Python dependencies

```bash
cd outreach_crm
python -m venv .venv
.venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
copy .env.example .env         # Windows
```

Then open `.env` and fill in: Google Sheet ID, AI provider + API key, Hostinger
SMTP and IMAP credentials, and your sender email. Nothing is hard-coded; all
secrets live only in `.env` (git-ignored).

### 3. Google service account

Follow `credentials/README.txt`, then share your CRM sheet with the service
account's email as an **Editor**.

### 4. Verify config (safe — secrets are masked)

```bash
python -m config.settings
```

This prints the loaded configuration with every secret masked, so you can
confirm your `.env` is read correctly without exposing anything.

---

## The CRM tabs

| Tab | Purpose |
|-----|---------|
| **Dashboard** | Live metrics, progress bars, charts, recent activity |
| **Leads** | Master lead list + `Status` that drives all processing |
| **Research** | Website summary, the one genuine observation, opportunity, evidence |
| **Emails** | Subject, Email 1, Follow-up 1, Follow-up 2, word count, validation |
| **Campaign** | Send timestamps, SMTP status, message IDs, follow-up due dates, replies |
| **Activity Log** | Append-only audit trail of every action |

Exact columns and the allowed `Status` values are defined once in
`config/schema.py`.

---

## Security

- All secrets come from `.env`; none are committed (`.gitignore` covers `.env`,
  `credentials/`, and `*.json`).
- `config/settings.py` masks secrets in every `__repr__`, so they never leak
  into logs or stack traces.
