from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import patch

import pytest


class _FakeAnalyticsDb:
    backend = "postgres"

    def __init__(self):
        self.fetchone_calls = []
        self.fetchall_calls = []

    async def fetchone(self, sql, params=()):
        self.fetchone_calls.append((sql, params))
        if "COUNT(*) AS c, COALESCE(SUM(size_bytes),0)" in sql:
            return {"c": 2, "total_bytes": 1024 ** 3}
        if "status='error'" in sql:
            return {"c": 1}
        if "AVG(" in sql:
            return {"avg_sec": 30}
        if "SUM(CASE WHEN status IN" in sql:
            return {"active": 3, "errors": 1}
        return {"c": 0}

    async def fetchall(self, sql, params=()):
        self.fetchall_calls.append((sql, params))
        return []


@pytest.mark.asyncio
async def test_queue_analytics_uses_datetime_params_and_postgres_hourly_sql():
    from services.analytics import get_queue_analytics

    fake_db = _FakeAnalyticsDb()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_db

    with patch("db.database.get_db", fake_get_db):
        result = await get_queue_analytics(24)

    assert "error" not in result
    all_params = [
        params
        for _, params in fake_db.fetchone_calls + fake_db.fetchall_calls
        if params
    ]
    assert all_params
    assert all(isinstance(params[0], datetime) for params in all_params)
    assert any("EXTRACT(EPOCH FROM (completed_at - created_at))" in sql for sql, _ in fake_db.fetchone_calls)
    assert not any("JULIANDAY" in sql for sql, _ in fake_db.fetchone_calls)
    assert any("DATE_TRUNC('hour', completed_at)" in sql for sql, _ in fake_db.fetchall_calls)
    assert not any("STRFTIME" in sql for sql, _ in fake_db.fetchall_calls)
