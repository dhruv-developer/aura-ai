# ADR-0012: V5 AURA Personal (B2C)

**Status:** accepted · **Date:** 2026-10-01

## Decisions
- **Same engine, different shell.** The personal app calls the same runtime as the API. The web app proxies `/rt/v1/*` to the runtime, so the HttpOnly session cookie is first-party. The runtime accepts that cookie as a second principal next to API keys:
  - CSRF uses the same custom-header rule as the control plane.
  - Access to an account comes from membership (`x-aura-account`, default: personal account) and is cached for 10 s. The session itself is checked in Redis on every request, so logout takes effect immediately.
  - No `api_access` entitlement is needed. The product UI is not the API.
- **Credits are a gate plus a meter, not a hot-path ledger write.**
  - An account is *credit-metered* when its plan has `monthly_credits` (the consumer plans).
  - **Gate:** while the cached balance (5 s L1, 30 s L2) is ≤ 0, platform-key providers are removed from routing. BYOK keeps working. With no own key the request fails with 402. Overdraft is bounded by the calls admitted within one cache TTL.
  - **Meter:** platform-key cost is charged at completion. It is idempotent per execution (`exec:{id}` reference) and allows overdraft, because the call already happened. Graph runs charge per LLM call.
  - Rate: 1 credit = $0.0001, rounded up, minimum one credit per paid call. BYOK calls and cache hits are free.
- **Grants:** the first month's credits are granted in the signup transaction. After that, `aura-workers credits` grants hourly; grants are idempotent per period.
  - Unused **granted** credits expire at the next grant. Consumption draws on granted credits first, so purchased and referral credits never expire.
- **Credit packs** (`credit_packs` in the catalog) are Razorpay orders.
  - Credits are posted only by the signed `order.paid` webhook, and only if the amount and currency match the pack. Notes alone are never trusted.
  - The posting happens inside the webhook transaction (`CreditService.post_in`). A retried delivery under a new event id is recorded as `duplicate_order`, so it is neither credited nor counted as revenue twice.
- **Referrals (§82):** each user gets one lazily assigned code.
  - Both sides get 2,000 credits when the code is redeemed within 14 days of the referee's signup.
  - Guards: no self-referral, one redemption per user, and at most 20 referrer rewards per month.
- **Personal memory (§17):** `user:{user_id}` scope, gated by `personal_memory`.
  - It is injected automatically for signed-in users.
  - A user can never address another member's user scope.
  - Users can list, add and delete items, and "forget everything" is a hard delete.
- **"How AURA optimized this" and share cards (§15):**
  - Every execution reports a **frontier baseline**: the same token usage priced on the most expensive enabled frontier model, together with `saved_usd`.
  - The runtime records a content-free summary in Redis for 7 days. It holds model, mode, tokens, cost, baseline, latency, and optimization *kinds* from an allow-list.
  - Sharing snapshots that summary into `shared_cards` (RLS-scoped). The public view drops internal ids. Prompts, answers, memory and credentials can never reach a card.
- **Unit economics (§76, §130):** consumer margin = paid invoices + credit packs − platform inference (credits consumed ÷ rate × FX) − a flat infra estimate per active account.
  - Exposed at `/v1/admin/economics` and the admin "Unit economics" page.
  - The credits worker logs an error when the monthly margin goes negative.
- **Crossover (§162):** "Bring AURA to your team" creates an organization from the personal app. The user keeps both accounts.

## Deferred
- Product analytics (PostHog), activation and churn dashboards.
- PWA packaging; the Documents, Research and Workflows tabs in `/app`. The APIs exist (knowledge bases, `/v1/execute` planned mode, `/v1/runs`), but the screens do not yet.
- Playwright E2E for the consumer flows. The frontends cannot be built in this sandbox, so the backend is covered by HTTP-level tests instead.
