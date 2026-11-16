"""Idempotently import existing spreadsheet rows into MyMoney SQLite."""
import json, re, sqlite3, hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials
ROOT=Path('/opt/expense-tracker')
cfg=json.loads((ROOT/'config.json').read_text())
creds=Credentials.from_service_account_file(str(ROOT/'service_account.json'),scopes=['https://www.googleapis.com/auth/spreadsheets'])
ws=gspread.authorize(creds).open_by_key(cfg['spreadsheet_id']).worksheet('Expenses')
rows=ws.get_all_values()
def amount(v):
 s=(v or '').replace('Rp','').replace('IDR','').replace(' ','').strip()
 return Decimal(s.replace('.','').replace(',','.')) if ',' in s else Decimal(s.replace('.',''))
with sqlite3.connect(ROOT/'web/transactions.db') as db:
 added=0; skipped=0; errors=[]
 for ix,row in enumerate(rows[1:],2):
  if not any(x.strip() for x in row): continue
  row=row+['']*(8-len(row))
  try:
   d=date.fromisoformat(row[0].strip()).isoformat(); amt=amount(row[3]); link=row[6].strip()
   mm=re.search(r'rfc822msgid%3A%3C([^%>]+)',link,re.I)
   email_id='<'+mm.group(1).replace('%40','@')+'>' if mm else 'sheet:'+hashlib.sha256((str(ix)+'|'+ '|'.join(row)).encode()).hexdigest()
   cur=db.execute('''INSERT OR IGNORE INTO transactions(date,kind,amount,description,category,payment_method,source,email_message_id,email_link,notes) VALUES(?,?,?,?,?,?,?,?,?,?)''',(d,'income' if row[4].strip().lower()=='income' else 'expense',str(amt),row[2].strip() or row[1].strip() or 'Transaction',row[4].strip() or 'uncategorized',row[5].strip(),row[1].strip() or 'sheet-import',email_id,link,row[7].strip()))
   added+=cur.rowcount; skipped+=1-cur.rowcount
  except Exception as e: errors.append((ix,str(e)))
 db.commit()
 print('sheet rows',len(rows)-1,'added',added,'skipped',skipped,'errors',errors)
 print('total db',db.execute('select count(*) from transactions').fetchone()[0])
