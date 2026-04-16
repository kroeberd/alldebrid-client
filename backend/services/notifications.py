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
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.notify")

APP_NAME    = "AllDebrid-Client"
_VER_PATH   = Path(__file__).resolve().parents[2] / "VERSION"
APP_VERSION = _VER_PATH.read_text(encoding="utf-8").strip() if _VER_PATH.exists() else "dev"
REPO_URL    = "https://github.com/kroeberd/alldebrid-client"
# Default logo — overridden at runtime by discord_avatar_url from settings
_DEFAULT_LOGO = "https://raw.githubusercontent.com/kroeberd/alldebrid-client/main/docs/logo.svg"


def _get_discord_identity() -> tuple[str, str]:
    """Returns (username, avatar_url) from settings, with safe defaults.
    Discord requires an HTTPS/HTTP URL for avatar_url — data URIs are rejected
    with a 400 error. If a data URI is stored (legacy), fall back to the default logo.
    """
    try:
        from core.config import get_settings
        cfg = get_settings()
        name   = (getattr(cfg, "discord_username",   "") or APP_NAME).strip() or APP_NAME
        avatar = (getattr(cfg, "discord_avatar_url", "") or "").strip()
        # Reject data URIs — Discord only accepts real HTTP(S) URLs
        if not avatar or avatar.startswith("data:"):
            avatar = _DEFAULT_LOGO
        return name, avatar
    except Exception:
        return APP_NAME, _DEFAULT_LOGO

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_INFO    = 0x3B82F6   # Blue    — general
COLOR_SUCCESS = 0x22C55E   # Green   — completed
COLOR_WARNING = 0xF59E0B   # Yellow  — warning / partial
COLOR_ERROR   = 0xEF4444   # Red     — error
COLOR_ADDED   = 0x8B5CF6   # Purple  — torrent added
COLOR_PARTIAL = 0xF97316   # Orange  — filtered files

# ── Throttling ────────────────────────────────────────────────────────────────
_RATE_LIMIT_SECONDS   = 2.0
_DEDUP_WINDOW_SECONDS = 30.0


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
    ) -> None:
        """Error during processing or download."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, Any]] = []
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

    async def test(self) -> bool:
        """Sends a test message. Returns True if successful."""
        if not self.webhook_url:
            return False
        try:
            await self._send(
                url=self.webhook_url,
                title="🔔 Test Notification",
                description=f"**{APP_NAME}** is connected and ready.",
                color=COLOR_INFO,
                fields=[
                    {"name": "Version", "value": APP_VERSION, "inline": True},
                    {"name": "Time",    "value": _now_utc(),  "inline": True},
                ],
            )
            return True
        except Exception:
            return False

    # ── Internal implementation ───────────────────────────────────────────────

    async def _send(
        self,
        url: str,
        title: str,
        description: str,
        color: int = COLOR_INFO,
        fields: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
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
            if now - last_hash < _DEDUP_WINDOW_SECONDS:
                logger.debug("Discord: duplicate suppressed (%s)", title)
                return

            # Rate limiting
            wait = max(0.0, _RATE_LIMIT_SECONDS - (now - self._last_sent_at.get(url, 0.0)))
            if wait > 0:
                await asyncio.sleep(wait)

            self._last_sent_at[url] = time.monotonic()
            self._sent_hashes[dedup_key] = time.monotonic()

            # Clean up old entries (> 5 minutes)
            cutoff = time.monotonic() - 300
            self._sent_hashes = {k: v for k, v in self._sent_hashes.items() if v > cutoff}

        embed: Dict[str, Any] = {
            "title":       title[:256],
            "description": description[:4096],
            "color":       color,
            "footer": {
                "text":     f"{APP_NAME} v{APP_VERSION} — {REPO_URL}",
                "icon_url": _DEFAULT_LOGO,
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

        _bot_name, _bot_avatar = _get_discord_identity()
        payload = {
            "username":   _bot_name,
            "avatar_url": _bot_avatar,
            "embeds":     [embed],
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
                                logger.warning("Discord retry status %d", resp2.status)
                    elif resp.status not in (200, 204):
                        body = await resp.text()
                        logger.warning("Discord webhook %d: %s", resp.status, body[:200])
        except Exception as exc:
            logger.error("Discord notification failed: %s", exc)
