# Changelog

## [1.0.0] — 2026-04-18

Erster öffentlicher Release. Alle Kernfunktionen stabil und produktionsbereit.

### Neu seit 0.9.x
- **FlexGet Integration** — Tasks manuell oder per Schedule auslösen (FlexGet v3 API)
  - Korrekte Nutzung von `POST /api/tasks/execute/` mit Task-Liste
  - Asynchrones Polling via `GET /api/tasks/queue/{id}/`
  - Einstellbarer Jitter (±N Sekunden) für Schedule
  - Webhook-Events: `run_started`, `task_ok`, `task_error`, `run_finished`
- **Statistik- und Reporting-Modul** — umfassende Metriken über alle Aktivitäten
  - Zeitfenster frei wählbar (1h bis ~1 Jahr)
  - JSON-Export, periodische Snapshots
  - Separate Zeitstempel-Filter pro Tabelle (SQLite + PostgreSQL korrekt)
- **PostgreSQL vollständig** — alle DB-Zugriffe über `get_db()` abstrahiert
  - `_CursorWrapper`: `(await db.execute(...)).fetchall()` funktioniert für beide Backends
  - Startup-Sync: fehlende SQLite-Zeilen beim Start in PG kopieren
  - Verbindungswartezeit: 15 × 10 Sekunden (150 s max)
- **Full-Sync** — vollständiger AllDebrid-Abgleich alle 5 Min. (konfigurierbar)
  - Erkennt `ready`-Torrents die lokal als `error` oder `queued` hängen
  - Trennung von `sync_status_loop` (30s) und `full_sync_loop` (5min)
- **aria2 Verbesserungen**
  - RPC-Serialisierung via `_rpc_lock` (ein Request gleichzeitig)
  - 50ms Mindestabstand zwischen Requests
  - `cached_downloads` verhindert N×`get_all()` pro Dispatch-Zyklus
- **Race Condition behoben** — "erfolgreich dann fehlerhaft"
  - `completed`-Files aus Sync-Query entfernt
  - `reset_on_sync` prüft Terminal-Status vor Reset
- **Erweiterte Fehler-Erkennung**
  - "Download took more than 3 days" → automatisch bereinigt
  - `processing/uploading` > 24h → automatisch zurückgesetzt
- **Discord-Tab** Layout-Fix (falsch verschachtelter Button)
- **10 Settings-Tabs** korrekt balanciert (keine Duplikate mehr)

### Stabile Features (seit 0.8.x / 0.9.x)
- Automatischer Torrent-Lifecycle (Upload → Poll → Unlock → aria2 → Done)
- Watch Folder für `.torrent`- und `.magnet`-Dateien
- Sonarr / Radarr Import-Trigger
- Discord Rich Embeds mit Bot-Identität
- File Filters (Erweiterungen, Keywords, Mindestgröße)
- Automatische No-Peer-Bereinigung
- Stuck-Download-Erkennung und Reset
- Automatische Backups
- Bidirektionale SQLite ↔ PostgreSQL Migration
- PostgreSQL-Fallback auf SQLite bei Ausfall

---

## [0.9.x] — 2026-04-15 bis 2026-04-18

Entwicklungsphase. Enthält alle Fixes und Features die in v1.0.0 eingeflossen sind.

Detaillierter Verlauf der Patch-Versionen: [GitHub Releases](https://github.com/kroeberd/alldebrid-client/releases)

---

## [0.8.0] — 2026-04-15

- Neues Logo (Radar/Orbit-Design)
- Discord Bot-Identität konfigurierbar
- aria2 als einziger Download-Client (Direct Download entfernt)
- File Filters standardmäßig deaktiviert
- Database-Status in der Sidebar
- PostgreSQL-Fallback-Anzeige

## [0.7.0] — 2026-04-15

- PostgreSQL-Unterstützung
- Rich Discord Embeds
- Bidirektionale Datenbank-Migration
- Erweiterte Statistiken
