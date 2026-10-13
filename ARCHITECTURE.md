# Architecture & scaling notes

What is **built and tested** in this repo, and what the **next steps to production scale** would be and why.
Being honest about that line matters more than a long feature list.

## Request path

```
client ──X-API-Key──▶ FastAPI ──▶ auth (hash lookup) ──▶ rate limit (token bucket) ──▶ LedgerService
                         │                                                                  │
                  request-id + JSON log + metrics                          ONE DB transaction:
                                                                           idempotency check → lock accounts (id order)
                                                                           → balance checks → entries + balances
                                                                           → outbox event → COMMIT
                                                                                  │
                                              worker (SKIP LOCKED) ◀──────── outbox_events ──▶ signed webhook
```

## Design decisions

| Decision | Why |
|---|---|
| Money = integer paise, double-entry, entries sum to 0 | no float error; money cannot appear or vanish; `/admin/reconciliation` proves it |
| Pessimistic row locks (`FOR UPDATE`) in id order | correctness over throughput: overdrafts are impossible and A→B / B→A never deadlock |
| Idempotency key `UNIQUE(client_id, key)` + request fingerprint | retries are safe; the *database* picks the winner of a race, not application code |
| Reversal = new compensating transaction, `UNIQUE(reversal_of_id)` | the ledger is append-only (auditable); "refund once" is enforced by the DB. The original row is locked first so concurrent refunds queue instead of failing with a confusing error |
| API keys stored as SHA-256 hashes | a database leak does not leak usable keys; a fast hash is fine for long random keys (unlike passwords) |
| Cross-tenant access returns 404, never 403 | does not reveal that another tenant's account exists |
| Transactional outbox | "DB committed but the message was lost" cannot happen: the event is committed with the money, and delivered later at-least-once (receivers de-duplicate on `X-Ledger-Event-Id`) |
| Webhook HMAC signature | receiver can verify the call came from the ledger (`X-Ledger-Signature: sha256=…`) |
| Metrics labelled by route *template* | `/accounts/{account_id}/deposits`, not the raw URL, so label cardinality stays bounded |

## Known limits (and the fix)

| Limit today | Impact | Next step |
|---|---|---|
| Rate-limit buckets live in process memory | with N instances a client gets N× the limit; resets on restart | move buckets to Redis with an atomic Lua script. `TokenBucketLimiter.check(key, per_minute)` is the only interface callers use |
| One shared SYSTEM account per currency | every deposit/withdrawal across all tenants serialises on that one row (a *hot row*) | shard the system account (N rows per currency, pick by hash of the transaction) or give each tenant its own; the reconciliation sum is unchanged |
| Admin dispatch/worker are separate processes | on free hosting there is no worker dyno | run `python -m app.worker` where workers exist; otherwise call `POST /admin/outbox/dispatch` from a cron or GitHub Action |
| Single Postgres primary | all writes go through one node | vertical scale first; then partition `ledger_entries` by time and move statements to a read replica (replica lag is acceptable for statements, never for balance checks) |
| No auth scopes / key rotation | one key per client with full access | add scopes (read-only keys) and two active keys per client for zero-downtime rotation |

## Going to many instances (designed, not built)

1. **Load balancer** (nginx / cloud LB) in front of N identical stateless API containers. State is only in Postgres, so any instance can serve any request; idempotency and locking already assume concurrent instances (the concurrency tests simulate that with parallel sessions).
2. **Connection pool sizing.** Each instance holds `pool_size` connections; total = instances × pool_size must stay below Postgres `max_connections` (typically ~100). Past that, put **PgBouncer** in *transaction pooling* mode between the API and Postgres. (asyncpg prepared statements need `statement_cache_size=0` behind PgBouncer.)
3. **Redis** for shared rate limits, and optionally a short-TTL cache for *statement* reads only. Balance checks always read the locked row, never the cache.
4. **Workers**: scale `app.worker` horizontally; `SKIP LOCKED` already prevents two workers claiming the same event.
5. **Observability**: scrape `/metrics` with Prometheus (`Authorization: Bearer <ADMIN_API_KEY>`), alert on `outbox` pending age, `ledger_rejections_total{code="insufficient_funds"}` spikes, 5xx rate and p99 latency; ship the JSON logs to any log store and search by `request_id`.
6. **Secrets** (DB URL, admin key, webhook secret) come from the platform's secret manager, never from the repo.
