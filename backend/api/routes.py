import asyncio
import logging
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, Query
from pydantic import BaseModel
import aiosqlite

from core.config import AppSettings, get_settings, save_settings, apply_settings
from services.notifications import NotificationService
from services.alldebrid import AllDebridService
from services.manager_v2 import manager
from db.database import DB_PATH

router = APIRouter()
logger = logging.getLogger("alldebrid.api")
CHANGELOG_PATH = next(
    (p for p in [
        Path(__file__).resolve().parents[2] / "CHANGELOG.md",
        Path("/app/CHANGELOG.md"),
        Path(__file__).resolve().parent / "CHANGELOG.md",
    ] if p.exists()),
    Path("/app/CHANGELOG.md"),
)


# ─── Settings ─────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings_ep():
    import os
    data = get_settings().model_dump()
    # If container runs with DB_TYPE=postgres_internal, inform the UI
    env_db_type = os.getenv("DB_TYPE", "").strip()
    if env_db_type == "postgres_internal":
        data["db_type"] = "postgres_internal"
        data["_db_type_locked"] = True   # UI should show this as read-only
    return data


@router.put("/settings")
async def update_settings(new: AppSettings):
    import os
    # DB_TYPE=postgres_internal comes from docker-compose env — do not let UI override it
    env_db_type = os.getenv("DB_TYPE", "").strip()
    if env_db_type == "postgres_internal":
        new = new.model_copy(update={"db_type": "postgres"})
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
        raise HTTPException(400, "No Discord webhook configured")
    svc = NotificationService(cfg.discord_webhook_url)
    ok = await svc.test()
    if not ok:
        raise HTTPException(502, "Discord test failed — check webhook URL")
    return {"ok": True}


@router.post("/settings/upload-avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    """
    Saves the avatar image to /app/config/avatar.<ext> and returns the
    public URL (http://<host>/api/avatar) that Discord can fetch.
    Discord requires an actual HTTPS/HTTP URL — data URIs are rejected.
    Max 8 MB (Discord limit); we enforce 4 MB.
    """
    import os

    ALLOWED_TYPES = {
        "image/png":  "png",
        "image/jpeg": "jpg",
        "image/gif":  "gif",
        "image/webp": "webp",
    }
    MAX_BYTES = 4 * 1024 * 1024  # 4 MB

    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(400,
            f"Unsupported type '{content_type}'. Allowed: PNG, JPG, GIF, WebP")

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413,
            f"File too large ({len(data)//1024} KB). Limit: 4 MB")

    ext = ALLOWED_TYPES[content_type]
    config_dir = Path(os.getenv("CONFIG_PATH", "/app/config/config.json")).parent
    config_dir.mkdir(parents=True, exist_ok=True)

    # Remove old avatar files
    for old in config_dir.glob("avatar.*"):
        old.unlink(missing_ok=True)

    avatar_path = config_dir / f"avatar.{ext}"
    avatar_path.write_bytes(data)

    # Build public URL from the incoming request
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost:8080"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    public_url = f"{scheme}://{host}/api/avatar"

    return {
        "ok":          True,
        "url":         public_url,
        "size_bytes":  len(data),
        "content_type": content_type,
    }


@router.get("/avatar")
async def serve_avatar():
    """Serves the uploaded avatar image so Discord can fetch it via HTTP."""
    import os
    config_dir = Path(os.getenv("CONFIG_PATH", "/app/config/config.json")).parent
    for ext in ("png", "jpg", "gif", "webp"):
        p = config_dir / f"avatar.{ext}"
        if p.exists():
            media = {"png":"image/png","jpg":"image/jpeg","gif":"image/gif","webp":"image/webp"}[ext]
            return FileResponse(str(p), media_type=media,
                                headers={"Cache-Control": "public, max-age=3600"})
    raise HTTPException(404, "No avatar uploaded")


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
        return {
            "ok":           True,
            "username":     u.get("username", ""),
            "isPremium":    u.get("isPremium", False),
            "premiumUntil": u.get("premiumUntil", u.get("premium_until", 0)),
        }
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/settings/test-aria2")
async def test_aria2():
    try:
        result = await manager.test_aria2()
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/settings/test-postgres")
async def test_postgres():
    """Tests the PostgreSQL connection with the current settings."""
    cfg = get_settings()
    if getattr(cfg, "db_type", "sqlite") not in ("postgres",):
        raise HTTPException(400, "PostgreSQL is not configured as the database type")
    try:
        import asyncpg  # type: ignore
    except ImportError:
        raise HTTPException(500, "asyncpg is not installed — run: pip install asyncpg")
    try:
        ssl_val = "require" if cfg.postgres_ssl else "disable"
        dsn = (
            f"postgresql://{cfg.postgres_user}:{cfg.postgres_password}"
            f"@{cfg.postgres_host}:{cfg.postgres_port}/{cfg.postgres_db}"
            f"?sslmode={ssl_val}"
        )
        conn = await asyncpg.connect(dsn, timeout=10)
        version = await conn.fetchval("SELECT version()")
        await conn.close()
        short_version = version.split(",")[0] if version else "unbekannt"
        return {
            "ok": True,
            "host": cfg.postgres_host,
            "port": cfg.postgres_port,
            "database": cfg.postgres_db,
            "user": cfg.postgres_user,
            "version": short_version,
        }
    except Exception as e:
        raise HTTPException(502, f"PostgreSQL connection failed: {e}")


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
        await db.execute("DELETE FROM download_files WHERE torrent_id=?", (torrent_id,))
        await db.commit()

    if row["provider_status"] == "ready" and row["alldebrid_id"]:
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

        # Completed size
        size_row = await (await db.execute(
            "SELECT SUM(size_bytes) as total FROM torrents WHERE status='completed'"
        )).fetchone()

        # File counters
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

        error_count = (await (await db.execute(
            "SELECT COUNT(*) as c FROM torrents WHERE status='error'"
        )).fetchone())["c"]

        completed_count = (await (await db.execute(
            "SELECT COUNT(*) as c FROM torrents WHERE status='completed'"
        )).fetchone())["c"]

        total_count = (await (await db.execute(
            "SELECT COUNT(*) as c FROM torrents"
        )).fetchone())["c"]

        last_day_completed = (await (await db.execute(
            "SELECT COUNT(*) as c FROM torrents WHERE completed_at >= datetime('now', '-1 day')"
        )).fetchone())["c"]

        last_week_completed = (await (await db.execute(
            "SELECT COUNT(*) as c FROM torrents WHERE completed_at >= datetime('now', '-7 days')"
        )).fetchone())["c"]

        # Avg download duration (for torrents with completed_at and created_at)
        avg_duration_row = await (await db.execute(
            """SELECT AVG(
                   CAST((julianday(completed_at) - julianday(created_at)) * 86400 AS INTEGER)
               ) as avg_secs
               FROM torrents
               WHERE completed_at IS NOT NULL AND created_at IS NOT NULL"""
        )).fetchone()
        avg_duration_seconds = int(avg_duration_row["avg_secs"] or 0)

        # Avg torrent size (completed torrents)
        avg_size_row = await (await db.execute(
            "SELECT AVG(size_bytes) as avg_bytes FROM torrents WHERE status='completed' AND size_bytes > 0"
        )).fetchone()
        avg_size_bytes = int(avg_size_row["avg_bytes"] or 0)

        # Erfolgsrate
        terminal = completed_count + error_count
        success_rate = round(completed_count / terminal * 100, 1) if terminal > 0 else None

        # db_type: after fallback, get_settings() reflects the actually active backend
        import os as _os
        env_db_type = _os.getenv("DB_TYPE", "").strip()
        active_db_type = getattr(get_settings(), "db_type", "sqlite")
        # If env said postgres_internal but active is sqlite → fallback occurred
        # postgres_internal mode is no longer supported in UI — treat as postgres
        if active_db_type == "sqlite" and env_db_type in ("postgres", "postgres_internal"):
            db_type_display = "sqlite_fallback"
        else:
            db_type_display = active_db_type
        return {
            "by_status": by_status,
            "total_completed_bytes": size_row["total"] or 0,
            "db_type": db_type_display,
            "total_blocked_files": blocked,
            "active_downloads": active,
            "queued_downloads": queued,
            "finished_events": finished,
            "completed_last_24h": last_day_completed,
            "completed_last_7d": last_week_completed,
            "error_count": error_count,
            "completed_count": completed_count,
            "total_count": total_count,
            "success_rate_pct": success_rate,
            "avg_download_duration_seconds": avg_duration_seconds,
            "avg_torrent_size_bytes": avg_size_bytes,
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
                   COALESCE(SUM(CASE WHEN status='completed' THEN size_bytes ELSE 0 END), 0) as completed_size,
                   COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), 0) as error_total,
                   COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0) as completed_total
               FROM torrents"""
        )).fetchone()

        # Daily trend data (last 14 days)
        daily_rows = await (await db.execute(
            """SELECT
                   date(completed_at) as day,
                   COUNT(*) as count,
                   COALESCE(SUM(size_bytes), 0) as size_bytes
               FROM torrents
               WHERE completed_at IS NOT NULL
                 AND completed_at >= datetime('now', '-14 days')
               GROUP BY date(completed_at)
               ORDER BY day ASC"""
        )).fetchall()

        event_levels = await (await db.execute(
            "SELECT level, COUNT(*) as count FROM events GROUP BY level"
        )).fetchall()

        latest_events = await (await db.execute(
            """SELECT e.level, e.message, e.created_at, t.name as torrent_name
               FROM events e
               LEFT JOIN torrents t ON e.torrent_id = t.id
               ORDER BY e.created_at DESC LIMIT 10"""
        )).fetchall()

        # Top-Quellen
        source_rows = await (await db.execute(
            "SELECT source, COUNT(*) as count FROM torrents GROUP BY source ORDER BY count DESC LIMIT 10"
        )).fetchall()

    totals_dict = dict(totals)
    terminal = totals_dict.get("completed_total", 0) + totals_dict.get("error_total", 0)
    totals_dict["success_rate_pct"] = (
        round(totals_dict["completed_total"] / terminal * 100, 1)
        if terminal > 0 else None
    )

    return {
        "torrent_status": {row["status"]: row["count"] for row in torrent_status_rows},
        "file_status": {
            row["status"]: {"count": row["count"], "size_bytes": row["size"]}
            for row in file_status_rows
        },
        "totals": totals_dict,
        "daily_completions": [dict(r) for r in daily_rows],
        "event_levels": {row["level"]: row["count"] for row in event_levels},
        "latest_events": [dict(row) for row in latest_events],
        "sources": {row["source"]: row["count"] for row in source_rows},
    }


# ─── Migration ────────────────────────────────────────────────────────────────

class MigrationRequest(BaseModel):
    direction: str   # "sqlite_to_postgres" | "postgres_to_sqlite"
    force: bool = False
    dry_run: bool = False


@router.post("/admin/migrate")
async def run_migration(req: MigrationRequest):
    """
    Runs a database migration.

    - direction: "sqlite_to_postgres" oder "postgres_to_sqlite"
    - dry_run: Nur validieren, keine Daten schreiben
    - force: overwrite existing data in target (use with caution)
    """
    from db.migration import migrate_sqlite_to_postgres, migrate_postgres_to_sqlite
    from db.database import DB_PATH as _DB_PATH

    cfg = get_settings()
    if not hasattr(cfg, "db_type"):
        raise HTTPException(400, "PostgreSQL configuration not available")

    # DSN aufbauen
    ssl = "require" if getattr(cfg, "postgres_ssl", False) else "disable"
    pg_dsn = (
        f"postgresql://{cfg.postgres_user}:{cfg.postgres_password}"
        f"@{cfg.postgres_host}:{cfg.postgres_port}/{cfg.postgres_db}"
        f"?sslmode={ssl}"
    )

    if req.direction == "sqlite_to_postgres":
        result = await migrate_sqlite_to_postgres(
            _DB_PATH, pg_dsn, force=req.force, dry_run=req.dry_run
        )
    elif req.direction == "postgres_to_sqlite":
        result = await migrate_postgres_to_sqlite(
            pg_dsn, _DB_PATH, force=req.force, dry_run=req.dry_run
        )
    else:
        raise HTTPException(400, f"Unknown direction: {req.direction!r}")

    if not result.success:
        raise HTTPException(500, result.error or "Migration failed")

    return {
        "ok": result.success,
        "direction": result.direction,
        "tables_migrated": result.tables_migrated,
        "warnings": result.warnings,
        "summary": result.summary(),
        "dry_run": req.dry_run,
    }


@router.get("/admin/migrate/validate")
async def validate_migration(direction: str = "sqlite_to_postgres"):
    """Validates a migration (dry_run=True) without writing any data."""
    from db.migration import MigrationRequest as _MR
    fake_req = MigrationRequest(direction=direction, force=False, dry_run=True)
    return await run_migration(fake_req)


# ─── Meta ─────────────────────────────────────────────────────────────────────

@router.get("/meta/changelog")
async def get_changelog():
    if not CHANGELOG_PATH.exists():
        raise HTTPException(404, "CHANGELOG.md nicht gefunden")
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
