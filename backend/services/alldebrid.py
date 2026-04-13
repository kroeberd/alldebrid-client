"""
AllDebrid API v4 / v4.1 client.

Auth: Authorization: Bearer <apikey> header only.
All mutating calls use POST.

Key endpoints:
  POST /v4.1/magnet/status  — status (id=X for single, no id for all)
  POST /v4/magnet/status    — deprecated fallback for "get all"
  POST /v4/magnet/files     — get download links for ready magnets
  POST /v4/magnet/upload    — upload magnet URI
  POST /v4/magnet/upload/file — upload .torrent file
  POST /v4/magnet/delete    — delete magnet
  POST /v4/link/unlock      — unlock a debrid link

statusCode: 0-3=Processing, 4=Ready, 5-15=Error

NOTE: Uses a fresh aiohttp session per request to avoid ServerDisconnected
errors from AllDebrid closing keep-alive connections.
"""

import aiohttp
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger("alldebrid.api")

API_V4  = "https://api.alldebrid.com/v4"
API_V41 = "https://api.alldebrid.com/v4.1"

TIMEOUT = aiohttp.ClientTimeout(total=30)


class AllDebridService:
    def __init__(self, api_key: str, agent: str = "AllDebrid-Client"):
        self.api_key = api_key
        self.agent   = agent

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _post(self, base: str, endpoint: str,
                    data: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{base}/{endpoint}"
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.post(url, data=data or {}, timeout=TIMEOUT) as resp:
                    result = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise Exception(f"Network error: {e}")

        if result.get("status") != "success":
            err  = result.get("error", {})
            code = err.get("code", "UNKNOWN") if isinstance(err, dict) else "UNKNOWN"
            msg  = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise Exception(f"AllDebrid [{code}]: {msg}")

        return result.get("data", {})

    async def _multipart(self, endpoint: str, form: aiohttp.FormData) -> Dict[str, Any]:
        url = f"{API_V4}/{endpoint}"
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.post(url, data=form,
                                  timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    result = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise Exception(f"Network error uploading: {e}")
        if result.get("status") != "success":
            err = result.get("error", {})
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise Exception(f"AllDebrid upload error: {msg}")
        return result.get("data", {})

    # ── User ──────────────────────────────────────────────────────────────────

    async def get_user(self) -> Dict:
        return await self._post(API_V4, "user")

    # ── Magnets ───────────────────────────────────────────────────────────────

    async def upload_magnet(self, magnet: str) -> Dict:
        data = await self._post(API_V4, "magnet/upload", {"magnets[]": magnet})
        magnets = data.get("magnets", [])
        if not magnets:
            raise Exception("AllDebrid returned no magnet data")
        m = magnets[0]
        if "error" in m:
            err = m["error"]
            raise Exception(f"AllDebrid [{err.get('code')}]: {err.get('message')}")
        return m

    async def upload_torrent_file(self, file_bytes: bytes, filename: str) -> Dict:
        form = aiohttp.FormData()
        form.add_field("files[]", file_bytes, filename=filename,
                       content_type="application/x-bittorrent")
        data = await self._multipart("magnet/upload/file", form)
        files = data.get("files", [])
        if not files:
            raise Exception("AllDebrid returned no file data after upload")
        f = files[0]
        if "error" in f:
            err = f["error"]
            raise Exception(f"AllDebrid [{err.get('code')}]: {err.get('message')}")
        return f

    async def get_magnet_status(self, magnet_id: Optional[str] = None) -> List[Dict]:
        """
        Get status for one magnet (with id) or all magnets (no id).
        Falls back to deprecated v4 endpoint if v4.1 is discontinued.
        """
        payload = {}
        if magnet_id:
            payload["id"] = str(magnet_id)

        try:
            data = await self._post(API_V41, "magnet/status", payload)
            raw = data.get("magnets", [])
            if isinstance(raw, dict):
                return [raw]
            return raw if isinstance(raw, list) else []
        except Exception as e:
            if magnet_id:
                raise  # per-ID failure is a real error
            err = str(e)
            if not any(kw in err for kw in
                       ("DISCONTINUED", "discontinued", "deprecated", "migrate")):
                raise
            logger.debug(f"v4.1 get-all unavailable, trying v4: {err}")

        # Fallback: deprecated /v4/magnet/status
        try:
            data = await self._post(API_V4, "magnet/status", payload)
            raw = data.get("magnets", [])
            if isinstance(raw, dict):
                return [raw]
            return raw if isinstance(raw, list) else []
        except Exception as e2:
            if any(kw in str(e2) for kw in ("DISCONTINUED", "discontinued")):
                raise Exception(
                    "AllDebrid has disabled 'list all magnets' for your account."
                )
            raise

    async def get_magnet_files(self, magnet_ids: List[str]) -> List[Dict]:
        if not magnet_ids:
            return []
        payload = {f"id[{i}]": str(mid) for i, mid in enumerate(magnet_ids)}
        data = await self._post(API_V4, "magnet/files", payload)
        return data.get("magnets", [])

    async def delete_magnet(self, magnet_id: str) -> bool:
        try:
            await self._post(API_V4, "magnet/delete", {"id": str(magnet_id)})
            return True
        except Exception as e:
            logger.error(f"Delete magnet {magnet_id}: {e}")
            return False

    async def restart_magnet(self, magnet_id: str) -> bool:
        try:
            await self._post(API_V4, "magnet/restart", {"id": str(magnet_id)})
            return True
        except Exception as e:
            logger.error(f"Restart magnet {magnet_id}: {e}")
            return False

    async def unlock_link(self, link: str) -> Dict:
        return await self._post(API_V4, "link/unlock", {"link": link})

    async def close(self):
        pass  # no persistent session to close


def flatten_files(nodes: List[Dict]) -> List[Dict]:
    """Recursively flatten the nested file tree from /v4/magnet/files."""
    result = []
    for node in nodes:
        if node is None:
            continue
        if "l" in node:
            result.append({
                "name": node.get("n", ""),
                "size": node.get("s", 0),
                "link": node["l"],
            })
        elif "e" in node and isinstance(node["e"], list):
            result.extend(flatten_files(node["e"]))
    return result
