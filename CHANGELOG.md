# Changelog

All notable changes to AllDebrid-Client are documented here.

---

## [0.7.0] — 2026-04-15

### Added
- **PostgreSQL support** — optional alternative to SQLite, three modes:
  - `sqlite` — default, fully backward compatible, no changes required
  - `postgres` — external PostgreSQL instance (configure via settings)
  - `postgres_internal` — managed PostgreSQL container via Docker Compose
- **Internal PostgreSQL container** (`docker-compose.yml` profile `postgres`)
  - Start with `COMPOSE_PROFILES=postgres docker compose up -d`
  - Persistent data via named Docker volume `postgres_data`
  - App waits up to 30 s for PostgreSQL to be ready on startup
  - Automatic fallback to SQLite if PostgreSQL is unreachable
- **Bridge mode support** for internal PostgreSQL
  - Shared `alldebrid-net` Docker network for container name resolution
  - `PG_HOST` env var override for isolated bridge setups (Unraid)
  - Full setup instructions in `docs/postgresql.md`
- **Bidirectional database migration** (`POST /api/admin/migrate`)
  - SQLite → PostgreSQL and PostgreSQL → SQLite
  - `dry_run` mode for validation without writing
  - `force` flag for overwriting existing target data
  - Post-migration row-count validation
  - Full safety guarantees — no silent data loss
- **Discord webhook improvements**
  - Rich embeds with structured fields (Source, Files, Size, Destination, Time)
  - `send_added()` — dedicated torrent-added event with source label
  - `send_complete()` — completion with file count, size, destination, client
  - `send_error()` — error with reason and context fields
  - `send_partial()` — partial download summary with filter counts
  - Separate `discord_webhook_added` URL for added-event notifications
  - Deduplication (same message within 30 s suppressed)
  - Discord 429 rate-limit handling with automatic retry
- **New torrent-added webhook** — triggered for both magnets and `.torrent` files
- **Expanded statistics** (`GET /api/stats`)
  - `completed_count`, `error_count`, `total_count`
  - `success_rate_pct` — percentage of completed vs all terminal torrents
  - `completed_last_7d` — completions in the last 7 days
  - `avg_download_duration_seconds` — average download duration
  - `avg_torrent_size_bytes` — average completed torrent size
  - `db_type` — active database backend (including `sqlite_fallback`)
- **New dashboard insight cards**: Last 7 days, Success Rate, Avg Duration, Avg Size, Database
- **Database status card** — shows active backend; highlights `⚠️ SQLite (PG Fallback)` when fallback occurred
- **Test PostgreSQL button** in Settings — shows host, port, database and server version
- **`GET /api/stats/detail`** — daily completions (14 days), sources breakdown
- **`GET /api/admin/migrate/validate`** — validate migration without writing
- `docker-compose.postgres.yml` convenience override file
- `.env.example` documenting `POSTGRES_PASSWORD` and `TZ`
- Documentation: `docs/postgresql.md`, `docs/migration.md`, `docs/aria2-robustness.md`, `docs/discord-webhooks.md`, `docs/statistics.md`

### Fixed
- **Dashboard Completed counter always showing 0** — `_delete_magnet_after_completion()` was setting `status='deleted'` after successful removal from AllDebrid, hiding all completed torrents from `by_status.completed`. Status now stays `'completed'` permanently.
- **`Cannot write to closing transport`** aria2 error — each HTTP call now creates its own `TCPConnector(force_close=True)`, preventing transport reuse across concurrent requests
- **Torrent-added webhook not firing for `.torrent` watch-folder files** — `_handle_torrent()` was missing the `send_added()` call
- **PostgreSQL internal connection failure in Bridge mode** — container name `alldebrid-postgres` replaces generic `postgres`; shared `alldebrid-net` network enables hostname resolution

### Changed
- All log messages, docstrings, comments, and UI strings are now in English
- `_delete_magnet_after_completion()` no longer sets `status='deleted'` — status remains `'completed'`
- `get_all()` in aria2 service returns `[]` on connection error instead of raising
- New `Aria2ConnectionError` class (subclass of `Aria2RPCError`) for network vs. RPC error distinction
- Dashboard uses `completed_count` from API directly instead of `by_status.completed`
- `docker-compose.yml` uses named bridge network `alldebrid-net`

### Internal
- `db/database.py` — `_DbConnection` abstraction, `get_db()` async context manager
- `db/migration.py` — new migration module
- `backend/main.py` — `_wait_for_postgres()`, `_fallback_to_sqlite()`, backend-aware stuck-download recovery
- 50 unit tests, all passing

---

## [0.6.3] — 2025-??-??

- Fix: avoid pre-creating aria2 destination folders (directory creation is left entirely to aria2)
- Fix: move failed watch files to error folder
- Fix: retry transient AllDebrid API responses
- Feat: add slot-based aria2 queue control
- Fix: match aria2 jobs by target path

## [0.6.2] and earlier

See git log for earlier history.
