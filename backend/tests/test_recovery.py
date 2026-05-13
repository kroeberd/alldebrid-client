"""Tests for services/recovery.py — Auto-Recovery System."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestRunRecoveryChecks:
    @pytest.mark.asyncio
    async def test_returns_summary_dict(self):
        with patch("services.recovery._fix_orphaned_queued_files", AsyncMock(return_value=0)), \
             patch("services.recovery._fix_missed_completions",    AsyncMock(return_value=0)), \
             patch("services.recovery._fix_queue_deadlock",        AsyncMock(return_value=False)):
            from services.recovery import run_recovery_checks
            result = await run_recovery_checks()
        assert "orphaned_queued_files" in result
        assert "missed_completions"    in result
        assert "deadlock_reset"        in result
        assert isinstance(result["errors"], list)

    @pytest.mark.asyncio
    async def test_errors_captured_not_raised(self):
        async def _boom(): raise RuntimeError("db error")
        with patch("services.recovery._fix_orphaned_queued_files", _boom), \
             patch("services.recovery._fix_missed_completions",    AsyncMock(return_value=0)), \
             patch("services.recovery._fix_queue_deadlock",        AsyncMock(return_value=False)):
            from services.recovery import run_recovery_checks
            result = await run_recovery_checks()
        assert len(result["errors"]) == 1
        assert "db error" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_deadlock_reset_reported(self):
        with patch("services.recovery._fix_orphaned_queued_files", AsyncMock(return_value=0)), \
             patch("services.recovery._fix_missed_completions",    AsyncMock(return_value=0)), \
             patch("services.recovery._fix_queue_deadlock",        AsyncMock(return_value=True)):
            from services.recovery import run_recovery_checks
            result = await run_recovery_checks()
        assert result["deadlock_reset"] is True


class TestFixQueueDeadlock:
    @pytest.mark.asyncio
    async def test_no_deadlock_when_active_downloads(self):
        mock_db = MagicMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__  = AsyncMock(return_value=False)
        mock_db.fetchone   = AsyncMock(side_effect=[
            {"c": 3},   # active count
            {"c": 5},   # ready count
        ])
        with patch("db.database.get_db", return_value=mock_db), \
             patch("services.manager_v2.manager"):
            from services.recovery import _fix_queue_deadlock
            result = await _fix_queue_deadlock()
        assert result is False

    @pytest.mark.asyncio
    async def test_deadlock_detected_and_reset(self):
        mock_db = MagicMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__  = AsyncMock(return_value=False)
        mock_db.fetchone   = AsyncMock(side_effect=[
            {"c": 0},   # 0 active
            {"c": 4},   # 4 ready
        ])
        mock_mgr = MagicMock()
        mock_mgr.reset_services = MagicMock()
        with patch("db.database.get_db", return_value=mock_db), \
             patch("services.manager_v2.manager", mock_mgr):
            from services.recovery import _fix_queue_deadlock
            result = await _fix_queue_deadlock()
        assert result is True
        mock_mgr.reset_services.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_deadlock_when_nothing_ready(self):
        mock_db = MagicMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__  = AsyncMock(return_value=False)
        mock_db.fetchone   = AsyncMock(side_effect=[
            {"c": 0},   # 0 active
            {"c": 0},   # 0 ready
        ])
        with patch("db.database.get_db", return_value=mock_db), \
             patch("services.manager_v2.manager"):
            from services.recovery import _fix_queue_deadlock
            result = await _fix_queue_deadlock()
        assert result is False
