# Changelog

## [1.3.23] — 2026-04-27

### Added
- **aria2 live download monitor** — the Download settings tab now shows the
  active aria2 queue with per-job progress, speed, completed/remaining bytes,
  target path, files, and error messages.
- **aria2 job controls** — active aria2 jobs can now be paused, resumed, or
  removed directly from the UI via dedicated API endpoints.
- **Auto-refreshing aria2 queue view** — the live queue refreshes while the
  Download tab is open so users can see what aria2 is doing without leaving the
  client.

## [1.3.22] — 2026-04-27

### Fixed
- **Existing installs with the old Docker default download path are migrated** —
  `/app/data/downloads` is normalised to the documented `/download` mount during
  config validation.

## [1.3.21] — 2026-04-27

### Fixed
- **Built-in aria2 now uses the configured Docker download mount** — internal
  aria2 runs in the same container namespace as the app and therefore ignores
  the external `aria2_download_path` override, using `download_folder` directly.
- **Built-in aria2 startup diagnostics are now visible** — startup failures now
  include process output, exit codes, log tails, and the active download folder
  in the runtime status panel instead of silently reporting an offline RPC.

### Changed
- **Docker defaults now align with the documented `/download` mount** — fresh
  installs default the download folder to `/download`, while the image still
  keeps the legacy `/app/data/downloads` directory for compatibility.

## [1.3.20] — 2026-04-27

### Added
- **Optional built-in aria2 runtime** — the container now includes `aria2c` and
  can run aria2 as a managed internal daemon while still supporting the existing
  external aria2 RPC mode.
- **aria2 runtime controls in the UI and API** — users can inspect built-in
  aria2 status, refresh diagnostics, start, stop, restart, apply tuning, and run
  cleanup from the Download settings tab or via `/api/aria2/runtime/*`.
- **Download performance tuning options** — split count, minimum split size,
  max connections per server, disk cache, file allocation, resume behavior, and
  lowest speed limit are now configurable and applied through aria2 RPC.

### Security
- **Built-in aria2 uses a fixed internal RPC secret and disables direct torrent
  behavior** — the managed daemon listens only on loopback, hides the internal
  secret from the UI, and enforces `follow-torrent=false`, DHT off, peer exchange
  off, and local peer discovery off so downloads remain AllDebrid-delivered
  HTTP(S) transfers.

## [1.3.19] — 2026-04-27

### Fixed
- **AllDebrid torrent failures now emit richer error webhooks consistently** —
  provider-side torrent failures such as no-peer cleanup, repeated polling
  failures, and explicit AllDebrid error states now trigger the error webhook
  with source, provider, AllDebrid ID, status code, reason, and context fields.

### Changed
- **Webhook payloads are more presentable for both Discord and generic
  integrations** — embeds now include repository/app metadata, and non-Discord
  webhooks receive a structured payload with severity, app info, fields, and an
  embed-compatible block for downstream formatting.

## [1.3.18] — 2026-04-27

### Changed
- **Jackett availability filtering now uses a regular dropdown control** —
  the Torrent Search form now presents availability as a standard select field
  with `All torrents` and `Seeded only`, matching the layout and behavior of
  the other search controls.

## [1.3.17] — 2026-04-27

### Changed
- **Jackett search can now hide dead torrents** — the Torrent Search view now
  offers a dedicated “Hide dead torrents” option, and the backend filters out
  results with zero seeders when that toggle is enabled so searches stay focused
  on currently downloadable items.

## [1.3.16] — 2026-04-27

### Changed
- **aria2 memory tuning is now applied immediately on startup and the default
  cleanup profile is more aggressive** — the client now pushes its aria2 memory
  options and runs one housekeeping pass during application startup instead of
  waiting for the next manual test, save cycle, or scheduled purge.

- **aria2 state polling now uses bounded waiting/stopped windows** — the client
  no longer asks aria2 for up to 1000 waiting and 1000 stopped jobs on every
  sync cycle by default. New settings expose dedicated waiting/stopped query
  windows, and the diagnostics panel reports the active limits alongside the
  current aria2 counters.

## [1.3.15] — 2026-04-27

### Fixed
- **Jackett add now falls back more gracefully when a tracker returns an HTML
  login page instead of a torrent file** — some private indexers expose a valid
  search result but require an authenticated tracker session for the direct
  `.torrent` download. The client now detects HTML/login responses explicitly,
  reports them clearly, and uses a synthetic magnet built from the available
  infohash whenever possible so valid results can still be queued.

## [1.3.14] — 2026-04-27

### Fixed
- **Jackett add no longer re-fetches short-lived `.torrent` links unnecessarily**
  after search-time hash enrichment. Downloaded torrent payloads are now cached
  briefly in memory and reused by the add flow, which prevents repeated or
  delayed add clicks from invalidating one-time tracker URLs and falling into
  `HTTP 404`.

- **Jackett add now always sends the resolved result hash from the frontend**
  when available, and the UI keeps an in-flight state per result to avoid
  duplicate add requests while a torrent is already being queued.

- **Changelog readability improved in light mode** — the changelog panel now
  uses the regular text color instead of a washed-out blue tone, and inline code
  gets stronger contrast in both themes.

## [1.3.13] — 2026-04-27

### Changed
- **Dark and light themes were rebalanced around the radar logo palette** — the
  dark mode now uses deeper navy surfaces with warmer amber accents that match
  the logo more closely, while the light mode gets stronger text contrast and
  clearer panel separation. Cards, inputs, tables, modals, toasts, and the
  sidebar now share the same visual language so both themes are easier to read.

## [1.3.12] — 2026-04-27

### Fixed
- **Jackett torrent downloads are now more tolerant of indexer-specific download
  links** — the client now resolves relative Jackett download URLs against the
  configured Jackett base URL, injects the API key when the download stays on
  the Jackett host, and also harvests magnet links from additional Jackett
  fields such as `Guid`, `Comments`, `Details`, and `InfoUrl`. This improves the
  add flow for results whose direct `.torrent` link previously returned `HTTP
  404` even though the item was otherwise valid.

## [1.3.11] — 2026-04-27

### Fixed
- **Jackett searches now backfill missing result hashes from `.torrent` files**
  when an indexer omits `InfoHash` in the search response. The client now
  derives the torrent infohash from the downloaded torrent metadata, uses that
  for result matching, and also forwards it into the add flow. This prevents
  already added or completed torrents from showing up as `New` again just
  because the original Jackett result did not include a stable hash.

## [1.3.10] — 2026-04-26

### Fixed
- **Jackett `.torrent` adds now preserve the Jackett infohash as the primary
  local identity** — when a result was added through the `.torrent` upload path,
  the client previously stored the AllDebrid-returned hash or fallback ID. That
  made later Jackett searches miss already downloaded items and show them as
  `New` again. The Jackett add route now forwards the original result hash into
  the upload path so the local torrent record stays aligned with later Jackett
  search results.

## [1.3.8] — 2026-04-26

### Fixed
- **Jackett results could regress to `New` on later searches** — when a search
  result came back without a stable hash, the UI only matched previously added
  items by hash and forgot completed downloads on later searches. The backend
  now also matches exact Jackett titles against torrent names and downloaded
  file names, so previously added or completed items remain marked correctly.

### Changed
- **Jackett sorting moved to the table headers** — instead of a separate sort
  dropdown, the search result headers are now clickable. Each click cycles the
  selected column through default direction, reverse direction, and back to the
  original backend order on the third click.

## [1.3.7] — 2026-04-26

### Changed
- **Jackett indexer picker now uses a regular dropdown again** — the temporary
  multi-select list was functional but visually too heavy for the search bar.
  The UI now uses a standard dropdown like the other controls and includes an
  explicit `All Indexers` option.

### Documentation
- **README refreshed for the current 1.3.x feature set** — added Jackett search,
  reporting webhook, database maintenance, Fenrus status endpoint, and the
  expanded REST API surface.

- **Unraid templates refreshed** — updated both the in-repo template and the
  external `kroeberd/unraid-templates` metadata so the AllDebrid-Client
  description matches the current capabilities and release line.

## [1.3.6] — 2026-04-26

### Fixed
- **Jackett add flow rejected valid `.torrent` results as invalid magnets** — the
  backend `POST /api/jackett/add` route previously sent `magnet or torrent_url`
  straight into the magnet-only manager path. When a Jackett result exposed only
  a `.torrent` download URL, the client incorrectly raised
  `Invalid magnet: no btih hash found`. The add flow now prefers downloading and
  uploading the `.torrent` file to AllDebrid first, and only falls back to the
  magnet when the torrent-file path fails and a magnet is available.

- **Jackett health checks were too narrow and could show false HTTP 400/502
  failures** — the backend connection test and indexer-loading path now try
  multiple Jackett-compatible endpoints, including Torznab indexer discovery and
  the actual `indexers/all/results` search endpoint. This makes the sidebar dot
  and the Test Connection action much more tolerant of setup differences.

### Added
- **Jackett search now marks already added torrents** — search results are
  annotated against existing torrent hashes in the database so previously added
  items show their current local status instead of looking new every time.

- **Multi-indexer selection in Jackett search** — the Search view now supports
  selecting multiple individual Jackett indexers instead of only a single
  dropdown value.

- **Client-side Jackett result sorting** — the Search view can now sort by
  seeders, name, size, and publish date.

## [1.3.5] — 2026-04-26

### Fixed
- **Jackett test connection still returned 502 on some valid setups** — the
  backend test previously relied on `GET /api/v2.0/server/config` only. Some
  Jackett installations or reverse-proxy setups do not expose that endpoint
  consistently even though authenticated API access works. The connection test
  now falls back to the authenticated `GET /api/v2.0/indexers?configured=true`
  endpoint and treats a successful indexer listing as a valid Jackett connection.

- **Settings test actions reset the active tab back to General** — saving or
  testing settings caused the settings UI to re-render and reactivate the first
  tab. The frontend now preserves and restores the currently active settings tab
  across Save, Discord test, aria2 test, and Jackett test actions.

- **Jackett had no sidebar health indicator** — the sidebar now shows a dedicated
  Jackett status dot with `ok`, `warn`, or `error` state based on whether Jackett
  is enabled, fully configured, and reachable from the backend.

## [1.3.4] — 2026-04-26

### Fixed
- **Jackett test connection used stale saved settings** — the Jackett Settings tab
  tested the backend connection without first persisting the values currently
  entered in the form. This meant users could enter a valid URL and API key,
  click **Test Connection**, and still get a backend error because the test was
  executed with the previous saved configuration. The Jackett test action now
  saves the current settings first, reloads them from the backend, and only then
  calls the Jackett test endpoint.

## [1.3.3] — 2026-04-26

### Fixed
- **Jackett settings tab rendering was broken in the 1.3.x UI** — the
  `tab-jackett` panel was accidentally nested inside the Reporting panel in the
  settings renderer, which caused the Jackett tab to stop behaving like an
  independent tab. The settings markup now closes the Reporting panel before the
  Jackett panel starts, so the Jackett tab can be opened normally again.

- **Settings DOM contained duplicated PostgreSQL test buttons** — several
  unrelated settings panels accidentally rendered extra `btn-test-postgres`
  elements with the same `id`, making the settings DOM more fragile and harder
  to reason about. These duplicate button injections were removed, leaving only
  the intended top-level database test action.

## [1.3.2] — 2026-04-26

### Fixed
- **Jackett Settings tab showed empty content** — in v1.3.0 the Jackett settings
  panel was inserted into the static HTML *outside* the `renderSettings()` template
  literal; v1.3.1 moved it into the template. This release confirms the fix and
  adds the additional improvements below.

- **Jackett webhook `_send()` return value ignored** — `send_jackett_webhook()` did
  not check whether `_send()` succeeded; a failed webhook send (HTTP error, rate
  limit) was silently swallowed. Now logs a WARNING when `_send()` returns `False`.
  Also passes `bypass_dedup=True` so that adding the same torrent twice still sends
  two webhook notifications.

### Changed
- **Search view: richer Add feedback** — the Add button now shows `Adding…` while
  the request is in flight, changes to `✅ Added` (green) on success, and shows
  the link type (`magnet` or `torrent URL`) plus the AllDebrid ID in the success
  toast. On error the button re-enables immediately.

- **Search view: smart not-configured state** — `initSearchView()` now checks
  `jackett_url` and `jackett_api_key` in addition to `jackett_enabled`; if any
  is missing the search bar is hidden and the "not configured" hint is shown.
  Auto-focuses the query input when everything is configured.

- **Search view: search bar has stable ID** (`id="jackett-search-bar"`) so
  `initSearchView()` can show/hide it independently of the not-configured card.

## [1.3.1] — 2026-04-26

### Fixed
- **Jackett Settings tab not visible** — the Jackett panel was inserted into the
  static HTML outside of the `renderSettings()` template literal. Because
  `settings-form.innerHTML` is fully replaced on every Settings open, the panel
  was overwritten immediately and never shown. Fixed by embedding the panel
  directly inside the template literal so it is rendered with every other tab.

- **`send_jackett_webhook()` import error** — the function attempted to import
  `_fmt_size` from `services.notifications`, which does not export that name.
  This caused an `ImportError` whenever a torrent was added via Jackett search
  and a webhook was configured. Fixed by using the local `_fmt_size` from
  `services/jackett.py` instead.

## [1.3.0] — 2026-04-26

### Added
- **Jackett torrent search integration** — search any tracker indexed by a Jackett
  instance directly from the AllDebrid-Client UI and add results to the download
  queue with a single click.

  **Backend (`backend/services/jackett.py`, new):**
  - `search()` — proxies `GET /api/v2.0/indexers/all/results` on the configured
    Jackett instance; normalises every result to a stable dict with `title`,
    `indexer`, `size_bytes`, `size_human`, `seeders`, `leechers`, `pub_date`,
    `magnet`, `torrent_url`, `has_link`; sorts by seeders descending.
  - `test_connection()` — pings `/api/v2.0/server/config`, validates API key,
    returns Jackett version string.
  - `get_indexers()` — returns the list of configured Jackett indexers (id + name)
    for the filter dropdown.
  - `send_jackett_webhook()` — fires a `jackett_torrent_added` Discord embed;
    uses `jackett_webhook_url` when set, falls back to `discord_webhook_url` +
    `discord_notify_added` flag; silently skips when both are unconfigured.
  - Error handling: Jackett unreachable, invalid API key, HTTP error, no results,
    missing magnet/torrent link — all produce a structured `error` field instead
    of raising.

  **API (`backend/api/routes.py`), 5 new routes:**
  - `POST /settings/test-jackett` — connection + API key test
  - `GET  /jackett/indexers` — live indexer list for the filter dropdown
  - `POST /jackett/search` — search (body: `query`, `category`, `tracker`, `limit`)
  - `POST /jackett/add` — add magnet or torrent URL to the download queue; fires
    webhook on success
  - `GET  /jackett/categories` — standard Torznab category list

  **Config (`backend/core/config.py`), 4 new fields:**
  `jackett_enabled`, `jackett_url` (default `http://localhost:9117`),
  `jackett_api_key`, `jackett_webhook_url`.

  **Config validator (`backend/core/config_validator.py`):**
  `jackett_url` and `jackett_webhook_url` are now validated for HTTP(S) format
  on startup.

  **Frontend (`frontend/static/index.html`):**
  - New **🔍 Search** nav item — hidden automatically when `jackett_enabled` is
    `false`, shown immediately after saving Settings.
  - **Search view** — query field with Enter key support, category dropdown
    (All / Movies / TV / Music / Books / Games / Software / XXX), live indexer
    dropdown (populated from the running Jackett config), Search button.
    Results table: Title, Indexer, Size, Seeds, Peers, Date, per-row Add button.
    Status feedback: searching spinner, empty state, error message, success toast,
    disabled Add button replaced with "Added" on success.
  - **Settings → Jackett tab** — Enable toggle, URL field, API key (password
    input), dedicated webhook URL, live "Test Connection" button with inline
    result.
  - All 4 Jackett fields included in `getFormSettings()` so they are persisted
    on Save.

  **Security:**
  - API key is never sent to the browser; all Jackett requests are proxied
    through the backend.
  - Jackett URL validated on startup; webhook URL validated the same way as all
    other webhook fields.

  **Tests (`backend/tests/test_jackett.py`, 18 new):**
  - `_fmt_size`: zero, negative, bytes, MB, GB
  - `_normalise_result`: all fields, magnet-preferred, torrent-URL fallback,
    no link, date parsing (valid / empty / malformed), missing optional fields,
    Peers→leechers mapping
  - `CATEGORIES`: all_zero, required keys present, positive IDs

## [1.2.15] — 2026-04-21

### Fixed
- **PostgreSQL straggler finalization still failed for large completed torrents** — the
  aria2 straggler pass could repeatedly detect fully completed torrents but fail during
  `_finalize_aria2_torrent()` when writing very large `size_bytes` totals back to
  PostgreSQL. The `UPDATE torrents ... size_bytes=CASE WHEN ? > 0 THEN ? ELSE size_bytes END`
  form kept re-binding the large size value in a way that could still trip PostgreSQL
  type inference on some databases.

  **Fix:** `_finalize_aria2_torrent()` now uses explicit SQL branches:
  - when `total_size > 0`, it writes `size_bytes=?` directly
  - when `total_size == 0`, it leaves `size_bytes` unchanged

  This removes the problematic `CASE WHEN` expression and allows large completed
  torrents to finalize cleanly instead of looping forever in the straggler check.

- **Discord completion/error notification failures logged no useful reason** — some
  webhook failures produced an exception with an empty string, which resulted in log
  lines like `Discord notification failed (...):` with no actionable detail.

  **Fix:** notification logging now includes the exception class name and falls back to
  `repr(exc)` when the exception message is empty, making Discord webhook failures
  diagnosable from the logs.

## [1.2.14] — 2026-04-21

### Fixed
- **`str object cannot be interpreted as an integer`** — regression introduced
  in v1.2.12: `_pg_safe()` converted large Python ints to `str` to work around
  asyncpg's int4 inference, but `str` parameters passed to asyncpg for columns
  typed `INTEGER`/`INT4` (e.g. `provider_status_code`, `polling_failures`) caused
  PostgreSQL to reject them with `invalid input for query argument $N: 'NNNN'
  (str object cannot be interpreted as integer)`.

  **Root cause:** the fix was wrong.  asyncpg 0.29 does support Python int natively
  for any integer column size.  The actual problem was that `size_bytes` columns
  were created as `INT4` (instead of `BIGINT`) in databases that pre-date the
  current schema, and PostgreSQL rejects values > 2 147 483 647 for INT4.

  **Real fix:**
  1. `_pg_safe()` reverted to a no-op (passes values through unchanged).
  2. `executemany()` SQLite branch no longer calls `_pg_safe()` (another
     regression in v1.2.12 where both branches applied it).
  3. New idempotent migration in `_init_db_postgres()`: if `torrents.size_bytes`
     or `download_files.size_bytes` is found to be `INT4`, it is altered to
     `BIGINT` at startup.  This runs once and is a no-op on new or already-
     migrated databases.

## [1.2.13] — 2026-04-21

### Fixed
- **PostgreSQL int32 overflow in fetchall/fetchone** — `_pg_safe()` was applied
  to `execute()` and `execute_returning_id()` in v1.2.12 but not to the standalone
  `fetchall()` and `fetchone()` methods on `_DbConnection`. Any SELECT with a large
  int parameter (e.g. `WHERE torrent_id=<big_id>`) could still trigger the overflow.
  Now applied to all four query methods.

### Changed
- **Reporting: Report Window field added to Settings UI** — `stats_report_window_hours`
  was already in the config and used by the scheduler but had no UI input field.
  Added to the Reporting tab alongside the interval setting, and included in
  `getFormSettings()` so it is saved when pressing Save Settings.

## [1.2.12] — 2026-04-21

### Fixed
- **PostgreSQL: "value out of int32 range" for size_bytes / alldebrid_id** —
  asyncpg 0.29 maps Python `int` to PostgreSQL `int4` (32-bit) by default.
  Values larger than 2 147 483 647 — such as `size_bytes` for files ≥ 2 GB or
  `alldebrid_id` values issued by AllDebrid — triggered
  `invalid input for query argument $N: <value> (value out of int32 range)`.

  This caused every sync cycle to fail with an exception caught by the straggler
  check's `try/except`, so the 13–14 stuck torrents were detected but never
  finalised (the exception prevented `_finalize_aria2_torrent` from completing).

  Fix: new `_pg_safe()` helper in `db/database.py` converts any Python `int`
  outside the int4 range to `str` before passing it to asyncpg.  PostgreSQL
  casts the string to the target column type (`BIGINT`, `TEXT`, etc.) without
  error.  Applied consistently in `execute()`, `execute_returning_id()`, and
  `executemany()`.

## [1.2.11] — 2026-04-21

### Fixed
- **Downloads not completing despite files already downloaded** — root cause:
  `sync_aria2_downloads()` and `deep_sync_aria2_finished()` both query
  `download_files WHERE status IN ('queued', 'downloading', 'paused')`.
  When all files were already marked `completed` in a previous sync cycle
  (but `_finalize_aria2_torrent()` subsequently threw an exception, or the
  container restarted after the file update but before finalisation), the
  query returned zero rows, `touched` remained empty, and `_finalize` was
  never called again — leaving the torrent stuck in `queued`/`downloading`
  indefinitely.

  Fix: both sync functions now run a **straggler query** after their main loop:
  ```sql
  SELECT DISTINCT torrent_id FROM download_files
  WHERE torrent_id IN (SELECT id FROM torrents WHERE status IN ('queued','downloading') ...)
  GROUP BY torrent_id
  HAVING SUM(CASE WHEN blocked=0 AND status != 'completed' THEN 1 ELSE 0 END) = 0
     AND SUM(CASE WHEN blocked=0 THEN 1 ELSE 0 END) > 0
  ```
  Any torrent found by this query (active status, but all non-blocked files
  already completed) is passed directly to `_finalize_aria2_torrent()`,
  which marks it completed, deletes the magnet from AllDebrid, and sends
  the Discord notification.

## [1.2.10] — 2026-04-21

### Fixed
- **aria2 completion/error reconciliation is now safer** — torrents that already
  have all required files completed are no longer reset to a re-download/error
  state on startup just because the finished aria2 entry has already been
  cleaned up.

- **`removed` aria2 jobs are no longer treated as successful downloads** — the
  sync and import paths now treat `removed` as lost state that must be
  re-queued or revalidated, instead of incorrectly marking files as completed.

- **Regression coverage for post-download false-error cases was added** — new
  manager tests now lock in the expected behavior for completed torrents with
  missing aria2 entries and for `removed` aria2 jobs during sync.

## [1.2.9] — 2026-04-21

### Fixed
- **Disabling FlexGet now takes effect immediately** — toggling `flexget_enabled`
  off now clears in-memory FlexGet runtime state and hides stale running-task
  indicators instead of continuing to look active until the next natural cycle.

### Added
- **Dedicated database maintenance settings** — the Database tab now includes
  separate controls for database-only backups and database wiping, independent
  from the existing full data backup settings.

- **Database backup endpoint and UI action** — you can now export JSON snapshots
  of the database tables on demand and browse the stored database backup sets
  directly from the settings UI.

- **Guarded database wipe workflow** — a dedicated wipe toggle, pause
  requirement, confirmation step, and optional automatic pre-wipe database
  backup were added to make destructive cleanup explicit and safer.

## [1.2.8] — 2026-04-21

### Fixed
- **Reporting settings now persist correctly** — the reporting time-window
  selector is now backed by a real persisted setting,
  `stats_report_window_hours`, instead of being a UI-only value.

- **Scheduled reports now use the configured report window instead of the send
  interval** — automatic reporting previously sent a report covering the same
  number of hours as the schedule cadence. The scheduler now keeps those values
  separate and uses `stats_report_interval_hours` only for cadence and
  `stats_report_window_hours` for report content.

- **Reporting settings reload cleanly after save** — the settings UI now
  refreshes itself from `GET /api/settings` after saving or running inline
  settings-dependent tests, so persisted values and sanitized values are shown
  immediately instead of relying on the pre-save form payload.

## [1.2.7] — 2026-04-21

### Fixed
- **Settings are now sanitized on save** — `PUT /api/settings` now runs the
  same config validation and sanitization path that was previously only applied
  during startup. Invalid Discord avatar values and malformed schedule JSON are
  corrected immediately instead of persisting until the next restart.

- **Scheduled stats reporting now matches the UI fallback contract** — the
  scheduler previously required `stats_report_webhook_url` to be set, even
  though the UI and manual send path documented a fallback to the main Discord
  webhook. Automatic reports now use the same fallback logic as manual reports.

- **Discord avatar upload now generates more usable URLs** — avatar uploads now
  respect `PUBLIC_BASE_URL` when configured and return a user-facing warning if
  the generated URL is private or loopback and therefore likely unreachable by
  Discord.

- **Statistics webhook identity now uses the same avatar rules as regular
  notifications** — reporting webhooks now reuse the shared Discord identity
  helper, ensuring SVG URLs and data URIs are excluded consistently across all
  webhook senders.

- **Release metadata version references were synchronized** — the Docker image
  label and the Unraid template overview version are now aligned with the
  repository version.

## [1.2.6] — 2026-04-21

### Changed
- **Discord avatar field: hint updated** — placeholder and help-text now
  explicitly state that Discord only accepts PNG/JPG/WEBP (not SVG).
- **`_send()`: success logged at DEBUG level** — previously no logging on
  successful delivery; now logs `Discord notification sent: <title>` at DEBUG
  and includes the title in the error message on failure for easier tracing.

## [1.2.5] — 2026-04-21

### Fixed
- **Discord webhooks failing with HTTP 400** — the root cause of webhook
  problems: `discord_avatar_url` defaulted to a `.svg` URL
  (`raw.githubusercontent.com/…/logo.svg`). Discord's webhook API rejects SVG
  for `avatar_url` with HTTP 400. Every notification without an explicitly
  configured avatar therefore silently failed.

  Fixes applied across the entire webhook stack:
  - `config.py`: `discord_avatar_url` default changed from the SVG URL to `""`
  - `notifications._get_discord_identity()`: now rejects SVG URLs (in addition to
    data URIs) and returns empty string — Discord will fall back to the webhook's
    own avatar
  - `config_validator`: SVG URLs in `discord_avatar_url` are now detected and
    cleared on startup, so existing configs with the bad default are auto-corrected
  - All three webhook senders (`notifications.py`, `flexget.py`, `stats.py`):
    `avatar_url` is now only included in the payload when it is non-empty

- **`test()` always returned success** — `_send()` logged HTTP errors at WARNING
  level but never raised, so `test()` always returned `True` and the route always
  responded `{"ok": True}`. Fixed: `_send()` now raises on non-200/204 status,
  returns `bool`, and the test route correctly surfaces failures as HTTP 502.

- **Test-button deduplicated on second click** — the test message is always
  identical, so a second click within 30 s was silently suppressed by the dedup
  guard. `test()` now passes `bypass_dedup=True` to `_send()`.

- **FlexGet webhook connection leak** — `resp = await s.post(url, …)` instead of
  `async with s.post(url, …) as resp:` left the HTTP connection open.

## [1.2.4] — 2026-04-20

### Fixed
- **XSS: user-controlled strings inserted into innerHTML without escaping** —
  torrent names, filenames, error messages, event log messages and FlexGet task
  labels were all interpolated directly into `innerHTML` template literals.
  A torrent name like `<img src=x onerror=alert(1)>` (set via AllDebrid,
  the watch folder, or the API) would execute arbitrary JavaScript.
  Added `esc(s)` helper (HTML-escapes `& < > " '`) and applied it to all
  user-controlled values inserted into the DOM via `innerHTML`:
  `t.name`, `t.label`, `t.error_message`, `f.filename`, `f.block_reason`,
  `ev.message`.
- **PostgreSQL: performance indexes were missing** — the 4 indexes added in
  v1.2.3 for SQLite were not added to `_init_db_postgres`. Fixed.
- **Flaky deduplication test** — `test_deduplication_suppresses_duplicate_within_window`
  patched `aiohttp.ClientSession` on a `SimpleNamespace` stub (set by another test
  file), making the mock silently fail. Rewritten to test the dedup state-machine
  directly without network patching.
- **Duplicate `# 3.` comment in startup** — two PostgreSQL sync blocks were both
  labelled `# 3.`; second renamed to `# 3b.` for clarity.

## [1.2.3] — 2026-04-20

### Fixed
- **TOCTOU race in `_start_download`** — the in-memory guard `torrent_id in self._active`
  was checked synchronously, but `_active.add()` happened *after* several `await`
  expressions (DB queries). Two concurrent tasks could both pass the check and both
  start the same download. Fixed: `_active.add()` now happens immediately after the
  synchronous check, before any `await`. If subsequent validation (DB status check)
  decides to skip, the id is discarded via `finally: _active.discard()`.
- **`stats_snapshots` table grew without bound** — `stats_snapshot_keep_days` existed
  in config but was never applied. `take_stats_snapshot()` now prunes rows older than
  `keep_days` in the same transaction as the insert.
- **Missing DB indexes** — no indexes existed despite every sync query filtering on
  these columns. Added (idempotent `CREATE INDEX IF NOT EXISTS`):
  `idx_dlfiles_torrent_status (torrent_id, status, blocked)`,
  `idx_torrents_alldebrid_id (alldebrid_id)`,
  `idx_torrents_status (status)`,
  `idx_events_torrent_id (torrent_id)`.
- **Duplicate `/stats/comprehensive` route** — defined twice in `routes.py`; the second
  (formatted report) now lives at `/stats/report-data`.
- **`backup._cfg()` silent failure** — exceptions were swallowed without logging;
  now logged at WARNING level.

## [1.2.2] — 2026-04-20

### Fixed
- **_start_download guard broke legitimate restarts** (regression from v1.2.1) —
  the DB-status guard checked `status IN (queued, downloading, paused)` but
  `_reset_torrent_for_redownload()` sets `status='downloading'` before calling
  `_start_download`. The guard therefore blocked the intended restart.
  Fixed: guard now checks whether active `download_files` rows exist, not just
  status. If download_files is empty (as after a reset) the restart is allowed
  even when status is `downloading`.
- **safe_name: torrent names starting with `..`** — `safe_name("../evil")` produced
  `.._evil` which starts with `..`. While not a path traversal (slashes are already
  replaced), it created confusing folder names. `safe_name` now strips leading dots.

### Added
- **Comprehensive download-logic test suite** (`tests/test_download_logic.py`,
  37 tests covering):
  - Status machine invariants (`_terminal_torrent_status`, restartable set)
  - `is_blocked`: extension, keyword, size filters
  - `_download` final-status decision for all-blocked / partial / normal cases
  - `_finalize_aria2_torrent` completion-detection logic
  - `normalize_provider_state` AllDebrid status-code mapping
  - `safe_name` / `safe_rel_path` path sanitisation
  - Config validator integration with download settings

## [1.2.1] — 2026-04-20

### Fixed
- **Downloads restarted while already in progress** — three independent fixes for
  a race condition that caused active torrents to be downloaded again:

  **Root cause:** `full_alldebrid_sync` checked `local_status in ('error', 'pending',
  'uploading', 'processing', 'ready', 'queued')` before calling `_start_download`.
  `'queued'` was incorrectly included — a torrent with `status=queued` is already
  being downloaded by aria2. After a container restart `_active` (the in-memory
  guard) is empty, so the `torrent_id in self._active` check passes, and `_download`
  is called again, which begins with `DELETE FROM download_files WHERE torrent_id=?`
  — wiping the existing aria2 GIDs and creating duplicate entries.

  **Fix 1 — `full_alldebrid_sync`**: `'queued'`, `'downloading'`, and `'paused'`
  removed from the restartable set. Torrents in these states are handled by
  `_dispatch_pending_aria2_queue` / `reconcile_aria2_on_startup`, not by a fresh
  `_start_download`.

  **Fix 2 — `_start_download` DB guard**: before adding to `_active`, queries the
  DB and returns early if `status` is already `queued`, `downloading`, or `paused`.
  This guards against post-restart races where `_active` is empty but the torrent
  is genuinely mid-download.

  **Fix 3 — `_download` stale aria2 cleanup**: before deleting `download_files`
  rows, cancels any active aria2 GIDs for the torrent. Without this, re-downloading
  a legitimately stale torrent (e.g. after `error`) would leave the old aria2 entry
  downloading in parallel.

## [1.2.0] — 2026-04-19

### Fixed
- **Filtered torrents not removed from AllDebrid** — when ALL files in a torrent
  were blocked by the filter rules, `_download()` set `final_status='error'` instead
  of `'completed'`, so `_delete_magnet_after_completion()` was never called and the
  torrent stayed on AllDebrid indefinitely. Analysis of all filter scenarios:

  | Scenario | Before | After |
  |---|---|---|
  | Some files blocked, rest downloaded | `status=queued` → downloads → `completed` → **deleted from AllDebrid** ✓ | unchanged ✓ |
  | All files blocked | `status=error` → stays on AllDebrid forever ✗ | `status=completed` → **deleted from AllDebrid** ✓ |

  Additional improvements for the all-blocked case:
  - Event log message: `"All N file(s) filtered/blocked — marked completed, removed from AllDebrid"`
  - Discord 'completed' notification suppressed (partial-filter notification was already sent)
  - Event messages for partial-filter runs now include the blocked count

## [1.1.9] — 2026-04-19

### Added
- **aria2ng shortcut in sidebar** — when an aria2 URL is configured in Settings,
  a clickable `↗ aria2ng` link appears at the bottom of the sidebar.
  - URL is derived automatically from the configured aria2 JSON-RPC URL:
    host is kept, port is replaced with `6880` (aria2ng default).
    Example: `http://192.168.1.100:6800/jsonrpc` → `http://192.168.1.100:6880/`
  - Link is hidden when no aria2 URL is configured.
  - Updates immediately after saving Settings (no reload required).
  - Opens in a new tab.

## [1.1.8] — 2026-04-19

### Added
- **Config validation and sanitisation at startup** (`backend/core/config_validator.py`)
  Runs as step 0 of the startup sequence — before database init, before scheduler.
  Checks every setting for common problems and automatically fixes the ones that
  can be safely corrected:

  | Check | Action |
  |---|---|
  | `discord_avatar_url` is a data URI | Reset to default logo URL |
  | `flexget_task_schedules_json` is not valid JSON | Reset to `[]` |
  | `db_type` not in `sqlite`, `postgres` | Reset to `sqlite` |
  | `download_client` not `aria2` | Reset to `aria2` |
  | Numeric field below minimum | Clamp to minimum |
  | Numeric field above maximum | Clamp to maximum |
  | URL fields malformed | Warning only (not auto-cleared) |
  | API key suspiciously short | Warning only |

  If any field is corrected, the fixed config is written back to `config.json`
  immediately so the user sees clean values on the next settings page load.
  All issues are logged at WARNING level; a clean config logs a single INFO line.
  14/14 unit tests in `tests/test_config_validator.py`.

## [1.1.7] — 2026-04-19

### Fixed
- **Settings changes not visually confirmed after Save** — `saveSettings()` now
  calls `renderSettings()` after a successful PUT, so any value normalised or
  adjusted by the backend (e.g. defaults, type coercion) is immediately reflected
  in the form without needing a manual tab switch.
- **Duplicate config fields** — `flexget_retry_delay_minutes` and
  `flexget_task_timeout_seconds` were declared twice in `AppSettings` (Pydantic
  keeps the last definition, so behaviour was correct, but it was confusing and
  caused the field to appear twice in serialised config). Removed the duplicates;
  legacy `flexget_schedule_minutes` and `flexget_jitter_seconds` kept for
  migration compatibility.
- **`postgres_application_name` not saved** — field existed in `AppSettings` but
  was missing from `getFormSettings()`, so it was always reset to its default on
  Save. Added to the form settings collection.
- **Full button/API audit** — verified every `onclick` handler maps to a defined
  JS function, every JS function's `api()` call maps to an existing backend route,
  and every `s-{field}` DOM element is covered by `getFormSettings()`.
  No broken buttons found; the above missing field was the only gap.

## [1.1.7] — 2026-04-20

### Fixed
- **Mobile: sidebar footer (dots + alldebrid.com link) always visible** —
  The `nav` element now has `flex: 1` and `overflow-y: auto`, so it scrolls
  independently. The `.sidebar-footer` has `flex-shrink: 0` and always stays
  at the bottom, even when the nav list is longer than the screen.
  Applies to both the desktop sticky sidebar and the mobile overlay sidebar.
- **Mobile: Settings Save/Test buttons visible** — `.save-bar` is now
  `position: sticky; bottom: 0` on mobile instead of `position: static`,
  so it stays anchored to the bottom of the viewport while scrolling through
  settings. `padding-bottom: env(safe-area-inset-bottom)` added so it clears
  the browser navigation bar on notched phones (iPhone, Android gesture nav).
- **Mobile: safe area insets** — `viewport-fit=cover` added to the viewport
  meta tag so `env(safe-area-inset-bottom)` works correctly on all devices.

## [1.1.6] — 2026-04-19

### Changed
- **Discord webhook embeds — visual improvements** across all three services
  (notifications, FlexGet events, statistics reports):
  - **Timestamp**: replaced raw ISO-8601 string in field values
    (`2026-04-19T17:16:25.341029+00:00`) with Discord's native `timestamp`
    embed field — Discord renders this automatically in the user's local timezone
    (e.g. "Today at 7:16 PM")
  - **Footer**: shortened from `AllDebrid-Client v1.1.6 — https://github.com/…`
    to just `AllDebrid-Client v1.1.6`, with the configured avatar as footer icon
  - **Avatar / username**: all three webhook senders now read
    `discord_avatar_url` and `discord_username` from Settings and include
    them in every payload. Discord caches the avatar image by URL — setting it
    once in Settings is sufficient, no repeated downloads occur.
  - Time fields in notification embeds use `dd.mm.yyyy, HH:MM UTC` format

## [1.1.6] — 2026-04-19

### Fixed
- **Pause/Resume button had no effect** — frontend called `/api/settings/pause`
  and `/api/settings/resume` which do not exist. Correct endpoints are
  `/api/processing/pause` and `/api/processing/resume`.

## [1.1.5] — 2026-04-19

### Fixed
- **loadStats retry loop ran 10× even on success** — `loadStats()` returned
  `undefined` (bare `return;`) on success. The startup retry loop tested
  `while (!loaded)` — `!undefined === true` — so it kept retrying even after
  `/api/stats` had been successfully fetched and the DOM updated.
  Fix: `loadStats()` now returns `true` on success and `false` on error.
  The internal 5-attempt retry inside `loadStats()` was also removed — the
  outer IIFE loop already handles retries, no duplication needed.
- **aria2 dot slow to appear** — `checkConnections()` was started only after
  the `loadStats` retry loop finished. Now it fires immediately at startup
  parallel to the stats retry, so the aria2 dot appears as soon as the
  aria2 test resolves.

## [1.1.4] — 2026-04-19

### Fixed
- **Root cause of all dashboard loading failures found and fixed** —
  Browser console showed:
  `Uncaught ReferenceError: async is not defined  (line 2544)`
  A stray `async ` fragment on its own line (between two function definitions)
  caused the browser to interpret it as an expression statement referencing
  an undefined variable `async`. This threw a `ReferenceError` that aborted
  the **entire script** before any function was defined or any IIFE ran.
  Result: no API calls, no DOM updates, no sidebar dots — only nav() onclick
  handlers worked because the browser had partially parsed the script before
  crashing (function declarations are hoisted, but the runtime error stopped
  the IIFE). Clicking any nav item re-triggered loadStats() which succeeded.
  Fix: removed the stray `async ` line.

## [1.1.3] — 2026-04-19

### Changed
- **Startup: debug status panel** — a small status strip appears below the stat cards
  on page load, showing each step of the startup sequence in real time
  (script start → settings → loadStats attempts → success/failure).
  This panel auto-hides after 10 seconds once stats are loaded, and helps
  diagnose why values were not appearing. The startup sequence is now a
  simple awaited loop (up to 10 attempts) instead of a detached background poller.

## [1.1.2] — 2026-04-19

### Fixed
- **Dashboard empty on load — definitive fix** — replaced the retry-loop approach
  with a persistent background poller (`pollUntilLoaded`) that runs independently
  of the startup `await` chain. The poller fires immediately and retries `loadStats()`
  with growing delays (400ms → 800ms → … → max 3s) until it succeeds, then
  triggers `loadRecent()`, `checkConnections()`, and `checkPremiumStatus()`.
  This means:
  - The startup `await` only blocks for `api('/settings')` (~50ms) and then
    `renderTopbarActions()`. Everything else is truly non-blocking.
  - If the server is slow on first request (DB warmup, etc.), the poller
    keeps retrying silently in the background until data arrives — no user
    interaction required.
  - `loadStats()` simplified back to a single attempt (returns `true`/`false`).
    Retry logic lives in the poller, not in `loadStats()` itself.

## [1.1.1] — 2026-04-19

### Fixed
- **UI values empty on load (root cause found and fixed)** —
  `loadStats()` had no retry logic: if `/api/stats` failed or timed out on the
  first request (common right after container start while the DB connection is
  being established), the `catch` block silently discarded the error and the
  dashboard stayed blank. The user had to click elsewhere to trigger a second
  call that succeeded. Fixed:
  - `loadStats()` now retries up to **5 times** with increasing delays
    (500 ms → 1 s → 1.5 s → 2 s). On permanent failure it sets the
    AllDebrid dot to red and logs to console.
  - **Safety-net setTimeout**: 3 seconds after startup, checks whether
    `s-total` is still blank and triggers a fresh `loadStats()` if so.
  - **Sidebar dots** are set to yellow "checking…" immediately on startup
    (before any API call) so the user sees active feedback, not stale defaults.
  - `checkConnections()` simplified: AllDebrid + DB dots are already set by
    `loadStats()`; `checkConnections()` now only handles the **aria2** dot,
    with up to **3 retries** (800 ms apart) before marking it as offline.

### Added
- `.dot.check` CSS now pulses (animation) to communicate "actively checking".
- `.dot.warn` CSS (yellow, no pulse) for "not configured" states.

## [1.1.0] — 2026-04-19

### Fixed
- **Dashboard still empty on first load** — settings and stats now load truly in
  parallel (`Promise.allSettled`). Previously `await api('/settings')` ran first,
  blocking `loadStats()` and delaying all visible data by the settings round-trip.
  Now both fire simultaneously; dashboard numbers appear as soon as `/api/stats` responds.
- **FlexGet scheduler silently broken** — `flexget_loop` called `run_flexget_tasks_with_retry`
  which was removed in v1.0.9. Every scheduled run threw a `NameError` and was silently
  swallowed. Fixed: scheduler now calls `run_flexget_tasks` directly.
- **FlexGet does not detect task completion** — `_poll_execution` treated HTTP 404
  on the queue URL as "try next URL", looping until timeout. In FlexGet v3 the queue
  entry is deleted when a task completes, so 404 means done. Fixed: two consecutive
  404s on the queue URL are now treated as successful completion.
- **FlexGet task timeout too short** — hardcoded 300s (5 min) caused long-running
  tasks (indexer updates, large RSS feeds) to time out prematurely.

### Added
- `flexget_task_timeout_seconds` config field (default: 0 = 3600s = 1h).
  Configurable in Settings → FlexGet → "Task timeout". Set higher for very long tasks.

## [1.0.9] — 2026-04-19

### Fixed
- **Dashboard still empty on first load** — `checkPremiumStatus()` was `await`ed
  in the startup sequence, blocking all rendering until the AllDebrid API responded
  (1–3s). Changed to fire-and-forget alongside `loadRecent()` and `checkConnections()`.
  Only `loadStats()` is awaited — it populates the dashboard in ~100ms.
- **FlexGet webhook returns HTTP 400 on Discord URLs** — the webhook sent a generic
  JSON payload (`{"event": "...", "source": "flexget"}`) which Discord rejects.
  Fixed: Discord URLs are auto-detected and the payload is formatted as a proper
  Discord embed (`{"embeds": [{"title": ..., "color": ..., "fields": [...]}]}`).
  Non-Discord URLs still receive the raw JSON payload.
  4xx responses from the webhook endpoint now log a WARNING with the response body.

### Changed
- **Per-task FlexGet webhooks removed** — replaced by a single optional FlexGet
  webhook URL in Settings → FlexGet. When empty, falls back to the Discord webhook
  from Settings → Discord. All events (run_started, task_started, task_ok,
  task_error, run_finished, server_unreachable, server_recovered) go through
  one configurable endpoint.

## [1.0.8] — 2026-04-19

### Fixed
- **Dashboard values only appear after first click** — root causes:
  1. `loadStats()` set the DB dot but not the AllDebrid dot; added `setDot('api','ok')` 
     directly in `loadStats()` so AllDebrid is green immediately when stats load
  2. Startup awaited `checkConnections()` (slow: includes `test-aria2` POST) before 
     showing any data; changed to fire-and-forget so stats render first
  3. `loadRecent()` now also runs fire-and-forget alongside `loadStats()`
- **Per-task webhook editor shows JS code as visible text** — root cause:
  `oninput="...split(',')..."` — the single quote inside `split(',')` broke the 
  HTML attribute, leaving `).map(function(e){...})` as literal visible text.
  Fixed by rebuilding `renderFgTaskWebhooks()` using DOM API (`createElement`, 
  `oninput` as JS property) instead of HTML string concatenation — no escaping issues.
- Per-task webhook hint clarified: URL is optional, falls back to global FlexGet webhook

## [1.0.7] — 2026-04-19

### Fixed
- **UI values only visible after first click** — `settingsData` defensive null-guard
  in startup; `loadStats()` guards against null settingsData; `checkConnections()`
  shows `aria2: not configured` (warn dot) instead of blank when aria2 URL is empty
- **FlexGet webhook silent** — webhook calls now log at INFO level (previously DEBUG
  only, invisible in normal logs); webhook failures log at WARNING; added INFO log
  at the start of each FlexGet run showing task list and triggered_by

## [1.0.6] — 2026-04-19

### Fixed
- `fgTaskWebhooks` TDZ (Temporal Dead Zone) error: "can't access lexical declaration
  before initialization" — caused by JS functions and `let` declaration landing
  inside the `innerHTML` template literal instead of the script scope.
  Fixed by:
  - Moving declaration to top-level with `var` (hoisted, no TDZ)
  - Placing all helper functions in script scope before `checkFlexgetRunning`
  - Rewriting `renderFgTaskWebhooks` without template literals in onclick
    attributes (avoids scope issues in inline event handlers)

## [1.0.5] — 2026-04-19

### Added
- Per-task FlexGet webhooks (`flexget_task_webhooks_json`)
  - Each task can have its own webhook URL and event filter
  - Events: task_started, task_ok, task_error (empty = all)
  - Falls back to global FlexGet webhook for unconfigured tasks
  - UI editor in Settings → FlexGet
- Task overlap prevention: per-task asyncio.Lock prevents the same task
  from running more than once simultaneously (skipped runs logged + persisted)
- `GET /flexget/running` endpoint — returns list of currently executing tasks
- `POST /flexget/run/{task_name}` endpoint — run a single task directly
  - Returns HTTP 409 if task is already running
- Sidebar FlexGet indicator now shows task names while running
- `task_started` webhook event fired before each task execution

### Fixed
- flexget.py: removed duplicate function definitions left by Codex merge
- `checkFlexgetRunning` now uses `/flexget/running` (real-time) instead of history
- `flexgetRunSingleTask` uses `/flexget/run/{task}` endpoint + handles 409

## [1.0.4] — 2026-04-19

### Fixed
- FlexGet schedule editor: white circle/dot above Remove button completely eliminated
  by replacing .ttrack toggle with inline-styled toggle (no ::after pseudo-element
  outside its container)
- flexgetRunSingleTask: was missing async keyword

### Added
- Run button per task directly in the schedule editor row
- FlexGet retry on unreachable: waits flexget_retry_delay_minutes (default 5),
  retries once, then sends server_unreachable webhook event
- server_recovered webhook event when FlexGet becomes reachable again after failure
- State deduplication: unreachable/recovered webhooks only fire on state change
- flexget_retry_delay_minutes config field (0 = disabled)
- Retry delay setting visible in Settings → FlexGet

## [1.0.3] — 2026-04-19

UI polish, FlexGet status indicator, Progressbar fix, Webhook fallback.

### Added
- FlexGet running indicator in sidebar (pulsing dot while tasks execute)
- Discord community link under Project in the sidebar
- Progressbar: animated stripe for downloading torrents with no percentage yet

### Changed
- FlexGet webhook and Reporting webhook now fall back to the main Discord
  webhook when no dedicated URL is configured
- FlexGet task schedule Remove button styled correctly (red border, no white circle)

### Fixed
- Progressbar: `prog-fill` now renders correctly (`display:block`, `min-width:0`)
- Progressbar: `completed` torrents always show 100% in green
- Progressbar: `downloading` torrents with 0% show an animated stripe instead of empty bar

## [1.0.2] — 2026-04-19

Release focused on version consistency, richer automation, and webhook-based reporting.

### Added
- Central runtime version loading from the root `VERSION` file via a shared backend helper
- New `/api/version` endpoint
- Per-task FlexGet schedules with independent interval and jitter handling
- Reporting webhook delivery with optional automatic scheduling
- Manual “Send Webhook Now” action in the reporting UI

### Changed
- Moved the AllDebrid integration block above Sonarr and Radarr in the integrations settings
- Frontend sidebar version now resolves from live backend stats instead of hardcoded release text
- Landing page version labels now load dynamically from the repository `VERSION` file
- GitHub release workflow now publishes the current changelog section, including the version heading itself

### Fixed
- Reporting UI and backend route naming are aligned again
- FlexGet scheduling is no longer limited to one global interval for all tasks

## [1.0.1] — 2026-04-19

Maintenance release focused on settings consistency and release metadata cleanup.

### Fixed
- Settings values of `0` now persist and render correctly in the web UI
- Deep filesystem sync can now be properly disabled with `0`
- Full AllDebrid sync can now be properly disabled with `0`
- Stats snapshots can now be properly disabled with `0`
- AllDebrid rate limiting now honors `0 = unlimited`
- Settings save flow now preserves non-visible config values instead of resetting them
- Sidebar version now follows the backend-reported app version instead of relying only on hardcoded UI text

### Changed
- Exposed additional active settings in the UI:
  - `aria2_poll_interval_seconds`
  - `full_sync_interval_minutes`
  - `aria2_error_retry_count`
  - `aria2_error_retry_delay_seconds`

### Removed
- Unused `notification_urls` setting from the config model
- Unused `stats_report_interval_hours` setting from the config model and UI

## [1.0.0] — 2026-04-18

First public release. All core features are stable and production-ready.

### New since 0.9.x
- **FlexGet Integration** — trigger tasks manually or on a schedule (FlexGet v3 API)
  - Correct use of `POST /api/tasks/execute/` with task list in body
  - Async polling via `GET /api/tasks/queue/{id}/`
  - Configurable jitter (±N seconds) for schedule
  - Webhook events: `run_started`, `task_ok`, `task_error`, `run_finished`
- **Statistics & Reporting module** — comprehensive metrics across all activity
  - Configurable time window (1h to ~1 year)
  - JSON export, periodic snapshots
  - Per-table timestamp filters (correct for both SQLite and PostgreSQL)
- **PostgreSQL fully abstracted** — all 45+ DB calls go through `get_db()`
  - `_CursorWrapper`: `(await db.execute(...)).fetchall()` works for both backends
  - Startup sync: missing SQLite rows copied to PostgreSQL on startup
  - Connection wait: 15 × 10 seconds (150s max)
- **Full-Sync** — full AllDebrid reconciliation every 5 min (configurable)
  - Detects `ready` torrents stuck locally as `error` or `queued`
  - Separate loops: `sync_status_loop` (30s) and `full_sync_loop` (5 min)
- **aria2 improvements**
  - RPC serialisation via `_rpc_lock` (one request at a time)
  - 50ms minimum interval between requests
  - `cached_downloads` prevents N×`get_all()` per dispatch cycle
- **Race condition fixed** — no more "success then error"
  - `completed` files removed from sync query
  - `reset_on_sync` checks terminal status before resetting
- **Extended error detection**
  - "Download took more than 3 days" → automatically cleaned up
  - `processing/uploading` > 24h → automatically reset
- **Discord tab** layout fix (misplaced nested button)
- **10 Settings tabs** correctly balanced (no more duplicates)

### Stable features (since 0.8.x / 0.9.x)
- Automatic torrent lifecycle (upload → poll → unlock → aria2 → done)
- Watch folder for `.torrent` and `.magnet` files
- Sonarr / Radarr import triggers
- Discord rich embeds with configurable bot identity
- File filters (extensions, keywords, minimum size)
- Automatic no-peer cleanup
- Stuck download detection and reset
- Automatic backups
- Bidirectional SQLite ↔ PostgreSQL migration
- PostgreSQL fallback to SQLite on startup failure

---

## [0.9.x] — 2026-04-15 to 2026-04-18

Development phase. All fixes and features merged into v1.0.0.

Full patch history: [GitHub Releases](https://github.com/kroeberd/alldebrid-client/releases)

---

## [0.8.0] — 2026-04-15

- New logo (radar/orbit design)
- Discord bot identity configurable (name + avatar URL)
- aria2 as the only download client (direct download removed)
- File filters disabled by default for new installs
- Database status indicator in sidebar
- PostgreSQL fallback indicator

## [0.7.0] — 2026-04-15

- PostgreSQL support
- Rich Discord embeds
- Bidirectional database migration
- Expanded statistics
