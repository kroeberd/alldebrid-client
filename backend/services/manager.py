import asyncio
import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Optional, List, Tuple, Set
import aiofiles
import aiosqlite

from core.config import get_settings, AppSettings
from services.alldebrid import AllDebridService
from services.notifications import NotificationService
from db.database import DB_PATH

logger = logging.getLogger("alldebrid.manager")


def extract_hash_from_magnet(magnet: str) -> Optional[str]:
    match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet, re.IGNORECASE)
    if match:
        h = match.group(1)
        if len(h) == 32:
            import base64
            try:
                h = base64.b32decode(h.upper()).hex()
            except Exception:
                return None
        return h.lower()
    return None


def is_file_blocked(filename: str, cfg: AppSettings) -> Tuple[bool, str]:
    ext = Path(filename).suffix.lower()
    if ext in [e.lower() for e in cfg.blocked_extensions]:
        return True, f"Blocked extension: {ext}"
    name_lower = filename.lower()
    for kw in cfg.blocked_keywords:
        if kw.lower() in name_lower:
            return True, f"Blocked keyword: {kw}"
    return False, ""


def _parse_torrent_sync(file_bytes: bytes) -> Tuple[str, str]:
    """Run in executor — returns (hash, name). Avoids async/thread conflicts."""
    import bencodepy
    data = bencodepy.decode(file_bytes)
    info = data[b"info"]
    h = hashlib.sha1(bencodepy.encode(info)).hexdigest()
    name = info.get(b"name", b"").decode("utf-8", errors="ignore")
    return h, name


class TorrentManager:
    def __init__(self):
        self._ad_service: Optional[AllDebridService] = None
        self._download_semaphore: Optional[asyncio.Semaphore] = None
        self._active_downloads: Set[int] = set()
        # Track files currently being processed to avoid retry spam
        self._processing_files: Set[str] = set()
        self._failed_files: Set[str] = set()  # permanently failed this session

    def reset_services(self):
        if self._ad_service:
            asyncio.create_task(self._ad_service.close())
        self._ad_service = None
        self._download_semaphore = None

    def _get_ad(self) -> AllDebridService:
        cfg = get_settings()
        if self._ad_service is None:
            self._ad_service = AllDebridService(cfg.alldebrid_api_key, cfg.alldebrid_agent)
        return self._ad_service

    def _get_semaphore(self) -> asyncio.Semaphore:
        cfg = get_settings()
        if self._download_semaphore is None:
            self._download_semaphore = asyncio.Semaphore(cfg.max_concurrent_downloads)
        return self._download_semaphore

    def _notify(self) -> NotificationService:
        return NotificationService(get_settings().discord_webhook_url)

    # ─── Watch Folder ────────────────────────────────────────────────────────

    async def scan_watch_folder(self):
        cfg = get_settings()
        watch = Path(cfg.watch_folder)
        processed = Path(cfg.processed_folder)
        watch.mkdir(parents=True, exist_ok=True)
        processed.mkdir(parents=True, exist_ok=True)

        for f in list(watch.iterdir()):
            fkey = str(f)
            if fkey in self._processing_files or fkey in self._failed_files:
                continue
            self._processing_files.add(fkey)
            try:
                if f.suffix.lower() == ".torrent":
                    await self._handle_torrent_file(f, processed)
                elif f.suffix.lower() in (".magnet", ".txt"):
                    await self._handle_magnet_file(f, processed)
            except Exception as e:
                logger.error(f"Watch folder error for {f.name}: {e}")
                self._failed_files.add(fkey)  # don't retry this session
            finally:
                self._processing_files.discard(fkey)

    async def _handle_torrent_file(self, path: Path, processed: Path):
        """Read torrent, upload file directly to AllDebrid (no local bencode hash needed)."""
        cfg = get_settings()
        loop = asyncio.get_event_loop()

        # Read file async
        async with aiofiles.open(path, "rb") as f:
            file_bytes = await f.read()

        if not file_bytes:
            logger.warning(f"Empty torrent file: {path.name}")
            self._failed_files.add(str(path))
            return

        # Parse in executor to avoid blocking event loop
        try:
            torrent_hash, name = await loop.run_in_executor(
                None, _parse_torrent_sync, file_bytes
            )
        except Exception as e:
            logger.error(f"Failed to parse torrent {path.name}: {e}")
            # Still try uploading the file directly
            torrent_hash = None
            name = path.stem

        if not cfg.alldebrid_api_key:
            logger.warning("No API key configured, skipping torrent upload")
            return

        ad = self._get_ad()
        try:
            # Upload .torrent file directly — more reliable than magnet
            result = await ad.upload_torrent_file(file_bytes, path.name)
            ad_id = str(result.get("id", ""))
            name = result.get("name", name)
            if not torrent_hash:
                torrent_hash = result.get("hash", ad_id)

            logger.info(f"Uploaded torrent file to AllDebrid: {name} (id={ad_id})")
            await self._upsert_torrent(torrent_hash or ad_id, None, name, ad_id, "watch_torrent")
            shutil.move(str(path), str(processed / path.name))
            self._failed_files.discard(str(path))
        except Exception as e:
            logger.error(f"AllDebrid upload failed for {path.name}: {e}")
            raise

    async def _handle_magnet_file(self, path: Path, processed: Path):
        async with aiofiles.open(path, "r", errors="ignore") as f:
            content = await f.read()
        magnets = [l.strip() for l in content.splitlines() if l.strip().startswith("magnet:")]
        if not magnets:
            # Not a magnet file, ignore
            self._failed_files.add(str(path))
            return
        for magnet in magnets:
            h = extract_hash_from_magnet(magnet)
            if h:
                await self._add_magnet(magnet, h, source="watch_file")
        shutil.move(str(path), str(processed / path.name))
        self._failed_files.discard(str(path))

    # ─── Magnet Management ────────────────────────────────────────────────────

    async def add_magnet_direct(self, magnet: str, source: str = "api") -> dict:
        h = extract_hash_from_magnet(magnet)
        if not h:
            raise ValueError("Invalid magnet link — no btih hash found")
        return await self._add_magnet(magnet, h, source=source)

    async def _add_magnet(self, magnet: str, torrent_hash: str, source: str = "unknown") -> dict:
        cfg = get_settings()
        ad = self._get_ad()

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM torrents WHERE hash=?", (torrent_hash,))
            existing = await cur.fetchone()
            if existing and existing["status"] in ("completed", "downloading", "ready", "uploading"):
                logger.info(f"Torrent {torrent_hash[:12]} already known ({existing['status']})")
                return dict(existing)

        try:
            result = await ad.upload_magnet(magnet)
            ad_id = str(result.get("id", ""))
            name = result.get("name", torrent_hash[:16])
            logger.info(f"Uploaded magnet to AllDebrid: {name} (id={ad_id})")
        except Exception as e:
            logger.error(f"AllDebrid magnet upload failed: {e}")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO torrents (hash,magnet,status,error_message,source) VALUES (?,?,?,?,?)",
                    (torrent_hash, magnet, "error", str(e), source),
                )
                await db.commit()
            raise

        row = await self._upsert_torrent(torrent_hash, magnet, name, ad_id, source)
        if cfg.discord_notify_added:
            await self._notify().send("📥 Torrent Added", f"**{name}**\nQueued on AllDebrid", 0x3498db)
        return row

    async def _upsert_torrent(self, hash_: str, magnet: Optional[str], name: str, ad_id: str, source: str) -> dict:
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
                "INSERT INTO events (torrent_id,level,message) SELECT id,'info',? FROM torrents WHERE hash=?",
                (f"Added to AllDebrid (id={ad_id})", hash_),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM torrents WHERE hash=?", (hash_,))
            row = await cur.fetchone()
            return dict(row) if row else {}

    # ─── Status Sync ─────────────────────────────────────────────────────────

    async def sync_alldebrid_status(self):
        cfg = get_settings()
        if not cfg.alldebrid_api_key:
            return
        ad = self._get_ad()

        try:
            magnets = await ad.get_all_magnets()
        except Exception as e:
            logger.error(f"Failed to fetch AllDebrid status: {e}")
            return

        # Status code mapping from AllDebrid docs
        status_map = {
            0: "processing",   # In Queue
            1: "processing",   # Downloading
            2: "processing",   # Compressing / Moving
            3: "downloading",  # Downloading
            4: "processing",   # Uploading
            5: "ready",        # Ready
            6: "error",        # Error
            7: "error",        # Virus
            8: "ready",        # 
            9: "ready",        # 
            10: "ready",       # 
            11: "ready",       # 
        }

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            for m in magnets:
                ad_id = str(m.get("id", ""))
                status_code = m.get("statusCode", 0)
                new_status = status_map.get(status_code, "processing")
                size = m.get("size", 0) or 0
                downloaded = m.get("downloaded", 0) or 0
                progress = (downloaded / size * 100) if size > 0 else 0

                cur = await db.execute(
                    "SELECT * FROM torrents WHERE alldebrid_id=?", (ad_id,)
                )
                row = await cur.fetchone()
                if not row:
                    continue
                if row["status"] in ("completed", "deleted"):
                    continue

                old_status = row["status"]
                await db.execute(
                    """UPDATE torrents SET status=?,progress=?,size_bytes=?,
                       updated_at=CURRENT_TIMESTAMP WHERE alldebrid_id=?""",
                    (new_status, progress, size, ad_id),
                )

                if new_status == "ready" and old_status != "ready":
                    await db.execute(
                        "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                        (row["id"], "info", "Ready — starting download"),
                    )
                    asyncio.create_task(
                        self._start_download(row["id"], ad_id, m)
                    )
                elif new_status == "error" and old_status != "error":
                    err_msg = m.get("error", "Unknown AllDebrid error")
                    await db.execute(
                        "UPDATE torrents SET error_message=? WHERE alldebrid_id=?",
                        (str(err_msg), ad_id)
                    )
                    if cfg.discord_notify_error:
                        name = row["name"] or ad_id
                        await self._notify().send(
                            "❌ AllDebrid Error", f"**{name}**\n{err_msg}", 0xef476f
                        )

            await db.commit()

    # ─── Download ─────────────────────────────────────────────────────────────

    async def _start_download(self, torrent_db_id: int, ad_id: str, magnet_data: dict):
        if torrent_db_id in self._active_downloads:
            return
        self._active_downloads.add(torrent_db_id)
        try:
            async with self._get_semaphore():
                await self._download_torrent(torrent_db_id, ad_id, magnet_data)
        except Exception as e:
            logger.error(f"Download task failed for {torrent_db_id}: {e}")
        finally:
            self._active_downloads.discard(torrent_db_id)

    async def _download_torrent(self, torrent_db_id: int, ad_id: str, magnet_data: dict):
        cfg = get_settings()
        ad = self._get_ad()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE torrents SET status='downloading',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_db_id,),
            )
            await db.commit()

        # Get links from status data or re-fetch
        links = magnet_data.get("links", [])
        name = magnet_data.get("name", f"torrent_{torrent_db_id}")

        if not links:
            try:
                status_data = await ad.get_magnet_status(ad_id)
                magnet_info = status_data.get("magnets", {})
                if isinstance(magnet_info, list):
                    magnet_info = magnet_info[0] if magnet_info else {}
                links = magnet_info.get("links", [])
                name = magnet_info.get("name", name)
            except Exception as e:
                logger.error(f"Could not get links for {ad_id}: {e}")

        dest_folder = Path(cfg.download_folder) / _safe_dirname(name)
        dest_folder.mkdir(parents=True, exist_ok=True)

        all_ok = True
        downloaded_files = []

        for link_info in links:
            link = link_info if isinstance(link_info, str) else link_info.get("link", "")
            if not link:
                continue
            try:
                unlocked = await ad.unlock_link(link)
                dl_link = unlocked.get("link", "")
                filename = unlocked.get("filename", link.split("/")[-1])

                blocked, reason = is_file_blocked(filename, cfg)
                if blocked:
                    logger.info(f"Blocking file: {filename} ({reason})")
                    await self._save_file_record(torrent_db_id, filename, dl_link, None, "blocked", reason)
                    continue

                # Route to integration or direct download
                local_path = await self._route_download(dl_link, str(dest_folder), filename)
                await self._save_file_record(torrent_db_id, filename, dl_link, local_path, "completed", None)
                downloaded_files.append(filename)

            except Exception as e:
                logger.error(f"File download error ({link}): {e}")
                await self._save_file_record(torrent_db_id, link.split("/")[-1], link, None, "error", str(e))
                all_ok = False

        final_status = "completed" if all_ok else "partial"
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT name FROM torrents WHERE id=?", (torrent_db_id,))
            row = await cur.fetchone()
            torrent_name = row["name"] if row else name
            await db.execute(
                """UPDATE torrents SET status=?,completed_at=CURRENT_TIMESTAMP,
                   local_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (final_status, str(dest_folder), torrent_db_id),
            )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                (torrent_db_id, "info" if all_ok else "warn", f"Download {final_status}: {len(downloaded_files)} files"),
            )
            await db.commit()

        # Only delete from AllDebrid after successful download
        if all_ok:
            deleted = await ad.delete_magnet(ad_id)
            if deleted:
                logger.info(f"Deleted magnet {ad_id} from AllDebrid")
            if cfg.discord_notify_finished:
                await self._notify().send(
                    "✅ Download Complete",
                    f"**{torrent_name}**\n{len(downloaded_files)} files → `{dest_folder}`",
                    0x2ecc71
                )
        else:
            if cfg.discord_notify_error:
                await self._notify().send(
                    "⚠️ Partial Download",
                    f"**{torrent_name}**\nSome files failed — magnet kept on AllDebrid",
                    0xe67e22
                )

    async def _route_download(self, url: str, dest: str, filename: str) -> Optional[str]:
        """Route to aria2, JDownloader, or direct download."""
        cfg = get_settings()
        if cfg.ariang_enabled and cfg.ariang_url:
            await self._send_to_aria2(url, dest)
            return None  # aria2 handles path
        elif cfg.jdownloader_enabled and cfg.jdownloader_url:
            await self._send_to_jdownloader(url, dest, filename)
            return None  # JD handles path
        else:
            return await self._download_file_direct(url, Path(dest), filename)

    async def _download_file_direct(self, url: str, dest: Path, filename: str) -> str:
        import aiohttp
        local_path = dest / _safe_filename(filename)
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                async with aiofiles.open(local_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        await f.write(chunk)
        return str(local_path)

    async def _send_to_aria2(self, url: str, dest: str):
        import aiohttp
        cfg = get_settings()
        rpc_url = cfg.ariang_url
        secret = ""
        # Support format: http://token:SECRET@host:port/jsonrpc
        if "@" in rpc_url:
            creds, rpc_url = rpc_url.rsplit("@", 1)
            secret = creds.split(":", 1)[-1] if ":" in creds else creds
            if not rpc_url.startswith("http"):
                rpc_url = "http://" + rpc_url

        payload = {
            "jsonrpc": "2.0",
            "method": "aria2.addUri",
            "id": "adc",
            "params": [f"token:{secret}", [url], {"dir": dest}],
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                result = await resp.json()
                if "error" in result:
                    raise Exception(f"aria2 error: {result['error']}")
                return result.get("result", "")

    async def _send_to_jdownloader(self, url: str, dest: str, filename: str):
        """Send via JDownloader local API (direct/deprecated API on port 9696)"""
        import aiohttp
        cfg = get_settings()
        jd_url = cfg.jdownloader_url.rstrip("/")
        auth = None
        if cfg.jdownloader_user:
            auth = aiohttp.BasicAuth(cfg.jdownloader_user, cfg.jdownloader_password)

        payload = {
            "links": url,
            "packageName": filename,
            "destinationFolder": dest,
            "autoStart": cfg.jdownloader_autostart,
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{jd_url}/flash/add", data=payload, auth=auth,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status not in (200, 201, 204):
                    raise Exception(f"JDownloader returned HTTP {resp.status}")

    async def _save_file_record(self, torrent_id: int, filename: str, url: str,
                                 local_path: Optional[str], status: str, block_reason: Optional[str]):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO download_files
                   (torrent_id,filename,download_url,local_path,status,blocked,block_reason)
                   VALUES (?,?,?,?,?,?,?)""",
                (torrent_id, filename, url, local_path, status,
                 1 if status == "blocked" else 0, block_reason),
            )
            await db.commit()

    # ─── Import & Management ─────────────────────────────────────────────────

    async def import_existing_magnets(self) -> List[dict]:
        ad = self._get_ad()
        magnets = await ad.get_all_magnets()
        results = []
        async with aiosqlite.connect(DB_PATH) as db:
            for m in magnets:
                ad_id = str(m.get("id", ""))
                hash_ = m.get("hash", ad_id).lower()
                name = m.get("name", "")
                status_code = m.get("statusCode", 0)
                status = "ready" if status_code == 5 else "processing"
                await db.execute(
                    """INSERT OR IGNORE INTO torrents (hash,name,alldebrid_id,status,source)
                       VALUES (?,?,?,?,?)""",
                    (hash_, name, ad_id, status, "alldebrid_existing"),
                )
                results.append({"hash": hash_, "name": name, "id": ad_id, "status": status})
            await db.commit()
        return results

    async def delete_torrent(self, torrent_id: int, delete_from_ad: bool = True):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError("Torrent not found")
            if delete_from_ad and row["alldebrid_id"] and row["status"] != "completed":
                await self._get_ad().delete_magnet(row["alldebrid_id"])
            await db.execute(
                "UPDATE torrents SET status='deleted',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_id,),
            )
            await db.commit()

    async def test_aria2(self) -> dict:
        import aiohttp
        cfg = get_settings()
        rpc_url = cfg.ariang_url
        secret = ""
        if "@" in rpc_url:
            creds, rpc_url = rpc_url.rsplit("@", 1)
            secret = creds.split(":", 1)[-1] if ":" in creds else creds
            if not rpc_url.startswith("http"):
                rpc_url = "http://" + rpc_url
        payload = {"jsonrpc": "2.0", "method": "aria2.getVersion", "id": "adc", "params": [f"token:{secret}"]}
        async with aiohttp.ClientSession() as s:
            async with s.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                result = await resp.json()
                if "error" in result:
                    raise Exception(result["error"]["message"])
                return result.get("result", {})

    async def test_jdownloader(self) -> dict:
        import aiohttp
        cfg = get_settings()
        jd_url = cfg.jdownloader_url.rstrip("/")
        auth = aiohttp.BasicAuth(cfg.jdownloader_user, cfg.jdownloader_password) if cfg.jdownloader_user else None
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{jd_url}/jdcheckjson", auth=auth, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return {"status": "ok", "http": resp.status}
                raise Exception(f"JDownloader returned HTTP {resp.status}")


def _safe_dirname(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)[:200] or "download"

def _safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)[:255] or "file"


manager = TorrentManager()
