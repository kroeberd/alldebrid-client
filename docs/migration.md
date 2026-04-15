# Datenbankmigration

## Überblick

Bidirektionale Migration zwischen SQLite und PostgreSQL über die REST-API.

## Schritte (empfohlen)

1. **Validieren** (kein Schreiben):
```bash
curl "http://localhost:8080/api/admin/migrate/validate?direction=sqlite_to_postgres"
```

2. **App stoppen**

3. **Migration durchführen**:
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
| `force`     | bool   | `false`  | Zieldaten überschreiben                      |

## Sicherheitsgarantien

- Ziel darf keine Daten enthalten (außer `force=true`)
- Vollständige Transaktion — Rollback bei Fehler
- Post-Migration-Zeilenzählung
- Quelldatenbank wird nie verändert

## Fehlercodes

| Fehler                              | Lösung                        |
|-------------------------------------|-------------------------------|
| Zieldatenbank enthält bereits Daten | `force: true` oder Ziel leeren |
| Quelldatei nicht gefunden           | `DB_PATH` prüfen              |
| asyncpg nicht installiert           | `pip install asyncpg`         |
| Zeilenzahl-Abweichung               | Migration wiederholen         |
