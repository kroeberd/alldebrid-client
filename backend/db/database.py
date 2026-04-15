"""
Datenbank-Schicht für AllDebrid-Client.

Unterstützt SQLite (Standard, abwärtskompatibel) und PostgreSQL.
Der aktive Backend wird durch `db_type` in den Einstellungen gesteuert.

Verwendung:
    async with get_db() as db:
        await db.execute("SELECT ...", params)
        rows = await db.fetchall("SELECT ...", params)
        row  = await db.fetchone("SELECT ...", params)
        await db.commit()

DB_PATH wird für Abwärtskompatibilität mit bestehendem Code exportiert.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import aiosqlite

logger = logging.getLogger("alldebrid.db")

# Abwärtskompatibel: bestehender Code importiert DB_PATH direkt
DB_PATH = Path(os.getenv("DB_PATH", "/app/data/alldebrid.db"))

# ─────────────────────────────────────────────────────────────────────────────
# Interne Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _get_settings():
    """Importiert Settings lazy, um Zirkularimporte zu vermeiden."""
    try:
        from core.config import get_settings
        return get_settings()
    except Exception:
        return None


def _is_postgres() -> bool:
    cfg = _get_settings()
    return cfg is not None and getattr(cfg, "db_type", "sqlite") == "postgres"


def _build_dsn() -> str:
    """Baut einen asyncpg-DSN aus den Einstellungen."""
    cfg = _get_settings()
    if cfg is None:
        raise RuntimeError("Einstellungen nicht verfügbar")
    ssl = "require" if getattr(cfg, "postgres_ssl", False) else "disable"
    return (
        f"postgresql://{cfg.postgres_user}:{cfg.postgres_password}"
        f"@{cfg.postgres_host}:{cfg.postgres_port}/{cfg.postgres_db}"
        f"?sslmode={ssl}&application_name={cfg.postgres_application_name}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Abstraktions-Wrapper
# ─────────────────────────────────────────────────────────────────────────────

class _DbConnection:
    """
    Einheitliche Verbindungs-API für SQLite und PostgreSQL.

    Unterstützt:
        execute(sql, params)    → cursor / status
        fetchall(sql, params)   → List[dict]
        fetchone(sql, params)   → Optional[dict]
        commit()
        rollback()
    """

    def __init__(self, backend: str, raw):
        self._backend = backend  # "sqlite" | "postgres"
        self._raw = raw           # aiosqlite.Connection | asyncpg.Connection

    @property
    def backend(self) -> str:
        return self._backend

    # ── SQL-Dialekt-Anpassung ──────────────────────────────────────────────

    def _adapt(self, sql: str) -> str:
        """
        Wandelt SQLite-spezifisches SQL in PostgreSQL-kompatibles SQL um.
        Placeholders ? → $1, $2, …; AUTOINCREMENT → entfernen; DATETIME → TIMESTAMPTZ.
        """
        if self._backend == "sqlite":
            return sql

        import re

        # ? → $1, $2, … (in Reihenfolge ersetzen)
        counter = 0

        def _replace_placeholder(_match):
            nonlocal counter
            counter += 1
            return f"${counter}"

        sql = re.sub(r"\?", _replace_placeholder, sql)

        # SQLite-Zeitfunktionen
        sql = sql.replace("CURRENT_TIMESTAMP", "NOW()")
        sql = re.sub(
            r"datetime\('now',\s*'(-?\d+)\s+(\w+)'\)",
            lambda m: f"(NOW() + INTERVAL '{m.group(1)} {m.group(2)}')",
            sql,
        )
        sql = re.sub(r"datetime\('now'\)", "NOW()", sql)

        # INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
        sql = re.sub(
            r"INTEGER PRIMARY KEY AUTOINCREMENT",
            "SERIAL PRIMARY KEY",
            sql,
            flags=re.IGNORECASE,
        )
        # DATETIME → TIMESTAMPTZ
        sql = re.sub(r"\bDATETIME\b", "TIMESTAMPTZ", sql, flags=re.IGNORECASE)

        # CREATE TABLE IF NOT EXISTS (PostgreSQL unterstützt das nativ)
        # ON CONFLICT(hash) DO UPDATE → PostgreSQL-Syntax ist identisch

        return sql

    # ── Öffentliche Methoden ────────────────────────────────────────────────

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
        # PostgreSQL: auto-commit bei asyncpg, explizite Transaktionen via context manager

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
            raise RuntimeError(
                "asyncpg ist nicht installiert. "
                "Installiere es mit: pip install asyncpg"
            )
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
# Legacy-Kompatibilität: aiosqlite.connect(DB_PATH) direkt nutzen
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    """SQLite-spezifische Spalten-Migration (nur für SQLite verwendet)."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cur.fetchall()}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def _ensure_column_pg(conn, table: str, column: str, definition: str):
    """PostgreSQL-spezifische Spalten-Migration."""
    import asyncpg  # type: ignore

    row = await conn.fetchrow(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name=$1 AND column_name=$2
        """,
        table,
        column,
    )
    if row is None:
        # definition ggf. anpassen (DATETIME → TIMESTAMPTZ)
        import re
        definition = re.sub(r"\bDATETIME\b", "TIMESTAMPTZ", definition, flags=re.IGNORECASE)
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")


# ─────────────────────────────────────────────────────────────────────────────
# Schema-DDL
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA_COLUMNS_TORRENTS = [
    ("provider_status",      "TEXT"),
    ("provider_status_code", "INTEGER"),
    ("polling_failures",     "INTEGER DEFAULT 0"),
    ("download_client",      "TEXT DEFAULT 'direct'"),
]

_SCHEMA_COLUMNS_FILES = [
    ("download_id",    "TEXT"),
    ("download_client","TEXT DEFAULT 'direct'"),
    ("updated_at",     "DATETIME DEFAULT CURRENT_TIMESTAMP"),
]


async def init_db():
    """Initialisiert das Datenbankschema für das aktive Backend."""
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
    logger.info("SQLite-Datenbank initialisiert: %s", DB_PATH)


async def _init_db_postgres():
    try:
        import asyncpg  # type: ignore
    except ImportError:
        raise RuntimeError("asyncpg nicht installiert. pip install asyncpg")

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
    logger.info("PostgreSQL-Datenbank initialisiert")


# Abwärtskompatibilität
async def get_db_legacy():
    """Veraltet: Verwende stattdessen get_db()."""
    return aiosqlite.connect(DB_PATH)
