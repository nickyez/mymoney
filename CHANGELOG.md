# Changelog

All notable changes to MyMoney are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Spreadsheet ID help dialog in Settings, including URL guidance and an example.

### Changed

- Email-imported transactions can edit only their category and notes.
- Email transactions are appended to the configured Google Sheet after being saved to SQLite.
- Release tooling updates the project version and changelog consistently.

## [1.0.0] - 2026-11-12

### Added

- Initial MyMoney application snapshot.
- FastAPI backend with SQLite-backed transaction storage and authentication.
- React, TypeScript, and Vite dashboard with monthly summaries, charts, category breakdowns, transaction editing, and CSV export.
- Gmail transaction capture through the IMAP watcher and sender-specific parsers.
- Google Sheets integration and preview-first sync flows.
- Systemd service and fallback timer definitions for long-running deployment.

[unreleased]: https://github.com/nickyez/mymoney/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/nickyez/mymoney/releases/tag/v1.0.0
