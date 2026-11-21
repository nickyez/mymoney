import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import app
import db
from fastapi.testclient import TestClient


def main():
    compose = (Path(__file__).resolve().parents[3] / 'docker-compose.yml').read_text()
    assert 'MYMONEY_CONFIG=/app/data/config.json' in compose
    assert 'mymoney-data:/app/data' in compose
    assert 'profiles: ["gmail"]' in compose
    assert './config.json:/app/config.json' not in compose
    assert './service_account.json:/app/web/backend/service_account.json:ro' in compose
    print('Clean-install Compose configuration checks passed')
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "transactions.db"
        app.DB_PATH = db.DB_PATH
        db.init_db()
        with db.transaction() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES('password_hash','test-hash')")
        client = TestClient(app.app)
        original_settings = app._get_setting
        original_config_env = os.environ.get('MYMONEY_CONFIG')
        original_config_file = app.CONFIG_FILE
        fixture_cfg = Path(tmp) / 'gmail-config.json'
        fixture_cfg.write_text(json.dumps({
            'gmail_user': 'fixture@example.com',
            'senders': ['noreply.livin@bankmandiri.co.id'],
            'spreadsheet_id': '',
            'gmail_app_password': 'fixture-only-secret',
        }))
        os.environ['MYMONEY_CONFIG'] = str(fixture_cfg)
        app.CONFIG_FILE = fixture_cfg
        app._get_setting = lambda key: "test-hash" if key == "password_hash" else original_settings(key)
        try:
            login = client.post('/api/auth/login', json={'password':'not-used-by-test'})
            assert login.status_code in (200, 401), login.text
            # First-run password setup should create a session and remain single-use.
            with db.transaction() as conn:
                conn.execute("DELETE FROM settings WHERE key='password_hash'")
            app.app.dependency_overrides.clear()
            setup_client = TestClient(app.app, base_url='https://testserver')
            app._get_setting = original_settings
            saved = setup_client.post('/api/auth/setup', json={'password':'valid-test-password-123'})
            assert saved.status_code == 200, saved.text
            assert saved.json()['setup_complete'] is True
            assert setup_client.get('/api/auth/status').json()['authenticated'] is True
            app.app.dependency_overrides[app._require_auth] = lambda: None
            assert setup_client.get('/api/summary?month=2026-11').status_code == 200
            manual_tx = setup_client.post('/api/transactions', json={
                'date':'2026-11-19', 'kind':'expense', 'amount':'12000',
                'description':'First manual transaction', 'category':'food',
                'payment_method':'Cash', 'notes':''
            })
            assert manual_tx.status_code == 201, manual_tx.text
            duplicate = setup_client.post('/api/auth/setup', json={'password':'another-valid-password-123'})
            assert duplicate.status_code == 409, duplicate.text
            print('First-run password setup session check passed')
            app.app.dependency_overrides[app._require_auth] = lambda: True
            rows = [
                ['Date','Source','Merchant','Amount','Category','Payment Method','Email Link'],
                ['2026-11-01','sheet-import','Fixture Cafe','12000','food','Cash',''],
                ['bad-date','sheet-import','Bad Row','oops','food','Cash',''],
            ]
            class Worksheet:
                def get_all_values(self): return rows
            app._sheet_services = lambda: Worksheet()
            preview = client.post('/api/sheets-sync/preview')
            assert preview.status_code == 200, preview.text
            data=preview.json()
            assert data['counts'].get('new') == 1, data
            assert data['error_count'] == 1, data
            key=data['items'][0]['key']
            applied=client.post('/api/sheets-sync/apply',json={'preview_token':data['preview_token'],'apply_ids':[key]})
            assert applied.status_code == 200, applied.text
            assert applied.json()['added'] == 1
            assert client.post('/api/sheets-sync/apply',json={'preview_token':data['preview_token'],'apply_ids':[key]}).status_code == 409
            print('Sheets preview/apply fixture checks passed')

            # Gmail preview uses mocked IMAP and parser; assert mailbox is readonly and never marks mail read.
            import email
            import imaplib
            import tracker_daemon
            gmail_message = (b"From: noreply.livin@bankmandiri.co.id\r\n"
                             b"Message-ID: <fixture-gmail-1@example.test>\r\n"
                             b"Date: Sun, 01 Nov 2026 10:00:00 +0700\r\n"
                             b"Subject: fixture\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nRp 12.000 di Cafe")
            class FakeIMAP:
                def __init__(self,*args,**kwargs): self.commands=[]
                def login(self,*args): self.commands.append(('login',args)); return 'OK',[]
                def select(self,*args,**kwargs): self.commands.append(('select',args,kwargs)); return 'OK',[]
                def search(self,*args): self.commands.append(('search',args)); return 'OK',[b'1']
                def fetch(self,*args): self.commands.append(('fetch',args)); return 'OK',[(b'1 BODY[]',gmail_message),b')']
                def logout(self): self.commands.append(('logout',)); return 'BYE',[]
            fake=FakeIMAP()
            original_imap=imaplib.IMAP4_SSL
            original_extract=tracker_daemon.extract
            imaplib.IMAP4_SSL=lambda *args,**kwargs: fake
            tracker_daemon.extract=lambda sender,body: ('livin-mandiri','Fixture Cafe',12000,'food')
            try:
                gmail_preview=client.post('/api/gmail-sync/preview')
                assert gmail_preview.status_code == 200, gmail_preview.text
                gmail_data=gmail_preview.json()
                assert gmail_data['counts'].get('new') == 1, gmail_data
                assert gmail_data['items'][0]['email_message_id']=='<fixture-gmail-1@example.test>'
                assert any(c[0]=='select' and c[2].get('readonly') is True for c in fake.commands)
                assert all(c[0] not in ('store','copy','expunge') for c in fake.commands)
                gmail_apply=client.post('/api/gmail-sync/apply',json={'preview_token':gmail_data['preview_token'],'apply_ids':['<fixture-gmail-1@example.test>']})
                assert gmail_apply.status_code == 200, gmail_apply.text
                assert gmail_apply.json()['added'] == 1
                print('Gmail read-only preview/apply fixture checks passed')
                original_cfg = app.CONFIG_FILE
                original_sheet_client = app._sheets_client
                cfg_path = Path(tmp) / 'config.json'
                cfg_path.write_text('{"spreadsheet_id":"fixture-spreadsheet-id-1234567890","gmail_user":"fixture@example.com","gmail_app_password":"secret"}')
                app.CONFIG_FILE = cfg_path
                class FakeWorksheet:
                    def __init__(self, values): self.values=values; self.updates=[]
                    def get_all_values(self): return self.values
                    def update(self, **kwargs): self.updates.append(kwargs)
                class FakeSpreadsheet:
                    def __init__(self, worksheet_value=None, first_values=None, all_tabs=None): self.worksheet_value=worksheet_value; self.first_values=first_values; self.all_tabs=all_tabs; self.added=[]
                    def worksheet(self, name):
                        if self.worksheet_value is None: raise RuntimeError('missing worksheet')
                        return self.worksheet_value
                    def worksheets(self): return self.all_tabs or [FakeWorksheet(self.first_values or [])]
                    def add_worksheet(self, title, **kwargs):
                        ws=FakeWorksheet([]); self.added.append(({'title':title,**kwargs},ws)); return ws
                class FakeClient:
                    def __init__(self, sheet): self.sheet=sheet
                    def open_by_key(self, key): return self.sheet
                blank_sheet=FakeSpreadsheet(first_values=[])
                app._sheets_client=lambda: (FakeClient(blank_sheet), 'service@example.iam.gserviceaccount.com')
                checked=app._check_sheets_connection()
                assert checked['initialized'] is True and blank_sheet.added[0][1].updates[0]['values'] == [app.SHEET_COLUMNS]
                mismatch=FakeSpreadsheet(worksheet_value=FakeWorksheet([['Wrong','Header']]))
                app._sheets_client=lambda: (FakeClient(mismatch), 'service@example.iam.gserviceaccount.com')
                checked=app._check_sheets_connection()
                assert checked['connected'] is False and checked['initialized'] is False and not mismatch.worksheet_value.updates
                populated_without_expenses=FakeSpreadsheet(all_tabs=[FakeWorksheet([]),FakeWorksheet([['Other','Tab'],['existing','data']])])
                app._sheets_client=lambda: (FakeClient(populated_without_expenses), 'service@example.iam.gserviceaccount.com')
                checked=app._check_sheets_connection()
                assert checked['connected'] is False and not populated_without_expenses.added
                blank_tab_with_data=FakeSpreadsheet(worksheet_value=FakeWorksheet([['Existing','Data'],['keep','intact']]))
                app._sheets_client=lambda: (FakeClient(blank_tab_with_data), 'service@example.iam.gserviceaccount.com')
                checked=app._check_sheets_connection()
                assert checked['connected'] is False and not blank_tab_with_data.worksheet_value.updates
                class CheckIMAP:
                    def __init__(self,*args,**kwargs): self.commands=[]
                    def login(self,*args): self.commands.append(('login',args)); return 'OK',[]
                    def select(self,*args,**kwargs): self.commands.append(('select',args,kwargs)); return 'OK',[]
                    def logout(self): self.commands.append(('logout',)); return 'BYE',[]
                check_imap=CheckIMAP(); imaplib.IMAP4_SSL=lambda *args,**kwargs: check_imap
                gmail_checked=app._check_gmail_connection()
                assert gmail_checked['connected'] is True
                assert check_imap.commands[1][2].get('readonly') is True
                app.CONFIG_FILE=original_cfg; app._sheets_client=original_sheet_client
                print('Connection and blank-sheet initialization checks passed')
            finally:
                imaplib.IMAP4_SSL=original_imap
                tracker_daemon.extract=original_extract
            original_config_env = os.environ.get('MYMONEY_CONFIG')
            original_config_file = app.CONFIG_FILE
            missing_cfg = Path(tmp) / 'missing-config.json'
            os.environ['MYMONEY_CONFIG'] = str(missing_cfg)
            app.CONFIG_FILE = missing_cfg
            missing_settings = client.get('/api/settings')
            assert missing_settings.status_code == 200, missing_settings.text
            missing_data = missing_settings.json()
            assert missing_data['gmail_user'] == '' and missing_data['senders'] == []
            assert missing_data['spreadsheet_id'] == ''
            assert missing_data['spreadsheet_configured'] is False
            assert missing_data['gmail_app_password_configured'] is False
            assert client.post('/api/sheets-connection', json={'spreadsheet_id':''}).status_code == 422
            missing_gmail = client.get('/api/gmail-connection')
            assert missing_gmail.status_code == 422, missing_gmail.text
            missing_sheet_preview = client.post('/api/sheets-sync/preview')
            assert missing_sheet_preview.status_code == 200, missing_sheet_preview.text
            assert missing_sheet_preview.json().get('error') or missing_sheet_preview.json().get('errors')
            missing_gmail_preview = client.post('/api/gmail-sync/preview')
            assert missing_gmail_preview.status_code == 422, missing_gmail_preview.text
            first_save = client.put('/api/settings', json={
                'gmail_user':'first@example.com', 'senders':['notice@example.com'],
                'spreadsheet_id':'', 'gmail_app_password':None
            })
            assert first_save.status_code == 200, first_save.text
            assert json.loads(missing_cfg.read_text())['gmail_user'] == 'first@example.com'
            assert missing_cfg.stat().st_mode & 0o777 == 0o600
            import tracker_daemon
            original_daemon_config = tracker_daemon.CONFIG
            tracker_daemon.CONFIG = missing_cfg
            assert tracker_daemon.load_cfg()['gmail_user'] == 'first@example.com'
            tracker_daemon.CONFIG = original_daemon_config
            missing_cfg.unlink()
            missing_cfg.write_text('{broken json')
            invalid_settings = client.get('/api/settings')
            assert invalid_settings.status_code == 200, invalid_settings.text
            assert invalid_settings.json()['gmail_user'] == ''
            app.CONFIG_FILE = original_config_file
            if original_config_env is None:
                os.environ.pop('MYMONEY_CONFIG', None)
            else:
                os.environ['MYMONEY_CONFIG'] = original_config_env
            print('Missing/invalid config settings recovery checks passed')
            original_config_file = app.CONFIG_FILE
            original_subprocess_run = app.subprocess.run
            save_cfg = Path(tmp) / 'save-config.json'
            save_cfg.write_text('{"gmail_user":"old@example.com","senders":["old-sender@example.com"],"spreadsheet_id":"fixture-spreadsheet-id-1234567890","gmail_app_password":"old-password","unrelated":"preserve"}')
            app.CONFIG_FILE = save_cfg
            app.subprocess.run = lambda *args, **kwargs: None
            saved_response = client.put('/api/settings', json={
                'gmail_user':'new@example.com', 'senders':['notice@example.com'],
                'spreadsheet_id':'fixture-spreadsheet-id-1234567890', 'gmail_app_password':None
            })
            assert saved_response.status_code == 200, saved_response.text
            saved_config = json.loads(save_cfg.read_text())
            assert saved_config['gmail_user'] == 'new@example.com'
            assert saved_config['unrelated'] == 'preserve'
            assert not save_cfg.read_bytes().endswith(b'\\\\n')
            assert save_cfg.stat().st_mode & 0o777 == 0o600
            app.CONFIG_FILE = original_config_file
            app.subprocess.run = original_subprocess_run
            print('Settings save JSON regression check passed')
        finally:
            app._get_setting = original_settings
            app.CONFIG_FILE = original_config_file
            if original_config_env is None:
                os.environ.pop('MYMONEY_CONFIG', None)
            else:
                os.environ['MYMONEY_CONFIG'] = original_config_env

if __name__ == '__main__': main()
