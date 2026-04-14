import logging
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import aiosqlite

from core.config import AppSettings, get_settings, save_settings, apply_settings
from services.notifications import NotificationService
from services.alldebrid import AllDebridService
from services.manager_v2 import manager
from db.database import DB_PATH

router = APIRouter()
logger = logging.getLogger("alldebrid.api")
CHANGELOG_PATH = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


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


@router.post("/settings/test-aria2")
async def test_aria2():
    try:
        result = await manager.test_aria2()
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
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT alldebrid_id, name, provider_status FROM torrents WHERE id=?", (torrent_id,)
        )).fetchone()
        if not row:
            raise HTTPException(404, "Torrent not found")
        await db.execute(
            "UPDATE torrents SET status='pending', error_message=NULL, polling_failures=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (torrent_id,),
        )
        await db.execute(
            "DELETE FROM download_files WHERE torrent_id=?", (torrent_id,)
        )
        await db.commit()

    # If AllDebrid already has the files ready, kick off the download immediately
    # rather than waiting for the next sync_alldebrid_status cycle.
    if row["provider_status"] == "ready" and row["alldebrid_id"]:
        import asyncio
        asyncio.create_task(manager._start_download(torrent_id, str(row["alldebrid_id"]), str(row["name"] or "")))

    return {"ok": True}


@router.post("/torrents/{torrent_id}/pause")
async def pause_torrent(torrent_id: int):
    try:
        await manager.pause_torrent(torrent_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/torrents/{torrent_id}/resume")
async def resume_torrent(torrent_id: int):
    try:
        await manager.resume_torrent(torrent_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


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
            "SELECT COUNT(*) as c FROM torrents WHERE status IN ('downloading','processing','uploading','paused')"
        )).fetchone())["c"]
        queued = (await (await db.execute(
            "SELECT COUNT(*) as c FROM torrents WHERE status='queued'"
        )).fetchone())["c"]
        finished = (await (await db.execute(
            "SELECT COUNT(*) as c FROM events WHERE message='Finished'"
        )).fetchone())["c"]
        last_day_completed = (await (await db.execute(
            "SELECT COUNT(*) as c FROM torrents WHERE completed_at >= datetime('now', '-1 day')"
        )).fetchone())["c"]
        return {
            "by_status": by_status,
            "total_completed_bytes": size_row["total"] or 0,
            "total_blocked_files": blocked,
            "active_downloads": active,
            "queued_downloads": queued,
            "finished_events": finished,
            "completed_last_24h": last_day_completed,
            "paused": bool(get_settings().paused),
        }


@router.get("/stats/detail")
async def get_stats_detail():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        torrent_status_rows = await (await db.execute(
            "SELECT status, COUNT(*) as count FROM torrents GROUP BY status"
        )).fetchall()
        file_status_rows = await (await db.execute(
            "SELECT status, COUNT(*) as count, COALESCE(SUM(size_bytes), 0) as size FROM download_files GROUP BY status"
        )).fetchall()
        totals = await (await db.execute(
            """SELECT
                   COUNT(*) as torrent_total,
                   COALESCE(SUM(size_bytes), 0) as torrent_size_total,
                   COALESCE(SUM(CASE WHEN completed_at IS NOT NULL THEN size_bytes ELSE 0 END), 0) as completed_size,
                   COALESCE(SUM(CASE WHEN status='partial' THEN 1 ELSE 0 END), 0) as partial_total
               FROM torrents"""
        )).fetchone()
        event_levels = await (await db.execute(
            "SELECT level, COUNT(*) as count FROM events GROUP BY level"
        )).fetchall()
        latest_events = await (await db.execute(
            """SELECT e.level, e.message, e.created_at, t.name as torrent_name
               FROM events e
               LEFT JOIN torrents t ON e.torrent_id = t.id
               ORDER BY e.created_at DESC LIMIT 8"""
        )).fetchall()

    return {
        "torrent_status": {row["status"]: row["count"] for row in torrent_status_rows},
        "file_status": {
            row["status"]: {"count": row["count"], "size_bytes": row["size"]}
            for row in file_status_rows
        },
        "totals": dict(totals),
        "event_levels": {row["level"]: row["count"] for row in event_levels},
        "latest_events": [dict(row) for row in latest_events],
    }


@router.get("/meta/changelog")
async def get_changelog():
    if not CHANGELOG_PATH.exists():
        raise HTTPException(404, "CHANGELOG.md not found")
    return {"content": CHANGELOG_PATH.read_text(encoding="utf-8", errors="replace")}


# ─── Events ───────────────────────────────────────────────────────────────────


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
