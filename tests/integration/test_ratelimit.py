import pytest

from app.core.config import settings
from app.core.ratelimit import TokenBucketLimiter
from tests.conftest import make_tenant



def test_bucket_allows_a_burst_then_refills():
    t = [0.0]
    lim = TokenBucketLimiter(clock=lambda: t[0])
    assert [lim.check("k", 60) for _ in range(60)] == [None] * 60   # full bucket = burst of 60
    wait = lim.check("k", 60)
    assert wait == pytest.approx(1.0)                                # 60/min = 1 token per second
    t[0] += 1.0
    assert lim.check("k", 60) is None
    assert lim.check("k", 60) is not None


def test_buckets_are_per_key_and_zero_disables():
    lim = TokenBucketLimiter(clock=lambda: 0.0)
    assert lim.check("a", 1) is None and lim.check("a", 1) is not None
    assert lim.check("b", 1) is None                                # another client is unaffected
    assert all(lim.check("c", 0) is None for _ in range(1000))


def test_bucket_never_exceeds_capacity_after_idle():
    t = [0.0]
    lim = TokenBucketLimiter(clock=lambda: t[0])
    lim.check("k", 5)
    t[0] += 10_000
    assert [lim.check("k", 5) for _ in range(5)] == [None] * 5
    assert lim.check("k", 5) is not None


async def test_api_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    codes = [(await client.get("/api/v1/accounts/00000000-0000-0000-0000-000000000000")).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]
    r = await client.get("/api/v1/accounts/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1


async def test_one_noisy_client_does_not_throttle_another(client, make_client, session_maker, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    other = await make_client((await make_tenant(session_maker, "calm"))[1])
    for _ in range(5):
        await client.get("/api/v1/accounts/00000000-0000-0000-0000-000000000000")
    assert (await other.get("/api/v1/accounts/00000000-0000-0000-0000-000000000000")).status_code == 404


async def test_bad_keys_do_not_consume_a_clients_budget(client, make_client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    attacker = await make_client("pl_wrong")
    for _ in range(10):
        assert (await attacker.get("/api/v1/accounts/00000000-0000-0000-0000-000000000000")).status_code == 401
    assert (await client.get("/api/v1/accounts/00000000-0000-0000-0000-000000000000")).status_code == 404
