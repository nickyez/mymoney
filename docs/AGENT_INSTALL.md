# Agent Install Tutorial — MyMoney

This guide is written for an **AI coding agent** (Claude Code, Codex, OpenCode, etc.) that has shell access to a target server and is asked to install, verify, or fix MyMoney. It tells you what to check, what to run, and — most importantly — what **not** to do. Follow it from top to bottom; do not skip preconditions.

> For a human-oriented setup, see the top-level [`README.md`](../README.md).

---

## 1. Architecture (know what you're standing up)

MyMoney is three pieces running against one SQLite database:

| Piece | Tech | Starts with |
|---|---|---|
| API + dashboard | FastAPI + React (built to `web/frontend/dist`) | `uvicorn app:app` |
| Email watcher | Gmail IMAP IDLE daemon | `python tracker_daemon.py` |
| Data store | SQLite at `web/transactions.db` (or `$EXPENSE_DB`) | — |

The watcher parses supported incoming email and writes transactions to SQLite **and** a Google Sheet. The dashboard never writes to Gmail or Sheets directly; imports are preview-first and read-only for Gmail.

**Source of truth:** SQLite. Everything else (Sheets, Gmail) is an input/output around it.

---

## 2. Preconditions — verify before touching anything

Run these checks first. If any fail, **stop and report** rather than guessing.

```bash
python3 --version        # need >= 3.11
node --version           # need 20.19+ or 22.12+ (Vite 8 requirement)
npm --version            # need >= 10
git status               # confirm clean/known working tree
```

**Credentials the user must supply before install.** You cannot invent these; ask the user for them:

- **Gmail account** with IMAP enabled and a **Gmail app password** (16-char Google-generated; *not* the normal account password).
- **Optional** — Google service-account JSON key + spreadsheet ID (only for Sheets mirroring). If omitted, the app works in Gmail-only mode: manual tracking and email capture function normally, Sheets sync is unavailable.
- **Spreadsheet ID** of a Google Sheet shared with the service account as **Editor** (only if Sheets mirroring is desired).

If the user has not set these up, follow the full walkthrough in [`docs/GOOGLE_SETUP.md`](../docs/GOOGLE_SETUP.md) — it covers the Google Cloud account, Sheets API, service account key, Gmail app password, and spreadsheet preparation. The only Cloud API required is the Google Sheets API; the app uses **IMAP** for Gmail, not the Gmail API.

---

## 3. Install sequence (run in order)

From the repository root:

### Step 1 — Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` is pinned; do not invent newer versions. If a pin fails to resolve on the target platform, stop and report the exact error.

### Step 2 — Frontend build

```bash
cd web/frontend
npm ci              # clean install from lockfile — do NOT use npm install here
npm run build
cd ../..
```

The build must produce `web/frontend/dist/`. If it fails, capture the error; do not "fix" by removing dependencies or switching to a different bundler.

### Step 3 — Dashboard password

```bash
cd web/backend
../../.venv/bin/python password_setup.py
```

This prompts for a password and **refuses to overwrite** an existing one. If a password is already configured, it is not a failure — move on.

### Step 4 — Credentials

Create `config.json` in the repository root with **real values the user gave you**. Do **not** commit it, echo it, or share it:

```json
{
  "gmail_user": "user@gmail.com",
  "gmail_app_password": "<16-char app password>",
  "senders": ["bank-notifications@example.com"],
  "spreadsheet_id": "<your spreadsheet id>"
}
```

Set restrictive permissions:

```bash
chmod 600 config.json service_account.json
```

Both `config.json` and `service_account.json` must be **outside git** — confirm `git check-ignore config.json service_account.json` reports matches (they are in `.gitignore`). If they are not ignored, add them to `.gitignore` before any commit.

### Step 5 — Run the API

```bash
cd web/backend
../../.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8017
```

In production run this under systemd bound to loopback and put a reverse proxy (Nginx + HTTPS) in front. **Never expose Uvicorn directly to the public internet.**

### Step 6 — Verify (non-negotiable)

```bash
curl -fsS http://127.0.0.1:8017/api/health
# {"status":"ok"}
```

Then run the smoke tests — they use mocked Gmail/Sheets and do **not** touch live accounts:

```bash
cd web/backend
../../.venv/bin/python -m py_compile app.py tests/sync_smoke.py
../../.venv/bin/python tests/sync_smoke.py
```

An install is **not complete** until: health returns `ok`, smoke tests pass, and Settings on the live dashboard shows both Gmail and Sheets connection checks passing.

### Step 7 — Email watcher (optional for local testing)

```bash
cd /path/to/expense-tracker
source .venv/bin/activate
python tracker_daemon.py   # long-running; Ctrl+C to stop
```

In production run it as a separate systemd service (`expense-daemon.service`) — adapt paths and security settings to the host.

---

## 4. Credentials handling rules (agents MUST follow)

1. **Never** write a real secret (app password, service-account private key, token) into: code, commit messages, chat summaries, logs piped somewhere the user asked you to share, or any file that gets committed.
2. **Never** run `git add -A` / `git add .` after creating `config.json` or `service_account.json`. Always `git add` specific files or `git status` first.
3. If you must show a value to debug, redact it: show `gmail_app_password: ****` or the last 4 chars at most.
4. If a secret was committed anywhere in history, do not just delete the file — history must be rewritten (and the user should rotate the secret).
5. `chmod 600` on credential files is part of the install, not optional.
6. Failed-email logs (`failed_emails.log`) and databases may contain **email contents** — treat them as sensitive; never commit them.

---

## 5. Non-destructive checks during install/fix

- Health endpoint and smoke tests are safe to run anytime.
- The Gmail and Sheets **Test connection** buttons in Settings are read-only (Gmail uses IMAP login + `BODY.PEEK[]`, no marking as read; Sheets only checks the configured service account).
- Import flows are preview-first: nothing writes until the user selects rows. Do not auto-apply imports.
- Do not delete or overwrite dashboard transactions because a row is missing from Gmail/Sheets — that is intended behavior.
- `password_setup.py` refuses to overwrite; do not bypass it.

---

## 6. Production hardening checklist

- [ ] Uvicorn behind nginx reverse proxy with HTTPS (or equivalent).
- [ ] `config.json`, `service_account.json` readable only by the service user (`chmod 600`, not web-served).
- [ ] Email watcher as its own systemd service (`expense-daemon.service`), not run inside the web server process.
- [ ] Database (`web/transactions.db`) backed up before upgrades/imports (SQLite backup API or consistent snapshot).
- [ ] Gmail app password restricted to the one app; revoke from Google Account if the server is retired.
- [ ] Service-account key never leaves the server.

---

## 7. Troubleshooting (self-recovery before reporting)

| Symptom | Likely cause | Action |
|---|---|---|
| `/api/health` not `ok` | Backend not running / wrong venv | Check `uvicorn` process, restart service, read service log |
| Settings save → server error | `config.json` invalid JSON | Validate JSON, look for the traceback in the API log |
| Gmail connection fails | IMAP off, app password stale, wrong env | Confirm IMAP enabled + current app password; check the API service uses `.venv` Python |
| Sheets connection fails | Bad ID, key, API disabled, missing Editor share | Verify spreadsheet ID, key at `service_account.json`, Sheets API enabled, Editor share, `Expenses` header |
| Sheet not auto-initialized | Existing worksheet data / unexpected header | App deliberately avoids changing it — correct the sheet manually, test again |
| Frontend changes missing | Stale `dist` | Rebuild (`npm ci && npm run build`), redeploy, hard reload |
| Row shows as conflict | Mismatch vs SQLite record | Compare all fields with the SQLite record; conflicts are review-only |
| Watcher silently not writing | Can't read credentials, wrong python | Check watcher service log + file perms on credentials |

Report to the user only what you could not resolve, with the exact error and what you verified.

---

## 8. What the agent should NOT do

- Do not invent or fabricate credentials, IDs, or test results.
- Do not push commits that contain credential files, `.bak` files, diagnostics, or databases.
- Do not run `git push --force` without explicit user approval.
- Do not expose the app on a public port without a reverse proxy.
- Do not delete rows from the spreadsheet or dashboard as "cleanup" — imports are user-reviewable by design.
