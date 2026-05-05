<div align="center">
  <img src="docs/logo.svg" width="96" alt="AllDebrid-Client Logo"/>
  <h1>AllDebrid-Client</h1>
  <p><strong>Self-hosted torrent automation via AllDebrid</strong><br/>Web UI · built-in or external aria2 · Jackett search · Discord notifications · PostgreSQL · FlexGet · Sonarr/Radarr</p>

  [![Website](https://img.shields.io/badge/ad-client.mediastarr.de-ff6b2b?logo=googlechrome&logoColor=white)](https://ad-client.mediastarr.de/)
  [![Release](https://img.shields.io/github/v/release/kroeberd/alldebrid-client?style=flat-square&color=f97316)](https://github.com/kroeberd/alldebrid-client/releases)
  [![Docker Pulls](https://img.shields.io/docker/pulls/kroeberd/alldebrid-client?style=flat-square&color=3b82f6)](https://hub.docker.com/r/kroeberd/alldebrid-client)
  [![Discord](https://img.shields.io/badge/Discord-Join-5865f2?logo=discord&logoColor=white)](https://discord.gg/8Vb9cj4ksv)
  [![License](https://img.shields.io/github/license/kroeberd/alldebrid-client?style=flat-square)](LICENSE)
  [![Tests](https://img.shields.io/badge/tests-188%20passing-22c55e?style=flat-square)](https://github.com/kroeberd/alldebrid-client/actions/workflows/tests.yml)
  [![CI](https://img.shields.io/github/actions/workflow/status/kroeberd/alldebrid-client/tests.yml?style=flat-square&label=CI)](https://github.com/kroeberd/alldebrid-client/actions/workflows/tests.yml)
  [![Release Build](https://github.com/kroeberd/alldebrid-client/actions/workflows/release.yml/badge.svg)](https://github.com/kroeberd/alldebrid-client/actions/workflows/release.yml)
  [![Docker Build](https://github.com/kroeberd/alldebrid-client/actions/workflows/Docker_Build.yml/badge.svg)](https://github.com/kroeberd/alldebrid-client/actions/workflows/Docker_Build.yml)
</div>

---

## What it does

AllDebrid-Client automates the full torrent lifecycle via your AllDebrid account:

1. **Add** magnet links or `.torrent` files via the web UI, Jackett search, watch folder, Sonarr/Radarr, or REST API
2. **Upload** to AllDebrid and poll until the torrent is ready
3. **Unlock** download links in parallel and submit them to aria2
4. **Monitor** aria2 until all files complete, then mark done and remove from AllDebrid
5. **Notify** via Discord with rich embeds for every event

---

## Screenshots

| Dashboard | Torrents | Settings |
|-----------|----------|----------|
| [![Dashboard](docs/screenshots/dashboard.svg)](docs/screenshots/dashboard.svg) | [![Torrents](docs/screenshots/torrents.svg)](docs/screenshots/torrents.svg) | [![Settings](docs/screenshots/settings.svg)](docs/screenshots/settings.svg) |

---

## Features

| Category | Details |
|----------|---------|
| **Input sources** | Web UI paste, Jackett search (multi-indexer), watch folder (`.torrent`/`.magnet`), Sonarr/Radarr, REST API |
| **Download client** | **Built-in aria2** (default, zero setup) or external aria2 instance |
| **Live speed badge** | Real-time aria2 download speed shown in the header (built-in mode only) |
| **Jackett search** | Multi-indexer selection with chip UI, category filters, direct Add from results |
| **Torrent list** | Pagination (15 / 25 / 50 / 100 per page), status filter, full-text search, bulk actions |
| **Downloads view** | Live aria2 queue with 1-second auto-refresh, per-file progress bars, Pause/Resume/Remove |
| **Discord webhooks** | Rich embeds for: Torrent Added, Download Complete, Error, Partial, FlexGet events, Stats reports |
| **FlexGet v3** | Schedule and trigger tasks from the UI; per-event Discord notifications |
| **Sonarr / Radarr** | Acts as a download client; triggers import on completion |
| **File filters** | Block by extension, keyword, or minimum size before any download starts |
| **Database** | SQLite (zero-config default) or external PostgreSQL |
| **Statistics** | Period selector (1h / 24h / 7d / 30d / 1y / all), rolling snapshots, Discord summary reports |
| **Help sidebar** | Built-in docs: Quick Start, How It Works, aria2, RAM & Memory, Integrations, Settings Reference, Troubleshooting |
| **Backups** | Scheduled SQLite backups with configurable retention |

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
  -v /path/to/downloads:/download \
  kroeberd/alldebrid-client:latest
```

> **File permissions:** set `PUID`/`PGID` to the UID/GID of the user that runs your other media containers (Sonarr, Radarr, Plex, etc.). Downloaded files will be owned by that user so they can be moved and imported without permission errors. Run `id` on the host to find the right values.

### Unraid

Install **AllDebrid-Client** from the Community Apps store. All paths are pre-filled.

---

## Configuration

All settings are in the **Settings** page of the web UI. The most important ones to set after first start:

| Setting | Where | Notes |
|---------|-------|-------|
| `PUID` / `PGID` env vars | `docker-compose.yml` | UID/GID for downloaded files — must match the user running Sonarr/Radarr/Plex. Run `id` on the host to find yours. |
| AllDebrid API key | Settings → ⚡ General | Required |
| Download folder | Settings → ⚡ General | Must be writable by the container |
| aria2 mode | Settings → ⬇️ Download | Built-in (default) or External |
| aria2 RPC URL | Settings → ⬇️ Download | Only for External mode: e.g. `http://localhost:6800/jsonrpc` |
| Discord webhook | Settings → 🔔 Notifications | Optional |
| Sonarr / Radarr | Settings → 🔌 Services | URL + API key |
| File filters | Settings → 🛠️ Advanced | Block extensions, keywords, min size |

Everything else has sensible defaults and can be tuned later.

### Built-in aria2 (default)

Built-in aria2 is enabled by default — no extra setup required. The container manages an embedded aria2 process automatically. The Downloads view shows the live queue with speed controls, and the header displays the current download speed in real time.

### External aria2

If you already run aria2 separately, switch to External in Settings → ⬇️ Download and enter the RPC URL. The Downloads view works identically.

---

## Jackett Search

1. Install and run [Jackett](https://github.com/Jackett/Jackett)
2. In AllDebrid-Client **Settings → 🔌 Services → Jackett**: enter the URL and API key, enable Jackett, Save
3. The **Search** sidebar entry appears — search by title, filter by category
4. Select one or multiple indexers using the chip picker (tap-friendly on mobile)
5. Click **Add** next to any result to queue it immediately

---

## Discord Webhooks

Set `discord_webhook_url` in Settings → 🔔 Notifications. Optionally set a separate URL for "torrent added" events (`discord_webhook_added`) and for Jackett additions (`jackett_webhook_url`).

**Events:**

| Event | Trigger |
|-------|---------|
| 📥 Torrent Added | Magnet/torrent accepted by AllDebrid |
| ✅ Download Complete | All files downloaded successfully |
| ❌ Download Error | One or more files failed |
| ⚠️ Partial | Some files filtered/blocked, rest downloaded |
| 🌿 FlexGet | Run started / task result / run finished / unreachable / recovered |
| 📊 Stats Report | Periodic summary webhook |

---

## Sonarr / Radarr

In Sonarr/Radarr: **Settings → Download Clients → Add → Torrent Blackhole**

- Watch folder: the same path mapped to the container's watch folder
- Completed download folder: the same path mapped to the download folder

Or add AllDebrid-Client as a custom script client pointing to the REST API.

---

## FlexGet

```bash
flexget web gentoken   # generate API token
```

Enter the token in **Settings → 🔌 Services → FlexGet**. Tasks are executed via `POST /api/tasks/execute/`.

---

## REST API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/stats` | Queue health, counters, averages |
| `GET` | `/api/stats/comprehensive?hours=N` | Comprehensive statistics |
| `GET` | `/api/stats/report?hours=N` | Formatted report payload |
| `POST` | `/api/stats/report/send?hours=N` | Send the current report to the reporting webhook |
| `GET` | `/api/stats/export?hours=N` | JSON export |
| `POST` | `/api/stats/snapshot` | Create a statistics snapshot |
| `GET` | `/api/torrents` | All torrent records |
| `POST` | `/api/torrents/add-magnet` | Add magnet link |
| `DELETE` | `/api/torrents/{id}` | Delete torrent |
| `POST` | `/api/torrents/{id}/retry` | Retry torrent |
| `GET` | `/api/events` | Event log |
| `POST` | `/api/jackett/search` | Search Jackett |
| `POST` | `/api/jackett/add` | Add a Jackett result |
| `GET` | `/api/jackett/indexers` | List configured Jackett indexers |
| `GET` | `/api/aria2/downloads` | Live aria2 queue |
| `POST` | `/api/aria2/downloads/{gid}/{action}` | Pause / resume / remove a job |
| `GET` | `/api/aria2/global-options` | Current aria2 speed limits and options |
| `POST` | `/api/aria2/global-options` | Set speed limit at runtime |
| `GET` | `/api/aria2/runtime` | Built-in aria2 runtime status and diagnostics |
| `POST` | `/api/aria2/runtime/start` | Start the built-in aria2 daemon |
| `POST` | `/api/aria2/runtime/stop` | Stop the built-in aria2 daemon |
| `POST` | `/api/aria2/runtime/restart` | Restart the built-in aria2 daemon |
| `POST` | `/api/aria2/runtime/apply` | Apply aria2 tuning and cleanup settings |
| `POST` | `/api/admin/full-sync` | Full AllDebrid reconciliation |
| `POST` | `/api/admin/deep-sync` | aria2 filesystem reconciliation |
| `POST` | `/api/admin/migrate` | SQLite ↔ PostgreSQL migration |
| `POST` | `/api/admin/database/backup` | Create a database backup |
| `GET` | `/api/admin/database/backups` | List database backups |
| `POST` | `/api/admin/database/wipe` | Wipe the database (guarded) |
| `POST` | `/api/flexget/run` | Execute FlexGet tasks |
| `GET` | `/api/flexget/tasks` | List FlexGet tasks |
| `GET` | `/api/flexget/history` | FlexGet run history |
| `GET` | `/api/integrations/fenrus/status` | Lightweight dashboard status for Fenrus |

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
  api/routes.py          # FastAPI endpoints
  core/config.py         # Settings model (Pydantic)
  core/scheduler.py      # Poll loops (AllDebrid, aria2, FlexGet, Stats)
  db/database.py         # SQLite/PostgreSQL abstraction (_DbConnection)
  db/migration.py        # Bidirectional migration
  services/
    alldebrid.py         # AllDebrid API client
    aria2.py             # aria2 JSON-RPC client (serialised, rate-limited)
    flexget.py           # FlexGet v3 REST client
    jackett.py           # Jackett search proxy
    manager_v2.py        # Core orchestration (TorrentManager)
    notifications.py     # Discord webhook service
    stats.py             # Statistics and reporting module
    backup.py            # Automatic backups
    integrations.py      # Sonarr/Radarr integration
  tests/
    test_manager_v2.py   # Torrent lifecycle / aria2 / reconciliation
    test_jackett.py      # Jackett integration
    test_webhook_settings_integration.py  # Settings and route regressions
frontend/
  static/index.html      # Single-file web UI (vanilla JS)
docs/
  logo.svg               # App logo
  logo.png               # PNG logo (for Unraid Community Apps)
  postgresql.md          # PostgreSQL setup guide
  migration.md           # Migration guide
  discord-webhooks.md    # Discord configuration
  screenshots/           # UI screenshots
```

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for full release history.

---

## License

MIT — see [LICENSE](LICENSE)
# Windows EXE Build

See [docs/windows-exe-build.md](docs/windows-exe-build.md) for full documentation on building and running the Windows EXE.
