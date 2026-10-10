"""Tests run on in-memory SQLite by default (fast, no setup).

To run the same tests - plus the concurrency/migration tests - on real PostgreSQL:
    docker compose up -d
    TEST_DATABASE_URL=postgresql+asyncpg://ledger_user:ledger_password@localhost:5432/payment_ledger uv run pytest
(that database is wiped by the tests, use a throwaway one).
"""
import os

TEST_DB = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("DATABASE_URL", TEST_DB)  # settings need *a* value at import time

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.database import get_session  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app import models as _models  # noqa: E402,F401  (registers tables on Base.metadata)

IS_SQLITE = TEST_DB.startswith("sqlite")


@pytest_asyncio.fixture
async def engine():
    kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}} if IS_SQLITE else {"pool_size": 25}
    eng = create_async_engine(TEST_DB, **kwargs)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session_maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(session_maker):
    async def override():
        async with session_maker() as session:
            yield session

    fastapi_app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as c:
        yield c
    fastapi_app.dependency_overrides.clear()
