import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import aiosqlite

from core.config import settings, save_settings, AppSettings, load_settings
from services.manager import manager
from services.notifications import NotificationService
from services.alldebrid import AllDebridService
from db.database import DB_PATH

router = APIRouter()
logger = logging.getLogger("alldebrid.api")


# ─── Settings ───────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings():
    return settings.model_dump()


@router.put("/settings")
async def update_settings(new: AppSettings):
    import core.config as cfg_module
    save_settings(new)
    cfg_module.settings = new
    # Reload global settings reference
    import services.manager as mgr_module
    mgr_module.manager.ad_service = None  # Force reinit
    return {"ok": True}


@router.post("/settings/test-discord")
async def test_discord():
    svc = NotificationService(settings.discord_webhook_url)
    ok = await svc.test()
    return {"ok": ok}


@router.post("/settings/test-alldebrid")
async def test_alldebrid():
    if not settings.alldebrid_api_key:
        raise HTTPException(400, "No API key configured")
    try:
        svc = AllDebridService(settings.alldebrid_api_key)
        user = await svc.get_user()
        return {"ok": True, "user": user}
    except Exception as e:
        raise HTTPException(400, str(e))


# ─── Torrents ────────────────────────────────────────────────────────────────

@router.get("/torrents")
async def list_torrents(
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where = ""
        params = []
        if status:
            where = "WHERE status = ?"
            params.append(status)
        cur = await db.execute(
            f"""SELECT t.*, 
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id) as file_count,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id AND blocked=1) as blocked_count
                FROM torrents t {where}
                ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            params + [limit, offset]
        )
        rows = await cur.fetchall()
        # Count
        cur2 = await db.execute(f"SELECT COUNT(*) FROM torrents {where}", params)
        total = (await cur2.fetchone())[0]
        return {"items": [dict(r) for r in rows], "total": total}


@router.get("/torrents/{torrent_id}")
async def get_torrent(torrent_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Torrent not found")
        files_cur = await db.execute(
            "SELECT * FROM download_files WHERE torrent_id=?", (torrent_id,)
        )
        files = [dict(f) for f in await files_cur.fetchall()]
        events_cur = await db.execute(
            "SELECT * FROM events WHERE torrent_id=? ORDER BY created_at DESC LIMIT 50",
            (torrent_id,)
        )
        events = [dict(e) for e in await events_cur.fetchall()]
        return {**dict(row), "files": files, "events": events}


class MagnetRequest(BaseModel):
    magnet: str


@router.post("/torrents/add-magnet")
async def add_magnet(req: MagnetRequest):
    if not settings.alldebrid_api_key:
        raise HTTPException(400, "AllDebrid API key not configured")
    try:
        result = await manager.add_magnet_direct(req.magnet, source="manual")
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/torrents/import-existing")
async def import_existing():
    if not settings.alldebrid_api_key:
        raise HTTPException(400, "AllDebrid API key not configured")
    results = await manager.import_existing_magnets()
    return {"imported": len(results), "items": results}


@router.delete("/torrents/{torrent_id}")
async def delete_torrent(torrent_id: int, from_alldebrid: bool = True):
    try:
        await manager.delete_torrent(torrent_id, delete_from_ad=from_alldebrid)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/torrents/{torrent_id}/retry")
async def retry_torrent(torrent_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE torrents SET status='pending', error_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (torrent_id,)
        )
        await db.commit()
    return {"ok": True}


# ─── Stats ───────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT status, COUNT(*) as count FROM torrents GROUP BY status
        """)
        rows = await cur.fetchall()
        by_status = {r["status"]: r["count"] for r in rows}

        cur2 = await db.execute(
            "SELECT SUM(size_bytes) as total FROM torrents WHERE status='completed'"
        )
        size_row = await cur2.fetchone()
        total_size = size_row["total"] or 0

        cur3 = await db.execute(
            "SELECT COUNT(*) as c FROM download_files WHERE blocked=1"
        )
        blocked = (await cur3.fetchone())["c"]

        return {
            "by_status": by_status,
            "total_completed_bytes": total_size,
            "total_blocked_files": blocked,
        }


# ─── Events ──────────────────────────────────────────────────────────────────

@router.get("/events")
async def get_events(limit: int = Query(100, le=500)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT e.*, t.name as torrent_name FROM events e
               LEFT JOIN torrents t ON e.torrent_id = t.id
               ORDER BY e.created_at DESC LIMIT ?""",
            (limit,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
