# ADR-0010: V3 execution graphs

**Status:** accepted · **Date:** 2026-10-01

## Decisions
- **IR (spec §48):** typed nodes (`llm`, `tool`, `retrieval`, `verify`, `synthesize`, `human_approval`), where edges are node inputs. The validator rejects cycles, dangling and self references, oversize graphs, excessive depth and excessive retry budgets *before* execution. It returns topological levels, and each level runs in parallel.
- **Intent → plan:** keyword rules compile goals into a task type (chat, qa, research, summarize, code, math), a risk level and needs. Templates map intents to graphs. Verification depth follows risk (§57): none for low, rule-based for medium, model-judged for high. Logged intents are training data for a later LLM planner.
- **Executor (§47):**
  - Emits the spec's event vocabulary.
  - Checkpoints node outputs to working memory, so re-running a `run_id` resumes for free; verify verdicts are never checkpointed.
  - Retries tools and falls back across models.
  - Loop protection (§124): maximum nodes, depth, retries, events, wall-clock time, plus a run budget. A stopped run returns the best partial answer with a status.
- **Escalation (§55–56):**
  - A failed verification is treated as a retrieval or model problem. The executor widens retrieval (k×2) and re-runs the target one tier up, at most twice, then returns the best answer with a caveat instead of failing.
  - A position-swapped judge isn't needed here, because verification is a single yes/no check.
- **Tools (§52):**
  - The calculator is a whitelisted-AST evaluator and the "no LLM" path; a math goal costs $0.
  - HTTP fetch is https-only, with resolved-IP checks against private, loopback, link-local and metadata ranges. An egress proxy is recommended against DNS rebinding.
  - SQL is single SELECT/WITH only, against *tenant-configured* connections, in a read-only transaction with a statement timeout and row cap. A data-modifying CTE is rejected by the read-only transaction.
  - Python runs only in a **remote sandbox**; nothing executes in-process.
- **Product surface:** `/v1/execute` with `mode: "planned"` runs synchronously and is metered (executions, plus `research_jobs` where the plan meters them). `/v1/runs` runs asynchronously on the job queue, requires `long_running_workflows`, and keeps goal and output envelope-encrypted at rest. The dashboard shows a per-run graph trace.

## Deferred
- An LLM planner for open-ended graphs, and automatic graph-template search. These are V4 learning work, informed by logged intents and traces.
- Human approval resumes only via a new run. An approval API that continues a paused run is future work.
