import aiosqlite
import os
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "/app/data/alldebrid.db"))


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    cur = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cur.fetchall()}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def init_db():
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
        await _ensure_column(db, "torrents", "provider_status", "TEXT")
        await _ensure_column(db, "torrents", "provider_status_code", "INTEGER")
        await _ensure_column(db, "torrents", "polling_failures", "INTEGER DEFAULT 0")
        await _ensure_column(db, "torrents", "download_client", "TEXT DEFAULT 'direct'")
        await _ensure_column(db, "download_files", "download_id", "TEXT")
        await _ensure_column(db, "download_files", "download_client", "TEXT DEFAULT 'direct'")
        await _ensure_column(db, "download_files", "updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP")
        await db.commit()


async def get_db():
    return aiosqlite.connect(DB_PATH)
