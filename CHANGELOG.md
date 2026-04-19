# Changelog

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
