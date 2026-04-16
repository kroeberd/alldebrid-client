# Changelog

## [0.8.1] — 2026-04-15

### Fixed
- `Test Discord` now saves the current Discord settings first, so changed username/avatar values are actually used for the test webhook.
- Avatar upload now warns when the generated `/api/avatar` URL points to a private or LAN-only host that Discord cannot reach.
- Remaining aria2-only cleanup: removed stale `direct` defaults and UI fallbacks that no longer matched the current downloader architecture.

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

---

## [0.9.0] — 2026-04-16

### Added

**Sonarr / Radarr Integration** (Settings → Integrations)
- After every completed download: `RescanSeries` sent to Sonarr, `RescanMovie` to Radarr
- Per-service enable/disable toggles + Test button
- Connection test via `/api/v3/system/status`

**Torrent Labels** (Settings → Integrations)
- Optional label per torrent, shown as purple badge in the torrent list
- Predefined label list (comma-separated), empty by default
- Set/clear via Details modal; bulk-clear via bulk action bar

**Bulk Actions**
- Checkbox per torrent row + select-all header checkbox
- Bulk Retry / Delete / Clear Label via the orange action bar
- `POST /api/torrents/bulk`

**Auto-Restart Stuck Downloads** (Settings → Integrations)
- Configurable timeout (hours, default 6h, 0 = disabled)
- Torrents stuck in queued/downloading auto-reset to ready

**AllDebrid Rate Limit** (Settings → Integrations)
- Configurable API calls per minute (default 60)
- Semaphore enforced across all manager instances

**Automatic Backups** (Settings → Backup)
- Backs up config.json + SQLite DB + avatar image
- Default: every 24h, kept for 7 days, stored in `/app/data/backups`
- Manual trigger + backup list in Settings
- `POST /api/admin/backup`, `GET /api/admin/backups`

**Statistics Chart**
- Bar chart showing daily completions over the last 14 days
- Rendered via Chart.js (loaded from CDN)

**Light / Dark Mode Toggle**
- 🌙/☀️ button fixed at bottom-right corner
- Preference stored in localStorage

**Retry Button for Error Torrents** *(existing, now works with bulk)*
- Already present in the actions column

### Changed
- Torrent table: added Checkbox column and Label sub-column in Source column
- DB schema: `label TEXT DEFAULT ''` and `priority INTEGER DEFAULT 0` added to torrents table (migration-safe via ALTER TABLE)
- Settings tabs: added Integrations and Backup tabs

### Fixed
- `_mark_finished()` now passes `name` to Sonarr/Radarr integrations
- 50/50 tests passing
