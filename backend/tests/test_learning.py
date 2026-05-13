from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeLearningDb:
    backend = "postgres"

    def __init__(self):
        self.fetchall_calls = []
        self.fetchone_calls = []

    async def fetchall(self, sql, params=()):
        self.fetchall_calls.append((sql, params))
        if "GROUP BY source" in sql:
            return [
                {
                    "source": "jackett",
                    "total": 5,
                    "completed": 4,
                    "errors": 1,
                    "no_peer": 0,
                }
            ]
        if "GROUP BY grp" in sql:
            return [{"grp": "GROUP", "total": 3, "completed": 3}]
        return [{"label": "movies", "cnt": 2}]

    async def fetchone(self, sql, params=()):
        self.fetchone_calls.append((sql, params))
        return {"total": 5, "no_peer": 1}


@pytest.mark.asyncio
async def test_learning_stats_use_datetime_params_and_postgres_safe_sql():
    from services.learning import get_learning_stats

    fake_db = _FakeLearningDb()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_db

    with patch("db.database.get_db", fake_get_db):
        result = await get_learning_stats()

    assert "error" not in result
    assert result["indexers"][0]["indexer"] == "jackett"
    calls_with_params = [
        params
        for _, params in fake_db.fetchall_calls + fake_db.fetchone_calls
        if params
    ]
    assert calls_with_params
    assert all(isinstance(params[0], datetime) for params in calls_with_params)
    assert not any("DATE('now'" in sql for sql, _ in fake_db.fetchall_calls + fake_db.fetchone_calls)
    assert not any("INSTR(" in sql for sql, _ in fake_db.fetchall_calls)
    assert any("STRPOS(" in sql for sql, _ in fake_db.fetchall_calls)
    assert any("HAVING COUNT(*) >= 3" in sql for sql, _ in fake_db.fetchall_calls)
