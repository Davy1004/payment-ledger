# Payment Transaction Ledger

A small **double-entry ledger** API: accounts, deposits, withdrawals and transfers where money can never be created,
lost, double-spent or applied twice.

**Stack:** FastAPI · async SQLAlchemy 2 · PostgreSQL · Alembic · pytest (+ real-Postgres concurrency tests)

## Guarantees (and the test that proves each)
| Guarantee | How | Test |
|---|---|---|
| Money is conserved | every transaction's entries sum to 0 | `test_reconciliation_*` |
| No overdraft, even under races | `SELECT … FOR UPDATE` + DB `CHECK (balance >= 0)` | `test_cannot_overspend_with_concurrent_withdrawals` |
| No deadlocks | locks always taken in id order | `test_opposite_transfers_do_not_deadlock` |
| Safe retries | `Idempotency-Key` header, UNIQUE in the DB | `test_same_idempotency_key_sent_concurrently_posts_once` |
| All-or-nothing | one DB transaction per operation | `test_insufficient_funds_changes_nothing` |
| Tampering is detectable | `/admin/reconciliation` | `test_reconciliation_detects_tampering` |

Amounts are **integers in minor units** (paise): `10050` = ₹100.50. Never floats.

## Run
```bash
docker compose up --build            # Postgres + migrations + API on http://localhost:8000/docs
# or locally:
docker compose up -d postgres && cp .env.example .env
uv sync && uv run alembic upgrade head && uv run uvicorn app.main:app --reload
```
```bash
# try it
curl -s -XPOST localhost:8000/api/v1/accounts -H 'content-type: application/json' -d '{"owner_name":"Asha","currency":"INR"}'
curl -s -XPOST localhost:8000/api/v1/accounts/<ID>/deposits -H 'Idempotency-Key: dep-0001-aaaa' \
     -H 'content-type: application/json' -d '{"amount":50000}'
```

## Tests
```bash
uv run pytest                                  # 21 tests on in-memory SQLite, instant
TEST_DATABASE_URL=postgresql+asyncpg://ledger_user:ledger_password@localhost:5432/payment_ledger uv run pytest
                                               # all 25, incl. concurrency + migration check (wipes that DB!)
```

## Endpoints (`/api/v1`)
`POST /accounts` · `GET /accounts/{id}` · `GET /accounts/{id}/entries` · `POST /accounts/{id}/deposits` ·
`POST /accounts/{id}/withdrawals` · `POST /transfers` · `GET /transactions/{id}` · `GET /admin/reconciliation`

## Not built (deliberately)
Authentication, reversals/refunds (add a compensating transaction), multi-currency FX, outbox/event publishing.
