"""Tests for the Jackett integration service."""
import asyncio
import sys, types
for mod, stub in {
    "aiohttp": types.SimpleNamespace(
        ClientSession=object, ClientTimeout=lambda **k: None,
        ClientConnectorError=Exception, ClientError=Exception, FormData=object,
    ),
    "aiofiles": types.SimpleNamespace(open=lambda *a, **kw: None),
    "aiosqlite": types.SimpleNamespace(connect=None, Row=object),
    "asyncpg":   types.SimpleNamespace(connect=None),
}.items():
    if mod not in sys.modules:
        sys.modules[mod] = stub

from services.jackett import (
    _normalise_result, _fmt_size, CATEGORIES, CATEGORY_ALL, _parse_torznab_indexers
)


class TestFmtSize:
    def test_zero(self):
        assert _fmt_size(0) == "—"

    def test_negative(self):
        assert _fmt_size(-1) == "—"

    def test_bytes(self):
        assert "B" in _fmt_size(512)

    def test_megabytes(self):
        result = _fmt_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = _fmt_size(2 * 1024 ** 3)
        assert "GB" in result


class TestNormaliseResult:
    def _make(self, **kwargs):
        base = {
            "Title": "Test Torrent",
            "Tracker": "MyTracker",
            "Size": 1073741824,   # 1 GB
            "Seeders": 42,
            "Peers": 5,
            "MagnetUri": "magnet:?xt=urn:btih:abc123",
            "Link": "",
            "PublishDate": "2024-01-15T10:00:00Z",
            "CategoryDesc": "Movies",
        }
        base.update(kwargs)
        return base

    def test_basic_fields(self):
        r = _normalise_result(self._make())
        assert r["title"] == "Test Torrent"
        assert r["indexer"] == "MyTracker"
        assert r["seeders"] == 42
        assert r["leechers"] == 5
        assert r["category"] == "Movies"

    def test_magnet_preferred(self):
        r = _normalise_result(self._make(
            MagnetUri="magnet:?xt=urn:btih:abc",
            Link="http://example.com/file.torrent"
        ))
        assert r["magnet"] == "magnet:?xt=urn:btih:abc"
        assert r["has_link"] is True

    def test_torrent_url_fallback(self):
        r = _normalise_result(self._make(MagnetUri="", Link="http://example.com/file.torrent"))
        assert r["magnet"] == ""
        assert r["torrent_url"] == "http://example.com/file.torrent"
        assert r["has_link"] is True

    def test_hash_taken_from_infohash(self):
        r = _normalise_result(self._make(InfoHash="ABCDEF1234567890"))
        assert r["hash"] == "abcdef1234567890"

    def test_hash_falls_back_to_magnet_btih(self):
        r = _normalise_result(self._make(
            MagnetUri="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            InfoHash="",
        ))
        assert r["hash"] == "0123456789abcdef0123456789abcdef01234567"

    def test_no_link(self):
        r = _normalise_result(self._make(MagnetUri="", Link=""))
        assert r["has_link"] is False

    def test_pub_date_parsed(self):
        r = _normalise_result(self._make(PublishDate="2024-06-01T00:00:00Z"))
        assert r["pub_date"] == "2024-06-01"

    def test_pub_date_empty(self):
        r = _normalise_result(self._make(PublishDate=""))
        assert r["pub_date"] == ""

    def test_pub_date_malformed(self):
        r = _normalise_result(self._make(PublishDate="not-a-date"))
        assert isinstance(r["pub_date"], str)

    def test_size_human(self):
        r = _normalise_result(self._make(Size=1073741824))
        assert "GB" in r["size_human"]

    def test_missing_optional_fields(self):
        """Should not raise on minimal input."""
        r = _normalise_result({"Title": "Minimal"})
        assert r["title"] == "Minimal"
        assert r["seeders"] == 0
        assert r["has_link"] is False

    def test_leechers_from_peers(self):
        """Peers field used when Leechers absent."""
        r = _normalise_result(self._make(Peers=7))
        assert r["leechers"] == 7


class TestCategories:
    def test_all_category_zero(self):
        assert CATEGORY_ALL == 0
        assert CATEGORIES["All"] == 0

    def test_required_categories_present(self):
        for cat in ("Movies", "TV", "Music", "Books", "Games", "Software", "XXX"):
            assert cat in CATEGORIES

    def test_category_values_positive(self):
        for name, cid in CATEGORIES.items():
            if name != "All":
                assert cid > 0, f"{name} category should have positive ID"


class _FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._data

    async def text(self):
        return str(self._data)


class _FakeSession:
    def __init__(self, responses, *args, **kwargs):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, *args, **kwargs):
        if not self._responses:
            raise AssertionError("No more fake Jackett responses configured")
        return self._responses.pop(0)


class TestConnectionFallback:
    def test_falls_back_to_indexers_when_server_config_is_unavailable(self):
        from services import jackett as jackett_mod

        cfg = types.SimpleNamespace(
            jackett_url="http://jackett:9117",
            jackett_api_key="secret",
        )
        fake_session_factory = lambda *a, **kw: _FakeSession([
            _FakeResponse(404, {"error": "not found"}),
            _FakeResponse(200, [{"id": "tracker-a", "name": "Tracker A"}]),
        ])

        original_cfg = jackett_mod._cfg
        original_session = jackett_mod.aiohttp.ClientSession
        try:
            jackett_mod._cfg = lambda: cfg
            jackett_mod.aiohttp.ClientSession = fake_session_factory
            result = asyncio.run(jackett_mod.test_connection())
        finally:
            jackett_mod._cfg = original_cfg
            jackett_mod.aiohttp.ClientSession = original_session

        assert result["ok"] is True
        assert result["version"] == "reachable (1 indexers)"

    def test_invalid_api_key_on_fallback_is_reported(self):
        from services import jackett as jackett_mod

        cfg = types.SimpleNamespace(
            jackett_url="http://jackett:9117",
            jackett_api_key="wrong",
        )
        fake_session_factory = lambda *a, **kw: _FakeSession([
            _FakeResponse(404, {"error": "not found"}),
            _FakeResponse(401, {"error": "unauthorized"}),
        ])

        original_cfg = jackett_mod._cfg
        original_session = jackett_mod.aiohttp.ClientSession
        try:
            jackett_mod._cfg = lambda: cfg
            jackett_mod.aiohttp.ClientSession = fake_session_factory
            result = asyncio.run(jackett_mod.test_connection())
        finally:
            jackett_mod._cfg = original_cfg
            jackett_mod.aiohttp.ClientSession = original_session

        assert result["ok"] is False
        assert result["error"] == "Invalid API key"

    def test_falls_back_to_results_endpoint_when_other_endpoints_fail(self):
        from services import jackett as jackett_mod

        cfg = types.SimpleNamespace(
            jackett_url="http://jackett:9117",
            jackett_api_key="secret",
        )
        fake_session_factory = lambda *a, **kw: _FakeSession([
            _FakeResponse(404, {"error": "not found"}),
            _FakeResponse(400, {"error": "bad request"}),
            _FakeResponse(400, "bad request"),
            _FakeResponse(200, {"Results": []}),
        ])

        original_cfg = jackett_mod._cfg
        original_session = jackett_mod.aiohttp.ClientSession
        try:
            jackett_mod._cfg = lambda: cfg
            jackett_mod.aiohttp.ClientSession = fake_session_factory
            result = asyncio.run(jackett_mod.test_connection())
        finally:
            jackett_mod._cfg = original_cfg
            jackett_mod.aiohttp.ClientSession = original_session

        assert result["ok"] is True
        assert result["version"] == "reachable"


class TestTorznabIndexers:
    def test_parses_indexers_from_torznab_xml(self):
        xml = """
        <indexers>
          <indexer id="tracker-a" name="Tracker A" />
          <indexer id="tracker-b" name="Tracker B" />
        </indexers>
        """
        items = _parse_torznab_indexers(xml)
        assert items == [
            {"id": "tracker-a", "name": "Tracker A"},
            {"id": "tracker-b", "name": "Tracker B"},
        ]
