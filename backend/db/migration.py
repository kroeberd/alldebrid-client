"""
Safe bidirectional database migration: SQLite ↔ PostgreSQL.

Safety guarantees:
- Validates source and target before migrating
- Refuses to overwrite existing data (unless force=True)
- Uses transactions with rollback on error
- Post-migration row-count validation
- No silent data loss
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite  # Module-level import so tests can patch db.migration.aiosqlite

logger = logging.getLogger("alldebrid.migration")

# Tables in migration order (respecting foreign keys)
MIGRATION_TABLES = ["torrents", "download_files", "events"]


@dataclass
class MigrationResult:
    success: bool
    direction: str
    tables_migrated: Dict[str, int] = field(default_factory=dict)  # Tabellenname → Zeilenzahl
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def summary(self) -> str:
        if not self.success:
            return f"Migration failed ({self.direction}): {self.error}"
        rows = ", ".join(f"{t}: {n}" for t, n in self.tables_migrated.items())
        warn_text = f" | Warnings: {len(self.warnings)}" if self.warnings else ""
        return f"Migration successful ({self.direction}): {rows}{warn_text}"


class MigrationError(Exception):
    """Raised on critical migration errors."""


# ─────────────────────────────────────────────────────────────────────────────
# Öffentliche API
# ─────────────────────────────────────────────────────────────────────────────

async def migrate_sqlite_to_postgres(
    sqlite_path: Path,
    pg_dsn: str,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> MigrationResult:
    """
    Migriert Daten von SQLite nach PostgreSQL.

    Args:
        sqlite_path: Pfad zur SQLite-Datenbankdatei
        pg_dsn:      asyncpg DSN for PostgreSQL
        force:       If True, existing data in the target is overwritten
        dry_run:     Wenn True, wird nur validiert ohne zu schreiben
    """
    result = MigrationResult(success=False, direction="sqlite→postgres")
    try:
        await _do_sqlite_to_pg(sqlite_path, pg_dsn, force=force, dry_run=dry_run, result=result)
        result.success = True
    except MigrationError as e:
        result.error = str(e)
    except Exception as e:
        result.error = f"Unexpected error: {e}"
        logger.exception("Migration sqlite→postgres failed")
    return result


async def migrate_postgres_to_sqlite(
    pg_dsn: str,
    sqlite_path: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> MigrationResult:
    """
    Migriert Daten von PostgreSQL nach SQLite.

    Args:
        pg_dsn:      asyncpg DSN for PostgreSQL
        sqlite_path: path to the target SQLite database file
        force:       If True, existing data in the target is overwritten
        dry_run:     Wenn True, wird nur validiert ohne zu schreiben
    """
    result = MigrationResult(success=False, direction="postgres→sqlite")
    try:
        await _do_pg_to_sqlite(pg_dsn, sqlite_path, force=force, dry_run=dry_run, result=result)
        result.success = True
    except MigrationError as e:
        result.error = str(e)
    except Exception as e:
        result.error = f"Unexpected error: {e}"
        logger.exception("Migration postgres→sqlite failed")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Implementierung: SQLite → PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────

async def _do_sqlite_to_pg(
    sqlite_path: Path,
    pg_dsn: str,
    *,
    force: bool,
    dry_run: bool,
    result: MigrationResult,
):
    try:
        import asyncpg  # type: ignore
    except ImportError:
        raise MigrationError("asyncpg is not installed. Run: pip install asyncpg")

    # ── Quell-Existenzprüfung (billig, vor allen Netzwerkoperationen) ─────
    if not sqlite_path.exists():
        raise MigrationError(f"SQLite source file not found: {sqlite_path}")

    # ── Target validation (first — fast-fail before opening any DB) ──────
    pg_conn = await _pg_connect(pg_dsn)
    try:
        pg_counts = await _count_rows_pg(pg_conn)
        total_existing = sum(pg_counts.values())
        if total_existing > 0 and not force:
            raise MigrationError(
                f"PostgreSQL target database already contains data "
                f"({total_existing} rows in {list(pg_counts.keys())}). "
                f"Use force=True to proceed."
            )
        if total_existing > 0:
            result.warnings.append(
                f"Target database contains {total_existing} existing rows — will be overwritten (force=True)"
            )
    finally:
        await pg_conn.close()

    # ── Source row count ──────────────────────────────────────────────────
    async with aiosqlite.connect(sqlite_path) as src_conn:
        src_counts = await _count_rows_sqlite(src_conn)

    logger.info("SQLite source row counts: %s", src_counts)

    if dry_run:
        result.tables_migrated = dict(src_counts)
        logger.info("Dry-run: no changes made. Source data: %s", src_counts)
        return

    # ── Initialise target schema ──────────────────────────────────────────
    from db.database import _init_db_postgres  # type: ignore
    await _init_db_postgres()

    # ── Transfer data ─────────────────────────────────────────────────────
    pg_conn = await _pg_connect(pg_dsn)
    try:
        async with aiosqlite.connect(sqlite_path) as src:
            src.row_factory = aiosqlite.Row
            async with pg_conn.transaction():
                for table in MIGRATION_TABLES:
                    rows = await _fetch_all_sqlite(src, table)
                    if not rows:
                        result.tables_migrated[table] = 0
                        continue

                    # PostgreSQL: TRUNCATE vor dem Befüllen wenn force
                    if force:
                        await pg_conn.execute(f"TRUNCATE TABLE {table} CASCADE")

                    count = await _insert_rows_pg(pg_conn, table, rows)
                    result.tables_migrated[table] = count
                    logger.info("Migrated: %s → %d rows to PostgreSQL", table, count)

                # Sequenzen zurücksetzen
                for table in MIGRATION_TABLES:
                    await pg_conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                        f"COALESCE(MAX(id), 0)) FROM {table}"
                    )
    finally:
        await pg_conn.close()

    # ── Post-migration validation ─────────────────────────────────────────
    pg_conn = await _pg_connect(pg_dsn)
    try:
        pg_counts_after = await _count_rows_pg(pg_conn)
    finally:
        await pg_conn.close()

    for table in MIGRATION_TABLES:
        expected = src_counts.get(table, 0)
        actual = pg_counts_after.get(table, 0)
        if expected != actual:
            result.warnings.append(
                f"Row count mismatch in '{table}': expected {expected}, got {actual}"
            )
        else:
            logger.info("Validation OK: %s — %d rows", table, actual)


# ─────────────────────────────────────────────────────────────────────────────
# Implementierung: PostgreSQL → SQLite
# ─────────────────────────────────────────────────────────────────────────────

async def _do_pg_to_sqlite(
    pg_dsn: str,
    sqlite_path: Path,
    *,
    force: bool,
    dry_run: bool,
    result: MigrationResult,
):
    try:
        import asyncpg  # type: ignore
    except ImportError:
        raise MigrationError("asyncpg is not installed. Run: pip install asyncpg")

    # ── Source validation ──────────────────────────────────────────────────
    pg_conn = await _pg_connect(pg_dsn)
    try:
        src_counts = await _count_rows_pg(pg_conn)
    finally:
        await pg_conn.close()

    logger.info("PostgreSQL source row counts: %s", src_counts)

    # ── Target validation ─────────────────────────────────────────────────
    if sqlite_path.exists():
        async with aiosqlite.connect(sqlite_path) as db:
            sqlite_counts = await _count_rows_sqlite(db)
        total_existing = sum(sqlite_counts.values())
        if total_existing > 0 and not force:
            raise MigrationError(
                f"SQLite target database '{sqlite_path}' already contains data "
                f"({total_existing} rows). Use force=True or delete the file."
            )
        if total_existing > 0:
            result.warnings.append(
                f"SQLite target database contains {total_existing} existing rows — will be overwritten (force=True)"
            )

    if dry_run:
        result.tables_migrated = dict(src_counts)
        logger.info("Dry-run: no changes made. Source data: %s", src_counts)
        return

    # ── Initialise target schema ──────────────────────────────────────────
    from db.database import _init_db_sqlite  # type: ignore
    await _init_db_sqlite()

    # ── Transfer data ─────────────────────────────────────────────────────
    pg_conn = await _pg_connect(pg_dsn)
    try:
        async with aiosqlite.connect(sqlite_path) as dst:
            for table in MIGRATION_TABLES:
                rows = await _fetch_all_pg(pg_conn, table)
                if not rows:
                    result.tables_migrated[table] = 0
                    continue

                if force:
                    await dst.execute(f"DELETE FROM {table}")

                count = await _insert_rows_sqlite(dst, table, rows)
                result.tables_migrated[table] = count
                logger.info("Migrated: %s → %d rows to SQLite", table, count)

            await dst.commit()
    finally:
        await pg_conn.close()

    # ── Post-migration validation ─────────────────────────────────────────
    async with aiosqlite.connect(sqlite_path) as db:
        sqlite_counts_after = await _count_rows_sqlite(db)

    for table in MIGRATION_TABLES:
        expected = src_counts.get(table, 0)
        actual = sqlite_counts_after.get(table, 0)
        if expected != actual:
            result.warnings.append(
                f"Row count mismatch in '{table}': expected {expected}, got {actual}"
            )
        else:
            logger.info("Validation OK: %s — %d rows", table, actual)


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

async def _pg_connect(dsn: str):
    import asyncpg  # type: ignore
    try:
        return await asyncpg.connect(dsn)
    except Exception as e:
        raise MigrationError(f"Cannot connect to PostgreSQL: {e}")


async def _count_rows_sqlite(db) -> Dict[str, int]:
    counts = {}
    for table in MIGRATION_TABLES:
        try:
            cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cur.fetchone()
            counts[table] = row[0] if row else 0
        except Exception:
            counts[table] = 0
    return counts


async def _count_rows_pg(conn) -> Dict[str, int]:
    counts = {}
    for table in MIGRATION_TABLES:
        try:
            row = await conn.fetchrow(f"SELECT COUNT(*) AS c FROM {table}")
            counts[table] = row["c"] if row else 0
        except Exception:
            counts[table] = 0
    return counts


async def _fetch_all_sqlite(db, table: str) -> List[Dict[str, Any]]:
    db.row_factory = aiosqlite.Row
    cur = await db.execute(f"SELECT * FROM {table} ORDER BY id")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _fetch_all_pg(conn, table: str) -> List[Dict[str, Any]]:
    rows = await conn.fetch(f"SELECT * FROM {table} ORDER BY id")
    return [dict(r) for r in rows]


async def _insert_rows_pg(conn, table: str, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    columns = list(rows[0].keys())
    placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
    col_list = ", ".join(f'"{c}"' for c in columns)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    count = 0
    for row in rows:
        values = []
        for col in columns:
            val = row[col]
            # Timestamp-Strings nach PostgreSQL-kompatiblen Wert konvertieren
            if isinstance(val, str) and col.endswith("_at") and val:
                try:
                    from datetime import datetime
                    val = datetime.fromisoformat(val.replace("Z", "+00:00"))
                except Exception:
                    pass
            values.append(val)
        await conn.execute(sql, *values)
        count += 1

    return count


async def _insert_rows_sqlite(db, table: str, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(f'"{c}"' for c in columns)
    sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"

    count = 0
    for row in rows:
        values = []
        for col in columns:
            val = row[col]
            # asyncpg gibt datetime-Objekte zurück — in ISO-String konvertieren
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            values.append(val)
        await db.execute(sql, values)
        count += 1

    return count
