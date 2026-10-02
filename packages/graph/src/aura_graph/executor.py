"""Event-driven graph executor (spec §47, §55-57, §124)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aura_evaluation import token_f1
from aura_graph.ir import Graph, Limits, Node, NodeKind, validate
from aura_graph.llm import ESCALATE, LlmCaller
from aura_graph.tools import ToolRegistry, extract_code
from aura_memory import LongTermMemory, WorkingMemory
from aura_policy import ExecutionPolicy
from aura_providers.domain.types import Message, ProviderError
from aura_shared.prompts import render

log = logging.getLogger("aura.graph")

ROLES = frozenset({"assistant", "analyst", "coder", "writer"})  # graph/role_<name>.j2


class _Stop(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass
class RunContext:
    account_id: str
    run_id: str
    goal: str
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    budget_usd: Decimal | None = None
    region: str = "global"
    # Reviewer decisions per approval node: {"decision": approved|rejected, "content": ...}.
    approvals: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class NodeOutcome:
    node_id: str
    kind: str
    status: str  # completed | failed | skipped | resumed
    output: str = ""
    cost: Decimal = Decimal(0)
    latency_ms: int = 0
    model: str | None = None
    attempts: int = 0
    error: str | None = None


@dataclass
class GraphResult:
    status: str  # completed | partial | budget_exceeded | deadline_exceeded | awaiting_approval
    output: str
    cost: Decimal
    nodes: dict[str, NodeOutcome]
    events: list[dict[str, Any]]
    escalations: int
    warnings: list[str]
    pending: dict[str, str] = field(default_factory=dict)  # approval node → content to review

    def trace(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "cost_usd": str(self.cost),
            "escalations": self.escalations,
            "warnings": self.warnings,
            "nodes": [
                {
                    "id": n.node_id,
                    "kind": n.kind,
                    "status": n.status,
                    "model": n.model,
                    "cost_usd": str(n.cost),
                    "latency_ms": n.latency_ms,
                    "attempts": n.attempts,
                    "error": n.error,
                }
                for n in self.nodes.values()
            ],
            "events": len(self.events),
        }


class GraphExecutor:
    def __init__(
        self,
        *,
        llm: LlmCaller,
        tools: ToolRegistry,
        memory: LongTermMemory | None = None,
        checkpoints: WorkingMemory | None = None,
        limits: Limits | None = None,
        max_escalations: int = 2,
    ) -> None:
        self._llm, self._tools, self._memory = llm, tools, memory
        self._checkpoints, self._limits = checkpoints, limits or Limits()
        self._max_escalations = max_escalations

    @property
    def llm(self) -> LlmCaller:
        return self._llm

    # ---- events / limits -------------------------------------------------------------
    def _event(self, st: _State, name: str, **data: Any) -> None:
        st.events.append({"event": name, "t_ms": int((time.monotonic() - st.t0) * 1000), **data})
        if len(st.events) > self._limits.max_events:
            raise _Stop("partial", "event limit reached (loop protection)")

    def _check(self, st: _State) -> None:
        if (time.monotonic() - st.t0) * 1000 > self._limits.max_wall_ms:
            raise _Stop("deadline_exceeded", "wall-clock limit reached")
        if st.ctx.budget_usd is not None and st.cost >= st.ctx.budget_usd:
            raise _Stop("budget_exceeded", "run budget exhausted")

    # ---- run ---------------------------------------------------------------------------
    async def run(self, graph: Graph, ctx: RunContext) -> GraphResult:
        levels = validate(graph, self._limits)
        st = _State(ctx=ctx, graph=graph)
        self._event(st, "REQUEST_RECEIVED", template=graph.template)
        for n in graph.nodes:
            self._event(st, "TASK_CREATED", node=n.id, kind=n.kind.value)
        status = "completed"
        try:
            for level in levels:
                self._check(st)
                async with asyncio.TaskGroup() as tg:
                    for node_id in level:
                        tg.create_task(self._run_node(st, graph.node(node_id)))
                if st.awaiting_approval:
                    status = "awaiting_approval"
                    break
        except* _Stop as eg:
            stop = eg.exceptions[0]
            assert isinstance(stop, _Stop)
            status = stop.status
            st.warnings.append(stop.reason)
        out = st.outcomes.get(graph.output)
        output = (
            out.output if out and out.status in ("completed", "resumed") else self._best_partial(st)
        )
        if status == "completed" and (out is None or out.status == "failed"):
            status = "partial"
        self._event(st, "EXECUTION_COMPLETED", status=status) if len(
            st.events
        ) < self._limits.max_events else None
        return GraphResult(
            status,
            output,
            st.cost,
            st.outcomes,
            st.events,
            st.escalations,
            st.warnings,
            dict(st.pending),
        )

    @staticmethod
    def _best_partial(st: _State) -> str:
        done = [
            o for o in st.outcomes.values() if o.status in ("completed", "resumed") and o.output
        ]
        return done[-1].output if done else ""

    async def _run_node(
        self,
        st: _State,
        node: Node,
        *,
        tier_override: str | None = None,
        k_override: int | None = None,
    ) -> NodeOutcome:
        ckpt_field = f"node:{node.id}"
        if tier_override is None and k_override is None and self._checkpoints is not None:
            saved = await self._checkpoints.get(st.ctx.account_id, st.ctx.run_id, ckpt_field)
            if saved is not None:
                outcome = NodeOutcome(
                    node.id,
                    node.kind.value,
                    "resumed",
                    saved["output"],
                    Decimal(saved["cost"]),
                    0,
                    saved.get("model"),
                )
                st.outcomes[node.id] = outcome
                self._event(st, "TASK_COMPLETED", node=node.id, resumed=True)
                return outcome
        self._event(st, "TASK_READY", node=node.id)
        inputs = {i: st.outcomes[i].output for i in node.inputs if i in st.outcomes}
        attempts, last_error = 0, None
        for attempts in range(1, node.max_retries + 2):
            self._check(st)
            self._event(st, "TASK_STARTED", node=node.id, attempt=attempts)
            t0 = time.monotonic()
            try:
                outcome = await self._dispatch(st, node, inputs, tier_override, k_override)
            except ProviderError as exc:
                last_error = f"model failure: {exc.kind.value}"
                self._event(st, "TASK_FAILED", node=node.id, error=last_error)
                if not exc.retryable:
                    break
                continue
            outcome.latency_ms = int((time.monotonic() - t0) * 1000)
            outcome.attempts = attempts
            if outcome.status == "failed":
                last_error = outcome.error
                self._event(st, "TASK_FAILED", node=node.id, error=outcome.error)
                continue
            st.cost += outcome.cost
            st.outcomes[node.id] = outcome
            self._event(st, "TASK_COMPLETED", node=node.id, cost=str(outcome.cost))
            if node.kind is NodeKind.VERIFY:
                # Verdicts are never checkpointed: a resumed run must re-check, and the
                # node's downstream output is the verified answer, not the verdict.
                if outcome.output == "UNSUPPORTED":
                    return await self._escalate(st, node, outcome)
                return self._pass_through(st, node, outcome)
            if self._checkpoints is not None and node.kind is not NodeKind.HUMAN_APPROVAL:
                await self._checkpoints.put(
                    st.ctx.account_id,
                    st.ctx.run_id,
                    ckpt_field,
                    {"output": outcome.output, "cost": str(outcome.cost), "model": outcome.model},
                )
            return outcome
        failed = NodeOutcome(
            node.id, node.kind.value, "failed", attempts=attempts, error=last_error
        )
        st.outcomes[node.id] = failed
        return failed

    async def _dispatch(
        self,
        st: _State,
        node: Node,
        inputs: dict[str, str],
        tier_override: str | None,
        k_override: int | None,
    ) -> NodeOutcome:
        p, kind = node.params, node.kind
        if kind in (NodeKind.LLM, NodeKind.SYNTHESIZE):
            tier = tier_override or str(p.get("tier", "auto"))
            role_name = str(p.get("role", "writer" if kind is NodeKind.SYNTHESIZE else "assistant"))
            role = render(f"graph/role_{role_name if role_name in ROLES else 'assistant'}.j2")
            system = role + (f"\n{p['instructions']}" if p.get("instructions") else "")
            # A node may carry its own prompt (e.g. one sub-question of the goal).
            body = str(p.get("prompt") or st.ctx.goal) + "".join(
                f"\n\n<{name}>\n{text}\n</{name}>" for name, text in inputs.items() if text
            )
            self._event(st, "MODEL_SELECTED", node=node.id, tier=tier)
            r = await self._llm.call(
                st.ctx.account_id,
                tier=tier,
                messages=[Message("system", system), Message("user", body)],
                policy=st.ctx.policy,
                region=st.ctx.region,
            )
            self._event(st, "MODEL_EXECUTED", node=node.id, model=r.model, cost=str(r.cost))
            return NodeOutcome(node.id, kind.value, "completed", r.text, r.cost, model=r.model)
        if kind is NodeKind.RETRIEVAL:
            if self._memory is None:
                return NodeOutcome(node.id, kind.value, "completed", "")
            k = k_override or int(p.get("k", 8))
            hits = await self._memory.search(
                st.ctx.account_id,
                scopes=[(s[0], s[1]) for s in p.get("scopes", [])],
                query=st.ctx.goal,
                k=k,
            )
            self._event(st, "CONTEXT_SELECTED", node=node.id, items=len(hits), k=k)
            return NodeOutcome(
                node.id, kind.value, "completed", "\n".join(f"- {h.content}" for h in hits)
            )
        if kind is NodeKind.TOOL:
            name = str(p.get("tool"))
            tool = self._tools.get(name)
            args = dict(p.get("args") or {})
            if p.get("args_from"):
                args["code"] = extract_code(inputs.get(str(p["args_from"]), ""))
            self._event(st, "TOOL_SELECTED", node=node.id, tool=name)
            res = await tool.run(st.ctx.account_id, args)
            self._event(st, "TOOL_EXECUTED", node=node.id, tool=name, ok=res.ok)
            if not res.ok:
                return NodeOutcome(
                    node.id, kind.value, "failed", res.output, error=f"tool failure: {res.output}"
                )
            return NodeOutcome(
                node.id, kind.value, "completed", res.output, res.cost, model=f"tool:{name}"
            )
        if kind is NodeKind.VERIFY:
            return await self._verify(st, node, inputs)
        if kind is NodeKind.HUMAN_APPROVAL:
            decision = st.ctx.approvals.get(node.id)
            if decision is None:
                st.awaiting_approval = True
                st.pending[node.id] = inputs.get(str(p.get("target")), "")
                self._event(st, "VERIFICATION_REQUIRED", node=node.id, level="human")
                return NodeOutcome(node.id, kind.value, "awaiting")
            if decision.get("decision") != "approved":
                self._event(st, "VERIFICATION_FAILED", node=node.id, level="human")
                raise _Stop("rejected", "rejected by reviewer")
            self._event(st, "VERIFICATION_PASSED", node=node.id, level="human")
            # Release exactly what the reviewer approved, even if upstream nodes were
            # recomputed on resume (expired checkpoints) and would now differ.
            return NodeOutcome(node.id, kind.value, "completed", decision.get("content", ""))
        raise ValueError(f"unknown node kind {kind}")

    async def _verify(self, st: _State, node: Node, inputs: dict[str, str]) -> NodeOutcome:
        target = str(node.params.get("target"))
        answer = inputs.get(target, "")
        sources = "\n".join(v for k, v in inputs.items() if k != target and v)
        level = str(node.params.get("level", "light"))
        self._event(st, "VERIFICATION_REQUIRED", node=node.id, level=level)
        verdict, cost, model = "SUPPORTED", Decimal(0), None
        if not answer.strip():
            verdict = "UNSUPPORTED"
        elif level == "light":
            # Rule-based: an answer that shares nothing with its sources is suspect.
            if sources and token_f1(answer, sources) < 0.05:
                verdict = "UNSUPPORTED"
        else:
            r = await self._llm.call(
                st.ctx.account_id,
                tier="frontier",
                policy=st.ctx.policy,
                max_tokens=8,
                messages=[
                    Message("system", render("graph/verify_system.j2")),
                    Message(
                        "user",
                        render(
                            "graph/verify_user.j2", goal=st.ctx.goal, sources=sources, answer=answer
                        ),
                    ),
                ],
            )
            cost, model = r.cost, r.model
            verdict = (
                "UNSUPPORTED" if re.search(r"\bUNSUPPORTED\b", r.text.upper()) else "SUPPORTED"
            )
        self._event(st, "VERIFICATION_COMPLETED", node=node.id, verdict=verdict)
        return NodeOutcome(node.id, NodeKind.VERIFY.value, "completed", verdict, cost, model=model)

    async def _escalate(self, st: _State, verify: Node, outcome: NodeOutcome) -> NodeOutcome:
        """Spec §55-56: classify, then re-run the cheapest fix: more retrieval, then a
        stronger model for the target, then re-verify."""
        target = st.graph.node(str(verify.params.get("target")))
        current_tier = str(target.params.get("tier", "auto"))
        for _ in range(self._max_escalations):
            if st.escalations >= self._max_escalations:
                break
            st.escalations += 1
            current_tier = ESCALATE.get(current_tier, "frontier")
            self._event(st, "ESCALATION_REQUIRED", node=target.id, to_tier=current_tier)
            self._event(st, "REPLAN_REQUIRED", reason="verification failed")
            for dep in target.inputs:
                dep_node = st.graph.node(dep)
                if dep_node.kind is NodeKind.RETRIEVAL:  # retrieval failure → widen context
                    await self._run_node(
                        st, dep_node, k_override=int(dep_node.params.get("k", 8)) * 2
                    )
            await self._run_node(st, target, tier_override=current_tier)
            inputs = {i: st.outcomes[i].output for i in verify.inputs if i in st.outcomes}
            verified = await self._verify(st, verify, inputs)
            st.cost += verified.cost
            st.outcomes[verify.id] = verified
            if verified.output == "SUPPORTED":
                return self._pass_through(st, verify, verified)
        st.warnings.append(
            "verification failed after escalation; returning best answer with caveat"
        )
        return self._pass_through(st, verify, st.outcomes[verify.id])

    @staticmethod
    def _pass_through(st: _State, verify: Node, verdict: NodeOutcome) -> NodeOutcome:
        """A verify node that is the graph output yields the verified answer."""
        target = str(verify.params.get("target"))
        answer = st.outcomes[target].output if target in st.outcomes else ""
        result = NodeOutcome(
            verify.id, NodeKind.VERIFY.value, "completed", answer, verdict.cost, model=verdict.model
        )
        result.error = None if verdict.output == "SUPPORTED" else "unverified"
        st.outcomes[verify.id] = result
        return result


@dataclass
class _State:
    ctx: RunContext
    graph: Graph
    t0: float = field(default_factory=time.monotonic)
    outcomes: dict[str, NodeOutcome] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    cost: Decimal = Decimal(0)
    escalations: int = 0
    warnings: list[str] = field(default_factory=list)
    awaiting_approval: bool = False
    pending: dict[str, str] = field(default_factory=dict)
