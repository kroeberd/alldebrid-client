"""Tests for the Jackett integration service."""
import sys, types
for mod, stub in {
    "aiohttp": types.SimpleNamespace(
        ClientSession=object, ClientTimeout=lambda **k: None,
        ClientConnectorError=Exception, ClientError=Exception,
    ),
    "aiosqlite": types.SimpleNamespace(connect=None, Row=object),
    "asyncpg":   types.SimpleNamespace(connect=None),
}.items():
    if mod not in sys.modules:
        sys.modules[mod] = stub

from services.jackett import (
    _normalise_result, _fmt_size, CATEGORIES, CATEGORY_ALL
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
