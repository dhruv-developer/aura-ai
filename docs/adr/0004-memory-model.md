# ADR-0004: Short-term, long-term and hybrid memory

**Status:** accepted · **Date:** 2026-10-01

## Decision
Memory types and their stores:
- **Working memory** (per run): Redis hash on the `state` role, TTL-bound.
- **Session memory:** Redis list plus rolling summary.
- **Long-term episodic and semantic memory:** Postgres + pgvector, with provenance, confidence and validity windows.
- **Procedural memory:** policy versions and workflows.

Writes are asynchronous (extract, dedupe, resolve conflicts, store) and never on the hot path. Reads use hybrid retrieval: full-text and vector search plus structured filters, fused with RRF, scored as relevance × recency × importance × confidence, then packed into the policy token budget by the context compiler.

Every memory item carries `account_id` and a scope. Vector queries always pre-filter by tenant.
