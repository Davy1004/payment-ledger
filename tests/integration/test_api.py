import uuid

import pytest

from tests.conftest import ADMIN

pytestmark = pytest.mark.asyncio


def key():
    return {"Idempotency-Key": uuid.uuid4().hex}


async def new_account(client, name="Asha", currency="INR"):
    r = await client.post("/api/v1/accounts", json={"owner_name": name, "currency": currency})
    assert r.status_code == 201
    return r.json()["id"]


async def deposit(client, account_id, amount, headers=None):
    return await client.post(f"/api/v1/accounts/{account_id}/deposits", json={"amount": amount}, headers=headers or key())


async def balance(client, account_id):
    return (await client.get(f"/api/v1/accounts/{account_id}")).json()["balance"]


async def test_new_account_starts_at_zero(client):
    acc = await new_account(client)
    assert await balance(client, acc) == 0


async def test_deposit_then_withdraw(client):
    acc = await new_account(client)
    r = await deposit(client, acc, 50_000)
    assert r.status_code == 201
    # double entry: one leg for the SYSTEM account, one for the user, summing to zero
    assert sorted(e["amount"] for e in r.json()["entries"]) == [-50_000, 50_000]
    r = await client.post(f"/api/v1/accounts/{acc}/withdrawals", json={"amount": 20_000}, headers=key())
    assert r.status_code == 201 and await balance(client, acc) == 30_000


async def test_transfer_moves_money(client):
    a, b = await new_account(client, "A"), await new_account(client, "B")
    await deposit(client, a, 1000)
    r = await client.post("/api/v1/transfers", json={"from_account_id": a, "to_account_id": b, "amount": 400,
                                                      "description": "rent"}, headers=key())
    assert r.status_code == 201 and r.json()["type"] == "TRANSFER"
    assert (await balance(client, a), await balance(client, b)) == (600, 400)


async def test_insufficient_funds_changes_nothing(client):
    a, b = await new_account(client, "A"), await new_account(client, "B")
    await deposit(client, a, 100)
    r = await client.post("/api/v1/transfers", json={"from_account_id": a, "to_account_id": b, "amount": 101}, headers=key())
    assert r.status_code == 422 and r.json()["error"]["code"] == "insufficient_funds"
    assert (await balance(client, a), await balance(client, b)) == (100, 0)
    assert (await client.get(f"/api/v1/accounts/{b}/entries")).json() == []


async def test_idempotent_retry_returns_same_transaction_once(client):
    acc = await new_account(client)
    h = key()
    first = await deposit(client, acc, 700, h)
    again = await deposit(client, acc, 700, h)
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert await balance(client, acc) == 700  # credited once, not twice


async def test_reusing_a_key_with_different_body_is_rejected(client):
    acc = await new_account(client)
    h = key()
    await deposit(client, acc, 700, h)
    r = await deposit(client, acc, 999, h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reuse"
    assert await balance(client, acc) == 700


async def test_idempotency_key_is_required(client):
    acc = await new_account(client)
    r = await client.post(f"/api/v1/accounts/{acc}/deposits", json={"amount": 5})
    assert r.status_code == 422


async def test_validation_and_not_found(client):
    a = await new_account(client)
    assert (await client.post("/api/v1/transfers", json={"from_account_id": a, "to_account_id": a, "amount": 5}, headers=key())).status_code == 422
    assert (await deposit(client, a, 0)).status_code == 422
    assert (await deposit(client, a, -3)).status_code == 422
    missing = str(uuid.uuid4())
    assert (await client.get(f"/api/v1/accounts/{missing}")).status_code == 404
    r = await client.post("/api/v1/transfers", json={"from_account_id": a, "to_account_id": missing, "amount": 1}, headers=key())
    assert r.status_code == 404 and r.json()["error"]["code"] == "account_not_found"


async def test_currencies_cannot_be_mixed(client):
    inr, usd = await new_account(client, "A", "INR"), await new_account(client, "B", "USD")
    await deposit(client, inr, 100)
    r = await client.post("/api/v1/transfers", json={"from_account_id": inr, "to_account_id": usd, "amount": 10}, headers=key())
    assert r.status_code == 422 and r.json()["error"]["code"] == "currency_mismatch"


async def test_statement_is_newest_first_with_running_balance(client):
    acc = await new_account(client)
    await deposit(client, acc, 100)
    await deposit(client, acc, 50)
    entries = (await client.get(f"/api/v1/accounts/{acc}/entries")).json()
    assert [e["amount"] for e in entries] == [50, 100]
    assert [e["balance_after"] for e in entries] == [150, 100]
    assert len((await client.get(f"/api/v1/accounts/{acc}/entries?limit=1")).json()) == 1


async def test_get_transaction(client):
    acc = await new_account(client)
    tx = (await deposit(client, acc, 10)).json()
    got = (await client.get(f"/api/v1/transactions/{tx['id']}")).json()
    assert got["id"] == tx["id"] and len(got["entries"]) == 2
    assert (await client.get(f"/api/v1/transactions/{uuid.uuid4()}")).status_code == 404


async def test_reconciliation_is_clean_after_activity(client):
    a, b = await new_account(client, "A"), await new_account(client, "B")
    await deposit(client, a, 1000)
    await client.post("/api/v1/transfers", json={"from_account_id": a, "to_account_id": b, "amount": 250}, headers=key())
    await client.post(f"/api/v1/accounts/{b}/withdrawals", json={"amount": 100}, headers=key())
    assert (await client.get("/api/v1/admin/reconciliation", headers=ADMIN)).json() == {"ok": True, "ledger_sum": 0, "mismatched_accounts": []}


async def test_reconciliation_detects_tampering(client, session_maker):
    from sqlalchemy import text
    a = await new_account(client)
    await deposit(client, a, 1000)
    async with session_maker() as s, s.begin():  # someone edits a balance behind the ledger's back
        await s.execute(text("UPDATE accounts SET balance = balance + 5 WHERE kind = 'USER'"))
    report = (await client.get("/api/v1/admin/reconciliation", headers=ADMIN)).json()
    assert report["ok"] is False and report["mismatched_accounts"][0]["cached_balance"] == 1005


async def test_system_account_is_not_reachable_through_public_api(client, session_maker):
    from sqlalchemy import select
    from app.models import Account, AccountKind
    a = await new_account(client)
    await deposit(client, a, 100)
    async with session_maker() as s:
        system_id = (await s.execute(select(Account.id).where(Account.kind == AccountKind.SYSTEM))).scalar_one()
    assert (await client.get(f"/api/v1/accounts/{system_id}")).status_code == 404
    r = await client.post("/api/v1/transfers", json={"from_account_id": a, "to_account_id": str(system_id), "amount": 1}, headers=key())
    assert r.status_code == 404
    assert await balance(client, a) == 100


async def test_health(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
