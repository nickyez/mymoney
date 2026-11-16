"""Ensure email IDs in SQLite have a corresponding transaction exactly once."""
import json, sqlite3, imaplib, email, email.utils, urllib.parse, sys
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0,'/opt/expense-tracker')
import tracker_daemon as td
ROOT=Path('/opt/expense-tracker'); cfg=json.loads((ROOT/'config.json').read_text()); dbpath=ROOT/'web/transactions.db'
m=imaplib.IMAP4_SSL('imap.gmail.com');m.login(cfg['gmail_user'],cfg['gmail_app_password']);m.select('INBOX',readonly=True)
since=(datetime.now()-timedelta(days=90)).strftime('%d-%b-%Y'); added=dupes=unparsed=0
for sender in cfg['senders']:
 typ,data=m.search(None,'(SINCE %s FROM "%s")'%(since,sender)); ids=data[0].split() if typ=='OK' else []
 for mid in ids:
  typ,raw=m.fetch(mid,'(BODY.PEEK[])')
  if typ!='OK' or not raw or raw[0] is None: continue
  msg=email.message_from_bytes(raw[0][1]); msgid=(msg.get('Message-ID') or '').strip()
  if not msgid: continue
  with sqlite3.connect(dbpath) as db:
   if db.execute('SELECT 1 FROM transactions WHERE email_message_id=?',(msgid,)).fetchone(): dupes+=1; continue
  frm=msg.get('From') or ''; addr=frm[frm.find('<')+1:frm.find('>')] if '<' in frm else frm
  res=td.extract(addr,td.get_body(msg))
  if not res: unparsed+=1; continue
  src,desc,amt,cat=res; d=email.utils.parsedate_to_datetime(msg.get('Date')).date().isoformat(); link='https://mail.google.com/mail/u/0/#search/'+urllib.parse.quote('rfc822msgid:'+msgid)
  with sqlite3.connect(dbpath) as db:
   cur=db.execute('''INSERT OR IGNORE INTO transactions(date,kind,amount,description,category,payment_method,source,email_message_id,email_link) VALUES(?,?,?,?,?,?,?,?,?)''',(d,'income' if cat=='income' else 'expense',str(amt),desc,cat,src.title(),src,msgid,link));db.commit();added+=cur.rowcount
m.logout();print('mail reconcile: added',added,'already present',dupes,'unparsed',unparsed)
with sqlite3.connect(dbpath) as db: print('total db:',db.execute('select count(*) from transactions').fetchone()[0])
