# PostgreSQL-Konfiguration

AllDebrid-Client unterstützt PostgreSQL als Alternative zu SQLite. SQLite bleibt der Standard und ist vollständig abwärtskompatibel.

## Aktivierung

In der `config.json` oder über die Einstellungs-API:

```json
{
  "db_type": "postgres",
  "postgres_host": "localhost",
  "postgres_port": 5432,
  "postgres_db": "alldebrid",
  "postgres_user": "alldebrid",
  "postgres_password": "geheimes-passwort",
  "postgres_schema": "public",
  "postgres_ssl": false,
  "postgres_application_name": "alldebrid-client"
}
```

## Voraussetzungen

```bash
pip install asyncpg>=0.29.0
```

Das Paket ist in `requirements.txt` enthalten und wird beim normalen Docker-Build installiert.

## Docker-Compose Beispiel

```yaml
services:
  alldebrid-client:
    environment:
      - CONFIG_PATH=/app/config/config.json
    volumes:
      - ./config:/app/config

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: alldebrid
      POSTGRES_USER: alldebrid
      POSTGRES_PASSWORD: geheimes-passwort
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

## Schema-Initialisierung

Das Schema wird beim Start automatisch erstellt. Es sind keine manuellen `CREATE TABLE`-Befehle notwendig.

## Umschalten von SQLite auf PostgreSQL

Siehe [migration.md](migration.md).
