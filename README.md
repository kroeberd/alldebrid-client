<div align="center">
  <img src="docs/logo.svg" width="96" alt="AllDebrid-Client Logo"/>
  <h1>AllDebrid-Client</h1>
  <p><strong>Self-hosted torrent automation via AllDebrid</strong><br/>Web UI · aria2 delivery · Discord notifications · PostgreSQL support · FlexGet integration · Jackett search</p>

  [![Website](https://img.shields.io/badge/ad-client.mediastarr.de-ff6b2b?logo=googlechrome&logoColor=white)](https://ad-client.mediastarr.de/)
  [![Release](https://img.shields.io/github/v/release/kroeberd/alldebrid-client?style=flat-square&color=f97316)](https://github.com/kroeberd/alldebrid-client/releases)
  [![Docker Pulls](https://img.shields.io/docker/pulls/kroeberd/alldebrid-client?style=flat-square&color=3b82f6)](https://hub.docker.com/r/kroeberd/alldebrid-client)
  [![Discord](https://img.shields.io/badge/Discord-Join-5865f2?logo=discord&logoColor=white)](https://discord.gg/8Vb9cj4ksv)
  [![License](https://img.shields.io/github/license/kroeberd/alldebrid-client?style=flat-square)](LICENSE)
  [![Tests](https://img.shields.io/badge/tests-50%20passing-22c55e?style=flat-square)](https://github.com/kroeberd/alldebrid-client/actions/workflows/tests.yml)
  [![CI](https://img.shields.io/github/actions/workflow/status/kroeberd/alldebrid-client/tests.yml?style=flat-square&label=CI)](https://github.com/kroeberd/alldebrid-client/actions/workflows/tests.yml)
  [![Release Build](https://github.com/kroeberd/alldebrid-client/actions/workflows/release.yml/badge.svg)](https://github.com/kroeberd/alldebrid-client/actions/workflows/release.yml)
  [![Docker Build](https://github.com/kroeberd/alldebrid-client/actions/workflows/Docker_Build.yml/badge.svg)](https://github.com/kroeberd/alldebrid-client/actions/workflows/Docker_Build.yml)
</div>

---

## What it does

AllDebrid-Client automates the full torrent lifecycle via your AllDebrid account:

1. **Add** magnet links or `.torrent` files via the web UI, Jackett search, watch folder, Sonarr/Radarr, or REST API
2. **Upload** to AllDebrid and poll until the torrent is ready
3. **Unlock** download links and submit them to aria2
4. **Monitor** aria2 until all files complete, then mark done and remove from AllDebrid
5. **Notify** via Discord with rich embeds for every event

---

## Screenshots

| Dashboard | Torrents | Settings |
|-----------|----------|----------|
| [![Dashboard](docs/screenshots/dashboard.svg)](docs/screenshots/dashboard.svg) | [![Torrents](docs/screenshots/torrents.svg)](docs/screenshots/torrents.svg) | [![Settings](docs/screenshots/settings.svg)](docs/screenshots/settings.svg) |

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
  -v /path/to/config:/app/config \
  -v /path/to/downloads:/download \
  kroeberd/alldebrid-client:latest
```

### Unraid

Image: `kroeberd/alldebrid-client:latest` · Port: `8080`

---

## Features

### Core
- 🔄 **Automatic lifecycle** — upload → poll → unlock → aria2 → done → Discord
- 📁 **Watch folder** — automatically process `.torrent` and `.magnet` files
- 🎯 **Slot-based aria2 queue** — configurable concurrent download limit
- 🔁 **Full-Sync** — regular reconciliation of all torrents against AllDebrid (every 5 min)
- 🚫 **File filters** — block by extension, keyword, or minimum size
- 🔍 **Jackett search** — search configured indexers directly from the UI and add results in one click
- 📌 **Result awareness** — Jackett results show already-added torrents and current local status

### Notifications
- 🔔 **Discord** — rich embeds for add / complete / error / partial events
- 🤖 **FlexGet integration** — trigger tasks manually or on a schedule (FlexGet v3 API)
- 🌐 **Webhook events** — FlexGet-specific webhooks (run_started, task_ok, task_error, run_finished)
- 📈 **Reporting webhook** — send scheduled or manual statistics reports to a dedicated webhook or Discord fallback

### Database & Reliability
- 🗄️ **SQLite** (default, no setup) or **PostgreSQL** (external)
- 🔄 **Startup sync** — automatically copies missing SQLite rows to PostgreSQL on startup
- 🛡️ **Automatic fallback** — continues with SQLite if PostgreSQL is unreachable
- 💾 **Automatic backups** — configurable interval and retention
- 🧹 **Database maintenance** — separate database backup and guarded wipe actions from the UI
- 🧠 **aria2 housekeeping** — cleanup and memory-tuning helpers for long-running installs

### Integrations
- 📺 **Sonarr / Radarr** — import trigger after download completes
- 📊 **Statistics module** — comprehensive metrics, snapshots, time windows, JSON export, scheduled reports
- 🔑 **PostgreSQL migration** — bidirectional, dry-run testable
- 🧩 **Fenrus endpoint** — lightweight dashboard status endpoint for external dashboards

---

## Configuration

All settings via the web UI under **Settings**:

| Tab | Settings |
|-----|----------|
| ⚡ **General** | AllDebrid API key, agent name, folder paths |
| ⬇️ **Download** | aria2 RPC URL, secret, download root, max concurrent |
| 🔔 **Discord** | Bot name, avatar, webhook URLs, notification toggles |
| 🔗 **Integrations** | Sonarr, Radarr |
| 🗄️ **Database** | SQLite / PostgreSQL, migration |
| 🚫 **Filters** | Blocked extensions, keywords, minimum file size |
| ⏱ **Polling** | AllDebrid interval, full-sync interval, watch folder |
| 💾 **Backup** | Automatic backups, interval, retention |
| 🤖 **FlexGet** | URL, API key, tasks, schedule, jitter, webhook |
| 📊 **Reporting** | Statistics snapshots, report cadence/window, reporting webhook, export |
| 🔍 **Jackett** | Jackett URL, API key, connection test, tracker search integration |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `/app/config/config.json` | Settings file path |
| `DB_PATH` | `/app/data/alldebrid.db` | SQLite database path |
| `TZ` | `Europe/Berlin` | Container timezone |
| `DB_TYPE` | — | Set to `postgres` to enable PostgreSQL |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose logs |

---

## PostgreSQL

See [docs/postgresql.md](docs/postgresql.md) for setup instructions and migration guide.

**Quick setup:**

```yaml
# docker-compose.yml environment
environment:
  DB_TYPE: postgres
  # Configure connection in Settings → Database
```

---

## FlexGet Integration

FlexGet v3 is controlled via its REST API:

```yaml
# FlexGet config.yml
web_server:
  bind: 0.0.0.0
  port: 5050
```

```bash
flexget web gentoken   # generate API token
```

Enter the token in Settings → 🤖 FlexGet. Tasks are executed via `POST /api/tasks/execute/`.

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
