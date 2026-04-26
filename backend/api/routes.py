"""
REST API routes for AllDebrid-Client.

Conventions:
- All DB access uses get_db() (supports both SQLite and PostgreSQL).
- Pydantic models for request bodies are defined inline.
- No inline `import` statements — all imports are at module level.
"""
import asyncio
import ipaddress
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.config import (
    AppSettings,
    apply_settings,
    get_settings,
    load_settings,
    save_settings,
)
from core.config_validator import validate_and_sanitise
from core.version import read_version
from db.database import DB_PATH, _is_postgres, get_db
from services.manager_v2 import manager

logger = logging.getLogger("alldebrid.routes")
router = APIRouter()


def _public_base_url(request: Request) -> str:
    """Return the externally reachable base URL for generated links."""
    configured = (os.getenv("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if configured:
        return configured
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost:8080"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    return f"{scheme}://{host}".rstrip("/")


def _avatar_reachability_warning(public_url: str) -> str:
    """Return a warning when Discord likely cannot fetch the generated avatar URL."""
    if _is_public_url(public_url):
        return ""
    return (
        "Avatar uploaded, but the generated URL is private or loopback and may not be reachable by Discord. "
        "Set PUBLIC_BASE_URL to a public HTTP(S) address or use a public avatar URL directly."
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_public_url(url: str) -> bool:
    """Returns True when url is reachable from outside the container."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host or host in ("localhost", "127.0.0.1", "::1"):
            return False
        addr = ipaddress.ip_address(host)
        return not (addr.is_loopback or addr.is_private or addr.is_link_local)
    except ValueError:
        # hostname — not an IP, assume public
        return True


# ── Settings ───────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings_ep():
    data = get_settings().model_dump()
    env_db_type = os.getenv("DB_TYPE", "").strip()
    if env_db_type == "postgres_internal":
        data["db_type"] = "postgres_internal"
        data["_db_type_locked"] = True
    return data


@router.get("/version")
async def get_version_ep():
    return {"version": read_version()}


@router.put("/settings")
async def update_settings(new: AppSettings):
    previous = get_settings()
    env_db_type = os.getenv("DB_TYPE", "").strip()
    if env_db_type == "postgres_internal":
        new = new.model_copy(update={"db_type": "postgres"})
    clean = validate_and_sanitise(new)
    save_settings(clean)
    apply_settings(clean)
    manager.reset_services()
    if getattr(clean, "aria2_url", "").strip():
        try:
            await manager.apply_aria2_memory_tuning()
        except Exception as exc:
            logger.warning("Could not apply aria2 memory settings immediately: %s", exc)
    if getattr(previous, "flexget_enabled", False) != getattr(clean, "flexget_enabled", False):
        from services.flexget import reset_runtime_state
        reset_runtime_state()
    return {"ok": True}


# ── Avatar ─────────────────────────────────────────────────────────────────────

@router.post("/settings/upload-avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    """
    Saves the avatar image to CONFIG_DIR/avatar.<ext> and returns the
    public HTTP URL so Discord can fetch it.
    Discord requires a real HTTPS/HTTP URL — data URIs are rejected.
    """
    ALLOWED = {"image/png": "png", "image/jpeg": "jpg",
                "image/gif": "gif", "image/webp": "webp"}
    MAX_BYTES = 4 * 1024 * 1024

    ct = (file.content_type or "").lower().split(";")[0].strip()
    if ct not in ALLOWED:
        raise HTTPException(400, f"Unsupported type '{ct}'. Allowed: PNG, JPG, GIF, WebP")

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"File too large ({len(data)//1024} KB). Limit: 4 MB")

    ext = ALLOWED[ct]
    config_dir = Path(os.getenv("CONFIG_PATH", "/app/config/config.json")).parent
    config_dir.mkdir(parents=True, exist_ok=True)

    for old in config_dir.glob("avatar.*"):
        old.unlink(missing_ok=True)
    (config_dir / f"avatar.{ext}").write_bytes(data)

    public_url = f"{_public_base_url(request)}/api/avatar"
    warning = _avatar_reachability_warning(public_url)

    if warning:
        logger.warning(
            "Avatar uploaded, but URL %s may not be reachable by Discord",
            public_url,
        )

    payload = {"ok": True, "url": public_url, "size_bytes": len(data), "content_type": ct}
    if warning:
        payload["warning"] = warning
    return payload


@router.get("/avatar")
async def serve_avatar():
    """Serves the stored avatar image for Discord to fetch."""
    config_dir = Path(os.getenv("CONFIG_PATH", "/app/config/config.json")).parent
    media_types = {"png": "image/png", "jpg": "image/jpeg",
                   "gif": "image/gif", "webp": "image/webp"}
    for ext, media_type in media_types.items():
        p = config_dir / f"avatar.{ext}"
        if p.exists():
            return FileResponse(str(p), media_type=media_type,
                                headers={"Cache-Control": "public, max-age=3600"})
    raise HTTPException(404, "No avatar uploaded")


# ── Connection tests ───────────────────────────────────────────────────────────

@router.post("/settings/test-discord")
async def test_discord():
    cfg = get_settings()
    if not cfg.discord_webhook_url:
        raise HTTPException(400, "No Discord webhook configured")
    from services.notifications import NotificationService
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
    from services.alldebrid import AllDebridService
    svc = AllDebridService(cfg.alldebrid_api_key, cfg.alldebrid_agent)
    try:
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


@router.post("/settings/aria2-housekeeping")
async def run_aria2_housekeeping_ep():
    try:
        return await manager.run_aria2_housekeeping()
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/settings/test-postgres")
async def test_postgres():
    """Tests the PostgreSQL connection with current settings."""
    cfg = get_settings()
    if getattr(cfg, "db_type", "sqlite") != "postgres":
        raise HTTPException(400, "PostgreSQL is not configured as the database type")
    try:
        import asyncpg
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
        return {
            "ok":       True,
            "host":     cfg.postgres_host,
            "port":     cfg.postgres_port,
            "database": cfg.postgres_db,
            "user":     cfg.postgres_user,
            "version":  (version or "").split(",")[0],
        }
    except Exception as e:
        raise HTTPException(502, f"PostgreSQL connection failed: {e}")


@router.post("/settings/test-sonarr")
async def test_sonarr():
    cfg = get_settings()
    if not cfg.sonarr_enabled or not cfg.sonarr_url:
        raise HTTPException(400, "Sonarr not configured")
    from services.integrations import test_connection
    result = await test_connection(cfg.sonarr_url, cfg.sonarr_api_key)
    if not result["ok"]:
        raise HTTPException(502, result.get("error", "Connection failed"))
    return result


@router.post("/settings/test-radarr")
async def test_radarr():
    cfg = get_settings()
    if not cfg.radarr_enabled or not cfg.radarr_url:
        raise HTTPException(400, "Radarr not configured")
    from services.integrations import test_connection
    result = await test_connection(cfg.radarr_url, cfg.radarr_api_key)
    if not result["ok"]:
        raise HTTPException(502, result.get("error", "Connection failed"))
    return result


# ── Torrents ───────────────────────────────────────────────────────────────────

@router.get("/torrents")
async def list_torrents(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(0, ge=0, le=5000),
    offset: int = 0,
):
    async with get_db() as db:
        clauses = []
        params = []

        if status:
            clauses.append("t.status = ?")
            params.append(status)

        if search:
            clauses.append(
                """(
                    LOWER(COALESCE(t.name, '')) LIKE ?
                    OR LOWER(COALESCE(t.hash, '')) LIKE ?
                    OR LOWER(COALESCE(t.source, '')) LIKE ?
                    OR LOWER(COALESCE(t.label, '')) LIKE ?
                )"""
            )
            needle = f"%{search.strip().lower()}%"
            params.extend([needle, needle, needle, needle])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""SELECT t.*,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id) as file_count,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id AND blocked=1) as blocked_count
                FROM torrents t {where}
                ORDER BY t.created_at DESC"""
        query_params = list(params)
        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            query_params.extend([limit, offset])

        rows = await db.fetchall(query, query_params)
        total_row = await db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
        )
        total = total_row["cnt"] if total_row else 0
        return {"items": rows, "total": total}


@router.post("/torrents/add-magnet")
async def add_magnet(body: dict):
    magnet = (body.get("magnet") or "").strip()
    if not magnet:
        raise HTTPException(400, "magnet is required")
    try:
        row = await manager.add_magnet_direct(magnet, source="manual")
        return row
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/torrents/import-existing")
async def import_existing():
    results = await manager.import_existing_magnets()
    return {"imported": len(results), "items": results}


@router.get("/torrents/{torrent_id}")
async def get_torrent(torrent_id: int):
    async with get_db() as db:
        row = await db.fetchone("SELECT * FROM torrents WHERE id=?", (torrent_id,))
        if not row:
            raise HTTPException(404, "Not found")
        files  = await db.fetchall("SELECT * FROM download_files WHERE torrent_id=? ORDER BY id", (torrent_id,))
        events = await db.fetchall("SELECT * FROM events WHERE torrent_id=? ORDER BY created_at DESC LIMIT 50", (torrent_id,))
        return {**row, "files": files, "events": events}


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
    async with get_db() as db:
        row = await db.fetchone("SELECT * FROM torrents WHERE id=?", (torrent_id,))
        if not row:
            raise HTTPException(404, "Torrent not found")
        if not row["magnet"] and not row["alldebrid_id"]:
            raise HTTPException(400, "No magnet or AllDebrid ID — cannot retry")
        new_status = "ready" if row["alldebrid_id"] else "uploading"
        await db.execute(
            """UPDATE torrents
               SET status=?, error_message=NULL,
                   polling_failures=0, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (new_status, torrent_id),
        )
        await db.execute(
            """UPDATE download_files
               SET status='pending', download_id=NULL, retry_count=0,
                   updated_at=CURRENT_TIMESTAMP
               WHERE torrent_id=? AND status='error'""",
            (torrent_id,),
        )
        await db.execute(
            "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
            (torrent_id, f"Manual retry — resetting to {new_status}"),
        )
        await db.commit()
    return {"ok": True, "new_status": new_status}


@router.post("/torrents/{torrent_id}/pause")
async def pause_torrent(torrent_id: int):
    try:
        await manager.pause_torrent(torrent_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/torrents/{torrent_id}/resume")
async def resume_torrent(torrent_id: int):
    try:
        await manager.resume_torrent(torrent_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


class LabelUpdate(BaseModel):
    label: str = ""
    priority: int = 0


@router.put("/torrents/{torrent_id}/label")
async def set_torrent_label(torrent_id: int, body: LabelUpdate):
    async with get_db() as db:
        await db.execute(
            "UPDATE torrents SET label=?, priority=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (body.label.strip(), body.priority, torrent_id),
        )
        await db.commit()
    return {"ok": True}


class BulkAction(BaseModel):
    ids: list
    action: str  # "delete" | "retry" | "remove_label"


@router.post("/torrents/bulk")
async def bulk_action(body: BulkAction):
    if not body.ids:
        raise HTTPException(400, "No IDs provided")
    ok = failed = 0
    for tid in body.ids:
        try:
            tid = int(tid)
            if body.action == "delete":
                await manager.delete_torrent(tid, delete_from_ad=True)
            elif body.action == "retry":
                async with get_db() as db:
                    await db.execute(
                        """UPDATE torrents
                           SET status='uploading', error_message=NULL,
                               polling_failures=0, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (tid,),
                    )
                    await db.commit()
            elif body.action == "remove_label":
                async with get_db() as db:
                    await db.execute(
                        "UPDATE torrents SET label='', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (tid,),
                    )
                    await db.commit()
            ok += 1
        except Exception:
            failed += 1
    return {"ok": ok, "failed": failed}


# ── Events ─────────────────────────────────────────────────────────────────────

@router.get("/events")
async def get_events(limit: int = Query(200, le=500)):
    async with get_db() as db:
        return await db.fetchall(
            """SELECT e.*, t.name AS torrent_name
               FROM events e
               LEFT JOIN torrents t ON t.id = e.torrent_id
               ORDER BY e.created_at DESC LIMIT ?""",
            (limit,),
        )


# ── Statistics ─────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats():
    async with get_db() as db:
        by_status_rows = await db.fetchall("SELECT status, COUNT(*) as count FROM torrents GROUP BY status")
        by_status = {r["status"]: r["count"] for r in by_status_rows}

        def _v(row, key="v"): return row[key] if row else 0
        def _c(row): return row["c"] if row else 0

        size_total      = _v(await db.fetchone("SELECT COALESCE(SUM(size_bytes),0) as v FROM torrents WHERE status='completed'"))
        blocked         = _c(await db.fetchone("SELECT COUNT(*) as c FROM download_files WHERE blocked=1"))
        active          = _c(await db.fetchone("SELECT COUNT(*) as c FROM torrents WHERE status IN ('downloading','processing','uploading','paused')"))
        queued          = _c(await db.fetchone("SELECT COUNT(*) as c FROM torrents WHERE status='queued'"))
        error_count     = _c(await db.fetchone("SELECT COUNT(*) as c FROM torrents WHERE status='error'"))
        completed_count = _c(await db.fetchone("SELECT COUNT(*) as c FROM torrents WHERE status='completed'"))
        last_24h        = _c(await db.fetchone("SELECT COUNT(*) as c FROM torrents WHERE completed_at >= datetime('now','-1 day')"))
        last_7d         = _c(await db.fetchone("SELECT COUNT(*) as c FROM torrents WHERE completed_at >= datetime('now','-7 days')"))
        avg_dur_row     = await db.fetchone(
            """SELECT AVG(CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)) as v
               FROM torrents WHERE completed_at IS NOT NULL AND created_at IS NOT NULL""")
        avg_duration    = int(_v(avg_dur_row) or 0)
        avg_size_row    = await db.fetchone("SELECT AVG(size_bytes) as v FROM torrents WHERE status='completed' AND size_bytes>0")
        avg_size        = int(_v(avg_size_row) or 0)

        terminal     = completed_count + error_count
        success_rate = round(completed_count / terminal * 100, 1) if terminal > 0 else None

        env_db  = os.getenv("DB_TYPE", "").strip()
        act_db  = getattr(get_settings(), "db_type", "sqlite")
        db_type = ("sqlite_fallback" if act_db == "sqlite" and env_db in ("postgres", "postgres_internal")
                   else act_db)

        return {
            "version":                      read_version(),
            "by_status":                    by_status,
            "total_completed_bytes":        size_total,
            "db_type":                      db_type,
            "total_blocked_files":          blocked,
            "active_downloads":             active,
            "queued_downloads":             queued,
            "error_count":                  error_count,
            "completed_count":              completed_count,
            "success_rate_pct":             success_rate,
            "completed_last_24h":           last_24h,
            "completed_last_7d":            last_7d,
            "avg_download_duration_seconds": avg_duration,
            "avg_torrent_size_bytes":       avg_size,
            "paused":                       bool(get_settings().paused),
        }


@router.get("/stats/detail")
async def get_stats_detail():
    async with get_db() as db:
        totals = await db.fetchone(
            "SELECT COUNT(*) as torrent_total, COALESCE(SUM(size_bytes),0) as torrent_size_total FROM torrents"
        ) or {}

        completed_count = (await db.fetchone("SELECT COUNT(*) as c FROM torrents WHERE status='completed'") or {}).get("c", 0)
        error_count     = (await db.fetchone("SELECT COUNT(*) as c FROM torrents WHERE status='error'") or {}).get("c", 0)
        terminal = completed_count + error_count
        totals["success_rate_pct"] = (
            round(completed_count / terminal * 100, 1) if terminal > 0 else None
        )

        torrent_status    = await db.fetchall("SELECT status, COUNT(*) as count FROM torrents GROUP BY status ORDER BY count DESC")
        file_status       = await db.fetchall(
            """SELECT status, COUNT(*) as count, COALESCE(SUM(size_bytes),0) as size_bytes
               FROM download_files GROUP BY status ORDER BY count DESC""")
        event_levels      = await db.fetchall("SELECT level, COUNT(*) as count FROM events GROUP BY level")
        latest_events     = await db.fetchall(
            """SELECT e.*, t.name AS torrent_name FROM events e
               LEFT JOIN torrents t ON t.id = e.torrent_id
               ORDER BY e.created_at DESC LIMIT 10""")
        daily_completions = await db.fetchall(
            """SELECT DATE(completed_at) as date, COUNT(*) as count
               FROM torrents WHERE completed_at >= datetime('now','-14 days')
               GROUP BY DATE(completed_at) ORDER BY date ASC""")
        sources = await db.fetchall(
            "SELECT source, COUNT(*) as count FROM torrents GROUP BY source ORDER BY count DESC")

        return {
            "totals":             totals,
            "torrent_status":     torrent_status,
            "file_status":        file_status,
            "event_levels":       event_levels,
            "latest_events":      latest_events,
            "daily_completions":  daily_completions,
            "sources":            sources,
        }


# ── Processing control ─────────────────────────────────────────────────────────

@router.post("/processing/pause")
async def pause_processing():
    cfg = get_settings()
    cfg = cfg.model_copy(update={"paused": True})
    save_settings(cfg)
    apply_settings(cfg)
    return {"ok": True, "paused": True}


@router.post("/processing/resume")
async def resume_processing():
    cfg = get_settings()
    cfg = cfg.model_copy(update={"paused": False})
    save_settings(cfg)
    apply_settings(cfg)
    return {"ok": True, "paused": False}


# ── Changelog ──────────────────────────────────────────────────────────────────

@router.get("/changelog")
async def get_changelog():
    for candidate in (
        Path("/app/CHANGELOG.md"),
        Path(__file__).resolve().parents[2] / "CHANGELOG.md",
    ):
        if candidate.exists():
            return {"content": candidate.read_text(encoding="utf-8")}
    return {"content": ""}


# ── Admin ──────────────────────────────────────────────────────────────────────

@router.post("/admin/migrate")
async def run_migration(req: dict):
    """Runs a database migration. direction: sqlite_to_postgres | postgres_to_sqlite"""
    direction = req.get("direction", "")
    dry_run   = bool(req.get("dry_run", False))
    force     = bool(req.get("force", False))

    if direction not in ("sqlite_to_postgres", "postgres_to_sqlite"):
        raise HTTPException(400, f"Unknown direction: {direction!r}")

    cfg = get_settings()
    try:
        ssl    = "require" if cfg.postgres_ssl else "disable"
        pg_dsn = (
            f"postgresql://{cfg.postgres_user}:{cfg.postgres_password}"
            f"@{cfg.postgres_host}:{cfg.postgres_port}/{cfg.postgres_db}"
            f"?sslmode={ssl}"
        )
    except Exception as e:
        raise HTTPException(500, f"PostgreSQL configuration not available: {e}")

    from db.migration import migrate_postgres_to_sqlite, migrate_sqlite_to_postgres
    try:
        if direction == "sqlite_to_postgres":
            result = await migrate_sqlite_to_postgres(DB_PATH, pg_dsn, force=force, dry_run=dry_run)
        else:
            result = await migrate_postgres_to_sqlite(pg_dsn, DB_PATH, force=force, dry_run=dry_run)
    except Exception as e:
        raise HTTPException(500, f"Migration failed: {e}")

    if not result.success:
        raise HTTPException(500, result.error or "Migration failed")

    return {
        "ok":             True,
        "tables_migrated": result.tables_migrated,
        "warnings":       result.warnings,
        "summary":        result.summary(),
    }


@router.get("/admin/migrate/validate")
async def validate_migration(direction: str = "sqlite_to_postgres"):
    return await run_migration({"direction": direction, "dry_run": True, "force": False})


@router.post("/admin/backup")
async def trigger_backup():
    from services.backup import run_backup
    result = await run_backup()
    return result


@router.get("/admin/backups")
async def list_backups():
    from services.backup import list_backups as _list
    return {"backups": _list()}


@router.post("/admin/database/backup")
async def trigger_database_backup():
    from services.db_maintenance import run_database_backup
    return await run_database_backup()


@router.get("/admin/database/backups")
async def list_database_backups():
    from services.db_maintenance import list_database_backups as _list
    return {"backups": _list()}


@router.post("/admin/database/wipe")
async def wipe_database_admin(body: dict | None = None):
    cfg = get_settings()
    if not getattr(cfg, "db_wipe_enabled", False):
        raise HTTPException(400, "Database wipe is disabled in settings")
    if not getattr(cfg, "paused", False):
        raise HTTPException(409, "Pause processing before wiping the database")
    if not (body or {}).get("confirm"):
        raise HTTPException(400, "Wipe confirmation required")

    backup_result = None
    if getattr(cfg, "db_backup_before_wipe", True):
        from services.db_maintenance import run_database_backup
        backup_result = await run_database_backup()

    from services.db_maintenance import wipe_database
    result = await wipe_database()
    manager.reset_services()
    return {**result, "backup": backup_result}



# ── FlexGet ────────────────────────────────────────────────────────────────────

@router.get("/flexget/tasks")
async def flexget_list_tasks():
    """List available FlexGet tasks."""
    cfg = get_settings()
    if not getattr(cfg, "flexget_enabled", False):
        return {"tasks": [], "enabled": False}
    from services.flexget import _client
    tasks = await _client().list_tasks()
    return {"tasks": tasks, "enabled": True}


@router.get("/flexget/running")
async def flexget_running():
    """Return which FlexGet tasks are currently executing (for UI indicator)."""
    if not getattr(get_settings(), "flexget_enabled", False):
        return {"running": []}
    from services.flexget import running_tasks
    return {"running": running_tasks()}


@router.post("/flexget/run/{task_name}")
async def flexget_run_single(task_name: str):
    """Run a single named FlexGet task immediately, with duplicate guard."""
    cfg = get_settings()
    if not getattr(cfg, "flexget_enabled", False):
        raise HTTPException(400, "FlexGet integration is not enabled")
    from services.flexget import run_flexget_tasks, is_task_running
    if is_task_running(task_name):
        raise HTTPException(409, f"Task '{task_name}' is already running")
    results = await run_flexget_tasks(tasks=[task_name], triggered_by="manual")
    r = results[0] if results else {"task": task_name, "status": "error", "error": "no result", "elapsed": 0}
    return {
        "ok": r.get("status") == "ok",
        "task": task_name,
        "status": r.get("status"),
        "elapsed": r.get("elapsed", 0),
        "first_error": r.get("error") if r.get("status") != "ok" else None,
    }


@router.post("/flexget/run")
async def flexget_run(body: dict = {}):
    """Trigger FlexGet task execution manually."""
    cfg = get_settings()
    if not getattr(cfg, "flexget_enabled", False):
        raise HTTPException(400, "FlexGet integration is not enabled")
    tasks = body.get("tasks") or None  # None = all tasks
    from services.flexget import run_flexget_tasks
    results = await run_flexget_tasks(tasks=tasks, triggered_by="manual")
    ok    = sum(1 for r in results if r.get("status") == "ok")
    errs  = len(results) - ok
    # Include first error detail for quick diagnosis in the UI
    first_error = next(
        (r.get("error") or str(r.get("result", "")) for r in results if r.get("status") != "ok"),
        None,
    )
    return {
        "ok": True,
        "tasks_total": len(results),
        "tasks_ok": ok,
        "tasks_error": errs,
        "first_error": first_error,
        "results": results,
    }


@router.get("/flexget/history")
async def flexget_history(limit: int = Query(50, le=200)):
    """Return recent FlexGet run history."""
    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT * FROM flexget_runs ORDER BY ran_at DESC LIMIT ?", (limit,)
        )
    return {"runs": rows}


# ── Statistics & Reporting ──────────────────────────────────────────────────────

# ── Jackett ────────────────────────────────────────────────────────────────────


@router.post("/settings/test-jackett")
async def test_jackett():
    from services.jackett import test_connection
    result = await test_connection()
    if not result["ok"]:
        raise HTTPException(502, result.get("error", "Jackett test failed"))
    return result


@router.get("/jackett/indexers")
async def jackett_indexers():
    from services.jackett import get_indexers
    return await get_indexers()


@router.post("/jackett/search")
async def jackett_search(body: dict):
    from services.jackett import search
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")
    category = int(body.get("category") or 0)
    tracker = (body.get("tracker") or "").strip()
    trackers = body.get("trackers") or []
    if tracker and tracker not in trackers:
        trackers = [tracker, *trackers]
    trackers = [str(t).strip() for t in trackers if str(t).strip()]
    limit = min(int(body.get("limit") or 100), 500)
    result = await search(query=query, category=category, trackers=trackers, limit=limit)
    hashes = sorted({str(item.get("hash") or "").strip().lower() for item in result.get("results", []) if str(item.get("hash") or "").strip()})
    if hashes:
        placeholders = ",".join("?" for _ in hashes)
        async with get_db() as db:
            rows = await db.fetchall(
                f"SELECT id, hash, status, name FROM torrents WHERE LOWER(hash) IN ({placeholders})",
                hashes,
            )
        existing_by_hash = {str(row["hash"]).strip().lower(): row for row in rows}
        for item in result.get("results", []):
            existing = existing_by_hash.get(str(item.get("hash") or "").strip().lower())
            item["already_added"] = bool(existing)
            item["existing_torrent_id"] = existing["id"] if existing else None
            item["existing_status"] = existing["status"] if existing else ""
    else:
        for item in result.get("results", []):
            item["already_added"] = False
            item["existing_torrent_id"] = None
            item["existing_status"] = ""
    return result


@router.post("/jackett/add")
async def jackett_add(body: dict):
    """
    Add a torrent found via Jackett to the download queue.
    Accepts a magnet link or a .torrent URL.
    Fires the Jackett webhook on success.
    """
    magnet      = (body.get("magnet")      or "").strip()
    torrent_url = (body.get("torrent_url") or "").strip()
    title       = (body.get("title")       or "").strip() or "Unknown"
    indexer     = (body.get("indexer")     or "").strip()
    size_bytes  = int(body.get("size_bytes") or 0)

    if not magnet and not torrent_url:
        raise HTTPException(400, "magnet or torrent_url is required")

    try:
        added_via = ""
        if torrent_url:
            from services.jackett import download_torrent_file
            try:
                payload = await download_torrent_file(torrent_url)
                row = await manager.add_torrent_file_direct(
                    payload["content"],
                    payload.get("filename") or f"{title or 'jackett'}.torrent",
                    source="jackett",
                )
                added_via = "torrent_file"
            except Exception as torrent_exc:
                if not magnet:
                    raise
                logger.warning("Jackett add: torrent URL failed for %s, falling back to magnet: %s", title, torrent_exc)
                row = await manager.add_magnet_direct(magnet, source="jackett")
                added_via = "magnet_fallback"
        else:
            row = await manager.add_magnet_direct(magnet, source="jackett")
            added_via = "magnet"
    except Exception as exc:
        raise HTTPException(400, str(exc))

    # Fire webhook (non-blocking, don't fail the request if it errors)
    try:
        from services.jackett import send_jackett_webhook
        ad_id = str(row.get("alldebrid_id") or "") if row else ""
        await send_jackett_webhook(
            title=title,
            indexer=indexer,
            size_bytes=size_bytes,
            magnet=magnet or torrent_url,
            alldebrid_id=ad_id,
        )
    except Exception as exc:
        import logging
        logging.getLogger("alldebrid.jackett").warning("Webhook failed: %s", exc)

    if row is not None:
        row["added_via"] = added_via
    return row


@router.get("/jackett/categories")
async def jackett_categories():
    from services.jackett import CATEGORIES
    return [{"id": v, "name": k} for k, v in CATEGORIES.items()]


@router.get("/stats/comprehensive")
async def get_comprehensive_stats(hours: int = Query(24, ge=1, le=8760)):
    """Comprehensive stats for a given time window (hours)."""
    from services.stats import collect_all_metrics
    return await collect_all_metrics(hours=hours)


@router.get("/stats/report")
@router.get("/stats/report-data")
async def get_stats_report(hours: int = Query(24, ge=1, le=8760)):
    """Formatted report for a given time window."""
    from services.stats import generate_report
    return await generate_report(hours=hours)


@router.post("/stats/report/send")
async def send_stats_report_ep(hours: int = Query(24, ge=1, le=8760)):
    """Send the current report to the configured reporting webhook."""
    from services.stats import send_stats_report
    return await send_stats_report(hours=hours, triggered_by="manual")


@router.post("/stats/snapshot")
async def trigger_stats_snapshot():
    """Manually trigger a stats snapshot."""
    from services.stats import take_stats_snapshot
    await take_stats_snapshot()
    return {"ok": True, "message": "Snapshot taken"}


@router.get("/stats/snapshots")
async def list_stats_snapshots(limit: int = Query(30, le=100)):
    """Return recent stats snapshots."""
    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT id, created_at FROM stats_snapshots ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return {"snapshots": rows}


@router.get("/stats/export")
async def export_stats(hours: int = Query(24, ge=1, le=8760)):
    """Export comprehensive stats as JSON."""
    from services.stats import collect_all_metrics
    from fastapi.responses import JSONResponse
    data = await collect_all_metrics(hours=hours)
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f"attachment; filename=stats_{hours}h.json"},
    )


@router.post("/admin/full-sync")
async def trigger_full_sync():
    """Manually trigger a full AllDebrid reconciliation (all torrents incl. error/queued)."""
    from services.manager_v2 import manager
    updated = await manager.full_alldebrid_sync()
    return {"ok": True, "updated": updated, "message": f"{updated} torrents updated"}


@router.post("/admin/deep-sync")
async def trigger_deep_sync():
    t0 = time.monotonic()
    await manager.deep_sync_aria2_finished()
    return {"ok": True, "elapsed_seconds": round(time.monotonic() - t0, 2)}
