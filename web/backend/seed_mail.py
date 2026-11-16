"""Import any watched email transactions missing from SQLite, without touching Sheets."""
import sys, json, imaplib, email, email.utils, urllib.parse, sqlite3
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, '/opt/expense-tracker')
import tracker_daemon as td
DB=Path('/opt/expense-tracker/web/transactions.db')
cfg=json.loads(Path('/opt/expense-tracker/config.json').read_text())
m=imaplib.IMAP4_SSL('imap.gmail.com');m.login(cfg['gmail_user'],cfg['gmail_app_password']);m.select('INBOX',readonly=True)
since=(datetime.now()-timedelta(days=90)).strftime('%d-%b-%Y')
written=dupes=nonparse=0
for sender in cfg['senders']:
    typ,data=m.search(None,'(SINCE %s FROM "%s")'%(since,sender))
    ids=data[0].split() if typ=='OK' else []
    print(sender, 'messages:',len(ids))
    for mid in ids:
        typ,raw=m.fetch(mid,'(BODY.PEEK[])')
        if typ!='OK' or not raw or raw[0] is None:continue
        msg=email.message_from_bytes(raw[0][1]); msgid=(msg.get('Message-ID') or '').strip()
        if not msgid:continue
        with sqlite3.connect(DB) as db:
            if db.execute('SELECT 1 FROM transactions WHERE email_message_id=?',(msgid,)).fetchone():
                dupes+=1;continue
        frm=msg.get('From') or ''; sender_addr=frm[frm.find('<')+1:frm.find('>')] if '<' in frm else frm
        body=td.get_body(msg); res=td.extract(sender_addr,body)
        if not res:
            nonparse+=1;continue
        src,desc,amt,cat=res
        dt=email.utils.parsedate_to_datetime(msg.get('Date')).date().isoformat()
        kind='income' if cat=='income' else 'expense'
        link='https://mail.google.com/mail/u/0/#search/'+urllib.parse.quote('rfc822msgid:'+msgid)
        with sqlite3.connect(DB) as db:
            cur=db.execute('''INSERT OR IGNORE INTO transactions(date,kind,amount,description,category,payment_method,source,email_message_id,email_link)
             VALUES(?,?,?,?,?,?,?,?,?)''',(dt,kind,str(amt),desc,cat,src.title(),src,msgid,link))
            db.commit();written+=cur.rowcount
m.logout()
print('seed written',written,'already present',dupes,'unparsed/nontransactions',nonparse)
