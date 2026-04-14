import unittest
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientTimeout=lambda *args, **kwargs: None,
        ClientSession=object,
        FormData=object,
        ClientError=Exception,
    )
if "aiofiles" not in sys.modules:
    sys.modules["aiofiles"] = types.SimpleNamespace(open=lambda *args, **kwargs: None)
if "aiosqlite" not in sys.modules:
    sys.modules["aiosqlite"] = types.SimpleNamespace(Connection=object, Row=object, connect=lambda *args, **kwargs: None)
if "pydantic" not in sys.modules:
    class _FakeBaseModel:
        model_fields = {}

        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            cls.model_fields = dict(getattr(cls, "__annotations__", {}))

        def __init__(self, **kwargs):
            for key, value in self.__class__.__dict__.items():
                if not key.startswith("_") and not callable(value):
                    setattr(self, key, value)
            for key, value in kwargs.items():
                setattr(self, key, value)

        def model_dump(self):
            return self.__dict__.copy()

        def model_copy(self, update=None):
            data = self.model_dump()
            if update:
                data.update(update)
            return self.__class__(**data)

    sys.modules["pydantic"] = types.SimpleNamespace(BaseModel=_FakeBaseModel)

from services.alldebrid import flatten_files
from services.aria2 import Aria2Service
from services.manager_v2 import normalize_provider_state, safe_rel_path


class ManagerV2Tests(unittest.TestCase):
    def test_flatten_files_preserves_nested_path(self):
        nodes = [
            {
                "n": "Season 01",
                "e": [
                    {"n": "Episode 01.mkv", "s": 123, "l": "https://example.invalid/1"},
                ],
            }
        ]

        flat = flatten_files(nodes)

        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["path"], "Season 01/Episode 01.mkv")

    def test_safe_rel_path_sanitizes_segments(self):
        path = safe_rel_path("../Season 01/Bad:Name?.mkv")
        self.assertEqual(str(path).replace("\\", "/"), "Season 01/Bad_Name_.mkv")

    def test_normalize_provider_state_ready(self):
        state = normalize_provider_state({"statusCode": 4, "size": 200, "downloaded": 200, "status": "Ready"})
        self.assertEqual(state["provider_status"], "ready")
        self.assertEqual(state["local_status"], "ready")
        self.assertEqual(int(state["progress"]), 100)

    def test_normalize_provider_state_error(self):
        state = normalize_provider_state({"statusCode": 8, "size": 200, "downloaded": 10, "status": "Error"})
        self.assertEqual(state["provider_status"], "error")
        self.assertEqual(state["local_status"], "error")


class Aria2ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_all_uses_individual_rpc_calls(self):
        service = Aria2Service("http://localhost:6800/jsonrpc", "secret", 15)
        calls = []

        async def fake_call(method, params=None):
            calls.append((method, params))
            if method == "aria2.tellActive":
                return [{"gid": "1", "status": "active"}]
            if method == "aria2.tellWaiting":
                return [{"gid": "2", "status": "waiting"}]
            if method == "aria2.tellStopped":
                return [{"gid": "3", "status": "complete"}]
            raise AssertionError(method)

        service._call = fake_call
        rows = await service.get_all()

        self.assertEqual([row.gid for row in rows], ["1", "2", "3"])
        self.assertEqual([method for method, _ in calls], ["aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"])


class ManagerDedupeTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_deduplicates_duplicate_file_entries(self):
        from services.manager_v2 import TorrentManager

        manager = TorrentManager()
        manager._log_file = AsyncMock()
        manager._send_partial_summary = AsyncMock()
        manager._log_event = AsyncMock()
        manager._delete_magnet_after_completion = AsyncMock()
        manager._mark_finished = AsyncMock()
        manager._download_direct = AsyncMock(return_value="done")
        fake_ad = types.SimpleNamespace(unlock_link=AsyncMock(return_value={"link": "https://download.invalid/file"}))
        manager.ad = lambda: fake_ad

        fake_cfg = types.SimpleNamespace(
            download_client="direct",
            download_folder=str(Path.cwd() / "tmp-downloads"),
            blocked_extensions=[],
            blocked_keywords=[],
            min_file_size_mb=0,
            aria2_start_paused=False,
            discord_notify_finished=False,
            discord_notify_error=False,
        )

        duplicate_files = [
            {"path": "folder/file.mp4", "name": "file.mp4", "size": 10, "link": "https://source.invalid/a"},
            {"path": "folder/file.mp4", "name": "file.mp4", "size": 10, "link": "https://source.invalid/a"},
        ]

        with patch("services.manager_v2.get_settings", return_value=fake_cfg), \
             patch("services.manager_v2.aiohttp.ClientSession"), \
             patch("services.manager_v2.aiosqlite.connect") as mock_connect:
            mock_db = AsyncMock()
            mock_connect.return_value.__aenter__.return_value = mock_db
            manager._fetch_ready_files = AsyncMock(return_value=duplicate_files)
            await manager._download(1, "ad-id", "Torrent Name")

        self.assertEqual(fake_ad.unlock_link.await_count, 1)
        self.assertEqual(manager._log_file.await_count, 1)


if __name__ == "__main__":
    unittest.main()
