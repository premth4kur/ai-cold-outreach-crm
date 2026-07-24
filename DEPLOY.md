# Deploy & run guide

You have three ways to run this. Pick based on what you need. **The dashboard on
your own PC is the recommended default** — it keeps your email password and API
keys on your machine and gives you a live control panel.

---

## Why not "just GitHub Pages"?

GitHub Pages only serves **static** files (HTML/CSS/JS). This system needs a
**Python backend** to research sites, call the AI, send via SMTP, read replies
via IMAP, and write to Google Sheets. A static page can't do any of that or hold
your secrets safely. So:

- **Your code** lives on GitHub (version control, backup, the Actions workflow).
- **The running app** lives either on your PC (dashboard) or on GitHub's runners
  (scheduled Action) or a cloud host.

---

## Option A — Local dashboard (recommended)

A friendly browser control panel on your own computer.

```bat
:: from the OutreachCRM folder, just double-click:
start_dashboard.bat
```

That creates a virtual environment, installs dependencies, opens
`http://127.0.0.1:5000`, and launches the dashboard. From there you can:

- see live metrics, the leads table, and the activity feed,
- preview the generated emails per lead,
- hit **Run now** (with a **Dry run** toggle) to launch the pipeline and watch
  the log stream live.

First-time setup (once): copy `.env.example` to `.env`, fill it in, and drop
`service_account.json` into `credentials/` (see `README.md` and
`credentials/README.txt`).

---

## Option B — Put the code on GitHub

From the `OutreachCRM` folder, in a terminal:

```bash
git init
git add .
git commit -m "AI Cold Outreach CRM"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

Your `.env`, `credentials/`, and `logs/` are already git-ignored, so **no secrets
are uploaded**. Verify on GitHub that those are absent before making the repo
public.

---

## Option C — Automatic scheduled runs (GitHub Actions)

`.github/workflows/scheduled-run.yml` runs the pipeline on a schedule on GitHub's
servers, so your PC doesn't need to be on.

1. Push the repo (Option B).
2. On GitHub: **Settings → Secrets and variables → Actions → New repository
   secret**, and add each secret listed at the top of the workflow file
   (paste the entire `service_account.json` contents into
   `GOOGLE_SERVICE_ACCOUNT_JSON`).
3. The job runs on the cron schedule, or trigger it manually from the **Actions**
   tab → *Scheduled Outreach Run* → **Run workflow**.

> Deliverability note: GitHub runners send from shared cloud IPs. For cold email
> reputation, sending from your own machine (Option A) or a dedicated host is
> usually better. Keep `MAX_SENDS_PER_RUN` modest.

---

## Option D — Host the dashboard in the cloud (advanced)

If you want the dashboard reachable from any device by URL, deploy it to a host
that runs Python (Render, Railway, Fly.io). Use `waitress`/gunicorn to serve
`dashboard.app:app`, set all secrets as environment variables in the host's
dashboard (not in a committed `.env`), and protect the URL with authentication
before exposing it publicly. Ask and I can add a `Procfile` / Dockerfile for a
specific host.

---

## Sanity checks before your first real send

1. `python -m unittest discover -s tests -v` — offline logic tests pass.
2. `python init_crm.py` — creates the six CRM tabs.
3. In `.env` set `DRY_RUN=true`, run once from the dashboard — confirm research,
   emails, and CRM writes look right, with nothing actually sent.
4. Set `DRY_RUN=false` and start for real.
