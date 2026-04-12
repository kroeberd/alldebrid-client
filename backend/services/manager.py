import asyncio
import hashlib
import logging
import os
import re
import shutil
import aiohttp
import aiofiles
from pathlib import Path
from typing import Optional, List, Tuple
import aiosqlite

from core.config import settings, AppSettings
from services.alldebrid import AllDebridService
from services.notifications import NotificationService
from db.database import DB_PATH

logger = logging.getLogger("alldebrid.manager")


def extract_hash_from_magnet(magnet: str) -> Optional[str]:
    match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet)
    if match:
        h = match.group(1)
        if len(h) == 32:
            # base32 -> hex
            import base64
            h = base64.b32decode(h.upper()).hex()
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


class TorrentManager:
    def __init__(self):
        self.ad_service: Optional[AllDebridService] = None
        self.notify: Optional[NotificationService] = None
        self._download_semaphore: Optional[asyncio.Semaphore] = None
        self._active_downloads = set()

    def _init_services(self):
        cfg = settings
        self.ad_service = AllDebridService(cfg.alldebrid_api_key, cfg.alldebrid_agent)
        self.notify = NotificationService(cfg.discord_webhook_url)
        self._download_semaphore = asyncio.Semaphore(cfg.max_concurrent_downloads)

    async def _get_db(self):
        return await aiosqlite.connect(DB_PATH)

    async def scan_watch_folder(self):
        cfg = settings
        watch = Path(cfg.watch_folder)
        processed = Path(cfg.processed_folder)
        watch.mkdir(parents=True, exist_ok=True)
        processed.mkdir(parents=True, exist_ok=True)

        for f in watch.iterdir():
            if f.suffix.lower() == ".torrent":
                await self._handle_torrent_file(f, processed)
            elif f.suffix.lower() == ".magnet" or f.name.endswith(".txt"):
                await self._handle_magnet_file(f, processed)

    async def _handle_torrent_file(self, path: Path, processed: Path):
        # Read torrent and extract info hash
        try:
            import bencodepy
            with open(path, "rb") as f:
                data = bencodepy.decode(f.read())
            info = data[b"info"]
            import hashlib, bencodepy
            h = hashlib.sha1(bencodepy.encode(info)).hexdigest()
            magnet = f"magnet:?xt=urn:btih:{h}&dn={info.get(b'name', b'').decode('utf-8', errors='ignore')}"
            await self._add_magnet(magnet, h, source="watch_torrent")
            shutil.move(str(path), str(processed / path.name))
        except Exception as e:
            logger.error(f"Failed to process torrent file {path}: {e}")

    async def _handle_magnet_file(self, path: Path, processed: Path):
        try:
            async with aiofiles.open(path, "r") as f:
                content = await f.read()
            magnets = [line.strip() for line in content.splitlines()
                       if line.strip().startswith("magnet:")]
            for magnet in magnets:
                h = extract_hash_from_magnet(magnet)
                if h:
                    await self._add_magnet(magnet, h, source="watch_file")
            if magnets:
                shutil.move(str(path), str(processed / path.name))
        except Exception as e:
            logger.error(f"Failed to process magnet file {path}: {e}")

    async def add_magnet_direct(self, magnet: str, source: str = "api") -> dict:
        h = extract_hash_from_magnet(magnet)
        if not h:
            raise ValueError("Invalid magnet link")
        return await self._add_magnet(magnet, h, source=source)

    async def _add_magnet(self, magnet: str, torrent_hash: str, source: str = "unknown") -> dict:
        self._init_services()
        async with await self._get_db() as db:
            db.row_factory = aiosqlite.Row
            # Check if already known
            cur = await db.execute(
                "SELECT * FROM torrents WHERE hash = ?", (torrent_hash,)
            )
            existing = await cur.fetchone()
            if existing:
                status = existing["status"]
                if status in ("completed", "downloading", "uploading"):
                    logger.info(f"Torrent {torrent_hash} already known (status: {status}), skipping")
                    return dict(existing)

            # Upload to AllDebrid
            try:
                result = await self.ad_service.upload_magnet(magnet)
                ad_id = str(result.get("id", ""))
                name = result.get("name", torrent_hash[:16])
                ready = result.get("ready", False)
                logger.info(f"Uploaded magnet to AllDebrid: {name} (id={ad_id}, ready={ready})")
            except Exception as e:
                logger.error(f"AllDebrid upload failed: {e}")
                await db.execute(
                    """INSERT OR IGNORE INTO torrents (hash, magnet, status, error_message, source)
                       VALUES (?,?,?,?,?)""",
                    (torrent_hash, magnet, "error", str(e), source)
                )
                await db.commit()
                return {"status": "error", "error": str(e)}

            # Upsert in DB
            await db.execute(
                """INSERT INTO torrents (hash, magnet, name, alldebrid_id, status, source)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(hash) DO UPDATE SET
                   alldebrid_id=excluded.alldebrid_id,
                   name=excluded.name,
                   status='uploading',
                   updated_at=CURRENT_TIMESTAMP""",
                (torrent_hash, magnet, name, ad_id, "uploading", source)
            )
            await db.execute(
                """INSERT INTO events (torrent_id, level, message)
                   SELECT id, 'info', ? FROM torrents WHERE hash=?""",
                (f"Added to AllDebrid (id={ad_id})", torrent_hash)
            )
            await db.commit()

            cur = await db.execute("SELECT * FROM torrents WHERE hash=?", (torrent_hash,))
            row = await cur.fetchone()

            if self.notify and settings.discord_notify_added:
                await self.notify.send(
                    title="📥 Torrent Added",
                    description=f"**{name}**\nAdded to AllDebrid queue",
                    color=0x3498db
                )
            return dict(row) if row else {}

    async def sync_alldebrid_status(self):
        """Poll AllDebrid and update local DB status"""
        self._init_services()
        try:
            magnets = await self.ad_service.get_all_magnets()
        except Exception as e:
            logger.error(f"Failed to fetch AllDebrid status: {e}")
            return

        async with await self._get_db() as db:
            db.row_factory = aiosqlite.Row
            for m in magnets:
                ad_id = str(m.get("id", ""))
                status_code = m.get("statusCode", 0)
                status_map = {
                    0: "processing",
                    1: "processing",
                    2: "processing",
                    3: "downloading",
                    4: "processing",
                    5: "ready",
                    6: "error",
                    7: "error",
                    8: "ready",
                    9: "ready",
                    10: "ready",
                    11: "ready",
                }
                new_status = status_map.get(status_code, "processing")
                progress = m.get("downloaded", 0) / max(m.get("size", 1), 1) * 100

                cur = await db.execute(
                    "SELECT * FROM torrents WHERE alldebrid_id=?", (ad_id,)
                )
                row = await cur.fetchone()
                if not row:
                    continue

                old_status = row["status"]
                await db.execute(
                    """UPDATE torrents SET status=?, progress=?, size_bytes=?, updated_at=CURRENT_TIMESTAMP
                       WHERE alldebrid_id=?""",
                    (new_status, progress, m.get("size", 0), ad_id)
                )

                if new_status == "ready" and old_status != "ready":
                    await db.execute(
                        """INSERT INTO events (torrent_id, level, message) VALUES (?,?,?)""",
                        (row["id"], "info", "Ready for download on AllDebrid")
                    )
                    asyncio.create_task(self._start_download(row["id"], ad_id, m))

                await db.commit()

    async def _start_download(self, torrent_db_id: int, ad_id: str, magnet_data: dict):
        self._init_services()
        if torrent_db_id in self._active_downloads:
            return
        self._active_downloads.add(torrent_db_id)

        async with self._download_semaphore:
            try:
                await self._download_torrent(torrent_db_id, ad_id, magnet_data)
            finally:
                self._active_downloads.discard(torrent_db_id)

    async def _download_torrent(self, torrent_db_id: int, ad_id: str, magnet_data: dict):
        cfg = settings
        async with await self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE torrents SET status='downloading', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_db_id,)
            )
            await db.commit()

        links = magnet_data.get("links", [])
        if not links:
            # Try fetching status again
            try:
                status_data = await self.ad_service.get_magnet_status(ad_id)
                links = status_data.get("magnets", {}).get("links", [])
            except Exception as e:
                logger.error(f"Could not get links for {ad_id}: {e}")

        name = magnet_data.get("name", f"torrent_{torrent_db_id}")
        dest_folder = Path(cfg.download_folder) / name
        dest_folder.mkdir(parents=True, exist_ok=True)

        all_ok = True
        for link_info in links:
            link = link_info if isinstance(link_info, str) else link_info.get("link", "")
            if not link:
                continue
            try:
                unlocked = await self.ad_service.unlock_link(link)
                dl_link = unlocked.get("link", "")
                filename = unlocked.get("filename", link.split("/")[-1])

                blocked, reason = is_file_blocked(filename, cfg)
                if blocked:
                    logger.info(f"Blocking file {filename}: {reason}")
                    async with await self._get_db() as db:
                        await db.execute(
                            """INSERT INTO download_files (torrent_id,filename,download_url,status,blocked,block_reason)
                               VALUES (?,?,?,?,1,?)""",
                            (torrent_db_id, filename, dl_link, "blocked", reason)
                        )
                        await db.commit()
                    continue

                # Check ariang
                if cfg.ariang_enabled and cfg.ariang_url:
                    await self._send_to_ariang(dl_link, str(dest_folder))
                elif cfg.jdownloader_enabled and cfg.jdownloader_url:
                    await self._send_to_jdownloader(dl_link, str(dest_folder), filename)
                else:
                    local_path = await self._download_file(dl_link, dest_folder, filename)

                async with await self._get_db() as db:
                    await db.execute(
                        """INSERT INTO download_files (torrent_id,filename,download_url,local_path,status)
                           VALUES (?,?,?,?,?)""",
                        (torrent_db_id, filename, dl_link,
                         str(dest_folder / filename), "completed")
                    )
                    await db.commit()
            except Exception as e:
                logger.error(f"Download error for link {link}: {e}")
                all_ok = False

        async with await self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT name FROM torrents WHERE id=?", (torrent_db_id,))
            row = await cur.fetchone()
            torrent_name = row["name"] if row else name

            final_status = "completed" if all_ok else "partial"
            await db.execute(
                """UPDATE torrents SET status=?, completed_at=CURRENT_TIMESTAMP,
                   local_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (final_status, str(dest_folder), torrent_db_id)
            )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                (torrent_db_id, "info", f"Download {final_status}")
            )
            await db.commit()

        if all_ok:
            # Delete from AllDebrid
            await self.ad_service.delete_magnet(ad_id)
            logger.info(f"Deleted magnet {ad_id} from AllDebrid after successful download")
            if self.notify and cfg.discord_notify_finished:
                await self.notify.send(
                    title="✅ Download Complete",
                    description=f"**{torrent_name}**\nSaved to: `{dest_folder}`",
                    color=0x2ecc71
                )
        elif self.notify and cfg.discord_notify_error:
            await self.notify.send(
                title="⚠️ Download Partial",
                description=f"**{torrent_name}**\nSome files failed",
                color=0xe67e22
            )

    async def _download_file(self, url: str, dest: Path, filename: str) -> str:
        cfg = settings
        local_path = dest / filename
        chunk_size = 1024 * 1024  # 1MB
        connector = aiohttp.TCPConnector(limit=1)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                async with aiofiles.open(local_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        await f.write(chunk)
        return str(local_path)

    async def _send_to_ariang(self, url: str, dest: str):
        # aria2 JSON-RPC
        cfg = settings
        payload = {
            "jsonrpc": "2.0",
            "method": "aria2.addUri",
            "id": "adc",
            "params": [
                f"token:{cfg.ariang_url.split('@')[-1] if '@' in cfg.ariang_url else ''}",
                [url],
                {"dir": dest}
            ]
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(cfg.ariang_url.split('@')[0] if '@' in cfg.ariang_url else cfg.ariang_url,
                              json=payload) as resp:
                return await resp.json()

    async def _send_to_jdownloader(self, url: str, dest: str, filename: str):
        cfg = settings
        jd_url = cfg.jdownloader_url.rstrip("/")
        payload = {
            "links": url,
            "packageName": filename,
            "destinationFolder": dest,
            "autoStart": True,
        }
        auth = None
        if cfg.jdownloader_user:
            auth = aiohttp.BasicAuth(cfg.jdownloader_user, cfg.jdownloader_password)
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{jd_url}/flash/add", data=payload, auth=auth) as resp:
                return resp.status

    async def import_existing_magnets(self) -> List[dict]:
        """Import all magnets already on AllDebrid account"""
        self._init_services()
        magnets = await self.ad_service.get_all_magnets()
        results = []
        async with await self._get_db() as db:
            for m in magnets:
                ad_id = str(m.get("id", ""))
                hash_ = m.get("hash", "").lower()
                name = m.get("name", "")
                if not hash_:
                    continue
                await db.execute(
                    """INSERT OR IGNORE INTO torrents
                       (hash, name, alldebrid_id, status, source)
                       VALUES (?,?,?,?,?)""",
                    (hash_, name, ad_id, "imported", "alldebrid_existing")
                )
                results.append({"hash": hash_, "name": name, "id": ad_id})
            await db.commit()
        return results

    async def delete_torrent(self, torrent_id: int, delete_from_ad: bool = True):
        async with await self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError("Torrent not found")
            if delete_from_ad and row["alldebrid_id"]:
                self._init_services()
                await self.ad_service.delete_magnet(row["alldebrid_id"])
            await db.execute("UPDATE torrents SET status='deleted', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (torrent_id,))
            await db.commit()


manager = TorrentManager()
