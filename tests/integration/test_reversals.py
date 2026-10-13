import asyncio
import uuid

import pytest

from app.core.errors import AlreadyReversed
from app.services.ledger import LedgerService
from tests.conftest import IS_SQLITE, ADMIN

pytestmark = pytest.mark.asyncio


def key():
    return {"Idempotency-Key": uuid.uuid4().hex}


async def acct(c, name="A"):
    return (await c.post("/api/v1/accounts", json={"owner_name": name})).json()["id"]


async def bal(c, a):
    return (await c.get(f"/api/v1/accounts/{a}")).json()["balance"]


async def reverse(c, tx, headers=None, **body):
    return await c.post(f"/api/v1/transactions/{tx}/reversals", json=body or None, headers=headers or key())


async def test_reversing_a_transfer_refunds_both_sides(client):
    a, b = await acct(client, "A"), await acct(client, "B")
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 1000}, headers=key())
    t = (await client.post("/api/v1/transfers", json={"from_account_id": a, "to_account_id": b, "amount": 300}, headers=key())).json()
    r = await reverse(client, t["id"], description="customer refund")
    assert r.status_code == 201
    body = r.json()
    assert body["type"] == "REVERSAL" and body["reversal_of_id"] == t["id"] and body["description"] == "customer refund"
    assert (await bal(client, a), await bal(client, b)) == (1000, 0)
    assert sorted(e["amount"] for e in body["entries"]) == [-300, 300]
    # history is append-only: the original transaction is untouched, the refund is a NEW one
    assert (await client.get(f"/api/v1/transactions/{t['id']}")).json()["type"] == "TRANSFER"
    assert len((await client.get(f"/api/v1/accounts/{a}/entries")).json()) == 3


async def test_reversing_deposits_and_withdrawals(client):
    a = await acct(client)
    dep = (await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 500}, headers=key())).json()
    wd = (await client.post(f"/api/v1/accounts/{a}/withdrawals", json={"amount": 200}, headers=key())).json()
    assert (await reverse(client, wd["id"])).status_code == 201 and await bal(client, a) == 500
    assert (await reverse(client, dep["id"])).status_code == 201 and await bal(client, a) == 0


async def test_a_transaction_can_only_be_reversed_once(client):
    a = await acct(client)
    dep = (await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 500}, headers=key())).json()
    assert (await reverse(client, dep["id"])).status_code == 201
    again = await reverse(client, dep["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_reversed"
    assert await bal(client, a) == 0


async def test_a_reversal_cannot_be_reversed(client):
    a = await acct(client)
    dep = (await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 500}, headers=key())).json()
    rev = (await reverse(client, dep["id"])).json()
    r = await reverse(client, rev["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_reversible"


async def test_cannot_reverse_money_that_was_already_spent(client):
    a, b = await acct(client, "A"), await acct(client, "B")
    dep = (await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=key())).json()
    await client.post(f"/api/v1/accounts/{a}/withdrawals", json={"amount": 100}, headers=key())
    r = await reverse(client, dep["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "insufficient_funds"
    assert await bal(client, a) == 0
    # and the failed attempt did not "use up" the one allowed reversal
    assert (await client.get(f"/api/v1/transactions/{dep['id']}")).status_code == 200


async def test_reversal_is_idempotent(client):
    a = await acct(client)
    dep = (await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 500}, headers=key())).json()
    h = key()
    first, second = await reverse(client, dep["id"], h), await reverse(client, dep["id"], h)
    assert first.status_code == 201 and second.status_code == 200 and first.json()["id"] == second.json()["id"]


async def test_unknown_transaction(client):
    assert (await reverse(client, uuid.uuid4())).status_code == 404


async def test_ledger_still_reconciles_after_reversals(client):
    a, b = await acct(client, "A"), await acct(client, "B")
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 1000}, headers=key())
    t = (await client.post("/api/v1/transfers", json={"from_account_id": a, "to_account_id": b, "amount": 400}, headers=key())).json()
    await reverse(client, t["id"])
    assert (await client.get("/api/v1/admin/reconciliation", headers=ADMIN)).json()["ok"] is True


@pytest.mark.skipif(IS_SQLITE, reason="needs PostgreSQL (set TEST_DATABASE_URL)")
async def test_concurrent_reversals_refund_exactly_once(session_maker, client_id):
    async with session_maker() as s:
        acc = await LedgerService(s, client_id).create_account("A", "INR")
    async with session_maker() as s:
        tx, _ = await LedgerService(s, client_id).deposit(account_id=acc.id, amount=1000, key=uuid.uuid4().hex)

    async def attempt():
        async with session_maker() as s:
            try:
                await LedgerService(s, client_id).reverse(transaction_id=tx.id, key=uuid.uuid4().hex)
                return "ok"
            except AlreadyReversed:
                return "already"

    results = await asyncio.wait_for(asyncio.gather(*[attempt() for _ in range(10)]), timeout=60)
    assert results.count("ok") == 1 and results.count("already") == 9
    async with session_maker() as s:
        assert (await LedgerService(s, client_id).get_account(acc.id)).balance == 0
        assert (await LedgerService(s, client_id).reconcile())["ok"]
