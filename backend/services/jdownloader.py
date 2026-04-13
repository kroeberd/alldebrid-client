"""
MyJDownloader client — uses myjdapi library directly (same as Quasarr).
"""

import asyncio
import logging
from pathlib import Path
import time
from typing import Optional, Dict, List, Any

logger = logging.getLogger("alldebrid.jdownloader")


def _make_jd(email: str, password: str) -> Any:
    """Connect and return myjdapi instance (sync)."""
    import myjdapi
    jd = myjdapi.Myjdapi()
    jd.set_app_key("AllDebrid-Client")
    jd.connect(email, password)
    jd.update_devices()
    return jd


def _get_device(jd: Any, device_name: str) -> Any:
    """Get device object, with direct connections disabled to avoid local-network failures."""
    if device_name:
        device = jd.get_device(device_name=device_name)
    else:
        device = jd.get_device()  # first available
    # Disable direct connection attempts — we always go via MyJD cloud
    device.disable_direct_connection()
    return device


async def myjd_list_devices(email: str, password: str) -> List[Dict]:
    """Login and return device list for the UI picker."""
    loop = asyncio.get_event_loop()
    def _run():
        jd = _make_jd(email, password)
        return [{"name": d["name"], "id": d["id"]} for d in (jd.list_devices() or [])]
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        err = str(e)
        if "AUTH_FAILED" in err or "403" in err:
            raise Exception("Wrong email or password")
        raise Exception(f"MyJDownloader: {err}")


class MyJDownloaderClient:
    def __init__(self, email: str, password: str, device_name: str = ""):
        self._email       = email
        self._password    = password
        self._device_name = device_name.strip()

    def _check_sync(self) -> Dict:
        jd      = _make_jd(self._email, self._password)
        devices = jd.list_devices() or []
        if not devices:
            raise Exception(
                "Connected to MyJDownloader but no devices found. "
                "Open JDownloader and log in to MyJDownloader in Settings → MyJDownloader."
            )
        # Find target device
        target = None
        for d in devices:
            if not self._device_name or d["name"].lower() == self._device_name.lower():
                target = d
                break
        if not target and self._device_name:
            names = [d["name"] for d in devices]
            raise Exception(f"Device '{self._device_name}' not found. Available: {names}")
        target = target or devices[0]

        # Try to reach device via cloud (not direct connection)
        reachable = False
        try:
            device = _get_device(jd, target["name"])
            state  = device.downloadcontroller.get_current_state()
            reachable = state is not None
        except Exception as e:
            logger.debug(f"JD ping failed for {target['name']}: {e}")

        return {
            "status":      "ok",
            "device":      target["name"],
            "device_id":   target["id"],
            "reachable":   reachable,
            "all_devices": [d["name"] for d in devices],
        }

    async def check(self) -> Dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._check_sync)

    def _find_linkgrabber_entry(self, device: Any, url: str, filename: str) -> Optional[Dict]:
        query = [{
            "name": True,
            "url": True,
            "uuid": True,
            "packageUUID": True,
            "enabled": True,
            "maxResults": -1,
            "startAt": 0,
        }]
        target_name = Path(filename).name if filename else ""
        for _ in range(10):
            links = device.linkgrabber.query_links(query) or []
            for entry in links:
                entry_url = str(entry.get("url", "")).strip()
                entry_name = Path(str(entry.get("name", ""))).name
                if entry_url == url or (target_name and entry_name == target_name):
                    return entry
            time.sleep(0.5)
        return None

    def _find_linkgrabber_entries(self, device: Any, urls: List[str], filename: str = "") -> List[Dict]:
        query = [{
            "name": True,
            "url": True,
            "uuid": True,
            "packageUUID": True,
            "enabled": True,
            "maxResults": -1,
            "startAt": 0,
        }]
        wanted_urls = {str(url).strip() for url in urls}
        target_name = Path(filename).name if filename else ""
        matches: Dict[str, Dict] = {}
        for _ in range(12):
            links = device.linkgrabber.query_links(query) or []
            for entry in links:
                entry_url = str(entry.get("url", "")).strip()
                entry_name = Path(str(entry.get("name", ""))).name
                if entry_url in wanted_urls or (target_name and entry_name == target_name):
                    key = str(entry.get("uuid") or entry_url or entry_name)
                    matches[key] = entry
            if matches:
                break
            time.sleep(0.5)
        return list(matches.values())

    def _move_linkgrabber_entries(self, device: Any, entries: List[Dict]) -> bool:
        if not entries:
            return False
        link_ids = [entry["uuid"] for entry in entries if entry.get("uuid") is not None]
        package_ids = list({entry["packageUUID"] for entry in entries if entry.get("packageUUID") is not None})
        if not link_ids and not package_ids:
            return False
        device.linkgrabber.move_to_downloadlist(link_ids, package_ids)
        return True

    def _find_download_entry(self, device: Any, url: str, filename: str) -> Optional[Dict]:
        query = [{
            "bytesLoaded": True,
            "bytesTotal": True,
            "eta": True,
            "finished": True,
            "name": True,
            "running": True,
            "speed": True,
            "status": True,
            "url": True,
            "maxResults": -1,
            "startAt": 0,
        }]
        target_name = Path(filename).name if filename else ""
        links = device.downloads.query_links(query) or []
        for entry in links:
            entry_url = str(entry.get("url", "")).strip()
            entry_name = Path(str(entry.get("name", ""))).name
            if entry_url == url or (target_name and entry_name == target_name):
                return entry
        return None

    def _add_links_sync(self, urls: List[str], dest_dir: str,
                        package_name: str, autostart: bool, auto_extract: bool):
        jd     = _make_jd(self._email, self._password)
        device = _get_device(jd, self._device_name)
        package_name = Path(package_name).name if package_name else None
        params = [{
            "links":                    "\n".join(urls),
            "destinationFolder":        dest_dir,
            "packageName":              package_name,
            "autostart":                autostart,
            "overwritePackagizerRules": False,
        }]
        device.linkgrabber.add_links(params)

        entries = self._find_linkgrabber_entries(device, urls, package_name or "")
        if not entries:
            raise Exception("Link was not accepted by JDownloader linkgrabber")

        if autostart:
            moved = self._move_linkgrabber_entries(device, entries)
            if not moved:
                raise Exception("JDownloader accepted links but could not move them from linkgrabber")

            for _ in range(12):
                if any(self._find_download_entry(device, url, package_name or "") for url in urls):
                    break
                lingering = self._find_linkgrabber_entries(device, urls, package_name or "")
                if lingering:
                    self._move_linkgrabber_entries(device, lingering)
                time.sleep(0.5)

    async def add_package(self, urls: List[str], dest_dir: str,
                          package_name: str = "",
                          autostart: bool = True,
                          auto_extract: bool = True) -> bool:
        if not urls:
            raise ValueError("No URLs supplied for JDownloader package")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self._add_links_sync,
            urls, dest_dir, package_name, autostart, auto_extract
        )
        url = urls[0]
        filename = package_name
        logger.info(f"JD addLinks: {filename or url[:60]} → {dest_dir}")
        return True

    async def add_link(self, url: str, dest_dir: str,
                       filename: str = "",
                       autostart: bool = True,
                       auto_extract: bool = True) -> bool:
        return await self.add_package([url], dest_dir, filename, autostart, auto_extract)

    def _get_download_state_sync(self, url: str, filename: str) -> Dict:
        jd = _make_jd(self._email, self._password)
        device = _get_device(jd, self._device_name)
        entry = self._find_download_entry(device, url, filename)
        if not entry:
            linkgrabber_entry = self._find_linkgrabber_entry(device, url, filename)
            if linkgrabber_entry:
                self._move_linkgrabber_entries(device, [linkgrabber_entry])
                time.sleep(1)
                entry = self._find_download_entry(device, url, filename)
            if not entry:
                return {"present": False, "finished": False, "status": "missing"}
        return {
            "present": True,
            "finished": bool(entry.get("finished")),
            "status": str(entry.get("status", "")),
            "running": bool(entry.get("running")),
            "bytesLoaded": int(entry.get("bytesLoaded", 0) or 0),
            "bytesTotal": int(entry.get("bytesTotal", 0) or 0),
        }

    async def get_download_state(self, url: str, filename: str = "") -> Dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_download_state_sync, url, filename)
