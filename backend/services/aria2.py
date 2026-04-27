"""
aria2 JSON-RPC client with robust connection handling.

Improvements over the original:
- Each HTTP request creates its own ClientSession with force_close=True,
  eliminating "Cannot write to closing transport" errors entirely
- Transient connection errors (aria2 restart, brief outages) are logged
  at DEBUG/WARNING instead of ERROR
- Clear error classes: Aria2RPCError (RPC logic) vs Aria2ConnectionError (network)
- Retry logic with backoff for connection errors
- get_all() returns an empty list on connection error instead of raising
"""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.aria2")

# Error messages indicating a closing or closed transport
_CLOSING_TRANSPORT_MSGS = frozenset({
    "Cannot write to closing transport",
    "Connection reset by peer",
    "Connection closed",
    "ServerDisconnectedError",
    "Cannot connect to host",
})


def _is_transient_connection_error(exc: Exception) -> bool:
    """Returns True if the exception is an expected transient connection error."""
    msg = str(exc)
    return any(m in msg for m in _CLOSING_TRANSPORT_MSGS) or isinstance(
        exc, (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError)
    )


class Aria2RPCError(Exception):
    """RPC error from aria2 (e.g. invalid parameters, unknown GID)."""


class Aria2ConnectionError(Aria2RPCError):
    """
    Connection error to aria2 (e.g. unreachable, closing transport).
    Subclass of Aria2RPCError for backward compatibility.
    """


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


def aria2_download_to_dict(download: Aria2DownloadStatus) -> Dict[str, Any]:
    total = int(getattr(download, "total_length", 0) or 0)
    completed = int(getattr(download, "completed_length", 0) or 0)
    progress = round((completed / total) * 100, 2) if total > 0 else 0.0
    files = []
    for file_info in getattr(download, "files", None) or []:
        path = str(file_info.get("path", "") or "")
        length = int(file_info.get("length", 0) or 0)
        completed_length = int(file_info.get("completedLength", 0) or 0)
        selected = str(file_info.get("selected", "true")).lower() != "false"
        uris = [
            str(uri.get("uri", "") or "")
            for uri in file_info.get("uris", []) or []
            if str(uri.get("uri", "") or "").strip()
        ]
        files.append({
            "path": path,
            "name": Path(path).name if path else "",
            "length": length,
            "completed_length": completed_length,
            "progress": round((completed_length / length) * 100, 2) if length > 0 else 0.0,
            "selected": selected,
            "uris": uris,
        })
    first_file = files[0] if files else {}
    name = first_file.get("name") or getattr(download, "gid", "")
    return {
        "gid": getattr(download, "gid", ""),
        "status": getattr(download, "status", ""),
        "name": name,
        "path": first_file.get("path", ""),
        "total_length": total,
        "completed_length": completed,
        "remaining_length": max(total - completed, 0),
        "progress": progress,
        "download_speed": int(getattr(download, "download_speed", 0) or 0),
        "error_code": getattr(download, "error_code", ""),
        "error_message": getattr(download, "error_message", ""),
        "files": files,
    }


class Aria2Service:
    def __init__(self, url: str, secret: str = "", timeout_seconds: int = 15):
        self.url = url.strip()
        self.secret = secret.strip()
        self.timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_seconds or 15)))
        self._request_id = 0
        self._uri_locks: Dict[str, asyncio.Lock] = {}
        self._rpc_lock = asyncio.Lock()   # serialises concurrent RPC calls
        self._last_call_time: float = 0.0 # for min-interval enforcement

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def test(self) -> Dict[str, Any]:
        version = await self._call("aria2.getVersion")
        return {
            "version": version.get("version", "unknown"),
            "enabled_features": version.get("enabledFeatures", []),
        }

    async def get_global_options(self) -> Dict[str, Any]:
        return await self._call("aria2.getGlobalOption")

    async def change_global_options(self, options: Dict[str, Any]) -> Any:
        return await self._call("aria2.changeGlobalOption", [options])

    async def purge_download_results(self) -> Any:
        return await self._call("aria2.purgeDownloadResult")

    async def get_memory_diagnostics(
        self,
        waiting_limit: int = 100,
        stopped_limit: int = 100,
    ) -> Dict[str, Any]:
        waiting_limit = self._bounded_window(waiting_limit)
        stopped_limit = self._bounded_window(stopped_limit)
        active, waiting, stopped = await asyncio.gather(
            self._call("aria2.tellActive", [self._keys()]),
            self._call("aria2.tellWaiting", [0, waiting_limit, self._keys()]),
            self._call("aria2.tellStopped", [0, stopped_limit, self._keys()]),
        )
        options = await self.get_global_options()
        return {
            "active_count": len(active or []),
            "waiting_count": len(waiting or []),
            "stopped_count": len(stopped or []),
            "query_limits": {
                "waiting": waiting_limit,
                "stopped": stopped_limit,
            },
            "global_options": {
                "max-download-result": str((options or {}).get("max-download-result", "")),
                "keep-unfinished-download-result": str((options or {}).get("keep-unfinished-download-result", "")),
            },
        }

    async def get_all(
        self,
        waiting_limit: int = 100,
        stopped_limit: int = 100,
    ) -> List[Aria2DownloadStatus]:
        """
        Fetches active, waiting and stopped downloads.

        On connection errors an empty list is returned and the error
        is logged as WARNING so the scheduler keeps running.
        """
        waiting_limit = self._bounded_window(waiting_limit)
        stopped_limit = self._bounded_window(stopped_limit)
        try:
            results = await asyncio.gather(
                self._call("aria2.tellActive", [self._keys()]),
                self._call("aria2.tellWaiting", [0, waiting_limit, self._keys()]),
                self._call("aria2.tellStopped", [0, stopped_limit, self._keys()]),
            )
        except Aria2ConnectionError as exc:
            logger.warning("aria2 unreachable (get_all): %s", exc)
            return []
        except Aria2RPCError as exc:
            logger.error("aria2 RPC error (get_all): %s", exc)
            return []

        downloads: List[Aria2DownloadStatus] = []
        for payload in results:
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
        cached_downloads: Optional[List["Aria2DownloadStatus"]] = None,
    ) -> str:
        """
        Adds a download to aria2 if not already present.

        Deduplication is performed by URI and target path.
        Pass cached_downloads to skip an extra get_all() call when dispatching
        multiple files in the same cycle (avoids aria2 request storms).
        """
        normalized_uri = uri.strip()
        target_path = self._target_path_from_options(options)
        async with self._lock_for_uri(normalized_uri):
            all_downloads = cached_downloads if cached_downloads is not None else await self.get_all()
            matches = self._find_all_matches(normalized_uri, target_path, all_downloads)

            for dl in matches:
                if dl.status in {"complete", "removed"}:
                    for dup in matches:
                        if dup.gid != dl.gid and dup.status not in {"complete", "removed"}:
                            logger.warning(
                                "Removing stale duplicate aria2 entry %s for %s", dup.gid, normalized_uri
                            )
                            await self.remove(dup.gid)
                    return dl.gid

            if len(matches) > 1:
                for dup in matches[1:]:
                    logger.warning(
                        "Removing duplicate aria2 entry %s for %s", dup.gid, normalized_uri
                    )
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
                    gid = await self._call("aria2.addUri", [[normalized_uri], rpc_options])
                    logger.info("aria2: queued download %s (%s)", normalized_uri, gid)
                    return gid
                except Aria2ConnectionError as exc:
                    last_error = exc
                    if attempt >= max_retries:
                        break
                    delay = min(attempt * attempt, 10)
                    logger.warning(
                        "aria2 unreachable (attempt %s/%s), retrying in %ss: %s",
                        attempt, max_retries, delay, exc,
                    )
                    await asyncio.sleep(delay)
                except Aria2RPCError as exc:
                    # RPC errors cannot be resolved by retrying
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt >= max_retries:
                        break
                    delay = min(attempt * attempt, 10)
                    logger.warning(
                        "Error queuing download (attempt %s/%s) for %s, retrying in %ss: %s",
                        attempt, max_retries, normalized_uri, delay, exc,
                    )
                    await asyncio.sleep(delay)

        raise Aria2RPCError(
            f"Unable to queue aria2 download for {normalized_uri}: {last_error}"
        )

    def _find_all_matches(
        self,
        uri: str,
        target_path: str,
        all_downloads: List["Aria2DownloadStatus"],
    ) -> List["Aria2DownloadStatus"]:
        uri = uri.strip()
        target_path = self._normalize_path(target_path)
        matched: List[Aria2DownloadStatus] = []
        for download in all_downloads:
            for file_info in download.files or []:
                current_path = self._normalize_path(str(file_info.get("path", "")))
                if target_path and current_path == target_path:
                    matched.append(download)
                    break
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
        all_downloads = await self.get_all()
        for dl in self._find_all_matches(uri, "", all_downloads):
            if dl.status not in {"complete", "removed"}:
                return dl
        return None

    def _lock_for_uri(self, uri: str) -> asyncio.Lock:
        lock = self._uri_locks.get(uri)
        if lock is None:
            lock = asyncio.Lock()
            self._uri_locks[uri] = lock
        return lock

    def _bounded_window(self, value: int) -> int:
        try:
            return max(10, min(1000, int(value or 100)))
        except Exception:
            return 100

    def _target_path_from_options(self, options: Optional[Dict[str, Any]]) -> str:
        if not options:
            return ""
        directory = str(options.get("dir", "") or "").strip()
        out_name = str(options.get("out", "") or "").strip()
        if not directory or not out_name:
            return ""
        return self._normalize_path(str(PurePosixPath(directory) / out_name))

    async def pause(self, gid: str):
        await self._best_effort("aria2.pause", [gid])

    async def resume(self, gid: str):
        await self._best_effort("aria2.unpause", [gid])

    async def remove(self, gid: str):
        await self._best_effort("aria2.forceRemove", [gid])
        await self._best_effort("aria2.removeDownloadResult", [gid])

    # ─────────────────────────────────────────────────────────────────────────
    # Interne RPC-Implementierung
    # ─────────────────────────────────────────────────────────────────────────

    async def _best_effort(self, method: str, params: List[Any]):
        try:
            await self._call(method, params)
        except Aria2ConnectionError as exc:
            logger.debug("aria2 %s skipped (connection error): %s", method, exc)
        except Exception as exc:
            logger.debug("aria2 %s failed for %s: %s", method, params, exc)

    async def _call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        """
        Executes a single JSON-RPC call.

        Creates a new ClientSession with force_close=True for each call
        to ensure no transport is written to while closing.
        Serialises concurrent calls via _rpc_lock and enforces a minimum
        50ms inter-request interval to prevent aria2 from dropping requests
        under rapid sequential load.
        """
        import time as _time
        async with self._rpc_lock:
            # Enforce minimum interval between aria2 RPC calls
            now = _time.monotonic()
            gap = now - self._last_call_time
            if gap < 0.05:  # 50ms minimum
                await asyncio.sleep(0.05 - gap)
            self._last_call_time = _time.monotonic()

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

        # force_close=True: each session closes the connection after the request.
        # This prevents 'Cannot write to closing transport' on subsequent calls.
        connector = aiohttp.TCPConnector(force_close=True)
        try:
            async with aiohttp.ClientSession(
                timeout=self.timeout,
                connector=connector,
            ) as session:
                try:
                    async with session.post(self.url, json=payload) as response:
                        data = await response.json(content_type=None)
                except (
                    aiohttp.ServerDisconnectedError,
                    aiohttp.ClientConnectorError,
                    aiohttp.ClientOSError,
                    ConnectionResetError,
                ) as exc:
                    raise Aria2ConnectionError(
                        f"Connection to aria2 lost: {exc}"
                    ) from exc
                except aiohttp.ClientError as exc:
                    if _is_transient_connection_error(exc):
                        raise Aria2ConnectionError(
                            f"Transient connection error to aria2: {exc}"
                        ) from exc
                    raise Aria2RPCError(f"Network error communicating with aria2: {exc}") from exc
        finally:
            await connector.close()

        if "error" in data:
            error = data["error"] or {}
            raise Aria2RPCError(
                f"aria2 [{error.get('code', 'UNKNOWN')}]: {error.get('message', 'Unknown error')}"
            )

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
