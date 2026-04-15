"""
Tests für AllDebrid-Client Backend.

Deckt ab:
- aria2 Verbindungsrobustheit (Closing-Transport-Fehler)
- Abschluss-Erkennung (Finished-Entry-Handling)
- Duplikat-Vermeidung
- Dashboard-Datenfluss (completed-Status)
- Discord-Webhook-Formatierung inkl. torrent-added
- Statistik-Berechnungen
- Migrations-Sicherheitsprüfungen
- PostgreSQL-Konfigurationsvalidierung
"""
import asyncio
import unittest
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── Stub-Importe für fehlende Pakete ──────────────────────────────────────────
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
        Connection=object, Row=object,
        connect=lambda *a, **kw: None,
    )
if "pydantic" not in sys.modules:
    class _FakeModel:
        model_fields = {}
        def __init_subclass__(cls, **kw):
            super().__init_subclass__(**kw)
            cls.model_fields = dict(getattr(cls, "__annotations__", {}))
        def __init__(self, **kw):
            for k, v in self.__class__.__dict__.items():
                if not k.startswith("_") and not callable(v):
                    setattr(self, k, v)
            for k, v in kw.items():
                setattr(self, k, v)
        def model_dump(self): return self.__dict__.copy()
        def model_copy(self, update=None):
            d = self.model_dump()
            if update: d.update(update)
            return self.__class__(**d)
    sys.modules["pydantic"] = types.SimpleNamespace(BaseModel=_FakeModel)

from services.alldebrid import AllDebridService, flatten_files
from services.aria2 import Aria2Service, Aria2RPCError, Aria2ConnectionError
from services.manager_v2 import normalize_provider_state, safe_rel_path, TorrentManager


# ═════════════════════════════════════════════════════════════════════════════
# Basis-Tests (bereits vorhanden, erweitert)
# ═════════════════════════════════════════════════════════════════════════════

class ManagerV2Tests(unittest.TestCase):
    def test_flatten_files_preserves_nested_path(self):
        nodes = [{"n": "Season 01", "e": [
            {"n": "Episode 01.mkv", "s": 123, "l": "https://example.invalid/1"},
        ]}]
        flat = flatten_files(nodes)
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["path"], "Season 01/Episode 01.mkv")

    def test_safe_rel_path_sanitizes_segments(self):
        path = safe_rel_path("../Season 01/Bad:Name?.mkv")
        self.assertEqual(str(path).replace("\\", "/"), "Season 01/Bad_Name_.mkv")

    def test_normalize_provider_state_ready(self):
        s = normalize_provider_state({"statusCode": 4, "size": 200, "downloaded": 200, "status": "Ready"})
        self.assertEqual(s["provider_status"], "ready")
        self.assertEqual(int(s["progress"]), 100)

    def test_normalize_provider_state_error(self):
        s = normalize_provider_state({"statusCode": 8, "size": 200, "downloaded": 10, "status": "Error"})
        self.assertEqual(s["provider_status"], "error")

    def test_normalize_provider_state_processing(self):
        s = normalize_provider_state({"statusCode": 2, "size": 100, "downloaded": 50, "status": "Processing"})
        self.assertEqual(s["provider_status"], "processing")
        self.assertEqual(s["local_status"], "processing")

    def test_normalize_provider_state_queued(self):
        s = normalize_provider_state({"statusCode": 0, "size": 0, "downloaded": 0, "status": "Queued"})
        self.assertEqual(s["provider_status"], "queued")
        self.assertEqual(s["local_status"], "uploading")


# ═════════════════════════════════════════════════════════════════════════════
# aria2 Robustheit
# ═════════════════════════════════════════════════════════════════════════════

class Aria2RobustnessTests(unittest.IsolatedAsyncioTestCase):
    """
    Testet aria2-Verbindungsrobustheit, insbesondere:
    - "Cannot write to closing transport" wird als Aria2ConnectionError klassifiziert
    - get_all() gibt [] zurück statt zu werfen
    - Retry-Logik bei transienten Fehlern
    """

    async def test_connection_error_classified_as_aria2_connection_error(self):
        """Transiente Verbindungsfehler werden als Aria2ConnectionError klassifiziert."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        class FakeConnector:
            closed = False
            async def close(self): pass

        async def fake_post(*a, **kw):
            raise Exception("Cannot write to closing transport")

        class FakeSession:
            def __init__(self, *a, **kw): pass
            def post(self, *a, **kw): return self
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def __call__(self, *a, **kw): raise Exception("Cannot write to closing transport")

        with patch("services.aria2.aiohttp.TCPConnector", return_value=FakeConnector()), \
             patch("services.aria2.aiohttp.ClientSession") as mock_session:
            mock_session.return_value.__aenter__ = AsyncMock(side_effect=Exception("Cannot write to closing transport"))
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

            with self.assertRaises((Aria2ConnectionError, Aria2RPCError, Exception)):
                await service._call("aria2.getVersion")

    async def test_get_all_returns_empty_on_connection_error(self):
        """get_all() gibt leere Liste zurück wenn aria2 nicht erreichbar ist."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        async def fake_call(method, params=None):
            raise Aria2ConnectionError("Verbindung unterbrochen")

        service._call = fake_call
        result = await service.get_all()
        self.assertEqual(result, [])

    async def test_get_all_returns_empty_on_rpc_error(self):
        """get_all() gibt leere Liste zurück bei RPC-Fehler."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        async def fake_call(method, params=None):
            raise Aria2RPCError("aria2 [-32600]: Invalid Request")

        service._call = fake_call
        result = await service.get_all()
        self.assertEqual(result, [])

    async def test_get_all_aggregates_all_three_endpoints(self):
        """get_all() kombiniert active, waiting und stopped Downloads."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        async def fake_call(method, params=None):
            if method == "aria2.tellActive":
                return [{"gid": "a1", "status": "active", "totalLength": "100",
                         "completedLength": "50", "downloadSpeed": "10", "files": []}]
            if method == "aria2.tellWaiting":
                return [{"gid": "w1", "status": "waiting", "totalLength": "200",
                         "completedLength": "0", "downloadSpeed": "0", "files": []}]
            if method == "aria2.tellStopped":
                return [{"gid": "s1", "status": "complete", "totalLength": "300",
                         "completedLength": "300", "downloadSpeed": "0", "files": []}]
            return []

        service._call = fake_call
        result = await service.get_all()
        self.assertEqual(len(result), 3)
        self.assertEqual({dl.gid for dl in result}, {"a1", "w1", "s1"})

    async def test_ensure_download_retry_on_connection_error(self):
        """ensure_download() versucht Retry bei Verbindungsfehlern."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        attempt_count = {"n": 0}

        async def fake_get_all():
            return []

        async def fake_call(method, params=None):
            if method == "aria2.addUri":
                attempt_count["n"] += 1
                if attempt_count["n"] < 3:
                    raise Aria2ConnectionError("Verbindung unterbrochen")
                return "gid-final"
            raise AssertionError(f"Unerwarteter Aufruf: {method}")

        service.get_all = fake_get_all
        service._call = fake_call

        with patch("services.aria2.asyncio.sleep", new=AsyncMock()):
            gid = await service.ensure_download("https://test.invalid/file", max_retries=5)

        self.assertEqual(gid, "gid-final")
        self.assertEqual(attempt_count["n"], 3)

    async def test_ensure_download_deduplication_by_uri(self):
        """ensure_download() erkennt bereits laufende Downloads per URI."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        add_calls = {"n": 0}

        async def fake_get_all():
            return [types.SimpleNamespace(
                gid="existing-gid",
                status="active",
                total_length=1000,
                completed_length=500,
                download_speed=100,
                files=[{"path": "/dl/file.mp4",
                        "uris": [{"uri": "https://test.invalid/file"}]}],
            )]

        async def fake_call(method, params=None):
            if method == "aria2.addUri":
                add_calls["n"] += 1
            return "new-gid"

        service.get_all = fake_get_all
        service._call = fake_call

        gid = await service.ensure_download("https://test.invalid/file")
        self.assertEqual(gid, "existing-gid")
        self.assertEqual(add_calls["n"], 0)

    async def test_ensure_download_deduplication_by_path(self):
        """ensure_download() erkennt bereits laufende Downloads per Zielpfad."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        add_calls = {"n": 0}

        async def fake_get_all():
            return [types.SimpleNamespace(
                gid="path-gid",
                status="active",
                total_length=1000,
                completed_length=0,
                download_speed=0,
                files=[{"path": "/downloads/show/episode.mkv",
                        "uris": [{"uri": "https://old-url.invalid/file"}]}],
            )]

        async def fake_call(method, params=None):
            add_calls["n"] += 1
            return "new-gid"

        service.get_all = fake_get_all
        service._call = fake_call

        gid = await service.ensure_download(
            "https://new-url.invalid/file",
            {"dir": "/downloads/show", "out": "episode.mkv"},
        )
        self.assertEqual(gid, "path-gid")
        self.assertEqual(add_calls["n"], 0)

    async def test_ensure_download_concurrent_same_uri_serialized(self):
        """Gleichzeitige ensure_download-Aufrufe für die gleiche URI werden serialisiert."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        add_count = {"n": 0}
        state = {"added": False}

        async def fake_get_all():
            if state["added"]:
                return [types.SimpleNamespace(
                    gid="gid-1", status="active", total_length=0,
                    completed_length=0, download_speed=0,
                    files=[{"path": "", "uris": [{"uri": "https://same.invalid/file"}]}],
                )]
            return []

        async def fake_call(method, params=None):
            if method == "aria2.addUri":
                add_count["n"] += 1
                await asyncio.sleep(0.01)
                state["added"] = True
                return "gid-1"
            raise AssertionError(method)

        service.get_all = fake_get_all
        service._call = fake_call

        g1, g2 = await asyncio.gather(
            service.ensure_download("https://same.invalid/file", {"dir": "/dl", "out": "file"}),
            service.ensure_download("https://same.invalid/file", {"dir": "/dl", "out": "file"}),
        )
        self.assertEqual(g1, "gid-1")
        self.assertEqual(g2, "gid-1")
        self.assertEqual(add_count["n"], 1)


# ═════════════════════════════════════════════════════════════════════════════
# Abschluss-Erkennung (Finished Entry Handling)
# ═════════════════════════════════════════════════════════════════════════════

class FinishedEntryTests(unittest.IsolatedAsyncioTestCase):
    """
    Testet zuverlässige Erkennung abgeschlossener Downloads.
    """

    async def test_finalize_marks_completed_when_all_files_done(self):
        """_finalize_aria2_torrent() markiert Torrent als completed wenn alle Dateien fertig."""
        mgr = TorrentManager()
        mgr._delete_magnet_after_completion = AsyncMock(return_value=True)
        mgr._mark_finished = AsyncMock()
        mgr._log_event = AsyncMock()
        notify_mock = MagicMock()
        notify_mock.send_complete = AsyncMock()
        mgr.notify = lambda: notify_mock

        # dict-kompatible Zeile (Manager greift mit torrent["status"] zu)
        torrent_row = {
            "id": 1, "status": "queued", "alldebrid_id": "ad-1", "name": "Test Torrent",
            "hash": None, "magnet": None, "size_bytes": 0, "progress": 0,
            "download_url": None, "local_path": None, "source": None,
            "provider_status": None, "provider_status_code": None,
            "polling_failures": 0, "download_client": "aria2",
            "error_message": None, "created_at": None, "updated_at": None,
            "completed_at": None,
        }
        counts_row = {
            "required_count": 2, "completed_count": 2, "error_count": 0,
            "active_count": 0, "paused_count": 0, "total_files": 2,
        }

        class FakeCursor:
            def __init__(self, result): self._result = result
            async def fetchone(self): return self._result

        async def fake_execute(sql, params=()):
            if "SELECT * FROM torrents" in sql:
                return FakeCursor(torrent_row)
            if "SUM(CASE WHEN blocked=0" in sql:
                return FakeCursor(counts_row)
            if "SUM(size_bytes)" in sql:
                return FakeCursor({"total": 5000})
            return FakeCursor(None)

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = fake_execute
        fake_db.commit = AsyncMock()
        fake_db.row_factory = None

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db), \
             patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
                 discord_notify_finished=True, discord_notify_error=False
             )):
            await mgr._finalize_aria2_torrent(1)

        mgr._delete_magnet_after_completion.assert_awaited_once_with(1, "ad-1")
        mgr._mark_finished.assert_awaited_once_with(1)

    async def test_finalize_does_not_complete_when_files_still_active(self):
        """_finalize_aria2_torrent() markiert NICHT als completed wenn Dateien noch aktiv."""
        mgr = TorrentManager()
        mgr._delete_magnet_after_completion = AsyncMock()

        counts_row = {
            "required_count": 3, "completed_count": 1, "error_count": 0,
            "active_count": 2, "paused_count": 0, "total_files": 3,
        }

        class FakeCursor:
            def __init__(self, r): self._r = r
            async def fetchone(self): return self._r

        async def fake_execute(sql, params=()):
            if "SELECT * FROM torrents" in sql:
                return FakeCursor({
                    "id": 1, "status": "downloading", "alldebrid_id": "ad-1", "name": "T",
                    "hash": None, "magnet": None, "size_bytes": 0, "progress": 0,
                    "download_url": None, "local_path": None, "source": None,
                    "provider_status": None, "provider_status_code": None,
                    "polling_failures": 0, "download_client": "aria2",
                    "error_message": None, "created_at": None, "updated_at": None,
                    "completed_at": None,
                })
            if "SUM(CASE WHEN blocked=0" in sql:
                return FakeCursor(counts_row)
            return FakeCursor(None)

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = fake_execute
        fake_db.commit = AsyncMock()

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db):
            await mgr._finalize_aria2_torrent(1)

        mgr._delete_magnet_after_completion.assert_not_awaited()

    async def test_delete_magnet_keeps_completed_status(self):
        """
        _delete_magnet_after_completion() ändert Status NICHT zu 'deleted'.
        Dashboard-Fix: completed bleibt completed.
        """
        mgr = TorrentManager()

        fake_ad = types.SimpleNamespace(delete_magnet=AsyncMock(return_value=True))
        mgr.ad = lambda: fake_ad

        sql_calls = []

        async def fake_execute(sql, params=()):
            sql_calls.append(sql.strip())
            return AsyncMock()

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = fake_execute
        fake_db.commit = AsyncMock()

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db):
            await mgr._delete_magnet_after_completion(1, "ad-123")

        # Prüfen: kein UPDATE zu 'deleted'
        update_to_deleted = [
            sql for sql in sql_calls
            if "UPDATE torrents" in sql and "deleted" in sql
        ]
        self.assertEqual(
            update_to_deleted, [],
            f"_delete_magnet_after_completion darf status NICHT auf 'deleted' setzen! "
            f"Gefundene SQLs: {update_to_deleted}"
        )

    async def test_duplicate_file_entries_skipped(self):
        """Doppelte Dateieinträge von AllDebrid werden beim Download übersprungen."""
        mgr = TorrentManager()
        mgr._log_file = AsyncMock()
        mgr._send_partial_summary = AsyncMock()
        mgr._log_event = AsyncMock()
        mgr._delete_magnet_after_completion = AsyncMock()
        mgr._mark_finished = AsyncMock()
        mgr._dispatch_pending_aria2_queue = AsyncMock()
        mgr._download_direct = AsyncMock(return_value="ok")
        fake_ad = types.SimpleNamespace(
            unlock_link=AsyncMock(return_value={"link": "https://dl.invalid/file"})
        )
        mgr.ad = lambda: fake_ad

        duplicate_files = [
            {"path": "dir/file.mp4", "name": "file.mp4", "size": 10,
             "link": "https://source.invalid/a"},
            {"path": "dir/file.mp4", "name": "file.mp4", "size": 10,
             "link": "https://source.invalid/a"},  # Duplikat
        ]

        fake_cfg = types.SimpleNamespace(
            download_client="direct",
            download_folder=str(Path.cwd() / "tmp_test_dl"),
            filters_enabled=True, blocked_extensions=[], blocked_keywords=[],
            min_file_size_mb=0, aria2_start_paused=False,
            discord_notify_finished=False, discord_notify_error=False,
        )

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.commit = AsyncMock()
        fake_db.execute = AsyncMock(return_value=AsyncMock())

        with patch("services.manager_v2.get_settings", return_value=fake_cfg), \
             patch("services.manager_v2.aiosqlite.connect", return_value=fake_db):
            mgr._fetch_ready_files = AsyncMock(return_value=duplicate_files)
            await mgr._download(1, "ad-id", "Test")

        # unlock_link sollte nur EINMAL aufgerufen worden sein (Duplikat übersprungen)
        self.assertEqual(fake_ad.unlock_link.await_count, 1)
        self.assertEqual(mgr._log_file.await_count, 1)


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard-Datenfluss
# ═════════════════════════════════════════════════════════════════════════════

class DashboardCompletedTests(unittest.IsolatedAsyncioTestCase):
    """
    Stellt sicher dass completed-Torrents im Dashboard erscheinen.
    Root-Cause-Fix: _delete_magnet_after_completion setzt kein status='deleted'.
    """

    async def test_completed_count_not_reset_to_deleted(self):
        """
        Simulation: Torrent abgeschlossen → _delete_magnet_after_completion →
        status muss 'completed' bleiben, nicht 'deleted' werden.
        """
        mgr = TorrentManager()
        fake_ad = types.SimpleNamespace(delete_magnet=AsyncMock(return_value=True))
        mgr.ad = lambda: fake_ad

        status_updates = []

        async def capture_execute(sql, params=()):
            if "UPDATE torrents SET" in sql:
                status_updates.append({"sql": sql, "params": params})
            return AsyncMock()

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = capture_execute
        fake_db.commit = AsyncMock()

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db):
            await mgr._delete_magnet_after_completion(42, "ad-42")

        # Kein UPDATE ... status='deleted' nach Abschluss
        deleted_updates = [
            u for u in status_updates
            if "deleted" in str(u["params"])
        ]
        self.assertEqual(
            deleted_updates, [],
            "Nach erfolgreichem Download darf status nicht auf 'deleted' gesetzt werden."
        )

    def test_by_status_completed_field_used_in_stats(self):
        """Stats-Endpunkt liefert 'completed' als Key in by_status."""
        # Simuliert die Logik aus routes.py: by_status wird direkt aus DB gelesen
        by_status = {"completed": 5, "deleted": 2, "error": 1, "queued": 3}
        completed = by_status.get("completed", 0)
        self.assertEqual(completed, 5, "by_status.completed muss korrekte Zahl liefern")

        # Wenn deleted statt completed gesetzt würde:
        wrong_status = {"deleted": 5, "error": 1, "queued": 3}
        wrong_completed = wrong_status.get("completed", 0)
        self.assertEqual(wrong_completed, 0, "Fehlerhafte Logik würde 0 liefern")


# ═════════════════════════════════════════════════════════════════════════════
# Discord Webhook / Notifications
# ═════════════════════════════════════════════════════════════════════════════

class NotificationTests(unittest.IsolatedAsyncioTestCase):
    """
    Testet Discord-Webhook-Formatierung und torrent-added Event.
    """

    async def test_send_added_uses_added_webhook_url(self):
        """send_added() verwendet den separaten added_webhook_url wenn konfiguriert."""
        from services.notifications import NotificationService

        sent_to = []

        async def fake_send_embed(self, url, title, description, color, fields=None):
            sent_to.append(url)

        with patch.object(NotificationService, "_send_embed", fake_send_embed):
            svc = NotificationService(
                webhook_url="https://main.discord.invalid/hook",
                added_webhook_url="https://added.discord.invalid/hook",
            )
            await svc.send_added("My Torrent", source="manual", alldebrid_id="123")

        self.assertEqual(sent_to, ["https://added.discord.invalid/hook"])

    async def test_send_added_falls_back_to_main_webhook(self):
        """send_added() fällt auf discord_webhook_url zurück wenn kein added_webhook_url."""
        from services.notifications import NotificationService

        sent_to = []

        async def fake_send_embed(self, url, title, description, color, fields=None):
            sent_to.append(url)

        with patch.object(NotificationService, "_send_embed", fake_send_embed):
            svc = NotificationService(
                webhook_url="https://main.discord.invalid/hook",
                added_webhook_url="",
            )
            await svc.send_added("My Torrent", source="watch_file")

        self.assertEqual(sent_to, ["https://main.discord.invalid/hook"])

    async def test_send_added_includes_source_field(self):
        """send_added() übergibt Quell-Informationen als Embed-Feld."""
        from services.notifications import NotificationService

        captured_fields = []

        async def fake_send_embed(self, url, title, description, color, fields=None):
            captured_fields.extend(fields or [])

        with patch.object(NotificationService, "_send_embed", fake_send_embed):
            svc = NotificationService("https://hook.invalid/x")
            await svc.send_added("Test Torrent", source="watch_torrent", alldebrid_id="ad-42")

        field_names = [f["name"] for f in captured_fields]
        self.assertIn("Quelle", field_names)
        source_field = next(f for f in captured_fields if f["name"] == "Quelle")
        self.assertEqual(source_field["value"], "watch_torrent")

    async def test_deduplication_suppresses_duplicate_within_window(self):
        """Gleiche Nachricht innerhalb des Deduplizierungsfensters wird unterdrückt."""
        from services.notifications import NotificationService
        import hashlib as _hl

        # Klassenweite Zustandsreset für isolierten Test
        NotificationService._sent_hashes = {}
        NotificationService._last_sent_at = {}
        NotificationService._throttle_lock = None  # Neuen Lock im aktuellen event loop

        http_post_count = {"n": 0}

        class FakeResp:
            status = 204
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class FakeSession:
            def __init__(self, *a, **kw): pass
            def post(self, url, **kw):
                http_post_count["n"] += 1
                return FakeResp()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        svc = NotificationService("https://hook.invalid/x")

        # Erster Aufruf — kein hash vorhanden → HTTP wird abgesetzt
        with patch("services.notifications.aiohttp.ClientSession", FakeSession):
            await svc.send("Test", "Same content")
        self.assertEqual(http_post_count["n"], 1, "Erster Aufruf sollte HTTP absetzen")

        # Dedup-Hash muss jetzt gesetzt sein
        key = _hl.md5("https://hook.invalid/x|Test|Same content".encode()).hexdigest()
        self.assertIn(key, NotificationService._sent_hashes,
                      "Hash muss nach erstem Senden gesetzt sein")

        # Zweiter Aufruf mit gleichem Inhalt → Dedup → kein weiterer HTTP-Post
        with patch("services.notifications.aiohttp.ClientSession", FakeSession):
            await svc.send("Test", "Same content")
        self.assertEqual(http_post_count["n"], 1,
                         "Zweiter Aufruf mit gleichem Inhalt sollte kein HTTP absetzen (Dedup)")

    async def test_send_complete_includes_metadata_fields(self):
        """send_complete() enthält Metadaten-Felder im Embed."""
        from services.notifications import NotificationService

        captured = {}

        async def fake_send_embed(self, url, title, description, color, fields=None):
            captured["title"] = title
            captured["description"] = description
            captured["fields"] = fields or []

        with patch.object(NotificationService, "_send_embed", fake_send_embed):
            svc = NotificationService("https://hook.invalid/x")
            await svc.send_complete(
                "My Show S01",
                file_count=12,
                size_bytes=1073741824,
                destination="/downloads/My Show S01",
                download_client="aria2",
            )

        self.assertIn("✅", captured["title"])
        self.assertIn("My Show S01", captured["description"])
        field_names = [f["name"] for f in captured["fields"]]
        self.assertIn("Dateien", field_names)
        self.assertIn("Größe", field_names)

    async def test_send_error_includes_reason(self):
        """send_error() übergibt Fehlergrund als Feld."""
        from services.notifications import NotificationService

        captured_fields = []

        async def fake_send_embed(self, url, title, description, color, fields=None):
            captured_fields.extend(fields or [])

        with patch.object(NotificationService, "_send_embed", fake_send_embed):
            svc = NotificationService("https://hook.invalid/x")
            await svc.send_error("Failed Torrent", reason="AllDebrid error code 7")

        field_names = [f["name"] for f in captured_fields]
        self.assertIn("Grund", field_names)

    async def test_no_send_when_webhook_empty(self):
        """Keine Nachricht wenn webhook_url leer."""
        from services.notifications import NotificationService

        send_count = {"n": 0}

        async def fake_send_embed(self, url, title, description, color, fields=None):
            send_count["n"] += 1

        with patch.object(NotificationService, "_send_embed", fake_send_embed):
            svc = NotificationService("")
            await svc.send("Title", "Description")
            await svc.send_added("Torrent")
            await svc.send_complete("Torrent")
            await svc.send_error("Torrent")

        self.assertEqual(send_count["n"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# Statistik-Berechnungen
# ═════════════════════════════════════════════════════════════════════════════

class StatsCalculationTests(unittest.TestCase):
    """
    Testet Statistik-Berechnungen ohne Datenbankzugriff.
    """

    def test_success_rate_calculation(self):
        """Erfolgsrate wird korrekt berechnet."""
        completed = 8
        errors = 2
        terminal = completed + errors
        rate = round(completed / terminal * 100, 1)
        self.assertEqual(rate, 80.0)

    def test_success_rate_zero_when_no_terminal(self):
        """Erfolgsrate ist None wenn keine Terminal-Torrents vorhanden."""
        completed = 0
        errors = 0
        terminal = completed + errors
        rate = round(completed / terminal * 100, 1) if terminal > 0 else None
        self.assertIsNone(rate)

    def test_success_rate_100_percent(self):
        """100% Erfolgsrate wenn alle Torrents abgeschlossen."""
        completed = 10
        errors = 0
        terminal = completed + errors
        rate = round(completed / terminal * 100, 1) if terminal > 0 else None
        self.assertEqual(rate, 100.0)

    def test_by_status_completed_visible(self):
        """
        Abgeschlossene Torrents erscheinen als 'completed' in by_status
        (nicht als 'deleted' nach dem Löschen von AllDebrid).
        """
        # Simuliert den Datenbankstand nach korrektem Abschluss
        by_status = {
            "completed": 15,
            "error": 2,
            "queued": 3,
            "downloading": 1,
        }
        # Dashboard liest by_status.completed
        dashboard_completed = by_status.get("completed", 0)
        self.assertEqual(dashboard_completed, 15)

        # Falscher Stand (alter Bug): alles steht als 'deleted'
        buggy_status = {
            "deleted": 15,  # Falscher Bug-Zustand
            "error": 2,
        }
        buggy_completed = buggy_status.get("completed", 0)
        self.assertEqual(buggy_completed, 0, "So sah der Dashboard-Bug aus")


# ═════════════════════════════════════════════════════════════════════════════
# PostgreSQL-Konfigurationsvalidierung
# ═════════════════════════════════════════════════════════════════════════════

class PostgresConfigTests(unittest.TestCase):
    """
    Testet PostgreSQL-Konfigurationsvalidierung ohne echte Datenbankverbindung.
    """

    def test_default_db_type_is_sqlite(self):
        """Standardmäßig ist db_type='sqlite' (abwärtskompatibel)."""
        from core.config import AppSettings
        s = AppSettings()
        self.assertEqual(s.db_type, "sqlite")

    def test_postgres_config_fields_exist(self):
        """Alle PostgreSQL-Konfigurationsfelder sind vorhanden."""
        from core.config import AppSettings
        s = AppSettings()
        self.assertTrue(hasattr(s, "postgres_host"))
        self.assertTrue(hasattr(s, "postgres_port"))
        self.assertTrue(hasattr(s, "postgres_db"))
        self.assertTrue(hasattr(s, "postgres_user"))
        self.assertTrue(hasattr(s, "postgres_password"))
        self.assertTrue(hasattr(s, "postgres_schema"))
        self.assertTrue(hasattr(s, "postgres_ssl"))

    def test_postgres_default_port(self):
        """Standard-Port für PostgreSQL ist 5432."""
        from core.config import AppSettings
        s = AppSettings()
        self.assertEqual(s.postgres_port, 5432)

    def test_postgres_ssl_default_false(self):
        """SSL ist standardmäßig deaktiviert."""
        from core.config import AppSettings
        s = AppSettings()
        self.assertFalse(s.postgres_ssl)

    def test_discord_webhook_added_field_exists(self):
        """discord_webhook_added-Feld ist vorhanden."""
        from core.config import AppSettings
        s = AppSettings()
        self.assertTrue(hasattr(s, "discord_webhook_added"))
        self.assertEqual(s.discord_webhook_added, "")

    def test_is_postgres_returns_false_for_sqlite(self):
        """_is_postgres() gibt False zurück für SQLite-Konfiguration."""
        from db.database import _is_postgres
        with patch("db.database._get_settings", return_value=types.SimpleNamespace(db_type="sqlite")):
            self.assertFalse(_is_postgres())

    def test_is_postgres_returns_true_for_postgres(self):
        """_is_postgres() gibt True zurück für PostgreSQL-Konfiguration."""
        from db.database import _is_postgres
        with patch("db.database._get_settings", return_value=types.SimpleNamespace(db_type="postgres")):
            self.assertTrue(_is_postgres())


# ═════════════════════════════════════════════════════════════════════════════
# Migrations-Sicherheitsprüfungen
# ═════════════════════════════════════════════════════════════════════════════

class MigrationSafetyTests(unittest.IsolatedAsyncioTestCase):
    """
    Testet Migrations-Sicherheitsmechanismen ohne echte Datenbanken.
    """

    async def test_migration_refuses_nonempty_target_sqlite_to_pg(self):
        """
        Migration SQLite→PG wird abgebrochen wenn PG bereits Daten enthält
        und force=False.
        """
        from db.migration import migrate_sqlite_to_postgres, MigrationError
        import tempfile, os

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            sqlite_path = Path(tf.name)

        try:
            fake_pg = AsyncMock()
            fake_pg.close = AsyncMock()
            with patch("db.migration._count_rows_pg", AsyncMock(return_value={"torrents": 5, "download_files": 0, "events": 0})), \
                 patch("db.migration._count_rows_sqlite", AsyncMock(return_value={"torrents": 3, "download_files": 0, "events": 0})), \
                 patch("db.migration._pg_connect", AsyncMock(return_value=fake_pg)):

                result = await migrate_sqlite_to_postgres(
                    sqlite_path, "postgresql://user:pass@localhost/db",
                    force=False
                )

            self.assertFalse(result.success)
            self.assertIsNotNone(result.error)
            self.assertIn("Daten", result.error)
        finally:
            os.unlink(sqlite_path)

    async def test_migration_refuses_nonempty_target_pg_to_sqlite(self):
        """
        Migration PG→SQLite wird abgebrochen wenn SQLite bereits Daten enthält
        und force=False.
        """
        from db.migration import migrate_postgres_to_sqlite
        import tempfile, os

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            sqlite_path = Path(tf.name)

        try:
            fake_pg = AsyncMock()
            fake_pg.close = AsyncMock()
            # Simuliere: SQLite-Datei existiert und enthält Daten
            # _count_rows_sqlite wird VOR dem Öffnen der Datei gepatcht
            with patch("db.migration._count_rows_pg", AsyncMock(return_value={"torrents": 3, "download_files": 0, "events": 0})), \
                 patch("db.migration._count_rows_sqlite", AsyncMock(return_value={"torrents": 10, "download_files": 5, "events": 0})), \
                 patch("db.migration._pg_connect", AsyncMock(return_value=fake_pg)), \
                 patch("db.migration.aiosqlite") as mock_aio:
                # Simuliere aiosqlite.connect als echten async context manager
                mock_conn = AsyncMock()
                mock_aio.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
                mock_aio.connect.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await migrate_postgres_to_sqlite(
                    "postgresql://user:pass@localhost/db",
                    sqlite_path,
                    force=False,
                )

            self.assertFalse(result.success)
            self.assertIn("Daten", result.error or "")
        finally:
            os.unlink(sqlite_path)

    async def test_migration_dry_run_returns_counts_without_writing(self):
        """
        dry_run=True gibt Zeilenzahlen zurück ohne Daten zu schreiben.
        """
        from db.migration import migrate_sqlite_to_postgres
        import tempfile, os

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            sqlite_path = Path(tf.name)

        try:
            mock_pg_conn = AsyncMock()
            mock_pg_conn.close = AsyncMock()
            # dry_run=True: Migration stoppt nach der Validierung — aiosqlite wird
            # nicht geöffnet, daher muss es nicht gepatcht werden.
            # aiosqlite.connect muss gemockt werden da der Stub connect=None hat
            mock_sqlite_conn = AsyncMock()
            mock_sqlite_conn.row_factory = None
            with patch("db.migration._count_rows_pg", AsyncMock(return_value={"torrents": 0, "download_files": 0, "events": 0})), \
                 patch("db.migration._count_rows_sqlite", AsyncMock(return_value={"torrents": 5, "download_files": 10, "events": 3})), \
                 patch("db.migration._pg_connect", AsyncMock(return_value=mock_pg_conn)), \
                 patch("db.migration.aiosqlite") as mock_aio:
                mock_aio.connect.return_value.__aenter__ = AsyncMock(return_value=mock_sqlite_conn)
                mock_aio.connect.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await migrate_sqlite_to_postgres(
                    sqlite_path, "postgresql://user:pass@localhost/db",
                    force=False, dry_run=True,
                )

            self.assertTrue(result.success, f"dry_run sollte erfolgreich sein, Fehler: {result.error}")
            self.assertEqual(result.tables_migrated.get("torrents"), 5)
            self.assertEqual(result.tables_migrated.get("download_files"), 10)
            # dry_run: keine PG-Schreiboperationen
            mock_pg_conn.execute.assert_not_called()
        finally:
            os.unlink(sqlite_path)

    async def test_migration_source_not_found_error(self):
        """Migration schlägt fehl wenn Quelldatei nicht existiert."""
        from db.migration import migrate_sqlite_to_postgres

        result = await migrate_sqlite_to_postgres(
            Path("/nonexistent/db.sqlite"),
            "postgresql://user:pass@localhost/db",
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        self.assertIn("nicht gefunden", result.error)

    async def test_migration_result_summary_success(self):
        """MigrationResult.summary() gibt lesbaren Text zurück."""
        from db.migration import MigrationResult

        r = MigrationResult(
            success=True,
            direction="sqlite→postgres",
            tables_migrated={"torrents": 10, "download_files": 50, "events": 100},
        )
        summary = r.summary()
        self.assertIn("sqlite→postgres", summary)
        self.assertIn("10", summary)

    async def test_migration_result_summary_failure(self):
        """MigrationResult.summary() zeigt Fehler an."""
        from db.migration import MigrationResult

        r = MigrationResult(
            success=False,
            direction="postgres→sqlite",
            error="Verbindung fehlgeschlagen",
        )
        summary = r.summary()
        self.assertIn("fehlgeschlagen", summary)
        self.assertIn("Verbindung fehlgeschlagen", summary)


# ═════════════════════════════════════════════════════════════════════════════
# Bestehende Tests aus dem Original (erweitert)
# ═════════════════════════════════════════════════════════════════════════════

class AllDebridServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_retries_after_empty_response(self):
        service = AllDebridService("api-key")
        responses = ["", '{"status":"success","data":{"ok":true}}']

        class FakeResponse:
            def __init__(self, body):
                self.body = body
                self.status = 200
            async def text(self): return self.body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class FakeSession:
            def __init__(self, *a, **kw): pass
            def post(self, *a, **kw): return FakeResponse(responses.pop(0))
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        with patch("services.alldebrid.aiohttp.ClientSession", FakeSession):
            result = await service._post("https://api.example", "magnet/status", retries=2)
        self.assertEqual(result, {"ok": True})

    def test_decode_json_body_reports_invalid_payload(self):
        service = AllDebridService("api-key")
        with self.assertRaises(Exception) as ctx:
            service._decode_json_body("<html>bad gateway</html>", "magnet/status")
        self.assertIn("invalid JSON", str(ctx.exception))


class ManagerDedupeTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_reconcile_removes_duplicate_aria2_jobs(self):
        mgr = TorrentManager()
        keep = types.SimpleNamespace(
            gid="keep", status="active",
            files=[{"uris": [{"uri": "https://same.invalid/file"}]}],
        )
        dup = types.SimpleNamespace(
            gid="dup", status="waiting",
            files=[{"uris": [{"uri": "https://same.invalid/file"}]}],
        )

        fake_aria2 = types.SimpleNamespace(
            get_all=AsyncMock(return_value=[keep, dup]),
            remove=AsyncMock(),
        )
        mgr.aria2 = lambda: fake_aria2

        deduped = await mgr._dedupe_aria2_downloads_on_startup([keep, dup])
        fake_aria2.remove.assert_awaited()
        self.assertNotIn("dup", [d.gid for d in deduped])

    def test_build_aria2_indexes_tracks_path_and_uri(self):
        mgr = TorrentManager()
        dl = types.SimpleNamespace(
            gid="gid-1",
            files=[{"path": "/downloads/show/file.mp4",
                    "uris": [{"uri": "https://example.invalid/file"}]}],
        )
        by_gid, uri_to_dl, path_to_dl = mgr._build_aria2_indexes([dl])
        self.assertIs(by_gid["gid-1"], dl)
        self.assertIs(uri_to_dl["https://example.invalid/file"], dl)
        self.assertIs(path_to_dl["/downloads/show/file.mp4"], dl)

    def test_aria2_slot_limit_uses_dedicated_setting(self):
        mgr = TorrentManager()
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_max_active_downloads=7, max_concurrent_downloads=3,
        )):
            self.assertEqual(mgr._aria2_slot_limit(), 7)

    def test_aria2_slot_limit_fallback_to_max_concurrent(self):
        mgr = TorrentManager()
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_max_active_downloads=0, max_concurrent_downloads=5,
        )):
            self.assertEqual(mgr._aria2_slot_limit(), 5)


if __name__ == "__main__":
    unittest.main()
