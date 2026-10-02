"""Sub-query decomposition (the plan's "task decomposer": intent → sub-tasks).

A multi-part question is split into independent sub-questions. Each one is routed on
its own, so the easy parts go to cheap models and only the hard part (if any) needs a
strong one. The answers run in parallel and a final step combines them.

Splitting is deterministic first (numbered or bulleted lists, several questions in a
row) and costs nothing. A small-model split is attempted only for long, compound
requests the rules cannot separate, and its output is strictly validated.
"""

from __future__ import annotations

import json
import re

from aura_graph.ir import Graph, Node, NodeKind
from aura_graph.llm import LlmCaller
from aura_policy import ExecutionPolicy
from aura_providers.domain.types import Message, ProviderError
from aura_shared.prompts import render

MAX_SUBQUERIES = 5
MIN_WORDS = 3
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.+?)\s*$")
_COMPOUND = re.compile(r"\b(compare|versus|vs\.?|pros and cons|and also|as well as)\b", re.I)


def _clean(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(item.split()).strip(" -")
        key = text.lower()
        if len(text.split()) >= MIN_WORDS and key not in seen:
            seen.add(key)
            out.append(text)
    return out[:MAX_SUBQUERIES]


def split_subqueries(text: str, *, as_tasks: bool = False) -> list[str]:
    """Return 2-5 sub-queries (questions or tasks), or [] when single request."""
    items = [m.group(1) for line in text.splitlines() if (m := _LIST_ITEM.match(line))]
    if len(items) >= 2:
        cleaned = _clean(items)
        if len(cleaned) >= 2:
            if as_tasks:
                return [q.rstrip("?") for q in cleaned]
            return cleaned
        return []
    parts = [q.strip() for q in re.split(r"\?+", text) if q.strip()]
    if text.count("?") >= 2:
        if not text.rstrip().endswith("?"):
            parts = parts[:-1]  # trailing text after the last "?" is not a sub-item
        if as_tasks:
            cleaned = _clean([p.rstrip("?") for p in parts])
        else:
            cleaned = _clean([p + "?" for p in parts])
        return cleaned if len(cleaned) >= 2 else []
    return []


def worth_llm_split(text: str) -> bool:
    """Long compound requests the rules cannot split may still be several questions or tasks."""
    return len(text.split()) >= 25 and bool(_COMPOUND.search(text))


async def llm_split(
    llm: LlmCaller, account_id: str, text: str, *, policy: ExecutionPolicy, as_tasks: bool = False
) -> list[str]:
    template = "graph/split_tasks.j2" if as_tasks else "graph/split.j2"
    prompt = render(template, max_subqueries=MAX_SUBQUERIES, request=text)
    try:
        r = await llm.call(
            account_id,
            tier="small",
            messages=[Message("user", prompt)],
            policy=policy,
            max_tokens=400,
        )
        data = json.loads(r.text.strip().removeprefix("```json").strip("`").strip())
    except (ProviderError, ValueError):
        return []
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        return []
    found = _clean(data)
    return found if len(found) >= 2 else []


def plan_subqueries(goal: str, subqueries: list[str], *, as_tasks: bool = False) -> Graph:
    """sq1..sqN in parallel (each routed by its own difficulty), then one synthesis."""
    if not 2 <= len(subqueries) <= MAX_SUBQUERIES:
        raise ValueError("need 2-5 sub-queries")
    answer_tmpl = "graph/subquery_task_answer.j2" if as_tasks else "graph/subquery_answer.j2"
    combine_tmpl = "graph/subquery_task_combine.j2" if as_tasks else "graph/subquery_combine.j2"
    nodes = [
        Node(
            f"sq{i}",
            NodeKind.LLM,
            {
                "tier": "auto",
                "prompt": q,
                "instructions": render(answer_tmpl),
            },
        )
        for i, q in enumerate(subqueries, 1)
    ]
    listing = "\n".join(f"sq{i}: {q}" for i, q in enumerate(subqueries, 1))
    nodes.append(
        Node(
            "answer",
            NodeKind.SYNTHESIZE,
            {
                "tier": "auto",
                "instructions": render(combine_tmpl, listing=listing),
            },
            tuple(n.id for n in nodes),
        )
    )
    return Graph(tuple(nodes), "answer", "subqueries")
