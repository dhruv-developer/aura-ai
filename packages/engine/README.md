# aura-engine

**Responsibility:** run one execution end to end (spec §46, V1 subset).

```
reserve quota → resolve policy → route → try model chain (fallback on retryable errors,
circuit breaker shared via Redis) → price with the served model → commit/release quota
→ emit ExecutionCompleted (non-blocking)
```

- **Streaming:** the engine can fall back to another model only until the first token has been sent. After that, a failure ends the stream with an error event.
- **Premium quota:** frontier calls count against `premium_model_calls` where the plan meters it. When that quota is exhausted, frontier models are skipped and the next model in the chain serves the request, with a note in the response.
- **Telemetry:** delivery is best-effort and never blocks or fails a request. Quotas are enforced synchronously in Redis, so billing does not depend on telemetry.

**Does not do:** HTTP (`apps/runtime`), authentication, or learning.
