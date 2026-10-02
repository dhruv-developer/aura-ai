# aura-providers

**Responsibility:** one normalized way to call any model, and know what it cost.

- `domain/types.py`: provider-neutral `ChatRequest`, `ChatResult`, `Usage` and stream events.
- `domain/catalog.py` + `models.yaml`: `ModelSpec` records with tier, context window, capabilities, region and versioned per-MTok prices.
- `domain/pricing.py`: exact cost from usage, plus a pre-flight estimate used by routing budgets.
- `adapters/`:
  - `AnthropicProvider`: official `anthropic` SDK, server-side refusal fallback on models that support it.
  - `OpenAICompatibleProvider`: OpenAI, OpenRouter, Google's OpenAI-compatible endpoint, vLLM, Ollama, and Indian model hosts.
  - `StubProvider`: deterministic, for tests and key-less local development.

Every adapter maps its native errors to `ProviderError(kind, retryable)`, which lets the runtime's fallback chain treat all providers the same way.

**Does not do:** choosing a model (`aura-routing`), retries across models (`aura-engine`), or storing credentials.
