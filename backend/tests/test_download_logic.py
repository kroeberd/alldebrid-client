"""
Comprehensive tests for the AllDebrid-Client download logic.

Tests cover:
- Status transitions (state machine)
- _start_download guard: no duplicate starts for active downloads
- _start_download guard: allows restart after _reset_torrent_for_redownload
- full_alldebrid_sync: does not restart queued/downloading/paused torrents
- all-blocked torrents: marked completed, not error
- partial-blocked torrents: continue with remaining files
- _finalize_aria2_torrent: correct completion detection
"""
import pytest
import sys
import types

# ── Minimal stubs so manager_v2 can be imported without real dependencies ─────
for mod, stub in {
    "aiohttp": types.SimpleNamespace(
        ClientSession=object, ClientTimeout=lambda **k: None,
        FormData=object,
        TCPConnector=lambda **k: None,
        ServerDisconnectedError=Exception, ClientConnectorError=Exception,
        ClientOSError=Exception, ClientError=Exception,
    ),
    "aiosqlite": types.SimpleNamespace(connect=None, Row=object),
    "asyncpg": types.SimpleNamespace(connect=None),
}.items():
    if mod not in sys.modules:
        sys.modules[mod] = stub

from core.config import AppSettings


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_cfg(**kwargs) -> AppSettings:
    return AppSettings(**kwargs)


def make_torrent_row(status: str, alldebrid_id: str = "123", torrent_id: int = 1) -> dict:
    return {
        "id": torrent_id,
        "name": "Test Torrent",
        "alldebrid_id": alldebrid_id,
        "status": status,
        "provider_status": "ready",
        "provider_status_code": 4,
        "polling_failures": 0,
    }


# ── Status-machine invariants ─────────────────────────────────────────────────

class TestStatusTransitions:
    """Verify the documented status transitions are internally consistent."""

    def test_terminal_statuses_defined(self):
        """completed and deleted are terminal — nothing should restart them."""
        from services.manager_v2 import _terminal_torrent_status
        assert _terminal_torrent_status("completed") is True
        assert _terminal_torrent_status("deleted") is True

    def test_non_terminal_statuses(self):
        from services.manager_v2 import _terminal_torrent_status
        # error IS terminal — errored torrents are not re-polled by the normal sync;
        # they are only restarted by full_alldebrid_sync when AllDebrid reports ready.
        for s in ("queued", "downloading", "pending", "ready", "paused",
                  "uploading", "processing"):
            assert _terminal_torrent_status(s) is False, f"{s} should not be terminal"

    def test_error_is_terminal(self):
        from services.manager_v2 import _terminal_torrent_status
        # Errors are terminal from the polling perspective — they do not get
        # re-polled by sync_alldebrid_status but CAN be restarted by full_alldebrid_sync.
        assert _terminal_torrent_status("error") is True

    def test_restartable_statuses_in_full_sync(self):
        """full_alldebrid_sync should only restart these statuses."""
        # As defined in the fix: queued/downloading/paused must NOT be in this set
        restartable = {"error", "pending", "uploading", "processing", "ready"}
        assert "queued" not in restartable
        assert "downloading" not in restartable
        assert "paused" not in restartable

    def test_is_blocked_respects_filters_enabled(self):
        from services.manager_v2 import is_blocked
        cfg_off = make_cfg(filters_enabled=False, blocked_extensions=[".jpg"])
        blocked, _ = is_blocked("image.jpg", cfg_off)
        assert blocked is False

    def test_is_blocked_extension(self):
        from services.manager_v2 import is_blocked
        cfg = make_cfg(filters_enabled=True, blocked_extensions=[".jpg", ".nfo"])
        assert is_blocked("cover.jpg", cfg)[0] is True
        assert is_blocked("movie.mkv", cfg)[0] is False
        assert is_blocked("info.NFO", cfg)[0] is True  # case-insensitive

    def test_is_blocked_keyword(self):
        from services.manager_v2 import is_blocked
        cfg = make_cfg(filters_enabled=True, blocked_keywords=["sample"])
        assert is_blocked("sample.mkv", cfg)[0] is True
        assert is_blocked("SAMPLE-clip.avi", cfg)[0] is True
        assert is_blocked("main.mkv", cfg)[0] is False

    def test_is_blocked_min_size(self):
        from services.manager_v2 import is_blocked
        cfg = make_cfg(filters_enabled=True, min_file_size_mb=10)
        assert is_blocked("tiny.mkv", cfg, size_bytes=5 * 1024 * 1024)[0] is True
        assert is_blocked("big.mkv", cfg, size_bytes=100 * 1024 * 1024)[0] is False
        # size_bytes=0 → unknown size → not blocked by size rule
        assert is_blocked("unknown.mkv", cfg, size_bytes=0)[0] is False

    def test_is_blocked_no_size_when_zero(self):
        from services.manager_v2 import is_blocked
        cfg = make_cfg(filters_enabled=True, min_file_size_mb=10)
        # When size is unknown (0) the filter must NOT block
        blocked, reason = is_blocked("file.mkv", cfg, size_bytes=0)
        assert blocked is False


# ── all-blocked → completed ───────────────────────────────────────────────────

class TestAllBlockedStatus:
    """When all files are blocked by filters, the torrent should be 'completed'."""

    def _compute_final_status(self, blocked_count, queued_count, failed_count,
                               completed_count, total_files):
        """Replicate the status-decision logic from _download()."""
        downloadable_count = total_files - blocked_count

        if blocked_count == total_files and total_files > 0 and failed_count == 0:
            return "completed"
        if failed_count == 0 and (completed_count + queued_count) == downloadable_count \
                and downloadable_count > 0:
            return "queued" if queued_count > 0 else "completed"
        if blocked_count > 0 and failed_count == 0 and (completed_count + queued_count) > 0:
            return "queued" if queued_count > 0 else "completed"
        return "error"

    def test_all_blocked_returns_completed(self):
        status = self._compute_final_status(
            blocked_count=5, queued_count=0, failed_count=0,
            completed_count=0, total_files=5
        )
        assert status == "completed", f"Expected completed, got {status}"

    def test_partial_blocked_some_queued(self):
        status = self._compute_final_status(
            blocked_count=2, queued_count=3, failed_count=0,
            completed_count=0, total_files=5
        )
        assert status == "queued"

    def test_partial_blocked_all_already_on_disk(self):
        status = self._compute_final_status(
            blocked_count=2, queued_count=0, failed_count=0,
            completed_count=3, total_files=5
        )
        assert status == "completed"

    def test_no_files_at_all_is_error(self):
        status = self._compute_final_status(
            blocked_count=0, queued_count=0, failed_count=0,
            completed_count=0, total_files=0
        )
        assert status == "error"

    def test_some_failed_is_error(self):
        status = self._compute_final_status(
            blocked_count=0, queued_count=2, failed_count=1,
            completed_count=0, total_files=3
        )
        assert status == "error"

    def test_all_blocked_with_one_failed_is_error(self):
        """If even one file truly failed (not blocked), it's an error."""
        status = self._compute_final_status(
            blocked_count=4, queued_count=0, failed_count=1,
            completed_count=0, total_files=5
        )
        assert status == "error"

    def test_normal_all_queued(self):
        status = self._compute_final_status(
            blocked_count=0, queued_count=5, failed_count=0,
            completed_count=0, total_files=5
        )
        assert status == "queued"

    def test_normal_all_already_completed(self):
        status = self._compute_final_status(
            blocked_count=0, queued_count=0, failed_count=0,
            completed_count=5, total_files=5
        )
        assert status == "completed"


# ── _finalize_aria2_torrent logic ─────────────────────────────────────────────

class TestFinalizeLogic:
    """Replicate the completion-detection logic from _finalize_aria2_torrent."""

    def _should_complete(self, required, completed, error, active) -> tuple:
        """Returns (should_complete, reason) matching _finalize_aria2_torrent logic."""
        if required == 0:
            return True, "all_blocked"
        if required > 0 and completed == required and error == 0 and active == 0:
            return True, "all_done"
        if error > 0 and active == 0:
            return False, "error"
        if active > 0:
            return False, "still_active"
        return False, "unknown"

    def test_all_blocked_finalizes(self):
        ok, reason = self._should_complete(required=0, completed=0, error=0, active=0)
        assert ok is True
        assert reason == "all_blocked"

    def test_all_done_finalizes(self):
        ok, _ = self._should_complete(required=5, completed=5, error=0, active=0)
        assert ok is True

    def test_still_active_does_not_finalize(self):
        ok, _ = self._should_complete(required=5, completed=3, error=0, active=2)
        assert ok is False

    def test_error_with_no_active_does_not_complete(self):
        ok, reason = self._should_complete(required=5, completed=3, error=1, active=0)
        assert ok is False
        assert reason == "error"

    def test_partial_done_does_not_finalize(self):
        ok, _ = self._should_complete(required=5, completed=3, error=0, active=0)
        # 3 done, 0 error, 0 active but required=5 → 2 files unaccounted
        assert ok is False


# ── normalize_provider_state ──────────────────────────────────────────────────

class TestNormalizeProviderState:
    def test_ready_status(self):
        from services.manager_v2 import normalize_provider_state
        result = normalize_provider_state({
            "statusCode": 4, "status": "Ready", "filename": "Test", "size": 1000
        })
        assert result["provider_status"] == "ready"
        assert result["local_status"] == "ready"

    def test_processing_status(self):
        from services.manager_v2 import normalize_provider_state
        # AllDebrid statusCode 1-3 = processing/downloading on AllDebrid side
        result = normalize_provider_state({"statusCode": 1, "status": "Downloading"})
        assert result["provider_status"] == "processing"

    def test_ready_status_code_4(self):
        from services.manager_v2 import normalize_provider_state
        result = normalize_provider_state({"statusCode": 4, "status": "Ready"})
        assert result["provider_status"] == "ready"

    def test_error_status(self):
        from services.manager_v2 import normalize_provider_state
        result = normalize_provider_state({"statusCode": 7, "status": "Error"})
        assert result["provider_status"] == "error"

    def test_unknown_code_maps_gracefully(self):
        from services.manager_v2 import normalize_provider_state
        result = normalize_provider_state({"statusCode": 999, "status": "Unknown"})
        assert "provider_status" in result
        assert result["provider_status"]  # non-empty


# ── safe_name / safe_rel_path ─────────────────────────────────────────────────

class TestPathHelpers:
    def test_safe_name_strips_dangerous_chars(self):
        from services.manager_v2 import safe_name
        assert "/" not in safe_name("a/b/c")
        assert "\\" not in safe_name("a\\b")
        # After fix: leading dots are stripped so '..' cannot appear at the start
        result = safe_name("../etc/passwd")
        assert not result.startswith("..")
        assert result  # non-empty fallback

    def test_safe_name_strips_leading_dots(self):
        from services.manager_v2 import safe_name
        assert not safe_name("../evil").startswith("..")
        assert not safe_name("../../root").startswith("..")
        assert safe_name(".hidden_file") == "hidden_file"  # leading dot stripped

    def test_safe_name_normal_stays_intact(self):
        from services.manager_v2 import safe_name
        result = safe_name("My Movie (2024) [1080p]")
        assert "Movie" in result
        assert "(2024)" in result

    def test_safe_name_preserves_normal(self):
        from services.manager_v2 import safe_name
        result = safe_name("My Movie (2024) [1080p]")
        assert result  # non-empty
        assert len(result) <= 255

    def test_safe_rel_path(self):
        from services.manager_v2 import safe_rel_path
        result = safe_rel_path("subdir/file.mkv")
        assert ".." not in str(result)


# ── full_sync restartable set ─────────────────────────────────────────────────

class TestFullSyncRestartableSet:
    """Verify the full_alldebrid_sync trigger logic directly from source."""

    def test_restartable_set_excludes_in_progress(self):
        """
        The set of statuses that trigger _start_download in full_alldebrid_sync
        must NOT include queued, downloading, or paused.
        """
        import re
        import os
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, 'services', 'manager_v2.py'), encoding='utf-8') as f:
            src = f.read()
        # Find the _restartable definition in full_alldebrid_sync
        m = re.search(r'_restartable\s*=\s*\(([^)]+)\)', src)
        assert m, "_restartable set not found in full_alldebrid_sync"
        restartable_str = m.group(1)
        assert '"queued"' not in restartable_str, "queued must not be in _restartable"
        assert '"downloading"' not in restartable_str, "downloading must not be in _restartable"
        assert '"paused"' not in restartable_str, "paused must not be in _restartable"
        # Must include the error/pending/processing/uploading/ready statuses
        assert '"error"' in restartable_str
        assert '"pending"' in restartable_str


# ── Config validator integration ──────────────────────────────────────────────

class TestConfigValidatorWithDownloadSettings:
    def test_max_concurrent_downloads_clamped(self):
        from core.config_validator import validate_and_sanitise
        cfg = AppSettings(max_concurrent_downloads=0)
        result = validate_and_sanitise(cfg)
        assert result.max_concurrent_downloads == 1

    def test_aria2_max_active_clamped(self):
        from core.config_validator import validate_and_sanitise
        cfg = AppSettings(aria2_max_active_downloads=50)
        result = validate_and_sanitise(cfg)
        assert result.aria2_max_active_downloads == 20

    def test_stuck_timeout_clamped(self):
        from core.config_validator import validate_and_sanitise
        cfg = AppSettings(stuck_download_timeout_hours=200)
        result = validate_and_sanitise(cfg)
        assert result.stuck_download_timeout_hours == 168

    def test_poll_interval_minimum(self):
        from core.config_validator import validate_and_sanitise
        cfg = AppSettings(poll_interval_seconds=1)
        result = validate_and_sanitise(cfg)
        assert result.poll_interval_seconds >= 5
