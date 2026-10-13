import uuid

import pytest

from app.core.config import settings
from tests.conftest import ADMIN, make_tenant

pytestmark = pytest.mark.asyncio


def key():
    return {"Idempotency-Key": uuid.uuid4().hex}


async def test_requests_without_a_key_are_rejected(make_client):
    anon = await make_client()
    r = await anon.get(f"/api/v1/accounts/{uuid.uuid4()}")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert r.headers["www-authenticate"] == "ApiKey"
    assert (await anon.post("/api/v1/accounts", json={"owner_name": "x"})).status_code == 401


async def test_wrong_key_is_rejected(make_client):
    c = await make_client("pl_not_a_real_key")
    assert (await c.post("/api/v1/accounts", json={"owner_name": "x"})).status_code == 401


async def test_health_stays_public(make_client):
    assert (await (await make_client()).get("/health")).status_code == 200


async def test_tenants_cannot_see_or_touch_each_others_money(make_client, session_maker, client):
    other = await make_client((await make_tenant(session_maker, "other"))[1])
    mine = (await client.post("/api/v1/accounts", json={"owner_name": "mine"})).json()["id"]
    theirs = (await other.post("/api/v1/accounts", json={"owner_name": "theirs"})).json()["id"]
    dep = await client.post(f"/api/v1/accounts/{mine}/deposits", json={"amount": 500}, headers=key())

    # every cross-tenant access looks like "not found" (never 403: don't leak that it exists)
    assert (await other.get(f"/api/v1/accounts/{mine}")).status_code == 404
    assert (await other.get(f"/api/v1/accounts/{mine}/entries")).status_code == 404
    assert (await other.post(f"/api/v1/accounts/{mine}/deposits", json={"amount": 1}, headers=key())).status_code == 404
    assert (await other.post(f"/api/v1/accounts/{mine}/withdrawals", json={"amount": 1}, headers=key())).status_code == 404
    assert (await other.get(f"/api/v1/transactions/{dep.json()['id']}")).status_code == 404
    assert (await other.post(f"/api/v1/transactions/{dep.json()['id']}/reversals", headers=key())).status_code == 404
    steal = await other.post("/api/v1/transfers", json={"from_account_id": mine, "to_account_id": theirs, "amount": 1}, headers=key())
    push = await client.post("/api/v1/transfers", json={"from_account_id": mine, "to_account_id": theirs, "amount": 1}, headers=key())
    assert steal.status_code == 404 and push.status_code == 404
    assert (await client.get(f"/api/v1/accounts/{mine}")).json()["balance"] == 500


async def test_idempotency_keys_are_scoped_per_tenant(make_client, session_maker, client):
    other = await make_client((await make_tenant(session_maker, "other"))[1])
    a = (await client.post("/api/v1/accounts", json={"owner_name": "a"})).json()["id"]
    b = (await other.post("/api/v1/accounts", json={"owner_name": "b"})).json()["id"]
    same = {"Idempotency-Key": "shared-key-0001"}
    r1 = await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 10}, headers=same)
    r2 = await other.post(f"/api/v1/accounts/{b}/deposits", json={"amount": 10}, headers=same)
    assert r1.status_code == 201 and r2.status_code == 201  # not treated as a replay of the other tenant's request
    assert r1.json()["id"] != r2.json()["id"]


async def test_admin_endpoints_need_the_admin_key(make_client):
    anon = await make_client()
    assert (await anon.get("/api/v1/admin/reconciliation")).status_code == 401
    assert (await anon.get("/api/v1/admin/reconciliation", headers={"X-Admin-Key": "nope"})).status_code == 401
    assert (await anon.get("/api/v1/admin/reconciliation", headers=ADMIN)).status_code == 200
    assert (await anon.get("/api/v1/admin/reconciliation", headers={"Authorization": "Bearer test-admin-key"})).status_code == 200


async def test_a_normal_api_key_is_not_an_admin_key(client, tenant):
    r = await client.get("/api/v1/admin/reconciliation", headers={"X-Admin-Key": tenant[1]})
    assert r.status_code == 401


async def test_admin_is_disabled_when_no_admin_key_is_configured(make_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", None)
    r = await (await make_client()).get("/api/v1/admin/reconciliation", headers=ADMIN)
    assert r.status_code == 503 and r.json()["error"]["code"] == "admin_disabled"


async def test_onboarding_a_client_returns_a_working_key_once(make_client, session_maker):
    anon = await make_client()
    created = (await anon.post("/api/v1/admin/clients", json={"name": "Acme"}, headers=ADMIN))
    assert created.status_code == 201
    body = created.json()
    assert body["api_key"].startswith("pl_")

    acme = await make_client(body["api_key"])
    assert (await acme.post("/api/v1/accounts", json={"owner_name": "x"})).status_code == 201

    # only the hash is stored
    from sqlalchemy import select
    from app.models import ApiClient
    async with session_maker() as s:
        row = (await s.execute(select(ApiClient).where(ApiClient.name == "Acme"))).scalar_one()
    assert row.key_hash != body["api_key"] and body["api_key"] not in row.key_hash


async def test_deactivated_client_is_locked_out(make_client):
    anon = await make_client()
    body = (await anon.post("/api/v1/admin/clients", json={"name": "Temp"}, headers=ADMIN)).json()
    temp = await make_client(body["api_key"])
    assert (await temp.post("/api/v1/accounts", json={"owner_name": "x"})).status_code == 201
    assert (await anon.post(f"/api/v1/admin/clients/{body['id']}/deactivate", headers=ADMIN)).status_code == 204
    assert (await temp.post("/api/v1/accounts", json={"owner_name": "x"})).status_code == 401
