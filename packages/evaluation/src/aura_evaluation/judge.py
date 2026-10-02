"""Pairwise LLM judge with position swapping.

Each comparison asks twice with the order swapped. A verdict counts only if both
orderings agree. Disagreement means the judge is position-biased or the answers are
equivalent, and either way it is scored as a tie. This is the standard control for LLM
judge position bias.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from aura_shared.prompts import render

TextCompleter = Callable[[str], Awaitable[str]]


_VERDICT = re.compile(r"\b(A|B|TIE)\b")


class JudgeVerdict(StrEnum):
    CANDIDATE_BETTER = "candidate_better"
    TIE = "tie"
    BASELINE_BETTER = "baseline_better"
    UNKNOWN = "unknown"  # judge output unparseable

    @property
    def candidate_not_worse(self) -> bool | None:
        if self is JudgeVerdict.UNKNOWN:
            return None
        return self is not JudgeVerdict.BASELINE_BETTER


class PairwiseJudge(Protocol):
    async def __call__(self, request: str, baseline: str, candidate: str) -> JudgeVerdict: ...


def _parse(text: str) -> str | None:
    m = _VERDICT.search(text.strip().upper()[:50])
    return m.group(1) if m else None


async def compare(
    complete: TextCompleter, request: str, baseline: str, candidate: str
) -> JudgeVerdict:
    first = _parse(
        await complete(render("evaluation/judge.j2", request=request, a=baseline, b=candidate))
    )
    second = _parse(
        await complete(render("evaluation/judge.j2", request=request, a=candidate, b=baseline))
    )
    if first is None or second is None:
        return JudgeVerdict.UNKNOWN
    # Map both to "who won" in candidate terms.
    w1 = {"A": "baseline", "B": "candidate", "TIE": "tie"}[first]
    w2 = {"A": "candidate", "B": "baseline", "TIE": "tie"}[second]
    if w1 != w2:
        return JudgeVerdict.TIE  # inconsistent under swap → treat as equivalent
    return {
        "candidate": JudgeVerdict.CANDIDATE_BETTER,
        "baseline": JudgeVerdict.BASELINE_BETTER,
        "tie": JudgeVerdict.TIE,
    }[w1]
