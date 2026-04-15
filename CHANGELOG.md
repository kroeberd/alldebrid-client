# Changelog

## [0.8.0] — 2026-04-15

### Removed
- **Direct download mode** — aria2 is now the only supported download client.
  This eliminates in-process HTTP transfers, improves resume capability, and
  removes the associated memory and reliability issues.
- **PostgreSQL internal (docker-compose) mode** — removed due to reliability
  issues in Bridge/Unraid setups. Use PostgreSQL external if you need a
  relational database backend (see docs/postgresql.md).

### Added
- **New logo** — redesigned SVG based on radar/sonar aesthetic
- **Discord bot identity settings** — configure bot name and avatar URL
  per-webhook in Settings → Discord. Defaults to the new logo and
  "AllDebrid-Client" as sender name.
- **Database status dot** in sidebar — shows SQLite ✓ / PostgreSQL ✓ /
  SQLite (fallback) ⚠ alongside the AllDebrid and aria2 status indicators
- **"Save first, then test" hint** in the Database settings card
- **"Test DB" button** in the save-bar, visible only when PostgreSQL is selected

### Changed
- **Enable File Filters** now defaults to **off** for new installations
- Beta-banner removed from Dashboard
- Download Client select simplified to aria2-only
- All version strings are now sourced from the `VERSION` file

### Fixed
- `postgres_internal` label removed from DB type selector in UI
- DB type labels cleaned up: `postgres_internal` → removed,
  `sqlite_fallback` shown clearly in orange

---

## [0.7.0] — 2026-04-15

### Added
- PostgreSQL support (sqlite / postgres / postgres_internal)
- Internal PG via Docker Compose profile with bridge network fix
- SQLite fallback when PostgreSQL unreachable at startup
- Bidirectional database migration (SQLite ↔ PostgreSQL)
- Rich Discord embeds with structured fields
- Separate `discord_webhook_added` webhook URL
- Dashboard Completed counter fix
- aria2 `Cannot write to closing transport` fix
- Expanded statistics (success rate, avg duration, avg size, db type)
- All text in English
- 50 unit tests

## [0.6.3] and earlier

See git log for earlier history.
