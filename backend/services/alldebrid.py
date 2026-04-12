import aiohttp
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger("alldebrid.api")

ALLDEBRID_API = "https://api.alldebrid.com/v4"


class AllDebridService:
    def __init__(self, api_key: str, agent: str = "AllDebrid-Client"):
        self.api_key = api_key
        self.agent = agent
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Use Bearer auth header (new API requirement) + apikey fallback param
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.api_key}"}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        session = await self._get_session()
        params = kwargs.pop("params", {})
        params["agent"] = self.agent
        # Keep apikey param for backwards compat — but Bearer header is primary
        params["apikey"] = self.api_key
        url = f"{ALLDEBRID_API}/{endpoint}"
        try:
            async with session.request(method, url, params=params, **kwargs) as resp:
                data = await resp.json(content_type=None)
                if data.get("status") != "success":
                    err = data.get("error", {})
                    msg = err.get("message", "Unknown error") if isinstance(err, dict) else str(err)
                    code = err.get("code", "") if isinstance(err, dict) else ""
                    raise Exception(f"AllDebrid API error [{code}]: {msg}")
                return data.get("data", {})
        except aiohttp.ClientError as e:
            raise Exception(f"Network error: {e}")

    async def get_user(self) -> Dict:
        return await self._request("GET", "user")

    async def upload_magnet(self, magnet: str) -> Dict:
        # Use POST with form data to avoid URL length issues
        data = await self._request(
            "POST", "magnet/upload",
            data={"magnets[]": magnet}
        )
        magnets = data.get("magnets", [])
        if not magnets:
            raise Exception("No magnet returned from AllDebrid upload")
        return magnets[0]

    async def upload_torrent_file(self, file_bytes: bytes, filename: str) -> Dict:
        """Upload a .torrent file directly to AllDebrid"""
        form = aiohttp.FormData()
        form.add_field("files[]", file_bytes, filename=filename, content_type="application/x-bittorrent")
        session = await self._get_session()
        params = {"agent": self.agent, "apikey": self.api_key}
        async with session.post(
            f"{ALLDEBRID_API}/magnet/upload/file", params=params, data=form
        ) as resp:
            data = await resp.json(content_type=None)
            if data.get("status") != "success":
                err = data.get("error", {})
                msg = err.get("message", "Upload failed") if isinstance(err, dict) else str(err)
                raise Exception(f"AllDebrid upload error: {msg}")
            files = data.get("data", {}).get("files", [])
            return files[0] if files else {}

    async def get_magnet_status(self, magnet_id: str) -> Dict:
        return await self._request("GET", "magnet/status", params={"id": magnet_id})

    async def get_all_magnets(self) -> List[Dict]:
        """Get all active magnets — uses ?status=active filter per new API"""
        try:
            data = await self._request("GET", "magnet/status")
            return data.get("magnets", [])
        except Exception as e:
            if "deprecated" in str(e).lower() or "discontinued" in str(e).lower():
                # Try without params — some API versions differ
                logger.warning("magnet/status all deprecated, trying active filter")
                data = await self._request("GET", "magnet/status", params={"status": "active"})
                return data.get("magnets", [])
            raise

    async def delete_magnet(self, magnet_id: str) -> bool:
        try:
            await self._request("GET", "magnet/delete", params={"id": magnet_id})
            return True
        except Exception as e:
            logger.error(f"Failed to delete magnet {magnet_id}: {e}")
            return False

    async def restart_magnet(self, magnet_id: str) -> bool:
        try:
            await self._request("GET", "magnet/restart", params={"ids[]": magnet_id})
            return True
        except Exception as e:
            logger.error(f"Failed to restart magnet {magnet_id}: {e}")
            return False

    async def unlock_link(self, link: str) -> Dict:
        return await self._request("GET", "link/unlock", params={"link": link})
