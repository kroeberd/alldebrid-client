"""
Database maintenance helpers for explicit database backups and wipe operations.

Backups are exported as JSON snapshots so they work for both SQLite and PostgreSQL.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import date, datetime, time, timezone
from pathlib import Path

from core.config import get_settings
from db.database import _is_postgres, get_db

logger = logging.getLogger("alldebrid.db_maintenance")

TABLES = [
    "torrents",
    "download_files",
    "events",
    "flexget_runs",
    "stats_snapshots",
]


def _folder() -> Path:
    cfg = get_settings()
    return Path(getattr(cfg, "db_backup_folder", "/app/data/db-backups"))


def _keep_days() -> int:
    cfg = get_settings()
    return max(1, int(getattr(cfg, "db_backup_keep_days", 7) or 7))


def _json_default(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


async def run_database_backup() -> dict:
    cfg = get_settings()
    if not getattr(cfg, "db_backup_enabled", True):
        return {"skipped": True, "reason": "database backup disabled"}

    backup_folder = _folder()
    backup_folder.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_folder / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "timestamp": ts,
        "db_type": "postgres" if _is_postgres() else "sqlite",
        "tables": {},
    }
    errors: list[str] = []

    try:
        async with get_db() as db:
            for table in TABLES:
                rows = await db.fetchall(f"SELECT * FROM {table} ORDER BY id")
                payload["tables"][table] = rows
    except Exception as exc:
        errors.append(f"export: {exc}")

    json_path = backup_dir / "database.json"
    if not errors:
        json_path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")

    removed = _rotate_old_backups(backup_folder, _keep_days())
    result = {
        "timestamp": ts,
        "backup_dir": str(backup_dir),
        "file": str(json_path),
        "tables": {table: len(payload["tables"].get(table, [])) for table in TABLES},
        "errors": errors,
        "rotated": removed,
    }
    if errors:
        logger.warning("Database backup completed with errors: %s", errors)
    else:
        logger.info("Database backup completed: %s", ts)
    return result


def _rotate_old_backups(folder: Path, keep_days: int) -> int:
    removed = 0
    cutoff = datetime.now(timezone.utc).timestamp() - (keep_days * 86400)
    for entry in folder.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                removed += 1
        except Exception as exc:
            logger.warning("Could not rotate DB backup %s: %s", entry.name, exc)
    return removed


def list_database_backups() -> list[dict]:
    folder = _folder()
    if not folder.exists():
        return []
    entries = []
    for d in sorted(folder.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        try:
            files = [f.name for f in d.iterdir() if f.is_file()]
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            entries.append({"name": d.name, "files": files, "size_bytes": size})
        except Exception:
            continue
    return entries


async def wipe_database() -> dict:
    async with get_db() as db:
        if getattr(db, "backend", "sqlite") == "postgres":
            await db.execute(
                "TRUNCATE TABLE download_files, events, flexget_runs, stats_snapshots, torrents RESTART IDENTITY CASCADE"
            )
        else:
            await db.execute("DELETE FROM download_files")
            await db.execute("DELETE FROM events")
            await db.execute("DELETE FROM flexget_runs")
            await db.execute("DELETE FROM stats_snapshots")
            await db.execute("DELETE FROM torrents")
            try:
                await db.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN ('torrents','download_files','events','flexget_runs','stats_snapshots')"
                )
            except Exception as _e:
                logger.debug("sqlite_sequence reset skipped: %s", _e)
        await db.commit()

    logger.warning("Database wipe completed")
    return {"ok": True, "wiped_tables": TABLES}
