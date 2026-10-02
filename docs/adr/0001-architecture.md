# ADR-0001: Hexagonal packages, control plane / data plane split

**Status:** accepted · **Date:** 2026-10-01

## Decision
- Every Python package follows ports and adapters: `domain/` (pure), `application/` (use cases), `ports/` (Protocols), `adapters/` (I/O). Apps are thin composition roots.
- The **control plane** (`apps/api`) owns Postgres truth. The **data plane** (`apps/runtime`) is stateless and reads control-plane state only via the L1 and L2 caches. Postgres is never on the synchronous hot path, except for a single-flight cache miss.
- Control-plane writes go through a transactional outbox, then NATS `aura.config.changed`, then per-pod L1 invalidation.
- Package dependencies point inward, toward `aura_shared`. This is enforced by import-linter in CI.

## Consequences
Runtime pods scale horizontally with no coordination. Config propagation is eventually consistent, with staleness bounded by L1 TTL (≤60 s) even if messages are lost.
