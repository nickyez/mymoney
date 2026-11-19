#!/usr/bin/env python3
"""Expense tracker: Livin' Mandiri emails -> Google Sheets.

Runs on a 5-min systemd timer. Gmail via IMAP app password; Sheets via
service account. Dedupes on the RFC822 Message-ID (stable), marks mail read.
"""
import json
import logging
import re
import sqlite3
import imaplib
import email
import email.utils
import urllib.parse
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
SA_KEY = BASE / "service_account.json"
DB = BASE / "seen.db"
FAILED_LOG = BASE / "failed_emails.log"
SHEET_TAB = "Expenses"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

COLUMNS = ["Date", "Source", "Merchant", "Amount", "Category",
           "Payment Method", "Email Link"]

CATEGORY_MAP = {
    "food": ["makan", "food", "kopi", "restaurant", "grabfood", "gofood", "kantin"],
    "transport": ["tol", "parkir", "grabcar", "gocar", "ojek", "bensin", "e-toll"],
    "shopping": ["shopee", "tokopedia", "lazada", "blibli", "tiktok shop", "zalora"],
    "bills": ["pln", "pdam", "internet", "wifi", "bpjs", "token", "pulsa", "paket data", "indihome", "first media"],
    "entertainment": ["netflix", "spotify", "disney", "vidio", "game", "steam", "youtube premium"],
    "health": ["apotek", "apotik", "klinik", "rumah sakit", "halodoc", "farmasi", "lab"],
    "transfer": ["transfer", "kirim uang", "antreks"],
    "topup": ["top up", "topup", "isi saldo", "deposit"],
}
CAT_RE = {c: re.compile("|".join(map(re.escape, kws)), re.I) for c, kws in CATEGORY_MAP.items()}

AMOUNT_RE = re.compile(r"(?:Rp|IDR)\s*\.?\s*([\d.,]+)")
INCOME_RE = re.compile(r"pemasukan|kredit\s*(?:masuk)?|top\s*up|saldo\s*(?:bertambah|masuk)", re.I)

log = logging.getLogger("tracker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")


def load_cfg():
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def get_creds():
    return Credentials.from_service_account_file(str(SA_KEY), scopes=SCOPES)


def imap_connect(user, app_password):
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(user, app_password)
    return imap


def get_body(msg):
    """Prefer text/plain; strip tags from text/html as fallback."""
    if msg.is_multipart():
        parts = {"text/plain": None, "text/html": None}
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in parts and parts[ct] is None:
                payload = part.get_payload(decode=True)
                if payload:
                    parts[ct] = payload.decode("utf-8", "replace")
        body = parts["text/plain"] or parts["text/html"] or ""
    else:
        payload = msg.get_payload(decode=True)
        body = payload.decode("utf-8", "replace") if payload else ""
    if "<html" in body.lower():
        body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def extract_fields(sender, body):
    """Return (merchant, amount, category) or None if no amount found."""
    m = AMOUNT_RE.search(body)
    if not m:
        return None
    raw = m.group(1)
    if "," in raw:
        # Indonesian decimal: 1.000,00 -> dots are thousands, comma is decimal
        amount = float(raw.replace(".", "").replace(",", "."))
    else:
        # No decimal part: 1.000 or 1.000.000 -> dots are thousands
        amount = float(raw.replace(".", ""))
    merchant = None
    for pat in [r"[Mm]erchant[:\s]+([^\n.,;]{2,40})", r"[Tt]ujuan[:\s]+([^\n.,;]{2,40})",
                r"ke\s+([A-Z][A-Za-z0-9 .&'-]{2,40}?)(?:\s+(?:tanggal|via|pada|sebesar)|$)"]:
        mm = re.search(pat, body)
        if mm:
            merchant = mm.group(1).strip()
            break
    if not merchant:
        merchant = sender
    category = "income" if INCOME_RE.search(body) else "expense"
    if category == "expense":
        for cat, rx in CAT_RE.items():
            if rx.search(body):
                category = cat
                break
    return merchant, amount, category


def db_init():
    with sqlite3.connect(DB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS processed (msg_id TEXT PRIMARY KEY, ts TEXT)")


def db_seen(msg_id):
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT 1 FROM processed WHERE msg_id=?", (msg_id,)).fetchone() is not None


def db_mark(msg_id):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR IGNORE INTO processed VALUES (?,?)", (msg_id, datetime.utcnow().isoformat()))


def append_row(ws, row):
    ws.append_row(row, value_input_option="USER_ENTERED")


def main():
    cfg = load_cfg()
    db_init()
    spreadsheet_id = (cfg.get("spreadsheet_id") or "").strip()
    sheets_on = bool(spreadsheet_id)
    imap = imap_connect(cfg["gmail_user"], cfg["gmail_app_password"])
    imap.select("INBOX", readonly=False)  # readonly=False lets us set \Seen; dedupe is in SQLite

    from_clauses = " ".join('FROM "%s"' % s for s in cfg["senders"])
    typ, data = imap.search(None, "(UNSEEN %s)" % from_clauses)
    ids = (data[0].split() if typ == "OK" else [])
    log.info("Found %d new email(s) from watched senders", len(ids))
    if not ids:
        imap.logout()
        return

    creds = get_creds()
    if sheets_on:
        gs = gspread.authorize(creds)
        sh = gs.open_by_key(spreadsheet_id)
        try:
            ws = sh.worksheet(SHEET_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(SHEET_TAB, rows=100, cols=len(COLUMNS))
            ws.update(range_name="A1", values=[COLUMNS])
        log.info("Appending parsed emails to Google Sheet")
    else:
        ws = None
        log.info("Sheets mirroring off; parsed emails save to local SQLite only")

    from tracker_daemon import persist_local_transaction

    written = 0
    for mid in ids:
        typ, raw = imap.fetch(mid, "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(raw[0][1])
        msg_id = (msg.get("Message-ID") or "").strip()
        if not msg_id or db_seen(msg_id):
            continue
        body = get_body(msg)
        fields = extract_fields(cfg["senders"][0], body)
        if not fields:
            with open(FAILED_LOG, "a") as f:
                f.write(f"{datetime.utcnow().isoformat()}\t{msg_id}\t{body[:300]}\n---\n")
            log.warning("No amount parsed, logged to %s", FAILED_LOG.name)
        else:
            merchant, amount, category = fields
            date_str = email.utils.parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d")
            link = "https://mail.google.com/mail/u/0/#search/" + urllib.parse.quote("rfc822msgid:" + msg_id)
            kind = "income" if category == "income" else "expense"
            inserted = persist_local_transaction(
                date_str=date_str, kind=kind, amount=amount, description=merchant,
                category=category, payment_method="Livin' (Mandiri)",
                source="livin-mandiri", message_id=msg_id, email_link=link)
            if inserted:
                log.info("WROTE SQLite: %s %s Rp %.0f [%s]", date_str, merchant, amount, category)
            else:
                log.info("SKIP duplicate local transaction: %s", msg_id[:40])
            if ws is not None:
                append_row(ws, [date_str, "livin-mandiri", merchant, amount, category,
                               "Livin' (Mandiri)", link])
                log.info("WROTE Sheets: %s %s Rp %.0f", date_str, merchant, amount)
            written += 1
        db_mark(msg_id)
        imap.store(mid, "+FLAGS", "\\Seen")

    imap.logout()
    log.info("Done: %d row(s) appended", written)


if __name__ == "__main__":
    main()
