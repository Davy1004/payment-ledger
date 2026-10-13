"""The Alembic migration must build exactly the schema the models describe (PostgreSQL only)."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from tests.conftest import IS_SQLITE, TEST_DB

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(IS_SQLITE, reason="needs PostgreSQL (set TEST_DATABASE_URL)")]


def alembic(*args):
    env = dict(os.environ, DATABASE_URL=TEST_DB)
    return subprocess.run([sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True)


async def test_upgrade_and_downgrade_roundtrip():
    eng = create_async_engine(TEST_DB)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    async with eng.connect() as c:
        tables = await c.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert {"accounts", "transactions", "ledger_entries"} <= tables
    check = alembic("check")  # models and migration agree -> "No new upgrade operations detected"
    assert check.returncode == 0, check.stdout + check.stderr
    down = alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr
    await eng.dispose()


async def test_existing_data_is_adopted_by_a_legacy_client_on_upgrade():
    eng = create_async_engine(TEST_DB)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    assert alembic("upgrade", "0001").returncode == 0
    acc, tx = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    async with eng.begin() as c:
        await c.exec_driver_sql(
            f"INSERT INTO accounts (id, owner_name, currency, kind, balance) VALUES ('{acc}', 'Old', 'INR', 'USER', 0)")
        await c.exec_driver_sql(
            f"INSERT INTO transactions (id, type, idempotency_key, request_hash) VALUES ('{tx}', 'DEPOSIT', 'old-key-0001', 'h')")
    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    async with eng.connect() as c:
        rows = (await c.exec_driver_sql(
            "SELECT a.client_id = t.client_id, k.name FROM accounts a, transactions t, api_clients k "
            "WHERE k.id = a.client_id")).all()
    assert rows == [(True, "legacy")]
    assert alembic("downgrade", "base").returncode == 0
    await eng.dispose()
