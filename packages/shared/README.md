# aura-shared

**Responsibility:** the foundation every other package builds on.

- `settings` — typed environment configuration.
- `errors` — `AuraError` hierarchy mapped to RFC 7807 problem+json.
- `ids` — prefixed, sortable identifiers.
- `tenancy` — `TenantContext`, the per-request isolation boundary.
- `redis` — the *only* place Redis keys are built (`keys`), TTL registry (`ttl`),
  connection pools per cluster role (`client`), atomic Lua scripts (`scripts`).
- `events` — versioned event contracts shared by runtime and workers.

**Does not do:** business logic, HTTP, persistence schemas. It must never import
another `aura_*` package (enforced by import-linter).
