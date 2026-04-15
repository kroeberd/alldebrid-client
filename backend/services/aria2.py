"""
aria2 JSON-RPC Client mit robuster Verbindungsbehandlung.

Verbesserungen gegenüber der ursprünglichen Version:
- Jede HTTP-Anfrage erstellt eine eigene ClientSession mit force_close=True,
  um "Cannot write to closing transport" vollständig zu vermeiden
- Transiente Verbindungsfehler (Neustart von aria2, kurze Ausfälle) werden
  als DEBUG/WARNING statt ERROR geloggt
- Klar getrennte Fehlerklassen: Aria2RPCError (RPC-Logik) vs. Aria2ConnectionError (Netzwerk)
- Retry-Logik mit Backoff für Verbindungsfehler
- get_all() gibt bei Verbindungsfehler eine leere Liste zurück statt zu werfen
"""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.aria2")

# Fehlermeldungen, die auf einen schließenden/geschlossenen Transport hinweisen
_CLOSING_TRANSPORT_MSGS = frozenset({
    "Cannot write to closing transport",
    "Connection reset by peer",
    "Connection closed",
    "ServerDisconnectedError",
    "Cannot connect to host",
})


def _is_transient_connection_error(exc: Exception) -> bool:
    """Prüft ob eine Exception ein erwarteter, transienter Verbindungsfehler ist."""
    msg = str(exc)
    return any(m in msg for m in _CLOSING_TRANSPORT_MSGS) or isinstance(
        exc, (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError)
    )


class Aria2RPCError(Exception):
    """RPC-Fehler von aria2 (z.B. ungültige Parameter, unbekannte GID)."""


class Aria2ConnectionError(Aria2RPCError):
    """
    Verbindungsfehler zu aria2 (z.B. nicht erreichbar, Transport schließt).
    Subklasse von Aria2RPCError für Abwärtskompatibilität.
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


class Aria2Service:
    def __init__(self, url: str, secret: str = "", timeout_seconds: int = 15):
        self.url = url.strip()
        self.secret = secret.strip()
        self.timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_seconds or 15)))
        self._request_id = 0
        self._uri_locks: Dict[str, asyncio.Lock] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Öffentliche API
    # ─────────────────────────────────────────────────────────────────────────

    async def test(self) -> Dict[str, Any]:
        version = await self._call("aria2.getVersion")
        return {
            "version": version.get("version", "unknown"),
            "enabled_features": version.get("enabledFeatures", []),
        }

    async def get_all(self) -> List[Aria2DownloadStatus]:
        """
        Ruft aktive, wartende und gestoppte Downloads ab.

        Bei Verbindungsfehlern wird eine leere Liste zurückgegeben und der
        Fehler als WARNING geloggt, damit der Scheduler weiterläuft.
        """
        try:
            results = await asyncio.gather(
                self._call("aria2.tellActive", [self._keys()]),
                self._call("aria2.tellWaiting", [0, 1000, self._keys()]),
                self._call("aria2.tellStopped", [0, 1000, self._keys()]),
            )
        except Aria2ConnectionError as exc:
            logger.warning("aria2 nicht erreichbar (get_all): %s", exc)
            return []
        except Aria2RPCError as exc:
            logger.error("aria2 RPC-Fehler (get_all): %s", exc)
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
    ) -> str:
        """
        Fügt einen Download zu aria2 hinzu, wenn er noch nicht vorhanden ist.

        Deduplizierung erfolgt via URI und Zielpfad.
        """
        normalized_uri = uri.strip()
        target_path = self._target_path_from_options(options)
        async with self._lock_for_uri(normalized_uri):
            all_downloads = await self.get_all()
            matches = self._find_all_matches(normalized_uri, target_path, all_downloads)

            for dl in matches:
                if dl.status in {"complete", "removed"}:
                    for dup in matches:
                        if dup.gid != dl.gid and dup.status not in {"complete", "removed"}:
                            logger.warning(
                                "Entferne veralteten aria2-Eintrag %s für %s", dup.gid, normalized_uri
                            )
                            await self.remove(dup.gid)
                    return dl.gid

            if len(matches) > 1:
                for dup in matches[1:]:
                    logger.warning(
                        "Entferne doppelten aria2-Eintrag %s für %s", dup.gid, normalized_uri
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
                    logger.info("aria2: Download eingereiht %s (%s)", normalized_uri, gid)
                    return gid
                except Aria2ConnectionError as exc:
                    last_error = exc
                    if attempt >= max_retries:
                        break
                    delay = min(attempt * attempt, 10)
                    logger.warning(
                        "aria2 nicht erreichbar (Versuch %s/%s), Retry in %ss: %s",
                        attempt, max_retries, delay, exc,
                    )
                    await asyncio.sleep(delay)
                except Aria2RPCError as exc:
                    # RPC-Fehler sind nicht durch Retry behebbar
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt >= max_retries:
                        break
                    delay = min(attempt * attempt, 10)
                    logger.warning(
                        "Fehler beim Einreihen (Versuch %s/%s) für %s, Retry in %ss: %s",
                        attempt, max_retries, normalized_uri, delay, exc,
                    )
                    await asyncio.sleep(delay)

        raise Aria2RPCError(
            f"Download konnte nicht eingereiht werden für {normalized_uri}: {last_error}"
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
            logger.debug("aria2 %s nicht ausführbar (Verbindung): %s", method, exc)
        except Exception as exc:
            logger.debug("aria2 %s fehlgeschlagen für %s: %s", method, params, exc)

    async def _call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        """
        Führt einen einzelnen JSON-RPC-Aufruf durch.

        Erstellt für jeden Aufruf eine neue ClientSession mit force_close=True,
        damit kein Transport im schließenden Zustand beschrieben wird.
        """
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

        # force_close=True: Jede Session schließt die Verbindung nach der Anfrage.
        # Das verhindert "Cannot write to closing transport" bei Folgeaufrufen.
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
                        f"Verbindung zu aria2 unterbrochen: {exc}"
                    ) from exc
                except aiohttp.ClientError as exc:
                    if _is_transient_connection_error(exc):
                        raise Aria2ConnectionError(
                            f"Transiente Verbindungsunterbrechung zu aria2: {exc}"
                        ) from exc
                    raise Aria2RPCError(f"Netzwerkfehler bei aria2-Kommunikation: {exc}") from exc
        finally:
            await connector.close()

        if "error" in data:
            error = data["error"] or {}
            raise Aria2RPCError(
                f"aria2 [{error.get('code', 'UNKNOWN')}]: {error.get('message', 'Unbekannter Fehler')}"
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
