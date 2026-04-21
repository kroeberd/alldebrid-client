"""
Database layer for AllDebrid-Client.

Supports two modes (controlled by db_type in AppSettings):
  sqlite   -> Default, fully backward compatible, no setup needed
  postgres -> External PostgreSQL instance

Both modes use the same _DbConnection abstraction.

Usage:
    async with get_db() as db:
        rows = await db.fetchall("SELECT * FROM torrents WHERE status=?", ("completed",))
        row  = await db.fetchone("SELECT * FROM torrents WHERE id=?", (1,))
        await db.execute("UPDATE torrents SET status=? WHERE id=?", ("done", 1))
        await db.commit()

DB_PATH is exported for backward compatibility with existing code.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import aiosqlite

logger = logging.getLogger("alldebrid.db")

DB_PATH = Path(os.getenv("DB_PATH", "/app/data/alldebrid.db"))


def _get_settings():
    try:
        from core.config import get_settings
        return get_settings()
    except Exception:
        return None


def _is_postgres() -> bool:
    cfg = _get_settings()
    return cfg is not None and getattr(cfg, "db_type", "sqlite") == "postgres"


def _build_dsn() -> str:
    cfg = _get_settings()
    if cfg is None:
        raise RuntimeError("Settings not available")
    ssl = "require" if getattr(cfg, "postgres_ssl", False) else "disable"
    app_name = getattr(cfg, "postgres_application_name", "alldebrid-client")
    return (
        f"postgresql://{cfg.postgres_user}:{cfg.postgres_password}"
        f"@{cfg.postgres_host}:{cfg.postgres_port}/{cfg.postgres_db}"
        f"?sslmode={ssl}&application_name={app_name}"
    )


def _pg_safe(v):
    """Cast Python int values that exceed PostgreSQL int4 range to str so asyncpg
    does not raise 'value out of int32 range'.  PostgreSQL will cast str to the
    correct column type (BIGINT, TEXT, etc.) automatically."""
    if isinstance(v, int) and not isinstance(v, bool):
        if not (-2_147_483_648 <= v <= 2_147_483_647):
            return str(v)
    return v


class _CursorWrapper:
    """
    Wraps cursor results so (await db.execute(...)).fetchall() works for both backends.

    SQLite:     wraps the aiosqlite cursor, calls fetchall/fetchone on it.
    PostgreSQL: asyncpg has no cursor from execute(); instead we store the
                pre-fetched rows at construction time (passed in by _DbConnection.execute).
    """
    def __init__(self, backend: str, cursor, pg_rows=None):
        self._backend  = backend
        self._cursor   = cursor
        self._pg_rows  = pg_rows or []   # pre-fetched rows for PostgreSQL
        self._pg_index = 0

    async def fetchall(self):
        if self._backend == "sqlite" and self._cursor is not None:
            rows = await self._cursor.fetchall()
            return [dict(r) for r in rows]
        # PostgreSQL: return pre-fetched rows
        return self._pg_rows

    async def fetchone(self):
        if self._backend == "sqlite" and self._cursor is not None:
            row = await self._cursor.fetchone()
            return dict(row) if row else None
        # PostgreSQL: return first row
        return self._pg_rows[0] if self._pg_rows else None

    def __getitem__(self, key):
        return None  # safe fallback


class _DbConnection:
    """Unified connection API for SQLite and PostgreSQL."""

    def __init__(self, backend: str, raw):
        self._backend = backend
        self._raw = raw

    @property
    def backend(self) -> str:
        return self._backend

    def _adapt(self, sql: str) -> str:
        if self._backend == "sqlite":
            return sql
        import re
        counter = 0
        def _repl(_m):
            nonlocal counter
            counter += 1
            return f"${counter}"
        sql = re.sub(r"\?", _repl, sql)
        sql = sql.replace("CURRENT_TIMESTAMP", "NOW()")

        # datetime('now', '-N unit') → (NOW() + INTERVAL 'N unit')
        sql = re.sub(
            r"datetime\('now',\s*'(-?\d+)\s+(\w+)'\)",
            lambda m: f"(NOW() + INTERVAL '{m.group(1)} {m.group(2)}')",
            sql,
        )
        # datetime('now', ? || ' hours') → (NOW() - INTERVAL ... ) handled via param
        # Simpler form: datetime('now') → NOW()
        sql = re.sub(r"datetime\('now'\)", "NOW()", sql)

        # datetime('now', ? || ' hours') — dynamic interval with parameter
        # SQLite: datetime('now', ? || ' hours')  e.g. param = '-6'
        # PG:     (NOW() + ($N || ' hours')::interval)
        sql = re.sub(
            r"datetime\('now',\s*(\$\d+)\s*\|\|\s*'(\s*\w+\s*)'\)",
            lambda m: f"(NOW() + ({m.group(1)} || ' {m.group(2).strip()}')::interval)",
            sql,
        )

        # CAST((julianday(a)-julianday(b))*86400 AS INTEGER)
        # → CAST(EXTRACT(EPOCH FROM (a::timestamptz - b::timestamptz)) AS INTEGER)
        # julianday difference * 86400 = seconds; EXTRACT(EPOCH) already = seconds
        sql = re.sub(
            r"CAST\(\(julianday\((\w+)\)\s*-\s*julianday\((\w+)\)\)\s*\*\s*86400\s*AS\s*INTEGER\)",
            lambda m: (
                f"CAST(EXTRACT(EPOCH FROM "
                f"({m.group(1)}::timestamptz - {m.group(2)}::timestamptz)) AS INTEGER)"
            ),
            sql,
        )
        # Standalone julianday(a) - julianday(b) (not wrapped in CAST*86400)
        sql = re.sub(
            r"julianday\((\w+)\)\s*-\s*julianday\((\w+)\)",
            lambda m: (
                f"(EXTRACT(EPOCH FROM "
                f"({m.group(1)}::timestamptz - {m.group(2)}::timestamptz)) / 86400.0)"
            ),
            sql,
        )

        # DATE(col) → col::date
        sql = re.sub(r"\bDATE\(([^)]+)\)", lambda m: f"({m.group(1)})::date", sql)

        # Schema changes
        sql = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bDATETIME\b", "TIMESTAMPTZ", sql, flags=re.IGNORECASE)
        return sql

    async def execute(self, sql: str, params: Sequence[Any] = ()):
        sql = self._adapt(sql)
        if self._backend == "sqlite":
            cursor = await self._raw.execute(sql, params)
            return _CursorWrapper("sqlite", cursor)
        else:
            # For PostgreSQL: asyncpg infers int4 for Python int by default.
            # Values > 2^31-1 (e.g. size_bytes=5.7 GB, alldebrid_id>2B) cause
            # "value out of int32 range".  We force all large ints through str so
            # PostgreSQL casts them to the target column type without overflow.
            safe_params = tuple(_pg_safe(p) for p in params)

            # For PostgreSQL: use fetch() for SELECT (returns rows), execute() for DML.
            stripped = sql.lstrip()
            if stripped.upper().startswith("SELECT") or stripped.upper().startswith("WITH"):
                rows = await self._raw.fetch(sql, *safe_params)
                pg_rows = [dict(r) for r in rows]
            else:
                await self._raw.execute(sql, *safe_params)
                pg_rows = []
            return _CursorWrapper("postgres", None, pg_rows=pg_rows)

    async def executemany(self, sql: str, params_list: List[Sequence[Any]]):
        sql = self._adapt(sql)
        if self._backend == "sqlite":
            await self._raw.executemany(sql, [tuple(_pg_safe(p) for p in row) for row in params_list])
        else:
            await self._raw.executemany(sql, [tuple(_pg_safe(p) for p in row) for row in params_list])

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        sql = self._adapt(sql)
        if self._backend == "sqlite":
            self._raw.row_factory = aiosqlite.Row
            cur = await self._raw.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        else:
            rows = await self._raw.fetch(sql, *tuple(_pg_safe(p) for p in params))
            return [dict(r) for r in rows]

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        sql = self._adapt(sql)
        if self._backend == "sqlite":
            self._raw.row_factory = aiosqlite.Row
            cur = await self._raw.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row else None
        else:
            row = await self._raw.fetchrow(sql, *tuple(_pg_safe(p) for p in params))
            return dict(row) if row else None

    async def execute_returning_id(self, sql: str, params: tuple = ()) -> Optional[int]:
        """Execute an INSERT and return the generated row id (works for both backends)."""
        sql_adapted = self._adapt(sql)
        if self._backend == "sqlite":
            cur = await self._raw.execute(sql_adapted, params)
            return cur.lastrowid
        else:
            # PostgreSQL: append RETURNING id
            pg_sql = sql_adapted.rstrip().rstrip(";") + " RETURNING id"
            row = await self._raw.fetchrow(pg_sql, *tuple(_pg_safe(p) for p in params))
            return int(row["id"]) if row else None

    async def commit(self):
        if self._backend == "sqlite":
            await self._raw.commit()

    async def rollback(self):
        if self._backend == "sqlite":
            await self._raw.rollback()


@asynccontextmanager
async def get_db() -> AsyncIterator[_DbConnection]:
    if _is_postgres():
        try:
            import asyncpg
        except ImportError:
            raise RuntimeError("asyncpg is not installed. Run: pip install asyncpg")
        dsn = _build_dsn()
        conn = await asyncpg.connect(dsn)
        try:
            async with conn.transaction():
                yield _DbConnection("postgres", conn)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(DB_PATH, timeout=30) as conn:
            conn.row_factory = aiosqlite.Row
            yield _DbConnection("sqlite", conn)


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    """Adds column to table if it does not exist. Safe to call repeatedly."""
    try:
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            await db.commit()
            logger.debug("Added column %s.%s (%s)", table, column, definition)
    except Exception as exc:
        logger.warning("_ensure_column %s.%s failed (ignored): %s", table, column, exc)


async def _ensure_column_pg(conn, table: str, column: str, definition: str):
    import re
    row = await conn.fetchrow(
        "SELECT 1 FROM information_schema.columns WHERE table_name=$1 AND column_name=$2",
        table, column,
    )
    if row is None:
        definition = re.sub(r"\bDATETIME\b", "TIMESTAMPTZ", definition, flags=re.IGNORECASE)
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")


_SCHEMA_COLUMNS_TORRENTS = [
    ("provider_status",      "TEXT"),
    ("provider_status_code", "INTEGER"),
    ("polling_failures",     "INTEGER DEFAULT 0"),
    ("download_client",      "TEXT DEFAULT 'aria2'"),
    ("label",                "TEXT DEFAULT ''"),
    ("priority",             "INTEGER DEFAULT 0"),
]

_SCHEMA_COLUMNS_FILES = [
    ("download_id",     "TEXT"),
    ("download_client", "TEXT DEFAULT 'aria2'"),
    ("retry_count",     "INTEGER DEFAULT 0"),
    ("updated_at",      "DATETIME DEFAULT CURRENT_TIMESTAMP"),
]


async def init_db():
    if _is_postgres():
        await _init_db_postgres()
    # Always initialise SQLite too — manager_v2 uses aiosqlite directly
    # regardless of the active backend, so the SQLite file must have all
    # columns even when PostgreSQL is the primary database.
    await _init_db_sqlite()


async def _init_db_sqlite():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        # Enable WAL mode and busy timeout — prevents "database is locked" under concurrent load
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.commit()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS torrents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hash TEXT UNIQUE NOT NULL,
                name TEXT,
                magnet TEXT,
                status TEXT DEFAULT 'pending',
                alldebrid_id TEXT,
                size_bytes INTEGER DEFAULT 0,
                progress REAL DEFAULT 0,
                download_url TEXT,
                local_path TEXT,
                source TEXT DEFAULT 'watch',
                provider_status TEXT,
                provider_status_code INTEGER,
                polling_failures INTEGER DEFAULT 0,
                download_client TEXT DEFAULT 'aria2',
                label TEXT DEFAULT '',
                priority INTEGER DEFAULT 0,
                error_message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torrent_id INTEGER,
                filename TEXT,
                size_bytes INTEGER,
                download_url TEXT,
                local_path TEXT,
                status TEXT DEFAULT 'pending',
                download_id TEXT,
                download_client TEXT DEFAULT 'aria2',
                blocked INTEGER DEFAULT 0,
                block_reason TEXT,
                retry_count INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (torrent_id) REFERENCES torrents(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torrent_id INTEGER,
                level TEXT DEFAULT 'info',
                message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (torrent_id) REFERENCES torrents(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS flexget_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name TEXT NOT NULL,
                status TEXT DEFAULT 'unknown',
                elapsed_seconds REAL DEFAULT 0,
                result_json TEXT DEFAULT '{}',
                triggered_by TEXT DEFAULT 'manual',
                ran_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stats_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_json TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col, defn in _SCHEMA_COLUMNS_TORRENTS:
            await _ensure_column(db, "torrents", col, defn)
        for col, defn in _SCHEMA_COLUMNS_FILES:
            await _ensure_column(db, "download_files", col, defn)
        await db.commit()

    # ── Performance indexes (idempotent) ─────────────────────────────────────
    async with aiosqlite.connect(DB_PATH) as idx_db:
        for ddl in [
            # download_files: all sync/finalize queries filter on these
            "CREATE INDEX IF NOT EXISTS idx_dlfiles_torrent_status "
            "ON download_files (torrent_id, status, blocked)",
            # torrents: full_alldebrid_sync, sync_alldebrid_status scan these
            "CREATE INDEX IF NOT EXISTS idx_torrents_alldebrid_id "
            "ON torrents (alldebrid_id)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_status "
            "ON torrents (status)",
            # events: detail view always queries by torrent_id
            "CREATE INDEX IF NOT EXISTS idx_events_torrent_id "
            "ON events (torrent_id)",
        ]:
            await idx_db.execute(ddl)
        await idx_db.commit()
    logger.debug("SQLite indexes ensured")

    # Verify critical columns are present after migration
    async with aiosqlite.connect(DB_PATH) as verify_db:
        cur = await verify_db.execute("PRAGMA table_info(torrents)")
        cols = {row[1] for row in await cur.fetchall()}
        critical = {"priority", "label", "provider_status", "polling_failures"}
        missing = critical - cols
        if missing:
            logger.error("CRITICAL: columns still missing after migration: %s", missing)
        else:
            logger.info("SQLite schema verified — all critical columns present")
    logger.info("SQLite database initialised: %s", DB_PATH)


async def _init_db_postgres():
    try:
        import asyncpg
    except ImportError:
        raise RuntimeError("asyncpg is not installed. Run: pip install asyncpg")
    dsn = _build_dsn()
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS torrents (
                    id SERIAL PRIMARY KEY,
                    hash TEXT UNIQUE NOT NULL,
                    name TEXT,
                    magnet TEXT,
                    status TEXT DEFAULT 'pending',
                    alldebrid_id TEXT,
                    size_bytes BIGINT DEFAULT 0,
                    progress DOUBLE PRECISION DEFAULT 0,
                    download_url TEXT,
                    local_path TEXT,
                    source TEXT DEFAULT 'watch',
                    provider_status TEXT,
                    provider_status_code INTEGER,
                    polling_failures INTEGER DEFAULT 0,
                    download_client TEXT DEFAULT 'aria2',
                    label TEXT DEFAULT '',
                    priority INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS download_files (
                    id SERIAL PRIMARY KEY,
                    torrent_id INTEGER REFERENCES torrents(id),
                    filename TEXT,
                    size_bytes BIGINT,
                    download_url TEXT,
                    local_path TEXT,
                    status TEXT DEFAULT 'pending',
                    download_id TEXT,
                    download_client TEXT DEFAULT 'aria2',
                    blocked INTEGER DEFAULT 0,
                    block_reason TEXT,
                    retry_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    torrent_id INTEGER REFERENCES torrents(id),
                    level TEXT DEFAULT 'info',
                    message TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS flexget_runs (
                    id SERIAL PRIMARY KEY,
                    task_name TEXT NOT NULL,
                    status TEXT DEFAULT 'unknown',
                    elapsed_seconds REAL DEFAULT 0,
                    result_json TEXT DEFAULT '{}',
                    triggered_by TEXT DEFAULT 'manual',
                    ran_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS stats_snapshots (
                    id SERIAL PRIMARY KEY,
                    snapshot_json TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            for col, defn in _SCHEMA_COLUMNS_TORRENTS:
                await _ensure_column_pg(conn, "torrents", col, defn)
            for col, defn in _SCHEMA_COLUMNS_FILES:
                await _ensure_column_pg(conn, "download_files", col, defn)
            # Performance indexes (idempotent)
            for ddl in [
                "CREATE INDEX IF NOT EXISTS idx_dlfiles_torrent_status "
                "ON download_files (torrent_id, status, blocked)",
                "CREATE INDEX IF NOT EXISTS idx_torrents_alldebrid_id "
                "ON torrents (alldebrid_id)",
                "CREATE INDEX IF NOT EXISTS idx_torrents_status "
                "ON torrents (status)",
                "CREATE INDEX IF NOT EXISTS idx_events_torrent_id "
                "ON events (torrent_id)",
            ]:
                await conn.execute(ddl)
    finally:
        await conn.close()
    logger.info("PostgreSQL database initialised")

