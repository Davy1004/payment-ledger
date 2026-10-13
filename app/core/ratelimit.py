"""Token-bucket rate limiter, one bucket per API client.

A bucket holds up to `capacity` tokens and refills continuously at capacity/60 per second, so
a client may burst up to `capacity` requests and then settle at `capacity` per minute.

Limits of this implementation (deliberate, documented in ARCHITECTURE.md): the buckets live in
this process's memory, so with N API instances a client effectively gets N times the limit.
Going multi-instance means moving the buckets to Redis (atomic Lua script); the interface
below (`check(key) -> retry_after | None`) would not change.
"""
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def reset(self) -> None:
        self._buckets.clear()

    def check(self, key: str, per_minute: int) -> float | None:
        """Take one token. Returns None if allowed, else seconds until a token is available."""
        if per_minute <= 0:
            return None
        now = self._clock()
        rate = per_minute / 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(tokens=float(per_minute), updated=now)
        bucket.tokens = min(float(per_minute), bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None
        return (1 - bucket.tokens) / rate


limiter = TokenBucketLimiter()
