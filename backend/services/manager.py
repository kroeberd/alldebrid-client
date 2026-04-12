import asyncio
import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional, List, Tuple
import aiohttp
import aiofiles
import aiosqlite

from core.config import get_settings, AppSettings
from services.alldebrid import AllDebridService
from services.notifications import NotificationService
from db.database import DB_PATH

logger = logging.getLogger("alldebrid.manager")


def extract_hash_from_magnet(magnet: str) -> Optional[str]:
    match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet)
    if match:
        h = match.group(1)
        if len(h) == 32:
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
        self._ad_service: Optional[AllDebridService] = None
        self._download_semaphore: Optional[asyncio.Semaphore] = None
        self._active_downloads: set = set()

    def reset_services(self):
        """Called after settings change — force re-init with new credentials."""
        if self._ad_service:
            asyncio.create_task(self._ad_service.close())
        self._ad_service = None
        self._download_semaphore = None

    def _get_ad_service(self) -> AllDebridService:
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

    async def scan_watch_folder(self):
        cfg = get_settings()
        watch = Path(cfg.watch_folder)
        processed = Path(cfg.processed_folder)
        watch.mkdir(parents=True, exist_ok=True)
        processed.mkdir(parents=True, exist_ok=True)

        for f in watch.iterdir():
            if f.suffix.lower() == ".torrent":
                await self._handle_torrent_file(f, processed)
            elif f.suffix.lower() in (".magnet", ".txt"):
                await self._handle_magnet_file(f, processed)

    async def _handle_torrent_file(self, path: Path, processed: Path):
        try:
            import bencodepy
            with open(path, "rb") as f:
                data = bencodepy.decode(f.read())
            info = data[b"info"]
            h = hashlib.sha1(bencodepy.encode(info)).hexdigest()
            name = info.get(b"name", b"").decode("utf-8", errors="ignore")
            magnet = f"magnet:?xt=urn:btih:{h}&dn={name}"
            await self._add_magnet(magnet, h, source="watch_torrent")
            shutil.move(str(path), str(processed / path.name))
        except Exception as e:
            logger.error(f"Failed to process torrent file {path}: {e}")

    async def _handle_magnet_file(self, path: Path, processed: Path):
        try:
            async with aiofiles.open(path, "r") as f:
                content = await f.read()
            magnets = [l.strip() for l in content.splitlines() if l.strip().startswith("magnet:")]
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
            raise ValueError("Invalid magnet link — no btih hash found")
        return await self._add_magnet(magnet, h, source=source)

    async def _add_magnet(self, magnet: str, torrent_hash: str, source: str = "unknown") -> dict:
        ad = self._get_ad_service()
        cfg = get_settings()

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM torrents WHERE hash=?", (torrent_hash,))
            existing = await cur.fetchone()
            if existing and existing["status"] in ("completed", "downloading", "uploading", "ready"):
                logger.info(f"Torrent {torrent_hash[:12]} already known ({existing['status']}), skipping")
                return dict(existing)

        try:
            result = await ad.upload_magnet(magnet)
            ad_id = str(result.get("id", ""))
            name = result.get("name", torrent_hash[:16])
            logger.info(f"Uploaded to AllDebrid: {name} (id={ad_id})")
        except Exception as e:
            logger.error(f"AllDebrid upload failed: {e}")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO torrents (hash,magnet,status,error_message,source) VALUES (?,?,?,?,?)",
                    (torrent_hash, magnet, "error", str(e), source),
                )
                await db.commit()
            raise

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """INSERT INTO torrents (hash,magnet,name,alldebrid_id,status,source)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(hash) DO UPDATE SET
                   alldebrid_id=excluded.alldebrid_id, name=excluded.name,
                   status='uploading', updated_at=CURRENT_TIMESTAMP""",
                (torrent_hash, magnet, name, ad_id, "uploading", source),
            )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) SELECT id,'info',? FROM torrents WHERE hash=?",
                (f"Added to AllDebrid (id={ad_id})", torrent_hash),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM torrents WHERE hash=?", (torrent_hash,))
            row = await cur.fetchone()

        if cfg.discord_notify_added:
            await self._notify().send("📥 Torrent Added", f"**{name}**\nAdded to AllDebrid queue", 0x3498db)

        return dict(row) if row else {}

    async def sync_alldebrid_status(self):
        cfg = get_settings()
        if not cfg.alldebrid_api_key:
            return
        ad = self._get_ad_service()
        try:
            magnets = await ad.get_all_magnets()
        except Exception as e:
            logger.error(f"Failed to fetch AllDebrid status: {e}")
            return

        status_map = {0:"processing",1:"processing",2:"processing",3:"downloading",
                      4:"processing",5:"ready",6:"error",7:"error",
                      8:"ready",9:"ready",10:"ready",11:"ready"}

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            for m in magnets:
                ad_id = str(m.get("id", ""))
                new_status = status_map.get(m.get("statusCode", 0), "processing")
                size = m.get("size", 0) or 0
                downloaded = m.get("downloaded", 0) or 0
                progress = (downloaded / size * 100) if size > 0 else 0

                cur = await db.execute("SELECT * FROM torrents WHERE alldebrid_id=?", (ad_id,))
                row = await cur.fetchone()
                if not row:
                    continue

                old_status = row["status"]
                if old_status in ("completed", "deleted"):
                    continue

                await db.execute(
                    "UPDATE torrents SET status=?,progress=?,size_bytes=?,updated_at=CURRENT_TIMESTAMP WHERE alldebrid_id=?",
                    (new_status, progress, size, ad_id),
                )
                if new_status == "ready" and old_status != "ready":
                    await db.execute(
                        "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                        (row["id"], "info", "Ready for download"),
                    )
                    asyncio.create_task(self._start_download(row["id"], ad_id, m))
            await db.commit()

    async def _start_download(self, torrent_db_id: int, ad_id: str, magnet_data: dict):
        if torrent_db_id in self._active_downloads:
            return
        self._active_downloads.add(torrent_db_id)
        try:
            async with self._get_semaphore():
                await self._download_torrent(torrent_db_id, ad_id, magnet_data)
        finally:
            self._active_downloads.discard(torrent_db_id)

    async def _download_torrent(self, torrent_db_id: int, ad_id: str, magnet_data: dict):
        cfg = get_settings()
        ad = self._get_ad_service()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE torrents SET status='downloading',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_db_id,),
            )
            await db.commit()

        links = magnet_data.get("links", [])
        if not links:
            try:
                status_data = await ad.get_magnet_status(ad_id)
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
                unlocked = await ad.unlock_link(link)
                dl_link = unlocked.get("link", "")
                filename = unlocked.get("filename", link.split("/")[-1])

                blocked, reason = is_file_blocked(filename, cfg)
                if blocked:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "INSERT INTO download_files (torrent_id,filename,download_url,status,blocked,block_reason) VALUES (?,?,?,?,1,?)",
                            (torrent_db_id, filename, dl_link, "blocked", reason),
                        )
                        await db.commit()
                    continue

                if cfg.ariang_enabled and cfg.ariang_url:
                    await self._send_to_ariang(dl_link, str(dest_folder))
                elif cfg.jdownloader_enabled and cfg.jdownloader_url:
                    await self._send_to_jdownloader(dl_link, str(dest_folder), filename)
                else:
                    await self._download_file(dl_link, dest_folder, filename)

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO download_files (torrent_id,filename,download_url,local_path,status) VALUES (?,?,?,?,?)",
                        (torrent_db_id, filename, dl_link, str(dest_folder / filename), "completed"),
                    )
                    await db.commit()
            except Exception as e:
                logger.error(f"Download error for {link}: {e}")
                all_ok = False

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT name FROM torrents WHERE id=?", (torrent_db_id,))
            row = await cur.fetchone()
            torrent_name = row["name"] if row else name
            final_status = "completed" if all_ok else "partial"
            await db.execute(
                "UPDATE torrents SET status=?,completed_at=CURRENT_TIMESTAMP,local_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (final_status, str(dest_folder), torrent_db_id),
            )
            await db.execute(
                "INSERT INTO events (torrent_id,level,message) VALUES (?,?,?)",
                (torrent_db_id, "info" if all_ok else "warn", f"Download {final_status}"),
            )
            await db.commit()

        if all_ok:
            await ad.delete_magnet(ad_id)
            if cfg.discord_notify_finished:
                await self._notify().send("✅ Download Complete", f"**{torrent_name}**\nSaved to: `{dest_folder}`", 0x2ecc71)
        elif cfg.discord_notify_error:
            await self._notify().send("⚠️ Download Partial", f"**{torrent_name}**\nSome files failed", 0xe67e22)

    async def _download_file(self, url: str, dest: Path, filename: str) -> str:
        local_path = dest / filename
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                async with aiofiles.open(local_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        await f.write(chunk)
        return str(local_path)

    async def _send_to_ariang(self, url: str, dest: str):
        cfg = get_settings()
        rpc_url = cfg.ariang_url
        token = ""
        if "@" in rpc_url:
            token, rpc_url = rpc_url.split("@", 1)
        payload = {"jsonrpc":"2.0","method":"aria2.addUri","id":"adc",
                   "params":[f"token:{token}",[url],{"dir":dest}]}
        async with aiohttp.ClientSession() as s:
            async with s.post(rpc_url, json=payload) as resp:
                return await resp.json()

    async def _send_to_jdownloader(self, url: str, dest: str, filename: str):
        cfg = get_settings()
        jd_url = cfg.jdownloader_url.rstrip("/")
        auth = aiohttp.BasicAuth(cfg.jdownloader_user, cfg.jdownloader_password) if cfg.jdownloader_user else None
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{jd_url}/flash/add",
                              data={"links": url, "packageName": filename,
                                    "destinationFolder": dest, "autoStart": True},
                              auth=auth) as resp:
                return resp.status

    async def import_existing_magnets(self) -> List[dict]:
        ad = self._get_ad_service()
        magnets = await ad.get_all_magnets()
        results = []
        async with aiosqlite.connect(DB_PATH) as db:
            for m in magnets:
                ad_id = str(m.get("id", ""))
                hash_ = m.get("hash", "").lower()
                name = m.get("name", "")
                if not hash_:
                    continue
                await db.execute(
                    "INSERT OR IGNORE INTO torrents (hash,name,alldebrid_id,status,source) VALUES (?,?,?,?,?)",
                    (hash_, name, ad_id, "imported", "alldebrid_existing"),
                )
                results.append({"hash": hash_, "name": name, "id": ad_id})
            await db.commit()
        return results

    async def delete_torrent(self, torrent_id: int, delete_from_ad: bool = True):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM torrents WHERE id=?", (torrent_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError("Torrent not found")
            if delete_from_ad and row["alldebrid_id"]:
                await self._get_ad_service().delete_magnet(row["alldebrid_id"])
            await db.execute(
                "UPDATE torrents SET status='deleted',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (torrent_id,),
            )
            await db.commit()


manager = TorrentManager()
