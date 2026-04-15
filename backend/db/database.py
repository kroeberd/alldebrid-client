"""
Database layer for AllDebrid-Client.

Supports three modes (controlled by db_type in AppSettings):
  sqlite            -> Default, fully backward compatible, no setup needed
  postgres          -> External PostgreSQL instance
  postgres_internal -> Internal Docker container (mapped to "postgres" before use)

All three modes use the same _DbConnection abstraction.

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

# Abwärtskompatibel — bestehender Code importiert DB_PATH direkt
DB_PATH = Path(os.getenv("DB_PATH", "/app/data/alldebrid.db"))


# ─────────────────────────────────────────────────────────────────────────────
# Interne Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _get_settings():
    """Lazy-imports settings to avoid circular imports."""
    try:
        from core.config import get_settings
        return get_settings()
    except Exception:
        return None


def _is_postgres() -> bool:
    """Returns True when PostgreSQL (internal or external) is configured."""
    cfg = _get_settings()
    return cfg is not None and getattr(cfg, "db_type", "sqlite") == "postgres"


def _build_dsn() -> str:
    """Builds an asyncpg DSN from AppSettings."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Abstraktions-Wrapper
# ─────────────────────────────────────────────────────────────────────────────

class _DbConnection:
    """
    Unified connection API for SQLite and PostgreSQL.
    Translates ? → $1/$2 and SQLite-specific types to PostgreSQL.
    """

    def __init__(self, backend: str, raw):
        self._backend = backend  # "sqlite" | "postgres"
        self._raw = raw

    @property
    def backend(self) -> str:
        return self._backend

    def _adapt(self, sql: str) -> str:
        """Translates SQLite SQL to PostgreSQL-compatible SQL."""
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
        sql = re.sub(
            r"datetime\('now',\s*'(-?\d+)\s+(\w+)'\)",
            lambda m: f"(NOW() + INTERVAL '{m.group(1)} {m.group(2)}')",
            sql,
        )
        sql = re.sub(r"datetime\('now'\)", "NOW()", sql)
        sql = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bDATETIME\b", "TIMESTAMPTZ", sql, flags=re.IGNORECASE)
        return sql

    async def execute(self, sql: str, params: Sequence[Any] = ()):
        sql = self._adapt(sql)
        if self._backend == "sqlite":
            return await self._raw.execute(sql, params)
        else:
            return await self._raw.execute(sql, *params)

    async def executemany(self, sql: str, params_list: List[Sequence[Any]]):
        sql = self._adapt(sql)
        if self._backend == "sqlite":
            await self._raw.executemany(sql, params_list)
        else:
            await self._raw.executemany(sql, params_list)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        sql = self._adapt(sql)
        if self._backend == "sqlite":
            self._raw.row_factory = aiosqlite.Row
            cur = await self._raw.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        else:
            rows = await self._raw.fetch(sql, *params)
            return [dict(r) for r in rows]

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        sql = self._adapt(sql)
        if self._backend == "sqlite":
            self._raw.row_factory = aiosqlite.Row
            cur = await self._raw.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row else None
        else:
            row = await self._raw.fetchrow(sql, *params)
            return dict(row) if row else None

    async def commit(self):
        if self._backend == "sqlite":
            await self._raw.commit()

    async def rollback(self):
        if self._backend == "sqlite":
            await self._raw.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# Context-Manager-Factory
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def get_db() -> AsyncIterator[_DbConnection]:
    """
    Liefert eine Datenbankverbindung als async context manager.

    Beispiel:
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM torrents")
    """
    if _is_postgres():
        try:
            import asyncpg  # type: ignore
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
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            yield _DbConnection("sqlite", conn)


# ─────────────────────────────────────────────────────────────────────────────
# Schema-Migration-Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    """SQLite: add column if it does not exist."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cur.fetchall()}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def _ensure_column_pg(conn, table: str, column: str, definition: str):
    """PostgreSQL: add column if it does not exist."""
    import re
    row = await conn.fetchrow(
        "SELECT 1 FROM information_schema.columns WHERE table_name=$1 AND column_name=$2",
        table, column,
    )
    if row is None:
        definition = re.sub(r"\bDATETIME\b", "TIMESTAMPTZ", definition, flags=re.IGNORECASE)
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")


# ─────────────────────────────────────────────────────────────────────────────
# Schema-Definition
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA_COLUMNS_TORRENTS = [
    ("provider_status",      "TEXT"),
    ("provider_status_code", "INTEGER"),
    ("polling_failures",     "INTEGER DEFAULT 0"),
    ("download_client",      "TEXT DEFAULT 'direct'"),
]

_SCHEMA_COLUMNS_FILES = [
    ("download_id",     "TEXT"),
    ("download_client", "TEXT DEFAULT 'direct'"),
    ("updated_at",      "DATETIME DEFAULT CURRENT_TIMESTAMP"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Initialisierung
# ─────────────────────────────────────────────────────────────────────────────

async def init_db():
    """Initialises the database schema for the active backend."""
    if _is_postgres():
        await _init_db_postgres()
    else:
        await _init_db_sqlite()


async def _init_db_sqlite():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
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
                download_client TEXT DEFAULT 'direct',
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
                download_client TEXT DEFAULT 'direct',
                blocked INTEGER DEFAULT 0,
                block_reason TEXT,
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
        for col, defn in _SCHEMA_COLUMNS_TORRENTS:
            await _ensure_column(db, "torrents", col, defn)
        for col, defn in _SCHEMA_COLUMNS_FILES:
            await _ensure_column(db, "download_files", col, defn)
        await db.commit()
    logger.info("SQLite database initialised: %s", DB_PATH)


async def _init_db_postgres():
    """
    Initialisiert PostgreSQL-Schema. Wird auch beim ersten Start mit internem
    PG-Container aufgerufen — idempotent dank IF NOT EXISTS / IF NOT EXISTS.
    """
    try:
        import asyncpg  # type: ignore
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
                    download_client TEXT DEFAULT 'direct',
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
                    download_client TEXT DEFAULT 'direct',
                    blocked INTEGER DEFAULT 0,
                    block_reason TEXT,
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
            for col, defn in _SCHEMA_COLUMNS_TORRENTS:
                await _ensure_column_pg(conn, "torrents", col, defn)
            for col, defn in _SCHEMA_COLUMNS_FILES:
                await _ensure_column_pg(conn, "download_files", col, defn)
    finally:
        await conn.close()
    logger.info("PostgreSQL database initialised")


# Abwärtskompatibilität
async def get_db_legacy():
    """Deprecated: use get_db() instead."""
    return aiosqlite.connect(DB_PATH)
