"""Tests run on in-memory SQLite by default (fast, no setup).

To run the same tests - plus the concurrency/migration tests - on real PostgreSQL:
    docker compose up -d
    TEST_DATABASE_URL=postgresql+asyncpg://ledger_user:ledger_password@localhost:5432/payment_ledger uv run pytest
(that database is wiped by the tests, use a throwaway one).
"""
import os
import uuid

TEST_DB = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("DATABASE_URL", TEST_DB)  # settings need *a* value at import time
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api.deps import hash_api_key  # noqa: E402
from app.core.ratelimit import limiter  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.database import get_session  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app import models as _models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.models import ApiClient  # noqa: E402

ADMIN = {"X-Admin-Key": "test-admin-key"}

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


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    limiter.reset()


async def make_tenant(session_maker, name="tenant"):
    """Insert an API client directly; returns (client_id, api_key)."""
    key = f"pl_test_{uuid.uuid4().hex}"
    async with session_maker() as s, s.begin():
        row = ApiClient(name=name, key_hash=hash_api_key(key))
        s.add(row)
    return row.id, key


@pytest_asyncio.fixture
async def tenant(session_maker):
    return await make_tenant(session_maker, "main")


@pytest_asyncio.fixture
async def client_id(tenant):
    return tenant[0]


@pytest_asyncio.fixture
async def make_client(session_maker):
    """Factory for HTTP clients of different tenants (default header = that tenant's API key)."""
    async def override():
        async with session_maker() as session:
            yield session

    fastapi_app.dependency_overrides[get_session] = override
    opened = []

    async def factory(api_key=None):
        headers = {"X-API-Key": api_key} if api_key else {}
        c = AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test", headers=headers)
        opened.append(c)
        return c

    yield factory
    for c in opened:
        await c.aclose()
    fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(make_client, tenant):
    return await make_client(tenant[1])
