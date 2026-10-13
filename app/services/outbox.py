"""Outbox delivery: turns committed events into signed webhook calls (at-least-once).

Run it as a loop (`python -m app.worker`), or one batch at a time via POST /admin/outbox/dispatch
(handy on free hosting that has no background workers: call it from a cron / GitHub Action).

Rules:
* Rows are claimed with SELECT ... FOR UPDATE SKIP LOCKED, so several workers can run
  side by side without delivering the same event twice at the same moment.
* A failure schedules a retry with exponential backoff (5s, 10s, 20s ... capped at 1h).
* After `outbox_max_attempts` the event becomes DEAD and stays visible for a human.
* Delivery is at-least-once: receivers must de-duplicate on the `X-Ledger-Event-Id` header.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.observability import OUTBOX_DELIVERIES, event
from app.models import OutboxEvent, OutboxStatus

BATCH = 20


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(5 * 2 ** (attempts - 1), 3600))


async def http_send(url: str, body: bytes, headers: dict) -> None:
    async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as http:
        resp = await http.post(url, content=body, headers=headers)
        resp.raise_for_status()


async def dispatch_batch(session: AsyncSession, send=http_send, now: datetime | None = None) -> dict:
    result = {"attempted": 0, "delivered": 0, "failed": 0, "dead": 0}
    if not settings.webhook_url:
        return result
    now = now or datetime.now(timezone.utc)
    async with session.begin():
        stmt = (select(OutboxEvent)
                .where(OutboxEvent.status == OutboxStatus.PENDING, OutboxEvent.next_attempt_at <= now)
                .order_by(OutboxEvent.id).limit(BATCH).with_for_update(skip_locked=True))
        for ev in (await session.execute(stmt)).scalars():
            body = json.dumps({"id": ev.id, "type": ev.event_type, "data": ev.payload},
                              sort_keys=True).encode()
            headers = {"Content-Type": "application/json", "X-Ledger-Event-Id": str(ev.id),
                       "X-Ledger-Signature": sign(settings.webhook_secret, body)}
            result["attempted"] += 1
            ev.attempts += 1
            try:
                await send(settings.webhook_url, body, headers)
            except Exception as exc:  # any failure => retry later
                ev.last_error = f"{type(exc).__name__}: {exc}"[:500]
                if ev.attempts >= settings.outbox_max_attempts:
                    ev.status = OutboxStatus.DEAD
                    result["dead"] += 1
                    OUTBOX_DELIVERIES.labels("dead").inc()
                else:
                    ev.next_attempt_at = now + backoff(ev.attempts)
                    result["failed"] += 1
                    OUTBOX_DELIVERIES.labels("failed").inc()
                event("outbox_failed", event_id=ev.id, attempts=ev.attempts, error=ev.last_error)
            else:
                ev.status, ev.delivered_at, ev.last_error = OutboxStatus.DELIVERED, now, None
                result["delivered"] += 1
                OUTBOX_DELIVERIES.labels("delivered").inc()
    return result


async def summary(session: AsyncSession) -> dict:
    counts = dict((await session.execute(
        select(OutboxEvent.status, func.count()).group_by(OutboxEvent.status))).all())
    oldest = (await session.execute(select(func.min(OutboxEvent.created_at)).where(
        OutboxEvent.status == OutboxStatus.PENDING))).scalar_one()
    age = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        age = round((datetime.now(timezone.utc) - oldest).total_seconds(), 1)
    return {"pending": counts.get(OutboxStatus.PENDING, 0), "delivered": counts.get(OutboxStatus.DELIVERED, 0),
            "dead": counts.get(OutboxStatus.DEAD, 0), "oldest_pending_age_seconds": age}
