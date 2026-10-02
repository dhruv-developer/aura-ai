# aura-cache

**Responsibility:** read-through, tenant-scoped, multi-tier caching (ADR-0003).

| Tier | Implementation |
|---|---|
| L1 | `L1Cache`: bounded in-process TTL/LRU, per pod |
| L2 | `RedisL2`: `redis-cache` role, versioned keys via `aura_shared.redis.build_key` |

`TieredCache.get_or_load()` provides:
- **In-process single-flight:** concurrent misses in one pod share one load.
- **Cross-pod single-flight:** a Redis `SET NX PX` lease. Losers poll L2 briefly, then fall back to loading themselves, so they never deadlock.
- **Probabilistic early refresh (XFetch):** hot keys recompute shortly before expiry, which avoids expiry storms.
- **Invalidation hooks:** `invalidate()` clears L1 and L2 and calls an optional publisher. Other pods call `invalidate_local()` from their NATS `config.changed` subscriber.

**Does not do:** semantic caching. That lives in the V2 semantic cache, built on `redis-vector`.
