# Datenbankmigration: SQLite ↔ PostgreSQL

## Überblick

Die Migration ist bidirektional:
- **SQLite → PostgreSQL**: Bei Umstieg auf PostgreSQL
- **PostgreSQL → SQLite**: Für Backups oder Rückstufung

## Sicherheitsgarantien

- Quelldatenbank wird nur gelesen (kein Schreiben)
- Zieldatenbank darf keine Daten enthalten (außer `force=True`)
- Vollständige Transaktion: bei Fehler wird alles zurückgerollt
- Post-Migration-Validierung: Zeilenzahlen werden verglichen
- `dry_run=True`: Nur validieren, nichts schreiben

## Migrations-API

### Validierung (ohne Schreiben)

```bash
curl http://localhost:8080/api/admin/migrate/validate?direction=sqlite_to_postgres
```

### Migration durchführen

```bash
curl -X POST http://localhost:8080/api/admin/migrate \
  -H "Content-Type: application/json" \
  -d '{"direction": "sqlite_to_postgres", "dry_run": false, "force": false}'
```

Mögliche Richtungen:
- `sqlite_to_postgres`
- `postgres_to_sqlite`

### Parameter

| Parameter | Typ | Standard | Beschreibung |
|-----------|-----|----------|--------------|
| `direction` | string | — | Migrationsrichtung (erforderlich) |
| `dry_run` | bool | `false` | Nur validieren, nichts schreiben |
| `force` | bool | `false` | Zieldaten überschreiben (Vorsicht!) |

## Empfohlener Ablauf

1. **Validierung** mit `dry_run=true` durchführen
2. Zeilenanzahl und Warnungen prüfen
3. Anwendung stoppen
4. Migration mit `dry_run=false` durchführen
5. `db_type` in der Konfiguration ändern
6. Anwendung neu starten

## Fehlerbehandlung

| Fehler | Ursache | Lösung |
|--------|---------|--------|
| "Zieldatenbank enthält bereits Daten" | Ziel nicht leer | `force=true` oder Ziel leeren |
| "Quelldatei nicht gefunden" | SQLite-Pfad falsch | `DB_PATH` env prüfen |
| "asyncpg nicht installiert" | Paket fehlt | `pip install asyncpg` |
| "Zeilenzahl-Abweichung" | Inkonsistenz | Migration wiederholen |
