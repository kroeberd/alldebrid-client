<div align="center">

# AllDebrid-Client

![AllDebrid-Client Logo](docs/logo.svg)

[![Docker Hub](https://img.shields.io/docker/pulls/kroeberd/alldebrid-client?label=Docker%20Pulls&logo=docker&logoColor=white)](https://hub.docker.com/r/kroeberd/alldebrid-client)
[![GitHub Release](https://img.shields.io/github/v/release/kroeberd/alldebrid-client?color=ff6b2b&label=Version)](https://github.com/kroeberd/alldebrid-client/releases)
[![License](https://img.shields.io/badge/License-MIT-3de68b)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-Join-5865f2?logo=discord&logoColor=white)](https://discord.gg/8Vb9cj4ksv)

Automated torrent downloading via AllDebrid with a polished web UI, watch-folder automation, Discord notifications, SQLite tracking, and unified direct/aria2 delivery.

Support the project: [buymeacoffee.com/kroeberd](https://buymeacoffee.com/kroeberd)

</div>

## Why AllDebrid-Client

- Add magnets manually or by dropping files into a watch folder
- Poll AllDebrid automatically until content is ready
- Download directly to disk or hand off unlocked links to aria2
- Track every torrent, file, and event in SQLite
- Remove completed magnets from AllDebrid automatically
- Emit Discord notifications for added, finished, and error states
- Review progress, errors, and finished events from a single dashboard

## Feature Highlights

### Download Flow

- Magnet input from the dashboard, torrent list, or watch folder
- `.torrent`, `.magnet`, and `.txt` files supported in the watch folder
- Finished downloads get a dedicated `Finished` monitor event
- Completed magnets are removed from AllDebrid automatically

### Monitoring

- Live dashboard with totals, active transfers, error count, blocked files, and recent completion insights
- Event log for upload, processing, queueing, finish, and cleanup actions
- Dedicated sidebar tabs for GitHub, changelog, support, and detailed statistics
- Detailed torrent modal with files, paths, status, and monitor history

### Delivery & Notifications

- Direct file download into your chosen target folder
- Optional aria2 JSON-RPC integration with duplicate protection, pause/resume, and start-paused support
- Discord webhook notifications with per-webhook throttling to reduce timeout pressure
- Partial webhook summaries when files are intentionally excluded, including counts and sizes for downloaded vs. skipped files

### Monitoring & Robustness

- Provider status and local download status are tracked separately
- Ready torrents only transition into delivery after usable file/link data is available
- Repeated AllDebrid polling failures are surfaced in the event log and escalated when they become persistent
- aria2 transfers are re-synced into the normal torrent lifecycle so finished torrents still end as `completed` and are removed from AllDebrid

### Safety & Persistence

- SQLite state tracking prevents duplicate processing
- File filters for blocked extensions, blocked keywords, and minimum file size
- Persistent config and database volumes for Docker and Unraid deployments

---

## Quick Start

### Docker Compose

```bash
git clone https://github.com/kroeberd/alldebrid-client.git
cd alldebrid-client
docker compose up -d
```

Open [http://localhost:8080](http://localhost:8080) and enter your AllDebrid API key in Settings.

### Docker build

```bash
docker build -t kroeberd/alldebrid-client:v0.5.3 .
```

Optional for local testing:

```bash
docker run --rm -p 8080:8080 \
  -e CONFIG_PATH=/app/config/config.json \
  -e DB_PATH=/app/config/alldebrid.db \
  -v ./config:/app/config \
  -v ./data:/app/data \
  kroeberd/alldebrid-client:v0.5.3
```

### Manual Run

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

---

## Configuration

All settings are editable in the web UI and persisted to `config/config.json`.

Migration note:

- Legacy JDownloader keys are ignored on load and can be removed from existing `config.json` files.
- If you switch to `aria2`, make sure `aria2_download_path` points at the same effective files that the app exposes under `download_folder`, or leave it empty when both containers share the same mount.

| Setting | Default | Description |
|---|---|---|
| `alldebrid_api_key` | - | Your AllDebrid API key |
| `alldebrid_agent` | `AllDebrid-Client` | Custom AllDebrid user agent |
| `watch_folder` | `/app/data/watch` | Folder scanned for `.torrent`, `.magnet`, or `.txt` files |
| `processed_folder` | `/app/data/processed` | Imported files are moved here after processing |
| `download_folder` | `/app/data/downloads` | Final download target |
| `max_concurrent_downloads` | `3` | Max parallel downloads |
| `discord_webhook_url` | - | Discord webhook target |
| `discord_notify_added` | `true` | Send notification for newly queued torrents |
| `discord_notify_finished` | `true` | Send notification when processing finishes |
| `discord_notify_error` | `true` | Send notification for errors |
| `download_client` | `direct` | `direct` for in-app downloads or `aria2` for external JSON-RPC delivery |
| `aria2_url` | `http://127.0.0.1:6800/jsonrpc` | aria2 JSON-RPC endpoint |
| `aria2_secret` | - | Optional RPC secret |
| `aria2_download_path` | - | Optional remote root path when aria2 writes to a different mount |
| `aria2_operation_timeout_seconds` | `15` | Timeout for aria2 RPC calls |
| `aria2_start_paused` | `false` | Queue new aria2 jobs paused |
| `blocked_extensions` | image and metadata types | Extensions blocked from download |
| `blocked_keywords` | `[]` | Case-insensitive filename keyword filter |
| `min_file_size_mb` | `0` | Minimum file size in MB, `0` disables the threshold |
| `poll_interval_seconds` | `30` | AllDebrid status polling interval |
| `watch_interval_seconds` | `10` | Watch-folder scan interval |

---

## Releases & Changelog

Every change, new feature, fix, and structural update must be recorded in [CHANGELOG.md](CHANGELOG.md) and released with a matching Git tag.

Versioning rules for this repository:

- `vX.Y.Z` for standard release tags
- New features increment `Y`: `vX.Y.0`
- Fixes, debugging, and small corrections increment `Z`: `vX.Y.Z`
- Fundamental or breaking structural changes start a new major stream and reset to `.0.0`: `vY.0.0`

Recommended release workflow:

1. Update the implementation.
2. Add the release entry to `CHANGELOG.md`.
3. Commit the release changes.
4. Create the matching tag, for example `git tag v0.5.3`.

GitHub automation included in this repository:

- Docker Hub description sync from `README.md`
- Smart multi-arch image builds to GHCR and Docker Hub on `main`, release tags, or scheduled base-image refresh runs
- Structured issue templates for bug reports and feature requests

---

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/torrents` | List torrents |
| `POST` | `/api/torrents/add-magnet` | Add a magnet |
| `POST` | `/api/torrents/import-existing` | Import magnets already on AllDebrid |
| `GET` | `/api/torrents/{id}` | Get torrent details, files, and monitor events |
| `DELETE` | `/api/torrents/{id}` | Delete a torrent |
| `POST` | `/api/torrents/{id}/retry` | Retry a failed torrent |
| `GET` | `/api/stats` | Dashboard statistics |
| `GET` | `/api/events` | Event and monitor feed |
| `GET` | `/api/settings` | Read settings |
| `PUT` | `/api/settings` | Save settings |
| `POST` | `/api/settings/test-discord` | Test the Discord webhook |
| `POST` | `/api/settings/test-alldebrid` | Test AllDebrid credentials |
| `POST` | `/api/settings/test-aria2` | Test aria2 JSON-RPC connectivity |
| `POST` | `/api/torrents/{id}/pause` | Pause aria2-backed file transfers |
| `POST` | `/api/torrents/{id}/resume` | Resume aria2-backed file transfers |

---

## Project Support

- Buy Me a Coffee: [buymeacoffee.com/kroeberd](https://buymeacoffee.com/kroeberd)
- Docker Hub: [kroeberd/alldebrid-client](https://hub.docker.com/r/kroeberd/alldebrid-client)
- Releases: [GitHub Releases](https://github.com/kroeberd/alldebrid-client/releases)
- Discord: [Join the server](https://discord.gg/8Vb9cj4ksv)

---

## Repository Layout

```text
alldebrid-client/
|-- backend/
|   |-- api/
|   |-- core/
|   |-- db/
|   |-- services/
|   |-- main.py
|   `-- requirements.txt
|-- docs/
|   `-- logo.svg
|-- frontend/static/
|   |-- index.html
|   `-- logo.svg
|-- .github/
|-- CHANGELOG.md
|-- Dockerfile
|-- docker-compose.yml
|-- VERSION
`-- README.md
```
