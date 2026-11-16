#!/usr/bin/env python3
"""Set initial dashboard password; run from a private server shell."""
import getpass
from auth import hash_password
from db import DB_PATH, init_db, transaction
init_db()
with transaction() as db:
    if db.execute("SELECT 1 FROM settings WHERE key='password_hash'").fetchone():
        raise SystemExit("Password already configured in %s; refusing to overwrite." % DB_PATH)
p1=getpass.getpass("New MyMoney password (12+ chars): ")
p2=getpass.getpass("Confirm password: ")
if len(p1)<12 or p1!=p2: raise SystemExit("Passwords must match and be at least 12 characters.")
h=hash_password(p1)
with transaction() as db: db.execute("INSERT INTO settings(key,value) VALUES('password_hash',?)",(h,))
print("Password set in %s. You can now sign in." % DB_PATH)
