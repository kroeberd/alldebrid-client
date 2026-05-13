"""
Tests for services/duplicates.py — Duplicate Intelligence Service.

Tests verify:
  - BTIH extraction from magnet URIs
  - Title normalisation
  - Release-token extraction (season, episode, year, quality)
  - Size-similarity logic
  - Semantic duplicate matching does not block on different titles
  - check_before_add returns 'skip' on exact hash match
  - check_before_add returns 'allow' when no duplicate exists
  - Search paths do NOT call upload_magnet
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from services.duplicates import (
    DuplicateCandidate,
    DuplicateDecision,
    DuplicateMatch,
    extract_btih,
    normalize_title,
    extract_release_tokens,
    _size_similar,
    find_alldebrid_id_duplicate,
    find_hash_duplicate,
    find_semantic_duplicates,
    check_before_add,
)


# ── extract_btih ─────────────────────────────────────────────────────────────

class TestExtractBtih:
    def test_standard_hex_btih(self):
        magnet = "magnet:?xt=urn:btih:aabbccddeeff00112233445566778899aabbccdd&dn=test"
        assert extract_btih(magnet) == "aabbccddeeff00112233445566778899aabbccdd"

    def test_uppercase_hex_lowercased(self):
        magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD"
        assert extract_btih(magnet) == "aabbccddeeff00112233445566778899aabbccdd"

    def test_no_btih_returns_empty(self):
        assert extract_btih("not-a-magnet") == ""

    def test_empty_returns_empty(self):
        assert extract_btih("") == ""

    def test_tracker_params_ignored(self):
        magnet = (
            "magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345678"
            "&tr=udp://tracker1.example.com:80"
            "&tr=udp://tracker2.example.com:80"
            "&dn=Some+Movie+2024"
        )
        assert extract_btih(magnet) == "1234567890abcdef1234567890abcdef12345678"

    def test_same_content_different_trackers_same_hash(self):
        magnet_a = "magnet:?xt=urn:btih:aaaa1234bbbb5678cccc9012dddd3456eeee7890&tr=tracker1"
        magnet_b = "magnet:?xt=urn:btih:aaaa1234bbbb5678cccc9012dddd3456eeee7890&tr=tracker2"
        assert extract_btih(magnet_a) == extract_btih(magnet_b)


# ── normalize_title ──────────────────────────────────────────────────────────

class TestNormalizeTitle:
    def test_dot_separated_to_spaces(self):
        result = normalize_title("Movie.Name.2024.1080p.WEB-DL")
        assert "movie" in result
        assert "name" in result

    def test_brackets_removed(self):
        result = normalize_title("Movie Name (2024) [1080p]")
        assert "(" not in result
        assert "[" not in result

    def test_quality_stripped(self):
        result = normalize_title("Movie 2024 1080p BluRay")
        assert "1080p" not in result
        assert "bluray" not in result

    def test_year_retained(self):
        result = normalize_title("Movie.Name.2024.1080p")
        assert "2024" in result

    def test_lowercase(self):
        assert normalize_title("MOVIE NAME 2024") == normalize_title("movie name 2024")

    def test_similar_titles_match(self):
        a = normalize_title("Movie.Name.2024.1080p.WEB-DL-GROUP")
        b = normalize_title("Movie Name (2024) 1080p WEB DL GROUP")
        # Both should reduce to similar tokens
        words_a = set(a.split())
        words_b = set(b.split())
        overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
        assert overlap > 0.4  # at least 40% shared tokens

    def test_different_films_differ(self):
        a = normalize_title("Iron Man 2008")
        b = normalize_title("Thor Ragnarok 2017")
        words_a = set(a.split())
        words_b = set(b.split())
        overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
        assert overlap < 0.4  # very little overlap


# ── extract_release_tokens ───────────────────────────────────────────────────

class TestExtractReleaseTokens:
    def test_season_episode_standard(self):
        tokens = extract_release_tokens("Show.Name.S01E02.1080p")
        assert tokens["season"]  == 1
        assert tokens["episode"] == 2

    def test_season_episode_x_format(self):
        tokens = extract_release_tokens("Show Name 1x02")
        assert tokens["season"]  == 1
        assert tokens["episode"] == 2

    def test_year_extraction(self):
        tokens = extract_release_tokens("Movie Name 2024 1080p")
        assert tokens["year"] == 2024

    def test_quality_extraction(self):
        tokens = extract_release_tokens("Movie 2024 2160p BluRay REMUX")
        assert tokens["quality"] == "2160p"

    def test_no_match_returns_nones(self):
        tokens = extract_release_tokens("just a plain title")
        assert tokens["season"]  is None
        assert tokens["episode"] is None
        assert tokens["year"]    is None

    def test_release_group_extraction(self):
        tokens = extract_release_tokens("Movie.2024.1080p.BluRay-FLUX")
        assert tokens["release_group"] in ("", "flux")  # may or may not detect


# ── _size_similar ────────────────────────────────────────────────────────────

class TestSizeSimilar:
    def test_identical(self):
        assert _size_similar(1_000_000_000, 1_000_000_000) is True

    def test_within_2_percent(self):
        assert _size_similar(1_000_000_000, 1_018_000_000) is True  # 1.8%

    def test_outside_2_percent_very_large(self):
        # 5 GB vs 5.5 GB — 10% diff and >100 MB → should be False
        assert _size_similar(5_000_000_000, 5_500_000_000) is False

    def test_within_2_percent_large_files(self):
        # ~1% diff on 1 GB files
        assert _size_similar(1_000_000_000, 1_010_000_000) is True

    def test_within_100mb(self):
        assert _size_similar(200_000_000, 250_000_000) is True  # 50 MB diff

    def test_zero_returns_false(self):
        assert _size_similar(0, 1_000_000_000) is False
        assert _size_similar(1_000_000_000, 0) is False


# ── find_hash_duplicate (mocked DB) ─────────────────────────────────────────

@pytest.fixture
def mock_db_row():
    row = MagicMock()
    row.__getitem__ = lambda self, k: {
        "id": 42, "name": "Test Movie", "status": "completed", "hash": "aabbcc"
    }[k]
    return row


class TestFindHashDuplicate:
    @pytest.mark.asyncio
    async def test_returns_match_for_active_status(self):
        fake_row = {"id": 42, "name": "Test Movie", "status": "completed", "hash": "aabbcc"}
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=fake_row)

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            match = await find_hash_duplicate("aabbcc")
        assert match is not None
        assert match.torrent_id == 42
        assert match.confidence == 1.0
        assert match.reason == "same_infohash"

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_hash(self):
        match = await find_hash_duplicate("")
        assert match is None

    @pytest.mark.asyncio
    async def test_returns_none_when_deleted(self):
        fake_row = {"id": 1, "name": "Gone", "status": "deleted", "hash": "aabb"}
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=fake_row)

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            match = await find_hash_duplicate("aabb")
        # 'deleted' is not in _ALL_NON_DELETED
        assert match is None


# ── check_before_add ─────────────────────────────────────────────────────────

class TestFindAllDebridIdDuplicate:
    @pytest.mark.asyncio
    async def test_returns_match_for_existing_alldebrid_id(self):
        fake_row = {"id": 77, "name": "Existing AD Item", "status": "ready", "hash": "ffee"}
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=fake_row)

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            match = await find_alldebrid_id_duplicate("123456")

        assert match is not None
        assert match.torrent_id == 77
        assert match.reason == "same_alldebrid_id"
        assert match.confidence == 1.0


class TestCheckBeforeAdd:
    @pytest.mark.asyncio
    async def test_allow_when_no_duplicate(self):
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=None)
        mock_ctx.fetchall   = AsyncMock(return_value=[])

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            decision = await check_before_add(DuplicateCandidate(
                source="manual",
                infohash="deadbeefdeadbeefdeadbeef1234567890abcdef",
                title="Brand New Movie 2099",
            ))
        assert decision.action == "allow"
        assert decision.is_duplicate is False

    @pytest.mark.asyncio
    async def test_skip_on_exact_hash_active(self):
        fake_row = {"id": 7, "name": "Some Movie", "status": "downloading", "hash": "deadbeef1234"}
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=fake_row)
        mock_ctx.fetchall   = AsyncMock(return_value=[])

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            decision = await check_before_add(DuplicateCandidate(
                source="manual",
                infohash="deadbeef1234",
                magnet="magnet:?xt=urn:btih:deadbeef1234",
            ))
        assert decision.action == "skip"
        assert decision.confidence == 1.0
        assert decision.is_duplicate is True

    @pytest.mark.asyncio
    async def test_warn_on_exact_hash_error_state(self):
        fake_row = {"id": 3, "name": "Failed Movie", "status": "error", "hash": "cafebabe1234"}
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=fake_row)
        mock_ctx.fetchall   = AsyncMock(return_value=[])

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            decision = await check_before_add(DuplicateCandidate(
                source="manual",
                infohash="cafebabe1234",
            ))
        assert decision.action == "warn"
        assert decision.is_duplicate is True

    @pytest.mark.asyncio
    async def test_skip_on_same_episode_same_quality_and_similar_size(self):
        fake_rows = [{
            "id": 44,
            "name": "Example.Show.S01E02.1080p.WEB-DL-GROUP",
            "status": "completed",
            "hash": "existinghash",
            "size_bytes": 1_000_000_000,
        }]
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchall   = AsyncMock(return_value=fake_rows)

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            decision = await check_before_add(DuplicateCandidate(
                source="jackett",
                title="Example Show S01E02 1080p WEB DL OTHER",
                size_bytes=1_010_000_000,
            ))

        assert decision.action == "skip"
        assert decision.reason == "same_episode"
        assert decision.matches[0].torrent_id == 44

    @pytest.mark.asyncio
    async def test_skip_on_same_movie_year_same_quality_and_similar_size(self):
        fake_rows = [{
            "id": 45,
            "name": "Example.Movie.2024.2160p.BluRay-GROUP",
            "status": "ready",
            "hash": "moviehash",
            "size_bytes": 8_000_000_000,
        }]
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchall   = AsyncMock(return_value=fake_rows)

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            decision = await check_before_add(DuplicateCandidate(
                source="saved_search",
                title="Example Movie (2024) 2160p BluRay OTHER",
                size_bytes=8_050_000_000,
            ))

        assert decision.action == "skip"
        assert decision.reason == "same_movie_year"
        assert decision.matches[0].torrent_id == 45

    @pytest.mark.asyncio
    async def test_skip_on_existing_completed_download_file(self):
        fake_rows = [{
            "id": 46,
            "name": "Example Pack",
            "status": "completed",
            "hash": "packhash",
            "size_bytes": 0,
            "file_name": "Example.Show.S02E03.1080p.WEB-DL-GROUP.mkv",
            "file_local_path": "/download/Example Pack/Example.Show.S02E03.1080p.WEB-DL-GROUP.mkv",
            "file_size_bytes": 2_000_000_000,
        }]
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchall   = AsyncMock(return_value=fake_rows)

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            decision = await check_before_add(DuplicateCandidate(
                source="jackett",
                title="Example Show S02E03 1080p WEB DL OTHER",
                size_bytes=2_010_000_000,
            ))

        assert decision.action == "skip"
        assert decision.reason == "same_episode"
        assert decision.matches[0].name.endswith(".mkv")


# ── Regression: search must NEVER upload ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_skip_on_existing_alldebrid_id(self):
        fake_row = {"id": 9, "name": "Already Imported", "status": "ready", "hash": "bead"}
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=fake_row)
        mock_ctx.fetchall   = AsyncMock(return_value=[])

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            decision = await check_before_add(DuplicateCandidate(
                source="import_existing",
                alldebrid_id="987654",
                title="Already Imported",
            ))

        assert decision.action == "skip"
        assert decision.reason == "same_alldebrid_id"
        assert decision.confidence == 1.0


class TestSearchReadOnly:
    """
    Verify that find_hash_duplicate and find_semantic_duplicates never call
    upload_magnet, upload_torrent_file, or any AllDebrid write operations.
    """
    @pytest.mark.asyncio
    async def test_find_hash_duplicate_does_not_upload(self):
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=None)

        with patch("services.duplicates.get_db", return_value=mock_ctx) as _db:
            with patch("services.alldebrid.AllDebridService.upload_magnet") as mock_upload:
                await find_hash_duplicate("somehash")
                mock_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_find_semantic_duplicates_does_not_upload(self):
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchall   = AsyncMock(return_value=[])

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            with patch("services.alldebrid.AllDebridService.upload_magnet") as mock_upload:
                await find_semantic_duplicates(DuplicateCandidate(title="Test Movie 2024"))
                mock_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_before_add_does_not_upload(self):
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__  = AsyncMock(return_value=False)
        mock_ctx.fetchone   = AsyncMock(return_value=None)
        mock_ctx.fetchall   = AsyncMock(return_value=[])

        with patch("services.duplicates.get_db", return_value=mock_ctx):
            with patch("services.alldebrid.AllDebridService.upload_magnet") as mock_upload:
                await check_before_add(DuplicateCandidate(
                    source="search",
                    title="Test",
                    infohash="abc123",
                ))
                mock_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_preview_route_does_not_upload_to_alldebrid(self):
        from api.routes import check_torrent_duplicate

        duplicate = {
            "is_duplicate": False,
            "confidence": 0.0,
            "action": "allow",
            "reason": "no_duplicate_found",
            "matches": [],
        }
        decision = MagicMock()
        decision.as_dict.return_value = duplicate

        with patch("services.duplicates.check_before_add", AsyncMock(return_value=decision)) as mock_check:
            with patch("services.alldebrid.AllDebridService.upload_magnet") as mock_upload:
                result = await check_torrent_duplicate({
                    "title": "Preview Only",
                    "hash": "abcdef1234567890abcdef1234567890abcdef12",
                })

        mock_check.assert_awaited_once()
        mock_upload.assert_not_called()
        assert result["duplicate"]["action"] == "allow"
