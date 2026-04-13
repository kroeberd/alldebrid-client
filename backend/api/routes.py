import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import aiosqlite

from core.config import AppSettings, get_settings, save_settings, apply_settings
from services.notifications import NotificationService
from services.alldebrid import AllDebridService
from services.jdownloader import myjd_list_devices
from services.manager import manager
from db.database import DB_PATH

router = APIRouter()
logger = logging.getLogger("alldebrid.api")


# ─── Settings ─────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings_ep():
    return get_settings().model_dump()


@router.put("/settings")
async def update_settings(new: AppSettings):
    save_settings(new)
    apply_settings(new)
    manager.reset_services()
    return {"ok": True}


@router.post("/settings/pause")
async def pause_processing():
    current = get_settings().model_copy(update={"paused": True})
    save_settings(current)
    apply_settings(current)
    return {"ok": True, "paused": True}


@router.post("/settings/resume")
async def resume_processing():
    current = get_settings().model_copy(update={"paused": False})
    save_settings(current)
    apply_settings(current)
    return {"ok": True, "paused": False}


@router.post("/settings/test-discord")
async def test_discord():
    cfg = get_settings()
    if not cfg.discord_webhook_url:
        raise HTTPException(400, "No Discord webhook URL configured")
    svc = NotificationService(cfg.discord_webhook_url)
    ok = await svc.test()
    if not ok:
        raise HTTPException(502, "Discord test failed — check webhook URL")
    return {"ok": True}


@router.post("/settings/test-alldebrid")
async def test_alldebrid():
    cfg = get_settings()
    if not cfg.alldebrid_api_key:
        raise HTTPException(400, "No API key configured")
    try:
        svc = AllDebridService(cfg.alldebrid_api_key, cfg.alldebrid_agent)
        user = await svc.get_user()
        await svc.close()
        u = user.get("user", user)
        return {"ok": True, "username": u.get("username", ""), "isPremium": u.get("isPremium", False)}
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/settings/test-jdownloader")
async def test_jdownloader():
    cfg = get_settings()
    if not cfg.jdownloader_email:
        raise HTTPException(400, "MyJDownloader email not configured")
    try:
        result = await manager.test_jdownloader()
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(502, str(e))


# ─── Torrents ─────────────────────────────────────────────────────────────────

@router.get("/torrents")
async def list_torrents(
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where = "WHERE t.status = ?" if status else ""
        params = [status] if status else []
        cur = await db.execute(
            f"""SELECT t.*,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id) as file_count,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id AND blocked=1) as blocked_count
                FROM torrents t {where}
                ORDER BY t.created_at DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        rows = await cur.fetchall()
        cur2 = await db.execute(f"SELECT COUNT(*) FROM torrents t {where}", params)
        total = (await cur2.fetchone())[0]
        return {"items": [dict(r) for r in rows], "total": total}

@router.post("/settings/jd-devices")
async def jd_list_devices():
    """Login to MyJDownloader and return available devices for the UI picker."""
    cfg = get_settings()
    if not cfg.jdownloader_email:
        raise HTTPException(400, "MyJDownloader email not configured")
    if not cfg.jdownloader_password:
        raise HTTPException(400, "MyJDownloader password not configured")
    try:
        devices = await myjd_list_devices(cfg.jdownloader_email, cfg.jdownloader_password)
        return {"devices": devices}
    except Exception as e:
        raise HTTPException(502, str(e))



@router.get("/torrents/{torrent_id}")
async def get_torrent(torrent_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        files = [dict(f) for f in await (await db.execute(
            "SELECT * FROM download_files WHERE torrent_id=?", (torrent_id,)
        )).fetchall()]
        events = [dict(e) for e in await (await db.execute(
            "SELECT * FROM events WHERE torrent_id=? ORDER BY created_at DESC LIMIT 100",
            (torrent_id,)
        )).fetchall()]
        return {**dict(row), "files": files, "events": events}


class MagnetRequest(BaseModel):
    magnet: str


@router.post("/torrents/add-magnet")
async def add_magnet(req: MagnetRequest):
    if not get_settings().alldebrid_api_key:
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
    if not get_settings().alldebrid_api_key:
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
            "UPDATE torrents SET status='pending',error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (torrent_id,),
        )
        await db.commit()
    return {"ok": True}


# ─── Stats ────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT status, COUNT(*) as count FROM torrents GROUP BY status")
        by_status = {r["status"]: r["count"] for r in await cur.fetchall()}
        size_row = await (await db.execute(
            "SELECT SUM(size_bytes) as total FROM torrents WHERE status='completed'"
        )).fetchone()
        blocked = (await (await db.execute(
            "SELECT COUNT(*) as c FROM download_files WHERE blocked=1"
        )).fetchone())["c"]
        active = (await (await db.execute(
            "SELECT COUNT(*) as c FROM torrents WHERE status IN ('downloading','processing','uploading')"
        )).fetchone())["c"]
        return {
            "by_status": by_status,
            "total_completed_bytes": size_row["total"] or 0,
            "total_blocked_files": blocked,
            "active_downloads": active,
            "paused": bool(get_settings().paused),
        }


# ─── Events ───────────────────────────────────────────────────────────────────


@router.post("/torrents/{torrent_id}/retry")
async def retry_torrent(torrent_id: int):
    """Re-trigger download for a torrent that is ready on AllDebrid."""
    import aiosqlite
    from db.database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Torrent not found")
    row = dict(row)
    if not row.get("alldebrid_id"):
        raise HTTPException(400, "No AllDebrid ID — cannot retry")
    import asyncio
    asyncio.create_task(
        manager._start_download(torrent_id, row["alldebrid_id"], row["name"])
    )
    return {"queued": True, "name": row["name"]}

@router.get("/events")
async def get_events(limit: int = Query(100, le=500)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT e.*, t.name as torrent_name FROM events e
               LEFT JOIN torrents t ON e.torrent_id = t.id
               ORDER BY e.created_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]
