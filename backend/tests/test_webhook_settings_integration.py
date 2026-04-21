import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientTimeout=lambda *a, **kw: None,
        ClientSession=object,
        TCPConnector=lambda **kw: None,
        FormData=object,
        ClientError=Exception,
        ServerDisconnectedError=Exception,
        ClientConnectorError=Exception,
        ClientOSError=Exception,
    )

if "aiofiles" not in sys.modules:
    sys.modules["aiofiles"] = types.SimpleNamespace(open=lambda *a, **kw: None)

if "aiosqlite" not in sys.modules:
    sys.modules["aiosqlite"] = types.SimpleNamespace(
        Connection=object,
        Row=object,
        connect=lambda *a, **kw: None,
    )

if "multipart" not in sys.modules:
    multipart_mod = types.ModuleType("multipart")
    multipart_mod.__version__ = "0.0-test"
    multipart_sub = types.ModuleType("multipart.multipart")
    multipart_sub.parse_options_header = lambda value: ("form-data", {})
    sys.modules["multipart"] = multipart_mod
    sys.modules["multipart.multipart"] = multipart_sub

from api import routes
from core.scheduler import _has_reporting_webhook
from services.stats import send_stats_report


class RouteHelperTests(unittest.TestCase):
    def test_public_base_url_prefers_env_override(self):
        request = SimpleNamespace(
            headers={"host": "internal.local:8080"},
            url=SimpleNamespace(scheme="http"),
        )
        with patch.dict("os.environ", {"PUBLIC_BASE_URL": "https://example.com/base"}, clear=False):
            self.assertEqual(routes._public_base_url(request), "https://example.com/base")

    def test_avatar_reachability_warning_for_private_url(self):
        warning = routes._avatar_reachability_warning("http://127.0.0.1:8080/api/avatar")
        self.assertIn("PUBLIC_BASE_URL", warning)

    def test_avatar_reachability_warning_empty_for_public_url(self):
        warning = routes._avatar_reachability_warning("https://example.com/api/avatar")
        self.assertEqual(warning, "")


class SchedulerWebhookTests(unittest.TestCase):
    def test_reporting_webhook_accepts_discord_fallback(self):
        cfg = SimpleNamespace(
            stats_report_webhook_url="",
            discord_webhook_url="https://discord.com/api/webhooks/test",
        )
        self.assertTrue(_has_reporting_webhook(cfg))

    def test_reporting_webhook_false_when_both_empty(self):
        cfg = SimpleNamespace(stats_report_webhook_url="", discord_webhook_url="")
        self.assertFalse(_has_reporting_webhook(cfg))


class SettingsSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_settings_sanitises_before_save(self):
        saved = {}

        def fake_save(cfg):
            saved["cfg"] = cfg

        def fake_apply(cfg):
            saved["applied"] = cfg

        with patch("api.routes.save_settings", side_effect=fake_save), \
             patch("api.routes.apply_settings", side_effect=fake_apply), \
             patch.object(routes.manager, "reset_services", MagicMock()):
            result = await routes.update_settings(
                routes.AppSettings(discord_avatar_url="data:image/png;base64,abc123")
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(saved["cfg"].discord_avatar_url, "")
        self.assertEqual(saved["applied"].discord_avatar_url, "")


class _FakeResponse:
    def __init__(self, payload_store):
        self.status = 204
        self._payload_store = payload_store

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return ""


class _FakeSession:
    last_json = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json):
        _FakeSession.last_json = {"url": url, "json": json}
        return _FakeResponse(_FakeSession.last_json)


class StatsWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_report_falls_back_to_main_discord_webhook(self):
        summary = {
            "torrents_processed": 5,
            "completed": 4,
            "errors": 1,
            "success_rate": "80%",
            "total_downloaded": "10 GB",
            "avg_duration": "5m 0s",
            "total_files": 7,
            "blocked_files": 0,
            "total_retries": 2,
        }
        cfg = SimpleNamespace(
            stats_report_webhook_url="",
            discord_webhook_url="https://discord.com/api/webhooks/test",
        )
        with patch("services.stats._cfg", return_value=cfg), \
             patch("services.stats.generate_report", AsyncMock(return_value={"report": {"summary": summary}, "raw": {}})), \
             patch("services.notifications._get_discord_identity", return_value=("Webhook Bot", "")), \
             patch("services.stats.aiohttp.ClientSession", _FakeSession):
            result = await send_stats_report(hours=24, triggered_by="manual")

        self.assertTrue(result["ok"])
        self.assertTrue(result["discord"])
        self.assertEqual(_FakeSession.last_json["url"], "https://discord.com/api/webhooks/test")
        self.assertEqual(_FakeSession.last_json["json"]["username"], "Webhook Bot")
        self.assertNotIn("avatar_url", _FakeSession.last_json["json"])


if __name__ == "__main__":
    unittest.main()
