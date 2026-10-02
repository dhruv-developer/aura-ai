# aura-db

**Responsibility:** the Postgres schema (SQLAlchemy models + Alembic migrations) and the
`Database` session factory that enforces tenant isolation.

- `Database.tenant(account_id)`: runs as role `aura_app` with `aura.account_id` set. Row-Level Security limits every tenant-owned table to that account.
- `Database.system()`: runs as role `aura_system` for cross-tenant control-plane work (signup, webhooks, API-key lookup, superadmin). Every call site must justify why it needs cross-tenant access.
- `audit.record()`: an append-only audit log. Updates and deletes are blocked by a trigger.
- `outbox.enqueue()`: a transactional outbox. Events are written in the same transaction as the state change, and a relay publishes them to NATS.

**Does not do:** business rules. Those live in each package's `domain/` and `application/`.

```bash
AURA_DATABASE_URL=postgresql+asyncpg://aura:aura@localhost:5432/aura \
  uv run alembic -c packages/db/alembic.ini upgrade head
```
