<div align="center">
  <img src="docs/logo.svg" width="100" alt="AllDebrid-Client Logo"/>
  <h1>AllDebrid-Client</h1>
  <p>Self-hosted torrent automation via AllDebrid — web UI, aria2 delivery, Discord notifications</p>

  [![Release](https://img.shields.io/github/v/release/kroeberd/alldebrid-client?style=flat-square&color=f97316)](https://github.com/kroeberd/alldebrid-client/releases)
  [![License](https://img.shields.io/github/license/kroeberd/alldebrid-client?style=flat-square)](LICENSE)
  [![Docker](https://img.shields.io/badge/docker-kroeberd%2Falldebrid--client-blue?style=flat-square&logo=docker)](https://hub.docker.com/r/kroeberd/alldebrid-client)
</div>

---

## What it does

AllDebrid-Client monitors your AllDebrid account, downloads completed torrents via **aria2**, and keeps everything organized. Add magnets via the web UI, a watch folder, or the REST API — the app handles the rest.

- **Uploads** magnet links and `.torrent` files to AllDebrid
- **Polls** AllDebrid until the torrent is ready
- **Unlocks** download links and hands them to aria2
- **Monitors** aria2 until all files are done, then marks the torrent complete and removes it from AllDebrid
- **Notifies** via Discord rich embeds on add, complete, error, and partial events
- **Auto-removes** torrents with "No peer after 30 minutes" status

---

## Screenshots

| Dashboard | Settings |
|-----------|----------|
| Insight cards, stat counters, recent activity | Tabbed settings: General · Download · Discord · Database · Filters · Polling |

---

## Quick Start

### Docker Compose (SQLite — recommended for most users)

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
  -v /path/to/data:/app/data \
  kroeberd/alldebrid-client:latest
```

### Unraid

Use the community template or add manually:
- Image: `kroeberd/alldebrid-client:latest`
- Port: `8080`
- Config path: `/mnt/user/appdata/alldebrid-client/config`
- Data paths: watch, processed, downloads

---

## Configuration

All settings are available in the web UI under **Settings** (tabbed layout):

| Tab | Settings |
|-----|----------|
| **⚡ General** | AllDebrid API key, agent name, folder paths |
| **⬇️ Download** | aria2 RPC URL, secret, download root, concurrency |
| **🔔 Discord** | Bot name, avatar URL, webhook URLs, notification toggles |
| **🗄️ Database** | SQLite (default) or PostgreSQL (external) |
| **🚫 Filters** | Blocked extensions, keywords, minimum file size |
| **⏱ Polling** | AllDebrid poll interval, watch folder interval |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `/app/config/config.json` | Settings file location |
| `DB_PATH` | `/app/data/alldebrid.db` | SQLite database location |
| `TZ` | `Europe/Berlin` | Container timezone |
| `DB_TYPE` | — | Set to `postgres` to enable PostgreSQL |

---

## Download Delivery

**aria2** is the only supported download client. The app unlocks each AllDebrid link and submits it to aria2 via JSON-RPC. aria2 handles bandwidth, concurrency, and resume.

Requirements:
- aria2 with RPC enabled (`--enable-rpc --rpc-listen-all`)
- RPC URL configured in Settings → Download
- Optional RPC secret

> ℹ️ Direct download mode has been removed in v0.8.0.

---

## Database

### SQLite (default)

No setup required. Database is stored at `DB_PATH`. Works for any installation.

### PostgreSQL (external)

Set `db_type` to `postgres` in Settings → Database and fill in connection details. See [docs/postgresql.md](docs/postgresql.md) for setup instructions including migration from SQLite.

If PostgreSQL is unreachable at startup the app **automatically falls back to SQLite** and shows `⚠️ SQLite (fallback)` in the sidebar.

---

## Discord Notifications

Configure in Settings → Discord:

| Setting | Description |
|---------|-------------|
| **Bot Name** | Sender name shown in Discord (default: `AllDebrid-Client`) |
| **Bot Avatar URL** | Custom avatar image (default: app logo) |
| **Webhook URL** | Main webhook for complete/error/partial events |
| **Webhook URL — Added** | Optional separate channel for torrent-added events |
| Notify on Added / Finished / Error | Per-event toggles |

Rich embed format with fields: Source, Files, Size, Destination, Time.

---

## Sidebar Status

The sidebar shows live status for:

| Indicator | Meaning |
|-----------|---------|
| 🟢 **AllDebrid** | API connected, shows username |
| Premium until DD.MM.YYYY (N days) | Account expiry shown above AllDebrid dot |
| 🟢 **aria2** | aria2 RPC connected, shows version |
| 🟢 **DB** | Active database backend (SQLite / PostgreSQL) |

---

## Auto-Remove: No Peers

Torrents that AllDebrid marks as **"No peer after 30 minutes"** (status code 8) are automatically deleted from AllDebrid and marked as `deleted` in the database. No manual cleanup needed.

---

## REST API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/stats` | Queue health, counts, averages, db_type |
| `GET` | `/api/stats/detail` | Daily completions, sources |
| `GET` | `/api/torrents` | All torrent records |
| `POST` | `/api/torrents` | Add magnet link |
| `DELETE` | `/api/torrents/{id}` | Delete torrent |
| `GET` | `/api/events` | Event log |
| `GET` | `/api/changelog` | Changelog content |
| `POST` | `/api/settings/test-alldebrid` | Test API key + get premium status |
| `POST` | `/api/settings/test-aria2` | Test aria2 connection |
| `POST` | `/api/settings/test-discord` | Send test webhook |
| `POST` | `/api/settings/test-postgres` | Test PostgreSQL connection |
| `POST` | `/api/admin/migrate` | Migrate SQLite ↔ PostgreSQL |
| `GET` | `/api/admin/migrate/validate` | Validate migration (dry run) |

---

## File Filtering

Configure in Settings → Filters (disabled by default for new installs):

- Block by **file extension** (e.g. `.nfo`, `.jpg`, `.sfv`)
- Block by **keyword** in filename
- Block files **below a minimum size** (MB)

Filtered files are skipped; the remaining files download normally. A partial-download Discord notification is sent when filters apply.

---

## Development

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8080

# Tests (50 unit tests)
python -m pytest tests/test_manager_v2.py -v
```

### Project structure

```
backend/
  api/routes.py          # FastAPI endpoints
  core/config.py         # Settings model
  core/scheduler.py      # Polling loops
  db/database.py         # SQLite/PostgreSQL abstraction
  db/migration.py        # Bidirectional migration
  services/
    alldebrid.py         # AllDebrid API client
    aria2.py             # aria2 JSON-RPC client
    manager_v2.py        # Core orchestration logic
    notifications.py     # Discord webhook service
  tests/
    test_manager_v2.py   # 50 unit tests
frontend/
  static/index.html      # Single-file web UI
docs/
  logo.svg               # App logo
  postgresql.md          # PostgreSQL setup guide
  migration.md           # Migration guide
```

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for full release history.

**v0.8.0** — New logo · aria2-only · Discord bot identity · DB sidebar dot · Settings tabs · Event log search · Premium status in sidebar · Auto-remove no-peer torrents · File Filters off by default

**v0.7.0** — PostgreSQL support · Dashboard fix · Discord rich embeds · aria2 robustness · Expanded statistics

---

## License

MIT — see [LICENSE](LICENSE)
