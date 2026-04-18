<div align="center">
  <img src="docs/logo.svg" width="96" alt="AllDebrid-Client Logo"/>
  <h1>AllDebrid-Client</h1>
  <p><strong>Self-hosted torrent automation via AllDebrid</strong><br/>Web UI · aria2 delivery · Discord notifications · PostgreSQL support · FlexGet integration</p>

  [![Release](https://img.shields.io/github/v/release/kroeberd/alldebrid-client?style=flat-square&color=f97316)](https://github.com/kroeberd/alldebrid-client/releases)
  [![Docker Pulls](https://img.shields.io/docker/pulls/kroeberd/alldebrid-client?style=flat-square&color=3b82f6)](https://hub.docker.com/r/kroeberd/alldebrid-client)
  [![License](https://img.shields.io/github/license/kroeberd/alldebrid-client?style=flat-square)](LICENSE)
  [![Tests](https://img.shields.io/badge/tests-50%20passing-22c55e?style=flat-square)](#development)
</div>

---

## What it does

AllDebrid-Client automates the full torrent lifecycle via your AllDebrid account:

1. **Add** magnet links via the web UI, watch folder, Sonarr/Radarr, or REST API
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

### Docker Compose (empfohlen)

```bash
git clone https://github.com/kroeberd/alldebrid-client.git
cd alldebrid-client
docker compose up -d
```

Open **http://localhost:8080** → Settings → AllDebrid API key eingeben.

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
- 🔄 **Automatischer Lifecycle** — Upload → Poll → Unlock → aria2 → Done → Discord
- 📁 **Watch Folder** — `.torrent`- und `.magnet`-Dateien automatisch verarbeiten
- 🎯 **Slot-basierte aria2-Queue** — konfigurierbares Concurrent-Download-Limit
- 🔁 **Full-Sync** — regelmäßiger Abgleich aller Torrents gegen AllDebrid (alle 5 Min.)
- 🚫 **File Filters** — Erweiterungen, Keywords, Mindestgröße blockieren

### Notifications
- 🔔 **Discord** — Rich Embeds für Add / Complete / Error / Partial
- 🤖 **FlexGet Integration** — Tasks manuell oder per Schedule auslösen (FlexGet v3 API)
- 🌐 **Webhook Events** — FlexGet-spezifische Webhooks (run_started, task_ok, task_error, run_finished)

### Database & Reliability
- 🗄️ **SQLite** (Standard, kein Setup) oder **PostgreSQL** (extern)
- 🔄 **Startup-Sync** — fehlende SQLite-Zeilen beim Start automatisch in PG kopieren
- 🛡️ **Automatischer Fallback** — bei PG-Ausfall Weiterarbeit mit SQLite
- 💾 **Automatische Backups** — konfigurierbares Intervall

### Integrations
- 📺 **Sonarr / Radarr** — Import-Trigger nach Download
- 📊 **Statistik-Modul** — umfassende Metriken, Zeitfenster, JSON-Export
- 🔑 **PostgreSQL Migration** — bidirektional, trocken-testbar

---

## Configuration

Alle Einstellungen über die Web-UI unter **Settings** (10 Tabs):

| Tab | Einstellungen |
|-----|---------------|
| ⚡ **General** | AllDebrid API-Key, Agent-Name, Ordnerpfade |
| ⬇️ **Download** | aria2 RPC-URL, Secret, Download-Root, Max Concurrent |
| 🔔 **Discord** | Bot-Name, Avatar, Webhook-URLs, Notification-Toggles |
| 🔗 **Integrations** | Sonarr, Radarr |
| 🗄️ **Database** | SQLite / PostgreSQL, Migration |
| 🚫 **Filters** | Blockierte Erweiterungen, Keywords, Mindestgröße |
| ⏱ **Polling** | AllDebrid-Intervall, Full-Sync-Intervall, Watch-Folder |
| 💾 **Backup** | Automatische Backups, Intervall, Aufbewahrung |
| 🤖 **FlexGet** | URL, API-Key, Tasks, Schedule, Jitter, Webhook |
| 📊 **Reporting** | Statistik-Snapshots, Zeitfenster, Export |

### Umgebungsvariablen

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `CONFIG_PATH` | `/app/config/config.json` | Pfad zur Konfigurationsdatei |
| `DB_PATH` | `/app/data/alldebrid.db` | SQLite-Datenbankpfad |
| `TZ` | `Europe/Berlin` | Container-Zeitzone |
| `DB_TYPE` | — | `postgres` für PostgreSQL |
| `LOG_LEVEL` | `INFO` | `DEBUG` für ausführliche Logs |

---

## PostgreSQL

Siehe [docs/postgresql.md](docs/postgresql.md) für Setup-Anleitung und Migration.

**Kurzversion:**

```yaml
# docker-compose.yml Umgebungsvariablen
environment:
  DB_TYPE: postgres
  # PostgreSQL-Verbindung in Settings → Database konfigurieren
```

---

## FlexGet Integration

FlexGet v3 wird über seine REST API angesteuert:

```yaml
# FlexGet config.yml
web_server:
  bind: 0.0.0.0
  port: 5050
```

```bash
flexget web gentoken   # → API-Token generieren
```

Token in Settings → 🤖 FlexGet eintragen. Tasks werden per `POST /api/tasks/execute/` ausgeführt.

---

## REST API

| Methode | Pfad | Beschreibung |
|---------|------|--------------|
| `GET` | `/api/stats` | Queue-Health, Zähler, Durchschnitte |
| `GET` | `/api/stats/comprehensive?hours=N` | Umfassende Statistiken |
| `GET` | `/api/stats/export?hours=N` | JSON-Export |
| `GET` | `/api/torrents` | Alle Torrent-Einträge |
| `POST` | `/api/torrents/add-magnet` | Magnet-Link hinzufügen |
| `DELETE` | `/api/torrents/{id}` | Torrent löschen |
| `POST` | `/api/torrents/{id}/retry` | Torrent neu starten |
| `GET` | `/api/events` | Event-Log |
| `POST` | `/api/admin/full-sync` | Vollständiger AllDebrid-Abgleich |
| `POST` | `/api/admin/deep-sync` | aria2-Filesystem-Abgleich |
| `POST` | `/api/admin/migrate` | SQLite ↔ PostgreSQL Migration |
| `POST` | `/api/flexget/run` | FlexGet-Tasks ausführen |
| `GET` | `/api/flexget/tasks` | FlexGet-Tasks auflisten |
| `GET` | `/api/flexget/history` | FlexGet-Verlauf |

---

## Development

```bash
# Backend (Python 3.12+)
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8080

# Tests (50 Unit-Tests)
python -m pytest tests/test_manager_v2.py -v
```

### Projektstruktur

```
backend/
  api/routes.py          # FastAPI-Endpunkte
  core/config.py         # Settings-Modell (Pydantic)
  core/scheduler.py      # Poll-Loops (AllDebrid, aria2, FlexGet, Stats)
  db/database.py         # SQLite/PostgreSQL-Abstraktion (_DbConnection)
  db/migration.py        # Bidirektionale Migration
  services/
    alldebrid.py         # AllDebrid API-Client
    aria2.py             # aria2 JSON-RPC-Client (serialisiert, Rate-Limit)
    flexget.py           # FlexGet v3 REST-Client
    manager_v2.py        # Core-Orchestrierung (TorrentManager)
    notifications.py     # Discord Webhook-Service
    stats.py             # Statistik- und Reporting-Modul
    backup.py            # Automatische Backups
    integrations.py      # Sonarr/Radarr-Integration
  tests/
    test_manager_v2.py   # 50 Unit-Tests
frontend/
  static/index.html      # Single-File Web-UI (vanilla JS)
docs/
  logo.svg               # App-Logo
  postgresql.md          # PostgreSQL-Setup
  migration.md           # Migrations-Anleitung
  discord-webhooks.md    # Discord-Konfiguration
  screenshots/           # UI-Screenshots
```

---

## Changelog

Vollständiger Verlauf in [CHANGELOG.md](CHANGELOG.md).

---

## License

MIT — siehe [LICENSE](LICENSE)
