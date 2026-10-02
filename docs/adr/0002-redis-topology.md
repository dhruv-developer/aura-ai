# ADR-0002: Redis topology, key conventions and atomicity

**Status:** accepted · **Date:** 2026-10-01

## Decision
Three logical Redis roles, so that eviction in one never affects another:

| Role | Data | Policy |
|---|---|---|
| `state` | sessions, rate limits, quota counters, circuit breakers, locks, idempotency, working/session memory | `noeviction`, AOF |
| `cache` | policies, entitlements, catalog, provider health, exact response/retrieval/tool caches | `allkeys-lfu`, no persistence |
| `vector` | semantic cache and memory vectors | `volatile-lfu` |

- Keys are built **only** by `aura_shared.redis.build_key`, using the layout `aura:{env}:{domain}:{{tenant}}:{parts}:v{schema}`. The tenant is a Cluster hash tag, so multi-key scripts stay single-slot.
- Every cache family has a TTL in `aura_shared.redis.ttl`, with ±10% jitter.
- Multi-step mutations are Lua scripts (`gcra_rate_limit`, `quota_reserve/commit/release`, `lease_release`), run via EVALSHA with NOSCRIPT fallback.
- Quotas use a **reserve → commit | release** pattern. Leases on reservations mean a crashed pod cannot leak quota. A reconciler flushes counters to the Postgres ledger, and Postgres wins on mismatch.
- `KEYS`, `FLUSHALL` and `FLUSHDB` are banned in application code by a pre-commit hook.
