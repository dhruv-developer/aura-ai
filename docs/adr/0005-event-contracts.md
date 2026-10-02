# ADR-0005: Event contracts and delivery

**Status:** accepted · **Date:** 2026-10-01

## Decision
Events are immutable Pydantic models in `aura_shared.events`. Each has a `type`, `schema_version` and an `event_id` used for idempotency.

- **Subjects:** `aura.<stream>.<region>.p<NN>.<type>`, with the partition computed as crc32(account_id) % 16.
- **Delivery:** JetStream durable consumers with explicit ack, max-deliver limits and dead-lettering to `aura.dlq.*`.
- **Idempotency:** consumers dedupe on `event_id`.
- **Schema evolution:** changes are additive. A breaking change requires a new `schema_version`, and consumers must handle both versions during rollout.
- **Routing data:** routing decisions log features and propensity from day one, so offline policy learning (V4) has unbiased data.
