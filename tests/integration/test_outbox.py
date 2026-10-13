import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import OutboxEvent, OutboxStatus
from app.services import outbox
from tests.conftest import ADMIN, IS_SQLITE

pytestmark = pytest.mark.asyncio


def key():
    return {"Idempotency-Key": uuid.uuid4().hex}


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setattr(settings, "webhook_url", "https://hooks.example/ledger")


class Receiver:
    """A fake webhook endpoint; can be told to fail."""
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if self.fail:
            raise RuntimeError("receiver is down")


async def events(session_maker):
    async with session_maker() as s:
        return list((await s.execute(select(OutboxEvent).order_by(OutboxEvent.id))).scalars())


async def new_account(client):
    return (await client.post("/api/v1/accounts", json={"owner_name": "A"})).json()["id"]


async def test_every_committed_movement_writes_exactly_one_event(client, session_maker):
    a = await new_account(client)
    dep = (await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=key())).json()
    await client.post(f"/api/v1/transactions/{dep['id']}/reversals", headers=key())
    evs = await events(session_maker)
    assert [e.event_type for e in evs] == ["transaction.deposit", "transaction.reversal"]
    assert evs[0].payload["transaction_id"] == dep["id"] and evs[0].status == OutboxStatus.PENDING
    assert sorted(x["amount"] for x in evs[0].payload["entries"]) == [-100, 100]
    assert evs[1].payload["reversal_of"] == dep["id"]


async def test_replays_and_failures_write_no_extra_events(client, session_maker):
    a = await new_account(client)
    h = key()
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=h)
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=h)           # replay
    await client.post(f"/api/v1/accounts/{a}/withdrawals", json={"amount": 999}, headers=key())     # rejected
    assert len(await events(session_maker)) == 1  # the event rolls back together with the money


async def test_delivery_is_signed_and_marks_events_delivered(client, session_maker, webhook):
    a = await new_account(client)
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=key())
    rx = Receiver()
    async with session_maker() as s:
        result = await outbox.dispatch_batch(s, send=rx)
    assert result == {"attempted": 1, "delivered": 1, "failed": 0, "dead": 0}
    url, body, headers = rx.calls[0]
    assert url == settings.webhook_url
    expected = "sha256=" + hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(headers["X-Ledger-Signature"], expected)
    assert json.loads(body)["type"] == "transaction.deposit"
    assert headers["X-Ledger-Event-Id"] == str(json.loads(body)["id"])
    (ev,) = await events(session_maker)
    assert ev.status == OutboxStatus.DELIVERED and ev.delivered_at is not None
    async with session_maker() as s:   # nothing left to send
        assert (await outbox.dispatch_batch(s, send=rx))["attempted"] == 0


async def test_failures_back_off_then_succeed_when_the_receiver_recovers(client, session_maker, webhook):
    a = await new_account(client)
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=key())
    t0 = datetime.now(timezone.utc)
    down = Receiver(fail=True)
    async with session_maker() as s:
        assert (await outbox.dispatch_batch(s, send=down, now=t0))["failed"] == 1
    (ev,) = await events(session_maker)
    assert ev.status == OutboxStatus.PENDING and ev.attempts == 1 and "receiver is down" in ev.last_error

    async with session_maker() as s:   # too early: still backing off
        assert (await outbox.dispatch_batch(s, send=down, now=t0 + timedelta(seconds=1)))["attempted"] == 0
    up = Receiver()
    async with session_maker() as s:
        assert (await outbox.dispatch_batch(s, send=up, now=t0 + timedelta(seconds=30)))["delivered"] == 1
    (ev,) = await events(session_maker)
    assert ev.status == OutboxStatus.DELIVERED and ev.attempts == 2 and ev.last_error is None


async def test_event_goes_dead_after_max_attempts(client, session_maker, webhook, monkeypatch):
    monkeypatch.setattr(settings, "outbox_max_attempts", 3)
    a = await new_account(client)
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=key())
    down, t = Receiver(fail=True), datetime.now(timezone.utc)
    outcomes = []
    for i in range(3):
        async with session_maker() as s:
            outcomes.append(await outbox.dispatch_batch(s, send=down, now=t + timedelta(hours=i * 2)))
    assert [o["dead"] for o in outcomes] == [0, 0, 1]
    (ev,) = await events(session_maker)
    assert ev.status == OutboxStatus.DEAD
    async with session_maker() as s:
        assert (await outbox.dispatch_batch(s, send=down, now=t + timedelta(days=9)))["attempted"] == 0


async def test_backoff_doubles_and_is_capped():
    assert [outbox.backoff(n).total_seconds() for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert outbox.backoff(30).total_seconds() == 3600


async def test_without_a_webhook_url_events_are_kept_but_not_sent(client, session_maker, monkeypatch):
    monkeypatch.setattr(settings, "webhook_url", None)
    a = await new_account(client)
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=key())
    rx = Receiver()
    async with session_maker() as s:
        assert (await outbox.dispatch_batch(s, send=rx))["attempted"] == 0
    assert rx.calls == [] and (await events(session_maker))[0].status == OutboxStatus.PENDING


async def test_admin_can_see_the_queue_and_dispatch(client, make_client, session_maker, monkeypatch):
    a = await new_account(client)
    await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 100}, headers=key())
    admin = await make_client()
    assert (await admin.get("/api/v1/admin/outbox")).status_code == 401
    s = (await admin.get("/api/v1/admin/outbox", headers=ADMIN)).json()
    assert (s["pending"], s["delivered"], s["dead"]) == (1, 0, 0) and s["oldest_pending_age_seconds"] >= 0
    monkeypatch.setattr(settings, "webhook_url", None)
    assert (await admin.post("/api/v1/admin/outbox/dispatch", headers=ADMIN)).json()["attempted"] == 0


@pytest.mark.skipif(IS_SQLITE, reason="needs PostgreSQL (SKIP LOCKED)")
async def test_parallel_workers_never_deliver_the_same_event_twice(client, session_maker, webhook):
    a = await new_account(client)
    for _ in range(15):
        await client.post(f"/api/v1/accounts/{a}/deposits", json={"amount": 1}, headers=key())
    rx = Receiver()

    async def worker():
        total = 0
        for _ in range(4):
            async with session_maker() as s:
                total += (await outbox.dispatch_batch(s, send=slow(rx)))["delivered"]
        return total

    def slow(r):
        async def send(url, body, headers):
            await asyncio.sleep(0.05)
            await r(url, body, headers)
        return send

    await asyncio.wait_for(asyncio.gather(*[worker() for _ in range(4)]), timeout=60)
    ids = [json.loads(c[1])["id"] for c in rx.calls]
    assert len(ids) == 15 and len(set(ids)) == 15
