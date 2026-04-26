"""
Jackett integration service.

Proxies torrent-search requests to a Jackett instance and normalises
the results into a consistent format for the frontend.

Jackett API endpoint used:
  GET /api/v2.0/indexers/all/results
    ?apikey=<key>&Query=<term>[&Category[]=<int>][&Tracker[]=<id>]

Docs: https://github.com/Jackett/Jackett#jackett-api
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.jackett")

# Jackett category IDs (Torznab standard)
CATEGORY_ALL   = 0
CATEGORIES: Dict[str, int] = {
    "All":          0,
    "Movies":       2000,
    "TV":           5000,
    "Music":        3000,
    "Books":        7000,
    "Games":        1000,
    "Software":     4000,
    "XXX":          6000,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg():
    try:
        from core.config import get_settings
        return get_settings()
    except Exception:
        return None


def _fmt_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if not size_bytes or size_bytes < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _normalise_result(item: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a single Jackett result into a stable frontend-friendly dict."""
    magnet   = (item.get("MagnetUri") or "").strip()
    torrent  = (item.get("Link")       or "").strip()
    size_b   = int(item.get("Size") or 0)
    seeders  = int(item.get("Seeders")  or 0)
    leechers = int(item.get("Peers")    or item.get("Leechers") or 0)

    pub_date = ""
    raw_date = item.get("PublishDate") or ""
    if raw_date:
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            pub_date = dt.strftime("%Y-%m-%d")
        except Exception:
            pub_date = str(raw_date)[:10]

    return {
        "title":      (item.get("Title") or "").strip(),
        "indexer":    (item.get("Tracker") or item.get("TrackerId") or "").strip(),
        "category":   (item.get("CategoryDesc") or "").strip(),
        "size_bytes": size_b,
        "size_human": _fmt_size(size_b),
        "seeders":    seeders,
        "leechers":   leechers,
        "pub_date":   pub_date,
        "magnet":     magnet,
        "torrent_url": torrent,
        # Prefer magnet; fall back to torrent URL
        "has_link": bool(magnet or torrent),
    }


# ── Service functions ─────────────────────────────────────────────────────────

async def search(
    query:    str,
    category: int = CATEGORY_ALL,
    tracker:  str = "",
    limit:    int = 100,
) -> Dict[str, Any]:
    """
    Search Jackett and return normalised results.

    Returns:
        {
            "results": [...],
            "total":   int,
            "query":   str,
            "error":   str | None,
        }
    """
    cfg = _cfg()
    if not cfg:
        return {"results": [], "total": 0, "query": query, "error": "Config unavailable"}

    if not cfg.jackett_enabled:
        return {"results": [], "total": 0, "query": query, "error": "Jackett is disabled"}

    url     = (cfg.jackett_url or "").rstrip("/")
    api_key = (cfg.jackett_api_key or "").strip()
    if not url or not api_key:
        return {"results": [], "total": 0, "query": query,
                "error": "Jackett URL or API key not configured"}

    params: Dict[str, Any] = {
        "apikey": api_key,
        "Query":  query.strip(),
        "limit":  limit,
    }
    if category and category != CATEGORY_ALL:
        params["Category[]"] = category
    if tracker:
        params["Tracker[]"] = tracker

    endpoint = f"{url}/api/v2.0/indexers/all/results"

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(endpoint, params=params) as resp:
                if resp.status == 401:
                    logger.warning("Jackett: invalid API key")
                    return {"results": [], "total": 0, "query": query,
                            "error": "Invalid Jackett API key"}
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Jackett: HTTP %d — %s", resp.status, body[:200])
                    return {"results": [], "total": 0, "query": query,
                            "error": f"Jackett returned HTTP {resp.status}"}
                data = await resp.json(content_type=None)

    except aiohttp.ClientConnectorError as exc:
        logger.warning("Jackett: connection refused — %s", exc)
        return {"results": [], "total": 0, "query": query,
                "error": "Jackett not reachable — check URL and port"}
    except Exception as exc:
        logger.error("Jackett: unexpected error during search: %s", exc)
        return {"results": [], "total": 0, "query": query, "error": str(exc)}

    raw_results = data.get("Results") or []
    normalised  = [_normalise_result(r) for r in raw_results]
    # Sort: seeders desc, filter out results without any link
    normalised.sort(key=lambda r: r["seeders"], reverse=True)

    logger.info("Jackett search %r → %d result(s)", query, len(normalised))
    return {"results": normalised, "total": len(normalised), "query": query, "error": None}


async def test_connection() -> Dict[str, Any]:
    """Ping Jackett and return status info."""
    cfg = _cfg()
    if not cfg:
        return {"ok": False, "error": "Config unavailable"}

    url     = (cfg.jackett_url or "").rstrip("/")
    api_key = (cfg.jackett_api_key or "").strip()
    if not url:
        return {"ok": False, "error": "Jackett URL not configured"}
    if not api_key:
        return {"ok": False, "error": "Jackett API key not configured"}

    endpoint = f"{url}/api/v2.0/server/config"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                endpoint, params={"apikey": api_key}
            ) as resp:
                if resp.status == 401:
                    return {"ok": False, "error": "Invalid API key"}
                if resp.status != 200:
                    return {"ok": False, "error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
                version = data.get("app_version") or data.get("version") or "?"
                return {"ok": True, "version": version}
    except aiohttp.ClientConnectorError:
        return {"ok": False, "error": "Jackett not reachable"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def get_indexers() -> List[Dict[str, str]]:
    """Return list of configured Jackett indexers (id + name)."""
    cfg = _cfg()
    if not cfg:
        return []
    url     = (cfg.jackett_url or "").rstrip("/")
    api_key = (cfg.jackett_api_key or "").strip()
    if not url or not api_key:
        return []
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{url}/api/v2.0/indexers",
                params={"apikey": api_key, "configured": "true"},
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                return [
                    {"id": item.get("id", ""), "name": item.get("name", "")}
                    for item in (data or [])
                    if item.get("id")
                ]
    except Exception:
        return []


# ── Webhook ───────────────────────────────────────────────────────────────────

async def send_jackett_webhook(
    *,
    title:       str,
    indexer:     str,
    size_bytes:  int,
    magnet:      str,
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
    default_hook = (cfg.discord_webhook_url  or "").strip()

    # Determine which URL to use
    if jackett_hook:
        webhook_url = jackett_hook
    elif cfg.discord_notify_added and default_hook:
        webhook_url = default_hook
    else:
        return  # nothing to send

    from services.notifications import NotificationService, _now_utc, COLOR_ADDED

    try:
        sz_human = _fmt_size(size_bytes)
    except Exception:
        sz_human = "—"

    svc = NotificationService(webhook_url=webhook_url)
    fields = [
        {"name": "Source",   "value": "🔍 Jackett Search", "inline": True},
        {"name": "Indexer",  "value": indexer or "—",       "inline": True},
        {"name": "Size",     "value": sz_human,              "inline": True},
        {"name": "Time",     "value": _now_utc(),            "inline": True},
    ]
    if alldebrid_id:
        fields.append({"name": "AllDebrid ID", "value": str(alldebrid_id), "inline": True})

    await svc._send(
        url=webhook_url,
        title="📥 Torrent Added via Jackett",
        description=f"**{title}**",
        color=COLOR_ADDED,
        fields=fields,
    )
    logger.info("Jackett webhook sent for %r", title[:60])
