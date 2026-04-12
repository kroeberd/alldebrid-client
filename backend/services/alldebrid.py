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
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        session = await self._get_session()
        params = kwargs.pop("params", {})
        params["agent"] = self.agent
        params["apikey"] = self.api_key
        url = f"{ALLDEBRID_API}/{endpoint}"
        async with session.request(method, url, params=params, **kwargs) as resp:
            data = await resp.json()
            if data.get("status") != "success":
                err = data.get("error", {})
                msg = err.get("message", "Unknown error") if isinstance(err, dict) else str(err)
                raise Exception(f"AllDebrid API error: {msg}")
            return data.get("data", {})

    async def get_user(self) -> Dict:
        return await self._request("GET", "user")

    async def upload_magnet(self, magnet: str) -> Dict:
        data = await self._request("GET", "magnet/upload", params={"magnets[]": magnet})
        magnets = data.get("magnets", [])
        if not magnets:
            raise Exception("No magnet returned from upload")
        return magnets[0]

    async def upload_magnets(self, magnets: List[str]) -> List[Dict]:
        results = []
        for magnet in magnets:
            try:
                result = await self.upload_magnet(magnet)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to upload magnet: {e}")
                results.append({"error": str(e), "magnet": magnet})
        return results

    async def get_magnet_status(self, magnet_id: str) -> Dict:
        return await self._request("GET", "magnet/status", params={"id": magnet_id})

    async def get_all_magnets(self) -> List[Dict]:
        data = await self._request("GET", "magnet/status")
        return data.get("magnets", [])

    async def delete_magnet(self, magnet_id: str) -> bool:
        try:
            await self._request("GET", "magnet/delete", params={"id": magnet_id})
            return True
        except Exception as e:
            logger.error(f"Failed to delete magnet {magnet_id}: {e}")
            return False

    async def unlock_link(self, link: str) -> Dict:
        return await self._request("GET", "link/unlock", params={"link": link})

    async def get_saved_links(self) -> List[Dict]:
        data = await self._request("GET", "user/links")
        return data.get("links", [])

    async def check_magnet_immediate(self, magnet: str) -> Dict:
        return await self._request("GET", "magnet/instant", params={"magnets[]": magnet})
