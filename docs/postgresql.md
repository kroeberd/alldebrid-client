# PostgreSQL-Konfiguration

AllDebrid-Client unterstützt drei Datenbankoptionen. SQLite ist der Standard
und benötigt kein Setup.

## Option 1 — SQLite (Standard)

Kein Setup erforderlich. Funktioniert sofort nach dem Start.
Geeignet für Einzelinstallationen und Homelab.

```json
{ "db_type": "sqlite" }
```

## Option 2 — PostgreSQL intern (empfohlen für neue Installationen)

PostgreSQL läuft als separater Container in docker-compose.
Daten werden in einem benannten Volume persistiert.

### Setup

```bash
# .env anlegen
cp .env.example .env
# POSTGRES_PASSWORD in .env setzen

# Mit internem PostgreSQL starten
COMPOSE_PROFILES=postgres docker compose up -d
```

Alternativ mit Override-Datei:
```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

Die App erkennt `DB_TYPE=postgres_internal` automatisch und konfiguriert
die Verbindung ohne weiteres Zutun.

### Vorteile
- Zero-Config — kein externes Setup nötig
- Daten in Docker-Volume, persistent über Container-Neustarts
- Kein einzelner SPOF (SQLite-Datei)

### Nachteile
- Etwas mehr RAM (~50–80 MB für PostgreSQL)
- Zwei Container statt einem

## Option 3 — PostgreSQL extern

Eigene PostgreSQL-Instanz (Synology, Proxmox, externer Server).

```json
{
  "db_type": "postgres",
  "postgres_host": "192.168.1.10",
  "postgres_port": 5432,
  "postgres_db": "alldebrid",
  "postgres_user": "alldebrid",
  "postgres_password": "sicher",
  "postgres_ssl": false
}
```

## Abwärtskompatibilität

Bestehende SQLite-Installationen starten unverändert weiter.
`db_type` fehlt in alten `config.json` → wird als `"sqlite"` interpretiert.

## Migration zwischen Backends

Siehe [migration.md](migration.md).

## Voraussetzungen

`asyncpg>=0.29.0` — bereits in `requirements.txt` und `Dockerfile` enthalten.
