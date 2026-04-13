import aiohttp
import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger("alldebrid.notify")

APP_NAME = "AllDebrid-Client"
VERSION_PATH = Path(__file__).resolve().parents[2] / "VERSION"
APP_VERSION = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "dev"
APP_LOGO_URL = "https://raw.githubusercontent.com/kroeberd/alldebrid-client/main/docs/logo.svg"


class NotificationService:
    _last_sent_at: dict[str, float] = {}
    _throttle_lock = asyncio.Lock()

    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url

    async def _respect_rate_limit(self):
        if not self.webhook_url:
            return

        async with self._throttle_lock:
            now = time.monotonic()
            last_sent = self._last_sent_at.get(self.webhook_url, 0.0)
            wait_for = max(0.0, 5.0 - (now - last_sent))
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_sent_at[self.webhook_url] = time.monotonic()

    async def send(self, title: str, description: str, color: int = 0x3498db):
        if not self.webhook_url:
            return
        await self._respect_rate_limit()
        payload = {
            "username": APP_NAME,
            "avatar_url": APP_LOGO_URL,
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "author": {"name": APP_NAME, "icon_url": APP_LOGO_URL},
                "thumbnail": {"url": APP_LOGO_URL},
                "footer": {"text": f"{APP_NAME} v{APP_VERSION}"},
            }]
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(self.webhook_url, json=payload) as resp:
                    if resp.status not in (200, 204):
                        logger.warning(f"Discord webhook returned {resp.status}")
        except Exception as e:
            logger.error(f"Discord notification failed: {e}")

    async def test(self) -> bool:
        try:
            await self.send("Test Notification", f"{APP_NAME} is connected and ready.", 0x5865F2)
            return True
        except Exception:
            return False
