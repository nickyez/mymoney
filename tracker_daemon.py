#!/usr/bin/env python3
"""Real-time expense tracker via IMAP IDLE.
Holds a persistent connection; Gmail pushes new-mail notifications instantly.
Service account key -> Google Sheets (writes).
Systemd unit: expense-daemon.service  (Type=notify, Restart=always).
Fallback timer: expense-fallback.timer (every 30 min, safety net if daemon dies).
"""
import imaplib
import json
import logging
import re
import sqlite3
import email
import email.utils
import urllib.parse
from datetime import datetime, timezone, timedelta
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

# ── Category map ────────────────────────────────────────────────────────────────
CATEGORY_MAP = {
    "food":        ["makan","food","kopi","restaurant","grabfood","gofood","kantin"],
    "transport":   ["tol","parkir","grabcar","gocar","ojek","bensin","e-toll"],
    "shopping":    ["shopee","tokopedia","lazada","blibli","tiktok","zalora"],
    "bills":       ["pln","pdam","internet","wifi","bpjs","token","pulsa",
                    "paket data","indihome","first media","token listrik"],
    "entertainment":["netflix","spotify","disney","vidio","game","steam",
                      "youtube premium"],
    "health":      ["apotek","apotik","klinik","rumah sakit","halodoc",
                     "farmasi","lab"],
    "transfer":    ["transfer","kirim uang","antreks","penerima"],
    "topup":       ["top up","topup","isi saldo","deposit"],
}
CAT_RE = {c: re.compile("|".join(map(re.escape, kws)), re.I)
          for c, kws in CATEGORY_MAP.items()}

AMOUNT_RE  = re.compile(r"(?:Rp|IDR)\s*\.?\s*([\d.,]+)")
INCOME_RE  = re.compile(r"pemasukan|kredit\s*(?:masuk)?|saldo\s*(?:bertambah|masuk)", re.I)
CANCEL_RE  = re.compile(r"batal|dibatalkan|cancelled|refund|pengembalian", re.I)

log = logging.getLogger("daemon")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")


# ── Config ─────────────────────────────────────────────────────────────────────
def load_cfg():
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_creds():
    return Credentials.from_service_account_file(str(SA_KEY), scopes=SCOPES)


def get_worksheet():
    """Return the Expenses worksheet, or None when Sheets mirroring is off."""
    if not (load_cfg().get("spreadsheet_id") or "").strip():
        return None
    creds = get_creds()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(load_cfg()["spreadsheet_id"])
    try:
        ws = sh.worksheet(SHEET_TAB)
    except Exception:
        ws = sh.add_worksheet(SHEET_TAB, rows=200, cols=len(COLUMNS))
        ws.update(range_name="A1", values=[COLUMNS])
    return ws


# ── IMAP ───────────────────────────────────────────────────────────────────────
def imap_connect(cfg):
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(cfg["gmail_user"], cfg["gmail_app_password"])
    m.select("INBOX")
    return m


def get_body(msg):
    if msg.is_multipart():
        parts = {"text/plain": None, "text/html": None}
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in parts and parts[ct] is None:
                p = part.get_payload(decode=True)
                if p:
                    parts[ct] = p.decode("utf-8", "replace")
        body = parts["text/plain"] or parts["text/html"] or ""
    else:
        p = msg.get_payload(decode=True)
        body = p.decode("utf-8", "replace") if p else ""
    if "<html" in body.lower():
        body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


# ── Parser ─────────────────────────────────────────────────────────────────────
# Per-sender extractors: (source_label, field_extractor)
def parse_livin(body):
    """Mandiri Livin'."""
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
    for pat in [
        r"[Tt]ujuan[:\s]+([^\n.,;]{2,40})",
        r"[Pp]enerima[:\s]+([^\n.,;]{2,40})",
        r"[Rr]ecipient[:\s]+([^\n.,;]{2,40})",
        r"ke\s+([A-Z][A-Za-z0-9 .&'-]{2,40}?)(?:\s+(?:tanggal|via|pada|sebesar|senilai)|$)",
        r"merchant[:\s]+([^\n.,;]{2,40})",
    ]:
        mm = re.search(pat, body)
        if mm:
            merchant = mm.group(1).strip()
            break
    if not merchant:
        merchant = "Livin' Mandiri"
    category = "income" if INCOME_RE.search(body) else "expense"
    if category == "expense":
        for cat, rx in CAT_RE.items():
            if rx.search(body):
                category = cat
                break
    return merchant, amount, category


def parse_shopee(body):
    """Shopee no-reply@shopee.co.id."""
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
    for pat in [
        r"[Pp]embelian\s*(?:di)?\s*([A-Za-z0-9 .&'-]{2,40})",
        r"[Tt]oko[:\s]+([^\n.,;]{2,40})",
        r"[Oo]rder\s*(?:di)?\s*([A-Za-z0-9 .&'-]{2,40})",
        r"Pesanan di\s+([^\n]{2,35}?)(?:\s+dengan|\s+sebesar|\s+Rp|\s+\d|\s+\()",
        r"(?:di|di\s+)\s*([A-Za-z][A-Za-z0-9 .&'-]{2,35})(?:\s+(?:order|pembelian|pesanan))",
    ]:
        mm = re.search(pat, body)
        if mm:
            merchant = mm.group(1).strip()
            break
    if not merchant:
        merchant = "Shopee"
    category = "shopping"
    return merchant, amount, category


def parse_jago(body):
    """Bank Jago noreply@jago.com. Format: Rp 23.350 (dots = thousands)."""
    m = AMOUNT_RE.search(body)
    if not m:
        return None
    raw = m.group(1)
    if "," in raw:
        # Indonesian decimal: 1.000,50 -> dots thousands, comma decimal
        amount = float(raw.replace(".", "").replace(",", "."))
    else:
        # No decimal: Rp 23.350 -> 23350 (dots are thousands separators)
        amount = float(raw.replace(".", ""))
    merchant = None
    for pat in [
        r"ke\s+([A-Za-z][A-Za-z0-9 .&'-]{2,40}?)(?:\s+(?:tanggal|via|pada|sebesar|senilai)|$)",
        r"[Tt]ujuan[:\s]+([^\n.,;]{2,40})",
        r"[Pp]enerima[:\s]+([^\n.,;]{2,40})",
        r"merchant[:\s]+([^\n.,;]{2,40})",
    ]:
        mm = re.search(pat, body)
        if mm:
            merchant = mm.group(1).strip()
            break
    if not merchant:
        merchant = "Bank Jago"
    category = "income" if INCOME_RE.search(body) else "expense"
    if category == "expense":
        for cat, rx in CAT_RE.items():
            if rx.search(body):
                category = cat
                break
    return merchant, amount, category


SENDER_PARSERS = {
    "noreply.livin@bankmandiri.co.id": ("livin-mandiri", parse_livin),
    "no-reply@shopee.co.id":            ("shopee",          parse_shopee),
    "no.reply@shopee.co.id":           ("shopee",          parse_shopee),
    "no-reply@notif.shopee.co.id":     ("shopee",          parse_shopee),
    "noreply@jago.com":                 ("jago",            parse_jago),
}


def extract(sender, body):
    """Return (source, merchant, amount, category) or None."""
    sender_lower = sender.lower()
    parser = None
    source = "unknown"
    for key, (src, fn) in SENDER_PARSERS.items():
        if key in sender_lower:
            parser = fn
            source = src
            break
    if not parser:
        return None

    if CANCEL_RE.search(body):
        return None  # skip cancelled/refund notifications

    result = parser(body)
    if not result:
        return None
    merchant, amount, category = result
    return source, merchant, amount, category


# ── Dedupe DB ──────────────────────────────────────────────────────────────────
def db_init():
    with sqlite3.connect(DB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS processed "
                  "(msg_id TEXT PRIMARY KEY, ts TEXT)")


def db_seen(msg_id):
    with sqlite3.connect(DB) as c:
        return c.execute(
            "SELECT 1 FROM processed WHERE msg_id=?", (msg_id,)
        ).fetchone() is not None


def db_mark(msg_id):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR IGNORE INTO processed VALUES (?,?)",
                  (msg_id, datetime.now(timezone.utc).isoformat()))


def persist_local_transaction(*, date_str, kind, amount, description, category,
                              payment_method, source, message_id, email_link):
    """Persist one parsed email locally before any optional spreadsheet export."""
    import os
    import sqlite3
    from pathlib import Path
    db_path = Path(os.environ.get("EXPENSE_DB", "/opt/expense-tracker/web/transactions.db"))
    if not db_path.exists():
        log.error("Local transaction DB missing; refusing to mark email processed")
        return False
    try:
        with sqlite3.connect(db_path, timeout=15) as db:
            cur = db.execute("""INSERT OR IGNORE INTO transactions
                (date,kind,amount,description,category,payment_method,source,email_message_id,email_link)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (date_str, kind, amount, description, category, payment_method,
                 source, message_id, email_link))
            db.commit()
            return cur.rowcount == 1
    except Exception:
        log.exception("Local SQLite write failed; leaving email unprocessed")
        return False


# ── Core: write one row ────────────────────────────────────────────────────────
def process_one(imap_conn, mid, ws):
    """Fetch a single email, parse, write to sheet, mark seen."""
    typ, raw = imap_conn.fetch(mid, "(RFC822)")
    if typ != "OK" or not raw or raw[0] is None:
        return False

    msg = email.message_from_bytes(raw[0][1])
    msg_id = (msg.get("Message-ID") or "").strip()
    if not msg_id or db_seen(msg_id):
        return False

    from_addr = msg.get("From") or ""
    sender = from_addr.split("@")[0] + "@" + from_addr.split("@")[-1].split(">")[0]

    body = get_body(msg)
    result = extract(sender, body)

    try:
        date_hdr = msg.get("Date")
        date = (email.utils.parsedate_to_datetime(date_hdr)
                if date_hdr else datetime.now(timezone.utc))
        date_str = date.astimezone().strftime("%Y-%m-%d")
    except Exception:
        date_str = datetime.now().strftime("%Y-%m-%d")

    if not result:
        with open(FAILED_LOG, "a") as f:
            f.write("%s\t%s\t%s\n---\n" %
                    (datetime.now(timezone.utc).isoformat(), msg_id, body[:300]))
        log.warning("No amount parsed: %s", msg_id[:40])
        db_mark(msg_id)
        imap_conn.store(mid, "+FLAGS", "\\Seen")
        return True  # marked seen even if unparseable

    source, merchant, amount, category = result
    link = ("https://mail.google.com/mail/u/0/#search/"
            + urllib.parse.quote("rfc822msgid:" + msg_id))
    kind = "income" if category == "income" else "expense"
    payment = "Livin' (Mandiri)" if source == "livin-mandiri" else source.title()
    inserted = persist_local_transaction(
        date_str=date_str, kind=kind, amount=amount, description=merchant,
        category=category, payment_method=payment,
        source=source, message_id=msg_id, email_link=link)
    if not inserted:
        log.info("SKIP duplicate local transaction: %s", msg_id[:40])
    else:
        log.info("WROTE SQLite: %s %s Rp %.2f [%s]", date_str, merchant, amount, category)
        if ws is not None:
            try:
                ws.append_row(
                    [date_str, source, merchant, str(amount), category, payment, link],
                    value_input_option="USER_ENTERED"
                )
                log.info("WROTE Sheets: %s %s Rp %.2f", date_str, merchant, amount)
            except Exception as sheet_err:
                log.error("Sheets write failed (SQLite already saved): %s", sheet_err)
        else:
            log.debug("Sheets mirroring off; skipped sheet write for %s", msg_id[:40])
    db_mark(msg_id)
    imap_conn.store(mid, "+FLAGS", "\\Seen")
    return True


# ── IDLE main loop ─────────────────────────────────────────────────────────────
IDLE_TIMEOUT = 25 * 60   # 25 min — renew before Gmail's 29-min hard cap
IDLE_GRACE   = 60         # seconds to wait for IDLE to return before reconnecting


def main():
    db_init()
    cfg = load_cfg()
    log.info("Starting expense tracker (IDLE mode)")

    sender_list = cfg["senders"]
    log.info("Watching: %s", sender_list)

    conn = imap_connect(cfg)
    ws = get_worksheet()
    if ws is None:
        log.info("Connected to Gmail (Sheets mirroring off)")
    else:
        log.info("Connected to Gmail and Sheets")

    idle_count = 0

    while True:
        try:
            # Re-read config each pass so Settings UI edits apply without restart.
            cfg = load_cfg()
            sender_list = cfg.get("senders") or []
            # Build search query for all watched senders
            def imap_or(crits):
                if len(crits) == 1:
                    return crits[0]
                return "OR %s %s" % (crits[0], imap_or(crits[1:]))

            if sender_list:
                since = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
                query = "(SINCE %s %s)" % (
                    since, imap_or(['FROM "%s"' % s for s in sender_list]))
                typ, data = conn.select("INBOX")
                if typ != "OK":
                    log.warning("INBOX select failed, reconnecting...")
                    conn = imap_connect(cfg)
                    ws = get_worksheet()
                    continue

                typ, data = conn.search(None, query)
                current_ids = set(data[0].split()) if typ == "OK" and data[0] else set()

                # Process ALL recent mail from watched senders; process_one
                # skips IDs already in the dedupe DB (works read or unread)
                if current_ids:
                    log.info("Scanning %d recent email(s) from watched senders", len(current_ids))
                    ws = get_worksheet()
                    for mid in current_ids:
                        try:
                            process_one(conn, mid, ws)
                        except Exception as e:
                            log.error("process_one error mid=%s: %s", mid, e)
            else:
                log.warning("No senders configured; skipping scan")

            idle_count += 1
            if idle_count % 50 == 0:
                log.info("IDLE heartbeat #%d", idle_count)

            # IDLE: wait for Gmail to push new-mail notification
            try:
                with conn.idle(duration=IDLE_TIMEOUT) as idler:
                    for typ, data in idler:
                        if typ == "EXISTS":
                            log.info("Mailbox EXISTS -> %s (processing now)", data)
                            break  # exit IDLE, process immediately
            except imaplib.IMAP4.error as e:
                log.warning("IDLE error, reconnecting: %s", e)
                conn = imap_connect(cfg)
                ws = get_worksheet()
                continue

        except Exception as e:
            log.error("Loop error: %s", e)
            try:
                conn.logout()
            except Exception:
                pass
            import time
            time.sleep(10)
            try:
                conn = imap_connect(cfg)
                ws = get_worksheet()
            except Exception as e2:
                log.error("Reconnect failed: %s", e2)


if __name__ == "__main__":
    main()
