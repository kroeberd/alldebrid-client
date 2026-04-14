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
from db.database import DB_PATH
from services.alldebrid import AllDebridService, flatten_files
from services.aria2 import Aria2Service
from services.notifications import NotificationService

logger = logging.getLogger("alldebrid.manager")

READY_CODE = 4
ERROR_CODES = set(range(5, 16))
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
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)[:200].strip() or "download"


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
        return NotificationService(get_settings().discord_webhook_url)

    def download_client_name(self) -> str:
        client = (get_settings().download_client or "direct").strip().lower()
        return client if client in {"direct", "aria2"} else "direct"

    async def scan_watch_folder(self):
        if self.is_paused():
            return
        cfg = get_settings()
        watch = Path(cfg.watch_folder)
        processed = Path(cfg.processed_folder)
        try:
            watch.mkdir(parents=True, exist_ok=True)
            processed.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error("Watch folder inaccessible: %s", exc)
            return

        for file_path in list(watch.iterdir()):
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
                # Write to DB events so the error shows in the UI
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO events (torrent_id, level, message) VALUES (NULL, 'error', ?)",
                        (f"Watch folder error [{file_path.name}]: {exc}",),
                    )
                    await db.commit()
            finally:
                self._processing_files.discard(key)

    async def _handle_torrent(self, path: Path, processed: Path):
        if not get_settings().alldebrid_api_key:
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
        shutil.move(str(path), str(processed / path.name))

    async def _handle_magnet_file(self, path: Path, processed: Path):
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

    async def _add_magnet(self, magnet: str, hash_value: str, source: str) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
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
            await self.notify().send("Added", f"**{name}**\nQueued on AllDebrid", 0x3B82F6)
        return row

    async def _upsert(self, hash_value: str, magnet: Optional[str], name: str, ad_id: str, source: str) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
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

    async def sync_alldebrid_status(self):
        if self.is_paused() or not get_settings().alldebrid_api_key:
            return

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """SELECT id, name, alldebrid_id, status, provider_status, provider_status_code, polling_failures
                       FROM torrents
                       WHERE alldebrid_id IS NOT NULL AND alldebrid_id != ''
                         AND status NOT IN ('completed', 'deleted', 'error')"""
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

    async def _apply_provider_update(self, row: aiosqlite.Row, magnet: Dict, normalized: Dict[str, object]):
        provider_status = str(normalized["provider_status"])
        local_status = str(normalized["local_status"])
        status_code = int(normalized["status_code"])
        progress = float(normalized["progress"])
        size_bytes = int(normalized["size_bytes"])
        provider_message = str(normalized["message"])
        current_status = row["status"]
        persisted_status = current_status if current_status in {"queued", "downloading", "paused"} and provider_status == "ready" else local_status

        async with aiosqlite.connect(DB_PATH) as db:
            if provider_status != (row["provider_status"] or "") or status_code != int(row["provider_status_code"] or -1):
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

        if provider_status == "ready" and current_status in {"pending", "uploading", "processing", "ready"}:
            name = magnet.get("filename") or magnet.get("name") or row["name"]
            asyncio.create_task(self._start_download(row["id"], str(row["alldebrid_id"]), str(name)))
        elif provider_status == "error" and current_status != "error":
            error_message = f"AllDebrid error code {status_code}: {provider_message}".strip()
            await self._fail_torrent(row["id"], error_message, notify=True)

    async def _increment_poll_failure(self, torrent_id: int, name: str, reason: str):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
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

        if failures >= PROVIDER_FAILURE_THRESHOLD and get_settings().discord_notify_error:
            await self.notify().send("Error", f"**{name}**\n{reason}", 0xEF4444)

    async def _start_download(self, torrent_id: int, ad_id: str, name: str):
        if self.is_paused() or torrent_id in self._active:
            return
        self._active.add(torrent_id)
        try:
            async with self.sem():
                await self._download(torrent_id, ad_id, name)
        except Exception as exc:
            logger.error("Download failed db_id=%s: %s", torrent_id, exc)
            await self._fail_torrent(torrent_id, str(exc), notify=True)
        finally:
            self._active.discard(torrent_id)

    async def _download(self, torrent_id: int, ad_id: str, name: str):
        cfg = get_settings()
        client_name = self.download_client_name()
        initial_status = "queued" if client_name == "aria2" else "downloading"

        async with aiosqlite.connect(DB_PATH) as db:
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
        destination_root.mkdir(parents=True, exist_ok=True)

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

                if client_name == "aria2":
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
                else:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    await _retry_async(self._download_direct, download_url, local_path)
                    transferred_items.append({"filename": display_name, "size_bytes": file_size})
                    await self._log_file(
                        torrent_id,
                        display_name,
                        download_url,
                        str(local_path),
                        "completed",
                        None,
                        file_size,
                        download_client="direct",
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

        if failed_count == 0 and completed_count == downloadable_count and downloadable_count > 0:
            final_status = "completed"
        elif failed_count == 0 and completed_count + queued_count == downloadable_count and queued_count > 0:
            final_status = "queued"
        elif blocked_count > 0 and failed_count == 0 and completed_count + queued_count > 0:
            final_status = "queued" if queued_count > 0 else "completed"
        else:
            final_status = "error"

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE torrents SET status=?, local_path=?, size_bytes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (final_status, str(destination_root), total_size_bytes, torrent_id),
            )
            if final_status == "completed":
                await db.execute("UPDATE torrents SET completed_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, "info" if final_status in {"completed", "queued", "paused"} else "warn", f"Download {final_status}: {completed_count + queued_count} files prepared"),
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
            await self._mark_finished(torrent_id)
            if cfg.discord_notify_finished:
                await self.notify().send("Complete", f"**{name}**\n{completed_count} files -> `{destination_root}`", 0x22C55E)
        elif final_status in {"queued", "paused"}:
            await self._log_event(
                torrent_id,
                "info",
                "Prepared for slot-based aria2 delivery",
            )
            await self._dispatch_pending_aria2_queue()
        else:
            if cfg.discord_notify_error:
                await self.notify().send("Error", f"**{name}**\nKept on AllDebrid for inspection", 0xEF4444)

    async def _fetch_ready_files(self, ad_id: str) -> List[Dict]:
        for attempt in range(1, READY_FILE_RETRIES + 1):
            files_data = await self.ad().get_magnet_files([ad_id])
            for entry in files_data:
                if str(entry.get("id", "")) == str(ad_id):
                    flat_files = flatten_files(entry.get("files", []))
                    if flat_files:
                        return flat_files
            await asyncio.sleep(attempt)
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

    async def _dispatch_pending_aria2_queue(self, all_downloads=None):
        if self.download_client_name() != "aria2" or self.is_paused():
            return

        async with self._aria2_dispatch_lock:
            current_downloads = all_downloads if all_downloads is not None else await self.aria2().get_all()
            in_flight = [dl for dl in current_downloads if dl.status in {"active", "waiting", "paused"}]
            available_slots = max(0, self._aria2_slot_limit() - len(in_flight))
            if available_slots <= 0:
                return

            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                pending_rows = await (
                    await db.execute(
                        """SELECT f.id AS file_id, f.torrent_id, f.filename, f.download_url, f.local_path, t.name AS torrent_name
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

            for row in pending_rows:
                source_link = str(row["download_url"] or "").strip()
                local_path = Path(row["local_path"])
                try:
                    unlocked = await _retry_async(self.ad().unlock_link, source_link)
                    download_url = unlocked.get("link", "")
                    if not download_url:
                        raise Exception("Empty download URL from unlock")

                    remote_path = self._remote_aria2_path(local_path)
                    remote_dir = str(PurePosixPath(remote_path).parent)
                    remote_name = PurePosixPath(remote_path).name
                    gid = await _retry_async(
                        self.aria2().ensure_download,
                        download_url,
                        {"dir": remote_dir, "out": remote_name},
                        get_settings().aria2_start_paused,
                    )
                    queued_status = "paused" if get_settings().aria2_start_paused else "queued"
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            """UPDATE download_files
                               SET status=?, download_id=?, download_url=?, updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (queued_status, gid, download_url, row["file_id"]),
                        )
                        await db.execute(
                            "UPDATE torrents SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('completed','deleted','error')",
                            (queued_status if get_settings().aria2_start_paused else "queued", row["torrent_id"]),
                        )
                        await db.commit()
                except Exception as exc:
                    logger.error("aria2 dispatch failed [%s]: %s", row["filename"], exc)
                    await self._update_file_state(row["file_id"], "error", row["local_path"], reason=str(exc))
                    await self._finalize_aria2_torrent(row["torrent_id"])

    async def sync_download_clients(self):
        if self.download_client_name() == "aria2":
            await self.sync_aria2_downloads()

    async def sync_aria2_downloads(self):
        if self.is_paused() or self.download_client_name() != "aria2":
            return

        all_downloads = await self.aria2().get_all()
        by_gid, uri_to_dl, path_to_dl = self._build_aria2_indexes(all_downloads)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """SELECT t.id AS torrent_id, t.name, t.alldebrid_id, t.status AS torrent_status,
                              f.id AS file_id, f.filename, f.local_path, f.download_url,
                              f.download_id, f.status, f.blocked
                       FROM torrents t
                       JOIN download_files f ON f.torrent_id = t.id
                       WHERE f.download_client='aria2'
                         AND f.blocked=0
                         AND f.status IN ('pending', 'queued', 'downloading', 'paused')"""
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
                    async with aiosqlite.connect(DB_PATH) as db:
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
            elif dl.status in {"complete", "removed"}:
                await self._update_file_state(row["file_id"], "completed", row["local_path"], size_bytes=sz)
                await self.aria2().remove(dl.gid)
                touched.add(row["torrent_id"])
            elif dl.status == "error":
                reason = f"{dl.error_code}: {dl.error_message}".strip(": ")
                await self._update_file_state(row["file_id"], "error", row["local_path"], reason=reason, size_bytes=sz)
                await self.aria2().remove(dl.gid)
                touched.add(row["torrent_id"])

        # Reset torrents whose entries are gone from aria2 (can't confirm completion)
        for torrent_id in reset_on_sync - touched:
            await self._reset_torrent_for_redownload(
                torrent_id, "aria2 entry lost during sync — reset for re-download"
            )
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                t = await (await db.execute(
                    "SELECT alldebrid_id, name FROM torrents WHERE id=?", (torrent_id,)
                )).fetchone()
            if t and t["alldebrid_id"]:
                asyncio.create_task(
                    self._start_download(torrent_id, str(t["alldebrid_id"]), str(t["name"] or ""))
                )

        # Finalize torrents where aria2 reported complete or error
        for torrent_id in touched:
            await self._finalize_aria2_torrent(torrent_id)

        await self._dispatch_pending_aria2_queue()

    async def _reset_torrent_for_redownload(self, torrent_id: int, reason: str):
        """Clear download_files and mark torrent as downloading so the sync loop
        ignores it while _start_download/_download re-runs and re-registers
        the new URIs with aria2. Status is updated to 'queued' or 'paused' once
        _download() completes and the new download_files rows are written."""
        async with aiosqlite.connect(DB_PATH) as db:
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
            all_downloads = await self.aria2().get_all()
        except Exception as exc:
            logger.warning("Startup aria2 reconciliation skipped: %s", exc)
            return

        all_downloads = await self._dedupe_aria2_downloads_on_startup(all_downloads)

        by_gid, uri_to_dl, path_to_dl = self._build_aria2_indexes(all_downloads)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
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
                    async with aiosqlite.connect(DB_PATH) as db:
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
            if dl.status in {"complete", "removed"}:
                await self._update_file_state(row["file_id"], "completed", row["local_path"], size_bytes=sz)
                await self.aria2().remove(dl.gid)
                touched.add(row["torrent_id"])
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
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
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
        async with aiosqlite.connect(DB_PATH) as db:
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

    async def _finalize_aria2_torrent(self, torrent_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
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
                    await self.notify().send("Error", f"**{torrent_dict['name']}**\nOne or more aria2 transfers failed", 0xEF4444)
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
                await db.execute(
                    """UPDATE torrents
                       SET status='completed', completed_at=CURRENT_TIMESTAMP,
                           size_bytes=CASE WHEN ? > 0 THEN ? ELSE size_bytes END,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (total_size, total_size, torrent_id),
                )
                await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, "info", event_msg))
                await db.commit()

        await self._delete_magnet_after_completion(torrent_id, torrent_dict["alldebrid_id"])
        await self._mark_finished(torrent_id)
        if get_settings().discord_notify_finished:
            await self.notify().send("Complete", f"**{torrent_dict['name']}**\n{completed_count} files finished via aria2", 0x22C55E)

    async def pause_torrent(self, torrent_id: int):
        if self.download_client_name() != "aria2":
            raise ValueError("Pause is only supported for the aria2 download client")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT download_id FROM download_files WHERE torrent_id=? AND download_client='aria2' AND blocked=0 AND download_id IS NOT NULL",
                    (torrent_id,),
                )
            ).fetchall()
        for row in rows:
            await self.aria2().pause(row["download_id"])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE download_files SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE torrent_id=? AND download_client='aria2' AND blocked=0", (torrent_id,))
            await db.execute("UPDATE torrents SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            await db.commit()
        await self._log_event(torrent_id, "info", "Paused aria2 transfer queue")

    async def resume_torrent(self, torrent_id: int):
        if self.download_client_name() != "aria2":
            raise ValueError("Resume is only supported for the aria2 download client")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT download_id FROM download_files WHERE torrent_id=? AND download_client='aria2' AND blocked=0 AND download_id IS NOT NULL",
                    (torrent_id,),
                )
            ).fetchall()
        for row in rows:
            await self.aria2().resume(row["download_id"])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE download_files SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE torrent_id=? AND download_client='aria2' AND blocked=0", (torrent_id,))
            await db.execute("UPDATE torrents SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE id=?", (torrent_id,))
            await db.commit()
        await self._log_event(torrent_id, "info", "Resumed aria2 transfer queue")

    async def _send_partial_summary(self, torrent_id: int, torrent_name: str, flat_files: List[Dict], blocked_items: List[dict], transferred_items: List[dict], failed_items: List[dict]):
        if not blocked_items:
            return
        total_size = _size_sum([{"size_bytes": int(item.get("size", 0) or 0)} for item in flat_files])
        lines = [
            f"**{torrent_name}**",
            f"Total files: {len(flat_files)} ({fmt_bytes(total_size)})",
            f"Will be downloaded: {len(transferred_items)} ({fmt_bytes(_size_sum(transferred_items))})",
            f"Not downloaded: {len(blocked_items) + len(failed_items)} ({fmt_bytes(_size_sum(blocked_items + failed_items))})",
            f"Excluded: {len(blocked_items)} ({fmt_bytes(_size_sum(blocked_items))})",
        ]
        if failed_items:
            lines.append(f"Failed: {len(failed_items)} ({fmt_bytes(_size_sum(failed_items))})")
        await self._log_event(torrent_id, "warn", "Filtered files were skipped while the remaining files continued normally")
        if get_settings().discord_webhook_url:
            await self.notify().send("Partial", "\n".join(lines), 0x8B5CF6)

    async def _download_direct(self, url: str, local_path: Path) -> str:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3600)) as response:
                response.raise_for_status()
                async with aiofiles.open(local_path, "wb") as file_handle:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        await file_handle.write(chunk)
        return str(local_path)

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
        download_client: str = "direct",
    ):
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, level, message))
            await db.commit()

    async def _delete_magnet_after_completion(self, torrent_id: int, ad_id: str) -> bool:
        deleted = await self.ad().delete_magnet(ad_id)
        async with aiosqlite.connect(DB_PATH) as db:
            if deleted:
                # Mark as deleted so import_existing_magnets and sync_alldebrid_status
                # never pick this torrent up again, even if AllDebrid still lists it briefly.
                await db.execute(
                    "UPDATE torrents SET status='deleted', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (torrent_id,),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                    (torrent_id, "info", "Removed from AllDebrid after completion"),
                )
            else:
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                    (torrent_id, "warn", "Finished, but removal from AllDebrid failed — will retry next cycle"),
                )
            await db.commit()
        return deleted

    async def _mark_finished(self, torrent_id: int):
        await self._log_event(torrent_id, "info", "Finished")

    async def _fail_torrent(self, torrent_id: int, message: str, notify: bool = False):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT name FROM torrents WHERE id=?", (torrent_id,))).fetchone()
            await db.execute(
                "UPDATE torrents SET status='error', error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (message, torrent_id),
            )
            await db.execute("INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)", (torrent_id, "error", message))
            await db.commit()
        if notify and row and get_settings().discord_notify_error:
            await self.notify().send("Error", f"**{row['name']}**\n{message}", 0xEF4444)

    async def _set_deleted(self, torrent_id: int, message: str):
        async with aiosqlite.connect(DB_PATH) as db:
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
                for dl in await self.aria2().get_all():
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
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
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
                    cur = await db.execute(
                        """INSERT INTO torrents
                           (hash, name, alldebrid_id, status, source, provider_status, provider_status_code, download_client)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (hash_value, name, ad_id, normalized["local_status"], "alldebrid_existing",
                         normalized["provider_status"], normalized["status_code"], self.download_client_name()),
                    )
                    torrent_id = cur.lastrowid
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
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
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
                        elif dl.status in {"complete", "removed"}:
                            await self._update_file_state(fr["file_id"], "completed", fr["local_path"])
                            await self.aria2().remove(dl.gid)
                            completed += 1
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
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
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
        return await self.aria2().test()


manager = TorrentManager()
