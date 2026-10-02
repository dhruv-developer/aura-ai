"""RAG optimization (spec §53): pick the cheapest retrieval configuration that still
finds the answer.

For each (top_k, token_budget) candidate, retrieve the context for every labelled
question, measure recall (did the expected snippet make it into the packed context?)
and context tokens. The recommendation is the configuration with the fewest tokens
whose recall meets the target, or the best recall if none does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura_memory.longterm import LongTermMemory
from aura_providers.domain.tokens import estimate_text_tokens

TOP_KS = (3, 5, 8, 12)
BUDGETS = (500, 1_000, 2_000, 4_000)


@dataclass(frozen=True, slots=True)
class LabelledQuestion:
    question: str
    expected: str  # a snippet the retrieved context must contain


@dataclass(frozen=True, slots=True)
class TuningResult:
    top_k: int
    token_budget: int
    recall: float
    avg_context_tokens: float


def _pack(contents: list[str], budget: int) -> str:
    out, used = [], 0
    for c in contents:
        t = estimate_text_tokens(c)
        if used + t > budget:
            break
        out.append(c)
        used += t
    return "\n".join(out)


async def tune(
    memory: LongTermMemory,
    account_id: str,
    *,
    scopes: list[tuple[str, str]],
    questions: list[LabelledQuestion],
    recall_target: float = 0.9,
) -> dict[str, Any]:
    if not questions:
        raise ValueError("at least one labelled question is required")
    # Retrieve once at the largest k; smaller k is a prefix of the same ranking.
    rankings = [
        [
            h.content
            for h in await memory.search(account_id, scopes=scopes, query=q.question, k=max(TOP_KS))
        ]
        for q in questions
    ]
    results: list[TuningResult] = []
    for k in TOP_KS:
        for budget in BUDGETS:
            hits = tokens = 0
            for q, ranked in zip(questions, rankings, strict=True):
                ctx = _pack(ranked[:k], budget)
                tokens += estimate_text_tokens(ctx)
                hits += q.expected.lower() in ctx.lower()
            results.append(TuningResult(k, budget, hits / len(questions), tokens / len(questions)))
    meeting = [r for r in results if r.recall >= recall_target]
    best = (
        min(meeting, key=lambda r: (r.avg_context_tokens, r.top_k))
        if meeting
        else max(results, key=lambda r: (r.recall, -r.avg_context_tokens))
    )
    return {
        "recommendation": {
            "top_k": best.top_k,
            "token_budget": best.token_budget,
            "recall": round(best.recall, 4),
            "avg_context_tokens": round(best.avg_context_tokens, 1),
            "meets_target": best.recall >= recall_target,
        },
        "grid": [
            {
                "top_k": r.top_k,
                "token_budget": r.token_budget,
                "recall": round(r.recall, 4),
                "avg_context_tokens": round(r.avg_context_tokens, 1),
            }
            for r in results
        ],
        "questions": len(questions),
    }
