# ADR-0007: Runtime, providers and routing (V1)

**Status:** accepted · **Date:** 2026-10-01

## Decisions

**Provider layer.**
- `ModelProvider.stream()` must be an async generator, because the engine calls `aclose()` to cancel the upstream request when a client disconnects.
- Every adapter maps native errors to `ProviderError(kind)`. Rate-limited, overloaded, unavailable and timeout errors are retryable and count against model health. Bad requests and auth/configuration problems are neither.
- Anthropic uses the official `anthropic` SDK, as required for Claude integrations in Python. Opus 5.5 and Sonnet 5.5 opt into server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`).
- Sampling parameters are dropped, with a note in the response, for models that reject them. Thinking is never sent, so models use their defaults. Only `text` blocks form the answer.
- Other vendors (OpenAI, OpenRouter, Google, vLLM, Ollama, Indian hosts) go through one OpenAI-compatible adapter.

**Catalog and money.**
- Prices are Decimal USD per MTok, in `models.yaml` with `prices_as_of`.
- The catalog's content hash is recorded on every execution, so historical costs can be reproduced.
- Billing uses provider-reported usage, never local estimates. The estimates exist only for routing and budget checks.

**Routing (`rules-v1`).**
- Hard constraints filter first: policy, residency, context fit, budget (which also clamps `max_tokens`), and open circuits.
- Then a tier is chosen, from the quality mode or a transparent complexity score.
- Then the cheapest model in that tier is picked, with a ranked fallback chain behind it.
- Explicitly requested models are never substituted.
- Decisions log features and propensity, so V4 learning has off-policy data.

**Engine.**
- `prepare()` reserves quota and routes before any bytes are sent, so failures get proper HTTP status codes.
- Fallback crosses models only for retryable errors. In a stream, it is allowed only before the first token.
- Circuit breakers live in Redis, are shared across pods, and stay off the success path.
- Quota is committed on success and released on any failure or disconnect.
- Frontier calls count against `premium_model_calls` where a plan meters it. When that quota is exhausted, frontier models are skipped instead of failing the request.

**Unsupported features are rejected explicitly.** Tools, `n>1`, JSON response formats, images and tool messages return 422. They are never silently dropped, because a client that believes a tool was offered must not get an answer that silently ignored it.

**Telemetry is at-least-once with effectively-once storage.**
- Publishing goes through a bounded, non-blocking queue, using JetStream `Nats-Msg-Id` for dedupe.
- Workers write to ReplacingMergeTree tables keyed on `event_id`.
- An event is claimed as seen only after the write succeeds.
- Quotas never depend on telemetry.

## Known gaps (tracked)
- BYOK credential storage: `CredentialResolver` is the extension point.
- Tool calling and multimodal input.
- Session memory (`session_id` is accepted and logged; ADR-0004).
- The NATS publisher/consumer and the ClickHouse DDL have not been exercised against live servers in CI. A docker-compose job will cover them.
