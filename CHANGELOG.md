# Changelog

## [1.5.26] — 2026-05-06

### Added — Max Downloads quick-setting + expanded header badge

**Header badge** (visible when built-in aria2 is running) now shows three values:
`↓ active / max | ↓ limit`
- **active**: how many files aria2 is currently downloading
- **max**: the configured concurrent download limit
- **limit**: the set speed cap (∞ = unlimited)

Previously the badge only showed the live download speed, which is less useful
at a glance than knowing the current caps.

**New "Max DL" widget** in the ⬇ Downloads view (next to the speed limiter):
A compact dropdown (1 / 2 / 3 / 5 / 10 / 20) that applies the concurrent
download limit immediately via RPC **and** persists it to `settings.json` so
it survives container restarts — same two-step approach as the speed limiter.

**Backend:**
- `GET /aria2/global-options` now also returns `max_concurrent_downloads`
- `POST /aria2/global-options` now accepts `max_concurrent_downloads`,
  applies it via `aria2.changeGlobalOption`, and persists it as
  `aria2_max_active_downloads` in settings (alongside the existing
  speed-limit persistence added in v1.5.25)

## [1.5.25] — 2026-05-06

### Fixed — Built-in aria2 ignores max download/upload speed after restart

**Symptom:** Setting a download/upload speed limit in the aria2 speed widget
had no effect after restarting the container or the built-in aria2 process.
The limit was applied via RPC but never persisted — on restart aria2c was
launched without `--max-overall-download-limit` / `--max-overall-upload-limit`.

**Root cause:** `aria2_global_options()` (which builds both the startup command
and the `changeGlobalOption` RPC call) did not include the speed limits.
The config field `max_speed_mbps` existed but was never translated into aria2
options. The `/aria2/global-options` POST endpoint applied limits at runtime
but did not save them.

**Fix:**

- `config.py`: two new persisted fields — `aria2_max_download_limit` (bytes/s)
  and `aria2_max_upload_limit` (bytes/s), default 0 (unlimited)
- `aria2_runtime.aria2_global_options()`: both fields are now included as
  `max-overall-download-limit` and `max-overall-upload-limit`, so they are
  applied both on startup (via `_command()`) and on live reload
  (via `apply_options()`)
- `routes./aria2/global-options` POST: after applying limits via RPC, the
  values are now written to `settings.json` so they survive restarts

## [1.5.24] — 2026-05-05

### Fixed — Magnet link shown as error message when "Add" fails

**Symptom:** Clicking "Add" on a magnet from sites like 0magnet showed a toast
with the entire raw magnet link as the error message:
`❌ Failed to add: magnet:?xt=urn:btih:…&dn=…&tr=…`

**Root causes (two separate paths):**

1. **Backend (`routes.py`):** `except Exception as e: raise HTTPException(400, str(e))`
   — `str(e)` could contain the full magnet string if AllDebrid echoed it back in
   an error response (e.g. invalid-JSON payload that included the submitted magnet).
2. **Frontend (`jackettAdd`):** The existing partial mitigation only handled
   `magnet:` at position 0; longer AllDebrid error messages that included the magnet
   further into the string still showed the full magnet URL.

**Fix:**

*Backend — `_sanitize_error(exc)`* (new helper in `routes.py`):
- Strips all `magnet:?xt=urn:btih:…` links from exception strings using regex
- Strips very long URLs (≥ 80 chars)
- Truncates messages to 200 characters
- Applied to every `raise HTTPException(400, …)` that wraps a caught exception

*Frontend — `sanitizeErrorMsg(msg)` (new helper):*
- Same magnet/URL stripping + 200-char truncation
- Applied in `addMagnet()`, `jackettAdd()`, and all other error toasts

## [1.5.24] — 2026-05-05

### Fixed — Magnet URL shown as error message when add fails

When adding a magnet from Jackett/0magnet fails, the toast used to display
the full raw magnet link as the error text (e.g.
`❌ Failed to add: magnet:?xt=urn:btih:…`). The actual error reason was hidden.

**Root cause:** AllDebrid's API echoes the submitted magnet URL verbatim in
the `error.message` field of failed upload responses.

**Fixes applied at three layers:**

1. **`alldebrid.py` `upload_magnet()`** — when AllDebrid's error message is a
   magnet URL, replaces it with a human-readable description
   (`AllDebrid rejected the magnet (code: MAGNET_INVALID_URL)`).
2. **`routes.py` `/jackett/add`** — sanitises the HTTP 400 detail string;
   a raw `magnet:` URL as the sole error detail is replaced with a generic message.
3. **`index.html` `jackettAdd()`** — defensive client-side guard: if `e.message`
   still contains a magnet URL (e.g. `AllDebrid [X]: magnet:?…`), the toast
   truncates it at the `magnet:` position so the user sees only the code prefix.

## [1.5.23] — 2026-05-05

### Added — Auto-retry when AllDebrid reports "Upload failed" (statusCode 5)

When AllDebrid returns statusCode 5 ("Upload failed") for a torrent, the client
now automatically re-queues the upload instead of marking the torrent as failed.

**Behaviour:**
1. On first statusCode 5: the failed magnet is deleted from AllDebrid, the torrent
   is set back to `uploading`, and the re-upload is scheduled after a configurable delay.
2. A Discord webhook notification is sent for each re-queue attempt (🔄) and
   on permanent failure (❌) if notifications are enabled.
3. After exhausting all retries the torrent is marked `error` with a permanent
   failure message and a final Discord notification is sent.

**New settings (Settings → ⬇️ Download):**

| Setting | Default | Description |
|---------|---------|-------------|
| `upload_fail_retry_count` | 3 | Max re-upload attempts for statusCode 5. Set to 0 to disable. |
| `upload_fail_retry_delay_minutes` | 5 | Minutes to wait between re-upload attempts. |

**Implementation:**
- `manager_v2._apply_provider_update`: statusCode 5 dispatches `_handle_upload_failed()` task
- `manager_v2._handle_upload_failed()`: new async method handling retry logic
- `notifications.send_requeue()`: new Discord embed — 🔄 "Upload failed — re-queued"
- `notifications.send_upload_failed_permanent()`: new Discord embed — ❌ "Upload failed permanently"
- `database._SCHEMA_COLUMNS_TORRENTS`: new `upload_retry_count` column (auto-migrated)
- `config.py`: `upload_fail_retry_count` and `upload_fail_retry_delay_minutes`

## [1.5.22] — 2026-05-05

### Fixed — All GitHub Actions workflows broken by incorrect SHA pinning

v1.5.21 pinned `actions/checkout`, `actions/setup-python`, `actions/cache`,
and `actions/upload-artifact` to commit SHAs fetched via the wrong API endpoint.
This caused every workflow to fail with *"unable to find version"* on "Set up job".

**Fix:** `actions/*` (GitHub first-party, not flagged by CodeQL) reverted to
`@v4`/`@v5` tags. `docker/*`, `peter-evans/*`, `softprops/*` remain pinned to
verified commit SHAs as required by the CodeQL `actions/unpinned-tag` alerts.

### Fixed — 50 CodeQL code-scanning alerts resolved (CWE-117, CWE-209, CWE-829, CWE-918 and more)

**ERRORs (4):**
- `py/log-injection` (jackett.py) — user-supplied search query truncated to 100 chars before logging
- `py/stack-trace-exposure` (routes.py) — `str(exc)` replaced with generic safe messages in API responses; full errors still logged server-side
- `py/partial-ssrf` (jackett.py) — URL validation in `_get_text()` ensures endpoint starts with `http://` or `https://`

**WARNINGs (10):**
- `actions/missing-workflow-permissions` — added `permissions: contents: read` to `tests.yml` and `build-windows-exe.yml`
- `actions/unpinned-tag` — `docker/metadata-action`, `docker/setup-qemu-action`, `docker/setup-buildx-action`, `docker/login-action`, `docker/build-push-action`, `peter-evans/dockerhub-description`, `softprops/action-gh-release` all pinned to full 40-char commit SHAs
- `py/implicit-string-concatenation-in-list` — `database.py` CREATE INDEX strings split with explicit continuation
- `py/redundant-comparison` — `manager_v2.py`: removed redundant `and active_count > 0` (guaranteed by elif branch)

**NOTEs (36):**
- `py/unused-import` — removed unused `asyncio`, `json`, `Tuple`, repeated `import logging` across routes.py, tests, migration.py
- `py/unused-global-variable` — removed unused vars in `windows_main.py`
- `py/empty-except` — 8 bare `except: pass` blocks replaced with `logger.debug()` calls (aria2_runtime.py, manager_v2.py, jackett.py, db_maintenance.py, backup.py, migration.py)
- `py/mixed-returns` — `notifications._send()`: implicit `return None` replaced with explicit `return False`


## [1.5.21] — 2026-05-05

### Fixed — GitHub Actions: all Actions pinned to full commit SHAs

All GitHub Actions across every workflow are now pinned to their full 40-character
commit SHA instead of mutable version tags (e.g. `@v4`). This eliminates the
supply-chain attack vector described in CWE-829 and also fixes the workflow
failures caused by the previous incomplete SHA pinning.

**Workflows updated:** `Docker_Build.yml`, `release.yml`, `tests.yml`,
`build-windows-exe.yml`, `codeql.yml`, `update-dockerhub-description.yml`

**Actions pinned:**
`actions/checkout`, `actions/cache`, `actions/setup-python`,
`actions/upload-artifact`, `docker/metadata-action`, `docker/setup-qemu-action`,
`docker/setup-buildx-action`, `docker/login-action`, `docker/build-push-action`,
`peter-evans/dockerhub-description`, `softprops/action-gh-release`

### Fixed — 50 CodeQL code-scanning alerts resolved

See previous commit for full details. Summary:
- ERRORs: log injection, stack-trace exposure, partial SSRF
- WARNINGs: missing workflow permissions, unpinned action tags,
  implicit string concatenation, redundant comparison
- NOTEs: unused imports, empty except blocks, mixed returns, unused variables

## [1.5.20] — 2026-05-05

### Fixed — "Request timed out" when adding a torrent from search results

**Root cause:** `jackettAdd()` called `api('POST', '/jackett/add', {...})` without
a custom timeout, so the default 8-second `AbortController` from `api()` applied.

The `/jackett/add` backend endpoint can take significantly longer than 8 s because it:
1. Downloads the `.torrent` file from the tracker via Jackett (up to 30 s backend timeout)
2. Uploads the torrent to AllDebrid and waits for confirmation

Both steps combined easily exceed 8 seconds, especially on slower trackers or
under AllDebrid load.

**Fix:**
- `jackettAdd()` now passes `60_000 ms` (60 s) as the fourth argument to `api()`.
- `loadJackettIndexers()` now passes `15_000 ms` (15 s) — the backend Jackett
  indexer fetch has a 10 s timeout, so 8 s was always too tight.

## [1.5.19] — 2026-05-05

### Fixed — Statistics 24h (and 1y) broken on PostgreSQL

**Root cause:** `_sql_strftime("%H:00", field)` produced `TO_CHAR(field, 'HH24:00')`.
In PostgreSQL `TO_CHAR` for timestamps, `'0'` is a **numeric format code** (digit with
leading zero), not a literal. So `':00'` was interpreted as colon + two numeric
placeholders applied to a timestamp field → PostgreSQL error.

The same issue affects `%Y-%m` → `'YYYY-MM'` where `'-'` fortunately has no
special meaning in PG timestamp format, but `%H:00` → `'HH24:00'` is broken.

**Fix:** `_sql_strftime` now wraps any characters that are not recognised
PostgreSQL format codes (not in `YYYY MM DD HH24 MI SS`) in double-quotes,
which are the PostgreSQL way to denote literal text inside a `TO_CHAR` format
string:

| Format | Old PG output | New PG output |
|--------|---------------|---------------|
| `%H:%M` | `TO_CHAR(…, 'HH24:MI')` | `TO_CHAR(…, 'HH24":"MI')` |
| `%H:00` | `TO_CHAR(…, 'HH24:00')` ❌ | `TO_CHAR(…, 'HH24":00"')` ✓ |
| `%Y-%m` | `TO_CHAR(…, 'YYYY-MM')` | `TO_CHAR(…, 'YYYY"-"MM')` |

All literal separators are now safely quoted; `HH24":"MI` produces `14:30`,
`HH24":00"` produces `14:00`, `YYYY"-"MM` produces `2026-05`.

## [1.5.18] — 2026-05-05

### Fixed — Statistics 24h period: internal error

**Root cause:** The 24h chart query used two different expressions for `SELECT`
and `GROUP BY`:

```sql
-- SQLite (was)
SELECT strftime('%m-%d %H:00', completed_at) as date  -- full label
...GROUP BY strftime('%H', completed_at)              -- hour only
```

PostgreSQL requires every `SELECT`-ed column that is not an aggregate to appear
identically in `GROUP BY`. Using `strftime('%m-%d %H:00', …)` in `SELECT` but
`strftime('%H', …)` in `GROUP BY` caused a PostgreSQL error. SQLite allowed it
silently, but it also caused incorrect grouping (two entries at the same hour on
different days would be merged into one bar).

**Fix:** Both `SELECT` and `GROUP BY` now use the same expression — hour label
only (`%H:00` → `"14:00"`), which is unambiguous within a 24-hour window and
valid on both SQLite and PostgreSQL:

```sql
-- SQLite (fixed)
SELECT strftime('%H:00', completed_at) as date
...GROUP BY strftime('%H:00', completed_at)

-- PostgreSQL (fixed)
SELECT TO_CHAR(completed_at, 'HH24:00') as date
...GROUP BY TO_CHAR(completed_at, 'HH24:00')
```

## [1.5.17] — 2026-05-05

### Fixed — Statistics periods work on PostgreSQL + existing installations

**Problem:** All stat-period queries (`1h`, `24h`, `7d`, `30d`, `1y`) used
SQLite-specific SQL functions that fail silently or throw errors on PostgreSQL:

| SQLite | PostgreSQL equivalent |
|--------|-----------------------|
| `datetime('now','-1 hour')` | `NOW() - INTERVAL '1 hour'` |
| `strftime('%H:%M', field)` | `TO_CHAR(field, 'HH24:MI')` |
| `strftime('%Y-%m', field)` | `TO_CHAR(field, 'YYYY-MM')` |

**Fix:** Three SQL dialect helper functions added to `routes.py`:

- `_sql_now_minus(interval)` — returns the correct "now minus interval" expression
  for the active DB engine (SQLite `datetime('now',…)` or PG `NOW() - INTERVAL …`)
- `_sql_strftime(fmt, field)` — converts strftime-style format to the correct
  `strftime(…)` (SQLite) or `TO_CHAR(…)` (PG) expression
- `_sql_date(field)` — `DATE(field)` works identically on both engines

All usages in `GET /stats/detail` (period_map, chart queries, file_status)
and the two hardcoded queries in `GET /stats` (last_24h, last_7d) are now
dialect-agnostic.

**Existing installations:** `updated_at` on `download_files` is guaranteed to
exist — it is part of `_SCHEMA_COLUMNS_FILES` which is applied via
`_ensure_column` / `_ensure_column_pg` (SQLite/PG `ALTER TABLE ADD COLUMN IF
NOT EXISTS`) on every app start. No manual migration needed.

## [1.5.16] — 2026-05-05

### Fixed — Statistics internal error for all periods except "All time"

**Root cause:** The `GET /stats/detail` endpoint used a shared `where_ts` clause
(`WHERE created_at >= datetime(...)`) for all tables. The `download_files` table
has no `created_at` column — only `updated_at`. SQLite raised
`"no such column: created_at"` which became a 500 Internal Server Error for
every period except `all` (where `where_ts` is an empty string and no column
is referenced).

**Fix:** A separate `where_files` clause using `updated_at` is now built for
`download_files` queries. All other tables (`torrents`, `events`) have `created_at`
and continue to use `where_ts` unchanged.

## [1.5.15] — 2026-05-05

### Fixed — Statistics period selector had no effect

**Root cause: duplicate function declaration**

There were two definitions of `loadDetailedStats` in the JavaScript:

1. `async function loadDetailedStats()` (L2603, old — no parameter, called `/stats/detail` without `?period=`, called it twice)
2. `async function loadDetailedStats(period)` (L2107, new — correct, sends `?period=` to backend)

In JavaScript, function declarations are hoisted and the **last** one wins.
The old definition (L2603) silently overwrote the new one, so:

- Every period tab always showed the same data (backend default: `all`)
- The chart title never updated when switching periods
- The API was called twice per load (once for stats, once for the chart)
- Sources breakdown was never rendered

**Fix:** Removed the stale duplicate. The surviving `async function loadDetailedStats(period)`:
- Reads the active period tab if called without argument (for initial load via `nav()`)
- Sends `GET /stats/detail?period=<period>` — a single call for all data
- Updates the chart title, metric cards, status breakdowns, sources, and chart in one pass

## [1.5.14] — 2026-05-05

### Fixed — Settings Services tab raw HTML persists on mobile (root fix)

v1.5.13 removed nested template literals but the bug persisted. The real root
cause was the **50 KB `innerHTML` template literal itself**: some mobile JS
engines (Android WebView, Samsung Internet) silently truncate very long template
literal string evaluations, leaving the remainder as raw text nodes in the DOM.

**Fix:** `renderSettings()` no longer assigns one giant template string.
Instead it clears the container and appends each of the five tab panels
individually via `insertAdjacentHTML('beforeend', ...)`:

```
form.innerHTML = '';
form.insertAdjacentHTML('beforeend', `[tab-general  — 4 712 chars]`);
form.insertAdjacentHTML('beforeend', `[tab-download — 14 307 chars]`);
form.insertAdjacentHTML('beforeend', `[tab-notifications — 4 398 chars]`);
form.insertAdjacentHTML('beforeend', `[tab-services — 11 318 chars]`);
form.insertAdjacentHTML('beforeend', `[tab-advanced — 15 531 chars]`);
```

Each individual string is well under the ~15 KB limit where mobile truncation
is observed. All five panels verified: 0 nested backticks, balanced divs.

## [1.5.13] — 2026-05-05

### Fixed — Settings Services tab content visible as raw HTML text on mobile

**Symptom:** On some mobile browsers the Settings page showed `id="tab-services">`
as literal text and the Sonarr/Radarr/Jackett content appeared inside the Advanced
tab instead of the Services tab.

**Root cause:** The settings `innerHTML` template literal contained three *nested*
template literals (backtick strings inside `${...}` expressions). While V8/SpiderMonkey
desktop handle these correctly, some mobile browser JS engines misparse nested
backticks within a template literal — they interpret the inner closing backtick as
the end of the *outer* template, which truncates the HTML at that point. Everything
after the truncation point is emitted as a raw text node, including the opening
`<div class="stab-panel" id="tab-services">` tag.

**Affected expressions (all in the Download tab):**
1. `aria2_url` value — `` `http://127.0.0.1:${port}/jsonrpc` ``
2. `aria2_file_allocation` options — `['none',...].map(v => \`<option ...>\`)`
3. `flexgetAvailableTasks` options — `tasks.map(task => \`<option ...>\`)`

**Fix:** All three nested template literals replaced with equivalent string
concatenation expressions. The outer `innerHTML` template now contains zero
nested backticks — compatible with all JS engines.

## [1.5.13] — 2026-05-05

### Fixed — Settings Services tab shows raw HTML attribute as text

The text `id="tab-services">` was visible on screen above the Sonarr card.

**Root cause:** When the Notifications panel was rebuilt in v1.5.11, the
opening `<div class="stab-panel"` of the `tab-services` panel was accidentally
stripped — only `id="tab-services">` remained as bare text in the template.
Additionally, the matching closing `</div>` of the `tab-services` stab-panel
wrapper was also missing, leaving the template with one unbalanced `<div>`.

**Fix:** Restored both the opening `<div class="stab-panel" id="tab-services">`
and its closing `</div>`. Template verified: 291/291 balanced divs, all 5
panels present.

## [1.5.12] — 2026-05-05

### Fixed — Jackett search times out immediately in the UI

**Root cause:** `api()` was given an `AbortController` with an 8-second timeout
in v1.5.9. The Jackett search request is sent to `/jackett/search` which then
waits up to 120 s for Jackett to respond. After 8 s the browser aborted the
fetch — Jackett was still running (visible in its own log as a successful
"Manual search") but the UI showed "Request timed out".

**Fix:**
- `api()` now accepts an optional fourth argument `timeoutMs` (default 8 s).
- `jackettSearch()` passes `150_000` ms (150 s) so long Jackett queries have
  room to complete even with 600+ indexers.
- Backend: `asyncio.TimeoutError` is now caught alongside `ServerTimeoutError`
  in both the Torznab and JSON search paths — previously it fell through to the
  generic `except Exception` handler and returned empty results silently.

### Updated — Help sidebar

| Section | Change |
|---------|--------|
| Integrations → Jackett | Settings path corrected to `🔌 Services → Jackett`; multi-indexer chip picker described; advanced search (genre/IMDb/season) explained; search-speed note added |
| Troubleshooting | Two new FAQ entries: *Jackett search returns no results or times out* and *Hamburger / sidebar does not open on mobile* |
| Quick Start | Jackett settings path corrected (`🔌 Services → Jackett`) |

## [1.5.11] — 2026-05-04

### Fixed — Notifications tab blank (structural and CSS bugs)

Three issues were causing the Notifications tab to render as an empty panel:

1. **Wrong CSS class on update-notification toggle**
   The "Notify on new version" toggle used `class="tswitch"` which has no CSS
   definition. All other toggles use `class="toggle"` (width/height/position
   defined). The undefined class caused the toggle-row to render with collapsed
   height, which (due to `overflow: hidden` on `.scard`) clipped the entire
   scard content.

2. **`form-hint` paragraph outside `scard-body`**
   The hint paragraph was placed between `scard-header` and `scard-body`,
   outside the flex container. This broke the flex layout of the scard.

3. **Residual `</div>` nesting drift**
   Previous div-balance fixes had left one misplaced `</div>` causing the
   browser's HTML error recovery to render the Notifications panel content
   in an unexpected position.

**Fix:** Notifications panel rebuilt from scratch with correct structure:
`scard > scard-header > scard-body > (all fields including hint, toggles,
inputs)`. All toggles use `class="toggle"`. Template verified:
290/290 balanced divs.

## [1.5.10] — 2026-05-04

### Fixed — Settings Notifications tab blank on mobile

**Root cause:** When the "Notify on new version" toggle and interval input were
inserted into the Notifications panel (v1.5.8), one extra `</div>` was added
at the end of the panel. This closed the parent `stab-panel` div one level too
early:

- The Notifications `<div class="stab-panel">` was never properly closed
  (its `</div>` had migrated one panel too far).
- The Advanced panel accumulated an extra `</div>`, causing it to close one
  element it shouldn't have.

The browser's HTML error recovery meant the Notifications tab rendered as an
empty gray area — all form fields existed in the DOM but were outside the
visible panel boundary.

**Fix:** Restored the correct `</div>` nesting so all five panels
(General / Download / Notifications / Services / Advanced) are exactly balanced.
All 5 panels verified: `opens == closes` per panel, `285/285` total.

## [1.5.9] — 2026-05-04

### Fixed — Hamburger menu unresponsive on mobile

Two bugs prevented the sidebar from opening on mobile:

1. **Sidebar blocks clicks when hidden (pointer-events)**
   The sidebar uses `position: fixed; transform: translateX(-100%)` on mobile.
   Even though it was visually off-screen, it still occupied the left edge of the
   viewport and intercepted all touch events there — including the hamburger button.
   **Fix:** Added `pointer-events: none` to the hidden sidebar state;
   `pointer-events: auto` on `#sidebar.open`.

2. **Topbar had no z-index**
   The sidebar has `z-index: 200` (mobile). The topbar had no `z-index` set,
   so the sidebar layer sat above the topbar even when closed.
   **Fix:** `#topbar` now has `position: relative; z-index: 210`.

### Fixed — "Waiting for stats" stuck on slow start

Two issues caused the dashboard to stay on the initial placeholder text:

3. **`api()` had no fetch timeout**
   On a slow mobile connection or slow server start, `fetch()` could hang for
   minutes with no timeout, making the app appear frozen.
   **Fix:** `api()` now uses `AbortController` with an **8-second timeout**.
   Timed-out requests throw `Error('Request timed out')` for clean retry handling.

4. **`loadStats()` exited after first failure instead of retrying**
   The internal `for` loop (5 attempts) contained `return false` in the `catch`
   block, which exited the entire function after the very first error — making the
   outer retry loop in the IIFE pointless.
   **Fix:** The loop now `continue`s with exponential back-off (`500ms × attempt`)
   for the first 4 failures, and only returns `false` on the 5th.

## [1.5.8] — 2026-05-04

### Fixed — Changelog always shows current version

**Root cause:** `CHANGELOG.md` is copied into the Docker image at build time.
When running a stale image the local file was missing the newer version entries.

**Fix:** `GET /changelog` now checks whether the local file contains the running
version's entry (`[1.5.x]` marker). If not, it fetches all release bodies from
the GitHub Releases API and returns them merged — cached for 1 hour to avoid
hitting rate limits. Users always see the up-to-date changelog regardless of
image age.

### Added — Automatic update check with Discord notification

| Component | Details |
|-----------|---------|
| `GET /version/check` | Compares running version against latest GitHub release. Returns `{current, latest, update_available, release_url, release_notes}`. Cached for 30 minutes. |
| `update_check_loop()` | Background scheduler task. Polls GitHub every N hours (configurable). Sends a Discord embed when a newer version is found. Skips duplicate notifications for the same version. |
| `Notifier.send_update()` | Rich Discord embed: new version number, current version, link to GitHub release, first 900 chars of release notes. |
| Settings → 🔔 Notifications | **"Notify on new version"** toggle (`discord_notify_update`, default on). **"Version check interval"** number input (hours, 0 = disabled, default 12). |
| Header update badge | Orange pill badge appears in the topbar when a newer version is available. Click opens the Changelog view. Checked automatically on each stats refresh. |

**Config fields added:**
- `discord_notify_update: bool = True`
- `update_check_interval_hours: int = 12`

## [1.5.7] — 2026-05-04

### Maintenance — Dependency updates (Dependabot PRs #12–18)

**GitHub Actions:**

| Action | From | To |
|--------|------|----|
| `docker/setup-qemu-action` | v3 | v4 |
| `actions/cache` | v4 | v5 |
| `docker/build-push-action` | v5 | v7 |
| `docker/metadata-action` | v5 | v6 |
| `github/codeql-action` | v3 | v4 |

**Python dependencies:**

| Package | From | To |
|---------|------|-----|
| `uvicorn[standard]` | 0.35.0 | 0.46.0 |
| `asyncpg` | ≥0.29.0 | ≥0.31.0 |

All changes verified locally — 188/188 tests passing. No breaking changes
affect this project. Notable removals in updated packages that were confirmed
not in use: `DOCKER_BUILD_NO_SUMMARY` env (build-push-action v7),
`Config.setup_event_loop()` (uvicorn 0.36.1), Python 3.9 support (uvicorn 0.40.0).

## [1.5.6] — 2026-05-02

### Fixed — Jackett search silently returning 0 results

**Root cause:** Jackett with 618 indexers took **33.18 seconds** to complete a search.
The backend had a 30-second timeout — the request was aborted and the broad
`except Exception` handler returned `{results: [], total: 0}` without showing any error.
The Jackett log showed `Found 331 releases [33180ms]` but the UI showed *"No results"*.

**Fixes:**
- Search timeout raised from **30 s → 120 s** (both JSON and Torznab paths)
- `aiohttp.ServerTimeoutError` now caught explicitly and shown to the user:
  *"Jackett search timed out — try fewer indexers or a more specific query"*
- Debug logging added when `Results` key is empty to diagnose future issues
- Frontend shows a *"still searching…"* hint after 8 s so the user knows
  Jackett is still running (large indexer sets can take up to 60 s)

## [1.5.5] — 2026-04-29

### Fixed — Search UI: flat consistent filter bar

The Search view was redesigned from two mismatched rows (main bar + separate Advanced panel)
into a single unified filter layout.

**Row 1:** Query input (full-width) + Search button

**Row 2:** Category · Indexers · Mode · Genre/Tag · IMDb ID · Year · Season · Episode · Availability · Reset

- All controls share the same height (34px) and font size (12px)
- Genre/Tag, IMDb, Year, Season, Episode are hidden when Mode=General and shown contextually
- No separate collapsible 'Advanced' panel — filters are always visible and accessible
- Reset button clears every filter including query, category, indexer chips, and mode
- idx-trigger height unified with other inputs

## [1.5.4] — 2026-04-29

### Fixed — Jackett extended search: correct Torznab endpoint

**Bug:** `genre`, `imdbid`, `year`, `season`, `ep` were sent to the JSON Results API
(`/api/v2.0/indexers/all/results`) which silently ignores Torznab-specific parameters.
**Fix:** When any extended parameter is set, the Torznab XML API is used instead
(`/api/v2.0/indexers/<filter>/results/torznab/api?t=<mode>&...`).
New `_parse_torznab_results()` parses the RSS XML response into the same normalised format.

**Bug:** Advanced search panel was hidden by default (display:none).
**Fix:** Panel is now open by default — genre and search-type fields are immediately visible.

### Security — Path traversal in Torznab tracker-filter URL

Tracker IDs from the frontend were joined directly into the endpoint URL:
`/api/v2.0/indexers/{tracker_id}/results/torznab/api`
An attacker could send `tracker='../../../etc'` to traverse paths on the Jackett server.
**Fix:** Whitelist regex `^[A-Za-z0-9][A-Za-z0-9_.-]*$` validates all tracker IDs
before they are joined into the URL segment. Invalid IDs are silently dropped;
if none remain the `all` aggregator is used.

### Security audit (full scan, all confirmed safe)

| Finding | Verdict |
|---------|---------|
| SQL `{table}` f-strings | ✅ Source is hardcoded `MIGRATION_TABLES` list |
| SQL `{where_*}` f-strings | ✅ Source is hardcoded `period_map` dict |
| SQL `{h}` in stats.py | ✅ `int(hours)` + `Query(ge=1, le=8760)` |
| URL params via aiohttp `params=dict` | ✅ Automatically URL-encoded |
| Genre chips DOM insertion | ✅ `textContent` (not `innerHTML`) |
| Jackett result titles | ✅ All output through `esc()` |
| `search_type` parameter | ✅ Whitelist-validated before use |
| `category` parameter | ✅ Cast to `int` before use |
| `limit` parameter | ✅ `min/max` clamped (1–500) |

## [1.5.3] — 2026-04-29

### Added — Extended Jackett tag/genre search

The Jackett Search view now has an **Advanced** toggle (collapsed by default) that exposes
the full Torznab extended-parameter set supported by Jackett's API:

| Field | Torznab param | Supported modes |
|-------|--------------|-----------------|
| Search type | `t=` | General / TV Series / Movie / Music / Book |
| Genre / Tag | `&genre=` | tvsearch · movie · music · book |
| IMDb ID | `&imdbid=` | movie · tvsearch |
| Year | `&year=` | movie · tvsearch |
| Season | `&season=` | tvsearch |
| Episode | `&ep=` | tvsearch |

**Genre chips:** type a genre and press Enter (or use commas) to add chips.
Multiple genres are sent as a comma-separated list (`genre=comedy,drama`).
Chips can be removed individually. A `datalist` provides 20+ common suggestions
for faster input.

**Search type** controls the Torznab `t=` mode. Fields that are not applicable to
the selected mode are hidden automatically (e.g. Season/Episode only shown for TV).

**Backend (`services/jackett.py`):**
- `_build_result_params()` extended with `genre`, `imdbid`, `year`, `season`, `ep`
- `search()` signature extended with same parameters
- IMDb normalisation: strips leading `t`s, adds `tt` prefix for bare numeric IDs

**Backend (`api/routes.py`):**
- `POST /jackett/search` reads `search_type`, `genre`, `imdbid`, `year`, `season`, `ep`
- `search_type` validated against allowed set; defaults to `search`

**Result count line** summarises all active filters:
`17 result(s) for "breaking bad" · mode: tvsearch · genre: drama · 2008`

## [1.5.2] — 2026-04-29

### Fixed — Settings icons showing as raw HTML entities
- **Root cause:** Tab label strings stored as HTML entities (`'&#9889; General'`) were passed
  through `esc()` which escaped `&` → `&amp;`, producing literal `&#9889;` in the DOM.
- **Fix:** All tab labels now use direct Unicode characters (⚡ ⬇️ 🔔 🔌 🛠️). `esc()` removed
  from tab label rendering — labels are hardcoded, not user input.

### Fixed — Statistics period filter had no effect
- **Root cause (backend):** `torrent_total` and `torrent_size_total` always queried all-time
  regardless of the selected period. `daily_completions` used a hardcoded 14-day window.
- **Root cause (frontend):** Chart always fetched `period=all` as a second API call, ignoring
  the user's selection.
- **Fix (backend):** All totals now respect `period`. `daily_completions` uses period-aware
  grouping: 1h → minutes, 24h → hours, 7d/30d → days, 1y → months, all → last 90 days.
- **Fix (frontend):** Chart uses data from the same period-filtered API call.
  Chart title updates dynamically: *"Completions — last hour"*, *"…last 7 days"*, etc.

### Replaced — Indexer multi-select with custom chip picker
- **Problem:** Native `<select multiple>` with Ctrl+click is non-functional on mobile.
- **Fix:** Custom dropdown with checkbox list and chip UI:
  - Tap any indexer to select it; chips appear in the trigger button with ✕ to remove
  - **All Indexers** toggle at the top (default)
  - Closes on outside click
  - Fully dark/light mode via CSS variables
  - Hidden `<select>` kept for backend compatibility

## [1.5.1] — 2026-04-29

### Settings — restructured from 11 tabs to 5

| Old (11 tabs) | New (5 tabs) |
|---|---|
| General, Download, Discord, Database, Filters, Polling, Integrations, Backup, FlexGet, Reporting, Jackett | **⚡ General**, **⬇️ Download**, **🔔 Notifications**, **🔌 Services**, **🛠️ Advanced** |

- **⚡ General** — AllDebrid API key, Folders, Stuck-download timeout, Rate limit & sync
- **⬇️ Download** — aria2 client setup, all aria2 performance options (unchanged)
- **🔔 Notifications** — Discord webhooks (moved from Discord tab)
- **🔌 Services** — Sonarr, Radarr, Jackett, FlexGet, Labels (consolidated)
- **🛠️ Advanced** — Filters, Polling, Backup, Reporting, Database (deprioritised but fully accessible)

All 88 settings fields retained. No breaking changes. Brief description added to each settings group.

### aria2 built-in set as default

`aria2_mode` default changed from `external` to `builtin` in `config.py` and both
`getFormSettings()` and `renderSettings()`. New installations default to the built-in
aria2 — no extra setup required.

### Header — live aria2 download speed

A small speed badge appears in the top bar (next to the page title) showing the current
aria2 download speed in real time. Only visible when aria2 is in built-in mode and running.
Updates every 5 s via `GET /aria2/runtime` (which now includes `download_speed` from
`aria2.getGlobalStat`). New `fmtSpeed()` helper formats bytes/s → KB/s / MB/s / GB/s.

### Dark Mode — hamburger icon contrast

`border` → `border2`, `background: surface` → `surface2`, explicit `color: var(--text)` added
to `.mobile-menu-btn` so the ☰ symbol is clearly visible on dark backgrounds.

### Statistics

- **Completed Size** and **Partial Torrents** cards now populate correctly.
  Root cause: `GET /stats/detail` was missing `completed_size` and `partial_total` in its
  response — both SQL queries added.
- **"Latest Signals" removed** and replaced with "Top Sources".
- **Period selector added**: 1h / 24h / 7d / 30d / 1y / All time. Selecting a period
  re-fetches `/stats/detail?period=<value>` and re-renders all cards.
- `loadDetailedStats()` and `setStatsPeriod()` are new; the old monolithic function is replaced.

### Search — multi-indexer selection

The Jackett indexer `<select>` is now `multiple` (size=4). Users can Ctrl+click to select
several indexers simultaneously. `jackettSelectedTrackers()` reads `selectedOptions` instead
of `.value`. Backend already supported `trackers: []` — no backend change needed.

### Torrents — pagination

- Default page size: 25 torrents per page (options: 15 / 25 / 50 / 100).
- Page navigation rendered by `renderTorrentPagination()`.
- `loadTorrents()` sends `limit` + `offset` query params. Backend `GET /torrents` already
  supported both — no backend change needed.
- `setFilter()` and `onTorrentSearchInput()` reset `torrentPage` to 1 on change.

### Backend changes

- `GET /stats/detail` — added `period` query param (1h/24h/7d/30d/1y/all), `completed_size`,
  `partial_total`, `completed_count` to the `totals` object; removed `latest_events`.
- `GET /aria2/runtime` — added `download_speed`, `upload_speed`, `active` from
  `aria2.getGlobalStat()`.
- `Aria2Service.get_global_stat()` — new method wrapping `aria2.getGlobalStat` RPC call.

## [1.5.0] — 2026-04-29

### Added — Help & Documentation sidebar view

New **❓ Help** entry in the sidebar with seven comprehensive tabs:

| Tab | Contents |
|-----|----------|
| 🚀 Quick Start | 5-step setup guide, Docker Compose reference |
| 📖 How It Works | Full pipeline explanation (Upload → Poll → Unlock → Download → Notify), status table |
| ⬇️ aria2 | Built-in vs External comparison, key settings explained, memory optimisations applied automatically |
| 🧠 RAM & Memory | Three RAM sources explained (process heap / kernel page cache / glibc arenas), how to use Memory Info and Drop Page Cache |
| 🔌 Integrations | Sonarr/Radarr, Discord, Jackett, Watch Folder, FlexGet — setup instructions for each |
| ⚙️ Settings Reference | Every settings tab documented with field-level explanations |
| 🔧 Troubleshooting | Eight expandable FAQ items covering stuck torrents, 503 errors, Sonarr import failures, high RAM, permission issues, Remux, SQLite errors, and external aria2 |

## [1.5.1] — 2026-04-29

### Fixed
- **Settings page blank (white/grey screen)** — v1.5.0 introduced a double
  definition of `switchSettingsTab()` (old + new) causing a JavaScript syntax
  error at runtime. The duplicate was removed and the function updated to use
  the new tab IDs (`tab-advanced`, `tab-services`).
- **`aria2_mode` default was `external`** in both `getFormSettings()` and
  `config.py` — the built-in aria2 was never selected by default despite being
  the recommended mode. Both now default to `builtin`.

### Added
- **Help sidebar view** (`❓ Help`) — accessible from the sidebar, contains
  six sections: Quick Start, How it works, aria2, RAM & Memory, Integrations,
  Troubleshoot. Answers the most common questions without leaving the app.
- **Built-in aria2 is now the default** — `aria2_mode` default changed from
  `external` to `builtin` in config and in the settings form.

## [1.5.0] — 2026-04-29

### Changed — Settings overhaul

The Settings UI has been fully redesigned: 11 tabs collapsed into 5 cleaner tabs
with every field annotated with a help text.

| Old (11 tabs) | New (5 tabs) |
|---|---|
| General, Download, Discord, Database, Filters, Polling, Integrations, Backup, FlexGet, Reporting, Jackett | **General**, **Download**, **Notifications**, **Services**, **Advanced** |

**General** — AllDebrid API key, folders, concurrent downloads, stuck-download timeout, sync interval.

**Download** — aria2 client selection (built-in vs external), RPC connection, performance
(split, connections, segment size, speed limit), storage (disk cache + explanation of 0 vs 16M for
network mounts, file allocation with per-option explanation), error retries, memory diagnostics.

**Notifications** — Discord webhooks with separate fields for added/complete/error events and
per-channel webhook URLs.

**Services** — Sonarr, Radarr, Jackett, FlexGet, Labels — all external integrations in one place
with Test Connection buttons and inline help text for each field.

**Advanced** — File filters, statistics/reporting, backups, database (SQLite/PostgreSQL),
migration, danger zone (wipe), and polling intervals — kept available but visually deprioritised.

#### Help texts added to every field
All 81 rendered settings fields now have a `form-hint` explaining what the setting does,
what the recommended value is, and when to change it.

#### Disk-cache help text updated for platform independence
The disk-cache field now explicitly explains:
- `0` = recommended for fast/local storage (~4 MB RAM per aria2 docs)
- `16M` = recommended for network mounts (NFS, SMB) or FUSE-based filesystems
  (mergerfs, overlayfs) on **any OS**, not just Unraid — fewer round-trips = lower peak RAM

#### page-cache drop note updated
The Memory Info / Drop Page Cache buttons and their description now note that
`posix_fadvise(DONTNEED)` works on any Linux system; it is a no-op on Windows/macOS.

## [1.5.0] — 2026-04-29

### Added — Help sidebar view

New **❓ Help** entry in the sidebar with seven documentation tabs:

| Tab | Contents |
|-----|----------|
| 🚀 Quick Start | 5-step setup guide, Docker Compose reference |
| 📖 How It Works | Full pipeline (Upload → Poll → Unlock → Download → Notify) + status table |
| ⬇️ aria2 | Built-in vs External, key settings, automatic memory optimisations |
| 🧠 RAM & Memory | Page cache vs process RAM, Memory Info / Drop Page Cache explained |
| 🔌 Integrations | Sonarr/Radarr, Discord, Jackett, Watch Folder, FlexGet setup |
| ⚙️ Settings Reference | Every settings field documented |
| 🔧 Troubleshooting | 8 expandable FAQ entries (stuck torrents, 503, Sonarr import, RAM, permissions…) |

## [1.4.9] — 2026-04-29

### Root cause confirmed: Linux kernel page cache on Unraid/mergerfs

20 GB RAM usage does not come from aria2 itself (which uses ~10–50 MB with
`disk-cache=0`). The actual source is the **Linux kernel page cache**.

When aria2 writes a downloaded file to disk, the kernel caches every byte in
RAM. The cache is only released when another process needs memory — on a
dedicated server with plenty of RAM this never happens, so the cache keeps
growing with every download. Unraid's dashboard reports this cache as "used"
RAM, making it look like a memory leak.

On Unraid the path is: aria2 → write() → kernel page cache → mergerfs (FUSE)
→ array disk. Mergerfs does not flush the page cache any faster than a native
filesystem.

### Added

**`GET /api/admin/memory-info`** — returns a breakdown of system RAM:
- `really_used`: actual process RAM (RSS of all processes)
- `page_cache`: kernel file cache (shown as "used" in Unraid dashboard but
  reclaimed automatically when needed)
- `available`: RAM immediately usable by new processes

**`POST /api/admin/drop-page-cache`** — calls
`posix_fadvise(POSIX_FADV_DONTNEED)` on every completed download file,
telling the kernel to release the cached pages immediately. Safe to call
at any time; the file on disk is not affected.

**`services/page_cache.py`** — `drop_page_cache_for_file()` called
automatically from `_finalize_aria2_torrent()` after every completed torrent.
This keeps the page cache from accumulating during long download sessions.

**UI buttons** in Settings → Download → aria2:
- **Memory Info** — shows real RAM vs page cache breakdown
- **Drop Page Cache** — releases cached pages and refreshes the display

### Why previous fixes had no visible effect
All previous changes (disk-cache, split, MALLOC_ARENA_MAX, file-allocation,
session clearing) correctly reduced aria2's *process heap*. But the process
heap was never the dominant factor — the page cache was. 20 GB of page cache
is expected when downloading 20 GB of files on a system that never frees it.

## [1.4.8] — 2026-04-29

### Analysis: why RAM keeps growing

The sustained RAM growth has three separate sources that require different
treatment:

**1. aria2 process heap (glibc malloc arena retention)**
Even with `MALLOC_ARENA_MAX=1` (set in v1.4.6), glibc never fully returns freed
pages to the OS after a busy download period. The only complete fix is a process
restart. `MALLOC_ARENA_MAX=1` slows the growth but does not stop it.

**2. Kernel page cache**
Every byte written to disk passes through the Linux kernel page cache first.
During active downloads the cache fills with download data and is released
by the kernel only when other processes need RAM. This appears as high "used"
memory in `top`/`htop` but is **not** a real memory leak — it is reclaimed
automatically and does not cause OOM situations.

**3. Filesystem interaction (mergerfs / FUSE)**
If `/download` is mounted via mergerfs (the default Unraid share layout),
every write from aria2 goes through FUSE in userspace. With `disk-cache=0`
aria2 writes each small HTTP chunk immediately to FUSE, causing many
round-trips and keeping more buffers live simultaneously. Counter-intuitively,
a small `disk-cache` (e.g. `16M`) **reduces** peak RSS on FUSE mounts because
aria2 coalesces writes and releases its recv-buffers sooner.

### Added

- **Periodic aria2 restart** (`aria2_restart_interval_hours`, default `0` =
  disabled) — when set, the built-in aria2 process is restarted after the
  configured number of hours, but only when no downloads are active. After
  restart, `_dispatch_pending_aria2_queue()` re-queues all pending files from
  the DB within one poll cycle (≤ 1 s). This is the only guaranteed way to
  fully reclaim glibc malloc arena memory. Recommended value: `4` to `8`.

### Changed

- **`disk-cache` comment updated** — clarifies that `0` is optimal for native
  filesystems (ext4, XFS) but a value like `16M` is better for FUSE-based
  mounts (mergerfs, NFS, SMB) where it reduces FUSE round-trips.

## [1.4.7] — 2026-04-29

### Fixed — built-in aria2 RAM usage (root causes, documentation-based)

After reading the official aria2 documentation carefully, three genuine root
causes were identified that explain the sustained high RAM usage:

#### Root cause 1: `file-allocation=falloc` blocks aria2 and causes indirect RAM pressure

The official aria2 documentation states:
> *"Don't use falloc with legacy file systems such as ext3 and FAT32 because
> it takes almost the same time as prealloc and it blocks aria2 entirely until
> allocation finishes."*

Even on modern file systems inside Docker (overlayfs, ext4) `falloc` calls
`posix_fallocate()` which holds the aria2 process **completely frozen** until
the kernel has allocated disk space. During this time:
- No RPC responses → our polling loop sees timeouts → retry storms
- New downloads queue up in memory waiting for aria2 to respond
- The `_wait_until_healthy()` loop burns CPU retrying every 250ms

**Fix:** `file-allocation=none` — no pre-allocation, downloads start instantly.
For AllDebrid CDN downloads (direct HTTP, no resume needed) pre-allocation
provides zero benefit.

#### Root cause 2: Session file loaded on restart → RAM spike

aria2 was started with `--input-file=session_file` on every restart. The
session file accumulates **all** downloads from the previous run — including
hundreds of completed/error entries that aria2 keeps until explicitly purged.
On restart, aria2 loads every entry as a `RequestGroup` object in its C++ heap,
causing an immediate RAM spike proportional to the session file size.

**Fix:** The session file is now **cleared** before aria2 starts. The database
is the single source of truth; `_dispatch_pending_aria2_queue()` re-queues all
`pending` files within one poll cycle (≤1 second). Session saving is kept so
aria2 can write state, but reading it back on startup is skipped.

#### Root cause 3: `--piece-length=1M` has no effect on HTTP downloads

`--piece-length` only affects BitTorrent downloads (the size of torrent pieces).
For plain HTTP/FTP downloads aria2 uses `--split` and `--min-split-size` to
control segmentation, not `--piece-length`. The flag was harmless but served
no purpose and was removed.

#### Additional hardening
- `--async-dns=false`: disables the c-ares async DNS resolver thread pool.
  Each resolver thread can trigger a new glibc malloc arena, compounding the
  arena-fragmentation issue fixed in v1.4.6. All AllDebrid CDN URLs resolve
  quickly via the system resolver; async DNS is unnecessary overhead.
- `--no-netrc=true`: disables `.netrc` file lookup on every download. We never
  use FTP credentials; the lookup is a small but avoidable startup cost.

#### Settings already correct (no change)
- `disk-cache=0`: per the aria2 homepage, this reduces RAM to **4 MiB** for
  HTTP/FTP downloads. Already set in v1.4.5.
- `MALLOC_ARENA_MAX=1` + `MALLOC_TRIM_THRESHOLD_=65536`: still applied to the
  aria2c subprocess environment (from v1.4.6).

## [1.4.6] — 2026-04-27

### Fixed — aria2 RAM usage (deep analysis)

After a thorough analysis of all RAM sources in the built-in aria2 process,
several compounding issues were identified and fixed:

#### Root cause 1: glibc malloc arena growth (most impactful)
aria2 uses glibc `malloc`. By default glibc creates up to 8× CPU-count
memory arenas for multi-threaded performance. Freed memory in one arena is
not visible to other arenas and is rarely returned to the OS — RSS grows
monotonically even when the heap is internally empty. After many download
cycles the process RSS can reach several hundred MB while actual live
allocations are minimal.

**Fix:** aria2c is now started with:
- `MALLOC_ARENA_MAX=1` — forces a single arena; `malloc_trim()` works
  globally and glibc can return unused pages to the OS.
- `MALLOC_TRIM_THRESHOLD_=65536` — triggers trim after 64 KB of free heap
  instead of the default 128 KB, releasing memory back to the OS faster.

#### Root cause 2: unbounded aria2 waiting queue
`_dispatch_pending_aria2_queue()` was sending **all** pending files to
aria2 at once. For a 200-file torrent this created 200 `RequestGroup`
objects in aria2's C++ heap (~5–15 KB each = 1–3 MB per large torrent).
With multiple large torrents in flight the waiting queue grew into tens of
MBs, and glibc arena fragmentation prevented reclaim.

**Fix:** dispatch is now capped at `max_concurrent_downloads × 4` files per
cycle. Remaining files stay as `pending` in the DB and are dispatched as
slots open. Default cap: 3 × 4 = 12 files maximum in aria2 at once.

#### Root cause 3: socket recv-buffer × connections
aria2 allocates a recv-buffer per active TCP connection. At high CDN speeds,
Linux TCP autotuning grows each buffer up to ~1 MB. With the previous
defaults (split=8, max-connection-per-server=8, 3 active downloads):
8 × 8 × 3 = 192 potential sockets × 1 MB = up to 192 MB in socket buffers.

**Fix:**
- `aria2_split`: 8 → **4** — halves the number of in-flight connections
- `aria2_max_connection_per_server`: 8 → **4**
- `aria2_disk_cache`: 16M → **8M** — further reduced write-back buffer

#### Root cause 4: orphaned stopped GIDs never removed
If `remove()` failed silently after marking a file `completed` in the DB,
the aria2 GID remained in the stopped list permanently (the next sync cycle
skips `status=completed` files). Over long sessions thousands of orphaned
entries could accumulate.

**Fix:** `run_aria2_housekeeping()` now iterates all `complete/removed/error`
GIDs from `get_all()` and explicitly calls `removeDownloadResult` on each,
in addition to the existing `purgeDownloadResult()` call.

## [1.4.5] — 2026-04-27

### Fixed / Changed
- **Built-in aria2 excessive RAM usage** — several compounding causes addressed:

  | Setting | Old | New | Effect |
  |---------|-----|-----|--------|
  | `disk-cache` | 64 M | **16 M** | aria2's write-buffer uses 4× less RAM; no measurable throughput difference for HTTP downloads (no BitTorrent piece assembly) |
  | `max-download-result` | 50 | **20** | fewer completed download records kept in aria2's in-memory result list |
  | `aria2_purge_interval_minutes` | 15 min | **5 min** | result list flushed more frequently via `aria2.purgeDownloadResult` |
  | `--piece-length` | (aria2 default ~1 MB) | **1M** (explicit) | prevents aria2 from choosing a larger piece size and allocating more piece-metadata RAM for large files |
  | `--max-download-result` | set only via RPC after start | now also **set at process start** | result list cap is enforced from the very first download |

  Additionally, `_finalize_aria2_torrent()` now calls `purge_download_results()`
  immediately after marking a torrent complete, instead of waiting for the next
  housekeeping interval.  For long-running sessions with many completed torrents
  this prevents the result list from growing between purge cycles.

  **Note on GitHub issue #902:** that issue describes a different pathological
  case (1.95 million files loaded at once). Our RAM growth comes from accumulated
  completed-download metadata and the write-buffer cache, not from input-file
  parsing.

## [1.4.4] — 2026-04-27

### Changed
- **Default PUID/PGID changed to 99:100** (nobody:users) — matches the
  Unraid default for all media containers (Sonarr, Radarr, Plex, etc.)
  so downloaded files are accessible out of the box without configuring
  environment variables. Override with `PUID` / `PGID` if needed.

## [1.4.3] — 2026-04-27

### Fixed
- **AllDebrid HTTP 503 on large torrents** — the parallel `unlock_link`
  optimisation introduced in v1.4.0/v1.4.1 fired all unlock calls
  simultaneously with no concurrency limit.  For torrents with 100+ files
  this produced a burst of hundreds of concurrent API requests, triggering
  AllDebrid rate-limiting (HTTP 503 Service Unavailable) and marking every
  file as failed.

  Fix: both `_download()` and `_dispatch_pending_aria2_queue()` now wrap
  their `unlock_link` coroutines with `asyncio.Semaphore(5)`, capping
  concurrent AllDebrid API calls at 5.  This keeps throughput high for
  small torrents (5 files unlock in parallel) while preventing burst
  overload on large ones (100 files unlock in groups of 5).

- **Default container user changed to `nobody:users`** (UID 65534 / GID 100)
  matching the Unraid default for media containers.  Override with
  `PUID` / `PGID` as needed.

## [1.4.2] — 2026-04-27

### Fixed
- **Downloaded files owned by root** — the container ran the entire app as
  `root`, so all files written to the download folder were owned by UID 0.
  Other containers (Sonarr, Radarr, Plex, Jellyfin, etc.) running as a
  regular user could not read, move, or import those files.

  **Fix:** PUID / PGID environment variables are now supported, identical to
  the LinuxServer.io convention:

  ```yaml
  environment:
    - PUID=1000   # UID of the user on the host / in other containers
    - PGID=1000   # GID of the group on the host / in other containers
  ```

  Run `id` on the host to find the correct values.

  Implementation details:
  - `entrypoint.sh` (new) — reads `PUID`/`PGID`, creates the user/group if
    they don't exist, `chown`s `/app/data`, `/app/config`, and `/download`
    to the requested UID:GID, then hands off to `gosu <user> uvicorn …`.
  - `Dockerfile` — installs `gosu` and `shadow` (for `useradd`/`groupadd`);
    creates a default `appuser` at UID/GID 1000; sets `ENTRYPOINT
    ["/entrypoint.sh"]`; `chown`s all app directories to `1000:1000` at
    build time so the image works correctly without any env vars.
  - Built-in aria2 inherits the same UID/GID because it is launched as a
    child process of uvicorn, which already runs as the target user.
  - `docker-compose.yml` — `PUID=1000` / `PGID=1000` added as documented
    defaults with an explanatory comment.
  - `README.md` — new permission note in both the configuration table and
    the `docker run` example.
  - Unraid template — `PUID` and `PGID` added as `Display="always"` fields
    so Unraid Community Apps prompts for them during install.

## [1.4.2] — 2026-04-27

### Added
- **PUID / PGID support** — downloaded files are now owned by a configurable
  UID/GID so that other containers (Sonarr, Radarr, Plex, Jellyfin, …) can read
  and write them without permission errors.

  **How it works:**
  - Two new environment variables: `PUID` (default `1000`) and `PGID` (default `1000`).
  - A new `/entrypoint.sh` runs as root on container start, creates/adjusts the
    matching user and group, `chown`s `/app/data`, `/app/config`, and `/download`,
    then hands off to the app via `gosu` running as that user.
  - The app and the built-in aria2 daemon both run as the configured user — all
    files created during a session (downloads, DB, session file, log) share the
    same ownership.

  **Dockerfile changes:**
  - Added `gosu` and `shadow` (for `useradd`/`usermod`) to the system packages.
  - Default `appuser` (UID 1000 / GID 1000) created at build time; runtime
    `PUID`/`PGID` override it without rebuilding the image.
  - `ENTRYPOINT ["/entrypoint.sh"]` replaces direct `CMD`.
  - `/app` and `/download` are `chown`'d to `1000:1000` at build time as the
    safe default.

  **docker-compose.yml:** `PUID` and `PGID` added to the environment block with
  a comment explaining how to find the right values (`id` on the host).

  **Unraid template:** `PUID` and `PGID` appear as explicit, always-visible
  config entries with descriptions linking them to the media-stack use-case.

  **README:** configuration table and `docker run` example both document
  `PUID`/`PGID` and explain the file-ownership rationale.

## [1.4.1] — 2026-04-27

### Fixed
- **`_dispatch_pending_aria2_queue()` called `unlock_link` sequentially** — the
  v1.4.0 parallelisation of `unlock_link` only covered the first call in
  `_download()` (which writes `source_link` to the DB as a placeholder). The
  actual dispatch loop in `_dispatch_pending_aria2_queue()` re-unlocked every
  link sequentially when handing jobs off to aria2. For a 10-file torrent this
  caused a ~3 s delay between the first and last file being sent to aria2.

  Fix: the dispatch loop now fires all `unlock_link` calls concurrently via
  `asyncio.gather()`, then iterates the results to call `aria2.ensure_download()`
  and update the DB. Files that fail unlock are marked `error` individually;
  the rest continue dispatching normally.

## [1.4.0] — 2026-04-27

### Summary: Jackett search, Downloads view, built-in aria2, and speed improvements

This release bundles all changes since v1.3.0 into a major release with one
additional batch of performance improvements.

---

### Added (since v1.3.0)

#### Jackett torrent search (v1.3.0)
- **Search view** in the sidebar — search any Jackett-indexed tracker directly
  from the UI. Query field, category filter (All/Movies/TV/Music/Books/Games/
  Software/XXX), live indexer dropdown, results table with Title/Indexer/Size/
  Seeds/Peers/Date, per-row Add button with inline status feedback.
- **Settings → Jackett tab** — enable toggle, URL, API key (password input),
  dedicated webhook URL, live Test Connection button.
- **Backend**: `services/jackett.py` — `search()`, `test_connection()`,
  `get_indexers()`, `send_jackett_webhook()`.
- **5 new API routes**: `POST /jackett/search`, `POST /jackett/add`,
  `GET /jackett/indexers`, `GET /jackett/categories`,
  `POST /settings/test-jackett`.
- **Webhook**: fires `jackett_torrent_added` embed; uses `jackett_webhook_url`
  if set, falls back to main Discord webhook.
- **Security**: API key proxied through backend — never sent to the browser.

#### Downloads view (v1.3.24)
- **New sidebar entry** (between Search and Monitor) — live aria2 queue with
  auto-refresh every second while active.
- Summary bar: active/waiting/stopped counts, total download speed, remaining bytes.
- Per-row table: status dot, filename, animated progress bar, total size, speed,
  status label, Pause/Resume/Remove buttons.
- **Quick speed limit** dropdown in the view header: Unlimited / 1 / 2 / 5 / 10 /
  20 / 50 MB/s + Custom (KB/s input). Sets `max-overall-download-limit` in aria2
  at runtime via `aria2.changeGlobalOption`. Current limit is read back from aria2
  on every view open and reflected in the preset.
- **New API routes**: `GET /aria2/global-options`, `POST /aria2/global-options`.

#### Built-in aria2 runtime (v1.3.20–v1.3.23)
- Optional embedded aria2 daemon — start, stop and restart from the Settings UI
  without leaving the app.
- Runtime status and diagnostics panel; live queue controls.
- Memory tuning: configurable `max-download-result` and
  `keep-unfinished-download-result`; periodic purge of stopped results.

---

### Changed (since v1.3.0)

#### Performance — v1.4.0
- **`unlock_link` calls parallelised** — previously, each file in a multi-file
  torrent was unlocked sequentially (200–600 ms per file × file count). Now all
  unlock calls for a single torrent fire concurrently via `asyncio.gather()`.
  A 10-file torrent drops from ~4 s to ~0.6 s for the unlock phase.
- **`aria2_poll_interval_seconds` default: 5 → 1** — the dispatch loop now runs
  every second instead of every 5 seconds. Downloads appear in aria2 within ~1 s
  of being queued rather than up to 5 s later.
- **aria2 RPC minimum interval: 50 ms → 20 ms** — the per-call rate limiter in
  `Aria2Service._call()` is tightened; dispatching a 10-file queue takes ~200 ms
  instead of ~500 ms.
- **Downloads view auto-refresh: 4 s → 1 s** — the aria2 queue view now polls
  every second for near-real-time progress feedback.

#### Stability fixes (v1.3.1–v1.3.24)
- Jackett Settings tab was not rendered (panel inserted outside `renderSettings()`
  template literal) — fixed in v1.3.1/v1.3.3.
- `send_jackett_webhook()` had a broken `_fmt_size` import — fixed in v1.3.1.
- Jackett search showed stale/dead torrents from disconnected trackers — added
  dead-torrent filter (0 seeders from single-tracker indexers) in v1.3.16.
- Jackett magnet-hash backfilling and torrent-file link hardening (v1.3.11–v1.3.12).
- aria2 download path and built-in daemon diagnostics improved (v1.3.20–v1.3.22).
- PostgreSQL `size_bytes` INT4 overflow migration — `BIGINT` upgrade at startup
  (v1.2.14, carried forward).
- Stuck-torrent straggler check — torrents with all files `completed` but status
  still `queued/downloading` are now auto-finalised on every sync cycle (v1.2.11).

---

### Tests
- 188 passing (up from 133 at v1.3.0).

## [1.3.24] — 2026-04-27

### Added
- **Downloads view** — new sidebar entry between Search and the Monitor group.
  Shows the live aria2 queue directly in the UI, similar to the ariang interface:
  - Auto-refreshes every 4 seconds while the view is active (stops when hidden)
  - Summary bar: active / waiting / stopped counts, total download speed, remaining data
  - Table per download: status indicator, filename, progress bar (animated), total size,
    current speed, status label, and per-row Pause / Resume / Remove buttons
  - Buttons use event delegation (data-gid / data-act attributes) — no inline `onclick`
    with GID interpolation
  - Active-download badge on the sidebar entry keeps count visible from any view

- **Quick speed-limit control** — in the Downloads view header, a preset dropdown
  (Unlimited / 1 / 2 / 5 / 10 / 20 / 50 MB/s) instantly applies
  `aria2.changeGlobalOption(max-overall-download-limit)` at runtime.
  Selecting **Custom…** reveals a KB/s input + Apply button for arbitrary values.
  The current limit is read from aria2 on every view open and reflected in the preset.

- **API routes** (`backend/api/routes.py`):
  - `GET  /aria2/global-options` — returns current `max-overall-download-limit` and
    `max-overall-upload-limit` plus all limit/speed keys from aria2's global options
  - `POST /aria2/global-options` — applies `max_download_speed` and/or
    `max_upload_speed` (bytes/s, 0 = unlimited) via `aria2.changeGlobalOption`

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
