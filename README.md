# AllDebrid-Client

![AllDebrid-Client Logo](docs/logo.svg)

Automated torrent downloading via AllDebrid with a clean Web UI, watch folder, Discord notifications, and integrations for aria2/AriaNg and JDownloader.

## Features

- **Watch Folder** — Drop `.torrent` files or `.magnet` text files in. They get uploaded to AllDebrid and moved to `processed/`
- **AllDebrid Integration** — Upload magnets, poll status, auto-download when ready, auto-delete after completion
- **Discord Webhooks** — Notify on added / finished / error
- **aria2 / AriaNg** — Forward unlocked download links to aria2 via JSON-RPC
- **JDownloader** — Forward unlocked links via FlashGot endpoint
- **File Filters** — Block file types (images by default), keywords, minimum size
- **Database** — SQLite, tracks all torrents. Already-completed hashes are not re-downloaded
- **Web UI** — Dashboard, torrent queue, event log, and full settings editor

---

## Quick Start

### Docker (recommended)

```bash
git clone <this-repo>
cd alldebrid-client
docker compose up -d
```

Open [http://localhost:8080](http://localhost:8080) and enter your AllDebrid API key in Settings.

### Docker build

```bash
docker build -t kroeberd/alldebrid-client:v0.2.0 .
```

Optional for local testing:

```bash
docker run --rm -p 8080:8080 \
  -e CONFIG_PATH=/app/config/config.json \
  -e DB_PATH=/app/config/alldebrid.db \
  -v ./config:/app/config \
  -v ./data:/app/data \
  kroeberd/alldebrid-client:v0.2.0
```

### Manual

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

---

## Configuration

All settings are editable in the Web UI under **Settings**. The config is persisted to `config/config.json`.

| Setting | Default | Description |
|---|---|---|
| `alldebrid_api_key` | — | Your AllDebrid API key |
| `watch_folder` | `/app/data/watch` | Drop `.torrent` or `.magnet` files here |
| `processed_folder` | `/app/data/processed` | Files moved here after processing |
| `download_folder` | `/app/data/downloads` | Where downloaded files land |
| `max_concurrent_downloads` | 3 | Parallel downloads |
| `max_speed_mbps` | 0 (unlimited) | Speed cap in MB/s |
| `discord_webhook_url` | — | Discord webhook for notifications |
| `ariang_url` | — | aria2 RPC endpoint |
| `ariang_enabled` | false | Enable aria2 forwarding |
| `jdownloader_url` | — | JDownloader URL |
| `jdownloader_enabled` | false | Enable JDownloader forwarding |
| `blocked_extensions` | image types | File extensions to skip |
| `blocked_keywords` | [] | Filename keywords to skip |
| `poll_interval_seconds` | 30 | How often to check AllDebrid status |
| `watch_interval_seconds` | 10 | How often to scan watch folder |

---

## Releases & Changelog

Every change, new feature, fix, and structural update must be recorded in [CHANGELOG.md](CHANGELOG.md) and released with a matching Git tag.

Versioning rules for this repository:

- `vX.Y.Z` for standard release tags
- New features increment `Y`: `vX.Y.0`
- Fixes, debugging, and small corrections increment `Z`: `vX.Y.Z`
- Fundamental or breaking structural changes start a new major stream and reset to `.0.0`: `vY.0.0`

Examples:

- `v0.1.0` for new functionality
- `v0.1.1` for a fix
- `v1.0.0` for a major structural release

Recommended release workflow:

1. Update the implementation.
2. Add the release entry to `CHANGELOG.md`.
3. Commit the release changes.
4. Create the matching tag, for example `git tag v0.2.0`.

GitHub automation included in this repository:

- Docker Hub description sync from `README.md`
- Smart multi-arch image builds to GHCR and Docker Hub on `main`, release tags, or scheduled base-image refresh runs
- Structured issue templates for bug reports and feature requests

---

## Adding Torrents

1. **Web UI** — Paste a magnet link on the Dashboard or Torrents page
2. **Watch Folder** — Drop a `.torrent` file or a `.txt`/`.magnet` file containing `magnet:?xt=...` links
3. **Import Existing** — Click "Import from AllDebrid" to pull in magnets already on your account
4. **API** — `POST /api/torrents/add-magnet` with `{"magnet": "magnet:?xt=..."}`

---

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/torrents` | List all torrents |
| POST | `/api/torrents/add-magnet` | Add magnet |
| POST | `/api/torrents/import-existing` | Import from AllDebrid |
| GET | `/api/torrents/{id}` | Torrent detail + files + events |
| DELETE | `/api/torrents/{id}` | Delete torrent |
| POST | `/api/torrents/{id}/retry` | Retry failed torrent |
| GET | `/api/stats` | Dashboard stats |
| GET | `/api/events` | Event log |
| GET | `/api/settings` | Read config |
| PUT | `/api/settings` | Write config |
| POST | `/api/settings/test-discord` | Test Discord webhook |
| POST | `/api/settings/test-alldebrid` | Test API key |

---

## Directory Layout

```
alldebrid-client/
├── backend/
│   ├── main.py               # FastAPI app
│   ├── requirements.txt
│   ├── api/routes.py         # REST endpoints
│   ├── core/
│   │   ├── config.py         # Settings management
│   │   └── scheduler.py      # Background tasks
│   ├── db/database.py        # SQLite schema
│   └── services/
│       ├── alldebrid.py      # AllDebrid API client
│       ├── manager.py        # Torrent lifecycle manager
│       └── notifications.py  # Discord webhooks
├── frontend/static/index.html  # Web UI (single file)
├── Dockerfile
├── docker-compose.yml
└── README.md
```
