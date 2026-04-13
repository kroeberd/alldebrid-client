import aiohttp
import asyncio
import logging
import time

logger = logging.getLogger("alldebrid.notify")


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
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": "AllDebrid-Client"},
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
            await self.send("🔔 Test Notification", "AllDebrid-Client is connected!", 0x9b59b6)
            return True
        except Exception:
            return False
