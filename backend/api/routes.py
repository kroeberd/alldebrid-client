"""
REST API routes for AllDebrid-Client.

Conventions:
- All DB access uses get_db() (supports both SQLite and PostgreSQL).
- Pydantic models for request bodies are defined inline.
- No inline `import` statements — all imports are at module level.
"""
import asyncio
import ipaddress
import json as _json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, AsyncGenerator
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel

from core.config import (
    AppSettings,
    apply_settings,
    get_settings,
    load_settings,
    save_settings,
)
from core.config_validator import validate_and_sanitise
from core.logging_utils import sanitize_exception
from core.version import read_version
from db.database import DB_PATH, _is_postgres, get_db


def _sanitize_error(exc: Exception) -> str:
    """Return a safe, short error message suitable for API responses.

    Strips raw magnet links and very long URLs that may appear in exception
    strings — e.g. when AllDebrid echoes back the submitted magnet in an
    error payload, or when a download_torrent_file exception includes the URL.
    Truncates the result to 200 characters.
    """
    return sanitize_exception(exc, max_length=200)


# ── SQL dialect helpers ────────────────────────────────────────────────────────
def _sql_now_minus(interval: str) -> str:
    """Return a SQL expression for (NOW - interval) that works on both SQLite and PostgreSQL.

    interval examples: '1 hour', '1 day', '7 days', '30 days', '1 year', '90 days'
    """
    if _is_postgres():
        return f"NOW() - INTERVAL '{interval}'"
    # SQLite: rewrite '1 hour' -> '-1 hour', '7 days' -> '-7 days', etc.
    parts = interval.split()
    n, unit = parts[0], parts[1]
    return f"datetime('now','-{n} {unit}')"


def _sql_strftime(fmt: str, field: str) -> str:
    """Return a SQL date-format expression for the given field.

    fmt uses strftime-style placeholders: %H, %M, %Y, %m, %d

    For PostgreSQL, literal text in the format string (e.g. ':00') must be
    quoted with double-quotes inside the TO_CHAR format string, because
    digits like '0' are format codes in PostgreSQL TO_CHAR (unlike strftime).
    """
    if _is_postgres():
        # Map strftime codes -> TO_CHAR codes, then quote any remaining literal text
        pg_fmt = (fmt
                  .replace("%Y", "YYYY")
                  .replace("%m", "MM")
                  .replace("%d", "DD")
                  .replace("%H", "HH24")
                  .replace("%M", "MI"))
        # Any remaining non-PG-code characters (like ':00') must be wrapped in
        # double-quotes so PostgreSQL treats them as literals, not format codes.
        # Split on known PG format codes and re-join with quoted literals.
        import re as _re
        parts = _re.split(r"(YYYY|MM|DD|HH24|MI|SS)", pg_fmt)
        quoted = "".join(
            part if part in ("YYYY", "MM", "DD", "HH24", "MI", "SS") else (
                '"' + part + '"' if part else ""
            )
            for part in parts
        )
        return f"TO_CHAR({field}, '{quoted}')"
    return f"strftime('{fmt}', {field})"


def _sql_date(field: str) -> str:
    """Return DATE(field) — same syntax on both SQLite and PostgreSQL."""
    return f"DATE({field})"

from services.manager_v2 import manager
from services.aria2_runtime import runtime as aria2_runtime
from services.aria2 import aria2_download_to_dict

logger = logging.getLogger("alldebrid.routes")
router = APIRouter()


def _jackett_title_key(value: str) -> str:
    """Return a tolerant comparison key for Jackett titles and stored filenames."""
    text = (value or "").strip().lower()
    if not text:
        return ""
    stem = Path(text).stem
    return re.sub(r"[^a-z0-9]+", "", stem)


def _duplicate_candidate_from_payload(payload: dict, source: str = "preview"):
    """Build a read-only duplicate-check candidate from API/search payload data."""
    from services.duplicates import DuplicateCandidate

    return DuplicateCandidate(
        source=source,
        title=str(payload.get("title") or payload.get("name") or "").strip(),
        magnet=str(payload.get("magnet") or "").strip(),
        torrent_url=str(payload.get("torrent_url") or "").strip(),
        infohash=str(payload.get("hash") or payload.get("infohash") or "").strip().lower(),
        alldebrid_id=str(payload.get("alldebrid_id") or "").strip(),
        size_bytes=int(payload.get("size_bytes") or payload.get("size") or 0),
        indexer=str(payload.get("indexer") or "").strip(),
        category=str(payload.get("category") or "").strip(),
        imdb_id=str(payload.get("imdb_id") or payload.get("imdbid") or "").strip(),
        tmdb_id=str(payload.get("tmdb_id") or payload.get("tmdbid") or "").strip(),
    )


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


@router.get("/health")
async def health_check():
    """
    Lightweight liveness probe for Docker HEALTHCHECK and uptime monitors.

    Returns HTTP 200 as long as the process is running. Does not check
    AllDebrid or aria2 — those are external and their absence should not
    restart the container. Use GET /api/stats for full service health.
    """
    return {"status": "ok", "version": read_version()}


@router.get("/version")
async def get_version_ep():
    return {"version": read_version()}


_update_check_cache: dict = {}


def _version_gt(a: str, b: str) -> bool:
    """True if semver a > b."""
    def _t(v: str):
        try: return tuple(int(x) for x in v.lstrip("v").split("."))
        except ValueError: return (0, 0, 0)
    return _t(a) > _t(b)


@router.get("/version/check")
async def version_check():
    """Compare running version with latest GitHub release. Cached 30 min."""
    import time, aiohttp as _aiohttp
    cache, now, current = _update_check_cache, time.time(), read_version()
    if cache.get("ts", 0) + 1800 > now:
        return cache.get("result", {"current": current, "latest": None, "update_available": False})
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(
                "https://api.github.com/repos/kroeberd/alldebrid-client/releases/latest",
                headers={"Accept": "application/vnd.github.v3+json"},
            ) as r:
                if r.status != 200: raise RuntimeError("GitHub API " + str(r.status))
                rel = await r.json()
        latest = (rel.get("tag_name") or "").lstrip("v")
        result = {
            "current": current, "latest": latest,
            "update_available": _version_gt(latest, current),
            "release_url":   rel.get("html_url", ""),
            "release_notes": (rel.get("body") or "").strip(),
            "published_at":  (rel.get("published_at") or "")[:10],
        }
        cache["result"] = result
        cache["ts"] = now
        return result
    except Exception as exc:
        logger.warning("Version check failed: %s", exc)
        return {"current": current, "latest": None, "update_available": False, "error": str(exc)}


@router.put("/settings")
async def update_settings(new: AppSettings):
    previous = get_settings()
    env_db_type = os.getenv("DB_TYPE", "").strip()
    if env_db_type == "postgres_internal":
        new = new.model_copy(update={"db_type": "postgres"})
    clean = validate_and_sanitise(new)
    # ── Sync derived fields before saving ───────────────────────────────────
    # max_concurrent_downloads is the single source of truth for "how many
    # parallel downloads".  Keep aria2_max_active_downloads in sync so that
    # aria2_global_options() (which reads aria2_max_active_downloads) always
    # sends the correct value to aria2 via apply_aria2_memory_tuning().
    if getattr(clean, "max_concurrent_downloads", None) is not None:
        try:
            clean = clean.model_copy(update={
                "aria2_max_active_downloads": clean.max_concurrent_downloads
            })
        except Exception as _e:
            logger.debug("Could not sync aria2_max_active_downloads: %s", _e)
    save_settings(clean)
    apply_settings(clean)
    manager.reset_services()
    if getattr(clean, "aria2_mode", "external") == "builtin":
        if (
            getattr(previous, "aria2_mode", "external") == "builtin"
            and getattr(previous, "aria2_builtin_port", 6800) != getattr(clean, "aria2_builtin_port", 6800)
        ):
            await aria2_runtime.restart()
        else:
            await aria2_runtime.ensure_started()
    elif getattr(previous, "aria2_mode", "external") == "builtin":
        await aria2_runtime.stop()
    if getattr(clean, "aria2_mode", "external") == "builtin" or getattr(clean, "aria2_url", "").strip():
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


@router.post("/settings/test-jackett-webhook")
async def test_jackett_webhook():
    cfg = get_settings()
    webhook_url = (cfg.jackett_webhook_url or cfg.discord_webhook_url or "").strip()
    if not webhook_url:
        raise HTTPException(400, "No Jackett or Discord webhook configured")
    from services.notifications import NotificationService, COLOR_ADDED, _now_utc
    svc = NotificationService(webhook_url)
    sent = await svc._send(
        url=webhook_url,
        title="📥 Jackett Webhook Test",
        description="**AllDebrid-Client** can send Jackett notifications.",
        color=COLOR_ADDED,
        fields=[
            {"name": "Source", "value": "Jackett Search", "inline": True},
            {"name": "Indexer", "value": "Test", "inline": True},
            {"name": "Time", "value": _now_utc(), "inline": True},
        ],
        bypass_dedup=True,
    )
    if not sent:
        raise HTTPException(502, "Jackett webhook test failed — check webhook URL")
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
        raise HTTPException(502, _sanitize_error(e))


@router.post("/settings/test-aria2")
async def test_aria2():
    try:
        result = await manager.test_aria2()
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.post("/settings/aria2-housekeeping")
async def run_aria2_housekeeping_ep():
    try:
        return await manager.run_aria2_housekeeping()
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.get("/aria2/runtime")
async def aria2_runtime_status():
    status = await aria2_runtime.status()
    diagnostics = {}
    speed_stat = {"download_speed": 0, "upload_speed": 0, "active": 0}
    try:
        if status.get("running"):
            diagnostics = await manager._aria2_get_memory_diagnostics()
            speed_stat  = await manager.aria2().get_global_stat()
    except Exception as exc:
        diagnostics = {"error": str(exc)}
    return {**status, "diagnostics": diagnostics, **speed_stat}

@router.post("/aria2/runtime/start")
async def aria2_runtime_start():
    status = await aria2_runtime.start()
    manager.reset_services()
    return status


@router.post("/aria2/runtime/stop")
async def aria2_runtime_stop():
    status = await aria2_runtime.stop()
    manager.reset_services()
    return status


@router.post("/aria2/runtime/restart")
async def aria2_runtime_restart():
    status = await aria2_runtime.restart()
    manager.reset_services()
    return status


@router.post("/aria2/runtime/apply")
async def aria2_runtime_apply():
    try:
        await aria2_runtime.apply_options()
        result = await manager.run_aria2_housekeeping()
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.get("/aria2/downloads")
async def aria2_downloads():
    cfg = get_settings()
    try:
        downloads = await manager.aria2().get_all(
            getattr(cfg, "aria2_waiting_window", 100),
            getattr(cfg, "aria2_stopped_window", 100),
        )
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))
    items = [aria2_download_to_dict(download) for download in downloads]
    groups = {
        "active": [item for item in items if item["status"] == "active"],
        "waiting": [item for item in items if item["status"] in {"waiting", "paused"}],
        "stopped": [item for item in items if item["status"] not in {"active", "waiting", "paused"}],
    }
    return {
        "ok": True,
        "items": items,
        "groups": groups,
        "summary": {
            "active": len(groups["active"]),
            "waiting": len(groups["waiting"]),
            "stopped": len(groups["stopped"]),
            "download_speed": sum(item["download_speed"] for item in groups["active"]),
            "remaining_length": sum(item["remaining_length"] for item in items),
        },
    }


@router.post("/aria2/downloads/{gid}/{action}")
async def aria2_download_action(gid: str, action: str):
    if action not in {"pause", "resume", "remove"}:
        raise HTTPException(400, "Unsupported aria2 action")
    try:
        svc = manager.aria2()
        if action == "pause":
            await svc.pause(gid)
        elif action == "resume":
            await svc.resume(gid)
        else:
            await svc.remove(gid)
        return {"ok": True, "gid": gid, "action": action}
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


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
        raise HTTPException(502, f"PostgreSQL connection failed: {_sanitize_error(e)}")


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
                    OR LOWER(COALESCE(t.alldebrid_id, '')) LIKE ?
                    OR LOWER(COALESCE(t.error_message, '')) LIKE ?
                )"""
            )
            needle = f"%{search.strip().lower()}%"
            params.extend([needle, needle, needle, needle, needle, needle])

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
        raise HTTPException(400, _sanitize_error(e))


@router.post("/torrents/check-duplicate")
async def check_torrent_duplicate(body: dict):
    """Read-only duplicate preview. Never uploads/imports anything to AllDebrid."""
    from services.duplicates import check_before_add

    candidate = _duplicate_candidate_from_payload(body, source=str(body.get("source") or "preview"))
    if not (candidate.infohash or candidate.magnet or candidate.title or candidate.alldebrid_id):
        raise HTTPException(400, "title, magnet, hash, infohash, or alldebrid_id is required")
    decision = await check_before_add(candidate)
    return {"ok": True, "duplicate": decision.as_dict()}


@router.post("/torrents/import-existing")
async def import_existing():
    results = await manager.import_existing_magnets()
    return {"imported": len(results), "items": results}


@router.get("/torrents/diagnose")
async def diagnose_torrents():
    """Return a full count breakdown of all local torrent statuses."""
    async with get_db() as db:
        all_counts = await (await db.execute(
            """SELECT status, COUNT(*) AS cnt FROM torrents
               GROUP BY status ORDER BY cnt DESC"""
        )).fetchall()
        non_terminal = await (await db.execute(
            """SELECT t.id, t.name, t.status, t.alldebrid_id,
                      (SELECT COUNT(*) FROM download_files f WHERE f.torrent_id=t.id AND f.blocked=0) AS file_count
               FROM torrents t
               WHERE t.status NOT IN ('completed', 'deleted')
               ORDER BY t.id DESC LIMIT 20"""
        )).fetchall()
    return {
        "status_counts": [dict(r) for r in all_counts],
        "sample_non_terminal": [dict(r) for r in non_terminal],
    }


@router.post("/torrents/recover-all")
async def recover_all_ready():
    """
    Immediately recover torrents stuck in bad states and dispatch all that
    AllDebrid reports as ready.

    Step 1: Reset any torrent with status='downloading' but no download_files
            records — these are stuck waiting for the semaphore and will never
            progress on their own.
    Step 2: Run import_existing_magnets() to pick up all ready AllDebrid magnets
            and dispatch _start_download for each.
    """
    try:
        # Step 1: Reset stuck 'downloading' torrents that have no download_files.
        # These were set to 'downloading' by _start_download before acquiring the
        # semaphore (v1.5.39 bug), so _download() never ran and no files were queued.
        reset_count = 0
        async with get_db() as db:
            stuck = await (await db.execute(
                """SELECT t.id FROM torrents t
                   WHERE t.status = 'downloading'
                     AND NOT EXISTS (
                         SELECT 1 FROM download_files f
                         WHERE f.torrent_id = t.id AND f.blocked = 0
                     )"""
            )).fetchall()
            if stuck:
                ids = [r["id"] for r in stuck]
                placeholders = ",".join("?" * len(ids))
                await db.execute(
                    f"UPDATE torrents SET status='ready', updated_at=CURRENT_TIMESTAMP "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
                await db.execute(
                    f"INSERT INTO events (torrent_id, level, message) "
                    f"SELECT id, 'warn', 'recover-all: reset stuck downloading (no download_files)' "
                    f"FROM torrents WHERE id IN ({placeholders})",
                    ids,
                )
                await db.commit()
                reset_count = len(ids)

        # Step 2: Import and dispatch all ready AllDebrid magnets.
        result = await manager.import_existing_magnets()
        started = sum(1 for r in result if r.get("should_queue") and r.get("status") == "ready")
        # Build breakdown for diagnosis
        from collections import Counter
        status_breakdown = Counter(r.get("status") for r in result)
        sq_breakdown = Counter(
            f"{r.get('status')}/sq={r.get('should_queue')}"
            for r in result
        )
        return {
            "ok": True,
            "reset": reset_count,
            "checked": len(result),
            "started": started,
            "status_breakdown": dict(status_breakdown),
            "should_queue_breakdown": dict(sq_breakdown),
        }
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.get("/torrents/{torrent_id}/files-preview")
async def torrent_files_preview(torrent_id: int):
    """Preview downloadable files for a ready torrent (fetched live from AllDebrid).

    Returns the file list without starting a download or changing torrent state.
    For torrents already in downloading/queued/completed state, returns the
    local download_files rows instead.
    """
    async with get_db() as db:
        row = await db.fetchone(
            "SELECT id, alldebrid_id, status, name FROM torrents WHERE id=?",
            (torrent_id,),
        )
        if not row:
            raise HTTPException(404, "Torrent not found")

        # For torrents already processed, return local download_files
        if row["status"] in ("queued", "downloading", "paused", "completed"):
            files = await db.fetchall(
                "SELECT id, filename, size_bytes, status, blocked, progress "
                "FROM download_files WHERE torrent_id=? AND blocked=0 ORDER BY id",
                (torrent_id,),
            )
            return {"source": "local", "files": [dict(f) for f in files]}

    # For ready/processing torrents, fetch live from AllDebrid
    if not row["alldebrid_id"]:
        raise HTTPException(400, "Torrent has no AllDebrid ID — not ready yet")
    try:
        files_data = await manager.ad().get_magnet_files([str(row["alldebrid_id"])])
        from services.alldebrid import flatten_files
        for entry in files_data:
            if str(entry.get("id", "")) == str(row["alldebrid_id"]):
                flat = flatten_files(entry.get("files", []))
                return {
                    "source": "alldebrid",
                    "files": [
                        {
                            "link":     f.get("link", ""),
                            "filename": f.get("filename") or f.get("name") or f.get("link", ""),
                            "size_bytes": int(f.get("size") or 0),
                        }
                        for f in flat
                    ],
                }
        return {"source": "alldebrid", "files": []}
    except Exception as exc:
        raise HTTPException(502, _sanitize_error(exc))


@router.post("/torrents/{torrent_id}/files/{file_id}/block")
async def block_file(torrent_id: int, file_id: int, blocked: bool = True):
    """Toggle the blocked flag on a download_files row.

    Blocked files are skipped by aria2 dispatch and not counted toward
    torrent completion. Use blocked=false to unblock.
    """
    async with get_db() as db:
        row = await db.fetchone(
            "SELECT id FROM download_files WHERE id=? AND torrent_id=?",
            (file_id, torrent_id),
        )
        if not row:
            raise HTTPException(404, "File not found")
        await db.execute(
            "UPDATE download_files SET blocked=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (1 if blocked else 0, file_id),
        )
        await db.commit()
    return {"ok": True, "file_id": file_id, "blocked": blocked}


@router.get("/torrents/{torrent_id}")
async def get_torrent(torrent_id: int):
    """Return a single torrent with its download files and recent events."""
    async with get_db() as db:
        row = await db.fetchone("SELECT * FROM torrents WHERE id=?", (torrent_id,))
        if not row:
            raise HTTPException(404, "Not found")
        files  = await db.fetchall(
            "SELECT * FROM download_files WHERE torrent_id=? ORDER BY id", (torrent_id,))
        events = await db.fetchall(
            "SELECT * FROM events WHERE torrent_id=? ORDER BY created_at DESC LIMIT 50", (torrent_id,))
        return {**dict(row), "files": [dict(f) for f in files], "events": [dict(e) for e in events]}


@router.delete("/torrents/{torrent_id}")
async def delete_torrent(torrent_id: int, from_alldebrid: bool = True):
    try:
        await manager.delete_torrent(torrent_id, delete_from_ad=from_alldebrid)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, _sanitize_error(e))
    except Exception as e:
        raise HTTPException(500, _sanitize_error(e))


@router.post("/torrents/{torrent_id}/retry")
async def retry_torrent(torrent_id: int):
    """Re-queue a failed torrent.

    If the torrent has a stored magnet link it is re-uploaded to AllDebrid
    from scratch (old alldebrid_id cleared, upload_retry_count reset).
    If only an alldebrid_id is known (no magnet — e.g. added via .torrent
    file) the status is reset to 'ready' so the poll cycle re-checks it.
    """
    async with get_db() as db:
        row = await db.fetchone("SELECT * FROM torrents WHERE id=?", (torrent_id,))
        if not row:
            raise HTTPException(404, "Torrent not found")

    magnet = (row.get("magnet") or "").strip()
    ad_id  = (row.get("alldebrid_id") or "").strip()

    if not magnet and not ad_id:
        raise HTTPException(400, "No magnet or AllDebrid ID — cannot retry")

    if magnet:
        # Re-upload the magnet to AllDebrid from scratch.
        # Clear the stale alldebrid_id and reset counters first so that
        # if the upload fails the torrent is left in a clean error state.
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET status='uploading', alldebrid_id=NULL,
                       error_message=NULL, polling_failures=0,
                       upload_retry_count=0, provider_status=NULL,
                       provider_status_code=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (torrent_id,),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (torrent_id, "Manual retry — re-uploading magnet to AllDebrid"),
            )
            await db.commit()
        try:
            await manager.add_magnet_direct(magnet, source=str(row.get("source") or "manual"))
        except Exception as exc:
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET status='error', error_message=?,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (_sanitize_error(exc), torrent_id),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'error', ?)",
                    (torrent_id, f"Manual retry failed: {_sanitize_error(exc)}"),
                )
                await db.commit()
            raise HTTPException(502, _sanitize_error(exc))
        return {"ok": True, "new_status": "uploading"}
    else:
        # No magnet stored (added via .torrent file) — reset status so
        # the poll cycle re-checks the existing alldebrid_id.
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET status='ready', error_message=NULL,
                       polling_failures=0, upload_retry_count=0,
                       provider_status=NULL, provider_status_code=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (torrent_id,),
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
                (torrent_id, "Manual retry — resetting to ready (no magnet stored)"),
            )
            await db.commit()
        return {"ok": True, "new_status": "ready"}


@router.post("/torrents/{torrent_id}/pause")
async def pause_torrent(torrent_id: int):
    try:
        await manager.pause_torrent(torrent_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, _sanitize_error(e))


@router.post("/torrents/{torrent_id}/resume")
async def resume_torrent(torrent_id: int):
    try:
        await manager.resume_torrent(torrent_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, _sanitize_error(e))


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
            elif body.action == "reset":
                # Reset any stuck/error torrent back to 'ready' so the
                # next sync cycle picks it up again
                async with get_db() as db:
                    await db.execute(
                        """UPDATE torrents
                           SET status='ready', error_message=NULL,
                               polling_failures=0, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND alldebrid_id IS NOT NULL""",
                        (tid,),
                    )
                    await db.commit()
            elif body.action == "pause":
                await manager.pause_torrent(tid)
            elif body.action == "resume":
                await manager.resume_torrent(tid)
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
        last_24h        = _c(await db.fetchone(f"SELECT COUNT(*) as c FROM torrents WHERE completed_at >= {_sql_now_minus('1 day')}") )
        last_7d         = _c(await db.fetchone(f"SELECT COUNT(*) as c FROM torrents WHERE completed_at >= {_sql_now_minus('7 days')}") )
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
async def get_stats_detail(period: str = "all"):
    """
    period: "1h" | "24h" | "7d" | "30d" | "1y" | "all"
    All metrics (including totals) are filtered to the selected period.
    """
    period_map = {
        "1h":  (_sql_now_minus("1 hour"),  "1h",  _sql_strftime("%H:%M", "completed_at"), 60),
        "24h": (_sql_now_minus("1 day"),   "24h", _sql_strftime("%H:00", "completed_at"), 24),
        "7d":  (_sql_now_minus("7 days"),  "7d",  _sql_date("completed_at"),              7),
        "30d": (_sql_now_minus("30 days"), "30d", _sql_date("completed_at"),              30),
        "1y":  (_sql_now_minus("1 year"),  "1y",  _sql_strftime("%Y-%m", "completed_at"), 12),
        "all": (None,                      "all", _sql_date("completed_at"),              None),
    }
    entry = period_map.get(period, period_map["all"])
    cutoff, period_label, date_fmt, _ = entry
    where_ts   = f"WHERE created_at >= {cutoff}"    if cutoff else ""
    where_done = f"WHERE completed_at >= {cutoff}"   if cutoff else ""
    where_comp = (f"WHERE status='completed' AND completed_at >= {cutoff}"
                  if cutoff else "WHERE status='completed'")

    async with get_db() as db:
        # ── Totals (period-filtered) ─────────────────────────────────────────
        totals_row = await db.fetchone(
            f"SELECT COUNT(*) as torrent_total, COALESCE(SUM(size_bytes),0) as torrent_size_total "
            f"FROM torrents {where_ts}"
        ) or {}
        totals = dict(totals_row)

        completed_count = (await db.fetchone(
            f"SELECT COUNT(*) as c FROM torrents {where_comp}") or {}).get("c", 0)
        error_count = (await db.fetchone(
            f"SELECT COUNT(*) as c FROM torrents WHERE status='error'"
            + (f" AND created_at >= {cutoff}" if cutoff else "")) or {}).get("c", 0)
        terminal = completed_count + error_count
        totals["success_rate_pct"] = round(completed_count / terminal * 100, 1) if terminal > 0 else None

        completed_size_row = await db.fetchone(
            f"SELECT COALESCE(SUM(size_bytes),0) as v FROM torrents {where_comp}")
        totals["completed_size"]  = completed_size_row["v"] if completed_size_row else 0
        totals["completed_count"] = completed_count

        partial_row = await db.fetchone(
            "SELECT COUNT(*) as c FROM torrents "
            "WHERE status IN ('processing','downloading','dispatched','partial')"
            + (f" AND created_at >= {cutoff}" if cutoff else ""))
        totals["partial_total"] = partial_row["c"] if partial_row else 0

        # ── Breakdowns ───────────────────────────────────────────────────────
        torrent_status = await db.fetchall(
            f"SELECT status, COUNT(*) as count FROM torrents {where_ts} "
            f"GROUP BY status ORDER BY count DESC")
        where_files = (f"WHERE updated_at >= {cutoff}" if cutoff else "")
        file_status = await db.fetchall(
            f"SELECT status, COUNT(*) as count, COALESCE(SUM(size_bytes),0) as size_bytes "
            f"FROM download_files {where_files} GROUP BY status ORDER BY count DESC")
        event_levels = await db.fetchall(
            f"SELECT level, COUNT(*) as count FROM events {where_ts} GROUP BY level")
        sources = await db.fetchall(
            f"SELECT source, COUNT(*) as count FROM torrents {where_ts} "
            f"GROUP BY source ORDER BY count DESC LIMIT 10")

        # ── Chart data (period-aware grouping) ───────────────────────────────
        _cutoff_90d = _sql_now_minus("90 days")
        if period == "1h":
            _grp = _sql_strftime("%H:%M", "completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {cutoff} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY date ASC")
        elif period == "24h":
            # Group and label by hour — both SELECT and GROUP BY use the same expression
            # to satisfy PostgreSQL's strict grouping rules.
            _grp = _sql_strftime("%H:00", "completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {cutoff} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY {_grp} ASC")
        elif period in ("7d", "30d"):
            _grp = _sql_date("completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {cutoff} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY date ASC")
        elif period == "1y":
            _grp = _sql_strftime("%Y-%m", "completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {cutoff} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY date ASC")
        else:  # all — last 90 days grouped by day
            _grp = _sql_date("completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {_cutoff_90d} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY date ASC")

        return {
            "period":             period_label,
            "totals":             totals,
            "torrent_status":     torrent_status,
            "file_status":        file_status,
            "event_levels":       event_levels,
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

_changelog_cache: dict = {}


@router.get("/changelog")
async def get_changelog():
    """Return CHANGELOG.md.
    Uses local file when it contains the running version entry.
    Falls back to GitHub Releases API (1h cache) for stale images."""
    import time, aiohttp as _aiohttp
    local: str | None = None
    for c in (Path("/app/CHANGELOG.md"),
              Path(__file__).resolve().parents[2] / "CHANGELOG.md"):
        if c.exists():
            local = c.read_text(encoding="utf-8"); break
    running = read_version()
    if local and ("[" + running + "]") in local:
        return {"content": local, "source": "local"}
    cache, now = _changelog_cache, time.time()
    if cache.get("ts", 0) + 3600 > now:
        return {"content": cache.get("content", local or ""), "source": "github_cache"}
    sep = "\n\n---\n\n"
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(
                "https://api.github.com/repos/kroeberd/alldebrid-client/releases?per_page=25",
                headers={"Accept": "application/vnd.github.v3+json"},
            ) as r:
                if r.status == 200:
                    rels = await r.json()
                    parts = []
                    for rel in rels:
                        body = (rel.get("body") or "").strip()
                        tag  = rel.get("tag_name", "")
                        date = (rel.get("published_at") or "")[:10]
                        parts.append(body or "## " + tag + " \u2014 " + date)
                    combined = sep.join(parts)
                    cache["content"] = combined
                    cache["ts"] = now
                    return {"content": combined, "source": "github"}
    except Exception as exc:
        logger.warning("Changelog GitHub fetch failed: %s", exc)
    return {"content": local or "", "source": "local_fallback"}


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
        raise HTTPException(500, f"PostgreSQL configuration not available: {_sanitize_error(e)}")

    from db.migration import migrate_postgres_to_sqlite, migrate_sqlite_to_postgres
    try:
        if direction == "sqlite_to_postgres":
            result = await migrate_sqlite_to_postgres(DB_PATH, pg_dsn, force=force, dry_run=dry_run)
        else:
            result = await migrate_postgres_to_sqlite(pg_dsn, DB_PATH, force=force, dry_run=dry_run)
    except Exception as e:
        raise HTTPException(500, f"Migration failed: {_sanitize_error(e)}")

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


@router.post("/admin/drop-page-cache")
async def drop_page_cache_ep():
    """
    Release the Linux kernel page cache for all completed download files.
    This frees RAM that Linux holds as file cache after downloads finish.
    Safe to call at any time — files on disk are not affected.
    """
    from services.page_cache import drop_page_cache_for_file
    from pathlib import Path

    try:
        async with get_db() as db:
            rows = await (await db.execute(
                "SELECT local_path FROM download_files "
                "WHERE status='completed' AND local_path IS NOT NULL"
            )).fetchall()
        paths = [r["local_path"] for r in rows if r["local_path"]]
        dropped = sum(1 for p in paths if drop_page_cache_for_file(p))
        return {
            "ok": True,
            "files_processed": len(paths),
            "cache_released": dropped,
            "message": f"Page cache released for {dropped}/{len(paths)} files",
        }
    except Exception as e:
        raise HTTPException(500, _sanitize_error(e))


@router.get("/admin/memory-info")
async def memory_info_ep():
    """
    Read /proc/meminfo to show the difference between total RAM usage
    and actual used RAM vs kernel page cache.
    This helps diagnose whether high RAM usage is a real leak or
    normal kernel page-cache behaviour.
    """
    import re as _re
    from pathlib import Path as _Path

    info = {}
    try:
        text = _Path("/proc/meminfo").read_text()
        for line in text.splitlines():
            m = _re.match(r"^(\w+):\s+(\d+)\s+kB$", line)
            if m:
                info[m.group(1)] = int(m.group(2)) * 1024

        def fmt(b: int) -> str:
            if b >= 1 << 30:
                return f"{b / (1 << 30):.1f} GB"
            if b >= 1 << 20:
                return f"{b / (1 << 20):.1f} MB"
            return f"{b / (1 << 10):.0f} KB"

        total       = info.get("MemTotal", 0)
        free        = info.get("MemFree", 0)
        available   = info.get("MemAvailable", 0)
        cached      = info.get("Cached", 0) + info.get("SwapCached", 0)
        buffers     = info.get("Buffers", 0)
        used        = total - free - cached - buffers
        page_cache  = cached + buffers

        return {
            "total":           fmt(total),
            "really_used":     fmt(used),
            "page_cache":      fmt(page_cache),
            "available":       fmt(available),
            "free":            fmt(free),
            "note": (
                "really_used is actual process RAM. "
                "page_cache is kernel file cache (shown as 'used' in Unraid dashboard "
                "but reclaimed automatically when needed). "
                "If page_cache is large, run POST /admin/drop-page-cache to release it."
            ),
            "raw_kb": {k: v // 1024 for k, v in info.items()
                       if k in ("MemTotal","MemFree","MemAvailable","Cached","Buffers","SwapTotal","SwapFree")},
        }
    except Exception as e:
        raise HTTPException(500, _sanitize_error(e))



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


@router.get("/aria2/global-options")
async def aria2_get_global_options():
    """Return current aria2 global options (includes speed limits)."""
    try:
        opts = await manager.aria2().get_global_options()
        return {
            "ok": True,
            "max_download_speed": int(opts.get("max-overall-download-limit") or 0),
            "max_upload_speed":   int(opts.get("max-overall-upload-limit")   or 0),
            "max_concurrent_downloads": int(opts.get("max-concurrent-downloads") or 0),
            "raw": {k: v for k, v in opts.items() if "limit" in k or "speed" in k or "concurrent" in k},
        }
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.post("/aria2/global-options")
async def aria2_set_global_options(body: dict):
    """
    Apply global aria2 options at runtime.
    Accepts: max_download_speed (bytes/s, 0=unlimited), max_upload_speed.
    """
    options: dict = {}
    cfg_updates: dict = {}
    if "max_download_speed" in body:
        val = int(body["max_download_speed"])
        options["max-overall-download-limit"] = str(val)
        cfg_updates["aria2_max_download_limit"] = val
    if "max_upload_speed" in body:
        val = int(body["max_upload_speed"])
        options["max-overall-upload-limit"] = str(val)
        cfg_updates["aria2_max_upload_limit"] = val
    if "max_concurrent_downloads" in body:
        val = max(1, int(body["max_concurrent_downloads"]))
        options["max-concurrent-downloads"] = str(val)
        # Persist in BOTH fields so aria2 startup and the Manager Semaphore
        # use the same value.  Previously only aria2_max_active_downloads was
        # written, causing the Manager Semaphore (which reads max_concurrent_downloads)
        # to diverge from aria2 after a quick-setter change.
        cfg_updates["aria2_max_active_downloads"] = val
        cfg_updates["max_concurrent_downloads"] = val
    if not options:
        raise HTTPException(400, "No valid options provided")
    try:
        await manager.aria2().change_global_options(options)
        # Persist so the limits survive an aria2 restart
        if cfg_updates:
            current = load_settings()
            for k, v in cfg_updates.items():
                setattr(current, k, v)
            save_settings(current)
            apply_settings(current)
        # If max_concurrent_downloads changed, reset the Manager Semaphore so
        # the next _start_download picks up the new limit immediately.
        if "max_concurrent_downloads" in cfg_updates:
            manager.reset_services()
            try:
                await manager._dispatch_pending_aria2_queue()
            except Exception as exc:
                logger.debug("aria2 quick slot dispatch skipped: %s", sanitize_exception(exc))
        return {"ok": True, "applied": options}
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))




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


# ── Prowlarr ──────────────────────────────────────────────────────────────────

@router.get("/prowlarr/indexers")
async def prowlarr_indexers():
    """List all Prowlarr indexers."""
    from services.prowlarr import get_indexers as pr_indexers
    return await pr_indexers()


@router.get("/prowlarr/search")
async def prowlarr_search(
    q:          str         = Query(..., min_length=1),
    indexerIds: str         = Query(""),
    categories: str         = Query(""),
    limit:      int         = Query(100, ge=1, le=500),
):
    """Search Prowlarr indexers and return normalised results."""
    from services.prowlarr import search as pr_search
    ids  = [int(i) for i in indexerIds.split(",") if i.strip().isdigit()] if indexerIds else None
    cats = [int(c) for c in categories.split(",") if c.strip().isdigit()] if categories else None
    try:
        return await pr_search(q, indexer_ids=ids, categories=cats, limit=limit)
    except Exception as exc:
        raise HTTPException(502, _sanitize_error(exc))


@router.post("/prowlarr/test")
async def prowlarr_test():
    """Verify Prowlarr connectivity and API key."""
    from services.prowlarr import test_connection
    result = await test_connection()
    if not result["ok"]:
        # Sanitise the error message — avoid leaking internal stack traces
        err = str(result.get("error") or "Prowlarr connection failed")[:200]
        raise HTTPException(502, err)
    # Return only non-sensitive fields
    return {"ok": True, "issues": result.get("issues", [])}


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
    hide_dead = bool(body.get("hide_dead"))
    # Extended tag-search parameters
    search_type = (body.get("search_type") or "search").strip().lower()
    if search_type not in ("search", "tvsearch", "movie", "music", "book"):
        search_type = "search"
    genre  = (body.get("genre")  or "").strip()
    imdbid = (body.get("imdbid") or "").strip()
    year   = (body.get("year")   or "").strip()
    season = (body.get("season") or "").strip()
    ep     = (body.get("ep")     or "").strip()
    result = await search(
        query=query, category=category, trackers=trackers, limit=limit,
        search_type=search_type, genre=genre, imdbid=imdbid,
        year=year, season=season, ep=ep,
    )
    if hide_dead:
        result["results"] = [
            item for item in (result.get("results", []) or [])
            if int(item.get("seeders") or 0) > 0
        ]
        result["total"] = len(result["results"])
    hashes = sorted({str(item.get("hash") or "").strip().lower() for item in result.get("results", []) if str(item.get("hash") or "").strip()})
    titles = sorted({str(item.get("title") or "").strip().lower() for item in result.get("results", []) if str(item.get("title") or "").strip()})

    existing_by_hash: dict[str, dict] = {}
    existing_by_title: dict[str, dict] = {}
    existing_by_title_key: dict[str, dict] = {}
    if hashes or titles:
        async with get_db() as db:
            if hashes:
                hash_placeholders = ",".join("?" for _ in hashes)
                rows = await db.fetchall(
                    f"SELECT id, hash, status, name FROM torrents WHERE LOWER(hash) IN ({hash_placeholders})",
                    hashes,
                )
                existing_by_hash = {str(row["hash"]).strip().lower(): row for row in rows}
            if titles:
                title_placeholders = ",".join("?" for _ in titles)
                title_rows = await db.fetchall(
                    f"""SELECT DISTINCT t.id, t.hash, t.status, t.name, df.filename
                        FROM torrents t
                        LEFT JOIN download_files df ON df.torrent_id = t.id
                        WHERE LOWER(COALESCE(t.name, '')) IN ({title_placeholders})
                           OR LOWER(COALESCE(df.filename, '')) IN ({title_placeholders})""",
                    [*titles, *titles],
                )
                for row in title_rows:
                    torrent_name = str(row.get("name") or "").strip().lower()
                    file_name = str(row.get("filename") or "").strip().lower()
                    if torrent_name and torrent_name not in existing_by_title:
                        existing_by_title[torrent_name] = row
                    torrent_key = _jackett_title_key(torrent_name)
                    if torrent_key and torrent_key not in existing_by_title_key:
                        existing_by_title_key[torrent_key] = row
                    if file_name and file_name not in existing_by_title:
                        existing_by_title[file_name] = row
                    file_key = _jackett_title_key(file_name)
                    if file_key and file_key not in existing_by_title_key:
                        existing_by_title_key[file_key] = row

    for item in result.get("results", []):
        item_hash = str(item.get("hash") or "").strip().lower()
        item_title = str(item.get("title") or "").strip().lower()
        item_title_key = _jackett_title_key(item_title)
        existing = None
        if item_hash:
            existing = existing_by_hash.get(item_hash)
        if not existing and item_title:
            existing = existing_by_title.get(item_title)
        if not existing and item_title_key:
            existing = existing_by_title_key.get(item_title_key)
        item["already_added"] = bool(existing)
        item["existing_torrent_id"] = existing["id"] if existing else None
        item["existing_status"] = existing["status"] if existing else ""
        if existing:
            item["duplicate"] = {
                "is_duplicate": True,
                "confidence": 1.0,
                "action": "skip",
                "reason": "existing_search_match",
                "matches": [{
                    "torrent_id": existing["id"],
                    "name": existing["name"],
                    "status": existing["status"],
                    "hash": existing["hash"],
                    "reason": "existing_search_match",
                    "confidence": 1.0,
                }],
            }
        # NOTE: per-item check_before_add() (semantic duplicate check) is intentionally
        # omitted here. Calling it for every search result would open hundreds of
        # sequential DB connections and is the primary cause of search timeouts.
        # Duplicate detection runs at add-time (POST /jackett/add) where it matters.

    # ── Learning Score: annotate results with indexer trust score ─────────────
    try:
        from services.learning import score_result, get_learning_stats
        learning = await get_learning_stats()
        indexer_scores = {
            ix["indexer"].lower(): ix["score"]
            for ix in learning.get("indexers", [])
        }
        for item in result.get("results", []):
            item["_score"] = score_result(item, indexer_scores)
        # Sort by score descending, then seeders descending as tiebreaker
        result["results"].sort(
            key=lambda x: (-(x.get("_score") or 0), -int(x.get("seeders") or 0))
        )
        result["total"] = len(result["results"])
    except Exception as exc:
        logger.debug("Learning score annotation failed: %s", exc)

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
    result_hash = (body.get("hash")        or "").strip().lower()
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
                    preferred_hash=(result_hash or str(payload.get("infohash") or "").strip().lower() or None),
                )
                added_via = "torrent_file"
            except Exception as torrent_exc:
                if not magnet:
                    raise
                logger.warning(
                    "Jackett add: torrent URL failed for %s, falling back to magnet: %s",
                    title,
                    sanitize_exception(torrent_exc),
                )
                row = await manager.add_magnet_direct(magnet, source="jackett")
                added_via = "magnet_fallback"
        else:
            row = await manager.add_magnet_direct(magnet, source="jackett")
            added_via = "magnet"
    except Exception as exc:
        # Sanitize error message — never expose raw magnet links as the error detail
        raw = str(exc)
        if raw.startswith("magnet:"):
            detail = "Failed to add magnet to AllDebrid (invalid or rejected)"
        else:
            detail = raw
        raise HTTPException(400, detail)

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
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    data = await collect_all_metrics(hours=hours)
    return JSONResponse(
        content=jsonable_encoder(data),
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


# ── Server-Sent Events (SSE) ──────────────────────────────────────────────────
# Lightweight pub/sub: a set of asyncio.Queue instances, one per connected client.
# The backend pushes events when significant state changes occur; the frontend
# listens via EventSource and drops its 15-second polling interval.
#
# Event types:
#   ping          — heartbeat every 30 s (keeps the connection alive through proxies)
#   stats_changed — basic stats object; frontend re-renders stats bar
#   torrent_updated — {id, status, name}; frontend refreshes the affected row
#
# This requires NO external dependencies (no Redis, no WebSocket library).

_sse_subscribers: set[asyncio.Queue] = set()
_sse_lock = asyncio.Lock()


async def _sse_broadcast(event_type: str, data: dict) -> None:
    """Push an SSE event to all connected clients (fire-and-forget)."""
    payload = f"event: {event_type}\ndata: {_json.dumps(data)}\n\n"
    dead: list[asyncio.Queue] = []
    async with _sse_lock:
        for q in _sse_subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _sse_subscribers.discard(q)


async def _sse_generator(request: Request) -> AsyncGenerator[str, None]:
    """Yield SSE frames until the client disconnects."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    async with _sse_lock:
        _sse_subscribers.add(queue)
    try:
        yield "event: connected\ndata: {}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=30)
                yield frame
            except asyncio.TimeoutError:
                # Send heartbeat so proxies don't close the connection
                yield "event: ping\ndata: {}\n\n"
    finally:
        async with _sse_lock:
            _sse_subscribers.discard(queue)


@router.get("/events/stream")
async def events_stream(request: Request):
    """Server-Sent Events stream for live UI updates.

    Connect via:  const es = new EventSource('/api/events/stream');
    Events:  connected, ping, stats_changed, torrent_updated
    """
    return StreamingResponse(
        _sse_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


@router.get("/events/subscriber-count")
async def sse_subscriber_count():
    """Diagnostic: how many SSE clients are currently connected."""
    return {"subscribers": len(_sse_subscribers)}


# ── Prometheus metrics ────────────────────────────────────────────────────────

@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint.

    Scrape with: `- job_name: alldebrid  static_configs: [{targets: [host:8080]}]`
    and set `metrics_path: /api/metrics`.
    """
    try:
        from prometheus_client import (
            Counter, Gauge, Histogram, CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
            REGISTRY,
        )
    except ImportError:
        raise HTTPException(
            503,
            "prometheus-client is not installed. Add it to requirements.txt and rebuild.",
        )

    async with get_db() as db:
        # Torrent counts by status
        rows = await db.fetchall(
            "SELECT status, COUNT(*) AS c FROM torrents GROUP BY status"
        )
        by_status = {r["status"]: int(r["c"]) for r in rows}

        # Download file counts by status
        frows = await db.fetchall(
            "SELECT status, COUNT(*) AS c FROM download_files GROUP BY status"
        )
        by_file_status = {r["status"]: int(r["c"]) for r in frows}

        # Total size downloaded (bytes)
        size_row = await db.fetchone(
            "SELECT COALESCE(SUM(size_bytes),0) AS total FROM torrents WHERE status='completed'"
        )
        total_bytes = int((size_row["total"] if size_row else 0) or 0)

    # Build output manually to avoid global registry side-effects on repeated scrapes
    lines: list[str] = []

    def _gauge(name: str, help_text: str, value: float, labels: dict | None = None) -> None:
        lstr = ""
        if labels:
            lstr = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{lstr} {value}")

    _gauge("alldebrid_torrents_total",
           "Number of torrents by status",
           sum(by_status.values()))

    for status, count in by_status.items():
        lines.append(f'alldebrid_torrents_by_status{{status="{status}"}} {count}')

    _gauge("alldebrid_active_downloads",
           "Torrents currently in queued or downloading state",
           by_status.get("queued", 0) + by_status.get("downloading", 0))

    _gauge("alldebrid_completed_downloads",
           "Total torrents completed",
           by_status.get("completed", 0))

    _gauge("alldebrid_error_torrents",
           "Torrents in error state",
           by_status.get("error", 0))

    _gauge("alldebrid_pending_files",
           "download_files rows in pending state (waiting for aria2 slot)",
           by_file_status.get("pending", 0))

    _gauge("alldebrid_sse_subscribers",
           "Number of active SSE connections",
           len(_sse_subscribers))

    _gauge("alldebrid_downloaded_bytes_total",
           "Total bytes downloaded (completed torrents)",
           total_bytes)

    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

# ── Priority Queue ────────────────────────────────────────────────────────────

@router.patch("/torrents/{torrent_id}/priority")
async def set_torrent_priority(torrent_id: int, body: dict):
    """Set the dispatch priority for a torrent.
    Higher priority = dispatched sooner.  Default: 0.
    Body: {"priority": <int>}
    """
    priority = int(body.get("priority") or 0)
    async with get_db() as db:
        row = await db.fetchone("SELECT id FROM torrents WHERE id=?", (torrent_id,))
        if not row:
            raise HTTPException(404, "Torrent not found")
        await db.execute(
            "UPDATE torrents SET priority=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (priority, torrent_id),
        )
        await db.commit()
    _sse_broadcast("torrent_updated", {"torrent_id": torrent_id, "priority": priority})
    return {"ok": True, "torrent_id": torrent_id, "priority": priority}


# ── Rule Engine ───────────────────────────────────────────────────────────────

@router.post("/rules/test")
async def test_rules(body: dict):
    """Dry-run the rule engine against a test context without affecting any torrent.
    Body: {"name": "...", "source": "...", "size_bytes": 0, "label": "..."}
    """
    from services.rules import evaluate as rules_evaluate, _load_rules
    rules = _load_rules()
    actions = rules_evaluate({
        "name":       str(body.get("name") or ""),
        "source":     str(body.get("source") or ""),
        "size_bytes": int(body.get("size_bytes") or 0),
        "label":      str(body.get("label") or ""),
        "priority":   int(body.get("priority") or 0),
    })
    return {"rules_count": len(rules), "actions": actions}


# ── Saved Searches ────────────────────────────────────────────────────────────

@router.get("/saved-searches")
async def list_saved_searches():
    async with get_db() as db:
        rows = await db.fetchall("SELECT * FROM saved_searches ORDER BY id DESC")
    return [dict(r) for r in rows]


@router.post("/saved-searches")
async def create_saved_search(body: dict):
    name  = (body.get("name") or "").strip()
    query = (body.get("query") or "").strip()
    if not name or not query:
        raise HTTPException(400, "name and query are required")
    async with get_db() as db:
        row_id = await db.execute_returning_id(
            """INSERT INTO saved_searches
               (name, query, indexer, category, min_seeders, max_size_gb,
                min_size_gb, regex_filter, auto_add, enabled, interval_minutes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name, query,
                body.get("indexer", ""), body.get("category", ""),
                int(body.get("min_seeders") or 1),
                float(body.get("max_size_gb") or 0),
                float(body.get("min_size_gb") or 0),
                body.get("regex_filter", ""),
                1 if body.get("auto_add") else 0,
                1 if body.get("enabled", True) else 0,
                int(body.get("interval_minutes") or 60),
            ),
        )
        await db.commit()
    return {"ok": True, "id": row_id}


@router.put("/saved-searches/{search_id}")
async def update_saved_search(search_id: int, body: dict):
    async with get_db() as db:
        row = await db.fetchone("SELECT id FROM saved_searches WHERE id=?", (search_id,))
        if not row:
            raise HTTPException(404, "Saved search not found")
        await db.execute(
            """UPDATE saved_searches SET
               name=?, query=?, indexer=?, category=?, min_seeders=?,
               max_size_gb=?, min_size_gb=?, regex_filter=?,
               auto_add=?, enabled=?, interval_minutes=?
               WHERE id=?""",
            (
                body.get("name", ""), body.get("query", ""),
                body.get("indexer", ""), body.get("category", ""),
                int(body.get("min_seeders") or 1),
                float(body.get("max_size_gb") or 0),
                float(body.get("min_size_gb") or 0),
                body.get("regex_filter", ""),
                1 if body.get("auto_add") else 0,
                1 if body.get("enabled", True) else 0,
                int(body.get("interval_minutes") or 60),
                search_id,
            ),
        )
        await db.commit()
    return {"ok": True, "id": search_id}


@router.delete("/saved-searches/{search_id}")
async def delete_saved_search(search_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM saved_searches WHERE id=?", (search_id,))
        await db.commit()
    return {"ok": True}


@router.post("/saved-searches/{search_id}/run")
async def run_saved_search(search_id: int):
    """Manually trigger a saved search run."""
    async with get_db() as db:
        row = await db.fetchone("SELECT * FROM saved_searches WHERE id=?", (search_id,))
        if not row:
            raise HTTPException(404, "Saved search not found")
    results = await _execute_saved_search(dict(row))
    return {"ok": True, "results": results}


async def _execute_saved_search(search: dict) -> dict:
    """Execute a single saved search via Jackett/Prowlarr and optionally auto-add."""
    query = search.get("query", "")
    results_count = 0
    added_count = 0
    try:
        from core.config import get_settings
        cfg = get_settings()
        raw_results: list = []

        # Try Prowlarr first if enabled
        if getattr(cfg, "prowlarr_enabled", False):
            try:
                from services.prowlarr import search as pr_search
                pr_payload = await pr_search(query, limit=50)
                raw_results = pr_payload.get("results", []) if isinstance(pr_payload, dict) else (pr_payload or [])
            except Exception as exc:
                logger.debug("saved_search: Prowlarr failed: %s", exc)

        # Fallback to Jackett
        if not raw_results and getattr(cfg, "jackett_enabled", False):
            try:
                from services.jackett import search as jk_search
                jk_payload = await jk_search(query)
                raw_results = jk_payload.get("results", []) if isinstance(jk_payload, dict) else (jk_payload or [])
            except Exception as exc:
                logger.debug("saved_search: Jackett failed: %s", exc)

        # Filter results
        import re
        min_seeders = int(search.get("min_seeders") or 1)
        max_size = float(search.get("max_size_gb") or 0) * 1024 ** 3
        min_size = float(search.get("min_size_gb") or 0) * 1024 ** 3
        regex_filter = search.get("regex_filter") or ""

        filtered = []
        for r in raw_results:
            if int(r.get("seeders") or 0) < min_seeders:
                continue
            size = int(r.get("size_bytes") or 0)
            if max_size > 0 and size > max_size:
                continue
            if min_size > 0 and size < min_size:
                continue
            if regex_filter:
                try:
                    if not re.search(regex_filter, r.get("title", ""), re.I):
                        continue
                except re.error:
                    pass
            filtered.append(r)

        results_count = len(filtered)

        # Auto-add if configured
        if search.get("auto_add") and filtered:
            for result in filtered[:10]:  # max 10 per run to avoid spam
                magnet = result.get("magnet", "")
                torrent_url = (result.get("torrent_url") or "").strip()
                added = False
                if torrent_url and str(result.get("source") or "").lower() == "jackett":
                    try:
                        from services.jackett import download_torrent_file
                        from services.manager_v2 import manager
                        payload = await download_torrent_file(torrent_url)
                        await manager.add_torrent_file_direct(
                            payload["content"],
                            payload.get("filename") or f"{result.get('title') or 'saved-search'}.torrent",
                            source="saved_search",
                            preferred_hash=(
                                str(result.get("hash") or "").strip().lower()
                                or str(payload.get("infohash") or "").strip().lower()
                                or None
                            ),
                        )
                        added_count += 1
                        added = True
                    except Exception as exc:
                        logger.debug("saved_search torrent-file auto-add failed: %s", sanitize_exception(exc))
                if added:
                    continue
                if not magnet:
                    continue
                try:
                    from services.manager_v2 import manager
                    await manager.add_magnet_direct(magnet, source="saved_search")
                    added_count += 1
                except Exception as exc:
                    logger.debug("saved_search auto-add failed: %s", sanitize_exception(exc))

        # Update last_run_at
        from db.database import get_db as _gdb
        async with _gdb() as db:
            await db.execute(
                "UPDATE saved_searches SET last_run_at=CURRENT_TIMESTAMP WHERE id=?",
                (search["id"],),
            )
            await db.commit()

    except Exception as exc:
        logger.error("saved_search execution error: %s", sanitize_exception(exc))

    return {"results_count": results_count, "added_count": added_count}


# ── Queue Analytics ───────────────────────────────────────────────────────────

@router.get("/analytics")
async def get_analytics(window_hours: int = Query(24, ge=1, le=720)):
    """Return queue performance metrics for the last *window_hours* hours."""
    from services.analytics import get_queue_analytics
    return await get_queue_analytics(window_hours)

# ── Download Profiles ─────────────────────────────────────────────────────────

@router.get("/download-profiles")
async def list_download_profiles():
    """Return the list of configured download profiles."""
    import json as _json
    cfg = load_settings()
    try:
        profiles = _json.loads(getattr(cfg, "download_profiles", None) or "[]")
    except Exception:
        profiles = []
    return {
        "profiles":       profiles if isinstance(profiles, list) else [],
        "active_profile": getattr(cfg, "active_profile", "") or "",
    }


@router.post("/download-profiles/activate")
async def activate_download_profile(body: dict):
    """Activate a profile by name ("" to clear)."""
    name = str(body.get("name") or "")
    current = load_settings()
    updated = current.model_copy(update={"active_profile": name})
    save_settings(updated)
    apply_settings(updated)
    return {"ok": True, "active_profile": name}


# ── Recovery ──────────────────────────────────────────────────────────────────

@router.post("/recovery/run")
async def run_recovery():
    """Manually trigger an auto-recovery pass."""
    from services.recovery import run_recovery_checks
    result = await run_recovery_checks()
    return {"ok": True, "result": result}


# ── Priority ──────────────────────────────────────────────────────────────────

@router.patch("/torrents/{torrent_id}/priority")
async def set_torrent_priority(torrent_id: int, body: dict):
    """Set dispatch priority for a torrent. Higher = processed sooner. Default: 0."""
    priority = int(body.get("priority") or 0)
    async with get_db() as db:
        row = await db.fetchone("SELECT id FROM torrents WHERE id=?", (torrent_id,))
        if not row:
            raise HTTPException(404, "Torrent not found")
        await db.execute(
            "UPDATE torrents SET priority=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (priority, torrent_id),
        )
        await db.commit()
    _sse_broadcast("torrent_updated", {"torrent_id": torrent_id, "priority": priority})
    return {"ok": True, "torrent_id": torrent_id, "priority": priority}

# ── MediaInfo ─────────────────────────────────────────────────────────────────

@router.get("/mediainfo")
async def get_mediainfo_endpoint(path: str = Query(..., description="Local file path")):
    """
    Return technical metadata (codec, resolution, HDR, audio) for a local file.
    Uses ffprobe (preferred) or pymediainfo as fallback.
    Result is cached in-process per file path.
    """
    from pathlib import Path as _Path
    # Security: only allow paths inside configured download folder
    cfg = load_settings()
    dl_root = str(_Path(getattr(cfg, "download_folder", "/download")).resolve())
    resolved = str(_Path(path).resolve())
    if not resolved.startswith(dl_root):
        raise HTTPException(403, "Path outside download folder")
    if not _Path(resolved).is_file():
        raise HTTPException(404, "File not found")
    from services.mediainfo import get_mediainfo
    return await get_mediainfo(resolved)

# ── Generic Webhook ───────────────────────────────────────────────────────────

@router.post("/webhooks/test")
async def test_webhook(body: dict):
    """
    Send a test POST to a webhook URL and return the HTTP status.
    Body: {"url": "https://...", "secret": "..."}
    Read-only — does not modify any torrent data.
    """
    url    = str(body.get("url") or "")
    secret = str(body.get("secret") or "")
    if not url:
        raise HTTPException(400, "url is required")
    from services.webhook_actions import fire, EVENT_COMPLETE
    ok = await fire(
        EVENT_COMPLETE,
        {"id": 0, "name": "Test Torrent", "status": "completed", "source": "manual", "size_bytes": 0},
        url=url, secret=secret or None,
    )
    return {"ok": ok, "url": url}


# ── Historical Learning ───────────────────────────────────────────────────────

@router.get("/stats/learning")
async def get_learning():
    """Return indexer + release-group performance stats from the last 90 days."""
    from services.learning import get_learning_stats
    return await get_learning_stats()

# ── AllDebrid orphan cleanup ───────────────────────────────────────────────────

@router.post("/admin/cleanup-alldebrid-orphans")
async def cleanup_alldebrid_orphans_endpoint():
    """
    Delete from AllDebrid any magnets with error/no-peer status that are not
    tracked by the local DB (or already marked deleted locally).
    Returns the number of magnets removed.
    """
    deleted = await manager.cleanup_alldebrid_orphans()
    return {"ok": True, "deleted": deleted}
