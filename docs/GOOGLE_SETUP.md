# Google Setup Guide for MyMoney

This guide covers all the **Google-side** preparation you need before installing MyMoney: a Google Cloud Platform account, the Google Sheets API, a service account key, a Gmail app password, and the spreadsheet itself.

**You only need one Cloud API: the Google Sheets API.** MyMoney reads your Gmail over **IMAP** (not the Gmail API), so you do **not** need to enable the Gmail API anywhere.

---

## Part 1 — Create a Google Cloud Platform account

1. Open [Google Cloud Console](https://console.cloud.google.com) in a browser.
2. Sign in with any Google (Gmail) account. If you have never used Cloud before, you will be asked to **accept the Terms of Service** and choose a region. Accept and continue.
3. Google may ask about **billing**. You do **not** need a billing account or credit card for this app — the Google Sheets API usage here is far within the free tier. You can dismiss/skip this.
4. You land on a project selector. Create a project named e.g. `mymoney` (top bar → **Select a project** → **New Project** → fill in the name → **Create**).

> If you already have a Google Cloud account, skip straight to Part 2; a fresh project is fine.

---

## Part 2 — Enable the Google Sheets API

1. With your project selected, go to **APIs & Services → Library**.
2. Search for **Google Sheets API**.
3. Click it, then click **Enable**.

That is the only Cloud API this app needs. Do **not** enable the Gmail API — MyMoney accesses Gmail over plain IMAP using an app password (Part 4), not through the Gmail API.

---

## Part 3 — Create the service account and download the key

1. Go to **IAM & Admin → Service Accounts** → **+ Create Service Account**.
2. Name it (e.g. `mymoney-writer`) → **Create and continue**.
3. Skip the optional role dropdown (no role needed) → **Done**.
4. Click the new service account row.
5. Go to the **Keys** tab → **Add Key → Create new key** → select **JSON** → **Create**. The key file downloads automatically.
6. Rename it to `service_account.json` and place it in the repository root. **Never commit or share this file** — it grants write access to your spreadsheet.
7. On the service account page, copy its **email address** (looks like `mymoney-writer@your-project.iam.gserviceaccount.com`). You will use it in Part 5.

> The service account is the identity MyMoney uses to **write to your spreadsheet**. It is separate from your personal Gmail login.

---

## Part 4 — Create a Gmail app password

MyMoney reads transaction emails from your Gmail **inbox** using IMAP. For security, Google requires an **app password** (a 16-character generated credential) instead of your normal password, and 2-Step Verification must be enabled.

1. Go to [Google Account Security](https://myaccount.google.com/security).
2. Turn on **2-Step Verification** (follow the on-screen prompts; app passwords only exist when 2FA is on).
3. Go to [App passwords](https://myaccount.google.com/apppasswords).
4. Create a password for **MyMoney** (or any custom name).
5. Copy the generated **16-character password** into `gmail_app_password` in `config.json`.

> The app password is stored on your server only, never returned by the app, and is revoked from Google's side if you remove it at the App passwords page. If the server is decommissioned, revoke it there.

---

## Part 5 — Create the spreadsheet

1. Open [Google Sheets](https://sheets.new) with the **same Google account** you used in Part 4.
2. Create a blank spreadsheet; name it e.g. `MyMoney`.
3. Copy the **Spreadsheet ID** — the part after `/d/` and before `/edit` in the URL:
   `https://docs.google.com/spreadsheets/d/` **`1AbCdeFGhIjKlMnOPqRstUvWxYz1234567890`** `/edit`
4. Click **Share**, paste the **service account email** from Part 3, choose **Editor**, and send.

### Worksheet layout

MyMoney expects a worksheet named **`Expenses`** with these columns in order:

```text
Date | Source | Merchant | Amount | Category | Payment Method | Email Link
```

You do not have to create the tab by hand: if the spreadsheet is completely blank (and the `Expenses` worksheet has no existing data), pressing **Test connection** in the app's Settings creates the tab and header automatically. If the sheet already has data with different headers, MyMoney intentionally leaves it alone and asks you to correct it.

---

## Common Google-side problems

| Symptom | Cause | Fix |
|---|---|---|
| `403` / "API not enabled" | Google Sheets API not enabled in the project | Part 2 |
| Sheets test fails but key file exists | Service account not shared as Editor | Part 5 step 4 |
| `Invalid credentials` on sheets | Wrong key file | Regenerate key (Part 3), save exactly as `service_account.json` |
| Gmail login fails | IMAP or 2FA / app password problem | Confirm 2FA is on and the app password is freshly generated (Part 4) |
| App shows the correct spreadsheet but claims it's not initialized | Existing data / unexpected header on `Expenses` | Fix the tab to the expected header; re-test |

If you hit a Google-side error not listed here, check the app's API service log for the underlying traceback before reporting.
