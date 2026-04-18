# Datenbankmigration

## Overview

Bidirectional migration between SQLite and PostgreSQL via the REST API.

## Steps (recommended)

1. **Validieren** (kein Schreiben):
```bash
curl "http://localhost:8080/api/admin/migrate/validate?direction=sqlite_to_postgres"
```

2. **App stoppen**

3. **Run migration**:
```bash
curl -X POST http://localhost:8080/api/admin/migrate \
  -H "Content-Type: application/json" \
  -d '{"direction": "sqlite_to_postgres", "dry_run": false, "force": false}'
```

4. **`db_type` in config.json** oder `DB_TYPE`-Env setzen

5. **App neu starten**

## Parameter

| Parameter   | Typ    | Standard | Beschreibung                                 |
|-------------|--------|----------|----------------------------------------------|
| `direction` | string | —        | `sqlite_to_postgres` oder `postgres_to_sqlite` |
| `dry_run`   | bool   | `false`  | Nur validieren, nichts schreiben             |
| `force`     | bool   | `false`  | Overwrite target data                        |

## Sicherheitsgarantien

- Target must not contain data (unless `force=true`)
- Full transaction — rollback on error
- Post-migration row count validation
- Source database is never modified

## Fehlercodes

| Error                               | Solution                      |
|-------------------------------------|-------------------------------|
| Target database already contains data | Use `force: true` or empty target |
| Source file not found               | Check `DB_PATH`               |
| asyncpg nicht installiert           | `pip install asyncpg`         |
| Zeilenzahl-Abweichung               | Migration wiederholen         |
