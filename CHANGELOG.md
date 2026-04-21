# Changelog

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
