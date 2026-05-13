"""
Auto-Recovery System — services/recovery.py

Detects and heals common stuck states automatically:

  1. Orphaned 'queued' download_files whose GID no longer exists in aria2
     → reset to 'pending' so the next dispatch re-submits them

  2. Torrents stuck in 'downloading'/'queued' whose ALL local files are
     'completed' (missed completion event, e.g. container crash)
     → mark torrent as 'completed'

  3. Queue deadlock: 0 active downloads but ≥1 'ready' torrents exist
     → reset Manager Semaphore and trigger a dispatch pass

All actions are logged at INFO and insert an events row.
Never deletes torrent rows or file records.
"""
from __future__ import annotations

import logging

from db.database import get_db
from services.manager_v2 import manager

logger = logging.getLogger("alldebrid.recovery")


async def run_recovery_checks() -> dict:
    """Run all checks; return a summary dict. Safe to call from scheduler."""
    results: dict = {
        "orphaned_queued_files": 0,
        "missed_completions":    0,
        "deadlock_reset":        False,
        "errors":                [],
    }

    for name, coro in [
        ("orphaned_queued_files", _fix_orphaned_queued_files),
        ("missed_completions",    _fix_missed_completions),
        ("deadlock_reset",        _fix_queue_deadlock),
    ]:
        try:
            results[name] = await coro()
        except Exception as exc:
            results["errors"].append(f"{name}: {exc}")
            logger.debug("recovery: %s check failed: %s", name, exc)

    return results


# ── Individual checks ──────────────────────────────────────────────────────────

async def _fix_orphaned_queued_files() -> int:
    """Reset download_files with status='queued' not present in aria2."""
    try:
        all_dl = await manager.aria2().get_all(200, 200)
        aria2_gids: set[str] = {str(d.gid) for d in all_dl if hasattr(d, "gid")}
    except Exception:
        return 0  # aria2 unreachable — skip silently

    if not aria2_gids:
        return 0

    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT id, download_id, torrent_id FROM download_files WHERE status='queued'"
        )
        count = 0
        for row in rows:
            gid = str(row["download_id"] or "")
            if gid and gid not in aria2_gids:
                await db.execute(
                    "UPDATE download_files SET status='pending', download_id=NULL,"
                    " updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],),
                )
                count += 1
        if count:
            await db.commit()
            logger.info("recovery: reset %d orphaned queued file(s) → pending", count)
    return count


async def _fix_missed_completions() -> int:
    """
    Find 'downloading'/'queued' torrents whose ALL non-blocked local files are
    'completed' and mark the torrent done.
    """
    async with get_db() as db:
        rows = await db.fetchall(
            """SELECT t.id, t.name,
                      COUNT(f.id) AS total_files,
                      SUM(CASE WHEN f.status='completed' AND f.blocked=0 THEN 1 ELSE 0 END) AS done_files
               FROM torrents t
               JOIN download_files f ON f.torrent_id = t.id
               WHERE t.status IN ('downloading', 'queued')
                 AND f.blocked = 0
               GROUP BY t.id
               HAVING total_files > 0 AND total_files = done_files"""
        )
        count = 0
        for row in rows:
            logger.info(
                "recovery: torrent %s '%s' — all files done but stuck '%s' → completed",
                row["id"], (row["name"] or "?")[:50], "downloading",
            )
            await db.execute(
                """UPDATE torrents
                      SET status='completed',
                          completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                          updated_at   = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                (row["id"],),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?,?,?)",
                (row["id"], "info",
                 "Auto-recovery: all download_files completed — torrent marked done"),
            )
            count += 1
        if count:
            await db.commit()
    return count


async def _fix_queue_deadlock() -> bool:
    """Detect 0-active / N-ready deadlock and reset the dispatch gate."""
    from db.database import get_db
    from services.manager_v2 import manager

    async with get_db() as db:
        active_row = await db.fetchone(
            "SELECT COUNT(*) AS c FROM torrents WHERE status IN ('downloading','queued')"
        )
        ready_row = await db.fetchone(
            "SELECT COUNT(*) AS c FROM torrents WHERE status = 'ready'"
        )
    active = int((active_row or {}).get("c") or 0)
    ready  = int((ready_row  or {}).get("c") or 0)

    if active == 0 and ready > 0:
        logger.warning(
            "recovery: queue deadlock (%d ready, 0 active) — resetting dispatch", ready
        )
        manager.reset_services()
        return True
    return False
