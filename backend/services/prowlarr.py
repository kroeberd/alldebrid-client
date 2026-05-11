"""
Prowlarr integration — modern indexer manager for the *arr ecosystem.

Prowlarr is the successor to Jackett and integrates natively with Sonarr,
Radarr, Lidarr etc. This module provides search and indexer listing via the
Prowlarr v1 REST API, returning results in the same normalised format used by
the Jackett integration so the frontend can handle both transparently.

API used:
  GET /api/v1/indexer         — list configured indexers
  GET /api/v1/search          — search all (or selected) indexers
  GET /api/v1/health          — connectivity test

Authentication: X-Api-Key header.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.prowlarr")

TIMEOUT = aiohttp.ClientTimeout(total=30)

# ── Shared result format (same keys as jackett._normalise_result) ─────────────

def _fmt_size(bytes_: Any) -> str:
    if not bytes_:
        return "?"
    b = int(bytes_)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.2f} {unit}"
        b //= 1024
    return f"{b:.2f} PB"


def _normalise(item: dict) -> dict:
    """Map a Prowlarr search result to our common result format."""
    magnet   = (item.get("magnetUrl")    or "").strip()
    torrent  = (item.get("downloadUrl")  or "").strip()
    guid     = (item.get("guid")         or item.get("infoUrl") or "").strip()

    # Try to extract hash from the magnet link
    import re as _re
    hash_val = ""
    if magnet:
        m = _re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet, _re.I)
        if m:
            hash_val = m.group(1).lower()

    size_bytes = int(item.get("size") or 0)

    return {
        "title":        item.get("title") or "",
        "indexer":      item.get("indexer") or "",
        "size_human":   _fmt_size(size_bytes),
        "size_bytes":   size_bytes,
        "seeders":      int(item.get("seeders") or 0),
        "leechers":     int(item.get("leechers") or 0),
        "pub_date":     (item.get("publishDate") or "")[:10],
        "magnet":       magnet,
        "torrent_url":  torrent,
        "hash":         hash_val,
        "guid":         guid,
        "category":     item.get("categories", [{}])[0].get("name", "") if item.get("categories") else "",
        "source":       "prowlarr",
    }


def _cfg():
    from core.config import get_settings
    cfg = get_settings()
    url  = str(getattr(cfg, "prowlarr_url",     "") or "").rstrip("/")
    key  = str(getattr(cfg, "prowlarr_api_key", "") or "")
    return url, key


def _headers(key: str) -> Dict[str, str]:
    return {"X-Api-Key": key, "Accept": "application/json"}


async def _get(path: str, params: Optional[Dict] = None) -> Any:
    url, key = _cfg()
    if not url or not key:
        raise ValueError("Prowlarr URL or API key not configured")
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get(f"{url}{path}", headers=_headers(key), params=params) as resp:
            if resp.status == 401:
                raise PermissionError("Invalid Prowlarr API key (401 Unauthorized)")
            resp.raise_for_status()
            return await resp.json()


# ── Public API ────────────────────────────────────────────────────────────────

async def test_connection() -> dict:
    """Verify connectivity and API key validity."""
    url, key = _cfg()
    if not url or not key:
        return {"ok": False, "error": "Prowlarr URL or API key not configured"}
    try:
        data = await _get("/api/v1/health")
        # Health endpoint returns a list of issues; empty list = healthy
        return {"ok": True, "issues": data if isinstance(data, list) else []}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def get_indexers() -> List[dict]:
    """Return all enabled indexers."""
    data = await _get("/api/v1/indexer")
    return [
        {
            "id":       item.get("id"),
            "name":     item.get("name") or "",
            "protocol": item.get("protocol") or "",
            "privacy":  item.get("privacy") or "",
            "enabled":  item.get("enable", True),
        }
        for item in (data if isinstance(data, list) else [])
    ]


async def search(
    query: str,
    indexer_ids: Optional[List[int]] = None,
    categories: Optional[List[int]] = None,
    limit: int = 100,
) -> List[dict]:
    """Search Prowlarr for torrents matching *query*.

    Args:
        query:       Search term.
        indexer_ids: Optional list of Prowlarr indexer IDs to restrict the search.
                     Empty / None = search all enabled indexers.
        categories:  Optional list of Newznab category IDs (e.g. 2000=Movies, 5000=TV).
        limit:       Maximum number of results to return.
    """
    params: dict = {"query": query, "type": "search", "limit": limit}
    if indexer_ids:
        # Prowlarr accepts repeated indexerIds parameters
        params["indexerIds"] = indexer_ids
    if categories:
        params["categories"] = categories

    try:
        data = await _get("/api/v1/search", params=params)
    except Exception as exc:
        logger.error("Prowlarr search failed: %s", exc)
        raise

    results = data if isinstance(data, list) else []
    return [_normalise(item) for item in results]
