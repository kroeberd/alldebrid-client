"""
Discord webhook notifications with rich embeds.

Features:
- Structured embeds with fields instead of raw text
- Clear color coding per event type
- Separate webhook URL for torrent-added events
- Deduplication: same message within 30s is suppressed (keyed on url+title+description)
- Rate limiting: minimum 2s between messages per URL
- Discord 429 handling with retry_after
- Lazy asyncio.Lock (compatible with IsolatedAsyncioTestCase)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
from core.version import read_version

logger = logging.getLogger("alldebrid.notify")

APP_NAME    = "AllDebrid-Client"
APP_VERSION = read_version()
REPO_URL    = "https://github.com/kroeberd/alldebrid-client"
# Default logo — overridden at runtime by discord_avatar_url from settings
# No SVG default — Discord only accepts PNG/JPG/WEBP for avatar_url


def _get_discord_identity() -> tuple[str, str]:
    """Returns (username, avatar_url) from settings.
    Returns empty string for avatar if not set, if a data URI is stored,
    or if a SVG URL is stored (Discord only accepts PNG/JPG/WEBP).
    """
    try:
        from core.config import get_settings
        cfg = get_settings()
        name   = (getattr(cfg, "discord_username",   "") or APP_NAME).strip() or APP_NAME
        avatar = (getattr(cfg, "discord_avatar_url", "") or "").strip()
        # Discord only accepts PNG/JPG/WEBP — reject data URIs and SVG
        if not avatar or avatar.startswith("data:") or avatar.lower().endswith(".svg"):
            avatar = ""
        return name, avatar
    except Exception:
        return APP_NAME, ""

def _is_discord_url(url: str) -> bool:
    """Returns True if the URL looks like a Discord webhook."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return "discord" in host or "discordapp" in host
    except Exception:
        return False


# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_INFO    = 0x3B82F6   # Blue    — general
COLOR_SUCCESS = 0x22C55E   # Green   — completed
COLOR_WARNING = 0xF59E0B   # Yellow  — warning / partial
COLOR_ERROR   = 0xEF4444   # Red     — error
COLOR_ADDED   = 0x8B5CF6   # Purple  — torrent added
COLOR_PARTIAL = 0xF97316   # Orange  — filtered files

# ── Throttling ────────────────────────────────────────────────────────────────
_RATE_LIMIT_SECONDS   = 2.0
_DEDUP_WINDOW_SECONDS = 10.0  # Reduced from 30s — prevents suppressing different torrents with same name


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _now_utc() -> str:
    """Human-readable UTC time for field values."""
    return datetime.now(timezone.utc).strftime("%d.%m.%Y, %H:%M UTC")


def _discord_timestamp() -> str:
    """ISO 8601 for Discord embed timestamp field — renders in user's local timezone."""
    return datetime.now(timezone.utc).isoformat()


def _source_label(source: str) -> str:
    """Maps internal source identifiers to readable labels."""
    return {
        "manual":             "Manual (UI)",
        "watch_file":         "Watch folder (.magnet)",
        "watch_torrent":      "Watch folder (.torrent)",
        "alldebrid_existing": "AllDebrid import",
        "api":                "API",
    }.get(source, source)


class NotificationService:
    # Class-level state — shared across all instances
    _last_sent_at: Dict[str, float] = {}
    _sent_hashes:  Dict[str, float] = {}
    _throttle_lock: Optional[asyncio.Lock] = None  # lazy — created per event loop

    def __init__(self, webhook_url: str = "", added_webhook_url: str = ""):
        self.webhook_url       = (webhook_url or "").strip()
        # Separate channel for added events; falls back to main webhook if not set
        self.added_webhook_url = (added_webhook_url or "").strip() or self.webhook_url

    # ── Lock (lazy, safe for test isolation) ─────────────────────────────────

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._throttle_lock is None:
            cls._throttle_lock = asyncio.Lock()
        return cls._throttle_lock

    # ── Public event methods ──────────────────────────────────────────────────

    async def send_added(
        self,
        name: str,
        source: str = "manual",
        alldebrid_id: str = "",
        extra_fields: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Torrent successfully uploaded to AllDebrid."""
        if not self.added_webhook_url:
            return
        fields: List[Dict[str, Any]] = [
            {"name": "Source",  "value": _source_label(source),      "inline": True},
            {"name": "Status",  "value": "Queued on AllDebrid",       "inline": True},
            {"name": "Time",    "value": _now_utc(),                  "inline": True},
        ]
        if alldebrid_id:
            fields.append({"name": "AllDebrid ID", "value": str(alldebrid_id), "inline": True})
        if extra_fields:
            fields.extend(extra_fields)
        await self._send(
            url=self.added_webhook_url,
            title="📥 Torrent Added",
            description=f"**{name}**",
            color=COLOR_ADDED,
            fields=fields,
        )

    async def send_complete(
        self,
        name: str,
        file_count: int = 0,
        size_bytes: int = 0,
        destination: str = "",
        download_client: str = "aria2",
    ) -> None:
        """Download fully completed."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, Any]] = []
        if file_count:
            fields.append({"name": "Files",       "value": str(file_count),       "inline": True})
        if size_bytes:
            fields.append({"name": "Size",        "value": _fmt_bytes(size_bytes), "inline": True})
        if download_client:
            fields.append({"name": "Client",      "value": download_client,        "inline": True})
        if destination:
            fields.append({"name": "Destination", "value": f"`{destination}`",     "inline": False})
        fields.append(    {"name": "Time",        "value": _now_utc(),              "inline": True})
        await self._send(
            url=self.webhook_url,
            title="✅ Download Complete",
            description=f"**{name}**",
            color=COLOR_SUCCESS,
            fields=fields,
        )

    async def send_error(
        self,
        name: str,
        reason: str = "",
        context: str = "",
        source: str = "",
        provider: str = "",
        alldebrid_id: str = "",
        status_code: str = "",
    ) -> None:
        """Error during processing or download."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, Any]] = []
        if source:
            fields.append({"name": "Source", "value": source[:200], "inline": True})
        if provider:
            fields.append({"name": "Provider", "value": provider[:200], "inline": True})
        if alldebrid_id:
            fields.append({"name": "AllDebrid ID", "value": str(alldebrid_id)[:200], "inline": True})
        if status_code:
            fields.append({"name": "Status Code", "value": str(status_code)[:200], "inline": True})
        if reason:
            fields.append({"name": "Reason",  "value": reason[:1000],  "inline": False})
        if context:
            fields.append({"name": "Context", "value": context[:500],   "inline": False})
        fields.append(    {"name": "Time",    "value": _now_utc(),       "inline": True})
        await self._send(
            url=self.webhook_url,
            title="❌ Error",
            description=f"**{name}**",
            color=COLOR_ERROR,
            fields=fields,
        )

    async def send_partial(
        self,
        name: str,
        total_files: int,
        downloaded_files: int,
        blocked_files: int,
        total_size: int = 0,
        downloaded_size: int = 0,
    ) -> None:
        """Partial download — some files were blocked by filters."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, Any]] = [
            {"name": "Total",        "value": str(total_files),          "inline": True},
            {"name": "Downloaded",   "value": str(downloaded_files),     "inline": True},
            {"name": "Filtered",     "value": str(blocked_files),        "inline": True},
        ]
        if total_size:
            fields.append({"name": "Total Size",      "value": _fmt_bytes(total_size),       "inline": True})
        if downloaded_size:
            fields.append({"name": "Downloaded Size", "value": _fmt_bytes(downloaded_size),  "inline": True})
        fields.append(    {"name": "Time",            "value": _now_utc(),                   "inline": True})
        await self._send(
            url=self.webhook_url,
            title="⚠️ Partial Download",
            description=f"**{name}**\nSome files were filtered",
            color=COLOR_PARTIAL,
            fields=fields,
        )

    async def send(self, title: str, description: str, color: int = COLOR_INFO) -> None:
        """Backward-compatible simple message without fields."""
        if not self.webhook_url:
            return
        await self._send(
            url=self.webhook_url,
            title=title,
            description=description,
            color=color,
        )

    async def send_update(
        self,
        current_version: str,
        latest_version: str,
        release_url: str = "",
        release_notes: str = "",
    ) -> None:
        """Notify that a new AllDebrid-Client version is available."""
        cfg = get_settings()
        if not getattr(cfg, "discord_notify_update", True):
            return
        url = self.webhook_url
        if not url:
            return False
        desc = (
            "**AllDebrid-Client " + latest_version + "** is available.\n"
            "You are running **" + current_version + "**."
        )
        fields: List[Dict[str, Any]] = []
        if release_url:
            fields.append({"name": "Release", "value": "[View on GitHub](" + release_url + ")", "inline": False})
        if release_notes:
            notes = release_notes[:900] + ("…" if len(release_notes) > 900 else "")
            fields.append({"name": "Release Notes", "value": notes, "inline": False})
        await self._send(
            url=url,
            title="📦 New version available",
            description=desc,
            color=COLOR_INFO,
            fields=fields or None,
        )

    async def test(self) -> bool:
        """Sends a test message. Returns True if actually sent to Discord."""
        if not self.webhook_url:
            return False
        return await self._send(
            url=self.webhook_url,
            title="🔔 Test Notification",
            description=f"**{APP_NAME}** is connected and ready.",
            color=COLOR_INFO,
            fields=[
                {"name": "Version", "value": APP_VERSION, "inline": True},
                {"name": "Time",    "value": _now_utc(),  "inline": True},
            ],
            bypass_dedup=True,  # Test button should always reach Discord
        )

    # ── Internal implementation ───────────────────────────────────────────────

    async def _send(
        self,
        url: str,
        title: str,
        description: str,
        color: int = COLOR_INFO,
        fields: Optional[List[Dict[str, Any]]] = None,
        bypass_dedup: bool = False,
    ) -> bool:
        """Send a Discord embed. Returns True if sent, False if deduplicated or failed."""
        if not url:
            return

        # Deduplication: same content within 30s → skip
        # Key includes description to avoid suppressing different torrents with same event type
        dedup_key = hashlib.md5(
            f"{url}|{title}|{description[:200]}".encode()
        ).hexdigest()

        async with self._get_lock():
            now = time.monotonic()

            last_hash = self._sent_hashes.get(dedup_key, 0.0)
            if not bypass_dedup and now - last_hash < _DEDUP_WINDOW_SECONDS:
                logger.debug("Discord: duplicate suppressed (%s)", title)
                return False

            # Rate limiting
            wait = max(0.0, _RATE_LIMIT_SECONDS - (now - self._last_sent_at.get(url, 0.0)))
            if wait > 0:
                await asyncio.sleep(wait)

            self._last_sent_at[url] = time.monotonic()
            self._sent_hashes[dedup_key] = time.monotonic()

            # Clean up old entries (> 5 minutes)
            cutoff = time.monotonic() - 300
            self._sent_hashes = {k: v for k, v in self._sent_hashes.items() if v > cutoff}

        _bot_name, _bot_avatar = _get_discord_identity()
        embed: Dict[str, Any] = {
            "title":       title[:256],
            "description": description[:4096],
            "color":       color,
            "timestamp":   _discord_timestamp(),
            "url":         REPO_URL,
            "author": {
                "name": APP_NAME,
                "url": REPO_URL,
            },
            "footer": {
                "text":     f"{APP_NAME} v{APP_VERSION}",
                "icon_url": _bot_avatar,
            },
        }
        if fields:
            embed["fields"] = [
                {
                    "name":   f.get("name", "")[:256],
                    "value":  f.get("value", "—")[:1024],
                    "inline": bool(f.get("inline", True)),
                }
                for f in fields[:25]
            ]

        if _is_discord_url(url):
            payload = {
                "username": _bot_name,
                "embeds":   [embed],
            }
            if _bot_avatar:
                payload["avatar_url"] = _bot_avatar
        else:
            # Generic webhook: send a simple flat JSON payload
            payload = {
                "event":       embed.get("title", ""),
                "event_key":   title.lower().replace(" ", "_"),
                "severity":    (
                    "error" if color == COLOR_ERROR else
                    "warning" if color in (COLOR_WARNING, COLOR_PARTIAL) else
                    "success" if color in (COLOR_SUCCESS, COLOR_ADDED) else
                    "info"
                ),
                "app": {
                    "name": APP_NAME,
                    "version": APP_VERSION,
                    "repository": REPO_URL,
                },
                "description": embed.get("description", ""),
                "color":       embed.get("color", 0),
                "fields":      {f["name"]: f["value"] for f in (embed.get("fields") or [])},
                "timestamp":   embed.get("timestamp", ""),
                "embed":       embed,
            }

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 429:
                        data = await resp.json(content_type=None)
                        retry_after = float(data.get("retry_after", 5))
                        logger.warning("Discord rate limit — waiting %.1fs", retry_after)
                        await asyncio.sleep(retry_after)
                        async with session.post(url, json=payload) as resp2:
                            if resp2.status not in (200, 204):
                                body2 = await resp2.text()
                                raise Exception(f"Discord webhook {resp2.status} after retry: {body2[:200]}")
                    elif resp.status not in (200, 204):
                        body = await resp.text()
                        raise Exception(f"Discord webhook {resp.status}: {body[:200]}")
            logger.debug("Discord notification sent: %s", title[:60])
            return True
        except Exception as exc:
            detail = str(exc).strip() or repr(exc)
            logger.error(
                "Discord notification failed (%s) [%s]: %s",
                title[:60],
                exc.__class__.__name__,
                detail,
            )
            return False
