"""Runtime endpoints."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text

from aura_engine import (
    EngineDelta,
    EngineDone,
    EngineError,
    Execution,
    ExecutionContext,
    ExecutionOptions,
)
from aura_engine.engine import PreparedExecution
from aura_policy import ExecutionPolicy
from aura_prompts import render
from aura_providers.domain.types import Message
from aura_runtime.deps import ContainerDep, ContextDep
from aura_runtime.schemas import (
    MODEL_ALIASES,
    ApprovalIn,
    ChatCompletionIn,
    DocumentIn,
    ExecuteIn,
    MemoryIn,
    OpeIn,
    OutcomeIn,
    PersonalMemoryIn,
    TuneIn,
    parse_memory_scopes,
)
from aura_shared.errors import Forbidden, NotFound, ValidationFailed
from aura_shared.ids import new_id

router = APIRouter()


def _headers(execution_id: str, trace_id: str, model: str | None = None) -> dict[str, str]:
    h = {"x-aura-execution-id": execution_id, "x-aura-trace-id": trace_id}
    if model:
        h["x-aura-model"] = model
    return h


def _usage(e: Execution) -> dict[str, int]:
    u = e.result.usage
    return {
        "prompt_tokens": u.input_tokens,
        "completion_tokens": u.output_tokens,
        "total_tokens": u.input_tokens + u.output_tokens,
    }


def _resolve_model(name: str) -> tuple[str | None, ExecutionPolicy | None]:
    """Aliases route (optionally with a quality mode). Anything else is an explicit model."""
    if name in MODEL_ALIASES:
        quality = MODEL_ALIASES[name]
        return None, ExecutionPolicy(quality=quality) if quality else None
    return name, None


# ---- /v1/chat/completions ---------------------------------------------------------------


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionIn, ctx: ContextDep, c: ContainerDep
) -> JSONResponse | StreamingResponse:
    req = body.to_chat_request()
    explicit, alias_policy = _resolve_model(body.model)
    request_policy = body.aura.to_policy() if body.aura else None
    a = body.aura
    prompt_version_id = None
    if a and a.prompt:
        resolved = await c.prompts.resolve(ctx.account_id, a.prompt)
        system = Message("system", render(resolved.content, a.prompt_vars))
        req = dataclasses.replace(req, messages=(system, *req.messages))
        prompt_version_id = resolved.version_id
    opts = await _options(
        c,
        ctx,
        model=explicit,
        # alias (e.g. aura/fast) first, explicit `aura` options override it
        policy=_combine(alias_policy, request_policy),
        max_tokens=req.max_tokens,
        session_id=a.session_id if a else None,
        workload=a.workload if a else None,
        sticky=(a.sticky_key if a else None) or body.user or (a.session_id if a else None),
        memory=a.memory if a else None,
    )
    opts = dataclasses.replace(opts, prompt_version_id=prompt_version_id)
    run = await c.engine.prepare(ctx, req, opts, stream=body.stream)
    created = int(time.time())
    if not body.stream:
        e = await c.engine.execute(run)
        return JSONResponse(
            {
                "id": f"chatcmpl-{e.execution_id}",
                "object": "chat.completion",
                "created": created,
                "model": e.spec.id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": e.result.text},
                        "finish_reason": e.result.finish_reason.value,
                    }
                ],
                "usage": _usage(e),
                "aura": e.aura_block(),
            },
            headers=_headers(e.execution_id, e.trace_id, e.spec.id),
        )
    return await _sse(c, run, created)


async def _options(
    c: Any,
    ctx: ExecutionContext,
    *,
    model: str | None,
    policy: ExecutionPolicy | None,
    max_tokens: int | None,
    session_id: str | None,
    workload: str | None,
    sticky: str | None,
    context_docs: tuple[str, ...] = (),
    memory: list[str] | None = None,
) -> ExecutionOptions:
    """Resolve persisted policies (account → workload, canary-aware) for this request."""
    r = await c.policies.resolve(
        ctx.account_id, workload=workload, sticky_key=sticky or new_id("trc")
    )
    scopes = parse_memory_scopes(memory)
    if ctx.user_id is not None:
        # A signed-in person: their personal memory always applies, and they can never
        # address another member's user scope in a shared account.
        if any(sc == "user" and ref != ctx.user_id for sc, ref in scopes):
            raise Forbidden("you can only use your own personal memory")
        if ("user", ctx.user_id) not in scopes:
            scopes = (*scopes, ("user", ctx.user_id))
    return ExecutionOptions(
        model=model,
        policy=policy,
        account_policy=r.policy,
        max_tokens=max_tokens,
        session_id=session_id,
        policy_id=r.policy_id,
        policy_version_id=r.policy_version_id,
        deployment_id=r.deployment_id,
        variant=r.variant,
        context_docs=context_docs,
        memory_scopes=scopes,
        workload=workload,
    )


def _combine(a: ExecutionPolicy | None, b: ExecutionPolicy | None) -> ExecutionPolicy | None:
    if a is None or b is None:
        return a or b
    from aura_policy import merge

    return merge(a, b)


async def _sse(c: Any, run: PreparedExecution, created: int) -> JSONResponse | StreamingResponse:
    events = c.engine.stream(run)
    # Pull the first event before committing to a 200 SSE response, so that failing to
    # start (every model down) still returns a proper HTTP error.
    first = await anext(events)
    if isinstance(first, EngineError):
        return JSONResponse(
            first.error.to_problem(),
            status_code=first.error.status,
            media_type="application/problem+json",
            headers=_headers(first.execution_id, first.trace_id),
        )
    chunk_id = f"chatcmpl-{run.execution_id}"

    def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> str:
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": run.route.primary.id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            **extra,
        }
        return f"data: {json.dumps(payload, default=str)}\n\n"

    async def body() -> AsyncIterator[str]:
        yield chunk({"role": "assistant", "content": ""})
        event = first
        try:
            while True:
                if isinstance(event, EngineDelta):
                    yield chunk({"content": event.text})
                elif isinstance(event, EngineDone):
                    e = event.execution
                    yield chunk(
                        {}, e.result.finish_reason.value, usage=_usage(e), aura=e.aura_block()
                    )
                    break
                elif isinstance(event, EngineError):
                    err = {"error": event.error.to_problem()}
                    yield f"data: {json.dumps(err, default=str)}\n\n"
                    break
                event = await anext(events)
        finally:
            await events.aclose()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            **_headers(run.execution_id, run.trace_id),
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
    )


# ---- /v1/execute ------------------------------------------------------------------------


@router.post("/v1/execute")
async def execute(body: ExecuteIn, ctx: ContextDep, c: ContainerDep) -> JSONResponse:
    if body.mode == "planned":
        return JSONResponse(await _run_graph(body, ctx, c))
    req = body.to_chat_request()
    explicit, alias_policy = _resolve_model(body.model) if body.model else (None, None)
    opts = await _options(
        c,
        ctx,
        model=explicit,
        policy=_combine(alias_policy, body.constraints.to_policy() if body.constraints else None),
        max_tokens=body.max_tokens,
        session_id=body.session_id,
        workload=body.workload,
        sticky=body.sticky_key or body.session_id,
        context_docs=(body.context,) if body.context else (),
        memory=body.memory,
    )
    run = await c.engine.prepare(ctx, req, opts, stream=False)
    e = await c.engine.execute(run)
    return JSONResponse(
        {
            "id": e.execution_id,
            "object": "aura.execution",
            "output": e.result.text,
            "finish_reason": e.result.finish_reason.value,
            "model": e.spec.id,
            "usage": _usage(e),
            "aura": e.aura_block(),
        },
        headers=_headers(e.execution_id, e.trace_id, e.spec.id),
    )


# ---- execution graphs -------------------------------------------------------------------


async def _graph_request(body: ExecuteIn, ctx: ExecutionContext, c: Any) -> Any:
    from aura_graph.service import GraphRequest
    from aura_policy import merge

    r = await c.policies.resolve(
        ctx.account_id, workload=body.workload, sticky_key=body.sticky_key or new_id("trc")
    )
    request_policy = body.constraints.to_policy() if body.constraints else None
    policy = merge(r.policy, request_policy)
    kb = tuple(s for s in parse_memory_scopes(body.memory) if s[0] == "kb")
    goal = (
        body.goal if not body.context else f"{body.goal}\n\n<context>\n{body.context}\n</context>"
    )
    return GraphRequest(
        ctx.account_id,
        ctx.workspace_id,
        ctx.region,
        goal,
        kb,
        policy,
        body.constraints.max_cost if body.constraints else None,
        body.require_approval,
    )


async def _run_graph(body: ExecuteIn, ctx: ExecutionContext, c: Any) -> dict[str, Any]:
    result = await c.graphs.execute(await _graph_request(body, ctx, c))
    return {
        "id": result["id"],
        "object": "aura.execution",
        "mode": "planned",
        "output": result["output"],
        "status": result["status"],
        "aura": {"intent": result["intent"], "graph": result["graph"], "trace": result["trace"]},
    }


@router.post("/v1/runs", status_code=202)
async def submit_run(body: ExecuteIn, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    run_id = await c.graphs.submit(await _graph_request(body, ctx, c))
    return {"id": run_id, "object": "aura.run", "status": "queued"}


@router.get("/v1/runs/{run_id}")
async def get_run(run_id: str, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    return dict(await c.graphs.get(ctx.account_id, run_id)) | {"object": "aura.run"}


@router.post("/v1/runs/{run_id}/approval")
async def decide_run(
    run_id: str, body: ApprovalIn, ctx: ContextDep, c: ContainerDep
) -> dict[str, Any]:
    """A person must decide: API keys (machines) cannot approve their own runs."""
    if ctx.user_id is None:
        raise Forbidden("approvals need a signed-in reviewer, not an API key")
    status = await c.graphs.decide(
        ctx.account_id, run_id, decision=body.decision, reviewer=ctx.user_id, reason=body.reason
    )
    return {"id": run_id, "object": "aura.run", "status": status}


# ---- /v1/outcomes -----------------------------------------------------------------------


@router.post("/v1/outcomes", status_code=202)
async def record_outcome(body: OutcomeIn, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    """Report a business outcome for an execution (spec §59): it feeds canary analysis
    and cost-per-outcome."""
    ref = await c.outcomes.record(ctx.account_id, body.execution_id, body.outcome)
    return {"status": "recorded", **ref}


# ---- long-term memory & knowledge bases ---------------------------------------------------


@router.post("/v1/memories", status_code=201)
async def add_memory(body: MemoryIn, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    await c.entitlements.require(ctx.account_id, "knowledge_retrieval")
    mid, created = await c.long_memory.add(
        ctx.account_id,
        scope=body.scope,
        scope_ref=body.scope_ref,
        content=body.content,
        kind=body.kind,
        importance=body.importance,
        source=f"api:{ctx.api_key_id}",
    )
    return {"id": mid, "created": created}


@router.post("/v1/knowledge/{kb}/documents", status_code=201)
async def add_document(
    kb: str, body: DocumentIn, ctx: ContextDep, c: ContainerDep
) -> dict[str, Any]:
    from aura_context.compiler import chunk

    await c.entitlements.require(ctx.account_id, "knowledge_retrieval")
    ids = []
    for part in chunk(body.text):
        mid, _ = await c.long_memory.add(
            ctx.account_id,
            scope="kb",
            scope_ref=kb,
            content=part,
            kind="document",
            source=body.source,
        )
        ids.append(mid)
    return {"knowledge_base": kb, "chunks": len(ids)}


@router.get("/v1/memories/search")
async def search_memory(
    scope: str, scope_ref: str, q: str, ctx: ContextDep, c: ContainerDep, k: int = 8
) -> dict[str, Any]:
    await c.entitlements.require(ctx.account_id, "knowledge_retrieval")
    hits = await c.long_memory.search(
        ctx.account_id,
        scopes=list(parse_memory_scopes([f"{scope}:{scope_ref}"])),
        query=q,
        k=min(max(k, 1), 50),
    )
    return {"data": [dataclasses.asdict(h) for h in hits]}


@router.post("/v1/knowledge/{kb}/tune")
async def tune_knowledge(kb: str, body: TuneIn, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    from aura_memory.rag_tuning import LabelledQuestion, tune

    await c.entitlements.require(ctx.account_id, "knowledge_retrieval")
    return await tune(
        c.long_memory,
        ctx.account_id,
        scopes=[("kb", kb)],
        questions=[LabelledQuestion(q.question, q.expected) for q in body.questions],
        recall_target=body.recall_target,
    )


@router.delete("/v1/memories", status_code=200)
async def forget_memory(
    scope: str, scope_ref: str, ctx: ContextDep, c: ContainerDep
) -> dict[str, Any]:
    deleted = await c.long_memory.forget(ctx.account_id, scope=scope, scope_ref=scope_ref)
    return {"deleted": deleted}


# ---- personal memory (spec §17: user-visible and editable) ---------------------------------


def _me(ctx: ExecutionContext) -> str:
    if ctx.user_id is None:
        raise Forbidden("personal memory needs a signed-in user, not an API key")
    return ctx.user_id


@router.get("/v1/me/memories")
async def list_my_memories(ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    user_id = _me(ctx)
    items = await c.long_memory.list_items(ctx.account_id, scope="user", scope_ref=user_id)
    return {"data": [{"id": m.id, "kind": m.kind, "content": m.content} for m in items]}


@router.post("/v1/me/memories", status_code=201)
async def add_my_memory(body: PersonalMemoryIn, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    user_id = _me(ctx)
    await c.entitlements.require(ctx.account_id, "personal_memory")
    mid, created = await c.long_memory.add(
        ctx.account_id,
        scope="user",
        scope_ref=user_id,
        content=body.content,
        kind=body.kind,
        importance=0.8,
        confidence=1.0,  # the user said it themselves
        source="user",
    )
    return {"id": mid, "created": created}


@router.delete("/v1/me/memories/{memory_id}", status_code=204)
async def delete_my_memory(memory_id: str, ctx: ContextDep, c: ContainerDep) -> None:
    user_id = _me(ctx)
    if not await c.long_memory.delete_item(
        ctx.account_id, memory_id, scope="user", scope_ref=user_id
    ):
        raise NotFound("memory not found")


@router.delete("/v1/me/memories")
async def forget_me(ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    user_id = _me(ctx)
    return {"deleted": await c.long_memory.forget(ctx.account_id, scope="user", scope_ref=user_id)}


# ---- learning: off-policy evaluation -------------------------------------------------------


@router.post("/v1/learning/evaluate")
async def evaluate_policy(body: OpeIn, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    """Estimate how a candidate routing policy would have performed on this tenant's
    logged decisions, before moving any traffic (IPS / SNIPS / doubly robust)."""
    from aura_learning import estimate

    await c.entitlements.require(ctx.account_id, "advanced_routing")
    logs = await c.learner.decisions(ctx.account_id, body.workload, limit=body.limit)
    if len(logs) < 20:
        raise ValidationFailed("not enough logged decisions yet (need at least 20)")
    if body.target == "learned":
        bandit = await c.learner.bandit(ctx.account_id, body.workload)

        def target(x: tuple[float, ...]) -> dict[str, float]:
            arms = sorted({d.action for d in logs})
            return bandit.probabilities(list(x), arms, epsilon=0.0)
    else:
        model = body.target.removeprefix("always:")

        def target(x: tuple[float, ...]) -> dict[str, float]:
            return {model: 1.0}

    return estimate(logs, target, bootstrap=300)


# ---- /v1/models -------------------------------------------------------------------------


@router.get("/v1/models")
async def list_models(ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    data: list[dict[str, Any]] = [
        {"id": alias, "object": "model", "owned_by": "aura"}
        for alias in ("aura/auto", "aura/fast", "aura/balanced", "aura/best")
    ]
    usable = await c.engine.usable_providers(ctx.account_id)
    for spec in sorted(c.engine.catalog.enabled(), key=lambda s: (s.provider, s.tier)):
        data.append(
            {
                "id": spec.id,
                "object": "model",
                "owned_by": spec.provider,
                "aura": {
                    "display_name": spec.display_name,
                    # Callable right now with a platform key or the account's own key.
                    "usable": spec.provider in usable,
                    "local": spec.prices.input == 0 and spec.prices.output == 0,
                    "tier": spec.tier.name.lower(),
                    "context_window": spec.context_window,
                    "max_output": spec.max_output,
                    "prices_usd_per_mtok": {
                        "input": str(spec.prices.input),
                        "output": str(spec.prices.output),
                        "cached_input": str(spec.prices.cached_input),
                    },
                },
            }
        )
    return {"object": "list", "data": data}


# ---- health -----------------------------------------------------------------------------


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/readyz")
async def readyz(c: ContainerDep) -> JSONResponse:
    checks: dict[str, bool] = {}
    try:
        async with c.db.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:  # noqa: BLE001
        checks["postgres"] = False
    checks.update({f"redis_{r.value}": ok for r, ok in (await c.redis.ping()).items()})
    checks["providers"] = bool(c.engine.catalog.enabled())
    ok = all(checks.values())
    return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)


def new_trace_id() -> str:
    return new_id("trc")
