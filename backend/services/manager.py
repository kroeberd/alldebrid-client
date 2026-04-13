import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Optional, List, Tuple, Set
import aiosqlite

from core.config import get_settings, AppSettings
from services.alldebrid import AllDebridService, flatten_files
from services.jdownloader import MyJDownloaderClient
from services.notifications import NotificationService
from db.database import DB_PATH

logger = logging.getLogger("alldebrid.manager")

READY_CODE = 4
ERROR_CODES = set(range(5, 16))
MAX_FILE_RETRIES = 3


def extract_hash(magnet: str) -> Optional[str]:
    m = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet, re.I)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 32:
        try:
            import base64
            h = base64.b32decode(h.upper()).hex()
        except Exception:
            return None
    return h.lower()


def is_blocked(filename: str, cfg: AppSettings) -> Tuple[bool, str]:
    ext = Path(filename).suffix.lower()
    if ext in [e.lower() for e in cfg.blocked_extensions]:
        return True, f"extension {ext}"
    for kw in cfg.blocked_keywords:
        if kw.lower() in filename.lower():
            return True, f"keyword '{kw}'"
    return False, ""


def safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s)[:200].strip() or "download"


def _size_sum(items: List[dict]) -> int:
    return sum(int(item.get("size_bytes", 0) or 0) for item in items)


def fmt_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size or 0)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}"


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _read_text(path: str) -> str:
    with open(path, "r", errors="ignore") as f:
        return f.read()


def _terminal_torrent_status(status: str) -> bool:
    return status in ("completed", "deleted", "error")


async def _retry_async(fn, *args, attempts: int = MAX_FILE_RETRIES, delay: float = 1.0):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn(*args)
        except Exception as e:
            last_error = e
            if attempt >= attempts:
                break
            await asyncio.sleep(delay * attempt)
    raise last_error


class TorrentManager:
    def __init__(self):
        self._ad: Optional[AllDebridService] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._active: Set[int] = set()
        self._processing_files: Set[str] = set()
        self._failed_files: Set[str] = set()

    def is_paused(self) -> bool:
        return bool(get_settings().paused)

    def reset_services(self):
        if self._ad:
            try:
                asyncio.get_event_loop().create_task(self._ad.close())
            except RuntimeError:
                pass
        self._ad = None
        self._sem = None

    def ad(self) -> AllDebridService:
        if self._ad is None:
            cfg = get_settings()
            self._ad = AllDebridService(cfg.alldebrid_api_key, cfg.alldebrid_agent)
        return self._ad

    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(get_settings().max_concurrent_downloads)
        return self._sem

    def notify(self) -> NotificationService:
        return NotificationService(get_settings().discord_webhook_url)

    def jd(self) -> Optional[MyJDownloaderClient]:
        cfg = get_settings()
        if cfg.jdownloader_enabled and cfg.jdownloader_email:
            return MyJDownloaderClient(
                cfg.jdownloader_email,
                cfg.jdownloader_password,
                cfg.jdownloader_device_name,
            )
        return None

    # ── Watch Folder ──────────────────────────────────────────────────────────

    async def scan_watch_folder(self):
        if self.is_paused():
            return
        cfg = get_settings()
        watch     = Path(cfg.watch_folder)
        processed = Path(cfg.processed_folder)
        watch.mkdir(parents=True, exist_ok=True)
        processed.mkdir(parents=True, exist_ok=True)

        for f in list(watch.iterdir()):
            key = str(f.resolve())
            if key in self._processing_files or key in self._failed_files:
                continue
            if f.suffix.lower() not in (".torrent", ".magnet", ".txt"):
                continue
            self._processing_files.add(key)
            try:
                if f.suffix.lower() == ".torrent":
                    await self._handle_torrent(f, processed)
                else:
                    await self._handle_magnet_file(f, processed)
            except Exception as e:
                logger.error(f"Watch [{f.name}]: {e}")
                self._failed_files.add(key)
            finally:
                self._processing_files.discard(key)

    async def _handle_torrent(self, path: Path, processed: Path):
        if not get_settings().alldebrid_api_key:
            return
        loop = asyncio.get_event_loop()
        file_bytes = await loop.run_in_executor(None, _read_bytes, str(path))
        if not file_bytes:
            raise ValueError("Empty torrent file")
        result = await self.ad().upload_torrent_file(file_bytes, path.name)
        ad_id = str(result.get("id", ""))
        name  = result.get("name") or result.get("filename") or path.stem
        hash_ = result.get("hash", ad_id).lower()
        logger.info(f"Uploaded torrent: {name} (ad_id={ad_id})")
        await self._upsert(hash_, None, name, ad_id, "watch_torrent")
        shutil.move(str(path), str(processed / path.name))

    async def _handle_magnet_file(self, path: Path, processed: Path):
        loop = asyncio.get_event_loop()
        content = await loop.run_in_executor(None, _read_text, str(path))
        magnets = [l.strip() for l in content.splitlines() if l.strip().startswith("magnet:")]
        if not magnets:
            self._failed_files.add(str(path.resolve()))
            return
        for magnet in magnets:
            h = extract_hash(magnet)
            if h:
                await self._add_magnet(magnet, h, "watch_file")
        shutil.move(str(path), str(processed / path.name))

    # ── Magnet Upload ─────────────────────────────────────────────────────────

    async def add_magnet_direct(self, magnet: str, source: str = "manual") -> dict:
        if self.is_paused():
            raise Exception("Processing is paused")
        h = extract_hash(magnet)
        if not h:
            raise ValueError("Invalid magnet — no btih hash found")
        return await self._add_magnet(magnet, h, source)

    async def _add_magnet(self, magnet: str, h: str, source: str) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM torrents WHERE hash=?", (h,))
            existing = await cur.fetchone()
            if existing and existing["status"] in (
                "uploading", "processing", "downloading", "ready", "completed"
            ):
                return dict(existing)

        result = await self.ad().upload_magnet(magnet)
        ad_id = str(result.get("id", ""))
        name  = result.get("name") or result.get("filename") or h[:16]
        hash_ = result.get("hash", h).lower()
        logger.info(f"Magnet uploaded: {name} (ad_id={ad_id})")

        row = await self._upsert(hash_, magnet, name, ad_id, source)
        cfg = get_settings()
        if cfg.discord_notify_added:
            await self.notify().send("📥 Added", f"**{name}**\nQueued on AllDebrid", 0x3b82f6)
        return row

    async def _upsert(self, hash_: str, magnet: Optional[str],
                      name: str, ad_id: str, source: str) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """INSERT INTO torrents (hash,magnet,name,alldebrid_id,status,source)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(hash) DO UPDATE SET
                     alldebrid_id=excluded.alldebrid_id, name=excluded.name,
                     status='uploading', updated_at=CURRENT_TIMESTAMP""",
                (hash_, magnet, name, ad_id, "uploading", source),
            )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) "
                "SELECT id,'info',? FROM torrents WHERE hash=?",
                (f"Uploaded to AllDebrid (id={ad_id})", hash_),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM torrents WHERE hash=?", (hash_,))
            row = await cur.fetchone()
            return dict(row) if row else {}

    # ── Status Polling ────────────────────────────────────────────────────────

    async def sync_alldebrid_status(self):
        if self.is_paused():
            return
        cfg = get_settings()
        if not cfg.alldebrid_api_key:
            return

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT id, alldebrid_id, name, status FROM torrents
                   WHERE alldebrid_id IS NOT NULL AND alldebrid_id != ''
                   AND status NOT IN ('completed','deleted','error')"""
            )
            rows = await cur.fetchall()

        if not rows:
            await self.sync_jdownloader_downloads()
            return

        for row in rows:
            ad_id = row["alldebrid_id"]
            try:
                magnets = await self.ad().get_magnet_status(str(ad_id))
                if not magnets:
                    continue
                m = magnets[0]

                code = m.get("statusCode", 0)
                size = m.get("size", 0) or 0
                dl   = m.get("downloaded", 0) or 0
                pct  = (dl / size * 100) if size > 0 else 0

                if code == READY_CODE:
                    new_status = "ready"
                elif code in ERROR_CODES:
                    new_status = "error"
                else:
                    new_status = "processing"

                old_status = row["status"]
                persisted_status = old_status if old_status in ("downloading", "queued") and new_status == "ready" else new_status

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE torrents SET status=?,progress=?,size_bytes=?,"
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (persisted_status, pct, size, row["id"]),
                    )
                    if new_status == "ready" and old_status in ("pending", "uploading", "processing"):
                        await db.execute(
                            "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                            (row["id"], "info", "Ready — fetching download links"),
                        )
                        name = m.get("filename") or row["name"]
                        asyncio.create_task(self._start_download(row["id"], ad_id, name))
                    elif new_status == "error" and old_status != "error":
                        err_msg = f"AllDebrid error code {code}: {m.get('status', '')}"
                        await db.execute(
                            "UPDATE torrents SET error_message=? WHERE id=?",
                            (err_msg, row["id"])
                        )
                        await db.execute(
                            "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                            (row["id"], "error", err_msg),
                        )
                        if cfg.discord_notify_error:
                            await self.notify().send(
                                "❌ Error", f"**{row['name']}**\n{err_msg}", 0xef4444
                            )
                    await db.commit()

            except Exception as e:
                if "MAGNET_INVALID_ID" in str(e):
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE torrents SET status='deleted',"
                            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (row["id"],)
                        )
                        await db.commit()
                else:
                    logger.error(f"Status poll failed for {ad_id}: {e}")

        await self.sync_jdownloader_downloads()

    # ── Download ──────────────────────────────────────────────────────────────

    async def _start_download(self, db_id: int, ad_id: str, name: str):
        if self.is_paused():
            return
        if db_id in self._active:
            return
        self._active.add(db_id)
        try:
            async with self.sem():
                await self._download(db_id, ad_id, name)
        except Exception as e:
            logger.error(f"Download failed db_id={db_id}: {e}")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE torrents SET status='error',error_message=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(e), db_id)
                )
                await db.commit()
        finally:
            self._active.discard(db_id)

    async def _download(self, db_id: int, ad_id: str, name: str):
        cfg = get_settings()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE torrents SET status='downloading',"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?", (db_id,)
            )
            await db.commit()

        try:
            files_data = await self.ad().get_magnet_files([ad_id])
            flat_files = []
            for entry in files_data:
                if str(entry.get("id", "")) == str(ad_id):
                    flat_files = flatten_files(entry.get("files", []))
                    break
        except Exception as e:
            logger.error(f"get_magnet_files failed for {ad_id}: {e}")
            flat_files = []

        if not flat_files:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE torrents SET status='error',"
                    "error_message='No files returned from AllDebrid',"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?", (db_id,)
                )
                await db.commit()
            return

        dest = Path(cfg.download_folder) / safe_name(name)
        dest.mkdir(parents=True, exist_ok=True)

        all_ok = True
        n_done = 0
        queued_for_jd = False
        jd_links = []
        jd_client = self.jd()
        total_files = len(flat_files)
        blocked_items: List[dict] = []
        transferred_items: List[dict] = []
        failed_items: List[dict] = []

        for file_info in flat_files:
            filename = file_info["name"]
            link     = file_info["link"]
            file_size = int(file_info.get("size", 0) or 0)

            blocked, reason = is_blocked(filename, cfg)
            if blocked:
                logger.info(f"Blocked: {filename} ({reason})")
                blocked_items.append({"filename": filename, "size_bytes": file_size, "reason": reason})
                await self._log_file(db_id, filename, link, None, "blocked", reason, file_size)
                continue

            try:
                unlocked = await _retry_async(self.ad().unlock_link, link)
                dl_url   = unlocked.get("link", "")
                filename = unlocked.get("filename", filename)
                routed_filename = safe_name(filename)

                if not dl_url:
                    raise Exception("Empty download URL from unlock")

                logger.info(
                    f"Routing [{filename}]: jd={'yes' if jd_client else 'no'}"
                )

                if jd_client:
                    jd_links.append({
                        "filename": filename,
                        "routed_filename": routed_filename,
                        "download_url": dl_url,
                        "size_bytes": file_size,
                    })
                else:
                    local = await _retry_async(self._download_direct, dl_url, dest, routed_filename)
                    await self._log_file(db_id, filename, dl_url, local, "completed", None, file_size)
                    transferred_items.append({"filename": filename, "size_bytes": file_size})

                n_done += 1

            except Exception as e:
                logger.error(f"File failed [{filename}]: {e}")
                failed_items.append({"filename": filename, "size_bytes": file_size, "reason": str(e)})
                await self._log_file(db_id, filename, link, None, "error", str(e), file_size)
                all_ok = False

        if jd_client and jd_links:
            try:
                await _retry_async(
                    jd_client.add_package,
                    [item["download_url"] for item in jd_links],
                    str(dest),
                    safe_name(name),
                    cfg.jdownloader_autostart,
                    cfg.jdownloader_extract,
                )
                for item in jd_links:
                    logger.info(f"JDownloader: queued {item['filename']}")
                    await self._log_file(db_id, item["filename"], item["download_url"], None, "queued", None, item["size_bytes"])
                    transferred_items.append({"filename": item["filename"], "size_bytes": item["size_bytes"]})
                queued_for_jd = True
            except Exception as e:
                logger.error(f"JDownloader package failed [{name}]: {e}")
                for item in jd_links:
                    failed_items.append({"filename": item["filename"], "size_bytes": item["size_bytes"], "reason": str(e)})
                    await self._log_file(db_id, item["filename"], item["download_url"], None, "error", str(e), item["size_bytes"])
                all_ok = False

        blocked_count = len(blocked_items)
        failed_count = len(failed_items)
        transferred_count = len(transferred_items)
        downloadable_count = total_files - blocked_count
        has_exclusions = blocked_count > 0

        if failed_count == 0 and transferred_count == downloadable_count and downloadable_count > 0:
            if queued_for_jd:
                final = "queued"
            else:
                final = "completed"
        elif transferred_count > 0 or blocked_count > 0:
            final = "partial"
        else:
            final = "error"

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT name FROM torrents WHERE id=?", (db_id,))
            row = await cur.fetchone()
            tname = row["name"] if row else name
            if final == "completed":
                await db.execute(
                    "UPDATE torrents SET status=?,completed_at=CURRENT_TIMESTAMP,"
                    "local_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (final, str(dest), db_id),
                )
            else:
                await db.execute(
                    "UPDATE torrents SET status=?,local_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (final, str(dest), db_id),
                )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                (db_id, "info" if final in ("completed", "queued") else "warn",
                 f"Download {final}: {n_done} files"),
            )
            await db.commit()

        if has_exclusions:
            partial_lines = [
                f"**{tname}**",
                f"Total files: {total_files} ({fmt_bytes(_size_sum([{'size_bytes': int(f.get('size', 0) or 0)} for f in flat_files]))})",
                f"Will be downloaded: {transferred_count} ({fmt_bytes(_size_sum(transferred_items))})",
                f"Not downloaded: {blocked_count + failed_count} ({fmt_bytes(_size_sum(blocked_items + failed_items))})",
            ]
            if blocked_count:
                partial_lines.append(f"Excluded: {blocked_count} ({fmt_bytes(_size_sum(blocked_items))})")
            if failed_count:
                partial_lines.append(f"Failed: {failed_count} ({fmt_bytes(_size_sum(failed_items))})")
            await self._log_event(db_id, "warn", "Filtered files were skipped while the remaining files continued normally")
            if get_settings().discord_webhook_url and (get_settings().discord_notify_finished or get_settings().discord_notify_error):
                await self.notify().send("Partial", "\n".join(partial_lines), 0x8B5CF6)

        if final == "completed":
            await self._delete_magnet_after_completion(db_id, ad_id)
            await self._mark_finished(db_id)
            if get_settings().discord_notify_finished:
                await self.notify().send(
                    "Complete", f"**{tname}**\n{n_done} files -> `{dest}`", 0x22c55e
                )
        elif final == "queued":
            await self._log_event(
                db_id,
                "info",
                "Queued in JDownloader - waiting for completed downloads before deleting from AllDebrid",
            )
        elif get_settings().discord_notify_error:
            await self.notify().send(
                "Error", f"**{tname}**\nKept on AllDebrid", 0xef4444
            )

    async def _log_event(self, torrent_id: int, level: str, message: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                (torrent_id, level, message),
            )
            await db.commit()

    async def _delete_magnet_after_completion(self, torrent_id: int, ad_id: str) -> bool:
        deleted = await self.ad().delete_magnet(ad_id)
        if deleted:
            await self._log_event(torrent_id, "info", "Removed from AllDebrid after completion")
        else:
            await self._log_event(torrent_id, "warn", "Finished, but removal from AllDebrid failed")
        return deleted

    async def _mark_finished(self, torrent_id: int):
        await self._log_event(torrent_id, "info", "Finished")

    async def sync_jdownloader_downloads(self):
        if self.is_paused():
            return
        jd_client = self.jd()
        if not jd_client:
            return

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT t.id, t.name, t.alldebrid_id, t.local_path, f.id AS file_id,
                          f.filename, f.download_url, f.status, f.blocked
                   FROM torrents t
                   JOIN download_files f ON f.torrent_id = t.id
                   WHERE t.alldebrid_id IS NOT NULL AND t.alldebrid_id != ''
                     AND t.status NOT IN ('completed','deleted','error')
                     AND f.blocked = 0
                     AND f.status IN ('queued')"""
            )
            rows = await cur.fetchall()

        if not rows:
            return

        touched_torrents: Set[int] = set()
        for row in rows:
            local_path = None
            if row["local_path"]:
                local_path = str(Path(row["local_path"]) / safe_name(row["filename"]))
                if Path(local_path).exists():
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE download_files SET status='completed', local_path=? WHERE id=?",
                            (local_path, row["file_id"]),
                        )
                        await db.commit()
                    touched_torrents.add(row["id"])
                    continue

            try:
                state = await jd_client.get_download_state(row["download_url"], row["filename"])
            except Exception as e:
                logger.warning(f"JD status check failed for {row['filename']}: {e}")
                continue

            if not state.get("finished"):
                continue

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE download_files SET status='completed', local_path=? WHERE id=?",
                    (local_path, row["file_id"]),
                )
                await db.commit()

            touched_torrents.add(row["id"])

        for torrent_id in touched_torrents:
            await self._finalize_completed_jd_torrent(torrent_id)

    async def _finalize_completed_jd_torrent(self, torrent_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM torrents WHERE id=?",
                (torrent_id,),
            )
            torrent = await cur.fetchone()
            if not torrent or _terminal_torrent_status(torrent["status"]):
                return

            cur = await db.execute(
                """SELECT
                       SUM(CASE WHEN blocked = 0 THEN 1 ELSE 0 END) AS required_count,
                       SUM(CASE WHEN blocked = 0 AND status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                       SUM(CASE WHEN blocked = 0 AND status = 'error' THEN 1 ELSE 0 END) AS error_count
                   FROM download_files
                   WHERE torrent_id=?""",
                (torrent_id,),
            )
            counts = await cur.fetchone()

            required_count = int(counts["required_count"] or 0)
            completed_count = int(counts["completed_count"] or 0)
            error_count = int(counts["error_count"] or 0)
            if required_count == 0 or completed_count != required_count or error_count:
                return

            await db.execute(
                "UPDATE torrents SET status='completed', completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_id,),
            )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                (torrent_id, "info", f"JDownloader completed {completed_count} files"),
            )
            await db.commit()

            torrent = dict(torrent)

        await self._delete_magnet_after_completion(torrent_id, torrent["alldebrid_id"])
        await self._mark_finished(torrent_id)
        if get_settings().discord_notify_finished:
            await self.notify().send(
                "Complete",
                f"**{torrent['name']}**\n{completed_count} files finished in JDownloader",
                0x22c55e,
            )

    async def _download_direct(self, url: str, dest: Path, filename: str) -> str:
        import aiohttp, aiofiles
        local = dest / safe_name(filename)
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=3600)) as r:
                r.raise_for_status()
                async with aiofiles.open(local, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 1024):
                        await f.write(chunk)
        return str(local)

    async def _log_file(self, torrent_id: int, filename: str, url: str,
                        local: Optional[str], status: str, reason: Optional[str],
                        size_bytes: int = 0):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO download_files
                   (torrent_id,filename,size_bytes,download_url,local_path,status,blocked,block_reason)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (torrent_id, filename, size_bytes, url, local, status,
                 1 if status == "blocked" else 0, reason),
            )
            await db.commit()

    # ── Import ────────────────────────────────────────────────────────────────

    async def import_existing_magnets(self) -> List[dict]:
        if self.is_paused():
            return []
        try:
            all_magnets = await self.ad().get_magnet_status()
        except Exception as e:
            err = str(e)
            if any(kw in err for kw in ("DISCONTINUED", "discontinued", "deprecated", "migrate")):
                raise Exception(
                    "AllDebrid has disabled 'list all magnets' for your account. "
                    "Add magnets manually via the UI or watch folder."
                )
            raise

        if not all_magnets:
            return []

        results = []
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            for m in all_magnets:
                ad_id = str(m.get("id", ""))
                hash_ = m.get("hash", ad_id).lower()
                name  = m.get("filename") or m.get("name") or ""
                code  = m.get("statusCode", 0)
                status = "ready" if code == READY_CODE else (
                    "error" if code in ERROR_CODES else "processing"
                )
                cur = await db.execute("SELECT id, status FROM torrents WHERE hash=?", (hash_,))
                existing = await cur.fetchone()
                should_queue = True
                if existing:
                    torrent_id = existing["id"]
                    existing_status = existing["status"]
                    if not _terminal_torrent_status(existing_status):
                        await db.execute(
                            "UPDATE torrents SET name=?, alldebrid_id=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (name, ad_id, status if existing_status in ("pending", "uploading", "processing", "ready") else existing_status, torrent_id),
                        )
                    else:
                        should_queue = False
                else:
                    cur = await db.execute(
                        """INSERT INTO torrents (hash,name,alldebrid_id,status,source)
                           VALUES (?,?,?,?,?)""",
                        (hash_, name, ad_id, status, "alldebrid_existing"),
                    )
                    torrent_id = cur.lastrowid
                results.append({
                    "hash": hash_,
                    "name": name,
                    "id": ad_id,
                    "status": status,
                    "torrent_id": torrent_id,
                    "should_queue": should_queue,
                })
            await db.commit()

        for item in results:
            if item["status"] == "ready" and item["should_queue"]:
                asyncio.create_task(self._start_download(item["torrent_id"], item["id"], item["name"]))
        return results

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete_torrent(self, torrent_id: int, delete_from_ad: bool = True):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError("Torrent not found")
            if delete_from_ad and row["alldebrid_id"] and row["status"] != "completed":
                await self.ad().delete_magnet(row["alldebrid_id"])
            await db.execute(
                "UPDATE torrents SET status='deleted',"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_id,)
            )
            await db.commit()

    # ── Tests ─────────────────────────────────────────────────────────────────

    async def test_jdownloader(self) -> dict:
        cfg = get_settings()
        if not cfg.jdownloader_email:
            raise Exception("MyJDownloader email not configured")
        return await MyJDownloaderClient(
            cfg.jdownloader_email,
            cfg.jdownloader_password,
            cfg.jdownloader_device_name,
        ).check()


manager = TorrentManager()
