# aura-billing

**Responsibility:** what an account is entitled to, and what it owes.

- `catalog.yaml`: the single source of plans, features and entitlements. `sync_catalog` upserts it and creates a **new plan version** whenever a plan's price or entitlements change. Existing subscribers stay pinned to their version (grandfathering).
- `EntitlementService`: the only way product code gates features (`require`, `limit`, `reserve → commit | release`). It is cached through `TieredCache`, and metered limits are enforced atomically in Redis. Never write `if plan == "pro"`.
- `SubscriptionService`: default plans at signup, checkout through the `PaymentGateway` port, idempotent webhook processing, and admin plan changes (audited).
- `CreditService`: a double-entry credit ledger. A database trigger rejects unbalanced transactions, postings are idempotent by reference, and the wallet row is locked during posting.
- `UsageReconciler`: snapshots Redis counters into Postgres. Counters are monotonic, so the higher value wins in either direction.

**Does not do:** identity or HTTP.
