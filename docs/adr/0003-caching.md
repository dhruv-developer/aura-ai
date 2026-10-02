# ADR-0003: Multi-tier caching

**Status:** accepted · **Date:** 2026-10-01

## Decision
Tiers:
- **L0:** per-request memo.
- **L1:** in-process TTL/LRU with a short TTL.
- **L2:** Redis `cache` role.
- **L3:** semantic and result caches on the `vector` role plus S3, added in V2.

`aura_cache.TieredCache` provides:
- in-process single-flight over the entire miss path;
- cross-pod single-flight via a Redis lease, where waiters poll L2 and then fall back to loading, so they never deadlock;
- serve-stale during a contended early refresh;
- XFetch probabilistic early refresh.

Cache scope is always tenant-bound. Nothing is cached across tenants. Billing data is never write-behind.

## Verification
`packages/cache/tests/test_tiered_cache.py` runs 1,000 concurrent requests across 4 simulated pods and checks for exactly one load.
