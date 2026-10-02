# ADR-0008: X-Ray, Shadow runs and evaluation

**Status:** accepted · **Date:** 2026-10-01

## X-Ray (spec §25, plan 1.8)
- **Workloads = prompt templates.** Requests are grouped by a fingerprint of the normalized system prompt (numbers, ids, emails, URLs and dates replaced by placeholders). Templates whose token sets have Jaccard similarity ≥ 0.8 are then merged, so a variable name inside a template does not split one workload into many. An explicit workload tag in the logs always wins. This is deterministic and explainable. An embedding clusterer can plug in later behind the same interface.
- **Detectors** score each request in dollars: model downgrade (frontier model on low-complexity requests, using the router's own complexity features), exact and near-duplicate caching, provider prompt caching, context trimming, truncation, and billed failures.
  - **Near-duplicates** use *light* normalization (case, whitespace, punctuation). Different ids or numbers mean different answers, never a cache hit.
  - **Prompt caching** groups on the exact system prompt, because provider caches require a byte-identical prefix.
- **Honest headline.** Each request counts only its single best opportunity, weighted by that detector's confidence ("expected"). The unweighted figure is shown as "potential". Both are labelled as estimates to be verified by Shadow.
- **Privacy.** Only aggregates persist. The public funnel stores headline numbers on the lead, never prompts. Gateway X-Ray reads ClickHouse with server-side parameter binding.

## Shadow runs (spec §26, plan 1.9)
- Production is untouched. A seeded sample of real requests (optionally one workload) is replayed against candidate models on workers via a Postgres job queue (`SKIP LOCKED`, visibility timeout, exponential backoff, dead-lettering).
- **Baselines:** the stored production answer when the logs include it (free), otherwise a re-run of the baseline model.
- **Budget:** a hard cap enforced with `TaskGroup`, so exhausting it cancels in-flight calls immediately.
- **Data handling:** replay prompts are envelope-encrypted (AAD = account|shadow|run). They are erased on completion or on final failure. Results are aggregates only.

## Evaluation
- **Judge:** a pairwise LLM judge with position swapping. A verdict counts only when both orderings agree; otherwise it is a tie, which controls for position bias.
- **Rule checks** (empty output, JSON-validity parity) override the judge.
- **Fallback:** without a judge verdict, a token-F1 ≥ 0.6 proxy is used, labelled `method: similarity` and reported as judge coverage.
- **Statistics:** a Wilson interval for the non-inferiority rate, and a seeded paired bootstrap for the cost change.
- **Deploy gate (spec §153):** refuse unless there are at least 30 samples, the lower confidence bound is at or above the quality target, there are no rule failures, and savings are at least 5%.

## Least-privilege lessons encoded as tests
`aura_app` may INSERT into `audit_log`, `outbox` and `jobs` without SELECT. A schema test fails if any column there needs `RETURNING` to read back a server-generated value.
