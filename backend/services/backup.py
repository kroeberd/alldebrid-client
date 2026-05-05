"""
Automatic backup service for AllDebrid-Client.

Backs up config.json and the SQLite database to a configurable folder.
Rotates backups by keeping only the last N days worth.
"""
import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("alldebrid.backup")


def _cfg():
    try:
        from core.config import get_settings
        return get_settings()
    except Exception as exc:
        logger.warning("backup: could not read config: %s", exc)
        return None


async def run_backup() -> dict:
    """
    Performs a single backup run. Returns a summary dict.
    Backup folder default: /app/data/backups
    """
    cfg = _cfg()
    if not cfg or not getattr(cfg, "backup_enabled", True):
        return {"skipped": True, "reason": "backup disabled"}

    backup_folder = Path(getattr(cfg, "backup_folder", "/app/data/backups"))
    keep_days = max(1, int(getattr(cfg, "backup_keep_days", 7)))

    backup_folder.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_folder / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    backed_up = []
    errors = []

    # 1. config.json
    try:
        from core.config import CONFIG_PATH
        if CONFIG_PATH.exists():
            shutil.copy2(CONFIG_PATH, backup_dir / "config.json")
            backed_up.append("config.json")
    except Exception as e:
        errors.append(f"config: {e}")

    # 2. SQLite database
    try:
        from db.database import DB_PATH, _is_postgres
        if not _is_postgres() and DB_PATH.exists():
            shutil.copy2(DB_PATH, backup_dir / DB_PATH.name)
            backed_up.append(DB_PATH.name)
    except Exception as e:
        errors.append(f"database: {e}")

    # 3. Uploaded avatar
    try:
        from core.config import CONFIG_PATH
        config_dir = CONFIG_PATH.parent
        for ext in ("png", "jpg", "gif", "webp"):
            p = config_dir / f"avatar.{ext}"
            if p.exists():
                shutil.copy2(p, backup_dir / p.name)
                backed_up.append(p.name)
                break
    except Exception as e:
        errors.append(f"avatar: {e}")

    # 4. Rotate old backups
    removed = _rotate_backups(backup_folder, keep_days)

    result = {
        "timestamp": ts,
        "backup_dir": str(backup_dir),
        "backed_up": backed_up,
        "errors": errors,
        "rotated": removed,
    }
    if errors:
        logger.warning("Backup completed with errors: %s", errors)
    else:
        logger.info("Backup completed: %s (%d files)", ts, len(backed_up))
    return result


def _rotate_backups(backup_folder: Path, keep_days: int) -> int:
    """Remove backup directories older than keep_days days."""
    removed = 0
    cutoff = datetime.now(timezone.utc).timestamp() - (keep_days * 86400)
    for entry in backup_folder.iterdir():
        if not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
            if mtime < cutoff:
                shutil.rmtree(entry)
                removed += 1
                logger.debug("Rotated old backup: %s", entry.name)
        except Exception as e:
            logger.warning("Could not rotate backup %s: %s", entry.name, e)
    return removed


def list_backups() -> list:
    """Returns a list of existing backup entries, newest first."""
    cfg = _cfg()
    if not cfg:
        return []
    folder = Path(getattr(cfg, "backup_folder", "/app/data/backups"))
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
        except Exception as _e:
            logger.debug("Backup dir listing failed: %s", _e)
    return entries
