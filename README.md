# Payment Transaction Ledger

A small **double-entry ledger** API: accounts, deposits, withdrawals and transfers where money can never be created,
lost, double-spent or applied twice.

**Stack:** FastAPI · async SQLAlchemy 2 · PostgreSQL · Alembic · Prometheus metrics · pytest (+ real-Postgres concurrency tests)

**Also included:** per-client API-key auth with tenant isolation, one-time reversals/refunds, rate limiting, a transactional outbox with signed webhooks and a worker, structured logs + request ids + `/metrics`. Scaling path and trade-offs: [ARCHITECTURE.md](ARCHITECTURE.md).

## Guarantees (and the test that proves each)
| Guarantee | How | Test |
|---|---|---|
| Money is conserved | every transaction's entries sum to 0 | `test_reconciliation_*` |
| No overdraft, even under races | `SELECT … FOR UPDATE` + DB `CHECK (balance >= 0)` | `test_cannot_overspend_with_concurrent_withdrawals` |
| No deadlocks | locks always taken in id order | `test_opposite_transfers_do_not_deadlock` |
| Safe retries | `Idempotency-Key` header, UNIQUE in the DB | `test_same_idempotency_key_sent_concurrently_posts_once` |
| All-or-nothing | one DB transaction per operation | `test_insufficient_funds_changes_nothing` |
| Tampering is detectable | `/admin/reconciliation` | `test_reconciliation_detects_tampering` |
| Tenants are isolated | every query scoped to the API client; cross-tenant = 404 | `test_tenants_cannot_see_or_touch_each_others_money` |
| Refund exactly once, even under races | compensating transaction + `UNIQUE(reversal_of_id)` + row lock | `test_concurrent_reversals_refund_exactly_once` |
| No lost events | outbox row committed in the same DB transaction as the money | `test_replays_and_failures_write_no_extra_events` |
| No double delivery across workers | `FOR UPDATE SKIP LOCKED` | `test_parallel_workers_never_deliver_the_same_event_twice` |
| Fair use | per-client token bucket, `429` + `Retry-After` | `test_api_returns_429_with_retry_after` |

Amounts are **integers in minor units** (paise): `10050` = ₹100.50. Never floats.

## Run
```bash
docker compose up --build            # Postgres + migrations + API on http://localhost:8000/docs
# or locally:
docker compose up -d postgres && cp .env.example .env
uv sync && uv run alembic upgrade head && uv run uvicorn app.main:app --reload
```
```bash
# 1. operator creates a client (key is shown once; set ADMIN_API_KEY first, see .env.example)
curl -s -XPOST localhost:8000/api/v1/admin/clients -H 'X-Admin-Key: <ADMIN_API_KEY>' \
     -H 'content-type: application/json' -d '{"name":"Acme Shop"}'          # -> {"id":..., "api_key":"pl_..."}
# 2. the client uses its key
curl -s -XPOST localhost:8000/api/v1/accounts -H 'X-API-Key: <KEY>' -H 'content-type: application/json' \
     -d '{"owner_name":"Asha","currency":"INR"}'
curl -s -XPOST localhost:8000/api/v1/accounts/<ID>/deposits -H 'X-API-Key: <KEY>' -H 'Idempotency-Key: dep-0001-aaaa' \
     -H 'content-type: application/json' -d '{"amount":50000}'
# 3. refund it (once)
curl -s -XPOST localhost:8000/api/v1/transactions/<TX_ID>/reversals -H 'X-API-Key: <KEY>' \
     -H 'Idempotency-Key: rev-0001-aaaa' -H 'content-type: application/json' -d '{"description":"customer refund"}'
```

## Tests
```bash
uv run pytest                                  # 64 tests on in-memory SQLite, instant
TEST_DATABASE_URL=postgresql+asyncpg://ledger_user:ledger_password@localhost:5432/payment_ledger uv run pytest
                                               # all 70, incl. concurrency, SKIP LOCKED and migration checks (wipes that DB!)
```

## Endpoints (`/api/v1`)
Client endpoints (header `X-API-Key`; money movements also need `Idempotency-Key`):
`POST /accounts` · `GET /accounts/{id}` · `GET /accounts/{id}/entries` · `POST /accounts/{id}/deposits` ·
`POST /accounts/{id}/withdrawals` · `POST /transfers` · `POST /transactions/{id}/reversals` · `GET /transactions/{id}`

Operator endpoints (header `X-Admin-Key`, or `Authorization: Bearer` for Prometheus):
`GET /admin/reconciliation` · `POST /admin/clients` · `POST /admin/clients/{id}/deactivate` ·
`GET /admin/outbox` · `POST /admin/outbox/dispatch` · `GET /metrics`

Public: `GET /health`. Errors are always `{"error": {"code", "message", "details"}}`.

## Webhooks
Every committed money movement emits `transaction.deposit | withdrawal | transfer | reversal` to `WEBHOOK_URL`
(POST, JSON). Verify `X-Ledger-Signature` = `sha256=` + HMAC-SHA256(`WEBHOOK_SECRET`, raw body), and de-duplicate on
`X-Ledger-Event-Id` (delivery is at-least-once, retried with exponential backoff, then marked `DEAD`).
Run `python -m app.worker` (the compose file starts one), or call `POST /admin/outbox/dispatch` from a cron.

## Not built (deliberately)
Multi-currency FX, partial refunds, key scopes/rotation, Redis-backed rate limits - see the "Known limits" table in
[ARCHITECTURE.md](ARCHITECTURE.md) for why and how each would be done.

## Deploy for free
1. **Database:** create a free project on [Neon](https://neon.com) and copy its connection string. Paste it as-is: the app converts `postgresql://…?sslmode=require` into the form its async driver needs.
2. **API:** on [Render](https://render.com) choose *New → Blueprint* (or *Web Service → Docker*) and select this repo. `render.yaml` is picked up; set `DATABASE_URL` and `ADMIN_API_KEY` when asked (`WEBHOOK_URL`/`WEBHOOK_SECRET` are optional). Each start runs `alembic upgrade head`, then serves the API on Render's `$PORT`.
3. Open `https://<your-service>.onrender.com/docs` for the interactive API.

Free-tier behaviour to expect: Render sleeps the service after 15 minutes without traffic and needs about a minute to wake it; Neon also suspends an idle database and wakes on the next query.

After the first deploy: create your client with `POST /api/v1/admin/clients` (see "try it") and send its key in the
`X-API-Key` header. Existing data from before multi-tenancy is adopted by a `legacy` client during the migration.
