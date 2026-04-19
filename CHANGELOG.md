# Changelog

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
