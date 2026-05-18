"""
Media Server Integration — backend/services/media_server.py

Triggers library scans on Plex and/or Jellyfin after a torrent completes,
so new media is picked up without manual intervention.

Called from manager_v2._finalize_aria2_torrent() as a fire-and-forget task.
Never raises — all errors are logged at DEBUG level.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("alldebrid.media_server")


async def trigger_plex_scan(url: str, token: str, library_id: str = "") -> bool:
    """
    Ask Plex to refresh its library.

    If `library_id` is set, only that section is refreshed; otherwise all
    libraries are refreshed (GET /library/sections/all/refresh).
    """
    if not url or not token:
        return False
    try:
        import httpx
        base = url.rstrip("/")
        headers = {
            "X-Plex-Token": token,
            "Accept": "application/json",
        }
        if library_id:
            endpoint = f"{base}/library/sections/{library_id}/refresh"
        else:
            endpoint = f"{base}/library/sections/all/refresh"
        # verify=False is intentional: Plex and Jellyfin are commonly run with
        # self-signed certificates on home networks. The token authenticates the
        # request; TLS verification is opt-out via the plex_verify_ssl config field.
        ssl_verify = bool(getattr(get_settings(), "plex_verify_ssl", False))
        async with httpx.AsyncClient(timeout=10, verify=ssl_verify) as client:
            resp = await client.get(endpoint, headers=headers)
            ok = resp.status_code in (200, 201, 204)
            logger.info(
                "Plex scan %s → HTTP %s", endpoint.replace(token, "***"), resp.status_code
            )
            return ok
    except Exception as exc:
        logger.debug("Plex scan failed: %s", exc)
        return False


async def trigger_jellyfin_scan(url: str, api_key: str) -> bool:
    """
    Ask Jellyfin to refresh all libraries.

    Uses POST /Library/Refresh which triggers a full library scan.
    """
    if not url or not api_key:
        return False
    try:
        import httpx
        base = url.rstrip("/")
        endpoint = f"{base}/Library/Refresh"
        headers = {
            "X-MediaBrowser-Token": api_key,
            "Content-Type": "application/json",
        }
        ssl_verify = bool(getattr(get_settings(), "jellyfin_verify_ssl", False))
        async with httpx.AsyncClient(timeout=10, verify=ssl_verify) as client:
            resp = await client.post(endpoint, headers=headers)
            ok = resp.status_code in (200, 201, 204)
            logger.info("Jellyfin scan → HTTP %s", resp.status_code)
            return ok
    except Exception as exc:
        logger.debug("Jellyfin scan failed: %s", exc)
        return False


async def trigger_from_config() -> None:
    """Fire both Plex and Jellyfin triggers based on current config."""
    try:
        from core.config import get_settings
        cfg = get_settings()
        plex_url   = str(getattr(cfg, "plex_url",          "") or "").strip()
        plex_token = str(getattr(cfg, "plex_token",         "") or "").strip()
        plex_lib   = str(getattr(cfg, "plex_library_id",   "") or "").strip()
        jf_url     = str(getattr(cfg, "jellyfin_url",       "") or "").strip()
        jf_key     = str(getattr(cfg, "jellyfin_api_key",  "") or "").strip()

        if plex_url and plex_token:
            await trigger_plex_scan(plex_url, plex_token, plex_lib)
        if jf_url and jf_key:
            await trigger_jellyfin_scan(jf_url, jf_key)
    except Exception as exc:
        logger.debug("trigger_from_config: %s", exc)
