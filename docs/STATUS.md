# AURA – Status: what's done and what's left

*Last updated: 2026-10-01 · 291 automated tests passing · lint, type-check (230 files) and module-boundary checks green.*

This is the single checklist of the project's state. For what AURA is and how it works, read [AURA-GUIDE.md](AURA-GUIDE.md). For the agent/coding direction, read [AGENT-MODE-PLAN.md](AGENT-MODE-PLAN.md).

Legend: ✅ done and tested · 🟡 partly done · ⬜ not started

---

## At a glance

| Area | State |
|---|---|
| V1 Runtime + X-Ray | ✅ |
| V2 Optimizers + safe deployment | ✅ |
| V3 Execution graphs | ✅ |
| V4 Learned execution policies | ✅ |
| V5 AURA Personal (consumer app) | ✅ |
| **P0 ChatGPT-style chat (multiple sessions, Auto mode, dark mode)** | ✅ new |
| **Sub-query decomposition (Auto mode)** | ✅ new |
| **Real answers locally (Ollama) + model picker + one-click theme switch** | ✅ new |
| Groq provider | 🟡 built and tested; **waiting for your API key** |
| V6 Enterprise | 🟡 about 3 of 11 items done |
| Production deployment (cloud, Helm/Terraform) | ⬜ |

---

## Done

### V1 – Runtime and X-Ray ✅
- **Sign-in:** passwordless email links and Google. Personal accounts and organizations; projects and dev/staging/prod environments; six built-in roles.
- **API keys:** shown once, hashed, revocable instantly.
- **Plans and billing:**
  - plan catalog with versions;
  - entitlements (no hard-coded plan checks);
  - Razorpay subscriptions and webhooks;
  - double-entry credit ledger;
  - usage counters, reconciled from Redis to Postgres.
- **Model providers:** Anthropic (official SDK), any OpenAI-compatible API, and a stub for tests. Versioned pricing.
- **Gateway:**
  - endpoints: OpenAI-compatible `/v1/chat/completions` (streaming too), `/v1/execute`, `/v1/models`;
  - fallback chains, circuit breakers, budgets, rate limits.
- **BYOK (customers' own provider keys):** envelope-encrypted, entitlement-gated.
- **Telemetry:** versioned events → NATS → ClickHouse, deduplicated.
- **X-Ray:** log upload, workload discovery and seven waste detectors, producing a savings report and a public lead funnel.
- **Shadow runs:** replay real traffic on cheaper setups, LLM judge with swapped positions, and confidence intervals.
- **Superadmin console:** TOTP login, metrics and revenue, account actions, leads, audit log.

### V2 – Optimizers and safe deployment ✅
- **Context compiler:** BM25 + fusion, dedupe, compression, budgets.
- **Exact and semantic response cache:** semantic matching uses Redis vector sets, scoped per tenant.
- **Long-term memory and knowledge bases:** hybrid retrieval, RAG tuning, real "forget".
- **Prompt registry and optimizer.**
- **Policy versions:** evidence gate before deploy, canary releases with automatic rollback, experiments.
- **Business outcomes and recommendations:** `POST /v1/outcomes` for cost per outcome, plus a recommendation engine.

### V3 – Execution graphs ✅
- **Planning:** intent compiler and a template planner (chat, QA, research, summarize, code, math).
- **Executor:** parallel steps, checkpoints and resume, risk-based verification, escalation, loop protection.
- **Safe tools:** calculator (no `eval`), SSRF-guarded fetch, read-only SQL, remote sandbox, web search.
- **Async runs:** `/v1/runs`, with inputs and outputs encrypted.

### V4 – Learned execution policies ✅
- **Learned routing:** LinUCB bandit inside the rules (rules always win), with an exploration budget.
- **Off-policy evaluation:** IPS, SNIPS and doubly-robust estimators; `POST /v1/learning/evaluate`.
- **Global priors with consent:** aggregate numbers only.
- **Autopilot:** proven Shadow results become canaries automatically. Plus optimization health for superadmins.

### V5 – AURA Personal ✅
- **Web sessions on the runtime:** the app uses cookies; no API key needed.
- **Credits:**
  - signup grant and monthly grants;
  - only unused *grant* credits expire;
  - credit packs via Razorpay, confirmed by the signed webhook;
  - BYOK keeps working when credits run out.
- **Personal memory:** view, add, delete, forget all.
- **Referrals** with abuse limits.
- **"How AURA optimized this" panel:** model, why, cost vs frontier, savings.
- **Share cards:** contain numbers only.
- **Consumer unit economics:** margin view and a negative-margin alert.
- **Pages:** subscription, "Bring AURA to your team", usage, invite.

### P0 – ChatGPT-style chat ✅ *(new)*
- **Multiple saved conversations:**
  - sidebar grouped Today / Yesterday / Previous 7 days / Older;
  - "New chat", rename and delete;
  - chats survive restarts.
- **Streaming answers** with a **Stop** button; Enter to send, Shift+Enter for a new line.
- **Modes per chat:** **Auto** (default), Fast, Balanced, Best.
- **Every answer** keeps its "How AURA optimized this" panel when the chat is reopened. Also on every answer: Copy and Share buttons.
- **Safe Markdown rendering:** code blocks, lists, headings, bold, inline code. It never renders raw HTML.
- **Dark mode:** Light / Dark / System toggle, remembered per browser, applied before first paint (no white flash), and covering the whole web app.
- **Privacy:**
  - chat titles and messages are encrypted at rest, per row;
  - each chat is private to its user, even inside a shared team account;
  - API keys can't read chats.
- **Where:** `apps/runtime/src/aura_runtime/chat.py`, `packages/memory/src/aura_memory/conversations.py`, `apps/web/components/personal/chat-view.tsx`, `apps/web/app/app/layout.tsx`, and migration `0017`.

### Sub-query decomposition ✅ *(new, from the original plan's "task decomposer")*
- **Auto mode only:** a multi-part message becomes 2–5 sub-questions. Each one is **routed on its own difficulty** (easy parts go to cheap models), they run **in parallel**, then one step combines them into a single answer.
- **Splitting is free and deterministic first:** numbered or bulleted lists, or several questions in a row. A small-model split is attempted only for long compound requests ("compare X and Y on…"), and its output is strictly validated.
- **In the UI:** "Answering 3 sub-questions in parallel…" while it works. Afterwards the panel lists each sub-question with its model and cost, plus the combining model and the total.
- **Where:** `packages/graph/src/aura_graph/decompose.py`.

### Real models, model picker, theme switch ✅ *(new)*
- **Local models (free, no key):**
  - Ollama models on this computer give real answers today: Gemma 2B (small) and Llama 3.1 8B (medium);
  - enabled with `AURA_OLLAMA_BASE_URL`, and started from the `ollama` entry in `.claude/launch.json`;
  - there is no frontier-size local model, so "Best" uses the strongest available one and the panel says so.
- **Model picker in the chat header:** "AURA chooses (recommended)" or a specific model. Models without a key (Anthropic, Groq) are listed but disabled with "needs API key".
- **One-click light/dark switch:** in the chat header, the website navigation and the team dashboard. The sidebar keeps Light / Dark / System, and all switches stay in sync.
- **Groq:** provider and models (Llama 3.1 8B, Llama 3.3 70B, GPT-OSS 120B) are wired in and tested. It needs your key in `.env`, which you'll be asked for when it's time.
- **Fixes found along the way:**
  - streamed answers arrived all at once through the web proxy (gzip buffering);
  - model IDs containing `:` broke circuit-breaker keys;
  - tests read the developer's `.env` and could call real models.

### V6 – Enterprise (done parts) ✅
- **PII policy:** allow / redact / block emails, phones, cards (Luhn), Aadhaar (Verhoeff), PAN and secrets before any provider sees them. It also applies to graph steps.
- **Human-approval steps:** runs pause for a person; machines can't approve; the exact reviewed text is released.
- **Team members and custom roles (API):** add, change and remove members. Rules: no privilege escalation, only owners make owners, and an organization always keeps one owner.

### Developer experience ✅
- **Local stack:** private Postgres and Redis under `.local/`, plus API, runtime, web and admin servers.
- **Demo data:** `uv run aura-api seed-demo` creates 8 demo logins; `uv run aura-api login-link --email …` prints a sign-in link. Both work in dev/test only.
- **Docs:** [AURA-GUIDE.md](AURA-GUIDE.md), ADRs 0001–0012, README.

---

## Left to do

### V6 Enterprise – remaining
| Item | State | Notes |
|---|---|---|
| SSO (OIDC + SAML), domain verification | 🟡 | Tables and the OIDC/SAML/DNS adapters exist. The SSO service file (`aura_auth/application/sso.py`) is written and type-checks, but it is **not wired into the API and has no tests**. Login enforcement is not hooked in yet. |
| SCIM provisioning | ⬜ | Tables exist; endpoints not written |
| Customer-managed encryption keys (per-tenant KMS) | ⬜ | The encryption layer already supports multiple key providers |
| Audit log export and SIEM streaming | ⬜ | |
| Data retention policies (auto-purge) | ⬜ | |
| Region pinning / dedicated runtimes | ⬜ | Per-request region filtering already exists |
| Status page, incidents, SLA uptime | ⬜ | |
| Performance-linked pricing (verified-savings ledger) | ⬜ | |
| Helm chart and Terraform modules | ⬜ | |
| Enterprise screens: members, roles, SSO, approvals | ⬜ | APIs exist for members, roles and approvals |
| ADR-0013 for V6 | ⬜ | |

### Product gaps (consumer app)
- ⬜ Documents tab: upload files and chat over them. The knowledge-base API exists.
- ⬜ Research tab and saved workflows (the planned-graph API exists).
- ⬜ Showing sub-question progress live, step by step. Today the combined answer arrives at the end.
- ⬜ Regenerate / edit-and-resend, message feedback (👍/👎 feeding the learner), search across chats.
- ⬜ AI-generated chat titles (titles currently come from the first message, at no cost).
- ⬜ PWA / mobile polish, product analytics (PostHog).

### Testing and verification gaps
- 🟡 Real model providers: local Ollama models are verified end to end. Groq is waiting for your key. Anthropic is not available yet.
- ⬜ NATS and ClickHouse telemetry (no local servers).
- ⬜ Razorpay test-mode payments (needs test keys).
- ⬜ Browser test suite (Playwright).
- ⬜ Click-through of the team dashboard pages and the admin console as a superadmin.
- ⬜ Load tests (k6) and chaos tests in a deployed environment.

### Production readiness
- ⬜ Cloud deployment (containers, managed Postgres/Redis, secrets in a KMS), CI deploy pipeline, monitoring and alerts, backups.
- ⬜ Security review of the new chat and SSO surfaces before launch.
- ⬜ Pricing validation: plan and credit-pack prices are still hypotheses.

---

## Suggested next priorities
1. **Ship the chat to real users:** connect a real provider key, add regenerate/feedback, then live sub-question progress.
2. **Documents tab:** most-requested consumer feature. The backend RAG already exists.
3. **Finish SSO + SCIM, then members/roles screens:** these unblock enterprise pilots.
4. **Agent mode:** see [AGENT-MODE-PLAN.md](AGENT-MODE-PLAN.md).
5. **Production deployment.**
