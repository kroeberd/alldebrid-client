import asyncio
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

from services.alldebrid import AllDebridService, flatten_files
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


class AllDebridServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_retries_after_empty_response(self):
        service = AllDebridService("api-key")
        responses = ["", '{"status":"success","data":{"ok":true}}']

        class FakeResponse:
            def __init__(self, body):
                self.body = body
                self.status = 200

            async def text(self):
                return self.body

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def post(self, *args, **kwargs):
                return FakeResponse(responses.pop(0))

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with patch("services.alldebrid.aiohttp.ClientSession", FakeSession):
            result = await service._post("https://api.example", "magnet/status", retries=2)

        self.assertEqual(result, {"ok": True})

    def test_decode_json_body_reports_invalid_payload(self):
        service = AllDebridService("api-key")
        with self.assertRaises(Exception) as ctx:
            service._decode_json_body("<html>bad gateway</html>", "magnet/status")
        self.assertIn("invalid JSON", str(ctx.exception))


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

    async def test_ensure_download_serializes_same_uri(self):
        service = Aria2Service("http://localhost:6800/jsonrpc", "secret", 15)
        state = {"added": False, "add_calls": 0}

        async def fake_get_all():
            if state["added"]:
                return [
                    types.SimpleNamespace(
                        gid="gid-1",
                        status="active",
                        files=[{"uris": [{"uri": "https://same.invalid/file"}]}],
                    )
                ]
            return []

        async def fake_call(method, params=None):
            if method == "aria2.addUri":
                state["add_calls"] += 1
                await asyncio.sleep(0.01)
                state["added"] = True
                return "gid-1"
            raise AssertionError(method)

        service.get_all = fake_get_all
        service._call = fake_call

        gid1, gid2 = await asyncio.gather(
            service.ensure_download("https://same.invalid/file", {"dir": "/downloads", "out": "file.bin"}),
            service.ensure_download("https://same.invalid/file", {"dir": "/downloads", "out": "file.bin"}),
        )

        self.assertEqual(gid1, "gid-1")
        self.assertEqual(gid2, "gid-1")
        self.assertEqual(state["add_calls"], 1)

    async def test_ensure_download_reuses_existing_target_path(self):
        service = Aria2Service("http://localhost:6800/jsonrpc", "secret", 15)
        service.get_all = AsyncMock(return_value=[
            types.SimpleNamespace(
                gid="gid-path",
                status="active",
                files=[{"path": "/downloads/show/file.mp4", "uris": [{"uri": "https://old.invalid/link"}]}],
            )
        ])
        service._call = AsyncMock()

        gid = await service.ensure_download(
            "https://new.invalid/link",
            {"dir": "/downloads/show", "out": "file.mp4"},
        )

        self.assertEqual(gid, "gid-path")
        service._call.assert_not_awaited()


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
            filters_enabled=True,
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

    async def test_aria2_download_preparation_does_not_create_destination_root(self):
        from services.manager_v2 import TorrentManager

        manager = TorrentManager()
        manager._log_file = AsyncMock()
        manager._send_partial_summary = AsyncMock()
        manager._log_event = AsyncMock()
        manager._delete_magnet_after_completion = AsyncMock()
        manager._mark_finished = AsyncMock()
        manager._dispatch_pending_aria2_queue = AsyncMock()
        fake_ad = types.SimpleNamespace(unlock_link=AsyncMock(return_value={"link": "https://download.invalid/file"}))
        manager.ad = lambda: fake_ad

        root = Path.cwd() / "backend" / "tests" / "_tmp_aria2_prepare"
        if root.exists():
            for child in sorted(root.rglob("*"), reverse=True):
                try:
                    if child.is_file():
                        child.unlink()
                    else:
                        child.rmdir()
                except Exception:
                    pass
        download_folder = root / "downloads"
        destination_root = download_folder / "Torrent Name"

        fake_cfg = types.SimpleNamespace(
            download_client="aria2",
            download_folder=str(download_folder),
            filters_enabled=True,
            blocked_extensions=[],
            blocked_keywords=[],
            min_file_size_mb=0,
            aria2_start_paused=False,
            discord_notify_finished=False,
            discord_notify_error=False,
        )

        files = [
            {"path": "folder/file.mp4", "name": "file.mp4", "size": 10, "link": "https://source.invalid/a"},
        ]

        try:
            with patch("services.manager_v2.get_settings", return_value=fake_cfg), \
                 patch("services.manager_v2.aiosqlite.connect") as mock_connect:
                mock_db = AsyncMock()
                mock_connect.return_value.__aenter__.return_value = mock_db
                manager._fetch_ready_files = AsyncMock(return_value=files)
                await manager._download(1, "ad-id", "Torrent Name")

            self.assertFalse(destination_root.exists())
            manager._dispatch_pending_aria2_queue.assert_awaited_once()
        finally:
            if root.exists():
                for child in sorted(root.rglob("*"), reverse=True):
                    try:
                        if child.is_file():
                            child.unlink()
                        else:
                            child.rmdir()
                    except Exception:
                        pass

    async def test_startup_reconcile_removes_duplicate_aria2_jobs(self):
        from services.manager_v2 import TorrentManager

        manager = TorrentManager()
        keep = types.SimpleNamespace(gid="keep", status="active", files=[{"uris": [{"uri": "https://same.invalid/file"}]}])
        dup = types.SimpleNamespace(gid="dup", status="waiting", files=[{"uris": [{"uri": "https://same.invalid/file"}]}])
        calls = {"count": 0}

        async def fake_get_all():
            calls["count"] += 1
            if calls["count"] == 1:
                return [keep, dup]
            return [keep]

        fake_aria2 = types.SimpleNamespace(
            get_all=AsyncMock(side_effect=fake_get_all),
            remove=AsyncMock(),
        )
        manager.aria2 = lambda: fake_aria2

        deduped = await manager._dedupe_aria2_downloads_on_startup([keep, dup])

        fake_aria2.remove.assert_awaited_once_with("dup")
        self.assertEqual([row.gid for row in deduped], ["keep"])

    def test_build_aria2_indexes_tracks_path_and_uri(self):
        from services.manager_v2 import TorrentManager

        manager = TorrentManager()
        download = types.SimpleNamespace(
            gid="gid-1",
            files=[{"path": "/downloads/show/file.mp4", "uris": [{"uri": "https://example.invalid/file"}]}],
        )

        by_gid, uri_to_dl, path_to_dl = manager._build_aria2_indexes([download])

        self.assertIs(by_gid["gid-1"], download)
        self.assertIs(uri_to_dl["https://example.invalid/file"], download)
        self.assertIs(path_to_dl["/downloads/show/file.mp4"], download)

    def test_aria2_slot_limit_uses_dedicated_setting(self):
        from services.manager_v2 import TorrentManager

        manager = TorrentManager()
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_max_active_downloads=7,
            max_concurrent_downloads=3,
        )):
            self.assertEqual(manager._aria2_slot_limit(), 7)

    async def test_scan_watch_folder_moves_failed_files_to_error_dir(self):
        from services.manager_v2 import TorrentManager

        manager = TorrentManager()
        root = Path.cwd() / "backend" / "tests" / "_tmp_watch_scan"
        root.mkdir(parents=True, exist_ok=True)
        try:
            watch = root / "watch"
            processed = root / "processed"
            watch.mkdir(parents=True, exist_ok=True)
            processed.mkdir(parents=True, exist_ok=True)
            failing = watch / "broken.torrent"
            failing.write_bytes(b"not-a-real-torrent")
            ignored_dir = watch / "error"
            ignored_dir.mkdir(exist_ok=True)
            (ignored_dir / "ignoreme.torrent").write_bytes(b"stay-here")

            seen = []

            async def raise_on_handle(file_path, processed_path):
                seen.append(file_path.name)
                raise RuntimeError("boom")

            manager._handle_torrent = raise_on_handle
            manager._move_watch_file_to_error = lambda file_path, watch_path: watch_path / "error" / file_path.name

            fake_cfg = types.SimpleNamespace(
                paused=False,
                watch_folder=str(watch),
                processed_folder=str(processed),
            )

            with patch("services.manager_v2.get_settings", return_value=fake_cfg), \
                 patch("services.manager_v2.aiosqlite.connect") as mock_connect:
                mock_db = AsyncMock()
                mock_connect.return_value.__aenter__.return_value = mock_db
                await manager.scan_watch_folder()

            self.assertEqual(seen, ["broken.torrent"])
        finally:
            if root.exists():
                for child in sorted(root.rglob("*"), reverse=True):
                    try:
                        if child.is_file():
                            child.unlink()
                        else:
                            child.rmdir()
                    except Exception:
                        pass
                try:
                    root.rmdir()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
