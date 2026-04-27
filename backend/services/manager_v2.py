import asyncio
import base64
import logging
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

import aiofiles
import aiohttp
import aiosqlite

from core.config import AppSettings, get_settings
from db.database import DB_PATH, get_db
from services.alldebrid import AllDebridService, flatten_files
from services.aria2 import Aria2Service
from services.notifications import NotificationService

logger = logging.getLogger("alldebrid.manager")

READY_CODE = 4
ERROR_CODES = set(range(5, 16))

# AllDebrid API rate limiter — shared across all manager instances
_ad_rate_sem: asyncio.Semaphore | None = None
_ad_rate_lock = asyncio.Lock()


class TransientAllDebridStateError(Exception):
    """Raised when AllDebrid is temporarily inconsistent but not actually failed."""


async def _get_ad_semaphore() -> asyncio.Semaphore:
    """Returns a semaphore that enforces alldebrid_rate_limit_per_minute."""
    global _ad_rate_sem
    async with _ad_rate_lock:
        try:
            limit = int(get_settings().alldebrid_rate_limit_per_minute)
        except Exception:
            limit = 60
        if limit <= 0:
            limit = 1_000_000
        if _ad_rate_sem is None or _ad_rate_sem._value != limit:
            _ad_rate_sem = asyncio.Semaphore(limit)
    return _ad_rate_sem
MAX_FILE_RETRIES = 3
READY_FILE_RETRIES = 5
PROVIDER_FAILURE_THRESHOLD = 6


def extract_hash(magnet: str) -> Optional[str]:
    match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet, re.I)
    if not match:
        return None
    value = match.group(1)
    if len(value) == 32:
        try:
            value = base64.b32decode(value.upper()).hex()
        except Exception:
            return None
    return value.lower()


def safe_name(value: str) -> str:
    """Sanitise a torrent/folder name for use as a filesystem path component.
    Replaces forbidden characters, strips leading dots to prevent '..'-style
    names, and ensures the result is non-empty.
    """
    sanitised = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)[:200].strip()
    # Remove leading dots (prevents names like '..' or '...') while keeping
    # hidden-file dots elsewhere (e.g. 'Movie.2024.mkv' → unchanged)
    sanitised = sanitised.lstrip(".")
    return sanitised or "download"


def safe_rel_path(value: str) -> Path:
    raw = str(PurePosixPath(value.replace("\\", "/"))).strip("/")
    cleaned = [safe_name(part) for part in raw.split("/") if part not in {"", ".", ".."}]
    if not cleaned:
        return Path("download.bin")
    return Path(*cleaned)


def is_blocked(filename: str, cfg: AppSettings, size_bytes: int = 0) -> Tuple[bool, str]:
    if not cfg.filters_enabled:
        return False, ""
    ext = Path(filename).suffix.lower()
    if ext in [entry.lower() for entry in cfg.blocked_extensions]:
        return True, f"extension {ext}"
    for keyword in cfg.blocked_keywords:
        if keyword.lower() in filename.lower():
            return True, f"keyword '{keyword}'"
    if cfg.min_file_size_mb > 0 and size_bytes > 0 and size_bytes < cfg.min_file_size_mb * 1024 * 1024:
        return True, f"smaller than {cfg.min_file_size_mb} MB"
    return False, ""


def fmt_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size or 0)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}"


def _size_sum(items: List[dict]) -> int:
    return sum(int(item.get("size_bytes", 0) or 0) for item in items)


def _terminal_torrent_status(status: str) -> bool:
    return status in {"completed", "deleted", "error"}


def _aria2_status_rank(status: str) -> int:
    order = {
        "complete": 0,
        "removed": 1,
        "active": 2,
        "waiting": 3,
        "paused": 4,
        "error": 5,
    }
    return order.get((status or "").strip().lower(), 99)


def _normalize_aria2_path(path: str) -> str:
    if not path:
        return ""
    return str(PurePosixPath(str(path).replace("\\", "/"))).strip()


def normalize_provider_state(magnet: Dict) -> Dict[str, object]:
    code = int(magnet.get("statusCode", 0) or 0)
    size = int(magnet.get("size", 0) or 0)
    downloaded = int(magnet.get("downloaded", 0) or 0)
    progress = (downloaded / size * 100) if size > 0 else 0.0

    if code == READY_CODE:
        provider_status = "ready"
        local_status = "ready"
    elif code in ERROR_CODES:
        provider_status = "error"
        local_status = "error"
    elif code <= 0:
        provider_status = "queued"
        local_status = "uploading"
    else:
        provider_status = "processing"
        local_status = "processing"

    return {
        "provider_status": provider_status,
        "local_status": local_status,
        "status_code": code,
        "progress": progress,
        "size_bytes": size,
        "message": str(magnet.get("status", "") or ""),
    }


async def _retry_async(fn, *args, attempts: int = MAX_FILE_RETRIES, delay: float = 1.0):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn(*args)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(delay * attempt)
    raise last_error


MAX_CONCURRENT_AD_UPLOADS = 5


def _file_in_use(path: Path) -> bool:
    """
    Returns True if any process currently has the file open (write or read).
    Works by scanning /proc/*/fd symlinks — available on Linux without lsof.
    Returns False if /proc is unavailable (non-Linux) or on any error.
    """
    try:
        target = str(path.resolve())
        proc_fd = Path("/proc")
        if not proc_fd.exists():
            return False
        for pid_dir in proc_fd.iterdir():
            if not pid_dir.name.isdigit():
                continue
            fd_dir = pid_dir / "fd"
            try:
                for fd in fd_dir.iterdir():
                    try:
                        if str(fd.resolve()) == target:
                            return True
                    except OSError:
                        continue
            except (PermissionError, FileNotFoundError):
                continue
    except Exception:
        pass
    return False


class TorrentManager:
    def __init__(self):
        self._ad: Optional[AllDebridService] = None
        self._aria2: Optional[Aria2Service] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._upload_sem: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_AD_UPLOADS)
        self._active: Set[int] = set()
        self._processing_files: Set[str] = set()
        self._failed_files: Set[str] = set()
        self._aria2_dispatch_lock = asyncio.Lock()

    def is_paused(self) -> bool:
        return bool(get_settings().paused)

    def reset_services(self):
        self._ad = None
        self._aria2 = None
        self._sem = None

    def ad(self) -> AllDebridService:
        if self._ad is None:
            cfg = get_settings()
            self._ad = AllDebridService(cfg.alldebrid_api_key, cfg.alldebrid_agent)
        return self._ad

    def aria2(self) -> Aria2Service:
        if self._aria2 is None:
            cfg = get_settings()
            self._aria2 = Aria2Service(cfg.aria2_url, cfg.aria2_secret, cfg.aria2_operation_timeout_seconds)
        return self._aria2

    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(get_settings().max_concurrent_downloads)
        return self._sem

    def notify(self) -> NotificationService:
        cfg = get_settings()
        return NotificationService(
            webhook_url=cfg.discord_webhook_url,
            added_webhook_url=getattr(cfg, "discord_webhook_added", ""),
        )

    async def _notify_provider_error(
        self,
        name: str,
        reason: str,
        *,
        context: str = "",
        source: str = "AllDebrid",
        provider: str = "AllDebrid",
        alldebrid_id: str = "",
        status_code: int | str | None = None,
    ) -> None:
        cfg = get_settings()
        if not getattr(cfg, "discord_notify_error", False):
            return
        await self.notify().send_error(
            name,
            reason=reason,
            context=context,
            source=source,
            provider=provider,
            alldebrid_id=str(alldebrid_id or ""),
            status_code="" if status_code is None else str(status_code),
        )

    def download_client_name(self) -> str:
        # Direct download mode has been removed — aria2 is the only supported client
        return "aria2"

    def _watch_error_dir(self, watch: Path) -> Path:
        return watch / "error"

    def _move_watch_file_to_error(self, file_path: Path, watch: Path) -> Optional[Path]:
        try:
            error_dir = self._watch_error_dir(watch)
            error_dir.mkdir(parents=True, exist_ok=True)
            destination = error_dir / file_path.name
            if destination.exists():
                stem = file_path.stem
                suffix = file_path.suffix
                counter = 1
                while True:
                    candidate = error_dir / f"{stem}_{counter}{suffix}"
                    if not candidate.exists():
                        destination = candidate
                        break
                    counter += 1
            shutil.move(str(file_path), str(destination))
            return destination
        except Exception as move_exc:
            logger.error("Unable to move failed watch file [%s] to error folder: %s", file_path.name, move_exc)
            return None

    async def scan_watch_folder(self):
        if self.is_paused():
            return
        cfg = get_settings()
        watch = Path(cfg.watch_folder)
        processed = Path(cfg.processed_folder)
        error_dir = self._watch_error_dir(watch)
        try:
            watch.mkdir(parents=True, exist_ok=True)
            processed.mkdir(parents=True, exist_ok=True)
            error_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error("Watch folder inaccessible: %s", exc)
            return

        for file_path in list(watch.iterdir()):
            if file_path.is_dir():
                if file_path.resolve() == error_dir.resolve():
                    continue
                continue
            key = str(file_path.resolve())
            if key in self._processing_files:
                continue
            suffix = file_path.suffix.lower()
            if suffix not in {".torrent", ".magnet", ".txt"}:
                continue
            # Retry previously failed files on every cycle — the error may be transient
            # (e.g. API key not yet configured, network blip). Remove from failed set
            # so it gets a fresh attempt.
            self._failed_files.discard(key)
            self._processing_files.add(key)
            try:
                if suffix == ".torrent":
                    await self._handle_torrent(file_path, processed)
                else:
                    await self._handle_magnet_file(file_path, processed)
            except Exception as exc:
                logger.error("Watch [%s]: %s", file_path.name, exc)
                self._failed_files.add(key)
                moved_to = self._move_watch_file_to_error(file_path, watch)
                # Write to DB events so the error shows in the UI
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO events (torrent_id, level, message) VALUES (NULL, 'error', ?)",
                        (
                            f"Watch folder error [{file_path.name}]: {exc}"
                            + (f" -> moved to {moved_to}" if moved_to else ""),
                        ),
                    )
                    await db.commit()
            finally:
                self._processing_files.discard(key)

    async def _handle_torrent(self, path: Path, processed: Path):
        if not get_settings().alldebrid_api_key:
            return
        # Check whether another process (e.g. a Sonarr/Radarr container) still
        # has the file open for writing before we read it.
        # Uses /proc/*/fd which is available on Linux/Docker without lsof.
        if _file_in_use(path):
            logger.debug("Watch [%s]: file still open by another process, will retry next cycle", path.name)
            return
        content = path.read_bytes()
        if not content:
            raise ValueError("Empty torrent file")
        async with self._upload_sem:
            result = await self.ad().upload_torrent_file(content, path.name)
        ad_id = str(result.get("id", ""))
        name = result.get("name") or result.get("filename") or path.stem
        hash_value = result.get("hash", ad_id).lower()
        logger.info("Uploaded torrent %s (ad_id=%s)", name, ad_id)
        await self._upsert(hash_value, None, name, ad_id, "watch_torrent")
        if get_settings().discord_notify_added:
            await self.notify().send_added(name, source="watch_torrent", alldebrid_id=ad_id)
        shutil.move(str(path), str(processed / path.name))

    async def _handle_magnet_file(self, path: Path, processed: Path):
        # Check whether another process still has the file open for writing.
        if _file_in_use(path):
            logger.debug("Watch [%s]: file still open by another process, will retry next cycle", path.name)
            return
        content = path.read_text(errors="ignore")
        magnets = [line.strip() for line in content.splitlines() if line.strip().startswith("magnet:")]
        if not magnets:
            self._failed_files.add(str(path.resolve()))
            return
        for magnet in magnets:
            hash_value = extract_hash(magnet)
            if hash_value:
                await self._add_magnet(magnet, hash_value, "watch_file")
        shutil.move(str(path), str(processed / path.name))

    async def add_magnet_direct(self, magnet: str, source: str = "manual") -> dict:
        if self.is_paused():
            raise Exception("Processing is paused")
        hash_value = extract_hash(magnet)
        if not hash_value:
            raise ValueError("Invalid magnet: no btih hash found")
        return await self._add_magnet(magnet, hash_value, source)

    async def add_torrent_file_direct(
        self,
        file_bytes: bytes,
        filename: str,
        source: str = "manual",
        preferred_hash: Optional[str] = None,
    ) -> dict:
        if self.is_paused():
            raise Exception("Processing is paused")
        if not get_settings().alldebrid_api_key:
            raise Exception("AllDebrid API key not configured")
        if not file_bytes:
            raise ValueError("Empty torrent file")

        async with self._upload_sem:
            result = await self.ad().upload_torrent_file(file_bytes, filename or "upload.torrent")

        ad_id = str(result.get("id", ""))
        name = result.get("name") or result.get("filename") or Path(filename or "upload.torrent").stem
        hash_value = str(preferred_hash or result.get("hash", ad_id) or ad_id).strip().lower()
        logger.info("Torrent file uploaded %s (ad_id=%s)", name, ad_id)
        row = await self._upsert(hash_value, None, name, ad_id, source)
        if get_settings().discord_notify_added:
            await self.notify().send_added(name, source=source, alldebrid_id=ad_id)
        return row

    async def _add_magnet(self, magnet: str, hash_value: str, source: str) -> dict:
        async with get_db() as db:
            cur = await db.execute("SELECT * FROM torrents WHERE hash=?", (hash_value,))
            existing = await cur.fetchone()
            if existing and existing["status"] in ("uploading", "processing", "queued", "downloading", "ready", "completed"):
                return dict(existing)

        async with self._upload_sem:
            result = await self.ad().upload_magnet(magnet)
        ad_id = str(result.get("id", ""))
        name = result.get("name") or result.get("filename") or hash_value[:16]
        normalized_hash = result.get("hash", hash_value).lower()
        logger.info("Magnet uploaded %s (ad_id=%s)", name, ad_id)
        row = await self._upsert(normalized_hash, magnet, name, ad_id, source)
        if get_settings().discord_notify_added:
            await self.notify().send_added(name, source=source, alldebrid_id=ad_id)
        return row

    async def _upsert(self, hash_value: str, magnet: Optional[str], name: str, ad_id: str, source: str) -> dict:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO torrents
                   (hash, magnet, name, alldebrid_id, status, source, provider_status, download_client)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(hash) DO UPDATE SET
                     magnet=COALESCE(excluded.magnet, torrents.magnet),
                     alldebrid_id=excluded.alldebrid_id,
                     name=excluded.name,
                     source=excluded.source,
                     status='uploading',
                     provider_status='queued',
                     updated_at=CURRENT_TIMESTAMP""",
                (hash_value, magnet, name, ad_id, "uploading", source, "queued", self.download_client_name()),
            )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) SELECT id,'info',? FROM torrents WHERE hash=?",
                (f"Uploaded to AllDebrid (id={ad_id})", hash_value),
            )
            await db.commit()
            row = await (await db.execute("SELECT * FROM torrents WHERE hash=?", (hash_value,))).fetchone()
        return dict(row) if row else {}

    async def full_alldebrid_sync(self) -> int:
        """
        Full reconciliation: fetches all magnets from AllDebrid and syncs
        every known torrent — including those marked 'completed' or 'error'.

        Returns number of torrents updated.

        This catches cases where:
        - A torrent is 'ready' on AllDebrid but locally stuck as 'error'
        - A torrent is in an unexpected state after restart
        - 100+ queued torrents that were never picked up
        """
        if self.is_paused() or not get_settings().alldebrid_api_key:
            return 0

        try:
            all_magnets = await self.ad().get_magnet_status()
        except Exception as exc:
            logger.warning("full_alldebrid_sync: could not fetch magnets: %s", exc)
            return 0

        if not all_magnets:
            return 0

        # Index by alldebrid_id
        ad_by_id: dict = {str(m.get("id", "")): m for m in all_magnets}

        # Fetch all torrents that have an alldebrid_id (any status)
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT id, name, alldebrid_id, status, provider_status, provider_status_code, polling_failures
                   FROM torrents
                   WHERE alldebrid_id IS NOT NULL AND alldebrid_id != ''"""
            )

        updated = 0
        for row in rows:
            ad_id = str(row["alldebrid_id"])
            magnet = ad_by_id.get(ad_id)

            if not magnet:
                # No longer on AllDebrid — mark deleted if not already terminal
                if row["status"] not in ("completed", "deleted", "error"):
                    logger.info("full_alldebrid_sync: magnet %s gone from AllDebrid → deleted", ad_id)
                    await self._set_deleted(row["id"], "Magnet no longer exists on AllDebrid")
                    updated += 1
                continue

            normalized = normalize_provider_state(magnet)
            provider_status = normalized["provider_status"]
            local_status = row["status"]

            # Ready on AllDebrid but locally stuck — trigger download
            # NEVER restart a torrent that is already downloading/queued in aria2.
            # Those are handled by _dispatch_pending_aria2_queue / reconcile_aria2_on_startup.
            _restartable = ("error", "pending", "uploading", "processing", "ready")
            if provider_status == "ready" and local_status in _restartable:
                logger.info(
                    "full_alldebrid_sync: torrent %s (local=%s) is ready on AllDebrid → starting download",
                    row["id"], local_status,
                )
                async with get_db() as db:
                    await db.execute(
                        """UPDATE torrents
                           SET status='ready', error_message=NULL, polling_failures=0,
                               provider_status=?, provider_status_code=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (provider_status, int(normalized["status_code"]), row["id"]),
                    )
                    await db.commit()
                name = magnet.get("filename") or magnet.get("name") or row["name"]
                asyncio.create_task(self._start_download(row["id"], ad_id, str(name)))
                updated += 1
            elif provider_status == "ready" and local_status in ("queued", "downloading", "paused"):
                # Already in progress locally — do not restart. Just keep provider_status in sync.
                try:
                    await self._apply_provider_update(row, magnet, normalized)
                    updated += 1
                except Exception as exc:
                    logger.error("full_alldebrid_sync: update failed for %s: %s", ad_id, exc)

            elif local_status not in ("completed", "deleted") and provider_status != (row["provider_status"] or ""):
                # Status changed — apply update
                try:
                    await self._apply_provider_update(row, magnet, normalized)
                    updated += 1
                except Exception as exc:
                    logger.error("full_alldebrid_sync: update failed for %s: %s", ad_id, exc)

        if updated:
            logger.info("full_alldebrid_sync: %d torrents updated", updated)
        return updated

    async def sync_alldebrid_status(self):
        if self.is_paused() or not get_settings().alldebrid_api_key:
            return

        async with get_db() as db:
            rows = await (
                await db.execute(
                    """SELECT id, name, alldebrid_id, status, provider_status, provider_status_code, polling_failures
                       FROM torrents
                       WHERE alldebrid_id IS NOT NULL AND alldebrid_id != ''
                         AND status NOT IN ('completed', 'deleted')"""
                )
            ).fetchall()

        for row in rows:
            try:
                magnets = await self.ad().get_magnet_status(str(row["alldebrid_id"]))
                if not magnets:
                    await self._increment_poll_failure(row["id"], row["name"], "No magnet data returned from AllDebrid")
                    continue
                magnet = magnets[0]
                normalized = normalize_provider_state(magnet)
                await self._apply_provider_update(row, magnet, normalized)
            except Exception as exc:
                if "MAGNET_INVALID_ID" in str(exc):
                    await self._set_deleted(row["id"], "Magnet no longer exists on AllDebrid")
                else:
                    logger.error("Status poll failed for %s: %s", row["alldebrid_id"], exc)
                    await self._increment_poll_failure(row["id"], row["name"], str(exc))

        await self.sync_download_clients()

    async def deep_sync_aria2_finished(self):
        """
        API-based deep sync for aria2 downloads.

        Runs against the aria2 JSON-RPC API — no filesystem access.

        For every download_file record in pending/queued/downloading/paused:

        1. Look up the GID via aria2.tellStatus. If the GID is gone or stale,
           fall back to matching by URI (download_url) across all active/waiting/
           stopped entries using _build_aria2_indexes().

        2. Based on the aria2 status:
           - complete   → mark download_file as 'completed', remove GID from aria2
           - error      → restart the download inside aria2 (removeDownloadResult +
                          addUri with same URL), update GID in DB, send Discord webhook
           - active     → update progress (completedLength/totalLength)
           - waiting/paused → keep current DB status, update size if available

        3. After processing all files, finalize torrents where all files are done.
        """
        if self.is_paused() or self.download_client_name() != "aria2":
            return

        # Fetch full aria2 state once — avoid hammering RPC per file
        try:
            all_downloads = await self._aria2_get_all()
        except Aria2ConnectionError:
            logger.warning("deep_sync: aria2 not reachable, skipping")
            return

        by_gid, uri_to_dl, path_to_dl = self._build_aria2_indexes(all_downloads)

        async with get_db() as db:
            rows = await (await db.execute(
                """SELECT f.id AS file_id, f.torrent_id, f.local_path,
                          f.size_bytes, f.download_id, f.download_url, f.filename,
                          f.status,
                          t.name AS torrent_name, t.alldebrid_id, t.status AS torrent_status
                   FROM download_files f
                   JOIN torrents t ON t.id = f.torrent_id
                   WHERE f.download_client = 'aria2'
                     AND f.blocked = 0
                     AND f.status IN ('pending', 'queued', 'downloading', 'paused', 'error')
                     AND t.status NOT IN ('completed', 'deleted')"""
            )).fetchall()

        if not rows:
            logger.info("deep_sync_aria2_finished: no active files to check")
            return

        touched: Set[int] = set()
        completed_count = 0
        restarted_count = 0
        cfg = get_settings()

        for row in rows:
            gid = str(row["download_id"] or "").strip()
            url = str(row["download_url"] or "").strip()
            file_id = row["file_id"]
            torrent_id = row["torrent_id"]

            # ── Step 1: find the aria2 entry ────────────────────────────────
            dl = by_gid.get(gid) if gid else None

            if dl is None and url:
                # GID stale or missing — try to find via URI
                dl = uri_to_dl.get(url)
                if dl:
                    # Update stale GID in DB
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET download_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dl.gid, file_id),
                        )
                        await db.commit()
                    logger.info(
                        "deep_sync: updated stale GID %s → %s for file %s (torrent %s)",
                        gid or "(none)", dl.gid, file_id, torrent_id,
                    )

            if dl is None:
                # Not found in aria2 at all — skip (can't act without API info)
                logger.debug(
                    "deep_sync: no aria2 entry for file %s (torrent %s, gid=%s)",
                    file_id, torrent_id, gid or "none",
                )
                continue

            # ── Step 2: act on aria2 status ──────────────────────────────────
            if dl.status == "complete":
                # aria2 says done — mark completed
                async with get_db() as db:
                    await db.execute(
                        "UPDATE download_files SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (file_id,),
                    )
                    await db.commit()
                await self.aria2().remove(dl.gid)
                touched.add(torrent_id)
                completed_count += 1
                logger.info(
                    "deep_sync: complete → torrent %s file %s (%s)",
                    torrent_id, file_id, row["filename"],
                )

            elif dl.status == "removed":
                logger.info(
                    "deep_sync: aria2 job was removed for torrent %s file %s (%s) â€” leaving recovery to the regular sync loop",
                    torrent_id, file_id, row["filename"],
                )
                continue
            elif dl.status == "error":
                # aria2 reports error — check retry count before restarting
                reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                max_retries = int(getattr(cfg, "aria2_error_retry_count", 3))
                current_retry = int(row.get("retry_count") or 0)

                await self.aria2().remove(dl.gid)

                if current_retry < max_retries:
                    # Still have retries left — restart
                    logger.warning(
                        "deep_sync: aria2 error torrent %s file %s (retry %d/%d) — restarting. %s",
                        torrent_id, file_id, current_retry + 1, max_retries, reason,
                    )
                    await self._log_event(
                        torrent_id, "warn",
                        f"deep_sync: aria2 error retry {current_retry+1}/{max_retries} for {row['filename']!r}: {reason}",
                    )
                    new_gid = None
                    if url:
                        try:
                            local_path_str = row["local_path"] or ""
                            options: dict = {}
                            if local_path_str:
                                from pathlib import PurePosixPath as _PPP
                                lp = Path(local_path_str)
                                options["dir"] = str(_PPP(str(lp.parent).replace(chr(92), "/")))
                                options["out"] = lp.name
                            new_gid = await self.aria2().ensure_download(url, options, start_paused=False)
                            async with get_db() as db:
                                await db.execute(
                                    """UPDATE download_files
                                       SET download_id=?, status='queued',
                                           retry_count=?, updated_at=CURRENT_TIMESTAMP
                                       WHERE id=?""",
                                    (new_gid, current_retry + 1, file_id),
                                )
                                await db.commit()
                            restarted_count += 1
                        except Exception as exc:
                            logger.error("deep_sync: restart failed for file %s: %s", file_id, exc)

                    if cfg.discord_notify_error:
                        torrent_name = row["torrent_name"] or f"torrent {torrent_id}"
                        await self.notify().send_error(
                            torrent_name,
                            reason=f"aria2 error (retry {current_retry+1}/{max_retries}): {reason}",
                            context=f"File: {row['filename']!r} — auto-restarted" if new_gid else "restart failed",
                            source="aria2",
                            provider="aria2",
                        )
                else:
                    # Max retries exhausted — mark as error, notify, remove from aria2
                    logger.error(
                        "deep_sync: max retries (%d) exhausted for torrent %s file %s — marking error. %s",
                        max_retries, torrent_id, file_id, reason,
                    )
                    await self._log_event(
                        torrent_id, "error",
                        f"deep_sync: max retries ({max_retries}) exhausted for {row['filename']!r}: {reason}",
                    )
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET status='error', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (file_id,),
                        )
                        await db.commit()
                    touched.add(torrent_id)

                    if cfg.discord_notify_error:
                        torrent_name = row["torrent_name"] or f"torrent {torrent_id}"
                        await self.notify().send_error(
                            torrent_name,
                            reason=f"aria2 download failed after {max_retries} retries: {reason}",
                            context=f"File: {row['filename']!r} — removed from queue",
                            source="aria2",
                            provider="aria2",
                        )

            elif dl.status == "active":
                # Update progress in DB
                if dl.total_length > 0:
                    progress = round(dl.completed_length / dl.total_length * 100, 1)
                    async with get_db() as db:
                        await db.execute(
                            """UPDATE download_files
                               SET status='downloading', size_bytes=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (dl.total_length, file_id),
                        )
                        # Update torrent-level progress
                        await db.execute(
                            "UPDATE torrents SET progress=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (progress, torrent_id),
                        )
                        await db.commit()

            elif dl.status in ("waiting", "paused"):
                # Update size if aria2 knows it
                if dl.total_length > 0:
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET size_bytes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dl.total_length, file_id),
                        )
                        await db.commit()

        logger.info(
            "deep_sync_aria2_finished: checked %d file(s), completed %d, restarted %d error(s), finalized %d torrent(s)",
            len(rows), completed_count, restarted_count, len(touched),
        )

        for torrent_id in touched:
            await self._finalize_aria2_torrent(torrent_id)

        # ── Stragglers (same logic as sync_aria2_downloads) ──────────────────
        try:
            async with get_db() as db:
                straggler_rows = await (await db.execute(
                    """SELECT DISTINCT torrent_id
                       FROM download_files
                       WHERE torrent_id IN (
                           SELECT id FROM torrents
                           WHERE status IN ('queued', 'downloading')
                             AND download_client = 'aria2'
                       )
                       GROUP BY torrent_id
                       HAVING SUM(CASE WHEN blocked=0 AND status != 'completed' THEN 1 ELSE 0 END) = 0
                          AND SUM(CASE WHEN blocked=0 THEN 1 ELSE 0 END) > 0""",
                )).fetchall()
            straggler_ids = {r["torrent_id"] for r in straggler_rows} - touched
            if straggler_ids:
                logger.info(
                    "deep_sync: found %d straggler torrent(s) with all files completed "
                    "but torrent still active — finalising: %s",
                    len(straggler_ids), sorted(straggler_ids),
                )
                for torrent_id in straggler_ids:
                    await self._finalize_aria2_torrent(torrent_id)
        except Exception as exc:
            logger.warning("deep_sync: straggler check failed: %s", exc)


    async def cleanup_stuck_downloads(self):
        """
        Resets torrents stuck in active states for too long.

        Two checks:
        1. Local download stuck (queued/downloading) > stuck_download_timeout_hours
           → reset to 'ready' so the download restarts
        2. AllDebrid processing stuck (processing/uploading) > 24h without update
           → reset to trigger re-poll; AllDebrid may have finished or errored
        """
        from core.config import get_settings
        cfg = get_settings()
        timeout_hours = getattr(cfg, "stuck_download_timeout_hours", 6)

        async with get_db() as db:
            # Check 1: local download stuck
            stuck_local = []
            if timeout_hours and timeout_hours > 0:
                stuck_local = await (await db.execute(
                    """SELECT id, name, alldebrid_id, status FROM torrents
                       WHERE status IN ('queued', 'downloading')
                         AND updated_at < datetime('now', ? || ' hours')""",
                    (f"-{timeout_hours}",)
                )).fetchall()

            # Check 2: AllDebrid processing stuck > 24h (configurable separately)
            stuck_ad = await (await db.execute(
                """SELECT id, name, alldebrid_id, status FROM torrents
                   WHERE status IN ('processing', 'uploading')
                     AND updated_at < datetime('now', '-24 hours')
                     AND alldebrid_id IS NOT NULL AND alldebrid_id != ''"""
            )).fetchall()

        rows = list(stuck_local) + [r for r in stuck_ad if r["id"] not in {s["id"] for s in stuck_local}]
        if not rows:
            return

        logger.info("cleanup_stuck_downloads: %d stuck torrent(s) found", len(rows))
        for row in rows:
            logger.info("Resetting stuck torrent %s (%s) [was %s]", row["id"], row["name"], row["status"])
            reason = (
                f"Auto-reset: stuck in '{row['status']}' for >{timeout_hours}h"
                if row["status"] in ("queued", "downloading")
                else f"Auto-reset: stuck in '{row['status']}' for >24h on AllDebrid"
            )
            async with get_db() as db:
                await db.execute(
                    "UPDATE torrents SET status='ready', polling_failures=0, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],)
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                    (row["id"], reason)
                )
                await db.commit()

    async def cleanup_no_peer_errors(self):
        """
        Finds torrents in 'error' status with known fatal error patterns and
        removes them from AllDebrid + marks as deleted + sends Discord webhook.

        Handles:
          - "No peer after 30 minutes" (provider_status_code=8 or LIKE '%no peer%')
          - "Download took more than 3 days" (AllDebrid timeout)
          - Any other provider timeout/abort patterns

        Also handles torrents WITHOUT an alldebrid_id (cleaned up locally only).
        """
        async with get_db() as db:
            rows = await (await db.execute(
                """SELECT id, name, alldebrid_id, error_message, provider_status_code
                   FROM torrents
                   WHERE status = 'error'
                     AND (
                       LOWER(error_message) LIKE '%no peer%'
                       OR LOWER(error_message) LIKE '%more than 3 day%'
                       OR LOWER(error_message) LIKE '%took more than%'
                       OR LOWER(error_message) LIKE '%timeout%'
                       OR LOWER(error_message) LIKE '%timed out%'
                       OR provider_status_code = 8
                       OR provider_status_code = 7
                     )"""
            )).fetchall()

        if not rows:
            return

        cfg = get_settings()
        logger.info("cleanup_no_peer_errors: found %d torrent(s) to clean up", len(rows))

        for row in rows:
            ad_id = str(row["alldebrid_id"] or "").strip()
            name  = row["name"] or f"torrent {row['id']}"

            if ad_id and ad_id.lower() not in ("none", "null", ""):
                try:
                    logger.info("no-peer cleanup: removing %s (%s) from AllDebrid", row["id"], name)
                    await self.ad().delete_magnet(ad_id)
                except Exception as exc:
                    logger.warning("no-peer cleanup: could not delete magnet %s: %s", ad_id, exc)
                event_msg = "No peers after 30 minutes — removed from AllDebrid and cleaned up"
            else:
                logger.info(
                    "no-peer cleanup: torrent %s (%s) has no AllDebrid ID — "
                    "marking deleted locally only", row["id"], name
                )
                event_msg = "No peers after 30 minutes — no AllDebrid ID, cleaned up locally"

            async with get_db() as db:
                await db.execute(
                    "UPDATE torrents SET status='deleted', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],)
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                    (row["id"], event_msg)
                )
                await db.commit()

            # Discord webhook notification
            await self._notify_provider_error(
                name,
                reason="No peers found after 30 minutes — torrent removed",
                context=(f"AllDebrid ID {ad_id} deleted" if ad_id and ad_id.lower() not in ("none","null","")
                         else "No AllDebrid ID available — cleaned up locally only"),
                alldebrid_id=str(ad_id or ""),
                status_code=8,
            )

    async def _apply_provider_update(self, row: Dict, magnet: Dict, normalized: Dict[str, object]):
        provider_status = str(normalized["provider_status"])
        local_status = str(normalized["local_status"])
        status_code = int(normalized["status_code"])
        progress = float(normalized["progress"])
        size_bytes = int(normalized["size_bytes"])
        provider_message = str(normalized["message"])
        current_status = row["status"]
        provider_state_changed = provider_status != (row["provider_status"] or "") or status_code != int(row["provider_status_code"] or -1)
        persisted_status = current_status if current_status in {"queued", "downloading", "paused"} and provider_status == "ready" else local_status

        async with get_db() as db:
            if provider_state_changed:
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                    (row["id"], "info", f"AllDebrid status -> {provider_status} [{status_code}] {provider_message}".strip()),
                )
            await db.execute(
                """UPDATE torrents
                   SET status=?, provider_status=?, provider_status_code=?, progress=?, size_bytes=?,
                       polling_failures=0, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (persisted_status, provider_status, status_code, progress, size_bytes, row["id"]),
            )
            await db.commit()

        if provider_status == "ready" and current_status in {"pending", "uploading", "processing", "ready", "error"}:
            # Also restart if local status is 'error' but AllDebrid reports ready —
            # the torrent may have recovered or been re-uploaded
            name = magnet.get("filename") or magnet.get("name") or row["name"]
            if current_status == "error":
                logger.info("Torrent %s recovered on AllDebrid (was error, now ready) — restarting download", row["id"])
                async with get_db() as _db:
                    await _db.execute(
                        "UPDATE torrents SET status='ready', error_message=NULL, polling_failures=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (row["id"],),
                    )
                    await _db.commit()
            asyncio.create_task(self._start_download(row["id"], str(row["alldebrid_id"]), str(name)))
        elif provider_status == "error" and current_status != "error":
            error_message = f"AllDebrid error code {status_code}: {provider_message}".strip()
            # statusCode 8 = "No peer after 30 minutes" — auto-remove from AllDebrid silently
            if status_code == 8 or "no peer" in provider_message.lower():
                logger.info(
                    "Auto-removing torrent %s (id=%s): no peers after 30 min",
                    row["id"], row.get("alldebrid_id", "?"),
                )
                await self._notify_provider_error(
                    str(row["name"] or magnet.get("filename") or magnet.get("name") or f"torrent {row['id']}"),
                    reason="No peers found after 30 minutes — torrent removed",
                    context="AllDebrid reported the torrent as unavailable due to missing peers.",
                    alldebrid_id=str(row.get("alldebrid_id") or ""),
                    status_code=status_code,
                )
                await self._log_event(row["id"], "warn",
                    f"Auto-removed: no peers found after 30 minutes (code {status_code})")
                await self.ad().delete_magnet(str(row["alldebrid_id"]))
                await self._set_deleted(row["id"], "Auto-removed: no peers after 30 minutes")
            else:
                await self._fail_torrent(row["id"], error_message, notify=True)
        elif provider_status == "error" and current_status == "error" and provider_state_changed:
            await self._notify_provider_error(
                str(row["name"] or magnet.get("filename") or magnet.get("name") or f"torrent {row['id']}"),
                reason=f"AllDebrid reported magnet error {status_code}",
                context=provider_message,
                alldebrid_id=str(row.get("alldebrid_id") or ""),
                status_code=status_code,
            )

    async def _increment_poll_failure(self, torrent_id: int, name: str, reason: str):
        async with get_db() as db:
            await db.execute(
                "UPDATE torrents SET polling_failures=polling_failures+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_id,),
            )
            row = await (await db.execute("SELECT polling_failures FROM torrents WHERE id=?", (torrent_id,))).fetchone()
            failures = int(row["polling_failures"] or 0)
            if failures == 1 or failures == PROVIDER_FAILURE_THRESHOLD:
                level = "warn" if failures < PROVIDER_FAILURE_THRESHOLD else "error"
                message = f"AllDebrid polling issue ({failures}): {reason}"
                await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, level, message))
                if failures >= PROVIDER_FAILURE_THRESHOLD:
                    await db.execute(
                        "UPDATE torrents SET status='error', error_message=? WHERE id=?",
                        (message, torrent_id),
                    )
            await db.commit()

        if failures >= PROVIDER_FAILURE_THRESHOLD:
            await self._notify_provider_error(
                name,
                reason=reason,
                context=f"Polling failed {failures} times in a row",
                source="AllDebrid polling",
                provider="AllDebrid",
            )

    async def _start_download(self, torrent_id: int, ad_id: str, name: str):
        if self.is_paused() or torrent_id in self._active:
            return
        # Atomically claim this torrent_id BEFORE any await to prevent TOCTOU:
        # two concurrent tasks could both pass "torrent_id in self._active"
        # if there is an await between the check and the add.
        # We add first, then validate — if validation fails we discard and return.
        self._active.add(torrent_id)
        try:
            # Guard: do not restart a torrent that is actively downloading.
            # Check both status AND whether download_files records exist.
            # If download_files is empty (after _reset_torrent_for_redownload)
            # the torrent is NOT actively downloading — restart is intended.
            try:
                async with get_db() as _guard_db:
                    _t = await (await _guard_db.execute(
                        "SELECT status FROM torrents WHERE id=?", (torrent_id,)
                    )).fetchone()
                    if _t is None:
                        logger.debug("_start_download: torrent %s no longer exists", torrent_id)
                        return
                    _status = _t["status"]
                    if _status in ("completed", "deleted"):
                        logger.debug(
                            "_start_download: torrent %s is terminal (status=%s) — skipping",
                            torrent_id, _status,
                        )
                        return
                    if _status in ("queued", "downloading", "paused"):
                        _file_count = await (await _guard_db.execute(
                            "SELECT COUNT(*) AS c FROM download_files "
                            "WHERE torrent_id=? AND blocked=0 "
                            "  AND status IN ('pending','queued','downloading','paused')",
                            (torrent_id,),
                        )).fetchone()
                        if _file_count and _file_count["c"] > 0:
                            logger.debug(
                                "_start_download: torrent %s already in progress "
                                "(status=%s, %d active files) — skipping",
                                torrent_id, _status, _file_count["c"],
                            )
                            return
            except Exception as exc:
                logger.debug("_start_download guard DB check failed: %s — proceeding", exc)
            async with self.sem():
                await self._download(torrent_id, ad_id, name)
        except TransientAllDebridStateError as exc:
            logger.warning("Download deferred db_id=%s: %s", torrent_id, exc)
            async with get_db() as db:
                await db.execute(
                    "UPDATE torrents SET status='ready', error_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (torrent_id,),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                    (torrent_id, "warn", str(exc)),
                )
                await db.commit()
        except Exception as exc:
            logger.error("Download failed db_id=%s: %s", torrent_id, exc)
            await self._fail_torrent(torrent_id, str(exc), notify=True)
        finally:
            self._active.discard(torrent_id)

    async def _download(self, torrent_id: int, ad_id: str, name: str):
        cfg = get_settings()
        client_name = self.download_client_name()
        initial_status = "queued"  # aria2 is the only download client

        # Cancel any existing aria2 jobs for this torrent before clearing the DB rows.
        # Without this, the old aria2 entries become orphans that download in parallel.
        try:
            async with get_db() as _pre_db:
                old_gids = await (await _pre_db.execute(
                    "SELECT download_id FROM download_files "
                    "WHERE torrent_id=? AND download_client='aria2' "
                    "AND download_id IS NOT NULL AND status NOT IN ('completed','error','blocked')",
                    (torrent_id,),
                )).fetchall()
            for _r in old_gids:
                _gid = str(_r["download_id"] or "")
                if _gid:
                    try:
                        await self.aria2().remove(_gid)
                        logger.debug("_download: cancelled stale aria2 GID %s for torrent %s", _gid, torrent_id)
                    except Exception:
                        pass  # GID already gone — fine
        except Exception as exc:
            logger.debug("_download: stale aria2 cleanup skipped: %s", exc)

        async with get_db() as db:
            await db.execute("DELETE FROM download_files WHERE torrent_id=?", (torrent_id,))
            await db.execute(
                "UPDATE torrents SET status=?, download_client=?, error_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (initial_status, client_name, torrent_id),
            )
            await db.commit()

        flat_files = await self._fetch_ready_files(ad_id)
        if not flat_files:
            raise Exception("No downloadable files returned from AllDebrid")

        destination_root = Path(cfg.download_folder) / safe_name(name)
        # Directory creation is left to aria2

        total_files = len(flat_files)
        blocked_items: List[dict] = []
        transferred_items: List[dict] = []
        queued_items: List[dict] = []
        failed_items: List[dict] = []
        seen_queue_keys: Set[Tuple[str, str]] = set()

        for file_info in flat_files:
            relative_path = file_info.get("path") or file_info.get("name") or "download.bin"
            display_name = str(PurePosixPath(relative_path.replace("\\", "/")))
            file_size = int(file_info.get("size", 0) or 0)
            blocked, reason = is_blocked(display_name, cfg, file_size)
            source_link = file_info["link"]
            local_path = destination_root / safe_rel_path(display_name)
            dedupe_key = (display_name.lower(), source_link.strip())

            if dedupe_key in seen_queue_keys:
                logger.info("Skipping duplicate AllDebrid file entry for %s", display_name)
                continue
            seen_queue_keys.add(dedupe_key)

            if blocked:
                blocked_items.append({"filename": display_name, "size_bytes": file_size, "reason": reason})
                await self._log_file(torrent_id, display_name, source_link, str(local_path), "blocked", reason, file_size)
                continue

            try:
                unlocked = await _retry_async(self.ad().unlock_link, source_link)
                download_url = unlocked.get("link", "")
                if not download_url:
                    raise Exception("Empty download URL from unlock")

                # unlock_link returns the authoritative file size — use it if
                # AllDebrid didn't provide one in the magnet/files response.
                if file_size <= 0:
                    file_size = int(unlocked.get("filesize", 0) or 0)

                if local_path.exists() and (file_size <= 0 or local_path.stat().st_size >= max(file_size - 1024, 0)):
                    transferred_items.append({"filename": display_name, "size_bytes": file_size})
                    await self._log_file(torrent_id, display_name, download_url, str(local_path), "completed", None, file_size)
                    continue

                # Queue for aria2 delivery
                queued_items.append({"filename": display_name, "size_bytes": file_size})
                await self._log_file(
                    torrent_id,
                    display_name,
                    source_link,
                    str(local_path),
                    "pending",
                    None,
                    file_size,
                    download_client="aria2",
                )
            except Exception as exc:
                logger.error("File failed [%s]: %s", display_name, exc)
                failed_items.append({"filename": display_name, "size_bytes": file_size, "reason": str(exc)})
                await self._log_file(torrent_id, display_name, source_link, str(local_path), "error", str(exc), file_size, download_client=client_name)

        blocked_count = len(blocked_items)
        failed_count = len(failed_items)
        completed_count = len(transferred_items)
        queued_count = len(queued_items)
        downloadable_count = total_files - blocked_count

        # Compute total size from all processed files — more reliable than the
        # AllDebrid magnet-status value which is often 0 until the torrent is ready.
        total_size_bytes = _size_sum(blocked_items + transferred_items + queued_items + failed_items)

        # All files go through aria2 — final_status is queued or error
        if blocked_count == total_files and total_files > 0 and failed_count == 0:
            # ALL files filtered — nothing to download; treat as completed so
            # the torrent is removed from AllDebrid and counted in statistics.
            final_status = "completed"
        elif failed_count == 0 and (completed_count + queued_count) == downloadable_count and downloadable_count > 0:
            # completed_count > 0 means files already existed on disk (skipped)
            final_status = "queued" if queued_count > 0 else "completed"
        elif blocked_count > 0 and failed_count == 0 and (completed_count + queued_count) > 0:
            final_status = "queued" if queued_count > 0 else "completed"
        else:
            final_status = "error"

        async with get_db() as db:
            await db.execute(
                "UPDATE torrents SET status=?, local_path=?, size_bytes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (final_status, str(destination_root), total_size_bytes, torrent_id),
            )
            if final_status == "completed":
                await db.execute("UPDATE torrents SET completed_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            # Build a descriptive event message
            if blocked_count == total_files and total_files > 0:
                _evt_msg = f"All {blocked_count} file(s) filtered/blocked — marked completed, removed from AllDebrid"
                _evt_lvl = "info"
            elif blocked_count > 0:
                _evt_msg = f"Download {final_status}: {completed_count + queued_count} files prepared, {blocked_count} filtered"
                _evt_lvl = "info" if final_status in {"completed", "queued", "paused"} else "warn"
            else:
                _evt_msg = f"Download {final_status}: {completed_count + queued_count} files prepared"
                _evt_lvl = "info" if final_status in {"completed", "queued", "paused"} else "warn"
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, _evt_lvl, _evt_msg),
            )
            await db.commit()

        await self._send_partial_summary(
            torrent_id,
            name,
            flat_files,
            blocked_items,
            transferred_items + queued_items,
            failed_items,
        )

        if final_status == "completed":
            await self._delete_magnet_after_completion(torrent_id, ad_id)
            await self._mark_finished(torrent_id, name=name)
            # For all-blocked torrents: partial notification already sent above;
            # skip the completed notification to avoid a confusing "0 files" message.
            if cfg.discord_notify_finished and blocked_count < total_files:
                await self.notify().send_complete(name, file_count=completed_count, destination=str(destination_root), download_client="aria2")
        elif final_status in {"queued", "paused"}:
            await self._log_event(
                torrent_id,
                "info",
                "Prepared for slot-based aria2 delivery",
            )
            await self._dispatch_pending_aria2_queue()
        else:
            await self._notify_provider_error(
                name,
                reason="Kept on AllDebrid for inspection",
                context="At least one file failed during preparation, so the torrent was left on AllDebrid.",
                alldebrid_id=str(ad_id or ""),
            )

    async def _fetch_ready_files(self, ad_id: str) -> List[Dict]:
        for attempt in range(1, READY_FILE_RETRIES + 1):
            files_data = await self.ad().get_magnet_files([ad_id])
            for entry in files_data:
                if str(entry.get("id", "")) == str(ad_id):
                    flat_files = flatten_files(entry.get("files", []))
                    if flat_files:
                        return flat_files
            await asyncio.sleep(attempt)
        try:
            status_rows = await self.ad().get_magnet_status(ad_id)
        except Exception:
            status_rows = []
        if status_rows:
            magnet = status_rows[0]
            normalized = normalize_provider_state(magnet)
            provider_status = str(normalized["provider_status"])
            provider_message = str(normalized["message"] or "").strip()
            status_code = int(normalized["status_code"])
            if provider_status in {"ready", "processing", "queued"}:
                raise TransientAllDebridStateError(
                    f"AllDebrid did not expose downloadable files yet (status {provider_status} [{status_code}] {provider_message})"
                )
            if provider_status == "error":
                raise Exception(
                    f"AllDebrid reported magnet error {status_code}: {provider_message}".strip()
                )
        return []

    def _remote_aria2_path(self, local_path: Path) -> str:
        cfg = get_settings()
        if cfg.aria2_download_path:
            relative = local_path.relative_to(Path(cfg.download_folder))
            return str(PurePosixPath(cfg.aria2_download_path.replace("\\", "/")) / PurePosixPath(str(relative).replace("\\", "/")))
        return str(PurePosixPath(str(local_path).replace("\\", "/")))

    def _build_aria2_indexes(self, all_downloads):
        by_gid = {download.gid: download for download in all_downloads}
        uri_to_dl = {}
        path_to_dl = {}
        for dl in all_downloads:
            for fi in dl.files or []:
                current_path = _normalize_aria2_path(str(fi.get("path", "")))
                if current_path:
                    path_to_dl[current_path] = dl
                for u in fi.get("uris", []) or []:
                    uri = str(u.get("uri", "")).strip()
                    if uri:
                        uri_to_dl[uri] = dl
        return by_gid, uri_to_dl, path_to_dl

    def _aria2_slot_limit(self) -> int:
        cfg = get_settings()
        value = int(getattr(cfg, "aria2_max_active_downloads", 0) or 0)
        if value <= 0:
            value = int(cfg.max_concurrent_downloads or 1)
        return max(1, value)

    def _aria2_state_windows(self) -> tuple[int, int]:
        cfg = get_settings()
        waiting = int(getattr(cfg, "aria2_waiting_window", 100) or 100)
        stopped = int(getattr(cfg, "aria2_stopped_window", 100) or 100)
        return max(10, min(1000, waiting)), max(10, min(1000, stopped))

    async def _aria2_get_all(self):
        waiting, stopped = self._aria2_state_windows()
        return await self.aria2().get_all(waiting_limit=waiting, stopped_limit=stopped)

    async def _aria2_get_memory_diagnostics(self):
        waiting, stopped = self._aria2_state_windows()
        return await self.aria2().get_memory_diagnostics(waiting_limit=waiting, stopped_limit=stopped)

    async def _dispatch_pending_aria2_queue(self, all_downloads=None):
        """
        The single authoritative gate between our DB and aria2.

        Invariant: at any point, at most aria2_max_active_downloads files
        may have status active/waiting/paused in aria2 at the same time.

        Steps:
        1. Fetch current aria2 state once.
        2. Count in-flight entries (active + waiting + paused).
        3. If over the limit (e.g. settings were reduced): remove the
           excess entries from aria2 and reset those download_files to
           pending so they are re-queued in order on the next cycle.
        4. Fill available slots from pending download_files, oldest first.
        """
        if self.download_client_name() != "aria2" or self.is_paused():
            return

        async with self._aria2_dispatch_lock:
            current_downloads = (
                all_downloads if all_downloads is not None
                else await self._aria2_get_all()
            )
            limit = self._aria2_slot_limit()
            in_flight = [
                dl for dl in current_downloads
                if dl.status in {"active", "waiting", "paused"}
            ]

            # ── Step 3: trim excess if limit was lowered ─────────────────────
            if len(in_flight) > limit:
                excess = in_flight[limit:]  # oldest are last — remove from end
                excess_gids = {dl.gid for dl in excess}
                logger.info(
                    "aria2 queue trim: %d in-flight > limit %d, removing %d",
                    len(in_flight), limit, len(excess),
                )
                # Find download_files rows for these GIDs and reset to pending
                async with get_db() as db:
                    gid_placeholders = ",".join("?" * len(excess_gids))
                    stale = await (await db.execute(
                        f"SELECT id FROM download_files WHERE download_id IN ({gid_placeholders})",
                        list(excess_gids),
                    )).fetchall()
                    if stale:
                        ids = [r["id"] for r in stale]
                        id_placeholders = ",".join("?" * len(ids))
                        await db.execute(
                            f"""UPDATE download_files
                               SET status='pending', download_id=NULL,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id IN ({id_placeholders})""",
                            ids,
                        )
                        await db.commit()
                for dl in excess:
                    await self.aria2().remove(dl.gid)
                in_flight = in_flight[:limit]

            # ── Step 4: fill available slots ─────────────────────────────────
            available_slots = max(0, limit - len(in_flight))
            if available_slots <= 0:
                return

            async with get_db() as db:
                pending_rows = await (
                    await db.execute(
                        """SELECT f.id AS file_id, f.torrent_id, f.filename,
                                  f.download_url, f.local_path, t.name AS torrent_name
                           FROM download_files f
                           JOIN torrents t ON t.id = f.torrent_id
                           WHERE f.download_client='aria2'
                             AND f.blocked=0
                             AND f.status='pending'
                             AND t.status NOT IN ('completed','deleted','error')
                           ORDER BY f.id ASC
                           LIMIT ?""",
                        (available_slots,),
                    )
                ).fetchall()

            if not pending_rows:
                return

            logger.info(
                "aria2 dispatch: %d slot(s) free, dispatching %d file(s)",
                available_slots, len(pending_rows),
            )

            # Snapshot of aria2 state for the whole dispatch batch.
            # Passing this to ensure_download() avoids one get_all() call per
            # file, which would cause a burst of rapid RPC requests that aria2
            # may drop or answer inconsistently.
            dispatch_snapshot = await self._aria2_get_all()

            for row in pending_rows:
                source_link = str(row["download_url"] or "").strip()
                local_path = Path(row["local_path"])
                try:
                    unlocked = await _retry_async(self.ad().unlock_link, source_link)
                    download_url = unlocked.get("link", "")
                    if not download_url:
                        raise Exception("Empty download URL from unlock")

                    remote_path = self._remote_aria2_path(local_path)
                    remote_dir  = str(PurePosixPath(remote_path).parent)
                    remote_name = PurePosixPath(remote_path).name
                    gid = await self.aria2().ensure_download(
                        download_url,
                        {"dir": remote_dir, "out": remote_name},
                        get_settings().aria2_start_paused,
                        cached_downloads=dispatch_snapshot,
                    )
                    queued_status = "paused" if get_settings().aria2_start_paused else "queued"
                    async with get_db() as db:
                        await db.execute(
                            """UPDATE download_files
                               SET status=?, download_id=?, download_url=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (queued_status, gid, download_url, row["file_id"]),
                        )
                        await db.execute(
                            """UPDATE torrents SET status=?, updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status NOT IN ('completed','deleted','error')""",
                            (queued_status, row["torrent_id"]),
                        )
                        await db.commit()
                    logger.info(
                        "aria2 dispatch: %s → GID %s (torrent %s)",
                        row["filename"], gid, row["torrent_id"],
                    )
                except Exception as exc:
                    logger.error("aria2 dispatch failed [%s]: %s", row["filename"], exc)
                    await self._update_file_state(row["file_id"], "error", row["local_path"], reason=str(exc))
                    await self._finalize_aria2_torrent(row["torrent_id"])

    async def sync_download_clients(self):
        if self.download_client_name() == "aria2":
            await self.sync_aria2_downloads()
            # Enforce slot limit and clean up orphaned finished entries
            await self._dispatch_pending_aria2_queue()
            await self._cleanup_aria2_orphans()

    async def _cleanup_aria2_orphans(self):
        """
        Removes 'complete' or 'error' entries from aria2 that either:
        - Have no matching download_files row (orphaned GID)
        - Correspond to a download_files row already marked 'completed'
        This prevents aria2's stopped list from accumulating stale entries.
        """
        if self.download_client_name() != "aria2" or self.is_paused():
            return
        try:
            all_downloads = await self._aria2_get_all()
        except Exception:
            return

        stopped = [dl for dl in all_downloads if dl.status in {"complete", "removed", "error"}]
        if not stopped:
            return

        # Collect all known GIDs from DB that are still active
        try:
            async with get_db() as db:
                rows = await (await db.execute(
                    """SELECT download_id, status FROM download_files
                       WHERE download_id IS NOT NULL
                         AND status NOT IN ('completed', 'error', 'blocked')"""
                )).fetchall()
            active_gids = {str(r["download_id"]) for r in rows}
        except Exception as exc:
            logger.debug("_cleanup_aria2_orphans: DB query failed: %s", exc)
            return

        removed = 0
        for dl in stopped:
            if dl.gid not in active_gids:
                await self.aria2().remove(dl.gid)
                removed += 1

        if removed:
            logger.info("aria2 orphan cleanup: removed %d stale finished/error entries", removed)

    async def sync_aria2_downloads(self):
        if self.is_paused() or self.download_client_name() != "aria2":
            return

        all_downloads = await self._aria2_get_all()
        by_gid, uri_to_dl, path_to_dl = self._build_aria2_indexes(all_downloads)

        async with get_db() as db:
            rows = await (
                await db.execute(
                    """SELECT t.id AS torrent_id, t.name, t.alldebrid_id, t.status AS torrent_status,
                              f.id AS file_id, f.filename, f.local_path, f.download_url,
                              f.download_id, f.status, f.blocked
                       FROM torrents t
                       JOIN download_files f ON f.torrent_id = t.id
                       WHERE f.download_client='aria2'
                         AND f.blocked=0
                         AND f.status IN ('queued', 'downloading', 'paused')
                         AND f.download_id IS NOT NULL"""
                )
            ).fetchall()

        touched: Set[int] = set()
        reset_on_sync: Set[int] = set()
        for row in rows:
            if row["torrent_id"] in reset_on_sync:
                continue  # already scheduled for reset
            if row["torrent_id"] in self._active:
                continue  # _start_download/_download is running — leave it alone

            dl = by_gid.get(str(row["download_id"] or ""))

            if row["status"] == "pending":
                continue

            if dl is None:
                remote_path = ""
                if row["local_path"]:
                    try:
                        remote_path = _normalize_aria2_path(self._remote_aria2_path(Path(row["local_path"])))
                    except Exception:
                        remote_path = _normalize_aria2_path(str(row["local_path"]))
                url = str(row["download_url"] or "").strip()
                dl = path_to_dl.get(remote_path) if remote_path else None
                if dl is None and url:
                    dl = uri_to_dl.get(url)

                if dl is not None:
                    # Found under different GID — update DB and fall through to status sync
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET download_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dl.gid, row["file_id"]),
                        )
                        await db.commit()
                    logger.info(
                        "Synced stale GID -> %s for torrent %s file %s via %s",
                        dl.gid,
                        row["torrent_id"],
                        row["file_id"],
                        "path" if remote_path and path_to_dl.get(remote_path) is dl else "url",
                    )
                elif remote_path or url:
                    logger.info(
                        "sync_aria2: aria2 entry not found for torrent %s file %s (path=%s, url=%s) -> scheduling reset",
                        row["torrent_id"], row["file_id"], remote_path or "-", url or "-",
                    )
                    reset_on_sync.add(row["torrent_id"])
                    continue
                else:
                    # No URL to look up — reset
                    reset_on_sync.add(row["torrent_id"])
                    continue

            # Status sync from aria2
            sz = dl.total_length if dl.total_length > 0 else None
            if dl.status == "paused":
                await self._update_file_state(row["file_id"], "paused", row["local_path"], size_bytes=sz)
            elif dl.status == "waiting":
                await self._update_file_state(row["file_id"], "queued", row["local_path"], size_bytes=sz)
            elif dl.status == "active":
                await self._update_file_state(row["file_id"], "downloading", row["local_path"], size_bytes=sz)
            elif dl.status == "complete":
                if row["status"] != "completed":
                    await self._update_file_state(row["file_id"], "completed", row["local_path"], size_bytes=sz)
                    touched.add(row["torrent_id"])
                # Always remove from aria2 — catches leftover 'finished' entries
                await self.aria2().remove(dl.gid)
                logger.debug("aria2 cleanup: removed GID %s for file %s (torrent %s)",
                             dl.gid, row["file_id"], row["torrent_id"])
            elif dl.status == "removed":
                logger.info(
                    "sync_aria2: aria2 job was removed for torrent %s file %s -> scheduling reset",
                    row["torrent_id"], row["file_id"],
                )
                reset_on_sync.add(row["torrent_id"])
                continue
            elif dl.status == "error":
                reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                await self._update_file_state(row["file_id"], "error", row["local_path"], reason=reason, size_bytes=sz)
                await self.aria2().remove(dl.gid)
                touched.add(row["torrent_id"])

        # Reset torrents whose entries are gone from aria2 (can't confirm completion)
        for torrent_id in reset_on_sync - touched:
            t = await self._get_torrent_completion_snapshot(torrent_id)
            if not t:
                continue
            # Don't reset if torrent is already in a terminal state
            if t["status"] in ("completed", "deleted", "error"):
                logger.debug(
                    "sync_aria2: skip reset for torrent %s — already %s",
                    torrent_id, t["status"],
                )
                continue
            # Don't reset if all non-blocked files are already completed
            if t["total"] > 0 and t["done"] >= t["total"]:
                logger.info(
                    "sync_aria2: torrent %s — all %d files completed, finalising instead of reset",
                    torrent_id, t["total"],
                )
                await self._finalize_aria2_torrent(torrent_id)
                continue
            logger.info(
                "sync_aria2: resetting torrent %s (aria2 entry lost, %d/%d files done)",
                torrent_id, t["done"], t["total"],
            )
            await self._reset_torrent_for_redownload(
                torrent_id, "aria2 entry lost during sync — reset for re-download"
            )
            if t["alldebrid_id"]:
                asyncio.create_task(
                    self._start_download(torrent_id, str(t["alldebrid_id"]), str(t["name"] or ""))
                )

        # Finalize torrents where aria2 reported complete or error
        for torrent_id in touched:
            await self._finalize_aria2_torrent(torrent_id)

        # ── Stragglers: torrents stuck in active state but all files already done ──
        # Happens when _finalize previously threw an exception after files were marked
        # completed, or after a restart where download_files rows survived but the
        # torrent status was not updated.  The normal rows query (status IN queued/
        # downloading/paused) skips files that are already 'completed', so these
        # torrents never appear in touched and _finalize is never called again.
        try:
            async with get_db() as db:
                straggler_rows = await (await db.execute(
                    """SELECT DISTINCT torrent_id
                       FROM download_files
                       WHERE torrent_id IN (
                           SELECT id FROM torrents
                           WHERE status IN ('queued', 'downloading')
                             AND download_client = 'aria2'
                       )
                       GROUP BY torrent_id
                       HAVING SUM(CASE WHEN blocked=0 AND status != 'completed' THEN 1 ELSE 0 END) = 0
                          AND SUM(CASE WHEN blocked=0 THEN 1 ELSE 0 END) > 0""",
                )).fetchall()
            straggler_ids = {r["torrent_id"] for r in straggler_rows} - touched
            if straggler_ids:
                logger.info(
                    "sync_aria2: found %d straggler torrent(s) with all files completed "
                    "but torrent still active — finalising now: %s",
                    len(straggler_ids), sorted(straggler_ids),
                )
                for torrent_id in straggler_ids:
                    await self._finalize_aria2_torrent(torrent_id)
        except Exception as exc:
            logger.warning("sync_aria2: straggler check failed: %s", exc)

        await self._dispatch_pending_aria2_queue()

    async def _reset_torrent_for_redownload(self, torrent_id: int, reason: str):
        """Clear download_files and mark torrent as downloading so the sync loop
        ignores it while _start_download/_download re-runs and re-registers
        the new URIs with aria2. Status is updated to 'queued' or 'paused' once
        _download() completes and the new download_files rows are written."""
        async with get_db() as db:
            await db.execute("DELETE FROM download_files WHERE torrent_id=?", (torrent_id,))
            await db.execute(
                "UPDATE torrents SET status='downloading', error_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_id,),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, "warn", reason),
            )
            await db.commit()

    async def reconcile_aria2_on_startup(self):
        """Called once at startup to reconcile DB state with what aria2 actually has.

        1. GID still in aria2 → sync status directly.
        2. GID gone, same URI active under new GID → update GID, sync status.
        3. GID gone, URI not in aria2 → reset download_files and re-queue via
           _start_download. We cannot know if the download completed cleanly or
           was dropped, so a safe re-download is the only correct action.
        """
        if self.download_client_name() != "aria2":
            return
        try:
            all_downloads = await self._aria2_get_all()
        except Exception as exc:
            logger.warning("Startup aria2 reconciliation skipped: %s", exc)
            return

        all_downloads = await self._dedupe_aria2_downloads_on_startup(all_downloads)

        by_gid, uri_to_dl, path_to_dl = self._build_aria2_indexes(all_downloads)

        async with get_db() as db:
            rows = await (
                await db.execute(
                    """SELECT t.id AS torrent_id, t.alldebrid_id, t.name,
                              f.id AS file_id, f.download_id, f.download_url,
                              f.local_path, f.status
                       FROM torrents t
                       JOIN download_files f ON f.torrent_id = t.id
                       WHERE f.download_client='aria2'
                         AND f.blocked=0
                         AND f.status IN ('pending', 'queued', 'downloading', 'paused')"""
                )
            ).fetchall()

        touched: Set[int] = set()
        reset_torrents: Set[int] = set()

        for row in rows:
            if row["torrent_id"] in reset_torrents:
                continue  # whole torrent already scheduled for reset

            if row["status"] == "pending":
                continue

            gid = str(row["download_id"] or "")
            dl = by_gid.get(gid)

            if dl is None:
                remote_path = ""
                if row["local_path"]:
                    try:
                        remote_path = _normalize_aria2_path(self._remote_aria2_path(Path(row["local_path"])))
                    except Exception:
                        remote_path = _normalize_aria2_path(str(row["local_path"]))
                url = str(row["download_url"] or "").strip()
                dl = path_to_dl.get(remote_path) if remote_path else None
                if dl is None and url:
                    dl = uri_to_dl.get(url)
                if dl:
                    # Case 2: same path or URI under new GID — update and fall through to sync
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET download_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dl.gid, row["file_id"]),
                        )
                        await db.commit()
                    logger.info(
                        "Reconciled GID %s -> %s for torrent %s file %s via %s",
                        gid or "(none)",
                        dl.gid,
                        row["torrent_id"],
                        row["file_id"],
                        "path" if remote_path and path_to_dl.get(remote_path) is dl else "url",
                    )
                else:
                    # Case 3: gone — reset whole torrent and re-queue
                    snapshot = await self._get_torrent_completion_snapshot(row["torrent_id"])
                    if snapshot and snapshot["status"] not in ("completed", "deleted", "error") and snapshot["total"] > 0 and snapshot["done"] >= snapshot["total"]:
                        logger.info(
                            "Startup reconcile: torrent %s already has all %d files completed -> finalising instead of reset",
                            row["torrent_id"], snapshot["total"],
                        )
                        touched.add(row["torrent_id"])
                        continue
                    logger.info(
                        "Startup reconcile: GID %s not in aria2 for torrent %s (path=%s, url=%s) -> resetting",
                        gid, row["torrent_id"], remote_path or "-", url or "-",
                    )
                    reset_torrents.add(row["torrent_id"])
                    await self._reset_torrent_for_redownload(
                        row["torrent_id"],
                        f"aria2 entry lost (GID {gid}) — reset for re-download on startup",
                    )
                    continue

            # Cases 1 + 2: sync status from aria2
            sz = dl.total_length if dl.total_length > 0 else None
            if dl.status == "complete":
                await self._update_file_state(row["file_id"], "completed", row["local_path"], size_bytes=sz)
                await self.aria2().remove(dl.gid)
                touched.add(row["torrent_id"])
            elif dl.status == "removed":
                logger.info(
                    "Startup reconcile: aria2 job was removed for torrent %s file %s -> scheduling reset",
                    row["torrent_id"], row["file_id"],
                )
                reset_torrents.add(row["torrent_id"])
                await self._reset_torrent_for_redownload(
                    row["torrent_id"],
                    f"aria2 entry removed (GID {dl.gid}) -> reset for re-download on startup",
                )
            elif dl.status == "error":
                reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                await self._update_file_state(row["file_id"], "error", row["local_path"], reason=reason, size_bytes=sz)
                await self.aria2().remove(dl.gid)
                touched.add(row["torrent_id"])
            elif dl.status == "active":
                await self._update_file_state(row["file_id"], "downloading", row["local_path"], size_bytes=sz)
            elif dl.status == "waiting":
                await self._update_file_state(row["file_id"], "queued", row["local_path"], size_bytes=sz)
            elif dl.status == "paused":
                await self._update_file_state(row["file_id"], "paused", row["local_path"], size_bytes=sz)

        for torrent_id in touched:
            await self._finalize_aria2_torrent(torrent_id)

        for torrent_id in reset_torrents:
            async with get_db() as db:
                t = await (await db.execute(
                    "SELECT alldebrid_id, name FROM torrents WHERE id=?", (torrent_id,)
                )).fetchone()
            if t and t["alldebrid_id"]:
                asyncio.create_task(
                    self._start_download(torrent_id, str(t["alldebrid_id"]), str(t["name"] or ""))
                )

        await self._dispatch_pending_aria2_queue()

    async def _dedupe_aria2_downloads_on_startup(self, all_downloads):
        by_uri: Dict[str, List] = {}
        removed_gids: Set[str] = set()
        for dl in all_downloads:
            for fi in dl.files or []:
                for u in fi.get("uris", []) or []:
                    uri = str(u.get("uri", "")).strip()
                    if uri:
                        by_uri.setdefault(uri, []).append(dl)

        duplicate_sets = 0
        removed = 0
        for uri, matches in by_uri.items():
            unique: List = []
            seen_gids: Set[str] = set()
            for dl in matches:
                if dl.gid and dl.gid not in seen_gids:
                    unique.append(dl)
                    seen_gids.add(dl.gid)

            if len(unique) <= 1:
                continue

            duplicate_sets += 1
            unique.sort(key=lambda dl: (_aria2_status_rank(dl.status), dl.gid))
            keep = unique[0]
            for dup in unique[1:]:
                logger.warning(
                    "Startup aria2 dedupe removed duplicate gid %s for %s; keeping %s (%s)",
                    dup.gid,
                    uri,
                    keep.gid,
                    keep.status,
                )
                await self.aria2().remove(dup.gid)
                removed_gids.add(dup.gid)
                removed += 1

        if duplicate_sets:
            logger.info(
                "Startup aria2 dedupe finished: %s duplicate url groups, %s duplicate jobs removed",
                duplicate_sets,
                removed,
            )
            return [dl for dl in all_downloads if dl.gid not in removed_gids]

        return all_downloads

        logger.info(
            "Startup aria2 reconciliation: %d file(s) checked, %d finalized, %d reset",
            len(rows), len(touched), len(reset_torrents),
        )

    async def _update_file_state(
        self,
        file_id: int,
        status: str,
        local_path: Optional[str],
        reason: Optional[str] = None,
        size_bytes: Optional[int] = None,
    ):
        async with get_db() as db:
            if size_bytes is not None and size_bytes > 0:
                await db.execute(
                    """UPDATE download_files
                       SET status=?, local_path=?, block_reason=?, size_bytes=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (status, local_path, reason, size_bytes, file_id),
                )
            else:
                await db.execute(
                    """UPDATE download_files
                       SET status=?, local_path=?, block_reason=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (status, local_path, reason, file_id),
                )
            await db.commit()

    async def _get_torrent_completion_snapshot(self, torrent_id: int) -> Optional[dict]:
        async with get_db() as db:
            row = await (
                await db.execute(
                    """SELECT t.id, t.alldebrid_id, t.name, t.status,
                              COUNT(CASE WHEN f.blocked=0 AND f.status='completed' THEN 1 END) AS done,
                              COUNT(CASE WHEN f.blocked=0 THEN 1 END) AS total
                       FROM torrents t
                       LEFT JOIN download_files f ON f.torrent_id=t.id
                       WHERE t.id=? GROUP BY t.id""",
                    (torrent_id,),
                )
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "alldebrid_id": row["alldebrid_id"],
            "name": row["name"],
            "status": row["status"],
            "done": int(row["done"] or 0),
            "total": int(row["total"] or 0),
        }

    async def _finalize_aria2_torrent(self, torrent_id: int):
        async with get_db() as db:
            torrent = await (await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))).fetchone()
            if not torrent or _terminal_torrent_status(torrent["status"]):
                return

            torrent_dict = dict(torrent)  # always available below

            counts = await (
                await db.execute(
                    """SELECT
                           SUM(CASE WHEN blocked=0 THEN 1 ELSE 0 END) AS required_count,
                           SUM(CASE WHEN blocked=0 AND status='completed' THEN 1 ELSE 0 END) AS completed_count,
                           SUM(CASE WHEN blocked=0 AND status='error' THEN 1 ELSE 0 END) AS error_count,
                           SUM(CASE WHEN blocked=0 AND status IN ('pending', 'queued', 'downloading', 'paused') THEN 1 ELSE 0 END) AS active_count,
                           SUM(CASE WHEN blocked=0 AND status='paused' THEN 1 ELSE 0 END) AS paused_count,
                           COUNT(*) AS total_files
                       FROM download_files WHERE torrent_id=?""",
                    (torrent_id,),
                )
            ).fetchone()

            required_count = int(counts["required_count"] or 0)
            completed_count = int(counts["completed_count"] or 0)
            error_count = int(counts["error_count"] or 0)
            active_count = int(counts["active_count"] or 0)
            paused_count = int(counts["paused_count"] or 0)
            total_files = int(counts["total_files"] or 0)

            should_complete = False

            if total_files == 0:
                # No file records yet — _download() hasn't run, nothing to do
                return
            elif required_count == 0:
                # All files were filtered/blocked — nothing to download
                should_complete = True
                event_msg = "All files were filtered/blocked — marked completed"
            elif required_count > 0 and completed_count == required_count and error_count == 0 and active_count == 0:
                should_complete = True
                event_msg = f"aria2 completed {completed_count} files"
            elif error_count > 0 and active_count == 0:
                await db.execute(
                    "UPDATE torrents SET status='error', error_message='One or more aria2 transfers failed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (torrent_id,),
                )
                await db.commit()
                if get_settings().discord_notify_error:
                    await self.notify().send_error(
                        torrent_dict["name"],
                        reason="One or more aria2 transfers failed",
                        source="aria2",
                        provider="aria2",
                    )
                return
            elif active_count > 0:
                new_status = "paused" if paused_count == active_count and active_count > 0 else "queued"
                await db.execute("UPDATE torrents SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, torrent_id))
                await db.commit()
                return
            else:
                return

            if should_complete:
                # Recompute total size from actual file sizes — aria2 provides the
                # authoritative value once downloads run, overwriting any 0 from AllDebrid.
                size_row = await (
                    await db.execute(
                        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM download_files WHERE torrent_id=?",
                        (torrent_id,),
                    )
                ).fetchone()
                total_size = int(size_row["total"] or 0)
                if total_size > 0:
                    await db.execute(
                        """UPDATE torrents
                           SET status='completed', completed_at=CURRENT_TIMESTAMP,
                               size_bytes=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (total_size, torrent_id),
                    )
                else:
                    await db.execute(
                        """UPDATE torrents
                           SET status='completed', completed_at=CURRENT_TIMESTAMP,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (torrent_id,),
                    )
                await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, "info", event_msg))
                await db.commit()

        await self._delete_magnet_after_completion(torrent_id, torrent_dict["alldebrid_id"])
        await self._mark_finished(torrent_id, name=torrent_dict.get("name",""))
        if get_settings().discord_notify_finished:
            await self.notify().send_complete(
                torrent_dict["name"],
                file_count=completed_count,
                size_bytes=total_size,
                download_client="aria2",
            )

    async def pause_torrent(self, torrent_id: int):
        if self.download_client_name() != "aria2":
            raise ValueError("Pause is only supported for the aria2 download client")
        async with get_db() as db:
            rows = await (
                await db.execute(
                    "SELECT download_id FROM download_files WHERE torrent_id=? AND download_client='aria2' AND blocked=0 AND download_id IS NOT NULL",
                    (torrent_id,),
                )
            ).fetchall()
        for row in rows:
            await self.aria2().pause(row["download_id"])
        async with get_db() as db:
            await db.execute("UPDATE download_files SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE torrent_id=? AND download_client='aria2' AND blocked=0", (torrent_id,))
            await db.execute("UPDATE torrents SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            await db.commit()
        await self._log_event(torrent_id, "info", "Paused aria2 transfer queue")

    async def resume_torrent(self, torrent_id: int):
        if self.download_client_name() != "aria2":
            raise ValueError("Resume is only supported for the aria2 download client")
        async with get_db() as db:
            rows = await (
                await db.execute(
                    "SELECT download_id FROM download_files WHERE torrent_id=? AND download_client='aria2' AND blocked=0 AND download_id IS NOT NULL",
                    (torrent_id,),
                )
            ).fetchall()
        for row in rows:
            await self.aria2().resume(row["download_id"])
        async with get_db() as db:
            await db.execute("UPDATE download_files SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE torrent_id=? AND download_client='aria2' AND blocked=0", (torrent_id,))
            await db.execute("UPDATE torrents SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            await db.commit()
        await self._log_event(torrent_id, "info", "Resumed aria2 transfer queue")

    async def _send_partial_summary(self, torrent_id: int, torrent_name: str, flat_files: List[Dict], blocked_items: List[dict], transferred_items: List[dict], failed_items: List[dict]):
        if not blocked_items:
            return
        total_size = _size_sum([{"size_bytes": int(item.get("size", 0) or 0)} for item in flat_files])
        downloaded_size = _size_sum(transferred_items)
        await self._log_event(torrent_id, "warn", "Filtered files were skipped while the remaining files continued normally")
        if get_settings().discord_webhook_url:
            await self.notify().send_partial(
                name=torrent_name,
                total_files=len(flat_files),
                downloaded_files=len(transferred_items),
                blocked_files=len(blocked_items) + len(failed_items),
                total_size=total_size,
                downloaded_size=downloaded_size,
            )

    # Direct download mode removed — aria2 handles all transfers

    async def _log_file(
        self,
        torrent_id: int,
        filename: str,
        url: str,
        local: Optional[str],
        status: str,
        reason: Optional[str],
        size_bytes: int = 0,
        download_id: Optional[str] = None,
        download_client: str = "aria2",
    ):
        async with get_db() as db:
            await db.execute(
                """INSERT INTO download_files
                   (torrent_id, filename, size_bytes, download_url, local_path, status, download_id, download_client, blocked, block_reason, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    torrent_id,
                    filename,
                    size_bytes,
                    url,
                    local,
                    status,
                    download_id,
                    download_client,
                    1 if status == "blocked" else 0,
                    reason,
                ),
            )
            await db.commit()

    async def _log_event(self, torrent_id: int, level: str, message: str):
        async with get_db() as db:
            await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, level, message))
            await db.commit()

    async def _delete_magnet_after_completion(self, torrent_id: int, ad_id: str) -> bool:
        """
        Deletes the magnet from AllDebrid after a successful download.
        Status stays 'completed' so the dashboard counts it correctly.
        sync_alldebrid_status already filters status NOT IN ('completed','deleted','error').
        """
        ad_id = str(ad_id or "").strip()
        if not ad_id or ad_id.lower() in ("none", "null", ""):
            logger.warning(
                "torrent %s: skipping AllDebrid deletion — no alldebrid_id", torrent_id
            )
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                    (torrent_id, "Completed locally, but no AllDebrid ID — cannot remove from AllDebrid"),
                )
                await db.commit()
            return False

        logger.info("torrent %s: removing from AllDebrid (id=%s)", torrent_id, ad_id)
        deleted = await self.ad().delete_magnet(ad_id)
        async with get_db() as db:
            msg = ("Removed from AllDebrid after completion" if deleted
                   else f"Completed, but AllDebrid removal failed (id={ad_id})")
            level = "info" if deleted else "warn"
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, level, msg),
            )
            await db.commit()
        if not deleted:
            logger.warning(
                "torrent %s: failed to remove from AllDebrid (id=%s) — "
                "it may have already been deleted or the API key lacks permission",
                torrent_id, ad_id,
            )
        return deleted

    async def _mark_finished(self, torrent_id: int, name: str = ""):
        await self._log_event(torrent_id, "info", "Finished")
        # Notify Sonarr + Radarr after completion
        try:
            from services.integrations import notify_sonarr, notify_radarr
            await asyncio.gather(
                notify_sonarr(name),
                notify_radarr(name),
                return_exceptions=True,
            )
        except Exception as exc:
            logger.warning("Integration notify failed: %s", exc)

    async def _fail_torrent(self, torrent_id: int, message: str, notify: bool = False):
        async with get_db() as db:
            row = await (await db.execute(
                "SELECT name, alldebrid_id, provider_status_code FROM torrents WHERE id=?",
                (torrent_id,),
            )).fetchone()
            await db.execute(
                "UPDATE torrents SET status='error', error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (message, torrent_id),
            )
            await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, "error", message))
            await db.commit()
        if notify and row:
            await self._notify_provider_error(
                row["name"],
                reason=message,
                context="Torrent marked as failed during processing",
                alldebrid_id=str(row.get("alldebrid_id") or ""),
                status_code=row.get("provider_status_code"),
            )

    async def _set_deleted(self, torrent_id: int, message: str):
        async with get_db() as db:
            await db.execute("UPDATE torrents SET status='deleted', updated_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, "warn", message))
            await db.commit()

    async def import_existing_magnets(self) -> List[dict]:
        if self.is_paused():
            return []
        try:
            all_magnets = await self.ad().get_magnet_status()
        except Exception as exc:
            error = str(exc)
            if any(keyword in error for keyword in ("DISCONTINUED", "discontinued", "deprecated", "migrate")):
                raise Exception("AllDebrid has disabled 'list all magnets' for your account. Add magnets manually via the UI or watch folder.")
            raise

        if not all_magnets:
            return []

        # Fetch aria2 state once — used to check per-file progress during import
        aria2_by_uri: Dict[str, "Aria2DownloadStatus"] = {}
        aria2_by_path: Dict[str, "Aria2DownloadStatus"] = {}
        if self.download_client_name() == "aria2":
            try:
                for dl in await self._aria2_get_all():
                    for fi in dl.files or []:
                        current_path = _normalize_aria2_path(str(fi.get("path", "")))
                        if current_path:
                            aria2_by_path[current_path] = dl
                        for u in fi.get("uris", []) or []:
                            uri = str(u.get("uri", "")).strip()
                            if uri:
                                aria2_by_uri[uri] = dl
            except Exception as exc:
                logger.warning("import_existing_magnets: could not fetch aria2 state: %s", exc)

        results = []
        async with get_db() as db:
            for magnet in all_magnets:
                ad_id = str(magnet.get("id", ""))
                hash_value = magnet.get("hash", ad_id).lower()
                name = magnet.get("filename") or magnet.get("name") or hash_value
                normalized = normalize_provider_state(magnet)
                cur = await db.execute("SELECT id, status FROM torrents WHERE hash=?", (hash_value,))
                existing = await cur.fetchone()
                should_queue = True
                if existing:
                    torrent_id = existing["id"]
                    if not _terminal_torrent_status(existing["status"]):
                        await db.execute(
                            """UPDATE torrents
                               SET name=?, alldebrid_id=?, provider_status=?, provider_status_code=?, download_client=?, updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (name, ad_id, normalized["provider_status"], normalized["status_code"], self.download_client_name(), torrent_id),
                        )
                    else:
                        should_queue = False
                else:
                    torrent_id = await db.execute_returning_id(
                        """INSERT INTO torrents
                           (hash, name, alldebrid_id, status, source, provider_status, provider_status_code, download_client)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (hash_value, name, ad_id, normalized["local_status"], "alldebrid_existing",
                         normalized["provider_status"], normalized["status_code"], self.download_client_name()),
                    )
                results.append({
                    "hash": hash_value,
                    "name": name,
                    "id": ad_id,
                    "status": normalized["local_status"],
                    "torrent_id": torrent_id,
                    "should_queue": should_queue,
                })
            await db.commit()

        for item in results:
            if item["status"] != "ready" or not item["should_queue"]:
                continue

            torrent_id = item["torrent_id"]
            ad_id = item["id"]

            # When using aria2, check existing download_files against aria2 state
            # before blindly re-queuing everything.
            #
            # Why URI-based and not GID-based:
            # aria2 GIDs are ephemeral — they are assigned at queue time and are not
            # persisted across aria2 restarts. After an aria2 restart all stopped/complete
            # entries are gone and active ones get new GIDs. The only stable identifier
            # we share with aria2 is the download URL (the unlocked AllDebrid link stored
            # in download_files.download_url).
            #
            # Note: unlocked AllDebrid links expire after some time, so for a torrent
            # that has never been through _download() (no download_files rows yet) we
            # cannot match against aria2 at all — we simply call _start_download which
            # generates fresh links and lets ensure_download handle deduplication.
            if self.download_client_name() == "aria2":
                async with get_db() as db:
                    file_rows = await (
                        await db.execute(
                            "SELECT id AS file_id, download_url, download_id, local_path, status "
                            "FROM download_files WHERE torrent_id=? AND blocked=0 AND download_client='aria2'",
                            (torrent_id,),
                        )
                    ).fetchall()

                if file_rows:
                    completed = 0
                    needs_reset = False
                    for fr in file_rows:
                        remote_path = ""
                        if fr["local_path"]:
                            try:
                                remote_path = _normalize_aria2_path(self._remote_aria2_path(Path(fr["local_path"])))
                            except Exception:
                                remote_path = _normalize_aria2_path(str(fr["local_path"]))
                        url = str(fr["download_url"] or "").strip()
                        dl = aria2_by_path.get(remote_path) if remote_path else None
                        if dl is None:
                            dl = aria2_by_uri.get(url)
                        if dl is None:
                            # Not tracked in aria2 — needs re-queue
                            needs_reset = True
                        elif dl.status == "complete":
                            await self._update_file_state(fr["file_id"], "completed", fr["local_path"])
                            await self.aria2().remove(dl.gid)
                            completed += 1
                        elif dl.status == "removed":
                            needs_reset = True
                        elif dl.status == "error":
                            reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                            await self._update_file_state(fr["file_id"], "error", fr["local_path"], reason=reason)
                            await self.aria2().remove(dl.gid)
                            needs_reset = True
                        # active/waiting/paused → already tracked, no action needed

                    if needs_reset:
                        await self._reset_torrent_for_redownload(
                            torrent_id, "Partial/missing aria2 state on import — reset for re-download"
                        )
                        asyncio.create_task(self._start_download(torrent_id, ad_id, item["name"]))
                    else:
                        # All files accounted for — let _finalize decide if we're done
                        await self._finalize_aria2_torrent(torrent_id)
                    continue  # handled above

            asyncio.create_task(self._start_download(torrent_id, ad_id, item["name"]))
        return results

    async def delete_torrent(self, torrent_id: int, delete_from_ad: bool = True):
        async with get_db() as db:
            row = await (await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))).fetchone()
            if not row:
                raise ValueError("Torrent not found")
            file_rows = await (
                await db.execute(
                    "SELECT download_id FROM download_files WHERE torrent_id=? AND download_client='aria2' AND download_id IS NOT NULL",
                    (torrent_id,),
                )
            ).fetchall()
            await db.execute("UPDATE torrents SET status='deleted', updated_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            await db.commit()

        for file_row in file_rows:
            await self.aria2().remove(file_row["download_id"])

        if delete_from_ad and row["alldebrid_id"] and row["status"] != "completed":
            await self.ad().delete_magnet(row["alldebrid_id"])

    async def test_aria2(self) -> dict:
        if not get_settings().aria2_url:
            raise Exception("aria2 URL not configured")
        test = await self.aria2().test()
        diagnostics = await self._aria2_get_memory_diagnostics()
        return {**test, "diagnostics": diagnostics}

    async def apply_aria2_memory_tuning(self) -> dict:
        cfg = get_settings()
        if not getattr(cfg, "aria2_url", "").strip():
            return {"ok": False, "skipped": True, "reason": "aria2 URL not configured"}
        options = {
            "max-download-result": str(int(getattr(cfg, "aria2_max_download_result", 200) or 200)),
            "keep-unfinished-download-result": "true" if bool(getattr(cfg, "aria2_keep_unfinished_download_result", False)) else "false",
        }
        await self.aria2().change_global_options(options)
        return {"ok": True, "applied": options}

    async def run_aria2_housekeeping(self) -> dict:
        cfg = get_settings()
        await self.apply_aria2_memory_tuning()
        await self.aria2().purge_download_results()
        diagnostics = await self._aria2_get_memory_diagnostics()
        return {"ok": True, "diagnostics": diagnostics}


manager = TorrentManager()
