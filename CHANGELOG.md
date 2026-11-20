# Changelog

All notable changes to MyMoney are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- First-run setup signs the user in immediately after creating a password and
  explains that manual transaction tracking works without optional integrations.
- Dashboard first-run notice confirms manual tracking is ready and points users
  to Settings for optional Gmail capture and Google Sheets mirroring.
- Settings labels Gmail and Google Sheets as optional integrations.


## [1.2.2] - 2026-11-17

### Fixed

- Docker: Settings save returned 500 because the backend container mounts
  `config.json` read-only (`:ro`) while the Settings endpoint rewrites that
  file. The backend now mounts it writable; the daemon keeps `:ro` since it
  only reads the file.
- `PUT /api/settings` now returns a clear 500 message ("Could not write
  config.json on the server") instead of a generic error when the config
  file cannot be written.

### To apply

On the deployment host: pull the repo, then `docker compose build && docker compose up -d`
(so the new compose file's writable mount takes effect).

## [1.2.1] - 2026-11-17

### Fixed

- Restored the `.settings-note` CSS rule that was dropped in 1.2.0, which
  broke the Settings information/notes badges (plain unstyled text).


## [1.2.0] - 2026-11-17

### Added

- Gmail-only mode: leave the Spreadsheet ID blank and the app works without
  any Google Cloud setup — manual tracking and Gmail email capture keep
  working; Sheets mirroring simply stays off.
- `spreadsheet_configured` field in `GET /api/settings` and an amber
  "Sheets mirroring is off" hint in the Settings form.

### Changed

- Google Sheets mirroring is optional in Settings (frontend field no longer
  required; empty ID disables mirroring cleanly instead of erroring).
- Saving Settings on hosts without systemd (e.g. Docker) no longer returns
  503; the response now reports whether the email watcher was restarted.
- The email watcher re-reads `config.json` every poll, so Settings edits
  apply without a watcher restart.
- `tracker.py` (5-minute timer) now saves parsed emails to local SQLite as
  well (or instead of the sheet when mirroring is off), so no email is ever
  lost in Gmail-only mode.
- Docker: the `service_account.json` mount is opt-in — Gmail-only
  deployments no longer need the key file on the host.
- Docs: Google Cloud / service account / spreadsheet setup marked optional;
  new `docs/GOOGLE_SETUP.md` walkthrough; README split accordingly.

## [1.1.0] - 2026-11-17

### Added

- Spreadsheet ID help dialog in Settings, including URL guidance and an example.
- First-run password setup: the login screen shows a "Create your password"
  form when no password is configured yet (no CLI step needed).

### Changed

- Email-imported transactions can edit only their category and notes.
- Email transactions are appended to the configured Google Sheet after being
  saved to SQLite.
- Release tooling updates the project version and changelog consistently.

## [1.0.0] - 2026-11-12

### Added

- Initial MyMoney application snapshot.
- FastAPI backend with SQLite-backed transaction storage and authentication.
- React, TypeScript, and Vite dashboard with monthly summaries, charts, category breakdowns, transaction editing, and CSV export.
- Gmail transaction capture through the IMAP watcher and sender-specific parsers.
- Google Sheets integration and preview-first sync flows.
- Systemd service and fallback timer definitions for long-running deployment.

[unreleased]: https://github.com/nickyez/mymoney/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/nickyez/mymoney/releases/tag/v1.1.0
[1.0.0]: https://github.com/nickyez/mymoney/releases/tag/v1.0.0
