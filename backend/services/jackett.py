"""
Jackett integration service.

Proxies torrent-search requests to a Jackett instance and normalises
the results into a consistent format for the frontend.
"""
from __future__ import annotations

import asyncio
import logging
import re
import base64
import hashlib
import importlib
import time
from datetime import datetime
from email.message import Message
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import aiohttp

logger = logging.getLogger("alldebrid.jackett")
TORRENT_CACHE_TTL_SECONDS = 300
_TORRENT_DOWNLOAD_CACHE: Dict[str, Dict[str, Any]] = {}

# Jackett category IDs (Torznab standard)
CATEGORY_ALL = 0
CATEGORIES: Dict[str, int] = {
    "All": 0,
    "Movies": 2000,
    "TV": 5000,
    "Music": 3000,
    "Books": 7000,
    "Games": 1000,
    "Software": 4000,
    "XXX": 6000,
}


def _cfg():
    try:
        from core.config import get_settings
        return get_settings()
    except Exception:
        return None


def _fmt_size(size_bytes: int) -> str:
    if not size_bytes or size_bytes < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _extract_btih(value: str) -> str:
    match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", value or "", re.I)
    if not match:
        return ""
    infohash = match.group(1)
    if len(infohash) == 32:
        try:
            infohash = base64.b32decode(infohash.upper()).hex()
        except Exception:
            return ""
    return infohash.lower()


def _normalise_result(item: Dict[str, Any]) -> Dict[str, Any]:
    title = (item.get("Title") or "").strip()
    magnet = (item.get("MagnetUri") or "").strip()
    if not magnet:
        for key in ("Guid", "Comments", "Details", "InfoUrl"):
            value = str(item.get(key) or "").strip()
            if value.lower().startswith("magnet:"):
                magnet = value
                break
    torrent = (item.get("Link") or "").strip()
    infohash = str(item.get("InfoHash") or "").strip().lower()
    if not infohash and magnet:
        infohash = _extract_btih(magnet)
    if not magnet and infohash:
        magnet = f"magnet:?xt=urn:btih:{infohash}"
        if title:
            magnet += f"&dn={quote(title)}"

    size_b = int(item.get("Size") or 0)
    seeders = int(item.get("Seeders") or 0)
    leechers = int(item.get("Peers") or item.get("Leechers") or 0)

    pub_date = ""
    raw_date = item.get("PublishDate") or ""
    if raw_date:
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            pub_date = dt.strftime("%Y-%m-%d")
        except Exception:
            pub_date = str(raw_date)[:10]

    return {
        "title": title,
        "indexer": (item.get("Tracker") or item.get("TrackerId") or "").strip(),
        "category": (item.get("CategoryDesc") or "").strip(),
        "size_bytes": size_b,
        "size_human": _fmt_size(size_b),
        "seeders": seeders,
        "leechers": leechers,
        "pub_date": pub_date,
        "magnet": magnet,
        "torrent_url": torrent,
        "hash": infohash,
        "has_link": bool(magnet or torrent),
    }


def _extract_torrent_infohash(content: bytes) -> str:
    if not content:
        return ""
    try:
        bencodepy = importlib.import_module("bencodepy")
        metainfo = bencodepy.decode(content)
        info = metainfo.get(b"info")
        if info is None:
            info = metainfo.get("info")
        if info is None:
            return ""
        encoded_info = bencodepy.encode(info)
        return hashlib.sha1(encoded_info).hexdigest().lower()
    except Exception:
        return ""


def _get_cached_torrent_payload(resolved_url: str) -> Optional[Dict[str, Any]]:
    entry = _TORRENT_DOWNLOAD_CACHE.get(resolved_url)
    if not entry:
        return None
    if float(entry.get("expires_at", 0) or 0) < time.monotonic():
        _TORRENT_DOWNLOAD_CACHE.pop(resolved_url, None)
        return None
    return {
        "filename": entry.get("filename") or "download.torrent",
        "content": entry.get("content") or b"",
        "infohash": entry.get("infohash") or "",
    }


def _store_cached_torrent_payload(resolved_url: str, payload: Dict[str, Any]) -> None:
    _TORRENT_DOWNLOAD_CACHE[resolved_url] = {
        "filename": payload.get("filename") or "download.torrent",
        "content": payload.get("content") or b"",
        "infohash": payload.get("infohash") or "",
        "expires_at": time.monotonic() + TORRENT_CACHE_TTL_SECONDS,
    }


async def _fill_missing_hashes_from_torrent_files(results: List[Dict[str, Any]]) -> None:
    pending = [item for item in results if not str(item.get("hash") or "").strip() and str(item.get("torrent_url") or "").strip()]
    if not pending:
        return

    sem = asyncio.Semaphore(8)

    async def _resolve(item: Dict[str, Any]) -> None:
        async with sem:
            try:
                payload = await download_torrent_file(str(item.get("torrent_url") or "").strip())
                infohash = str(payload.get("infohash") or "").strip().lower()
                if infohash:
                    item["hash"] = infohash
            except Exception:
                return

    await asyncio.gather(*[_resolve(item) for item in pending])


def _build_result_params(query: str, category: int, trackers: List[str], limit: int, api_key: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "apikey": api_key,
        "Query": query.strip(),
        "limit": limit,
    }
    if category and category != CATEGORY_ALL:
        params["Category[]"] = category
    tracker_values = [str(t).strip() for t in trackers if str(t).strip()]
    if tracker_values:
        params["Tracker[]"] = tracker_values
    return params


async def _get_json(session: aiohttp.ClientSession, endpoint: str, params: Dict[str, Any]) -> Any:
    async with session.get(endpoint, params=params) as resp:
        if resp.status == 401:
            raise PermissionError("Invalid Jackett API key")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
        return await resp.json(content_type=None)


async def _get_text(session: aiohttp.ClientSession, endpoint: str, params: Dict[str, Any]) -> str:
    async with session.get(endpoint, params=params) as resp:
        if resp.status == 401:
            raise PermissionError("Invalid Jackett API key")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
        return await resp.text()


def _parse_torznab_indexers(xml_text: str) -> List[Dict[str, str]]:
    if not xml_text.strip():
        return []
    root = ET.fromstring(xml_text)
    items: List[Dict[str, str]] = []
    for indexer in root.findall(".//indexer"):
        idx = (indexer.attrib.get("id") or "").strip()
        name = (indexer.attrib.get("name") or idx).strip()
        if idx:
            items.append({"id": idx, "name": name})
    if items:
        return items
    for item in root.findall(".//item"):
        idx = (item.findtext("id") or item.attrib.get("id") or "").strip()
        name = (item.findtext("title") or item.findtext("name") or idx).strip()
        if idx:
            items.append({"id": idx, "name": name})
    return items


def _filename_from_response(url: str, content_disposition: str) -> str:
    msg = Message()
    if content_disposition:
        msg["content-disposition"] = content_disposition
        filename = msg.get_filename()
        if filename:
            return filename
    path = PurePosixPath(urlparse(url).path or "")
    name = path.name or "download.torrent"
    return name if name.lower().endswith(".torrent") else f"{name}.torrent"


def _resolve_torrent_download_url(url: str) -> str:
    cfg = _cfg()
    base_url = ((cfg.jackett_url or "").strip().rstrip("/") if cfg else "")
    api_key = ((cfg.jackett_api_key or "").strip() if cfg else "")
    candidate = (url or "").strip()
    if not candidate:
        return ""

    if base_url and candidate.startswith("/"):
        candidate = urljoin(f"{base_url}/", candidate.lstrip("/"))

    parsed = urlsplit(candidate)
    if not parsed.scheme and base_url:
        candidate = urljoin(f"{base_url}/", candidate)
        parsed = urlsplit(candidate)

    if base_url and api_key:
        base_parts = urlsplit(base_url)
        same_host = (
            parsed.scheme.lower() == base_parts.scheme.lower()
            and parsed.netloc.lower() == base_parts.netloc.lower()
        )
        if same_host:
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if "apikey" not in {k.lower() for k in query.keys()}:
                query["apikey"] = api_key
                candidate = urlunsplit((
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    urlencode(query, doseq=True),
                    parsed.fragment,
                ))
    return candidate


async def search(
    query: str,
    category: int = CATEGORY_ALL,
    trackers: Optional[List[str]] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg:
        return {"results": [], "total": 0, "query": query, "error": "Config unavailable"}
    if not cfg.jackett_enabled:
        return {"results": [], "total": 0, "query": query, "error": "Jackett is disabled"}

    url = (cfg.jackett_url or "").rstrip("/")
    api_key = (cfg.jackett_api_key or "").strip()
    if not url or not api_key:
        return {"results": [], "total": 0, "query": query, "error": "Jackett URL or API key not configured"}

    endpoint = f"{url}/api/v2.0/indexers/all/results"
    params = _build_result_params(query, category, trackers or [], limit, api_key)

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data = await _get_json(session, endpoint, params)
    except aiohttp.ClientConnectorError as exc:
        logger.warning("Jackett: connection refused — %s", exc)
        return {"results": [], "total": 0, "query": query, "error": "Jackett not reachable — check URL and port"}
    except PermissionError as exc:
        logger.warning("Jackett: %s", exc)
        return {"results": [], "total": 0, "query": query, "error": str(exc)}
    except RuntimeError as exc:
        logger.warning("Jackett: %s", exc)
        return {"results": [], "total": 0, "query": query, "error": str(exc)}
    except Exception as exc:
        logger.error("Jackett: unexpected error during search: %s", exc)
        return {"results": [], "total": 0, "query": query, "error": str(exc)}

    raw_results = data.get("Results") or []
    normalised = [_normalise_result(r) for r in raw_results]
    await _fill_missing_hashes_from_torrent_files(normalised)
    normalised.sort(key=lambda r: r["seeders"], reverse=True)
    logger.info("Jackett search %r → %d result(s)", query, len(normalised))
    return {"results": normalised, "total": len(normalised), "query": query, "error": None}


async def test_connection() -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg:
        return {"ok": False, "error": "Config unavailable"}

    url = (cfg.jackett_url or "").rstrip("/")
    api_key = (cfg.jackett_api_key or "").strip()
    if not url:
        return {"ok": False, "error": "Jackett URL not configured"}
    if not api_key:
        return {"ok": False, "error": "Jackett API key not configured"}

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                data = await _get_json(session, f"{url}/api/v2.0/server/config", {"apikey": api_key})
                version = data.get("app_version") or data.get("version") or "?"
                return {"ok": True, "version": version}
            except PermissionError:
                return {"ok": False, "error": "Invalid API key"}
            except Exception:
                pass

            try:
                data = await _get_json(session, f"{url}/api/v2.0/indexers", {"apikey": api_key, "configured": "true"})
                if isinstance(data, list):
                    return {"ok": True, "version": f"reachable ({len(data)} indexers)"}
            except PermissionError:
                return {"ok": False, "error": "Invalid API key"}
            except Exception:
                pass

            try:
                xml_text = await _get_text(
                    session,
                    f"{url}/api/v2.0/indexers/all/results/torznab/api",
                    {"apikey": api_key, "t": "indexers", "configured": "true"},
                )
                indexers = _parse_torznab_indexers(xml_text)
                if indexers:
                    return {"ok": True, "version": f"reachable ({len(indexers)} indexers)"}
            except PermissionError:
                return {"ok": False, "error": "Invalid API key"}
            except Exception:
                pass

            try:
                await _get_json(
                    session,
                    f"{url}/api/v2.0/indexers/all/results",
                    _build_result_params("__healthcheck__", CATEGORY_ALL, [], 1, api_key),
                )
                return {"ok": True, "version": "reachable"}
            except PermissionError:
                return {"ok": False, "error": "Invalid API key"}
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
    except aiohttp.ClientConnectorError:
        return {"ok": False, "error": "Jackett not reachable"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "Jackett test failed"}


async def get_indexers() -> List[Dict[str, str]]:
    cfg = _cfg()
    if not cfg:
        return []
    url = (cfg.jackett_url or "").rstrip("/")
    api_key = (cfg.jackett_api_key or "").strip()
    if not url or not api_key:
        return []

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                data = await _get_json(session, f"{url}/api/v2.0/indexers", {"apikey": api_key, "configured": "true"})
                items = [
                    {"id": str(item.get("id", "")).strip(), "name": str(item.get("name", "")).strip()}
                    for item in (data or [])
                    if str(item.get("id", "")).strip()
                ]
                if items:
                    return items
            except Exception:
                pass

            try:
                xml_text = await _get_text(
                    session,
                    f"{url}/api/v2.0/indexers/all/results/torznab/api",
                    {"apikey": api_key, "t": "indexers", "configured": "true"},
                )
                return _parse_torznab_indexers(xml_text)
            except Exception:
                return []
    except Exception:
        return []


async def download_torrent_file(url: str) -> Dict[str, Any]:
    resolved_url = _resolve_torrent_download_url(url)
    if not resolved_url:
        raise RuntimeError("Jackett torrent URL is empty")
    cached = _get_cached_torrent_payload(resolved_url)
    if cached:
        return cached
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(resolved_url, allow_redirects=True) as resp:
            body = await resp.read()
            preview = body[:400].decode("utf-8", errors="ignore").strip()
            content_type = (resp.headers.get("Content-Type", "") or "").lower()
            looks_like_html = preview.startswith("<!doctype") or preview.startswith("<html") or "<form" in preview[:400].lower()
            if resp.status != 200:
                if looks_like_html or "text/html" in content_type:
                    raise RuntimeError("Tracker download requires a valid Jackett login/session for this indexer")
                raise RuntimeError(f"Jackett torrent URL returned HTTP {resp.status}: {preview[:200]}")
            data = body
            if not data:
                raise RuntimeError("Jackett returned an empty torrent file")
            if looks_like_html or "text/html" in content_type:
                raise RuntimeError("Tracker download returned an HTML login page instead of a torrent file")
            filename = _filename_from_response(resolved_url, resp.headers.get("Content-Disposition", ""))
            payload = {
                "filename": filename,
                "content": data,
                "infohash": _extract_torrent_infohash(data),
            }
            _store_cached_torrent_payload(resolved_url, payload)
            return payload


async def send_jackett_webhook(
    *,
    title: str,
    indexer: str,
    size_bytes: int,
    magnet: str,
    alldebrid_id: str = "",
) -> None:
    """
    Fire a webhook for jackett_torrent_added.
    Uses jackett_webhook_url if set, otherwise falls back to discord_webhook_url.
    Respects discord_notify_added — if False and no dedicated Jackett webhook, skip.
    """
    cfg = _cfg()
    if not cfg:
        return

    jackett_hook = (cfg.jackett_webhook_url or "").strip()
    default_hook = (cfg.discord_webhook_url or "").strip()

    if jackett_hook:
        webhook_url = jackett_hook
    elif cfg.discord_notify_added and default_hook:
        webhook_url = default_hook
    else:
        return

    from services.notifications import NotificationService, _now_utc, COLOR_ADDED

    try:
        sz_human = _fmt_size(size_bytes)
    except Exception:
        sz_human = "—"

    svc = NotificationService(webhook_url=webhook_url)
    fields = [
        {"name": "Source", "value": "🔍 Jackett Search", "inline": True},
        {"name": "Indexer", "value": indexer or "—", "inline": True},
        {"name": "Size", "value": sz_human, "inline": True},
        {"name": "Time", "value": _now_utc(), "inline": True},
    ]
    if alldebrid_id:
        fields.append({"name": "AllDebrid ID", "value": str(alldebrid_id), "inline": True})

    sent = await svc._send(
        url=webhook_url,
        title="📥 Torrent Added via Jackett",
        description=f"**{title}**",
        color=COLOR_ADDED,
        fields=fields,
        bypass_dedup=True,
    )
    if sent:
        logger.info("Jackett webhook sent for %r", title[:60])
    else:
        logger.warning("Jackett webhook not sent (HTTP error or rate limited) for %r", title[:60])
