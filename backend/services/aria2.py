import asyncio
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.aria2")


class Aria2RPCError(Exception):
    pass


@dataclass
class Aria2DownloadStatus:
    gid: str
    status: str
    total_length: int
    completed_length: int
    download_speed: int
    error_code: str = ""
    error_message: str = ""
    files: Optional[List[Dict[str, Any]]] = None


class Aria2Service:
    def __init__(self, url: str, secret: str = "", timeout_seconds: int = 15):
        self.url = url.strip()
        self.secret = secret.strip()
        self.timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_seconds or 15)))
        self._request_id = 0

    async def test(self) -> Dict[str, Any]:
        version = await self._call("aria2.getVersion")
        return {
            "version": version.get("version", "unknown"),
            "enabled_features": version.get("enabledFeatures", []),
        }

    async def get_all(self) -> List[Aria2DownloadStatus]:
        token_prefix = [f"token:{self.secret}"] if self.secret else []
        methods = [
            {"methodName": "aria2.tellActive", "params": token_prefix + [self._keys()]},
            {"methodName": "aria2.tellWaiting", "params": token_prefix + [0, 1000, self._keys()]},
            {"methodName": "aria2.tellStopped", "params": token_prefix + [0, 1000, self._keys()]},
        ]
        results = await self._call_multicall(methods)
        downloads: List[Aria2DownloadStatus] = []
        for entry in results or []:
            payload = entry[0] if isinstance(entry, list) and entry else []
            for raw in payload or []:
                downloads.append(self._normalize(raw))
        return downloads

    async def tell_status(self, gid: str) -> Aria2DownloadStatus:
        result = await self._call("aria2.tellStatus", [gid, self._keys()])
        return self._normalize(result)

    async def ensure_download(
        self,
        uri: str,
        options: Optional[Dict[str, Any]] = None,
        start_paused: bool = False,
        max_retries: int = 5,
    ) -> str:
        """Add a download to aria2 if not already present.

        Deduplication is done exclusively by URI — path comparison is unreliable
        when the manager and aria2 run in separate containers with different mounts.

        Returns the GID of the (existing or newly added) download.
        """
        all_downloads = await self.get_all()
        matches = self._find_all_matching_by_uri(uri, all_downloads)

        # Already finished — remove any stale duplicates, return the complete GID.
        for dl in matches:
            if dl.status in {"complete", "removed"}:
                for dup in matches:
                    if dup.gid != dl.gid and dup.status not in {"complete", "removed"}:
                        logger.warning("Removing stale duplicate aria2 entry %s for %s", dup.gid, uri)
                        await self.remove(dup.gid)
                return dl.gid

        # Multiple active entries for the same URI — keep the first, drop the rest.
        if len(matches) > 1:
            for dup in matches[1:]:
                logger.warning("Removing duplicate aria2 entry %s for %s", dup.gid, uri)
                await self.remove(dup.gid)

        if matches:
            existing = matches[0]
            if start_paused and existing.status != "paused":
                await self.pause(existing.gid)
            return existing.gid

        rpc_options: Dict[str, Any] = dict(options or {})
        if start_paused:
            rpc_options["pause"] = "true"

        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                gid = await self._call("aria2.addUri", [[uri], rpc_options])
                logger.info("Queued aria2 download %s (%s)", uri, gid)
                return gid
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                delay = min(attempt * attempt, 10)
                logger.warning(
                    "Unable to queue aria2 download on attempt %s/%s for %s, retrying in %ss: %s",
                    attempt, max_retries, uri, delay, exc,
                )
                await asyncio.sleep(delay)

        raise Aria2RPCError(f"Unable to queue aria2 download for {uri}: {last_error}")

    def _find_all_matching_by_uri(
        self,
        uri: str,
        all_downloads: List["Aria2DownloadStatus"],
    ) -> List["Aria2DownloadStatus"]:
        """Return all downloads whose URI list contains the given URI.
        complete/removed entries are sorted first so they are detected early."""
        uri = uri.strip()
        matched: List[Aria2DownloadStatus] = []
        for download in all_downloads:
            for file_info in download.files or []:
                for u in file_info.get("uris", []) or []:
                    if str(u.get("uri", "")).strip() == uri:
                        matched.append(download)
                        break
                else:
                    continue
                break
        matched.sort(key=lambda d: 0 if d.status in {"complete", "removed"} else 1)
        return matched

    async def find_existing_download(
        self,
        uri: str,
    ) -> Optional["Aria2DownloadStatus"]:
        """Return the first active (non-complete) download for the given URI."""
        all_downloads = await self.get_all()
        for dl in self._find_all_matching_by_uri(uri, all_downloads):
            if dl.status not in {"complete", "removed"}:
                return dl
        return None

    async def pause(self, gid: str):
        await self._best_effort("aria2.pause", [gid])

    async def resume(self, gid: str):
        await self._best_effort("aria2.unpause", [gid])

    async def remove(self, gid: str):
        await self._best_effort("aria2.forceRemove", [gid])
        await self._best_effort("aria2.removeDownloadResult", [gid])

    async def _call_multicall(self, methods: List[Dict[str, Any]]) -> Any:
        """Call system.multicall without prepending the token at the outer level.
        The token must already be embedded in each sub-call's params."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._request_id),
            "method": "system.multicall",
            "params": [methods],
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(self.url, json=payload) as response:
                    data = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise Aria2RPCError(f"Network error talking to aria2: {exc}") from exc

        if "error" in data:
            error = data["error"] or {}
            raise Aria2RPCError(f"aria2 [{error.get('code', 'UNKNOWN')}]: {error.get('message', 'Unknown error')}")

        return data.get("result")

    async def _best_effort(self, method: str, params: List[Any]):
        try:
            await self._call(method, params)
        except Exception as exc:
            logger.debug("aria2 %s failed for %s: %s", method, params, exc)

    async def _call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        self._request_id += 1
        rpc_params = list(params or [])
        if self.secret:
            rpc_params.insert(0, f"token:{self.secret}")

        payload = {
            "jsonrpc": "2.0",
            "id": str(self._request_id),
            "method": method,
            "params": rpc_params,
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(self.url, json=payload) as response:
                    data = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise Aria2RPCError(f"Network error talking to aria2: {exc}") from exc

        if "error" in data:
            error = data["error"] or {}
            raise Aria2RPCError(f"aria2 [{error.get('code', 'UNKNOWN')}]: {error.get('message', 'Unknown error')}")

        return data.get("result")

    def _normalize(self, raw: Dict[str, Any]) -> Aria2DownloadStatus:
        return Aria2DownloadStatus(
            gid=str(raw.get("gid", "")),
            status=str(raw.get("status", "")),
            total_length=int(raw.get("totalLength", 0) or 0),
            completed_length=int(raw.get("completedLength", 0) or 0),
            download_speed=int(raw.get("downloadSpeed", 0) or 0),
            error_code=str(raw.get("errorCode", "") or ""),
            error_message=str(raw.get("errorMessage", "") or ""),
            files=list(raw.get("files") or []),
        )

    @staticmethod
    def _keys() -> List[str]:
        return [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "errorCode",
            "errorMessage",
            "files",
        ]

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not path:
            return ""
        return str(PurePosixPath(path.replace("\\", "/"))).strip()
