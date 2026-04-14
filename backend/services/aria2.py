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
        remote_file_path: str,
        start_paused: bool = False,
        max_retries: int = 5,
    ) -> str:
        existing = await self.find_existing_download(uri, remote_file_path)
        if existing:
            if start_paused:
                await self.pause(existing.gid)
            return existing.gid

        remote = self._normalize_path(remote_file_path)
        remote_dir = str(PurePosixPath(remote).parent)
        remote_name = PurePosixPath(remote).name
        options: Dict[str, Any] = {"dir": remote_dir, "out": remote_name}
        if start_paused:
            options["pause"] = "true"

        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                gid = await self._call("aria2.addUri", [[uri], options])
                await self.tell_status(gid)
                logger.info("Queued aria2 download %s -> %s (%s)", uri, remote, gid)
                return gid
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                delay = min(attempt * attempt, 10)
                logger.warning(
                    "Unable to queue aria2 download on attempt %s/%s for %s, retrying in %ss: %s",
                    attempt,
                    max_retries,
                    uri,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        raise Aria2RPCError(f"Unable to queue aria2 download for {uri}: {last_error}")

    async def find_existing_download(
        self,
        uri: str,
        remote_file_path: str,
    ) -> Optional[Aria2DownloadStatus]:
        normalized_path = self._normalize_path(remote_file_path)
        for download in await self.get_all():
            if download.status in {"complete", "removed"}:
                continue
            for file_info in download.files or []:
                current_path = self._normalize_path(str(file_info.get("path", "")))
                if current_path and current_path == normalized_path:
                    return download
                for current_uri in file_info.get("uris", []) or []:
                    if str(current_uri.get("uri", "")).strip() == uri.strip():
                        return download
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
