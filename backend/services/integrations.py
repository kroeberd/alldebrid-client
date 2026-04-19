"""
Sonarr and Radarr integration.

After a download completes, triggers a RescanSeries/RescanMovie command
so the media library picks up new files automatically.
"""
import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger("alldebrid.integrations")

TIMEOUT = aiohttp.ClientTimeout(total=15)


async def _trigger(base_url: str, api_key: str, command: str) -> bool:
    """POST a command to Sonarr/Radarr and return True on success."""
    url = base_url.rstrip("/") + "/api/v3/command"
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    payload = {"name": command}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(url, json=payload, headers=headers) as resp:
                ok = resp.status in (200, 201, 202)
                if not ok:
                    body = await resp.text()
                    logger.warning("%s command failed (%d): %s", command, resp.status, body[:200])
                return ok
    except Exception as exc:
        logger.error("%s trigger failed: %s", command, exc)
        return False


async def notify_sonarr(name: str = "") -> bool:
    try:
        from core.config import get_settings
        cfg = get_settings()
        if not cfg.sonarr_enabled or not cfg.sonarr_url or not cfg.sonarr_api_key:
            return False
        logger.info("Notifying Sonarr (RescanSeries) for: %s", name)
        return await _trigger(cfg.sonarr_url, cfg.sonarr_api_key, "RescanSeries")
    except Exception as exc:
        logger.error("Sonarr notify error: %s", exc)
        return False


async def notify_radarr(name: str = "") -> bool:
    try:
        from core.config import get_settings
        cfg = get_settings()
        if not cfg.radarr_enabled or not cfg.radarr_url or not cfg.radarr_api_key:
            return False
        logger.info("Notifying Radarr (RescanMovie) for: %s", name)
        return await _trigger(cfg.radarr_url, cfg.radarr_api_key, "RescanMovie")
    except Exception as exc:
        logger.error("Radarr notify error: %s", exc)
        return False


async def test_connection(base_url: str, api_key: str) -> dict:
    """Tests connectivity to a Sonarr/Radarr instance. Returns status dict."""
    url = base_url.rstrip("/") + "/api/v3/system/status"
    headers = {"X-Api-Key": api_key}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return {
                        "ok": True,
                        "version": data.get("version", "?"),
                        "app_name": data.get("appName", "?"),
                    }
                return {"ok": False, "error": f"HTTP {resp.status}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
