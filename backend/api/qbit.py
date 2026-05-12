"""
qBittorrent v4.3.2 Web API emulation layer.

Allows Sonarr, Radarr, Lidarr and other *arr applications to use AllDebrid-Client
as if it were a standard qBittorrent instance. Configure in *arr:

    Download client: qBittorrent
    Host:            <your-host>
    Port:            <your-port>
    Category:        (any — stored but not used for routing)
    Username/Pass:   (anything — auth handled by AllDebrid-Client's own auth middleware)

Implemented endpoints (qBittorrent WebUI API v4.1/v4.3.2):
  POST /api/v2/auth/login
  POST /api/v2/auth/logout
  GET  /api/v2/app/version
  GET  /api/v2/app/webapiVersion
  GET  /api/v2/app/preferences
  POST /api/v2/app/setPreferences
  GET  /api/v2/torrents/info
  POST /api/v2/torrents/add
  GET  /api/v2/torrents/files
  GET  /api/v2/torrents/properties
  POST /api/v2/torrents/delete
  POST /api/v2/torrents/pause
  POST /api/v2/torrents/resume
  POST /api/v2/torrents/setCategory
  GET  /api/v2/torrents/categories
  GET  /api/v2/transfer/info

Unimplemented endpoints return a safe stub so *arr doesn't abort on them.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

from core.config import get_settings
from db.database import get_db
from services.manager_v2 import manager

logger = logging.getLogger("alldebrid.qbit")

router = APIRouter()

# ── qBit state mapping ────────────────────────────────────────────────────────
# Maps AllDebrid-Client status → qBittorrent state strings.
# Sonarr/Radarr use these to decide whether a torrent is ready to import.
_STATUS_MAP: dict[str, str] = {
    "pending":     "stalledDL",
    "uploading":   "stalledDL",
    "processing":  "stalledDL",
    "ready":       "stalledDL",
    "downloading": "downloading",
    "queued":      "downloading",
    "paused":      "pausedDL",
    "completed":   "uploading",   # "uploading" means seeding in qBit — signals import-ready
    "error":       "error",
    "deleted":     "missingFiles",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _qbit_state(status: str) -> str:
    return _STATUS_MAP.get(status, "unknown")


def _torrent_row_to_qbit(row: dict) -> dict:
    """Convert a torrents DB row into a qBittorrent torrent-info dict."""
    status = str(row.get("status") or "")
    state  = _qbit_state(status)
    size   = int(row.get("size_bytes") or 0)
    prog   = float(row.get("progress") or 0.0)
    completed_bytes = int(size * prog)

    # Build a plausible save_path from config + torrent name
    cfg       = get_settings()
    save_path = str(getattr(cfg, "download_folder", "/downloads") or "/downloads")
    name      = str(row.get("name") or "")

    return {
        "hash":              str(row.get("hash") or ""),
        "name":              name,
        "magnet_uri":        str(row.get("magnet") or ""),
        "size":              size,
        "progress":          prog,
        "dlspeed":           0,
        "upspeed":           0,
        "num_seeds":         0,
        "num_leechs":        0,
        "ratio":             0.0,
        "eta":               8640000,
        "state":             state,
        "category":          str(row.get("source") or ""),
        "tags":              "",
        "super_seeding":     False,
        "force_start":       False,
        "save_path":         save_path,
        "content_path":      str(row.get("local_path") or ""),
        "downloaded":        completed_bytes,
        "uploaded":          0,
        "downloaded_session": completed_bytes,
        "uploaded_session":   0,
        "amount_left":        max(0, size - completed_bytes),
        "added_on":           0,
        "completion_on":      0,
        "tracker":            "",
        "trackers_count":     0,
        "availability":       1.0 if status == "completed" else 0.0,
        "max_ratio":          -1,
        "max_seeding_time":   -1,
        "auto_tmm":           False,
        "time_active":        0,
        "seeding_time":       0,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

@router.post("/auth/login")
async def qbit_login():
    """Accept any credentials — AllDebrid-Client's own auth middleware handles real auth."""
    return PlainTextResponse("Ok.")


@router.post("/auth/logout")
async def qbit_logout():
    return PlainTextResponse("Ok.")


# ── App ───────────────────────────────────────────────────────────────────────

@router.get("/app/version")
async def qbit_app_version():
    return PlainTextResponse("v4.3.2")


@router.get("/app/webapiVersion")
async def qbit_webapi_version():
    return PlainTextResponse("2.8.3")


@router.get("/app/buildInfo")
async def qbit_build_info():
    return {
        "qt":       "6.4.2",
        "libtorrent": "1.2.19.0",
        "boost":    "1.81.0",
        "openssl":  "3.0.8",
        "bitness":  64,
    }


@router.get("/app/preferences")
async def qbit_preferences():
    cfg = get_settings()
    return {
        "save_path":                  str(getattr(cfg, "download_folder", "/downloads") or "/downloads"),
        "temp_path_enabled":          False,
        "temp_path":                  "/downloads/temp",
        "max_active_downloads":       int(getattr(cfg, "max_concurrent_downloads", 3) or 3),
        "max_active_torrents":        int(getattr(cfg, "max_concurrent_downloads", 3) or 3),
        "max_active_uploads":         0,
        "queueing_enabled":           True,
        "download_limit":             0,
        "upload_limit":               0,
        "dht":                        False,
        "pex":                        False,
        "lsd":                        False,
        "encryption":                 0,
        "anonymous_mode":             False,
        "proxy_type":                 0,
        "web_ui_port":                8080,
        "web_ui_username":            "admin",
        "bypass_local_auth":          True,
        "bypass_auth_subnet_whitelist_enabled": False,
        "add_trackers_enabled":       False,
        "create_subfolder_enabled":   True,
        "incomplete_files_ext":       False,
    }


@router.post("/app/setPreferences")
async def qbit_set_preferences(json_data: Optional[str] = Form(None, alias="json")):
    """Silently accept preference changes — we don't apply them."""
    return PlainTextResponse("Ok.")


# ── Torrents ──────────────────────────────────────────────────────────────────

@router.get("/torrents/info")
async def qbit_torrents_info(
    filter:   Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    tag:      Optional[str] = Query(None),
    sort:     Optional[str] = Query(None),
    reverse:  bool          = Query(False),
    limit:    int           = Query(0),
    offset:   int           = Query(0),
    hashes:   Optional[str] = Query(None),
):
    """Return all (or filtered) torrents as qBittorrent torrent-info objects."""
    clauses = []
    params: list = []

    # Hash filter (pipe-separated)
    if hashes:
        hash_list = [h.strip().lower() for h in hashes.split("|") if h.strip()]
        if hash_list:
            placeholders = ",".join("?" * len(hash_list))
            clauses.append(f"hash IN ({placeholders})")
            params.extend(hash_list)

    # State filter
    if filter and filter != "all":
        if filter == "downloading":
            clauses.append("status IN ('queued','downloading')")
        elif filter == "completed":
            clauses.append("status = 'completed'")
        elif filter == "paused":
            clauses.append("status = 'paused'")
        elif filter in ("stalled", "stalled_downloading"):
            clauses.append("status IN ('pending','uploading','processing','ready')")
        elif filter == "errored":
            clauses.append("status = 'error'")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    query = f"SELECT * FROM torrents {where} ORDER BY id ASC"

    async with get_db() as db:
        rows = await db.fetchall(query, params)

    result = [_torrent_row_to_qbit(dict(r)) for r in rows]

    if limit > 0:
        result = result[offset:offset + limit]
    elif offset:
        result = result[offset:]

    return result


@router.post("/torrents/add")
async def qbit_torrents_add(
    request:  Request,
    torrents: Optional[UploadFile] = File(None),
    urls:     Optional[str]        = Form(None),
    magnet:   Optional[str]        = Form(None),
    savepath: Optional[str]        = Form(None),
    category: Optional[str]        = Form(None),
    tags:     Optional[str]        = Form(None),
):
    """Add a torrent by magnet link or .torrent file upload."""
    magnet_link = (magnet or "").strip() or (urls or "").strip()

    if magnet_link:
        try:
            await manager.add_magnet_direct(magnet_link, source="qbit")
            logger.info("qBit API: added magnet via URLs/magnet param")
            return PlainTextResponse("Ok.")
        except Exception as exc:
            logger.warning("qBit API: magnet add failed: %s", exc)
            return PlainTextResponse("Fails.", status_code=400)

    if torrents:
        try:
            data = await torrents.read()
            await manager.add_torrent_file_direct(data, source="qbit")
            logger.info("qBit API: added torrent file %s", torrents.filename)
            return PlainTextResponse("Ok.")
        except Exception as exc:
            logger.warning("qBit API: torrent file add failed: %s", exc)
            return PlainTextResponse("Fails.", status_code=400)

    # Fallback: try to read raw body as magnet
    try:
        body = await request.body()
        if body:
            from urllib.parse import parse_qs
            parsed = parse_qs(body.decode("utf-8", errors="replace"))
            raw_urls = parsed.get("urls", parsed.get("magnet", []))
            if raw_urls:
                await manager.add_magnet_direct(raw_urls[0].strip(), source="qbit")
                return PlainTextResponse("Ok.")
    except Exception as exc:
        logger.debug("qBit add: %s", exc)

    return PlainTextResponse("Fails.", status_code=400)


@router.get("/torrents/files")
async def qbit_torrent_files(hash: str = Query(...)):
    """Return the download_files for a torrent as qBit file objects."""
    hash_lower = hash.lower()
    async with get_db() as db:
        torrent = await db.fetchone(
            "SELECT id, name, local_path, download_folder FROM torrents WHERE hash=?",
            (hash_lower,),
        )
        if not torrent:
            return []
        files = await db.fetchall(
            "SELECT filename, size_bytes, status, progress FROM download_files WHERE torrent_id=? AND blocked=0",
            (torrent["id"],),
        )

    if not files:
        # Single-file torrent with no DB file rows — synthesise one entry
        return [{
            "index":        0,
            "name":         str(torrent["name"] or hash_lower),
            "size":         0,
            "progress":     1.0 if torrent.get("status") == "completed" else 0.0,
            "priority":     1,
            "is_seed":      False,
            "piece_range":  [0, 0],
            "availability": 1.0,
        }]

    return [
        {
            "index":        i,
            "name":         str(f["filename"] or ""),
            "size":         int(f["size_bytes"] or 0),
            "progress":     float(f["progress"] or (1.0 if f["status"] == "completed" else 0.0)),
            "priority":     1,
            "is_seed":      False,
            "piece_range":  [0, 0],
            "availability": 1.0,
        }
        for i, f in enumerate(files)
    ]


@router.get("/torrents/properties")
async def qbit_torrent_properties(hash: str = Query(...)):
    """Return extended properties for a single torrent."""
    hash_lower = hash.lower()
    async with get_db() as db:
        row = await db.fetchone("SELECT * FROM torrents WHERE hash=?", (hash_lower,))
    if not row:
        return Response(status_code=404)
    q = _torrent_row_to_qbit(dict(row))
    return {
        **q,
        "comment":           "",
        "creation_date":     0,
        "piece_size":        0,
        "pieces_have":       0,
        "pieces_num":        0,
        "reannounce":        0,
        "seeds":             0,
        "peers":             0,
        "seeds_total":       0,
        "peers_total":       0,
        "nb_connections":    0,
        "nb_connections_limit": 100,
        "share_ratio":       0.0,
        "addition_date":     0,
        "completion_date":   0,
        "dl_limit":          0,
        "up_limit":          0,
        "dl_speed":          0,
        "up_speed":          0,
        "dl_speed_avg":      0,
        "up_speed_avg":      0,
        "eta":               8640000,
        "connect_count":     0,
        "is_private":        False,
    }


@router.post("/torrents/delete")
async def qbit_torrents_delete(
    hashes:      str  = Form(...),
    deleteFiles: bool = Form(False),
):
    """Delete torrents by pipe-separated hash list."""
    hash_list = [h.strip().lower() for h in hashes.split("|") if h.strip()]
    deleted = 0
    for hash_val in hash_list:
        try:
            async with get_db() as db:
                row = await db.fetchone("SELECT id FROM torrents WHERE hash=?", (hash_val,))
            if row:
                await manager.delete_torrent(row["id"], delete_files=deleteFiles)
                deleted += 1
        except Exception as exc:
            logger.warning("qBit API: delete failed for %s: %s", hash_val[:64].replace("\n",""), exc)
    return PlainTextResponse("Ok.")


@router.post("/torrents/pause")
async def qbit_torrents_pause(hashes: str = Form(...)):
    hash_list = [h.strip().lower() for h in hashes.split("|") if h.strip()]
    for hash_val in hash_list:
        try:
            async with get_db() as db:
                row = await db.fetchone("SELECT id FROM torrents WHERE hash=?", (hash_val,))
            if row:
                await manager.pause_torrent(row["id"])
        except Exception as exc:
            logger.warning("qBit API: pause failed for %s: %s", hash_val[:64].replace("\n",""), exc)
    return PlainTextResponse("Ok.")


@router.post("/torrents/resume")
async def qbit_torrents_resume(hashes: str = Form(...)):
    hash_list = [h.strip().lower() for h in hashes.split("|") if h.strip()]
    for hash_val in hash_list:
        try:
            async with get_db() as db:
                row = await db.fetchone("SELECT id FROM torrents WHERE hash=?", (hash_val,))
            if row:
                await manager.resume_torrent(row["id"])
        except Exception as exc:
            logger.warning("qBit API: resume failed for %s: %s", hash_val[:64].replace("\n",""), exc)
    return PlainTextResponse("Ok.")


@router.post("/torrents/setCategory")
async def qbit_set_category(
    hashes:   str = Form(...),
    category: str = Form(""),
):
    """Store the category on the torrent's source field (best-effort)."""
    hash_list = [h.strip().lower() for h in hashes.split("|") if h.strip()]
    if category and hash_list:
        async with get_db() as db:
            for h in hash_list:
                await db.execute(
                    "UPDATE torrents SET source=? WHERE hash=?",
                    (category, h),
                )
            await db.commit()
    return PlainTextResponse("Ok.")


@router.get("/torrents/categories")
async def qbit_categories():
    """Return an empty categories dict — we don't use qBit categories internally."""
    return {}


@router.post("/torrents/addCategory")
async def qbit_add_category(category: str = Form(""), savePath: str = Form("")):
    return PlainTextResponse("Ok.")


@router.post("/torrents/editCategory")
async def qbit_edit_category(category: str = Form(""), savePath: str = Form("")):
    return PlainTextResponse("Ok.")


@router.post("/torrents/removeCategories")
async def qbit_remove_categories(categories: str = Form("")):
    return PlainTextResponse("Ok.")


@router.post("/torrents/addTags")
async def qbit_add_tags(hashes: str = Form(""), tags: str = Form("")):
    return PlainTextResponse("Ok.")


@router.post("/torrents/removeTags")
async def qbit_remove_tags(hashes: str = Form(""), tags: str = Form("")):
    return PlainTextResponse("Ok.")


@router.get("/torrents/tags")
async def qbit_tags():
    return []


# ── Transfer ──────────────────────────────────────────────────────────────────

@router.get("/transfer/info")
async def qbit_transfer_info():
    """Return basic transfer statistics."""
    try:
        aria2_status = await manager.aria2().get_global_stat()
        dl_speed = int(aria2_status.get("downloadSpeed", 0) or 0)
    except Exception:
        dl_speed = 0

    return {
        "dl_info_speed":    dl_speed,
        "dl_info_data":     0,
        "ul_info_speed":    0,
        "ul_info_data":     0,
        "dl_rate_limit":    0,
        "ul_rate_limit":    0,
        "dht_nodes":        0,
        "connection_status": "connected",
    }


# ── Sync ──────────────────────────────────────────────────────────────────────

@router.get("/sync/maindata")
async def qbit_sync_maindata(rid: int = Query(0)):
    """Minimal maindata response — full state, no delta."""
    torrents_info = await qbit_torrents_info()
    torrents_dict = {t["hash"]: t for t in torrents_info}
    return {
        "rid":            rid + 1,
        "full_update":    True,
        "torrents":       torrents_dict,
        "categories":     {},
        "tags":           [],
        "server_state":   await qbit_transfer_info(),
    }


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/search/plugins")
async def qbit_search_plugins():
    return []


@router.post("/search/start")
async def qbit_search_start():
    return {"id": 1}


@router.get("/search/status")
async def qbit_search_status():
    return [{"id": 1, "status": "Running", "total": 0}]


@router.get("/search/results")
async def qbit_search_results():
    return {"results": [], "status": "Stopped", "total": 0}
