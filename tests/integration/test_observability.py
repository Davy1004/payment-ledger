import json
import logging
import uuid

import pytest

from tests.conftest import ADMIN

pytestmark = pytest.mark.asyncio


def key():
    return {"Idempotency-Key": uuid.uuid4().hex}


async def metrics(c):
    r = await c.get("/metrics", headers=ADMIN)
    assert r.status_code == 200
    return r.text


async def test_metrics_are_not_public(make_client):
    anon = await make_client()
    assert (await anon.get("/metrics")).status_code == 401
    assert (await anon.get("/metrics", headers={"Authorization": "Bearer test-admin-key"})).status_code == 200


async def test_request_id_is_generated_and_echoed(client):
    r = await client.get("/health")
    assert len(r.headers["x-request-id"]) >= 16
    r = await client.get("/health", headers={"X-Request-ID": "trace-me-123"})
    assert r.headers["x-request-id"] == "trace-me-123"


async def test_metrics_count_transactions_and_label_routes_by_template(client):
    a = (await client.post("/api/v1/accounts", json={"owner_name": "A"})).json()["id"]
    before = await metrics(client)
    h = key()
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 5}, headers=h)
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 5}, headers=h)          # replay
    await client.post(f"/api/v1/accounts/{a}/withdrawals", json={"amount": 99}, headers=key())  # rejected
    after = await metrics(client)

    def value(text, prefix):
        for line in text.splitlines():
            if line.startswith(prefix):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    created = 'ledger_transactions_total{outcome="created",type="DEPOSIT"}'
    replayed = 'ledger_transactions_total{outcome="replayed",type="DEPOSIT"}'
    rejected = 'ledger_rejections_total{code="insufficient_funds"}'
    assert value(after, created) - value(before, created) == 1
    assert value(after, replayed) - value(before, replayed) == 1
    assert value(after, rejected) - value(before, rejected) == 1
    # the account id must NOT appear in labels (unbounded cardinality)
    assert a not in after and 'route="/api/v1/accounts/{account_id}/deposits"' in after


async def test_requests_are_logged_as_json_with_request_id(client, caplog):
    caplog.set_level(logging.INFO, logger="ledger")
    logging.getLogger("ledger").propagate = True   # let caplog see it
    try:
        await client.get(f"/api/v1/accounts/{uuid.uuid4()}", headers={"X-Request-ID": "log-me-1"})
    finally:
        logging.getLogger("ledger").propagate = False
    rec = next(r for r in caplog.records if r.getMessage() == "request")
    assert rec.fields["request_id"] == "log-me-1" and rec.fields["status"] == 404
    assert rec.fields["route"] == "/api/v1/accounts/{account_id}" and rec.fields["client_id"]

    from app.core.observability import JsonFormatter
    line = json.loads(JsonFormatter().format(rec))
    assert line["msg"] == "request" and line["status"] == 404 and "ts" in line
