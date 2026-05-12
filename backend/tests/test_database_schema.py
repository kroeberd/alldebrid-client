from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePostgresConnection:
    def __init__(self):
        self.statements: list[str] = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, sql: str, *args):
        self.statements.append(sql)

    async def fetchrow(self, sql: str, *args):
        if "data_type" in sql:
            return {"data_type": "bigint"}
        return {"exists": 1}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_postgres_schema_creates_saved_searches_table():
    from db.database import _init_db_postgres

    fake_conn = _FakePostgresConnection()

    with patch("db.database._build_dsn", return_value="postgresql://test"), \
         patch("asyncpg.connect", AsyncMock(return_value=fake_conn)):
        await _init_db_postgres()

    ddl = "\n".join(fake_conn.statements)
    assert "CREATE TABLE IF NOT EXISTS saved_searches" in ddl
    assert "last_run_at TIMESTAMPTZ" in ddl
    assert "interval_minutes INTEGER DEFAULT 60" in ddl


def test_saved_searches_are_included_in_bidirectional_migration_tables():
    from db.migration import MIGRATION_TABLES

    assert MIGRATION_TABLES[-1] == "saved_searches"
