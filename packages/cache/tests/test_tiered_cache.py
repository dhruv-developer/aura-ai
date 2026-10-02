import asyncio
import contextlib
import random
from typing import Any

import redis.asyncio as aioredis

from aura_cache import L1Cache, RedisL2, TieredCache
from aura_shared.redis import KeyFamily, RedisScripts


def _pod(client: aioredis.Redis, **kw: Any) -> TieredCache:
    return TieredCache(
        KeyFamily.POLICY,
        l1=L1Cache(1_000),
        l2=RedisL2(client),
        scripts=RedisScripts(client),
        rng=random.Random(1),
        xfetch_beta=0.0,  # deterministic: disable early refresh unless a test enables it
        **kw,
    )


def test_l1_ttl_and_lru_eviction() -> None:
    now = [0.0]
    l1 = L1Cache(2, clock=lambda: now[0])
    l1.set("a", 1, 10)
    l1.set("b", 2, 10)
    l1.get("a")
    l1.set("c", 3, 10)  # evicts b (least recently used)
    assert l1.get("b", None) is None and l1.get("a") == 1
    now[0] = 11
    assert l1.get("a", None) is None


async def test_read_through_l1_then_l2(redis_client: aioredis.Redis) -> None:
    calls = 0

    async def load() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"v": 1}

    pod_a, pod_b = _pod(redis_client), _pod(redis_client)
    assert await pod_a.get_or_load("acct_1", "wl_1", loader=load) == {"v": 1}
    assert await pod_a.get_or_load("acct_1", "wl_1", loader=load) == {"v": 1}
    assert await pod_b.get_or_load("acct_1", "wl_1", loader=load) == {"v": 1}
    assert calls == 1
    assert pod_a.stats.l1_hits == 1 and pod_b.stats.l2_hits == 1


async def test_stampede_across_pods_loads_once(redis_client: aioredis.Redis) -> None:
    calls = 0

    async def slow_load() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "policy-v7"

    pods = [_pod(redis_client) for _ in range(4)]
    results = await asyncio.gather(
        *[pods[i % 4].get_or_load("acct_1", "hot", loader=slow_load) for i in range(1_000)]
    )
    assert set(results) == {"policy-v7"}
    assert calls == 1


async def test_tenants_are_isolated(redis_client: aioredis.Redis) -> None:
    pod = _pod(redis_client)

    async def load_a() -> str:
        return "A"

    async def load_b() -> str:
        return "B"

    assert await pod.get_or_load("acct_a", "same", loader=load_a) == "A"
    assert await pod.get_or_load("acct_b", "same", loader=load_b) == "B"


async def test_invalidate_clears_tiers_and_publishes(redis_client: aioredis.Redis) -> None:
    published: list[str] = []

    async def publish(key: str) -> None:
        published.append(key)

    version = 1

    async def load() -> int:
        return version

    pod = _pod(redis_client, publisher=publish)
    assert await pod.get_or_load("acct_1", "p", loader=load) == 1
    version = 2
    await pod.invalidate("acct_1", "p")
    assert published == [pod.key("acct_1", "p")]
    assert await pod.get_or_load("acct_1", "p", loader=load) == 2


async def test_loader_failure_propagates_and_is_not_cached(redis_client: aioredis.Redis) -> None:
    pod = _pod(redis_client)
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("db down")
        return "ok"

    with contextlib.suppress(RuntimeError):
        await pod.get_or_load("acct_1", "f", loader=flaky)
    assert await pod.get_or_load("acct_1", "f", loader=flaky) == "ok"


async def test_xfetch_refreshes_hot_key_before_expiry(redis_client: aioredis.Redis) -> None:
    calls = 0

    async def expensive() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)  # nonzero compute time drives the XFetch lead
        return calls

    pod = TieredCache(
        KeyFamily.POLICY,
        l1=L1Cache(10),
        l2=RedisL2(redis_client),
        scripts=RedisScripts(redis_client),
        ttl_s=1,
        l1_ttl_s=0.0001,
        xfetch_beta=1e6,
    )
    await pod.get_or_load("acct_1", "x", loader=expensive)
    await asyncio.sleep(0.001)
    await pod.get_or_load("acct_1", "x", loader=expensive)
    assert pod.stats.early_refreshes >= 1 and calls == 2


async def test_redis_failures_degrade_to_source_not_errors(redis_client: aioredis.Redis) -> None:
    from redis.exceptions import TimeoutError as RedisTimeout

    class BrokenL2:
        async def get(self, key: str) -> Any:
            raise RedisTimeout("Timeout reading from redis")

        async def set(self, *a: Any, **kw: Any) -> None:
            raise ConnectionError("redis down")

        async def delete(self, *keys: str) -> None:
            raise RedisTimeout("down")

    pod = TieredCache(
        KeyFamily.POLICY,
        l1=L1Cache(10),
        l2=BrokenL2(),  # type: ignore[arg-type]
        scripts=RedisScripts(redis_client),
        xfetch_beta=0.0,
    )
    calls = 0

    async def load() -> str:
        nonlocal calls
        calls += 1
        return "from-postgres"

    assert await pod.get_or_load("acct_1", "k", loader=load) == "from-postgres"
    assert await pod.get_or_load("acct_1", "k", loader=load) == "from-postgres"  # L1 still works
    assert calls == 1 and pod.stats.degraded >= 2
    await pod.invalidate("acct_1", "k")  # must not raise
