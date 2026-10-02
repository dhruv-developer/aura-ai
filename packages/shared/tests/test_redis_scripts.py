import asyncio

import redis.asyncio as aioredis

from aura_shared.redis import KeyFamily, RedisScripts, build_key
from aura_shared.redis.scripts import QuotaKeys


async def test_gcra_allows_burst_then_denies_then_recovers(redis_client: aioredis.Redis) -> None:
    s = RedisScripts(redis_client)
    key = build_key(KeyFamily.RATE_LIMIT, "acct_1", "key_1")
    now = 1_000_000
    results = [await s.rate_limit(key, rate_per_s=10, burst=5, now_ms=now) for _ in range(6)]
    assert [r.allowed for r in results] == [True] * 5 + [False]
    assert results[-1].retry_after_ms == 100
    later = await s.rate_limit(key, rate_per_s=10, burst=5, now_ms=now + 100)
    assert later.allowed


async def test_gcra_is_atomic_under_concurrency(redis_client: aioredis.Redis) -> None:
    s = RedisScripts(redis_client)
    key = build_key(KeyFamily.RATE_LIMIT, "acct_1", "burst")
    res = await asyncio.gather(
        *[s.rate_limit(key, rate_per_s=1, burst=20, now_ms=5_000) for _ in range(100)]
    )
    assert sum(r.allowed for r in res) == 20


async def test_quota_reserve_never_overconsumes_under_concurrency(
    redis_client: aioredis.Redis,
) -> None:
    s = RedisScripts(redis_client)
    keys = QuotaKeys.for_("acct_1", "executions", "2026-10")
    res = await asyncio.gather(
        *[s.quota_reserve(keys, limit=20, amount=1, reservation_id=f"r{i}") for i in range(50)]
    )
    assert sum(r.ok for r in res) == 20
    assert int(await redis_client.get(keys.used)) == 20


async def test_quota_commit_adjusts_to_actual_and_release_refunds(
    redis_client: aioredis.Redis,
) -> None:
    s = RedisScripts(redis_client)
    keys = QuotaKeys.for_("acct_1", "tokens", "2026-10")
    assert (await s.quota_reserve(keys, limit=1000, amount=300, reservation_id="a")).used == 300
    # Reserving the same id twice is idempotent.
    assert (await s.quota_reserve(keys, limit=1000, amount=300, reservation_id="a")).used == 300
    assert (await s.quota_commit(keys, reservation_id="a", actual=120)).used == 120
    assert not (await s.quota_commit(keys, reservation_id="a", actual=120)).ok  # already done
    assert (await s.quota_reserve(keys, limit=1000, amount=500, reservation_id="b")).used == 620
    assert (await s.quota_release(keys, reservation_id="b")).used == 120


async def test_expired_reservations_are_reclaimed(redis_client: aioredis.Redis) -> None:
    s = RedisScripts(redis_client)
    keys = QuotaKeys.for_("acct_1", "credits", "2026-10")
    await s.quota_reserve(
        keys, limit=10, amount=10, reservation_id="dead", lease_ms=1_000, now_ms=0
    )
    blocked = await s.quota_reserve(keys, limit=10, amount=1, reservation_id="x", now_ms=500)
    assert not blocked.ok
    ok = await s.quota_reserve(keys, limit=10, amount=1, reservation_id="y", now_ms=2_000)
    assert ok.ok and ok.used == 1


async def test_lease_release_requires_owner_token(redis_client: aioredis.Redis) -> None:
    s = RedisScripts(redis_client)
    key = build_key(KeyFamily.LOCK, "_global", "reconciler")
    assert await s.lease_acquire(key, "me", 5_000)
    assert not await s.lease_acquire(key, "other", 5_000)
    assert not await s.lease_release(key, "other")
    assert await s.lease_release(key, "me")


async def test_scripts_survive_script_flush(redis_client: aioredis.Redis) -> None:
    """EVALSHA falls back to EVAL on NOSCRIPT (e.g. after failover)."""
    s = RedisScripts(redis_client)
    key = build_key(KeyFamily.RATE_LIMIT, "acct_1", "flush")
    await s.rate_limit(key, rate_per_s=1, burst=1)
    await redis_client.script_flush()
    await s.rate_limit(key, rate_per_s=1, burst=5)
