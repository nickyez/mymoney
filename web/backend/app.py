from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from auth import check_csrf, hash_password, login as do_login, logout as do_logout, require_auth
from db import DB_PATH, connect, init_db, transaction

BASE = Path(__file__).resolve().parent
WEB = BASE.parent
ROOT = BASE.parent.parent
CONFIG_FILE = Path(os.environ.get("MYMONEY_CONFIG", ROOT / "config.json"))
SHEET_TAB = "Expenses"
SHEET_COLUMNS = ["Date", "Source", "Merchant", "Amount", "Category",
                 "Payment Method", "Email Link"]

app = FastAPI(title="MyMoney API", docs_url=None, redoc_url=None)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class SetupBody(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class SettingsBody(BaseModel):
    gmail_user: str = Field(min_length=3, max_length=254)
    senders: list[str] = Field(min_length=1, max_length=30)
    spreadsheet_id: str = Field(default="", max_length=200)
    gmail_app_password: str | None = Field(default=None, max_length=256)

    @field_validator("gmail_user")
    @classmethod
    def validate_gmail_user(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid Gmail address")
        return value

    @field_validator("senders")
    @classmethod
    def validate_senders(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(v.strip().lower() for v in values if str(v).strip()))
        if not cleaned or any(not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) for v in cleaned):
            raise ValueError("Provide at least one valid sender email address")
        return cleaned

    @field_validator("spreadsheet_id")
    @classmethod
    def validate_spreadsheet_id(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""  # Sheets mirroring is optional
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", value):
            raise ValueError("Invalid Google Spreadsheet ID")
        return value


class ConnectionCheckBody(BaseModel):
    spreadsheet_id: str | None = Field(default=None, min_length=20, max_length=200)

    @field_validator("spreadsheet_id")
    @classmethod
    def validate_connection_spreadsheet_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", value.strip()):
            raise ValueError("Invalid Google Spreadsheet ID")
        return value.strip() if value else value


class SheetSyncBody(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)
    apply_ids: list[str] = Field(default_factory=list, max_length=500)
    start: date | None = None
    end: date | None = None


class GmailSyncBody(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)
    apply_ids: list[str] = Field(default_factory=list, max_length=500)


class TxBody(BaseModel):
    date: date
    kind: Literal["income", "expense"]
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    description: str = Field(min_length=1, max_length=180)
    category: str = Field(min_length=1, max_length=50)
    payment_method: str = Field(default="", max_length=60)
    notes: str = Field(default="", max_length=1000)

    @field_validator("description", "category", "payment_method", "notes")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


def _get_setting(key: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _require_auth(request: Request):
    check_csrf(request)
    return require_auth(request)


def _serialize(row):
    result = dict(row)
    result["amount"] = float(result["amount"])
    return result


def _json_error(status: int, msg: str):
    return JSONResponse({"detail": msg}, status_code=status)


@app.on_event("startup")
def startup():
    init_db()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/auth/status")
def auth_status(request: Request):
    try:
        require_auth(request)
        return {"authenticated": True, "setup_required": False}
    except HTTPException:
        return {"authenticated": False, "setup_required": not bool(_get_setting("password_hash"))}


@app.post("/api/auth/setup")
def setup(body: SetupBody, request: Request, response: Response):
    check_csrf(request)
    if _get_setting("password_hash"):
        raise HTTPException(409, "Password already configured")
    with transaction() as db:
        # Re-check inside the write transaction to avoid a race double-insert.
        if db.execute("SELECT 1 FROM settings WHERE key='password_hash'").fetchone():
            raise HTTPException(409, "Password already configured")
        password_hash = hash_password(body.password)
        db.execute("INSERT INTO settings(key,value) VALUES('password_hash',?)",
                   (password_hash,))
    do_login(body.password, password_hash, request.client.host if request.client else "unknown", response)
    return {"setup_complete": True}


@app.post("/api/auth/login")
def auth_login(body: LoginBody, request: Request, response: Response):
    hashed = _get_setting("password_hash")
    if not hashed:
        raise HTTPException(409, "Initial setup required")
    do_login(body.password, hashed, request.client.host if request.client else "unknown", response)
    return {"authenticated": True}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response, _=Depends(_require_auth)):
    do_logout(request, response)
    return {"authenticated": False}


def _read_config() -> dict:
    """Read config.json, treating missing or malformed files as unconfigured."""
    try:
        value = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@app.get("/api/settings")
def read_settings(_=Depends(_require_auth)):
    cfg = _read_config()
    return {
        "gmail_user": cfg.get("gmail_user", ""),
        "senders": cfg.get("senders", []),
        "spreadsheet_id": cfg.get("spreadsheet_id", ""),
        "spreadsheet_configured": bool(cfg.get("spreadsheet_id")),
        "gmail_app_password_configured": bool(cfg.get("gmail_app_password")),
        "gmail_app_password_masked": "configured" if cfg.get("gmail_app_password") else "",
        "sheets_auth": "service_account",
        "sheets_key_configured": (ROOT / "service_account.json").is_file(),
        "service_account_email": _service_account_email(),
        "note": "Use the server-side service account JSON for Sheets access. Private keys are never sent to the browser.",
    }


@app.put("/api/settings")
def update_settings(body: SettingsBody, _=Depends(_require_auth)):
    cfg = _read_config()
    # Preserve unrelated keys and never return stored credentials in API responses.
    cfg["gmail_user"] = body.gmail_user
    cfg["senders"] = body.senders
    cfg["spreadsheet_id"] = body.spreadsheet_id
    if body.gmail_app_password:
        cfg["gmail_app_password"] = body.gmail_app_password.replace(" ", "")
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(CONFIG_FILE)
    except OSError as exc:
        # e.g. config.json is mounted read-only into the container
        raise HTTPException(500, f"Could not write config.json on the server: {exc}") from exc
    # Reload mailbox settings promptly. On hosts without systemd (e.g. Docker)
    # the daemon either watches config.json live or restarts on next loop, so a
    # missing systemd is not an error — only a real restart failure is.
    daemon_restarted = False
    try:
        subprocess.run(["/usr/bin/systemctl", "restart", "expense-daemon.service"],
                       check=True, timeout=20, capture_output=True, text=True)
        daemon_restarted = True
    except (subprocess.SubprocessError, OSError):
        # unit missing or systemd absent — leave to Docker/relaunch paths
        pass
    return {"saved": True, "watching": body.senders, "gmail_user": body.gmail_user,
            "spreadsheet_id": body.spreadsheet_id, "daemon_restarted": daemon_restarted}


def _service_account_email() -> str | None:
    key_file = ROOT / "service_account.json"
    if not key_file.is_file():
        return None
    try:
        return json.loads(key_file.read_text())["client_email"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _sheets_client():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise HTTPException(503, "Google Sheets dependencies are unavailable") from exc
    key_file = ROOT / "service_account.json"
    if not key_file.is_file():
        raise HTTPException(503, "Google Sheets service-account key is not configured")
    try:
        credentials = Credentials.from_service_account_file(
            str(key_file), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return gspread.authorize(credentials), credentials.service_account_email
    except Exception as exc:
        raise HTTPException(502, "Could not load the Google Sheets service account") from exc


def _check_sheets_connection(spreadsheet_id: str | None = None, initialize: bool = True):
    cfg = _read_config()
    spreadsheet_id = (spreadsheet_id or cfg.get("spreadsheet_id", "")).strip()
    if not spreadsheet_id:
        raise HTTPException(422, "A Google Spreadsheet ID is not configured")
    client, service_email = _sheets_client()
    try:
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            worksheet = spreadsheet.worksheet(SHEET_TAB)
        except Exception:
            worksheets = spreadsheet.worksheets()
            workbook_has_data = any(
                str(value).strip()
                for tab in worksheets
                for row in tab.get_all_values()
                for value in row
            )
            if workbook_has_data:
                return {"connected": False, "initialized": False, "service_account_email": service_email,
                        "detail": "Spreadsheet has existing data but no Expenses tab; nothing was changed."}
            if not initialize:
                return {"connected": False, "initialized": False, "service_account_email": service_email,
                        "detail": "Expenses tab is missing."}
            worksheet = spreadsheet.add_worksheet(SHEET_TAB, rows=200, cols=len(SHEET_COLUMNS))
            worksheet.update(range_name="A1", values=[SHEET_COLUMNS])
            return {"connected": True, "initialized": True, "service_account_email": service_email,
                    "worksheet": SHEET_TAB, "detail": "Created Expenses tab and required header."}
        values = worksheet.get_all_values()
        if not values:
            worksheet.update(range_name="A1", values=[SHEET_COLUMNS])
            return {"connected": True, "initialized": True, "service_account_email": service_email,
                    "worksheet": SHEET_TAB, "detail": "Added required header to empty Expenses tab."}
        header = [str(value).strip().lower() for value in values[0][:len(SHEET_COLUMNS)]]
        if header != [value.lower() for value in SHEET_COLUMNS]:
            return {"connected": False, "initialized": False, "service_account_email": service_email,
                    "detail": "Expenses tab has an unexpected header; nothing was changed."}
        return {"connected": True, "initialized": False, "service_account_email": service_email,
                "worksheet": SHEET_TAB, "detail": "Connected to Expenses tab."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Could not access the configured Google Sheet") from exc


def _check_gmail_connection():
    import imaplib
    cfg = _read_config()
    if not cfg.get("gmail_user") or not cfg.get("gmail_app_password"):
        raise HTTPException(422, "Gmail account or app password is not configured")
    mailbox = None
    try:
        mailbox = imaplib.IMAP4_SSL("imap.gmail.com")
        mailbox.login(cfg["gmail_user"], cfg["gmail_app_password"])
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise HTTPException(502, "Gmail login succeeded, but INBOX could not be opened read-only")
        return {"connected": True, "gmail_user": cfg["gmail_user"], "detail": "Connected to Gmail INBOX in read-only mode."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Could not connect to Gmail") from exc
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except Exception:
                pass


@app.post("/api/sheets-connection")
def sheets_connection(body: ConnectionCheckBody, _=Depends(_require_auth)):
    return _check_sheets_connection(body.spreadsheet_id)


@app.get("/api/gmail-connection")
def gmail_connection(_=Depends(_require_auth)):
    return _check_gmail_connection()


def _sheet_services():
    cfg = _read_config()
    spreadsheet_id = (cfg.get("spreadsheet_id") or "").strip()
    if not spreadsheet_id:
        raise HTTPException(422, "A Google Spreadsheet ID is not configured")
    client, _ = _sheets_client()
    try:
        return client.open_by_key(spreadsheet_id).worksheet(SHEET_TAB)
    except Exception as exc:
        raise HTTPException(502, "Could not read the configured Google Sheet") from exc


def _sheet_amount(value: str) -> Decimal:
    value = (value or "").replace("Rp", "").replace("IDR", "").replace(" ", "").strip()
    if not value:
        raise ValueError("amount missing")
    normalized = value.replace(".", "").replace(",", ".") if "," in value else value.replace(".", "")
    amount = Decimal(normalized)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be positive")
    return amount.quantize(Decimal("0.01"))


def _sheet_email_id(link: str) -> str | None:
    match = re.search(r"rfc822msgid(?::|%3A)(.+)$", link or "", re.I)
    if not match:
        return None
    raw = unquote(match.group(1)).strip()
    return raw if raw.startswith("<") and raw.endswith(">") else None


def _normalized_sheet_row(row: list[str], row_number: int):
    padded = (row + [""] * len(SHEET_COLUMNS))[:len(SHEET_COLUMNS)]
    if not any(value.strip() for value in padded):
        return None
    try:
        tx_date = date.fromisoformat(padded[0].strip()).isoformat()
        amount = _sheet_amount(padded[3])
        category = padded[4].strip().lower() or "uncategorized"
        kind = "income" if category == "income" else "expense"
        source = padded[1].strip() or "sheet-import"
        description = padded[2].strip() or source or "Transaction"
        payment = padded[5].strip()
        link = padded[6].strip()
        message_id = _sheet_email_id(link)
        key = message_id or "sheet:" + hashlib.sha256(
            json.dumps([tx_date, kind, str(amount), description, category, payment, source],
                       ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return {"key": key, "date": tx_date, "kind": kind, "amount": str(amount),
                "description": description, "category": category, "payment_method": payment,
                "source": source, "email_link": link, "email_message_id": message_id,
                "row_number": row_number}
    except (ValueError, InvalidOperation) as exc:
        return {"row_number": row_number, "error": str(exc), "raw": padded[:7]}


def _preview_sheet_sync(start: date | None = None, end: date | None = None):
    if start and end and start > end:
        raise HTTPException(422, "Start date must be on or before end date")
    worksheet = _sheet_services()
    values = worksheet.get_all_values()
    if not values:
        raise HTTPException(422, "The Expenses tab is empty")
    header = [str(value).strip().lower() for value in values[0][:len(SHEET_COLUMNS)]]
    expected = [value.lower() for value in SHEET_COLUMNS]
    if header != expected:
        raise HTTPException(422, "Unexpected Expenses header; expected the existing seven-column layout")

    parsed, errors = [], []
    for row_number, raw in enumerate(values[1:], start=2):
        item = _normalized_sheet_row([str(value) for value in raw], row_number)
        if not item:
            continue
        if "error" in item:
            errors.append(item)
        elif start and item["date"] < start.isoformat():
            continue
        elif end and item["date"] > end.isoformat():
            continue
        else:
            parsed.append(item)

    counts, items = {}, []
    db_rows = {}
    with connect() as db:
        rows = db.execute("SELECT * FROM transactions WHERE email_message_id IS NOT NULL").fetchall()
        for row in rows:
            db_rows[row["email_message_id"]] = dict(row)
        for row in db.execute("SELECT * FROM transactions WHERE source='sheet-import'").fetchall():
            db_rows[row["email_message_id"]] = dict(row)

    seen_keys = set()
    for sheet_row in parsed:
        key = sheet_row["key"]
        if key in seen_keys:
            status = "duplicate"
            counts[status] = counts.get(status, 0) + 1
            items.append({**sheet_row, "status": status, "detail": "Duplicate identity in sheet"})
            continue
        seen_keys.add(key)
        local = db_rows.get(key)
        if local:
            comparable = (local["date"], local["kind"], Decimal(str(local["amount"])),
                          local["description"].strip(), local["category"].strip().lower(),
                          local["payment_method"].strip())
            from_sheet = (sheet_row["date"], sheet_row["kind"], Decimal(sheet_row["amount"]),
                          sheet_row["description"].strip(), sheet_row["category"],
                          sheet_row["payment_method"].strip())
            status = "matched" if comparable == from_sheet else "conflict"
            item = {**sheet_row, "status": status, "transaction_id": local["id"]}
            if status == "conflict":
                item["local"] = {"date": local["date"], "kind": local["kind"],
                                 "amount": float(local["amount"]), "description": local["description"],
                                 "category": local["category"], "payment_method": local["payment_method"]}
        else:
            status = "new"
            item = {**sheet_row, "status": status}
        counts[status] = counts.get(status, 0) + 1
        if status in ("new", "matched", "conflict", "duplicate"):
            items.append(item)

    token = hashlib.sha256(os.urandom(32)).hexdigest()
    with transaction() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                   ("sheet_sync_preview:" + token, json.dumps(items, separators=(",", ":"))))
    return {"preview_token": token, "counts": counts, "items": items[:500],
            "errors": errors[:100], "error_count": len(errors), "total_rows": len(parsed)}


def _preview_gmail_sync(start: date | None = None, end: date | None = None):
    if start and end and start > end:
        raise HTTPException(422, "Start date must be on or before end date")
    try:
        import email
        import email.utils
        import imaplib
        import urllib.parse
        import sys
        sys.path.insert(0, str(ROOT))
        import tracker_daemon
    except ImportError as exc:
        raise HTTPException(503, "Gmail sync dependencies are unavailable") from exc

    cfg = _read_config()
    senders = [str(value).strip().lower() for value in cfg.get("senders", []) if str(value).strip()]
    if not senders:
        raise HTTPException(422, "No Gmail sender addresses are configured")
    rows, errors = [], []
    mailbox = None
    try:
        mailbox = imaplib.IMAP4_SSL("imap.gmail.com")
        mailbox.login(cfg["gmail_user"], cfg["gmail_app_password"])
        mailbox.select("INBOX", readonly=True)
        since = start.strftime("%d-%b-%Y") if start else "01-Jan-2000"
        before = (end.fromordinal(end.toordinal() + 1).strftime("%d-%b-%Y") if end else None)
        for sender in senders:
            query = f'(SINCE {since} FROM "{sender}")'
            if before:
                query = f'(SINCE {since} BEFORE {before} FROM "{sender}")'
            status, data = mailbox.search(None, query)
            if status != "OK":
                continue
            for sequence in data[0].split():
                status, fetched = mailbox.fetch(sequence, "(BODY.PEEK[])")
                if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    continue
                message = email.message_from_bytes(fetched[0][1])
                message_id = (message.get("Message-ID") or "").strip()
                import email.header
                subject = str(email.header.make_header(email.header.decode_header(message.get("Subject", ""))))
                parsed_date = email.utils.parsedate_to_datetime(message.get("Date", ""))
                tx_date = parsed_date.date()
                if start and tx_date < start or end and tx_date > end:
                    continue
                email_link = ("https://mail.google.com/mail/u/0/#search/" + urllib.parse.quote("rfc822msgid:" + message_id)
                              if message_id else "")
                if not message_id:
                    errors.append({"sender": sender, "subject": subject, "date": tx_date.isoformat(),
                                   "error": "Message-ID missing", "reason": "Gmail message has no Message-ID"})
                    continue
                body = tracker_daemon.get_body(message)
                if tracker_daemon.CANCEL_RE.search(body):
                    reason = "Cancellation or refund notification is skipped"
                elif not tracker_daemon.AMOUNT_RE.search(body):
                    reason = "Transaction amount was not found in the message"
                else:
                    reason = "Message format is not recognized by the transaction parser"
                result = tracker_daemon.extract(sender, body)
                if not result:
                    errors.append({"sender": sender, "message_id": message_id, "subject": subject,
                                   "date": tx_date.isoformat(), "email_link": email_link,
                                   "error": "Could not parse transaction", "reason": reason})
                    continue
                source, description, amount, category = result
                rows.append({"key": message_id, "email_message_id": message_id, "date": tx_date.isoformat(),
                             "kind": "income" if category == "income" else "expense", "amount": str(amount),
                             "description": description, "category": category, "payment_method": source.title(),
                             "source": source, "email_link": email_link, "status": "new"})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Could not read configured Gmail mailbox") from exc
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except Exception:
                pass

    counts, items = {}, []
    with connect() as db:
        existing = {row["email_message_id"]: dict(row) for row in db.execute(
            "SELECT * FROM transactions WHERE email_message_id IS NOT NULL").fetchall()}
    seen = set()
    for item in rows:
        key = item["key"]
        if key in seen:
            item["status"] = "duplicate"
            item["detail"] = "Duplicate Message-ID in mailbox results"
        else:
            seen.add(key)
            local = existing.get(key)
            if local:
                comparable = (local["date"], local["kind"], Decimal(str(local["amount"])),
                              local["description"].strip(), local["category"].strip().lower(),
                              local["payment_method"].strip())
                incoming = (item["date"], item["kind"], Decimal(item["amount"]),
                            item["description"].strip(), item["category"].strip().lower(),
                            item["payment_method"].strip())
                item["status"] = "matched" if comparable == incoming else "conflict"
                if item["status"] == "conflict":
                    item["local"] = {"date":local["date"],"kind":local["kind"],"amount":float(local["amount"]),
                                     "description":local["description"],"category":local["category"],
                                     "payment_method":local["payment_method"]}
            else:
                item["status"] = "new"
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        if item["status"] in ("new", "matched", "conflict", "duplicate"):
            items.append(item)
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    with transaction() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                   ("gmail_sync_preview:" + token, json.dumps(items, separators=(",", ":"))))
    return {"preview_token":token,"counts":counts,"items":items[:500],
            "errors":errors[:100],"error_count":len(errors),"total_rows":len(rows)}


@app.post("/api/sheets-sync/preview")
def preview_sheets_sync(start: date | None = None, end: date | None = None,
                        _=Depends(_require_auth)):
    return _preview_sheet_sync(start, end)


@app.post("/api/gmail-sync/preview")
def preview_gmail_sync(start: date | None = None, end: date | None = None,
                       _=Depends(_require_auth)):
    return _preview_gmail_sync(start, end)


@app.post("/api/gmail-sync/apply")
def apply_gmail_sync(body: GmailSyncBody, _=Depends(_require_auth)):
    preview_key = "gmail_sync_preview:" + body.preview_token
    selected = set(body.apply_ids)
    with transaction() as db:
        saved = db.execute("SELECT value FROM settings WHERE key=?", (preview_key,)).fetchone()
        if not saved:
            raise HTTPException(409, "Preview expired; run preview again")
        preview_items = json.loads(saved["value"])
        candidates = {item["key"]:item for item in preview_items if item["status"] == "new"}
        if not selected or not selected.issubset(candidates):
            raise HTTPException(422, "Select one or more new Gmail rows from this preview")
        added = skipped = 0
        for key in selected:
            item = candidates[key]
            cur = db.execute("""INSERT OR IGNORE INTO transactions
                (date,kind,amount,description,category,payment_method,source,email_message_id,email_link)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (item["date"],item["kind"],item["amount"],item["description"],item["category"],
                 item["payment_method"],item["source"],item["email_message_id"],item["email_link"]))
            added += cur.rowcount
            skipped += 1-cur.rowcount
        db.execute("DELETE FROM settings WHERE key=?", (preview_key,))
    return {"added":added,"skipped_existing":skipped,"conflicts_applied":0,"deleted":0}


@app.post("/api/sheets-sync/apply")
def apply_sheets_sync(body: SheetSyncBody, _=Depends(_require_auth)):
    preview_key = "sheet_sync_preview:" + body.preview_token
    selected = set(body.apply_ids)
    with transaction() as db:
        saved = db.execute("SELECT value FROM settings WHERE key=?", (preview_key,)).fetchone()
        if not saved:
            raise HTTPException(409, "Preview expired; run preview again")
        preview_items = json.loads(saved["value"])
        candidates = {item["key"]: item for item in preview_items if item["status"] == "new"}
        if not selected or not selected.issubset(candidates):
            raise HTTPException(422, "Select one or more new rows from this preview")
        added = skipped = 0
        for key in selected:
            item = candidates[key]
            cur = db.execute("""INSERT OR IGNORE INTO transactions
                (date,kind,amount,description,category,payment_method,source,email_message_id,email_link)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (item["date"], item["kind"], item["amount"], item["description"],
                 item["category"], item["payment_method"], "sheet-import", key,
                 item["email_link"]))
            added += cur.rowcount
            skipped += 1 - cur.rowcount
        db.execute("DELETE FROM settings WHERE key=?", (preview_key,))
    return {"added": added, "skipped_existing": skipped,
            "conflicts_applied": 0, "deleted": 0}


@app.get("/api/categories")
def categories(_=Depends(_require_auth)):
    with connect() as db:
        rows = db.execute("SELECT DISTINCT category FROM transactions ORDER BY category").fetchall()
    return [r["category"] for r in rows]


@app.get("/api/transactions")
def list_transactions(
    start: date | None = None,
    end: date | None = None,
    kind: Literal["income", "expense"] | None = None,
    category: str | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    _=Depends(_require_auth),
):
    clauses, params = [], []
    if start:
        clauses.append("date >= ?"); params.append(start.isoformat())
    if end:
        clauses.append("date <= ?"); params.append(end.isoformat())
    if kind:
        clauses.append("kind = ?"); params.append(kind)
    if category:
        clauses.append("category = ?"); params.append(category)
    if q:
        clauses.append("(description LIKE ? OR notes LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as db:
        total = db.execute("SELECT COUNT(*) n FROM transactions" + where, params).fetchone()["n"]
        rows = db.execute("SELECT * FROM transactions" + where +
                          " ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
                          [*params, limit, offset]).fetchall()
    return {"items": [_serialize(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/api/transactions/{tx_id}")
def get_transaction(tx_id: int, _=Depends(_require_auth)):
    with connect() as db:
        row = db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Transaction not found")
    return _serialize(row)


def _insert_body(db, body: TxBody):
    cur = db.execute("""INSERT INTO transactions
        (date,kind,amount,description,category,payment_method,source,notes)
        VALUES(?,?,?,?,?,?, 'manual', ?)""",
        (body.date.isoformat(), body.kind, str(body.amount), body.description,
         body.category, body.payment_method, body.notes))
    return cur.lastrowid


@app.post("/api/transactions", status_code=201)
def create_transaction(body: TxBody, _=Depends(_require_auth)):
    with transaction() as db:
        tx_id = _insert_body(db, body)
        row = db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    return _serialize(row)


@app.put("/api/transactions/{tx_id}")
def update_transaction(tx_id: int, body: TxBody, _=Depends(_require_auth)):
    with transaction() as db:
        row = db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Transaction not found")
        if dict(row)["source"] != "manual":
            db.execute("""UPDATE transactions SET category=?,notes=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id=?""",
                (body.category, body.notes, tx_id))
        else:
            db.execute("""UPDATE transactions SET date=?,kind=?,amount=?,description=?,
                category=?,payment_method=?,notes=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id=?""",
                (body.date.isoformat(), body.kind, str(body.amount), body.description,
                 body.category, body.payment_method, body.notes, tx_id))
        row = db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    return _serialize(row)


@app.delete("/api/transactions/{tx_id}", status_code=204)
def delete_transaction(tx_id: int, _=Depends(_require_auth)):
    with transaction() as db:
        cur = db.execute("DELETE FROM transactions WHERE id=? AND source='manual'", (tx_id,))
        if cur.rowcount == 0:
            exists = db.execute("SELECT source FROM transactions WHERE id=?", (tx_id,)).fetchone()
            if exists:
                raise HTTPException(403, "Email-imported transactions cannot be deleted here")
            raise HTTPException(404, "Transaction not found")
    return Response(status_code=204)


@app.get("/api/summary")
def summary(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    _=Depends(_require_auth),
):
    year, mon = map(int, month.split("-"))
    if mon < 1 or mon > 12:
        raise HTTPException(422, "Invalid month")
    start = f"{year:04d}-{mon:02d}-01"
    end = f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"
    with connect() as db:
        totals = db.execute("""SELECT kind, COALESCE(SUM(amount),0) AS total, COUNT(*) AS count
            FROM transactions WHERE date BETWEEN ? AND ? GROUP BY kind""", (start, end)).fetchall()
        cats = db.execute("""SELECT category, SUM(amount) AS total, COUNT(*) AS count
            FROM transactions WHERE date BETWEEN ? AND ? AND kind='expense'
            GROUP BY category ORDER BY total DESC""", (start, end)).fetchall()
    result = {"income": 0.0, "expenses": 0.0, "income_count": 0, "expense_count": 0}
    for r in totals:
        if r["kind"] == "income":
            result["income"], result["income_count"] = float(r["total"]), r["count"]
        else:
            result["expenses"], result["expense_count"] = float(r["total"]), r["count"]
    result["net"] = result["income"] - result["expenses"]
    result["by_category"] = [dict(r) for r in cats]
    result["month"] = month
    return result


@app.get("/api/export.csv")
def export_csv(_=Depends(_require_auth)):
    import csv
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["date","kind","amount_idr","description","category","payment_method","source","notes"])
    with connect() as db:
        for row in db.execute("SELECT date,kind,amount,description,category,payment_method,source,notes FROM transactions ORDER BY date,id"):
            writer.writerow(list(row))
    return Response(output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transactions.csv"})


@app.get("/{path:path}")
def frontend(path: str):
    # API paths never fall through to static content.
    if path.startswith("api/"):
        raise HTTPException(404)
    target = WEB / "frontend" / "dist" / path
    if target.is_file():
        return FileResponse(target)
    index = WEB / "frontend" / "dist" / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse({"detail": "Frontend not built yet"}, status_code=503)
