"""Graph runs as a product surface: synchronous (inside a request) and asynchronous
(job queue). Metered, policy-constrained, telemetry-emitting; async goal and output are
envelope-encrypted at rest."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aura_billing.application.entitlements import EntitlementService, Reservation
from aura_db import Database, audit, jobs
from aura_db.jobs import ClaimedJob
from aura_db.models import GraphRun
from aura_graph.agents import AgentOrchestrator, AgentResult, EventSink
from aura_graph.decompose import llm_split, plan_subqueries, split_subqueries, worth_llm_split
from aura_graph.executor import GraphExecutor, GraphResult, RunContext
from aura_graph.intent import TaskType, compile_intent
from aura_graph.planner import plan, with_approval
from aura_policy import ExecutionPolicy, from_dict, to_dict
from aura_shared.crypto import Envelope, Sealed
from aura_shared.errors import Conflict, NotFound, QuotaExceeded, ValidationFailed
from aura_shared.events import Event, ExecutionCompleted, ExecutionStatus
from aura_shared.ids import new_id

JOB_KIND = "graph_run"


@dataclass(frozen=True, slots=True)
class GraphRequest:
    account_id: str
    workspace_id: str
    region: str
    goal: str
    kb_scopes: tuple[tuple[str, str], ...] = ()
    policy: ExecutionPolicy | None = None
    budget_usd: Decimal | None = None
    require_approval: bool = False
    approvals: dict[str, dict[str, str]] = field(default_factory=dict)
    subqueries: tuple[str, ...] = ()  # set: answer these in parallel, then combine
    as_tasks: bool = False  # True: subqueries generated as tasks, not questions


def _aad(account_id: str, run_id: str) -> str:
    return f"{account_id}|graph|{run_id}"


class GraphRunService:
    def __init__(
        self,
        db: Database,
        *,
        executor: GraphExecutor,
        entitlements: EntitlementService,
        envelope: Envelope,
        can_search: bool,
        publish: Callable[[Event], None] | None = None,
        agents: AgentOrchestrator | None = None,
    ) -> None:
        self._db = db
        self._executor = executor
        self._ent = entitlements
        self._envelope = envelope
        self._can_search = can_search
        self._publish = publish
        self._agents = agents or AgentOrchestrator(
            executor.llm, reviewer_model="groq/openai/gpt-oss-20b"
        )

    async def _reserve(self, account_id: str, run_id: str, research: bool) -> list[Reservation]:
        ents = await self._ent.get(account_id)
        out = []
        try:
            if ents.allows("executions"):
                out.append(
                    await self._ent.reserve(
                        account_id, "executions", amount=1, reservation_id=f"{run_id}:executions"
                    )
                )
            if research and ents.allows("research_jobs"):
                out.append(
                    await self._ent.reserve(
                        account_id, "research_jobs", amount=1, reservation_id=f"{run_id}:research"
                    )
                )
        except QuotaExceeded:
            for r in out:
                await self._ent.release(r)
            raise
        return out

    async def execute(self, req: GraphRequest, *, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or new_id("grun")
        intent = compile_intent(
            req.goal, has_knowledge=bool(req.kb_scopes), can_search=self._can_search
        )
        graph = (
            plan_subqueries(req.goal, list(req.subqueries), as_tasks=req.as_tasks)
            if req.subqueries
            else plan(intent, goal=req.goal, kb_scopes=list(req.kb_scopes))
        )
        if req.require_approval:
            graph = with_approval(graph)
        research = intent.task is TaskType.RESEARCH and not req.subqueries
        reservations = await self._reserve(req.account_id, run_id, research)
        try:
            result = await self._executor.run(
                graph,
                RunContext(
                    req.account_id,
                    run_id,
                    req.goal,
                    policy=req.policy or ExecutionPolicy(),
                    budget_usd=req.budget_usd,
                    region=req.region,
                    approvals=dict(req.approvals),
                ),
            )
        except BaseException:
            for r in reservations:
                await self._ent.release(r)
            raise
        for r in reservations:
            await self._ent.commit(r, actual=1)
        self._emit(req, run_id, graph.template, result)
        return {
            "id": run_id,
            "output": result.output,
            "status": result.status,
            "intent": {
                "task": intent.task.value,
                "risk": intent.risk.value,
                "needs": sorted(intent.needs),
                "reason": intent.reason,
            },
            "graph": graph.to_dict(),
            "trace": result.trace(),
            "pending": result.pending,
        }

    def _emit(self, req: GraphRequest, run_id: str, template: str, result: GraphResult) -> None:
        if self._publish is None:
            return
        self._publish(
            ExecutionCompleted(
                account_id=req.account_id,
                region=req.region,
                trace_id=run_id,
                execution_id=run_id,
                workspace_id=req.workspace_id,
                mode=f"graph:{template}",
                status=ExecutionStatus.SUCCEEDED
                if result.status == "completed"
                else ExecutionStatus.FAILED,
                total_latency_ms=result.events[-1]["t_ms"] if result.events else 0,
                total_cost=result.cost,
                error_code=None if result.status == "completed" else result.status,
                notes=result.warnings,
            )
        )

    async def decompose(
        self,
        account_id: str,
        text: str,
        *,
        policy: ExecutionPolicy | None = None,
        always: bool = False,
        as_tasks: bool = False,
    ) -> list[str]:
        """Sub-queries (questions or tasks) for a multi-part request ([] for single).
        Rules first (free); a small-model split for long compound requests, or for
        every request when ``always``."""
        found = split_subqueries(text, as_tasks=as_tasks)
        if not found and (always or worth_llm_split(text)):
            found = await llm_split(
                self._executor.llm,
                account_id,
                text,
                policy=policy or ExecutionPolicy(),
                as_tasks=as_tasks,
            )
        return found

    async def run_agents(
        self,
        req: GraphRequest,
        *,
        subqueries: list[str],
        mode: str = "auto",
        drafter_model: str | None = None,
        on_event: EventSink | None = None,
        history: str | None = None,
        memory: list[str] | None = None,
    ) -> AgentResult:
        """Drafter ↔ Reviewer → Finaliser over a query tree, counted as one execution."""
        run_id = new_id("grun")
        reservations = await self._reserve(req.account_id, run_id, research=False)
        try:
            result = await self._agents.run(
                req.account_id,
                req.goal,
                subqueries=subqueries,
                policy=req.policy,
                mode=mode,
                drafter_model=drafter_model,
                region=req.region,
                on_event=on_event,
                history=history,
                memory=memory,
            )
        except BaseException:
            for r in reservations:
                await self._ent.release(r)
            raise
        for r in reservations:
            await self._ent.commit(r, actual=1)
        if self._publish is not None:
            self._publish(
                ExecutionCompleted(
                    account_id=req.account_id,
                    region=req.region,
                    trace_id=run_id,
                    execution_id=run_id,
                    workspace_id=req.workspace_id,
                    mode="agents",
                    status=ExecutionStatus.SUCCEEDED,
                    total_latency_ms=result.ms,
                    total_cost=result.cost,
                    notes=result.warnings,
                )
            )
        return result

    # ---- async runs ------------------------------------------------------------------
    async def submit(self, req: GraphRequest) -> str:
        await self._ent.require(req.account_id, "long_running_workflows")
        run_id = new_id("grun")
        sealed = self._envelope.seal(
            json.dumps({"goal": req.goal}), aad=_aad(req.account_id, run_id)
        )
        async with self._db.tenant(req.account_id) as s:
            s.add(
                GraphRun(
                    id=run_id,
                    account_id=req.account_id,
                    status="queued",
                    request={
                        "workspace_id": req.workspace_id,
                        "region": req.region,
                        "kb_scopes": [list(x) for x in req.kb_scopes],
                        "policy": to_dict(req.policy) if req.policy else {},
                        "budget_usd": str(req.budget_usd) if req.budget_usd else None,
                        "require_approval": req.require_approval,
                        "approvals": {},
                    },
                    key_id=sealed.key_id,
                    wrapped_dek=sealed.wrapped_dek,
                    nonce=sealed.nonce,
                    ciphertext=sealed.ciphertext,
                )
            )
            await jobs.enqueue(s, JOB_KIND, {"run_id": run_id}, account_id=req.account_id)
        return run_id

    async def process(self, job: ClaimedJob) -> None:
        run_id, account_id = job.payload["run_id"], job.account_id
        assert account_id is not None
        async with self._db.tenant(account_id) as s:
            row = await s.get(GraphRun, run_id)
            if row is None or row.status not in ("queued", "running"):
                return
            row.status = "running"
            sealed = Sealed(
                row.key_id or "", row.wrapped_dek or b"", row.nonce or b"", row.ciphertext or b""
            )
            opts = dict(row.request)
        stored = json.loads(self._envelope.open(sealed, aad=_aad(account_id, run_id)))
        goal = stored["goal"]
        approved_content: dict[str, str] = stored.get("approved") or {}
        approvals = {
            node: {"decision": meta["decision"], "content": approved_content.get(node, "")}
            for node, meta in (opts.get("approvals") or {}).items()
        }
        req = GraphRequest(
            account_id,
            opts["workspace_id"],
            opts["region"],
            goal,
            tuple((str(x[0]), str(x[1])) for x in opts.get("kb_scopes", [])),
            from_dict(opts.get("policy") or {}),
            Decimal(opts["budget_usd"]) if opts.get("budget_usd") else None,
            bool(opts.get("require_approval")),
            approvals,
        )
        try:
            result = await self.execute(req, run_id=run_id)
            status, error = result["status"], None
        except Exception as exc:
            if job.attempts < job.max_attempts:
                raise
            result, status, error = {"output": "", "trace": {}}, "failed", str(exc)[:2000]
        out = self._envelope.seal(
            json.dumps(
                {
                    "goal": goal,
                    "output": result["output"],
                    "pending": result.get("pending") or {},
                    "approved": approved_content,
                }
            ),
            aad=_aad(account_id, run_id),
        )
        async with self._db.tenant(account_id) as s:
            row = await s.get(GraphRun, run_id)
            assert row is not None
            row.status, row.error = status, error
            row.template = (result.get("graph") or {}).get("template")
            row.trace = result.get("trace") or {}
            row.cost_usd = float(Decimal(row.trace.get("cost_usd", "0")))
            row.key_id, row.wrapped_dek, row.nonce, row.ciphertext = (
                out.key_id,
                out.wrapped_dek,
                out.nonce,
                out.ciphertext,
            )

    async def get(self, account_id: str, run_id: str) -> dict[str, Any]:
        async with self._db.tenant(account_id) as s:
            row = await s.get(GraphRun, run_id)
            if row is None:
                raise NotFound("run not found")
            sealed = Sealed(
                row.key_id or "", row.wrapped_dek or b"", row.nonce or b"", row.ciphertext or b""
            )
            body = json.loads(self._envelope.open(sealed, aad=_aad(account_id, run_id)))
            return {
                "id": row.id,
                "status": row.status,
                "template": row.template,
                "output": body.get("output"),
                "trace": row.trace,
                "cost_usd": str(row.cost_usd),
                "error": row.error,
                "pending_approval": body.get("pending") or None,
            }

    async def decide(
        self, account_id: str, run_id: str, *, decision: str, reviewer: str, reason: str = ""
    ) -> str:
        """Record a reviewer's decision on a paused run. Approval resumes the run and
        releases exactly the reviewed content; rejection ends it."""
        if decision not in ("approved", "rejected"):
            raise ValidationFailed("decision must be approved or rejected")
        async with self._db.tenant(account_id) as s:
            row = await s.get(GraphRun, run_id, with_for_update=True)
            if row is None:
                raise NotFound("run not found")
            if row.status != "awaiting_approval":
                raise Conflict(f"run is {row.status}, not awaiting approval")
            sealed = Sealed(
                row.key_id or "", row.wrapped_dek or b"", row.nonce or b"", row.ciphertext or b""
            )
            body = json.loads(self._envelope.open(sealed, aad=_aad(account_id, run_id)))
            pending: dict[str, str] = body.get("pending") or {}
            if not pending:
                raise Conflict("nothing is awaiting approval")
            request = dict(row.request)
            approvals = dict(request.get("approvals") or {})
            approved = dict(body.get("approved") or {})
            for node, content in pending.items():
                approvals[node] = {"decision": decision, "by": reviewer, "reason": reason[:500]}
                if decision == "approved":
                    approved[node] = content
            request["approvals"] = approvals
            row.request = request
            out = self._envelope.seal(
                json.dumps(
                    {"goal": body["goal"], "output": body.get("output"), "approved": approved}
                ),
                aad=_aad(account_id, run_id),
            )
            row.key_id, row.wrapped_dek, row.nonce, row.ciphertext = (
                out.key_id,
                out.wrapped_dek,
                out.nonce,
                out.ciphertext,
            )
            if decision == "approved":
                row.status = "queued"
                await jobs.enqueue(s, JOB_KIND, {"run_id": run_id}, account_id=account_id)
            else:
                row.status = "rejected"
            await audit.record(
                s,
                actor_type="user",
                actor_id=reviewer,
                account_id=account_id,
                action=f"graph_run.{decision}",
                target_type="graph_run",
                target_id=run_id,
                after={"nodes": sorted(pending), "reason": reason[:500]},
            )
            return row.status
