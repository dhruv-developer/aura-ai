"""Multi-agent answering as a query tree: Drafter ↔ Reviewer per node, then a Finaliser.

    root (the user's message, depth 0)
     ├── 1   sub-query        Drafter drafts → Reviewer (fixed model) critiques → Drafter
     │   └── 1.1 follow-up    revises … (a conversation, at most ``max_turns`` drafts).
     └── 2   sub-query        The Reviewer may add follow-up sub-queries: child nodes,
                              down to ``max_depth``.
    Finaliser: one answer from the whole reviewed tree.

Every bound (depth, turns, follow-ups, nodes, model calls, wall time) is a hard cap.
Hitting one stops new work and goes straight to the Finaliser, so there is always an
answer. Prompts are templates in ``aura_shared/prompts/templates/agents/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aura_graph.decompose import MAX_SUBQUERIES
from aura_graph.llm import LlmCaller, LlmResult
from aura_policy import ExecutionPolicy
from aura_providers.domain.types import Message, ProviderError
from aura_shared.errors import DependencyUnavailable
from aura_shared.prompts import render, render_with_id

log = logging.getLogger("aura.graph.agents")

MODE_TIER = {"auto": "auto", "fast": "small", "balanced": "medium", "best": "frontier"}
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)

AgentEvent = dict[str, Any]
EventSink = Callable[[AgentEvent], None]


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_depth: int = 3  # sub-queries at depth 1..3 below the user's message
    max_turns: int = 3  # drafts per node: the first draft + 2 revisions
    max_follow_ups: int = 2  # per node
    max_nodes: int = 12  # per tree
    max_calls: int = 40  # model calls per tree (drafts + reviews + finaliser)
    max_wall_ms: int = 120_000
    drafter_max_tokens: int = 4_000
    reviewer_max_tokens: int = 1_500  # reasoning models spend tokens before the JSON
    finaliser_max_tokens: int = 8_000


@dataclass(slots=True)
class Turn:
    role: str  # drafter | reviewer
    model: str
    text: str
    cost: Decimal
    ms: int
    prompt_id: str
    verdict: str | None = None  # reviewer: approve | revise
    score: int | None = None
    issues: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    parsed: bool = True  # reviewer: was the JSON usable?


@dataclass(slots=True)
class TreeNode:
    id: str  # "1", "1.2", "1.2.1"
    question: str
    depth: int
    parent_id: str | None
    origin: str  # split | follow_up
    parent_question: str | None = None  # set for follow-ups (context for both agents)
    status: str = "pending"  # approved | unresolved | unreviewed | skipped_cap | failed
    answer: str = ""
    issues: list[str] = field(default_factory=list)  # reviewer's last open issues
    turns: list[Turn] = field(default_factory=list)
    children: list[TreeNode] = field(default_factory=list)

    def walk(self) -> list[TreeNode]:
        out = [self]
        for c in self.children:
            out.extend(c.walk())
        return out

    def outline(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "status": self.status,
            "issues": self.issues,
            "children": [c.outline() for c in self.children],
        }

    def to_trace(self) -> dict[str, Any]:
        """Full record (contains user content: stored encrypted)."""
        return {
            "id": self.id,
            "question": self.question,
            "depth": self.depth,
            "origin": self.origin,
            "status": self.status,
            "answer": self.answer,
            "issues": self.issues,
            "turns": [
                {
                    "role": t.role,
                    "model": t.model,
                    "text": t.text,
                    "cost_usd": str(t.cost),
                    "ms": t.ms,
                    "prompt": t.prompt_id,
                    "verdict": t.verdict,
                    "score": t.score,
                    "issues": t.issues,
                    "follow_ups": t.follow_ups,
                    "parsed": t.parsed,
                }
                for t in self.turns
            ],
            "children": [c.to_trace() for c in self.children],
        }


@dataclass(slots=True)
class AgentResult:
    answer: str
    root: TreeNode
    cost: Decimal
    calls: int
    ms: int
    finaliser_model: str | None
    warnings: list[str]

    def nodes(self) -> list[TreeNode]:
        return self.root.walk()[1:]

    def summary(self) -> dict[str, Any]:
        """Content-free numbers (safe to keep unencrypted)."""
        nodes = self.nodes()
        turns = [t for n in nodes for t in n.turns]
        return {
            "kind": "agents",
            "cost_usd": str(self.cost),
            "calls": self.calls,
            "ms": self.ms,
            "nodes": len(nodes),
            "max_depth": max((n.depth for n in nodes), default=0),
            "follow_ups": sum(1 for n in nodes if n.origin == "follow_up"),
            "statuses": {
                s: sum(1 for n in nodes if n.status == s) for s in {n.status for n in nodes}
            },
            "drafts": sum(1 for t in turns if t.role == "drafter"),
            "reviews": sum(1 for t in turns if t.role == "reviewer"),
            "revisions": sum(1 for t in turns if t.role == "drafter") - len(nodes),
            "models": {
                "drafter": sorted({t.model for t in turns if t.role == "drafter"}),
                "reviewer": sorted({t.model for t in turns if t.role == "reviewer"}),
                "finaliser": self.finaliser_model,
            },
            "warnings": self.warnings
            + [
                f"part {n.id} not reviewed: {n.issues[0]}"
                for n in nodes
                if n.status == "unreviewed"
            ],
        }


@dataclass(frozen=True, slots=True)
class Review:
    verdict: str
    score: int | None
    issues: list[str]
    follow_ups: list[str]


def parse_review(text: str) -> Review | None:
    """Strict-but-forgiving JSON: tolerates code fences and surrounding text, then
    validates the shape. ``revise`` with no concrete issue counts as ``approve``
    (the reviewer has nothing the drafter could act on)."""
    m = _JSON_OBJECT.search(text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in ("approve", "revise"):
        return None

    def strings(key: str, limit: int) -> list[str]:
        raw = data.get(key) or []
        if not isinstance(raw, list):
            return []
        return [" ".join(str(x).split())[:400] for x in raw if str(x).strip()][:limit]

    score = data.get("score")
    issues = strings("issues", 6)
    verdict = "approve" if data["verdict"] == "revise" and not issues else data["verdict"]
    return Review(
        verdict,
        max(1, min(5, int(score))) if isinstance(score, int | float) else None,
        issues,
        strings("follow_ups", 4),
    )


def _norm(q: str) -> str:
    return re.sub(r"[^\w]+", " ", q.lower()).strip()


class _Budget:
    def __init__(self, limits: AgentLimits) -> None:
        self.limits = limits
        self.calls = 0
        self.t0 = time.monotonic()
        self.cost = Decimal(0)

    @property
    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def exhausted(self, reserve: int = 0) -> str | None:
        """Reason to stop, if any. ``reserve`` keeps calls back for the Finaliser."""
        if self.calls + reserve >= self.limits.max_calls:
            return "model-call limit"
        if self.ms >= self.limits.max_wall_ms:
            return "time limit"
        return None


class AgentOrchestrator:
    def __init__(
        self,
        llm: LlmCaller,
        *,
        reviewer_model: str,
        limits: AgentLimits | None = None,
        retry_delay_s: float = 1.0,
    ) -> None:
        self._llm = llm
        self._reviewer = reviewer_model
        self._limits = limits or AgentLimits()
        self._retry_delay_s = retry_delay_s

    async def run(
        self,
        account_id: str,
        question: str,
        *,
        subqueries: list[str],
        policy: ExecutionPolicy | None = None,
        mode: str = "auto",
        drafter_model: str | None = None,
        region: str = "global",
        on_event: EventSink | None = None,
        history: str | None = None,
        memory: list[str] | None = None,
    ) -> AgentResult:
        lim = self._limits
        budget = _Budget(lim)
        warnings: list[str] = []
        emit = on_event or (lambda _e: None)
        # Mode decides the drafter's tier (the tier wins over the policy's quality).
        base = policy or ExecutionPolicy()
        call_policy = ExecutionPolicy(**{**_policy_fields(base), "quality": None})
        drafter_tier = MODE_TIER.get(mode, "auto")
        ctx = _RunCtx(
            account_id,
            question,
            call_policy,
            region,
            drafter_tier,
            drafter_model,
            history or None,
            tuple(memory or ()),
        )

        root = TreeNode("0", question, 0, None, "root", status="approved")
        firsts = (subqueries or [question])[: min(MAX_SUBQUERIES, lim.max_nodes)]
        for i, q in enumerate(firsts, 1):
            root.children.append(TreeNode(str(i), q, 1, "0", "split"))
        seen = {_norm(n.question) for n in root.children}
        emit(
            {
                "type": "split",
                "nodes": [{"id": n.id, "question": n.question} for n in root.children],
            }
        )

        level = list(root.children)
        while level:
            follow_ups: dict[str, list[str]] = {}
            async with asyncio.TaskGroup() as tg:
                tasks = {n.id: tg.create_task(self._converse(ctx, n, budget, emit)) for n in level}
            for node_id, task in tasks.items():
                follow_ups[node_id] = task.result()
            level = []
            by_id = {n.id: n for n in root.walk()}
            for node_id, questions in follow_ups.items():
                parent = by_id[node_id]
                if parent.depth >= lim.max_depth:
                    continue
                for q in questions[: lim.max_follow_ups]:
                    key = _norm(q)
                    if not key or key in seen:
                        continue
                    if len(root.walk()) - 1 >= lim.max_nodes:
                        warnings.append("node limit reached: some follow-ups were not explored")
                        break
                    child = TreeNode(
                        f"{parent.id}.{len(parent.children) + 1}",
                        q,
                        parent.depth + 1,
                        parent.id,
                        "follow_up",
                        parent_question=parent.question,
                    )
                    parent.children.append(child)
                    seen.add(key)
                    level.append(child)
                    emit(
                        {
                            "type": "follow_up",
                            "node": child.id,
                            "parent": parent.id,
                            "depth": child.depth,
                            "question": q,
                        }
                    )
            if level and (reason := budget.exhausted(reserve=1)):
                for n in level:
                    n.status = "skipped_cap"
                    emit({"type": "node_done", "node": n.id, "status": n.status})
                warnings.append(f"{reason} reached: some follow-ups were not explored")
                level = []

        nodes = root.walk()[1:]
        answered = [n for n in nodes if n.answer]
        if not answered:
            raise DependencyUnavailable("no sub-question could be answered")
        answer, finaliser = await self._finalise(ctx, root, budget, emit)
        return AgentResult(
            answer, root, budget.cost, budget.calls, budget.ms, finaliser, sorted(set(warnings))
        )

    # ---- one node: the drafter ↔ reviewer conversation ------------------------------
    async def _converse(
        self, ctx: _RunCtx, node: TreeNode, budget: _Budget, emit: EventSink
    ) -> list[str]:
        lim = self._limits
        system = render_with_id(
            "agents/drafter_system.j2",
            root_question=ctx.question,
            parent_question=node.parent_question,
            history=ctx.history,
            memory=list(ctx.memory),
        )
        task = render_with_id("agents/drafter_task.j2", question=node.question)
        messages = [Message("system", system.text), Message("user", task.text)]
        follow_ups: list[str] = []
        for attempt in range(1, lim.max_turns + 1):
            if reason := budget.exhausted(reserve=2):
                node.status = "unresolved" if node.answer else "skipped_cap"
                emit(
                    {"type": "node_done", "node": node.id, "status": node.status, "reason": reason}
                )
                return follow_ups
            try:
                draft = await self._call(
                    ctx,
                    budget,
                    tier=ctx.drafter_tier,
                    model=ctx.drafter_model,
                    messages=messages,
                    max_tokens=lim.drafter_max_tokens,
                )
            except ProviderError as exc:
                node.status = "unresolved" if node.answer else "failed"
                emit(
                    {"type": "node_done", "node": node.id, "status": node.status, "error": str(exc)}
                )
                return follow_ups
            node.answer = draft.text
            node.turns.append(
                Turn(
                    "drafter",
                    draft.model,
                    draft.text,
                    draft.cost,
                    draft.latency_ms,
                    system.prompt_id,
                )
            )
            emit(
                {
                    "type": "draft",
                    "node": node.id,
                    "attempt": attempt,
                    "model": draft.model,
                    "cost_usd": str(draft.cost),
                }
            )
            review = await self._review(ctx, node, attempt, budget)
            if isinstance(review, str):
                # Keep the draft, but say plainly that it was not reviewed.
                node.status, node.issues = "unreviewed", [review]
                emit({"type": "review_failed", "node": node.id, "reason": review})
                break
            emit(
                {
                    "type": "review",
                    "node": node.id,
                    "attempt": attempt,
                    "model": node.turns[-1].model,
                    "verdict": review.verdict,
                    "score": review.score,
                    "issues": review.issues,
                    "follow_ups": len(review.follow_ups),
                }
            )
            follow_ups = review.follow_ups
            if review.verdict == "approve":
                node.status, node.issues = "approved", []
                break
            node.issues = review.issues
            if attempt == lim.max_turns:
                node.status = "unresolved"
                break
            revise = render(
                "agents/drafter_revise.j2",
                round=attempt,
                max_rounds=lim.max_turns - 1,
                issues=review.issues,
            )
            messages += [Message("assistant", draft.text), Message("user", revise)]
            emit({"type": "revise", "node": node.id, "attempt": attempt + 1})
        emit({"type": "node_done", "node": node.id, "status": node.status})
        return follow_ups

    async def _review(
        self, ctx: _RunCtx, node: TreeNode, attempt: int, budget: _Budget
    ) -> Review | str:
        """A Review, or the reason no review could be made (never a silent pass)."""
        lim = self._limits
        system = render_with_id("agents/reviewer_system.j2", max_follow_ups=lim.max_follow_ups)
        user = render(
            "agents/reviewer_user.j2",
            root_question=ctx.question,
            parent_question=node.parent_question,
            history=ctx.history,
            question=node.question,
            draft=node.answer,
            attempt=attempt,
            max_attempts=lim.max_turns,
            allow_follow_ups=node.depth < lim.max_depth,
        )
        messages = [Message("system", system.text), Message("user", user)]
        for retry in range(2):  # one retry if the JSON is unusable
            if reason := budget.exhausted(reserve=1):
                return f"{reason} reached before review"
            try:
                r = await self._call_with_retry(
                    ctx,
                    budget,
                    tier="small",
                    model=self._reviewer,
                    messages=messages,
                    max_tokens=lim.reviewer_max_tokens,
                )
            except ProviderError as exc:
                log.warning("reviewer unavailable for node %s: %s", node.id, exc)
                return f"reviewer unavailable ({exc.kind.value})"
            review = parse_review(r.text)
            turn = Turn("reviewer", r.model, r.text, r.cost, r.latency_ms, system.prompt_id)
            node.turns.append(turn)
            if review is not None:
                turn.verdict, turn.score = review.verdict, review.score
                turn.issues, turn.follow_ups = review.issues, review.follow_ups
                return review
            turn.parsed = False
            if retry == 0:
                messages += [
                    Message("assistant", r.text),
                    Message("user", render("agents/reviewer_json_retry.j2")),
                ]
        # Never stall on a malformed review: keep the draft, note it.
        return Review("approve", None, [], [])

    async def _finalise(
        self, ctx: _RunCtx, root: TreeNode, budget: _Budget, emit: EventSink
    ) -> tuple[str, str | None]:
        only = root.children[0] if len(root.children) == 1 else None
        if only is not None and not only.children and only.status == "approved":
            # One reviewed answer and nothing to combine: it already is the final answer.
            emit({"type": "finalise", "model": None, "skipped": True})
            return only.answer, None
        messages = [
            Message("system", render("agents/finaliser_system.j2")),
            Message(
                "user",
                render(
                    "agents/finaliser_user.j2",
                    root_question=ctx.question,
                    history=ctx.history,
                    memory=list(ctx.memory),
                    nodes=[c.outline() for c in root.children],
                ),
            ),
        ]
        emit({"type": "finalise", "model": None, "skipped": False})
        try:
            r = await self._call(
                ctx,
                budget,
                tier="auto",
                model=None,
                messages=messages,
                max_tokens=self._limits.finaliser_max_tokens,
            )
        except ProviderError:
            # Fall back to the reviewed parts themselves rather than failing the turn.
            parts = [f"**{n.question}**\n\n{n.answer}" for n in root.walk()[1:] if n.answer]
            return "\n\n".join(parts), None
        emit({"type": "finalised", "model": r.model, "cost_usd": str(r.cost)})
        return r.text, r.model

    async def _call_with_retry(
        self,
        ctx: _RunCtx,
        budget: _Budget,
        **kw: Any,
    ) -> LlmResult:
        """One retry after a short pause: provider errors are often momentary (429/5xx)."""
        try:
            return await self._call(ctx, budget, **kw)
        except ProviderError as exc:
            if not exc.retryable or budget.exhausted(reserve=1):
                raise
            await asyncio.sleep(self._retry_delay_s)
            return await self._call(ctx, budget, **kw)

    async def _call(
        self,
        ctx: _RunCtx,
        budget: _Budget,
        *,
        tier: str,
        model: str | None,
        messages: list[Message],
        max_tokens: int,
    ) -> LlmResult:
        budget.calls += 1
        r = await self._llm.call(
            ctx.account_id,
            tier=tier,
            messages=messages,
            policy=ctx.policy,
            max_tokens=max_tokens,
            region=ctx.region,
            model=model,
        )
        budget.cost += r.cost
        return r


@dataclass(frozen=True, slots=True)
class _RunCtx:
    account_id: str
    question: str
    policy: ExecutionPolicy
    region: str
    drafter_tier: str
    drafter_model: str | None
    history: str | None = None  # earlier turns, so follow-ups like "shorter" make sense
    memory: tuple[str, ...] = ()  # the user's saved preferences


def _policy_fields(p: ExecutionPolicy) -> dict[str, Any]:
    return {f: getattr(p, f) for f in p.__dataclass_fields__}
