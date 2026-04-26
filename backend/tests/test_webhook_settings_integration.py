import sys
import types
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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

    def test_jackett_title_key_ignores_punctuation_and_extension(self):
        self.assertEqual(
            routes._jackett_title_key("CzechSexCasting.E435.Kathy.Deep.1080p.mp4"),
            routes._jackett_title_key("CzechSexCasting E435 Kathy Deep 1080p.mkv"),
        )


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
             patch.object(routes.manager, "apply_aria2_memory_tuning", AsyncMock(return_value={"ok": True})), \
             patch.object(routes.manager, "reset_services", MagicMock()):
            result = await routes.update_settings(
                routes.AppSettings(discord_avatar_url="data:image/png;base64,abc123")
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(saved["cfg"].discord_avatar_url, "")
        self.assertEqual(saved["applied"].discord_avatar_url, "")

    async def test_update_settings_resets_flexget_runtime_when_toggled_off(self):
        previous = routes.AppSettings(flexget_enabled=True)
        saved = {}

        def fake_save(cfg):
            saved["cfg"] = cfg

        def fake_apply(cfg):
            saved["applied"] = cfg

        with patch("api.routes.get_settings", return_value=previous), \
             patch("api.routes.save_settings", side_effect=fake_save), \
             patch("api.routes.apply_settings", side_effect=fake_apply), \
             patch.object(routes.manager, "apply_aria2_memory_tuning", AsyncMock(return_value={"ok": True})), \
             patch("services.flexget.reset_runtime_state") as reset_runtime_state, \
             patch.object(routes.manager, "reset_services", MagicMock()):
            result = await routes.update_settings(routes.AppSettings(flexget_enabled=False))

        self.assertEqual(result, {"ok": True})
        reset_runtime_state.assert_called_once()

    async def test_update_settings_persists_reporting_window(self):
        saved = {}

        def fake_save(cfg):
            saved["cfg"] = cfg

        def fake_apply(cfg):
            saved["applied"] = cfg

        with patch("api.routes.save_settings", side_effect=fake_save), \
             patch("api.routes.apply_settings", side_effect=fake_apply), \
             patch.object(routes.manager, "apply_aria2_memory_tuning", AsyncMock(return_value={"ok": True})), \
             patch.object(routes.manager, "reset_services", MagicMock()):
            result = await routes.update_settings(
                routes.AppSettings(
                    stats_report_interval_hours=12,
                    stats_report_window_hours=168,
                    stats_report_webhook_url="https://discord.com/api/webhooks/test",
                )
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(saved["cfg"].stats_report_interval_hours, 12)
        self.assertEqual(saved["cfg"].stats_report_window_hours, 168)
        self.assertEqual(saved["applied"].stats_report_window_hours, 168)


class _FakeDb:
    def __init__(self, rows=None, total=0):
        self.rows = rows or []
        self.total = total
        self.fetchall_calls = []
        self.fetchone_calls = []

    async def fetchall(self, sql, params=()):
        self.fetchall_calls.append((sql, list(params)))
        return self.rows

    async def fetchone(self, sql, params=()):
        self.fetchone_calls.append((sql, list(params)))
        return {"cnt": self.total}


@asynccontextmanager
async def _fake_db_context(db):
    yield db


class TorrentListingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_torrents_uses_search_and_status_filters_without_limit_clause(self):
        db = _FakeDb(rows=[{"id": 1, "name": "Example"}], total=1)

        with patch("api.routes.get_db", return_value=_fake_db_context(db)):
            result = await routes.list_torrents(
                status="completed",
                search="Example",
                limit=0,
                offset=0,
            )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"], [{"id": 1, "name": "Example"}])
        sql, params = db.fetchall_calls[0]
        self.assertIn("t.status = ?", sql)
        self.assertIn("LOWER(COALESCE(t.name, '')) LIKE ?", sql)
        self.assertNotIn("LIMIT ? OFFSET ?", sql)
        self.assertEqual(
            params,
            ["completed", "%example%", "%example%", "%example%", "%example%"],
        )
        total_sql, total_params = db.fetchone_calls[0]
        self.assertIn("SELECT COUNT(*) AS cnt FROM torrents t WHERE", total_sql)
        self.assertEqual(total_params, params)

    async def test_list_torrents_appends_limit_and_offset_when_requested(self):
        db = _FakeDb(rows=[], total=0)

        with patch("api.routes.get_db", return_value=_fake_db_context(db)):
            await routes.list_torrents(status=None, search=None, limit=250, offset=25)

        sql, params = db.fetchall_calls[0]
        self.assertIn("LIMIT ? OFFSET ?", sql)
        self.assertEqual(params[-2:], [250, 25])


class JackettRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_jackett_search_marks_existing_hashes(self):
        db = _FakeDb(rows=[{"id": 7, "hash": "abc123", "status": "completed", "name": "Existing"}], total=1)
        payload = {
            "results": [
                {"title": "Existing", "hash": "abc123", "magnet": "magnet:?xt=urn:btih:abc123", "torrent_url": ""},
                {"title": "New", "hash": "def456", "magnet": "", "torrent_url": "http://example/test.torrent"},
            ],
            "total": 2,
            "query": "test",
            "error": None,
        }
        with patch("api.routes.get_db", return_value=_fake_db_context(db)), \
             patch("services.jackett.search", AsyncMock(return_value=payload)):
            result = await routes.jackett_search({"query": "test", "trackers": ["a", "b"]})

        self.assertTrue(result["results"][0]["already_added"])
        self.assertEqual(result["results"][0]["existing_torrent_id"], 7)
        self.assertEqual(result["results"][0]["existing_status"], "completed")
        self.assertFalse(result["results"][1]["already_added"])

    async def test_jackett_search_marks_existing_titles_via_download_files(self):
        db = _FakeDb(
            rows=[{"id": 8, "hash": "zzz999", "status": "completed", "name": "Some Torrent", "filename": "Exact.Match.File.mp4"}],
            total=1,
        )
        payload = {
            "results": [
                {"title": "Exact.Match.File.mp4", "hash": "", "magnet": "", "torrent_url": "http://example/test.torrent"},
            ],
            "total": 1,
            "query": "test",
            "error": None,
        }
        with patch("api.routes.get_db", return_value=_fake_db_context(db)), \
             patch("services.jackett.search", AsyncMock(return_value=payload)):
            result = await routes.jackett_search({"query": "test"})

        self.assertTrue(result["results"][0]["already_added"])
        self.assertEqual(result["results"][0]["existing_torrent_id"], 8)
        self.assertEqual(result["results"][0]["existing_status"], "completed")

    async def test_jackett_search_marks_existing_titles_with_punctuation_variants(self):
        db = _FakeDb(
            rows=[{"id": 12, "hash": "zzz111", "status": "completed", "name": "Some Torrent", "filename": "CzechSexCasting E435 Kathy Deep 1080p.mkv"}],
            total=1,
        )
        payload = {
            "results": [
                {"title": "CzechSexCasting.E435.Kathy.Deep.1080p.mp4", "hash": "", "magnet": "", "torrent_url": "http://example/test.torrent"},
            ],
            "total": 1,
            "query": "test",
            "error": None,
        }
        with patch("api.routes.get_db", return_value=_fake_db_context(db)), \
             patch("services.jackett.search", AsyncMock(return_value=payload)):
            result = await routes.jackett_search({"query": "test"})

        self.assertTrue(result["results"][0]["already_added"])
        self.assertEqual(result["results"][0]["existing_torrent_id"], 12)
        self.assertEqual(result["results"][0]["existing_status"], "completed")

    async def test_jackett_add_prefers_torrent_file_before_magnet(self):
        row = {"id": 5, "status": "uploading", "alldebrid_id": "123"}
        with patch("services.jackett.download_torrent_file", AsyncMock(return_value={"filename": "item.torrent", "content": b"abc"})) as download_mock, \
             patch.object(routes.manager, "add_torrent_file_direct", AsyncMock(return_value=row)) as add_torrent_mock, \
             patch.object(routes.manager, "add_magnet_direct", AsyncMock(return_value={"id": 9})) as add_magnet_mock, \
             patch("services.jackett.send_jackett_webhook", AsyncMock(return_value=None)):
            result = await routes.jackett_add({
                "magnet": "magnet:?xt=urn:btih:abcdefabcdefabcdefabcdefabcdefabcdefabcd",
                "torrent_url": "http://example/item.torrent",
                "title": "Example",
                "indexer": "Tracker",
                "size_bytes": 123,
            })

        download_mock.assert_awaited_once()
        add_torrent_mock.assert_awaited_once()
        add_magnet_mock.assert_not_awaited()
        self.assertEqual(result["added_via"], "torrent_file")

    async def test_jackett_add_passes_result_hash_to_torrent_upload_path(self):
        row = {"id": 6, "status": "uploading", "alldebrid_id": "124"}
        with patch("services.jackett.download_torrent_file", AsyncMock(return_value={"filename": "item.torrent", "content": b"abc"})), \
             patch.object(routes.manager, "add_torrent_file_direct", AsyncMock(return_value=row)) as add_torrent_mock, \
             patch("services.jackett.send_jackett_webhook", AsyncMock(return_value=None)):
            await routes.jackett_add({
                "hash": "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
                "magnet": "",
                "torrent_url": "http://example/item.torrent",
                "title": "Example",
                "indexer": "Tracker",
                "size_bytes": 123,
            })

        add_torrent_mock.assert_awaited_once_with(
            b"abc",
            "item.torrent",
            source="jackett",
            preferred_hash="abcdef1234567890abcdef1234567890abcdef12",
        )


class FlexGetRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_flexget_running_returns_empty_when_disabled(self):
        with patch("api.routes.get_settings", return_value=SimpleNamespace(flexget_enabled=False)):
            result = await routes.flexget_running()
        self.assertEqual(result, {"running": []})


class DatabaseMaintenanceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_wipe_requires_feature_toggle(self):
        cfg = SimpleNamespace(db_wipe_enabled=False, paused=True, db_backup_before_wipe=True)
        with patch("api.routes.get_settings", return_value=cfg):
            with self.assertRaises(routes.HTTPException) as exc:
                await routes.wipe_database_admin({"confirm": True})
        self.assertEqual(exc.exception.status_code, 400)

    async def test_database_wipe_requires_pause(self):
        cfg = SimpleNamespace(db_wipe_enabled=True, paused=False, db_backup_before_wipe=True)
        with patch("api.routes.get_settings", return_value=cfg):
            with self.assertRaises(routes.HTTPException) as exc:
                await routes.wipe_database_admin({"confirm": True})
        self.assertEqual(exc.exception.status_code, 409)


class DatabaseBackupServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_backup_serializes_datetime_rows(self):
        from services import db_maintenance
        temp_root = Path(__file__).resolve().parent / "_tmp_db_backup"
        if temp_root.exists():
            import shutil
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        cfg = SimpleNamespace(
            db_backup_enabled=True,
            db_backup_folder=str(temp_root),
            db_backup_keep_days=7,
        )
        row = {
            "id": 1,
            "created_at": datetime(2026, 4, 21, 12, 34, 56, tzinfo=timezone.utc),
        }

        class _BackupDb:
            async def fetchall(self, sql, params=()):
                return [row]

        @asynccontextmanager
        async def _db_ctx():
            yield _BackupDb()

        try:
            with patch("services.db_maintenance.get_settings", return_value=cfg), \
                 patch("services.db_maintenance.get_db", return_value=_db_ctx()), \
                 patch("services.db_maintenance._is_postgres", return_value=False):
                result = await db_maintenance.run_database_backup()

            self.assertEqual(result["errors"], [])
            exported = Path(result["file"]).read_text(encoding="utf-8")
            self.assertIn("2026-04-21T12:34:56+00:00", exported)
        finally:
            if temp_root.exists():
                import shutil
                shutil.rmtree(temp_root)


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
