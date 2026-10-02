# ADR-0006: Persistence, tenant isolation, sessions and billing integrity

**Status:** accepted · **Date:** 2026-10-01

## Decisions

**1. A shared `aura_db` package owns the schema; domain layers stay pure.**
There is one Alembic history for the whole control plane. Application services use SQLAlchemy through `aura_db`. Protocol ports are reserved for *external* services (mailer, OAuth providers, payment gateway). This is a deliberate simplification of ADR-0001: two implementations of every repository would add cost without isolating anything that matters. Integration tests run against real Postgres instead of fakes. Import-linter enforces that `*.domain` never imports SQLAlchemy, Redis, httpx, FastAPI or `aura_db`.

**2. Tenant isolation is defence in depth: application scoping plus Postgres RLS.**
- There are two NOLOGIN group roles.
  - `aura_app` sees tenant tables only where `account_id = current_setting('aura.account_id')`. With no tenant bound, it sees nothing (fails closed).
  - `aura_system` is for cross-tenant control-plane work: signup, webhooks, API-key lookup, superadmin.
- `Database.tenant()` and `Database.system()` use `SET LOCAL ROLE`, so pooled connections never leak a role or tenant between transactions.
- `aura_app` gets least privilege. It has INSERT but no SELECT on `audit_log`. On `outbox` it has INSERT plus `SELECT (id)` only. Because of that, the ORM sets timestamps client-side on those tables.

**3. Sessions are opaque and server-side (Redis), not JWTs.**
Logout, "log out everywhere" and suspension take effect immediately. There is no signing-key rotation to manage, and control-plane request rates make a Redis lookup free. The cookie is `HttpOnly` and `SameSite=Lax`. Cookie-authenticated unsafe methods also require an `X-AURA-CSRF: 1` header, which cross-site forms cannot send.

**4. API keys: `aura_sk_<env>_<prefix>_<secret>`, stored as HMAC-SHA256(pepper, key).**
The keys are high-entropy, so a fast keyed hash is appropriate and keeps authentication off the hot path's latency budget. Lookup by prefix is cached for 60 s, including negative results. Revocation invalidates the cache directly, and the TTL bounds staleness on other pods.

**5. Plans are versioned and immutable. Entitlements are the only feature gate.**
A catalog change to price or entitlements creates a new `plan_version`, and existing subscriptions stay pinned to theirs. Product code calls `EntitlementService`. Metered limits use Redis `reserve → commit | release`. The `UsageReconciler` makes Postgres durable, and the higher value wins in both directions.

**6. The money trail is tamper-evident.**
- `audit_log` and ledger tables are append-only (enforced by triggers).
- Ledger transactions must balance to zero (deferred constraint trigger).
- Credit postings are idempotent by reference and serialized by locking the wallet row.
- Webhooks are stored once (unique on provider event id) and processed in the same transaction. A failure rolls back both, so the provider retries.
- Out-of-order gateway events cannot resurrect a canceled subscription.

## Consequences
- Hard-deleting an account (a future "forget me" job) must deliberately bypass the append-only triggers on ledger rows.
- Entitlement caches invalidate locally now. The outbox rows written on change are ready for the NATS relay that broadcasts `aura.config.changed` to other pods. That relay is not built yet; until it exists, the L1 TTL (30 s) bounds cross-pod staleness.
