# ADR-0011: V4 learned execution policies

**Status:** accepted · **Date:** 2026-10-01

## Decisions
- **Rules always win.** The LinUCB bandit only *reorders* the chain the rules router already filtered by policy, residency, budget, context fit and circuit health. Explicit model requests and plans without `advanced_routing` are never touched.
- **Exploration has a true propensity.** Selection is ε-greedy over the UCB argmax, so every logged decision carries its real selection probability, which is the prerequisite for unbiased off-policy evaluation. ε is bounded by a per-tenant daily exploration budget.
- **State:** one Redis value per tenant × workload, updated with WATCH/MULTI optimistic concurrency, plus a capped decision log (10k).
- **Reward (spec §154):** success − cost_weight·(cost/ref) − latency_weight·(latency/ref).
  - "Success" at completion means the chosen arm itself answered (no fallback) and the answer was not truncated or refused.
  - Business outcomes add a second observation.
- **Off-policy evaluation:** IPS, SNIPS and doubly robust estimators with bootstrap CIs, exposed at `POST /v1/learning/evaluate` for `always:<model>` or the current learned policy. It is validated by simulation against a known ground truth.
- **Global learning boundary (spec §62, §80):** priors are per-arm mean rewards aggregated from **consenting** tenants' decision logs only. They are numbers, never content. New tenants are warm-started from them through pseudo-observations on the bias feature.
- **Autopilot (spec §168):** a completed shadow run whose gate passed becomes a workload policy pinned to the proven model and a canary deployment using that run as evidence (never forced). The canary monitor promotes it or rolls it back. Each run is consumed once, and accounts opt in (`autopilot` setting, `policy_deployment` entitlement).
- **Superadmin optimization health (§150):** deployment, version and shadow outcomes, autopilot activity, rollback rate and recent rollback reasons.

## Deferred
- Graph-template search (mutate template graphs and evaluate them in shadow).
- A learned quality predictor per (model, context config). The bandit's per-arm linear model covers the routing case for now.
