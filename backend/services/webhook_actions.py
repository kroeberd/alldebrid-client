"""
Generic Webhook Action System — backend/services/webhook_actions.py

Fires HTTP POST webhooks on torrent lifecycle events:
  - on_added:    torrent accepted into the queue
  - on_complete: all files downloaded successfully
  - on_error:    torrent reached error state

Each webhook receives a JSON payload with full torrent context. An optional
Bearer secret can be set to authenticate requests.

This is separate from Discord notifications (which use the AllDebrid-Client
notification service) and the on_torrent_complete shell script.

Design principles:
  - Fire-and-forget: webhook failures are logged but never raise
  - Safe to call from any async context
  - No retries (keeps things simple; the caller should handle failures)
  - Sanitises URLs before logging (no credential leakage)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("alldebrid.webhook")

# Events
EVENT_ADDED    = "torrent.added"
EVENT_COMPLETE = "torrent.completed"
EVENT_ERROR    = "torrent.error"


async def fire(
    event: str,
    torrent: dict,
    *,
    url: Optional[str] = None,
    secret: Optional[str] = None,
) -> bool:
    """
    POST a JSON webhook to *url* (if set) for *event*.

    Returns True on HTTP 2xx, False otherwise (including no-op when url is empty).
    Never raises.
    """
    if not url:
        return False
    try:
        import httpx
        payload: dict[str, Any] = {
            "event":       event,
            "torrent_id":  torrent.get("id"),
            "name":        torrent.get("name", ""),
            "status":      torrent.get("status", ""),
            "source":      torrent.get("source", ""),
            "label":       torrent.get("label", ""),
            "size_bytes":  torrent.get("size_bytes", 0),
            "hash":        torrent.get("hash", ""),
            "alldebrid_id": torrent.get("alldebrid_id", ""),
            "error_message": torrent.get("error_message"),
            "local_path":  torrent.get("local_path"),
        }
        headers = {"Content-Type": "application/json", "User-Agent": "AllDebrid-Client"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            ok = resp.is_success
            log_url = _sanitize_url(url)
            if ok:
                logger.debug("webhook %s → %s HTTP %s", event, log_url, resp.status_code)
            else:
                logger.warning(
                    "webhook %s → %s failed: HTTP %s", event, log_url, resp.status_code
                )
            return ok
    except Exception as exc:
        logger.debug("webhook %s failed: %s", event, exc)
        return False


async def fire_from_config(event: str, torrent: dict) -> None:
    """Fire the appropriate webhook URL from the current config."""
    try:
        from core.config import get_settings
        cfg = get_settings()
        secret = getattr(cfg, "webhook_secret", "") or ""

        url_map = {
            EVENT_ADDED:    getattr(cfg, "webhook_on_added",    "") or "",
            EVENT_COMPLETE: getattr(cfg, "webhook_on_complete", "") or "",
            EVENT_ERROR:    getattr(cfg, "webhook_on_error",    "") or "",
        }
        url = url_map.get(event, "")
        if url:
            await fire(event, torrent, url=url, secret=secret)
    except Exception as exc:
        logger.debug("fire_from_config: %s", exc)


def _sanitize_url(url: str) -> str:
    """Remove credentials from URL for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url)
        safe = p._replace(netloc=f"{p.hostname}:{p.port}" if p.port else str(p.hostname or ""))
        return urlunparse(safe)
    except Exception:
        return url[:40] + "…" if len(url) > 40 else url
