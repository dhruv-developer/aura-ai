# ADR-0009: V2 optimizers and policy deployment

**Status:** accepted · **Date:** 2026-10-01

## Policy engine (spec §13–14, §61, §152–153)
- **Scopes:** policies apply at account or workload level (the client sends `aura.workload`). They have immutable versions with lifecycle `draft → canary → active` (or `rolled_back`/`superseded`).
- **Merge order:** account, then workload, then request. Hard limits are stricter-wins at every step. New dimensions follow the same rule:
  - `cache_mode`: off < exact < semantic.
  - `context_mode`: strict < auto < budget.
  - Budgets take the minimum. Similarity thresholds take the maximum.
- **Evidence gate:** a version deploys only if it pins `allowed_models` to models that a *completed* shadow run recommended. Anything else needs an audited `force_reason`, because an unpinned policy proves nothing.
- **Canary:**
  - Assignment is a deterministic hash bucket, so it is sticky per user and monotonic as stages grow.
  - Per-variant counters live in Redis, and an execution→variant map feeds `POST /v1/outcomes`.
  - The monitor advances 1→5→10→25→50→100% after minimum samples and dwell time. It rolls back automatically on error-rate, latency, cost-per-request or outcome-success regressions.
- **Experiments (§152):** the same machinery with a fixed 5–50% split, which only a human concludes (promote or stop).

## Response caching (§54)
- **Opt-in and isolated:** off by default. Keys are namespaced by account and routed model, so a cached answer can't cross tenants, models or policies. Only untruncated, non-refused answers are stored.
- **Semantic matching:**
  - It uses Redis 8 **vector sets**, one per account × model × system-prompt digest.
  - It applies only to single-turn requests.
  - It requires the `semantic_reuse` entitlement, otherwise it falls back to exact matching.
- **Accounting:** a hit costs nothing, still counts as an execution, and reports `cost_saved`.
- **Embedders:** behind a port. A local hashing embedder (with light stemming) is the default, and any OpenAI-compatible `/embeddings` endpoint can be configured.

## Context compiler (§49, §147)
- **Pipeline:** BM25 relevance (optionally fused with embedding rank via RRF), exact and shingle near-duplicate removal, query-aware extractive compression, budget packing, and edge ordering against "lost in the middle".
- **History:** long chat histories are compressed by keeping system and recent turns verbatim and squeezing older ones.
- **Default:** `strict`, meaning customer context is never altered unless a policy opts in.

## Long-term memory and RAG (ADR-0004)
- **Deviation:** pgvector is not available in this environment, so the ANN index uses Redis vector sets, with **Postgres as the source of truth** (`reindex` rebuilds the index). A pgvector adapter can replace it behind the same interface.
- **Full-text search:** an `english || simple` tsvector, which gives stemming plus language-agnostic matching. Queries use OR semantics built only from `\w` terms, which rules out tsquery injection.
- **Ranking:** RRF fusion of full-text and vector ranks (vector candidates below cosine 0.15 are dropped), then weighted by recency, importance and confidence.
- **Write path:** near-duplicates reinforce instead of duplicating. `forget` hard-deletes rows and vectors, and `decay` archives unused, low-importance items.
- **RAG tuning (§53):** a grid over top-k and budget against labelled questions, which recommends the cheapest configuration that meets the recall target.

## Prompt registry and optimizer (§50)
- **Registry:** templates with `{{var}}` placeholders, immutable versions, audited activation and rollback, cached resolution, and per-execution `prompt_version_id`.
- **Optimizer:**
  - It proposes deterministic rewrites (whitespace, duplicate instructions, filler, concise-output) that preserve every variable.
  - Each rewrite is replayed on the customer's eval set, which is sealed and erased afterwards.
  - It is scored against reference answers, or against the current prompt's own answers.
  - A rewrite is recommended only if the quality gate passes *and* prompt tokens drop at least 5%. Results are saved as drafts and never auto-activated.

## Recommendation engine (Phase 12)
Pure mapping from X-Ray opportunities plus shadow evidence to `policy` (an applicable draft spec), `run_shadow` (evidence needed) or `advice` (no policy field applies). Applying a recommendation creates a draft, or a new version of an existing workload policy.
