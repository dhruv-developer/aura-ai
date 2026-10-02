# AURA — AI Execution Optimization

One engine, two surfaces. **Start with [docs/AURA-GUIDE.md](docs/AURA-GUIDE.md)**: what AURA is, the problem it solves, how it works, and how to run and test it.

- [docs/STATUS.md](docs/STATUS.md): what's done and what's left.
- [docs/PROJECT-JOURNAL.md](docs/PROJECT-JOURNAL.md): everything built, how, and why (including the bugs we hit).
- [docs/AGENT-MODE-PLAN.md](docs/AGENT-MODE-PLAN.md): the plan for a Claude Code / Antigravity-style agent with AURA Auto.
- `docs/adr/`: architectural decisions.

## Layout
```
apps/        api (control plane :8000) · runtime (data plane :8100) · workers · web · admin · docs
packages/    shared · cache · db · http · auth · billing · credentials · memory · context · providers
             policy · routing · engine · evaluation · xray · shadow · deploy · prompts · graph · learning
infrastructure/  docker (local stack) · terraform · k8s
docs/adr/    architecture decision records
```

## Develop
```bash
uv sync                                                        # Python workspace
docker compose -f infrastructure/docker/compose.yml up -d     # local infra
AURA_DATABASE_URL=postgresql+asyncpg://aura:aura@localhost:5432/aura \
  uv run alembic -c packages/db/alembic.ini upgrade head        # schema, roles, RLS
uv run aura-api                                                # control-plane API on :8000 (syncs the plan catalog)
AURA_STUB_PROVIDER=true uv run aura-runtime                    # data plane on :8100 (stub models; set AURA_ANTHROPIC_API_KEY for Claude)
uv run aura-workers migrate-clickhouse && uv run aura-workers telemetry
uv run aura-workers jobs          # shadow runs and other background jobs
uv run aura-workers outbox-relay  # cross-pod cache invalidation
uv run aura-workers canary-monitor  # advance / roll back policy canaries
uv run aura-workers autopilot       # proven shadow results → canaries; consent-gated priors
uv run aura-workers credits         # monthly consumer credit grants + B2C margin alert
uv run pytest                                                  # spins up throwaway redis-server + Postgres if not provided
uv run ruff check . && uv run mypy && uv run lint-imports
```

### Frontends (Next.js)
```bash
pnpm install
pnpm --filter @aura/web dev     # http://localhost:3000 (marketing, X-Ray funnel, B2B dashboard)
pnpm --filter @aura/admin dev   # http://localhost:3001 (superadmin; needs TOTP)
uv run aura-api create-superadmin --email you@company.com
```
Both apps proxy `/v1/*` to the API (`AURA_API_URL`, default `http://localhost:8000`), and the web
app proxies `/rt/v1/*` to the runtime (`AURA_RUNTIME_URL`, default `http://localhost:8100`), so
session cookies are first-party and no CORS is needed. Set `NEXT_PUBLIC_B2C_APP=true` to open `/app`.

## Status
- [x] Repo foundation, CI, local infra, ADR-0001–0005
- [x] `aura-shared`: settings, errors (RFC 7807), ids, tenancy, Redis layer (keys, TTLs, pools, Lua: GCRA rate limit, quota reserve/commit/release, leases), event contracts
- [x] `aura-cache`: L1/L2 tiered cache, in-process + cross-pod single-flight, XFetch early refresh
- [x] `aura-db`: schema + migration 0001, `Database.tenant()` / `.system()` with Postgres RLS, append-only audit log, transactional outbox, ledger balance trigger
- [x] `aura-auth`: RBAC, magic links, Google OIDC (PKCE), opaque Redis sessions, API keys (peppered HMAC, cached lookup, instant revoke)
- [x] `aura-billing`: versioned plan catalog, entitlement service (Redis-atomic quotas), subscriptions + Razorpay checkout/webhooks, double-entry credits, usage reconciler
- [x] `apps/api`: `/v1/auth/*`, `/v1/me`, `/v1/organizations`, `/v1/accounts/{id}/{api-keys,subscription,entitlements,credits}`, `/v1/plans`, `/v1/webhooks/razorpay`
- [x] `aura-providers`: model catalog + Decimal pricing; Anthropic (official SDK, refusal fallback), OpenAI-compatible, stub adapters
- [x] `aura-policy` + `aura-routing`: stricter-wins policy merge; constraint-aware rules-v1 router with fallback chains
- [x] `aura-engine`: quota reserve/commit, fallback with Redis circuit breakers, streaming with pre-first-token failover, telemetry
- [x] `apps/runtime`: OpenAI-compatible `/v1/chat/completions` (JSON + SSE), `/v1/execute`, `/v1/models`
- [x] `apps/workers`: NATS JetStream → ClickHouse telemetry with dedupe, usage reconciliation; ClickHouse DDL + rollups; outbox relay; Postgres job queue
- [x] Cross-pod cache invalidation (outbox → Redis pub/sub → every pod's L1); Redis failures degrade, never 500
- [x] BYOK: envelope-encrypted tenant provider keys (AES-256-GCM, AAD-bound), entitlement-gated
- [x] Session memory (`session_id`) and run-scoped working memory
- [x] X-Ray: JSONL/CSV/gateway ingestion, template-based workload discovery, 7 waste detectors, confidence-weighted savings, HTML report, public lead funnel
- [x] Shadow runs + evaluation: encrypted replay datasets, budgeted replays, position-swapped LLM judge, Wilson/bootstrap CIs, deploy gate
- [x] Superadmin API: TOTP-backed admin sessions, metrics/MRR, accounts, suspend/restore (enforced on the runtime), plans, credits, overrides, leads, audit
- [x] **V2** policy engine (versions, evidence gate, canary + auto-rollback, experiments), `/v1/outcomes`, exact + semantic response cache, context compiler, long-term memory + knowledge-base RAG + RAG tuning, prompt registry + optimizer, recommendation engine (ADR-0009)
- [x] **V3** execution graphs: IR + validator, intent compiler, planner templates, event-driven executor (parallel levels, checkpoints/resume, verification + escalation, loop protection), safe tools (calculator, SSRF-guarded fetch, read-only SQL, remote sandbox, web search), planned `/v1/execute` + async `/v1/runs` (ADR-0010)
- [x] **V4** learned routing (LinUCB inside hard constraints, true propensities, exploration budget), off-policy evaluation API, consent-gated global priors, Autopilot (shadow evidence → canary), optimization health (ADR-0011)
- [x] **V5** AURA Personal: runtime session-cookie access, credit gate + meter (BYOK keeps working at zero balance), monthly grants with expiry of unused grants, Razorpay credit packs, referrals, personal memory, frontier-baseline savings + content-free share cards, consumer unit economics, `/app` chat (modes, "How AURA optimized this"), memory, usage, subscription, invites (ADR-0012)
- [x] `apps/web` (marketing, pricing, public X-Ray, auth, B2B dashboard) and `apps/admin` (superadmin console). These are **written but not yet built or type-checked here**, because node is blocked in the dev sandbox. The CI `web` job builds them.
