<div align="center">
  <img src="docs/logo.svg" width="96" alt="AllDebrid-Client Logo"/>
  <h1>AllDebrid-Client</h1>
  <p><strong>Self-hosted torrent automation via AllDebrid</strong><br/>Web UI · built-in aria2 · Sonarr/Radarr (qBit API) · Jackett search · SSE live updates · Discord · Prometheus · PostgreSQL</p>

  [![Website](https://img.shields.io/badge/ad-client.mediastarr.de-ff6b2b?logo=googlechrome&logoColor=white)](https://ad-client.mediastarr.de/)
  [![Release](https://img.shields.io/github/v/release/kroeberd/alldebrid-client?style=flat-square&color=f97316)](https://github.com/kroeberd/alldebrid-client/releases)
  [![Docker Pulls](https://img.shields.io/docker/pulls/kroeberd/alldebrid-client?style=flat-square&color=3b82f6)](https://hub.docker.com/r/kroeberd/alldebrid-client)
  [![Discord](https://img.shields.io/badge/Discord-Join-5865f2?logo=discord&logoColor=white)](https://discord.gg/8Vb9cj4ksv)
  [![License](https://img.shields.io/github/license/kroeberd/alldebrid-client?style=flat-square)](LICENSE)
  [![Tests](https://img.shields.io/badge/tests-292%20passing-22c55e?style=flat-square)](https://github.com/kroeberd/alldebrid-client/actions/workflows/tests.yml)
  [![CI](https://img.shields.io/github/actions/workflow/status/kroeberd/alldebrid-client/tests.yml?style=flat-square&label=CI)](https://github.com/kroeberd/alldebrid-client/actions/workflows/tests.yml)
  [![Docker Build](https://github.com/kroeberd/alldebrid-client/actions/workflows/Docker_Build.yml/badge.svg)](https://github.com/kroeberd/alldebrid-client/actions/workflows/Docker_Build.yml)
</div>

---

## What it does

AllDebrid-Client automates the full torrent lifecycle via your AllDebrid account:

1. **Add** magnet links or `.torrent` files via web UI, Jackett search, watch folder, Sonarr/Radarr, or REST API
2. **Upload** to AllDebrid and poll until ready (bulk API, token-bucket rate limiter, automatic retry on failure)
3. **Unlock** download links and submit them to aria2 in FIFO order
4. **Monitor** aria2 until all files complete, then mark done and remove from AllDebrid
5. **Notify** via Discord, trigger Sonarr/Radarr import, run post-processing scripts

---

## Features

| Category | Details |
|----------|---------|
| **Sonarr / Radarr** | Native qBittorrent v4.3.2 API emulation at `/api/v2/` — configure as a standard qBit download client, no webhook setup needed |
| **Input sources** | Web UI paste, Jackett search (multi-indexer, bulk add), watch folder (`.torrent`/`.magnet`), Sonarr/Radarr, REST API |
| **Download client** | **Built-in aria2** (default, zero setup) or external aria2 instance via JSON-RPC |
| **Live updates** | Server-Sent Events (SSE) push status changes instantly — no polling delay |
| **Access control** | Optional HTTP Basic Auth (Settings → Access Control); health-check paths exempt |
| **Disk space guard** | Abort download before start if free space below threshold |
| **Post-processing** | Shell script run after each completed download (`{name}`, `{path}`, `{torrent_id}` placeholders, 300 s timeout) |
| **Auto-extraction** | `.zip`, `.rar`, `.7z`, `.tar.*` and more after download; configurable concurrency and Discord notification |
| **Error recovery** | Auto-retry Upload Failed (code 5) and No Peers (code 8); ⟳ Recover All button; stuck-download cleanup |
| **Rate limiting** | Token-bucket rate limiter for AllDebrid API (configurable req/min) — not a concurrency semaphore |
| **FIFO queue** | Oldest torrents always processed first (ORDER BY id ASC throughout all dispatch paths) |
| **Discord webhooks** | Rich embeds per event type: added, complete, error, upload-failed, no-peers, extraction, stats |
| **Jackett search** | Multi-indexer chip UI, category filters, per-row Add, Add Selected checkbox, Add All button |
| **Prowlarr search** | Modern Jackett alternative;  — same result format as Jackett |
| **FlexGet v3** | Schedule and trigger tasks from UI; per-event Discord notifications |
| **Statistics** | Period selector (1h–all-time), rolling snapshots, Discord summary reports, JSON export |
| **Prometheus metrics** | `GET /api/metrics` — torrent counts by status, active downloads, errors, SSE subscribers, bytes downloaded |
| **Database** | SQLite (zero-config default) or external PostgreSQL; automatic schema migration; 8 performance indexes |
| **Backups** | Scheduled JSON backups with configurable interval and retention |
| **Event log TTL** | Automatic pruning of old event log entries (default: 30 days); torrent rows never deleted |
| **Diagnostics** | `GET /api/torrents/diagnose` — status breakdown; `POST /api/torrents/recover-all` — one-click recovery |
| **State machine** | Formal torrent lifecycle with validated transitions (`services/torrent_state.py`) |
| **Rule Engine** | JSON-based pre-upload rules: set download path, priority, label, block or pause torrents by title/size/source |
| **Download Profiles** | Named preset bundles (path + priority + label); one active at a time, overrideable by rules |
| **Saved Searches** | Scheduled Jackett/Prowlarr queries with auto-add and configurable intervals |
| **Priority Queue** | Integer priority field; `ORDER BY priority DESC` dispatch; drag-and-drop reordering in UI |
| **Historical Learning** | Tracks indexer success/failure rates; annotates Jackett results with trust score (0–100) |
| **Webhook Actions** | Generic HTTP POST webhooks on torrent lifecycle events (added, complete, error); separate from Discord |
| **Plex / Jellyfin** | Automatic library refresh after each completed download via Plex token or Jellyfin API key |
| **AllDebrid orphan cleanup** | Auto-deletes error/no-peer magnets on AllDebrid that have no local DB row; manual trigger button |
| **Extraction passwords** | Per-archive password list (newline-separated); each tried in order for 7z and RAR |
| **Analytics** | Queue analytics with hourly chart and configurable time windows (1h / 24h / 7d / 30d) |
| **Smart Scheduler** | Time-window enforcement for download slots; configurable active hours |

---

## Quick Start

### Docker Compose (recommended)

```bash
git clone https://github.com/kroeberd/alldebrid-client.git
cd alldebrid-client
docker compose up -d
```

Open **http://localhost:8080** → Settings → enter your AllDebrid API key.

### Docker run

```bash
docker run -d \
  --name alldebrid-client \
  --restart unless-stopped \
  -p 8080:8080 \
  -e PUID=99 \
  -e PGID=100 \
  -e TZ=Europe/Berlin \
  -v /path/to/config:/app/config \
  -v /path/to/downloads:/downloads \
  kroeberd/alldebrid-client:latest
```

> **File permissions:** set `PUID`/`PGID` to the UID/GID of the user that runs your other media containers (Sonarr, Radarr, Plex, etc.). Run `id` on the host to find the right values.

### Unraid

Install **AllDebrid-Client** from the Community Apps store. All paths are pre-filled.

---

## Configuration

All settings are in the **Settings** page of the web UI. The most important ones to set after first start:

| Setting | Where | Notes |
|---------|-------|-------|
| `PUID` / `PGID` env vars | `docker-compose.yml` | UID/GID for downloaded files |
| AllDebrid API key | Settings → General | Required |
| Download folder | Settings → Download Client | Must be writable by the container |
| aria2 mode | Settings → Download Client | Built-in (default) or External RPC |
| Discord webhook | Settings → Notifications | Optional |
| Sonarr / Radarr URL + API key | Settings → Services | Optional |
| Auth username / password | Settings → Access Control | Optional — leave either empty to disable |
| Min free disk space (GB) | Settings → Download Client | 0 = disabled |
| `log_level` / `log_pretty` / `log_format` | `config.json` | Optional Docker-safe logging controls; defaults are `INFO`, `false`, `plain` |

See **Help → Settings Reference** in the web UI for a full description of every setting.

---

## Sonarr / Radarr Integration

AllDebrid-Client emulates the **qBittorrent v4.3.2 Web API** at `/api/v2/`. Configure it as a standard qBit download client:

```
Settings → Download Clients → + → qBittorrent
  Host:      your-server-ip
  Port:      8080  (or your mapped port)
  Category:  (any value — stored but not used for routing)
  Username:  (empty, or match Settings → Access Control)
  Password:  (empty, or match Settings → Access Control)
```

Click **Test** — it should show a green checkmark. See **Help → Sonarr/Radarr** in the web UI for the full status mapping table and troubleshooting guide.

---

## Jackett Search

1. Install and run [Jackett](https://github.com/Jackett/Jackett)
2. In AllDebrid-Client **Settings → Services → Jackett**: enter URL and API key, enable, Save
3. The **Search** view appears — search by title, filter by indexer
4. Add individual results, use **Add Selected** (checkbox per row), or **Add All**

---

## Auto-Extraction

Enable in **Settings → Auto-Extraction**. Archives are extracted automatically after every successful download. Auto-extract uses the completed file list recorded by the downloader, so it does not recursively scan large media folders.

| Format | Extension(s) | Engine |
|--------|-------------|--------|
| ZIP | `.zip` | Python `zipfile` (built-in) |
| TAR (all compressions) | `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.tar.zst` | Python `tarfile` (built-in) |
| Gzip / Bzip2 / XZ | `.gz`, `.bz2`, `.xz` | Python built-ins |
| 7-Zip | `.7z` | `7z` binary (`p7zip-full`) |
| RAR / RAR5 | `.rar`, `.r00`, multi-part | `7z` (primary) + `unrar-free` (fallback) |

Both `p7zip-full` and `unrar-free` are included in the Docker image — no extra setup needed.

---

## Discord Webhooks

Set `discord_webhook_url` in Settings → Notifications. Per-event toggles control which events trigger a notification independently.

| Event | Trigger |
|-------|---------|
| 📥 Torrent Added | Magnet/torrent accepted by AllDebrid |
| ✅ Download Complete | All files downloaded successfully |
| ❌ Download Error | One or more files failed |
| ⚠️ Upload Failed | AllDebrid returned code 5 (auto-retry in progress) |
| 🔗 No Peers | AllDebrid returned code 8 (auto-retry or manual re-add needed) |
| ⚠️ Partial | Some files filtered/blocked, rest downloaded |
| 🌿 FlexGet | Run started / task result / run finished |
| 📊 Stats Report | Periodic summary webhook |

---

## Prometheus Metrics

```yaml
# prometheus.yml
- job_name: alldebrid
  static_configs:
    - targets: [your-host:8080]
  metrics_path: /api/metrics
```

Available metrics: `alldebrid_torrents_by_status`, `alldebrid_active_downloads`, `alldebrid_completed_downloads`, `alldebrid_error_torrents`, `alldebrid_pending_files`, `alldebrid_sse_subscribers`, `alldebrid_downloaded_bytes_total`.

---

## REST API

### Core

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/torrents` | List torrents (status filter, search, pagination) |
| `POST` | `/api/torrents/add-magnet` | Add magnet link |
| `POST` | `/api/torrents/check-duplicate` | Read-only duplicate preview before adding |
| `POST` | `/api/torrents/import-existing` | Import all AllDebrid magnets not yet in local DB |
| `POST` | `/api/torrents/recover-all` | Reset stuck torrents and dispatch all ready AllDebrid magnets |
| `GET` | `/api/torrents/diagnose` | Status breakdown and sample of non-terminal torrents |
| `GET` | `/api/torrents/{id}` | Single torrent detail |
| `DELETE` | `/api/torrents/{id}` | Delete torrent |
| `POST` | `/api/torrents/{id}/retry` | Retry failed torrent (re-uploads magnet if stored) |
| `GET` | `/api/stats` | Aggregate statistics |
| `GET` | `/api/settings` | Current settings |
| `PUT` | `/api/settings` | Update settings |

### SSE

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/events/stream` | SSE stream (`connected`, `ping`, `torrent_updated`, `stats_changed`) |
| `GET` | `/api/events/subscriber-count` | Active SSE connection count |

### qBittorrent API emulation (`/api/v2/`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v2/auth/login` | Accept credentials |
| `GET` | `/api/v2/app/version` | Returns `v4.3.2` |
| `GET` | `/api/v2/torrents/info` | Torrent list with qBit state mapping |
| `POST` | `/api/v2/torrents/add` | Add via magnet or `.torrent` upload |
| `GET` | `/api/v2/torrents/files` | Per-file progress |
| `GET` | `/api/v2/torrents/properties` | Extended torrent properties |
| `POST` | `/api/v2/torrents/delete` | Delete torrent(s) |
| `POST` | `/api/v2/torrents/pause` / `resume` | Pause / resume |
| `GET` | `/api/v2/transfer/info` | Download speed |
| `GET` | `/api/v2/sync/maindata` | Full state snapshot |

### Observability & Admin

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/metrics` | Prometheus-compatible metrics |
| `GET` | `/api/version` | Client version |
| `POST` | `/api/admin/full-sync` | Full AllDebrid reconciliation |
| `POST` | `/api/admin/deep-sync` | aria2 filesystem reconciliation |
| `POST` | `/api/admin/database/backup` | Create a database backup |
| `POST` | `/api/admin/migrate` | SQLite ↔ PostgreSQL migration |
| `POST` | `/api/admin/database/wipe` | Wipe the database (guarded) |

---

## FlexGet

```bash
flexget web gentoken   # generate API token
```

Enter the token in **Settings → Services → FlexGet**. Tasks are executed via the FlexGet v3 REST API.

---

## Development

```bash
# Backend (Python 3.12+)
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8080

# Tests
python -m pytest tests -v
```

### Project structure

```
backend/
  api/
    routes.py          # FastAPI endpoints (71 routes)
    qbit.py            # qBittorrent v4.3.2 API emulation (28 routes)
  core/
    config.py          # Settings model (Pydantic, ~65 settings)
    scheduler.py       # Poll loops: AllDebrid, aria2, FlexGet, Stats, Events TTL
  db/
    database.py        # SQLite/PostgreSQL abstraction + 8 performance indexes
    migration.py       # Bidirectional SQLite ↔ PostgreSQL migration
  services/
    alldebrid.py       # AllDebrid API client (token-bucket rate limited)
    aria2.py           # aria2 JSON-RPC client (serialised, rate-limited)
    aria2_runtime.py   # Built-in aria2 process manager
    extractor.py       # Auto-extraction (zip/7z/rar/tar)
    flexget.py         # FlexGet v3 REST client
    jackett.py         # Jackett search proxy
    manager_v2.py      # Core orchestration (TorrentManager)
    notifications.py   # Discord webhook service
    stats.py           # Statistics and reporting
    backup.py          # Automatic backups
    db_maintenance.py  # Events TTL cleanup
    integrations.py    # Sonarr/Radarr import webhooks
    torrent_state.py   # Formal state machine: TorrentStatus enum + VALID_TRANSITIONS
  tests/               # 228 tests (pytest + pytest-asyncio)
frontend/
  static/index.html    # Single-file web UI (vanilla JS, SSE, no build step)
docs/
  logo.svg / logo.png  # App logo
  postgresql.md        # PostgreSQL setup guide
  migration.md         # Migration guide
  discord-webhooks.md  # Discord configuration
```

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for full release history.

---

## Troubleshooting

### Jackett is slow or times out
- Increase the search timeout in Settings → Search / Indexers → Jackett
- Check Jackett logs for indexer-specific errors (`docker logs jackett`)
- Disable dead indexers in Jackett admin to reduce concurrent requests

### aria2 is not reachable
- If using built-in aria2: check Settings → Download → aria2 mode is set to **Built-in**
- If using external aria2: verify the JSON-RPC URL (e.g. `http://aria2:6800/jsonrpc`) and secret
- Run `POST /api/settings/test-aria2` or use the **Test aria2** button in Settings

### Duplicate detection blocks a re-add
- The same infohash cannot be added twice while the torrent is active
- To force a re-add: delete the existing torrent, then add the magnet again
- Check Settings → General → Duplicate Detection level

### Expired magnets ("Expired — files removed")
- AllDebrid removes cached files after ~30 days of inactivity
- The client detects `statusCode 3` and automatically re-uploads the magnet if stored
- If no magnet is stored, the torrent moves to error state — add the magnet again manually

### No-peer torrents not cleaning up
- Use **🧹 Clean AD Orphans** in the Torrents view to purge error magnets from AllDebrid
- These are magnets added directly on AllDebrid outside the client
- The client also runs `cleanup_alldebrid_orphans()` automatically every sync cycle

### PostgreSQL connection issues
- Ensure `DB_TYPE=postgres` and all `POSTGRES_*` env vars are set correctly
- Run `POST /api/settings/test-postgres` or use **Test PostgreSQL** in Settings → Database
- Check container logs: `docker logs alldebrid-client`

### Permission errors (download folder, extraction)
- Set `PUID` and `PGID` to match your host user (`id -u` / `id -g`)
- Ensure the volume mounts exist and are writable by the PUID/PGID user
- Example: `-e PUID=1000 -e PGID=1000`

### Docker networking (Jackett/aria2 unreachable)
- All services should be on the same Docker network, or use host IPs
- Use the container name as hostname: `http://jackett:9117` (if on same bridge)
- With `network_mode: host`, use `127.0.0.1` as the host


---

## License

MIT — see [LICENSE](LICENSE)
