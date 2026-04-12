import aiohttp
import logging
from typing import Optional

logger = logging.getLogger("alldebrid.notify")


class NotificationService:
    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url

    async def send(self, title: str, description: str, color: int = 0x3498db):
        if not self.webhook_url:
            return
        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": "AllDebrid-Client"},
            }]
        }
        try:
            async with aiohttp.ClientSession() as s:
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
