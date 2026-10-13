"""Race-condition tests. They need a real database with row locking, so they only run on PostgreSQL."""
import asyncio
import uuid

import pytest

from app.core.errors import InsufficientFunds
from app.services.ledger import LedgerService
from tests.conftest import IS_SQLITE

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(IS_SQLITE, reason="needs PostgreSQL (set TEST_DATABASE_URL)")]


CID = None  # the tenant used by every test in this file (set per test)


@pytest.fixture(autouse=True)
def _tenant(client_id):
    global CID
    CID = client_id


async def open_account(session_maker, name, balance=0):
    async with session_maker() as s:
        acc = await LedgerService(s, CID).create_account(name, "INR")
    if balance:
        async with session_maker() as s:
            await LedgerService(s, CID).deposit(account_id=acc.id, amount=balance, key=uuid.uuid4().hex)
    return acc.id


async def reconciled(session_maker):
    async with session_maker() as s:
        return await LedgerService(s, CID).reconcile()


async def test_cannot_overspend_with_concurrent_withdrawals(session_maker):
    acc = await open_account(session_maker, "A", 1000)

    async def withdraw():
        async with session_maker() as s:
            try:
                await LedgerService(s, CID).withdraw(account_id=acc, amount=100, key=uuid.uuid4().hex)
                return "ok"
            except InsufficientFunds:
                return "refused"

    results = await asyncio.wait_for(asyncio.gather(*[withdraw() for _ in range(25)]), timeout=60)
    assert results.count("ok") == 10 and results.count("refused") == 15
    async with session_maker() as s:
        assert (await LedgerService(s, CID).get_account(acc)).balance == 0
    assert (await reconciled(session_maker))["ok"]


async def test_opposite_transfers_do_not_deadlock(session_maker):
    a, b = await open_account(session_maker, "A", 10_000), await open_account(session_maker, "B", 10_000)

    async def move(src, dst):
        async with session_maker() as s:
            await LedgerService(s, CID).transfer(from_id=src, to_id=dst, amount=10, key=uuid.uuid4().hex)

    jobs = [move(a, b) if i % 2 else move(b, a) for i in range(40)]
    await asyncio.wait_for(asyncio.gather(*jobs), timeout=60)  # a deadlock would raise/timeout here
    async with session_maker() as s:
        svc = LedgerService(s, CID)
        assert (await svc.get_account(a)).balance + (await svc.get_account(b)).balance == 20_000
    assert (await reconciled(session_maker))["ok"]


async def test_same_idempotency_key_sent_concurrently_posts_once(session_maker):
    acc = await open_account(session_maker, "A")
    k = uuid.uuid4().hex

    async def dep():
        async with session_maker() as s:
            tx, created = await LedgerService(s, CID).deposit(account_id=acc, amount=500, key=k)
            return tx.id, created

    results = await asyncio.wait_for(asyncio.gather(*[dep() for _ in range(12)]), timeout=60)
    assert len({tx_id for tx_id, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    async with session_maker() as s:
        assert (await LedgerService(s, CID).get_account(acc)).balance == 500
    assert (await reconciled(session_maker))["ok"]
