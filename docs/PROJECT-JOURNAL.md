# AURA – Project Journal: what we built, how, and why

*Written 2026-10-01. Covers everything built so far, version by version, with the reasoning behind each decision.*

**Companion documents:**
- [AURA-GUIDE.md](AURA-GUIDE.md): what AURA is, in plain language.
- [STATUS.md](STATUS.md): the done/left checklist.
- [AGENT-MODE-PLAN.md](AGENT-MODE-PLAN.md): the plan for a coding agent.
- [`docs/adr/`](adr/): the formal decision records.

**Where things stand:** 293 automated tests pass; lint, type-check (230 files) and module-boundary checks are green; the web and admin apps type-check; and the chat gives real answers from Groq.

---

## Contents
1. [The idea in one paragraph](#1-the-idea-in-one-paragraph)
2. [Foundations: decisions that shape everything](#2-foundations-decisions-that-shape-everything)
3. [V1 – Gateway and X-Ray](#3-v1--gateway-and-x-ray)
4. [V2 – Optimizers and safe deployment](#4-v2--optimizers-and-safe-deployment)
5. [V3 – Execution graphs](#5-v3--execution-graphs)
6. [V4 – Learned execution policies](#6-v4--learned-execution-policies)
7. [V5 – AURA Personal (consumer app)](#7-v5--aura-personal-consumer-app)
8. [V6 – Enterprise (partial)](#8-v6--enterprise-partial)
9. [The ChatGPT-style chat (P0)](#9-the-chatgpt-style-chat-p0)
10. [Sub-query decomposition](#10-sub-query-decomposition)
11. [Real models: stub → Ollama → Groq](#11-real-models-stub--ollama--groq)
12. [How Auto mode picks a model](#12-how-auto-mode-picks-a-model)
13. [Running it locally and developer tooling](#13-running-it-locally-and-developer-tooling)
14. [Bugs we hit, and what each taught us](#14-bugs-we-hit-and-what-each-taught-us)
15. [Security and privacy decisions](#15-security-and-privacy-decisions)
16. [What's left](#16-whats-left)

---

## 1. The idea in one paragraph
Most people and companies send every AI request to one big, expensive model, with bloated context and no caching, and with no way to know or safely change what they're paying for. **AURA sits between the app and the AI providers and decides, per request, how it should be computed:**
- which model;
- how much context;
- whether a cached answer already exists;
- whether to split the task into parts;
- what rules apply (budget, data region, personal data).

Then it **proves** the savings with measurements and safe rollouts. The same engine powers two products:
- **AURA for Business:** a drop-in OpenAI-compatible gateway, plus X-Ray → Shadow → Pilot → Autopilot.
- **AURA Personal:** a consumer chat app.

---

## 2. Foundations: decisions that shape everything

| Decision | How | Why |
|---|---|---|
| **One engine, two products** | Shared Python packages; B2B and B2C are thin "shells" over them | Spec rule: no duplicated logic, so every improvement helps both products |
| **Monorepo with strict module boundaries** | `uv` workspace (Python 3.12) for services and packages, `pnpm` for the Next.js apps; `import-linter` enforces layers (apps → features → domain → shared) and "pure domain code does no I/O" | Many packages (21) stay understandable only if lower layers can't import higher ones; the checks run in CI |
| **Control plane vs data plane** | `apps/api` (accounts, billing, policies, admin) separate from `apps/runtime` (answers requests) | The API needs strong consistency; the runtime needs high volume and statelessness. They scale and fail separately |
| **Postgres with row-level security (RLS)** | Every tenant table has `account_id`; the app sets the tenant per transaction; the database enforces it with roles `aura_app` / `aura_system` | Two independent locks on customer data. An application bug still can't leak another tenant's rows |
| **Redis split by role** | `state` (sessions, quotas, never evicted), `cache` (evictable), `vector` (semantic search); one key builder `build_key` | A cache flood must never evict a quota counter or a login session |
| **Atomic Lua scripts** | Rate limits (GCRA) and quota reserve/commit/release run inside Redis | Without atomicity, parallel requests can overspend a limit |
| **Transactional outbox** | Config changes are written with the DB transaction, then relayed to every server's cache | Settings changes reach all servers within about a second, without losing messages |
| **Envelope encryption** | Each secret gets its own key (AES-256-GCM), wrapped by a master key and bound to its owner (AAD) | A copied ciphertext won't decrypt under another account; master keys can rotate |
| **Fail-open vs fail-closed** | If Redis is down: business tiers keep running; free consumer tiers pause | Our outage mustn't stop a customer's production, but mustn't become free unlimited usage either |
| **Typed errors → RFC 7807** | One `AuraError` hierarchy; every error is `application/problem+json` | Clients get consistent, machine-readable errors |
| **Quality gates on every change** | ruff, mypy `--strict`, import-linter, pytest with real Postgres + Redis | Fast feedback; tests catch the real database behaviour, not mocks |

---

## 3. V1 – Gateway and X-Ray
**Goal:** a gateway that works, plus a report that proves the waste.

- **Identity and accounts.**
  - *What:* passwordless email links and Google sign-in; personal accounts and organizations; dev/staging/prod environments; six roles.
  - *How:* opaque sessions in Redis (not JWTs) with a CSRF header rule; API keys `aura_sk_…` stored only as peppered HMAC hashes, cached for fast checks, revocable instantly.
  - *Why:* passwords are a liability. Opaque sessions can be revoked instantly; JWTs can't.
- **Billing foundation.**
  - *What:* plan catalog (Free, Plus, Pro, Startup, Growth, Enterprise), entitlements, Razorpay subscriptions, a double-entry credit ledger.
  - *How:* the catalog lives in `catalog.yaml` with versioned plans, so existing subscribers keep their version. Feature checks always go through `entitlements` (never `if plan == "pro"`), and the ledger is append-only with balanced entries.
  - *Why:* prices are hypotheses that will change. Money needs an audit trail.
- **Model providers.**
  - *What:* Anthropic (official SDK), any OpenAI-compatible API, and a deterministic stub for tests.
  - *How:* a provider-neutral interface and a versioned price catalog (`models.yaml`).
  - *Why:* AURA's value is choosing between providers, so none may be hard-wired.
- **Runtime gateway.**
  - *What:* an OpenAI-compatible `/v1/chat/completions` (normal and streaming), plus `/v1/execute` and `/v1/models`.
  - *How:* a pipeline of auth → quota reserve → policy → route → call with fallback and circuit breakers → settle → telemetry. Every answer carries an `aura` block (model, why, cost).
  - *Why:* "change one URL" adoption for businesses, and full transparency.
- **BYOK (customers' own provider keys):** envelope-encrypted, decrypted only inside the runtime.
- **Telemetry:** versioned events go to NATS, then to ClickHouse, deduplicated by event ID, so analytics never slow down requests.
- **X-Ray.**
  - *What:* upload logs and get spend broken down by workload, plus seven waste detectors: model downgrade, exact cache, semantic cache, prompt caching, context trimming, truncation waste, failed calls.
  - *Why:* a concrete, money-denominated report is the sales opener.
- **Shadow runs.**
  - *What:* replay real traffic on cheaper setups and judge quality.
  - *How:* rule checks, an LLM judge with swapped answer positions (removes position bias), and Wilson/bootstrap confidence intervals.
  - *Why:* "cheaper" is only credible with evidence of "no worse".
- **Superadmin console:** TOTP-protected; shows metrics and revenue, account actions and the audit log.

## 4. V2 – Optimizers and safe deployment
**Goal:** turn "this would save money" into "this is saving money, safely".

- **Context compiler** (BM25 + reciprocal-rank fusion, dedupe, compression, budgets): most token spend is bloated context.
- **Exact and semantic response cache** (Redis vector sets, per tenant, risk-aware thresholds): repeated questions shouldn't be paid for twice.
- **Long-term memory and knowledge bases:** hybrid retrieval (full-text + vectors), RAG tuning, real "forget".
- **Prompt registry and optimizer:** versioned prompts, with candidates evaluated before use.
- **Policies, evidence gate and canaries.**
  - *How:* immutable policy versions follow draft → canary → active → rolled back. A deploy needs enough evaluated samples and a safe lower confidence bound. Sticky traffic splits run at 1–100%, with automatic rollback.
  - *Why:* changing models in production is scary; this makes it safe and reversible.
- **Business outcomes and recommendations:** `POST /v1/outcomes` gives cost per real outcome, and recommendations turn findings into ready-to-apply changes.

## 5. V3 – Execution graphs
**Goal:** handle tasks that need more than one model call.

- **Intent compiler + template planner:** classifies a goal (chat, QA, research, summarize, code, math) and builds a step graph. Math uses a calculator: no model needed, so it costs $0.
- **Executor:**
  - runs independent steps in parallel;
  - checkpoints, so a crashed run resumes;
  - verifies answers, with depth based on risk;
  - escalates to a stronger model on failure;
  - enforces hard limits on steps, events, budget and time.
- **Safe tools:** a calculator that parses expressions (never `eval`), SSRF-guarded fetch (can't reach internal networks), read-only SQL, a remote sandbox, web search.
- **Why:** cramming research or analysis into one giant prompt is both worse and more expensive.

## 6. V4 – Learned execution policies
**Goal:** AURA gets better with use.

- **LinUCB contextual bandit:** only *reorders* models the rules already allow. **Rules always win.** Exploration is capped per tenant.
- **True propensities logged:** every decision records its real probability, which enables **off-policy evaluation** (IPS, SNIPS, doubly robust). It estimates how a new policy *would have* done before trying it live.
- **Consent-gated global priors:** only aggregate numbers, from accounts that opt in.
- **Autopilot:** a proven Shadow result becomes a canary automatically, then is promoted or rolled back.

## 7. V5 – AURA Personal (consumer app)
- **Web sessions on the runtime:** the app uses its sign-in cookie, so consumers don't need API keys.
- **Credits.**
  - 1 credit ≈ $0.0001.
  - Charged per call, only when AURA's own keys are used; BYOK and cache hits are free.
  - Monthly grants expire; purchased and referral credits don't.
  - Packs are confirmed only by the signed Razorpay webhook with an amount check, never by the browser.
  - *Why:* fair, predictable, and impossible to forge.
- **Personal memory, referrals with abuse limits, consumer unit economics** (margin alert when negative).
- **"How AURA optimized this" panel and share cards.** Every answer reports a **frontier baseline** (what the same tokens would cost on the priciest model) and the savings. Share cards contain numbers only, never prompts or answers.

## 8. V6 – Enterprise (partial)
| Done | How / why |
|---|---|
| **Personal-data policy (allow / redact / block)** | Detects emails, phones, cards (Luhn checksum), Aadhaar (Verhoeff checksum), PAN and secrets before any provider sees them. Checksums avoid mangling ordinary numbers. The strictest rule wins |
| **Human-approval steps** | A graph run pauses for a person. API keys (machines) can't approve. The **exact content reviewed** is what's released, even if steps re-run |
| **Team members and custom roles (API)** | No privilege escalation, only owners create owners, and an organization always keeps one owner. Every change is audited |

Remaining V6 work (SSO/SCIM, customer-managed keys, audit export, retention, region pinning, status/SLA, verified-savings pricing, Helm/Terraform, enterprise screens) is listed in [STATUS.md](STATUS.md). The SSO service file is written but deliberately not wired in, because you asked to pause V6.

---

## 9. The ChatGPT-style chat (P0)
**Why it was P0:** the consumer product needs a familiar, complete chat experience before anything else.

| What | How | Why |
|---|---|---|
| **Multiple saved conversations** | New `conversations` and `conversation_messages` tables (migration `0017`); the sidebar groups chats by Today / Yesterday / 7 days / Older; rename and delete | Chats must survive restarts. The old 24-hour session memory wasn't enough |
| **Encrypted at rest** | Every title and message is sealed with envelope encryption bound to account, conversation and row | A database dump alone reveals nothing people asked |
| **Private per user** | Other users (even in the same team) get "not found"; API keys can't read chats | Chats are personal |
| **Streaming answers** | Server-sent events: `user`, `plan`, `delta`, `done`, `error` | Words appear as they're generated |
| **Answers survive leaving the page** | Each answer runs as a server-side task; the page only *listens*. Reopening shows "Still answering…" until it lands; **Stop** cancels on the server and keeps the partial answer | A real bug: leaving a chat used to cancel and lose the answer (see §14) |
| **Modes + model picker** | Auto / Fast / Balanced / Best, or pick one specific model; the picker lists only models your keys can use | Auto for most people, control for power users |
| **Safe Markdown** | A small renderer for code blocks, lists, headings, **tables**, rules, bold, and inline code; it builds React elements and never raw HTML | Model output can't inject scripts |
| **Dark mode** | Light / Dark / System in the sidebar, plus a one-click ☀/☾ button in headers; applied before first paint; all toggles stay in sync | No white flash; works on every screen |

## 10. Sub-query decomposition
**What:** in Auto mode, a multi-part message becomes 2–5 sub-questions. They're answered **in parallel**, **each routed on its own difficulty**, and then a final step combines them.

**How:**
- **Splitting is free and deterministic:** numbered or bulleted lists, or several questions in a row.
- **A small-model split** is attempted only for long compound requests ("compare X and Y on…"), and its output is strictly validated.
- The split becomes a graph: `sq1 … sqN` (parallel) → `answer` (combine). It runs on the V3 executor, so it reuses budgets, retries, credits and limits.
- The UI shows "Answering 3 sub-questions in parallel…". The panel then lists each sub-question with its model and cost.

**Why:**
- Most multi-part questions mix easy and hard parts.
- Answering the easy parts with a cheap model and only the hard part with a strong one is the core AURA idea, applied *inside* a single question.
- Verified on Groq: "capital of Australia" went to GPT-OSS 20B, while the calculus and architecture parts went to GPT-OSS 120B, for $0.0015 in 5 seconds.

## 11. Real models: stub → Ollama → Groq
| Stage | What | Why |
|---|---|---|
| **Stub** | A fake model that echoes input | Deterministic tests with no keys or costs. Still used by every automated test |
| **Ollama (local)** | Gemma 2B and Llama 3.1 8B on your Mac via an OpenAI-compatible endpoint (`AURA_OLLAMA_BASE_URL`) | Real answers before any API key was available. Turned **off** now |
| **Groq (current)** | Your key in the git-ignored `.env`; models fetched live from Groq's `/v1/models` and priced from Groq's docs | Fast, cheap, and you had a key |

**The Groq lineup and why:**

| Model | Tier | Price per 1M tokens (in/out) | Notes |
|---|---|---|---|
| GPT-OSS 20B | small | $0.075 / $0.30 | Cheapest and fastest |
| GPT-OSS 120B | frontier | $0.15 / $0.60 | The strong model |
| Qwen 3.8 27B | frontier | $0.80 / $4.00 | Preview and 5× pricier, so only used if you pick it or as a fallback |

**Left out, and why:**
- Llama 3.1 and 3.3: now enterprise-only on Groq.
- Whisper and Orpheus: speech models.
- Prompt Guard and GPT-OSS Safeguard: safety classifiers.
- ALLaM 2 7B: Arabic-focused, 4k context, no published price.

## 12. How Auto mode picks a model
1. **Split** (if multi-part), then route each part separately.
2. **Score difficulty (0–1):** start at 0.15; +0.12 per reasoning word (*analyze, compare, trade-offs, derive, step by step, design, evaluate, why, plan, debug, optimize…*, up to 3); +0.15 for maths; +0.15 for code; small extras for very long input or output.
3. **Map to a size:** under 0.35 = small; 0.35–0.65 = medium; over 0.65 = frontier.
4. **Filter:** drop models with no key, blocked by policy, wrong data region, too little context, or failing (circuit breaker).
5. **Choose:** the cheapest model of that size. If the size is missing, move **up** a size rather than down (quality first); a backup list handles failures.
6. **Learned routing (V4)** can reorder within the allowed set on Plus/Pro single answers.

**Why rules first:** explainable (the panel shows `auto: complexity 0.51`), cheap, and safe. A learned model replaces the keyword scorer over time using logged decisions.

---

## 13. Running it locally and developer tooling
| Piece | How it runs | Why |
|---|---|---|
| Postgres + Redis | Private instances under `.local/` on ports 55432 / 56379 | Docker wasn't running, and your own databases must stay untouched |
| API, runtime, web, admin | `.claude/launch.json` entries started from the app's browser pane | One place to start, stop and read logs |
| Workers | `tools/run-workers.sh` (jobs, outbox relay, credits) | The pane allows 5 servers; workers aren't web servers anyway |
| Frontends | Node 22 via nvm + pnpm (you allowed node) | Next 15 / Tailwind 4 need Node ≥ 20 |
| Demo data | `uv run aura-api seed-demo` creates 8 accounts | Realistic testing without real sign-ups |
| Sign-in links | `tools/signin.sh <email> [--open]`, `--all`, `--admin`, `--seed` | No mail server locally; links are minted directly (dev/test only) |

**Demo accounts:**
- `free@`, `plus@` and `pro@demo.aura`: personal accounts on each plan.
- `owner@`, `admin@`, `dev@` and `viewer@acme.demo`: the members of the Acme team.
- `admin@aura.demo`: superadmin.

## 14. Bugs we hit, and what each taught us
| Symptom | Root cause | Fix | Lesson |
|---|---|---|---|
| Postgres socket errors in tests | Unix socket path too long on macOS | TCP-only test databases | Prefer explicit TCP in fixtures |
| Inserts failed under least-privilege roles | ORM asked the DB to return server defaults | Client-side defaults plus a guard test | Least privilege surfaces hidden assumptions |
| Redis hiccup produced HTTP 500s | Cache errors weren't treated as degradable | The cache degrades; quotas fail open/closed by tier | Decide failure modes explicitly |
| Budget overspend in parallel graphs | `gather` didn't stop siblings | `TaskGroup` | Structured concurrency for budgets |
| Webhook retry could credit twice | A new event ID for the same order | Duplicate orders are flagged and not counted | Idempotency keys must match business identity |
| Tailwind build error on first frontend build | Tailwind 4 can't `@apply` a plain class | Declare `btn` as a utility | Build early; the frontends had never been compiled |
| Tiny costs showed "$0.00" | Formatter rounded to cents | Two significant digits below a cent | AI costs are fractions of a cent |
| "Thinking…" with no streaming | Next.js gzip buffered the proxied event stream | `compress: false` (compression belongs at the CDN) | Test streaming through the real proxy |
| Local model names broke Redis keys | `gemma:2b` contains the key separator `:` | `key_part()` escaping | Never put raw identifiers in keys |
| Test suite hung | Tests read the developer's `.env` and called real models | Tests construct settings with `_env_file=None` | Tests must be hermetic |
| "Why did it not answer?" | Leaving the chat cancelled the in-flight answer | Answers run as server tasks; reopen and poll; real Stop | The server must own long work, not the browser tab |
| Groq models "disappeared" | Groq moved the Llama models to enterprise | Catalog built from the live `/v1/models` list | Verify provider catalogs against the live API |
| Raw `| … |` in answers | Renderer lacked tables | Table and rule support | Render what models actually produce |

## 15. Security and privacy decisions
- **Secrets:**
  - API keys are hashed with a secret pepper;
  - provider keys are envelope-encrypted;
  - the Groq key lives only in the **git-ignored `.env`** and was never printed. **Rotate it**, since it was pasted in chat.
- **Isolation:** application checks plus database RLS. Cross-tenant requests return "not found", and chats are private per user.
- **Untrusted output:** model answers render through a renderer that never outputs raw HTML; share cards contain numbers only.
- **Payments:** credits are added only after a signed webhook with a matching amount.
- **Personal data:** an optional redact/block policy runs before any provider call.
- **Dev-only tools** (`seed-demo`, `login-link`, the log mailer) refuse to run outside dev/test.
- **Audit log:** append-only for admin, billing, membership and approval actions.

## 16. What's left
See [STATUS.md](STATUS.md) for the full list. The headlines:
1. **Chat polish:**
   - regenerate / edit-and-resend;
   - 👍/👎 feedback feeding the learner;
   - live sub-question progress;
   - a browser click-through of Stop and resume.
2. **Documents tab:** chat over uploaded files (the knowledge-base API already exists).
3. **V6 enterprise:** finish SSO + SCIM, then the remaining items and screens.
4. **Agent mode:** the plan in [AGENT-MODE-PLAN.md](AGENT-MODE-PLAN.md). It starts with tool-call support in the gateway.
5. **Production:** cloud deployment, monitoring, backups, a security review, and validated pricing.

**To start everything again:** ask me, or start Postgres and Redis (commands in [STATUS.md](STATUS.md) / [AURA-GUIDE.md](AURA-GUIDE.md)), then the launch-config servers, then `tools/run-workers.sh`.
