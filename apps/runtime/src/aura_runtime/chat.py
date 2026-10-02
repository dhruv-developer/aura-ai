"""The AURA app's chat: multiple saved conversations, streamed answers, and Auto mode
with sub-query decomposition.

Each turn:
1. The user's message is stored (encrypted), and the chat is titled from it.
2. Auto mode only: a multi-part message becomes 2-5 sub-questions. They are answered in
   parallel, each routed by its own difficulty, and then combined (graph executor).
3. Otherwise the message is answered by the engine with the conversation's history,
   streamed token by token.
4. The answer is stored together with its content-free ``aura`` metadata, so reopening
   a chat shows how every answer was optimized.

Server-sent events: ``user`` (stored user message), ``plan`` (sub-questions),
``delta`` (text), ``done`` (stored assistant message), ``error``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_engine import ExecutionContext
from aura_engine.engine import EngineDelta, EngineDone, EngineError, EngineEvent
from aura_memory import MessageView
from aura_policy import ExecutionPolicy, QualityMode
from aura_providers.domain.types import ChatRequest, Message
from aura_runtime.container import RuntimeContainer
from aura_runtime.deps import ContainerDep, ContextDep
from aura_shared.errors import AuraError, Conflict, Forbidden, ValidationFailed

router = APIRouter(prefix="/v1/conversations", tags=["chat"])
log = logging.getLogger("aura.runtime.chat")

# Answers in progress, by conversation. An answer runs as its own task so that closing or
# reloading the page does not cancel it: it finishes and is saved, and reopening the chat
# shows it. Only an explicit Stop cancels. (Per process: with several runtime replicas,
# Stop would need a shared signal such as Redis pub/sub.)
_RUNNING: dict[str, asyncio.Task[None]] = {}

_SSE_HEADERS = {"cache-control": "no-cache", "x-accel-buffering": "no"}


def _detach(conv_id: str, producer: AsyncIterator[str]) -> AsyncIterator[str]:
    """Run ``producer`` to completion in the background and relay its events to whoever
    is listening. If the listener goes away, the work (and its saved answer) carries on."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def run() -> None:
        try:
            async for item in producer:
                queue.put_nowait(item)
        except asyncio.CancelledError:
            raise
        except Exception:  # never lose the stream silently
            log.exception("chat answer failed for %s", conv_id)
            queue.put_nowait(_sse("error", AuraError("answer failed").to_problem()))
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(run())
    _RUNNING[conv_id] = task
    task.add_done_callback(
        lambda t: _RUNNING.pop(conv_id, None) if _RUNNING.get(conv_id) is t else None
    )

    async def relay() -> AsyncIterator[str]:
        while (item := await queue.get()) is not None:
            yield item

    return relay()


Mode = Literal["auto", "fast", "balanced", "best"]
HISTORY_CHAR_BUDGET = 32_000  # ~8k tokens of prior turns per request


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    mode: Mode = "auto"


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    mode: Mode | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=100_000)
    mode: Mode | None = None
    # A specific model instead of AURA's routing (no sub-query split in that case).
    model: str | None = Field(default=None, max_length=120)
    # Decompose into tasks instead of questions (overrides AURA_SUBQUERY_AS_TASKS setting)
    subquery_as_tasks: bool | None = None


def _user(ctx: ExecutionContext) -> str:
    if ctx.user_id is None:
        raise Forbidden("conversations belong to signed-in people; API keys use /v1/chat")
    return ctx.user_id


def _conv_json(v: Any) -> dict[str, Any]:
    d = dataclasses.asdict(v)
    d["created_at"], d["updated_at"] = v.created_at.isoformat(), v.updated_at.isoformat()
    return d


def _msg_json(m: MessageView) -> dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "aura": m.aura,
        "trace": m.trace,
        "created_at": m.created_at.isoformat(),
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _history(messages: list[MessageView]) -> list[Message]:
    """Newest turns first until the budget is spent, then back in order."""
    kept: list[Message] = []
    used = 0
    for m in reversed(messages):
        used += len(m.content)
        if used > HISTORY_CHAR_BUDGET and kept:
            break
        kept.append(Message("assistant" if m.role == "assistant" else "user", m.content))
    kept.reverse()
    return kept


@router.get("")
async def list_conversations(ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    items = await c.conversations.list_for(ctx.account_id, _user(ctx))
    return {"data": [_conv_json(v) for v in items]}


@router.post("", status_code=201)
async def create_conversation(
    body: ConversationIn, ctx: ContextDep, c: ContainerDep
) -> dict[str, Any]:
    v = await c.conversations.create(
        ctx.account_id, _user(ctx), title=body.title or "New chat", mode=body.mode
    )
    return _conv_json(v)


@router.get("/{conv_id}")
async def get_conversation(conv_id: str, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    user = _user(ctx)
    conv = await c.conversations.get(ctx.account_id, user, conv_id)
    messages = await c.conversations.messages(ctx.account_id, user, conv_id)
    return {**_conv_json(conv), "messages": [_msg_json(m) for m in messages]}


@router.patch("/{conv_id}")
async def update_conversation(
    conv_id: str, body: ConversationPatch, ctx: ContextDep, c: ContainerDep
) -> dict[str, Any]:
    v = await c.conversations.update(
        ctx.account_id, _user(ctx), conv_id, title=body.title, mode=body.mode
    )
    return _conv_json(v)


@router.delete("/{conv_id}", status_code=204)
async def delete_conversation(conv_id: str, ctx: ContextDep, c: ContainerDep) -> None:
    await c.conversations.delete(ctx.account_id, _user(ctx), conv_id)


@router.post("/{conv_id}/stop")
async def stop_answer(conv_id: str, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    await c.conversations.get(ctx.account_id, _user(ctx), conv_id)  # ownership check
    task = _RUNNING.get(conv_id)
    if task is None or task.done():
        return {"stopped": False}
    task.cancel()
    return {"stopped": True}


@router.get("/{conv_id}/status")
async def answer_status(conv_id: str, ctx: ContextDep, c: ContainerDep) -> dict[str, Any]:
    await c.conversations.get(ctx.account_id, _user(ctx), conv_id)
    task = _RUNNING.get(conv_id)
    return {"answering": task is not None and not task.done()}


@router.post("/{conv_id}/messages", response_model=None)
async def send_message(
    conv_id: str, body: MessageIn, ctx: ContextDep, c: ContainerDep
) -> StreamingResponse | JSONResponse:
    user = _user(ctx)
    conv = await c.conversations.get(ctx.account_id, user, conv_id)
    running = _RUNNING.get(conv_id)
    if running is not None and not running.done():
        raise Conflict("still answering the previous message; wait or press Stop")
    if body.model is not None and c.engine.catalog.get(body.model) is None:
        raise ValidationFailed(f"unknown model {body.model!r}")  # before storing anything
    mode = body.mode or conv.mode
    if mode != conv.mode:
        await c.conversations.update(ctx.account_id, user, conv_id, mode=mode)
    prior = await c.conversations.messages(ctx.account_id, user, conv_id, limit=200)
    stored_user = await c.conversations.append(
        ctx.account_id,
        user,
        conv_id,
        role="user",
        content=body.content,
        retitle_from=body.content if not prior else None,
    )
    policy = ExecutionPolicy(quality=QualityMode(mode))

    if c.settings.chat_agents:
        return StreamingResponse(
            _detach(
                conv_id,
                _agent_turn(
                    c, ctx, user, conv_id, prior, body.content, mode, body.model, stored_user
                ),
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )
    as_tasks = (
        body.subquery_as_tasks
        if body.subquery_as_tasks is not None
        else c.settings.subquery_as_tasks
    )
    subqueries: list[str] = []
    if mode == "auto" and body.model is None:
        subqueries = await c.graphs.decompose(
            ctx.account_id, body.content, policy=policy, as_tasks=as_tasks
        )
    if subqueries:
        return StreamingResponse(
            _detach(
                conv_id,
                _subquery_turn(
                    c, ctx, user, conv_id, body.content, subqueries, stored_user, as_tasks=as_tasks
                ),
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )
    return await _single_turn(
        c, ctx, user, conv_id, prior, body.content, policy, stored_user, body.model
    )


async def _single_turn(
    c: RuntimeContainer,
    ctx: ExecutionContext,
    user: str,
    conv_id: str,
    prior: list[MessageView],
    content: str,
    policy: ExecutionPolicy,
    stored_user: MessageView,
    model: str | None = None,
) -> StreamingResponse | JSONResponse:
    from aura_runtime.routes import _options

    req = ChatRequest((*_history(prior), Message("user", content)))
    opts = await _options(
        c,
        ctx,
        model=model,
        policy=policy,
        max_tokens=None,
        session_id=None,
        workload=None,
        sticky=conv_id,
    )
    # Errors before the first byte (quota, credits, no model) become a proper HTTP error.
    run = await c.engine.prepare(ctx, req, opts, stream=True)
    events = c.engine.stream(run)
    first = await anext(events)
    if isinstance(first, EngineError):
        await events.aclose()
        return JSONResponse(
            first.error.to_problem(),
            status_code=first.error.status,
            media_type="application/problem+json",
        )

    async def body() -> AsyncIterator[str]:
        yield _sse("user", _msg_json(stored_user))
        event: EngineEvent = first
        partial: list[str] = []
        try:
            while True:
                if isinstance(event, EngineDelta):
                    partial.append(event.text)
                    yield _sse("delta", {"text": event.text})
                elif isinstance(event, EngineDone):
                    e = event.execution
                    aura = {"kind": "single", **e.aura_block()}
                    saved = await c.conversations.append(
                        ctx.account_id,
                        user,
                        conv_id,
                        role="assistant",
                        content=e.result.text,
                        aura=aura,
                    )
                    yield _sse("done", _msg_json(saved))
                    break
                elif isinstance(event, EngineError):
                    yield _sse("error", event.error.to_problem())
                    break
                event = await anext(events)
        except asyncio.CancelledError:
            # Stopped by the user: keep what was written so far, clearly marked.
            if partial:
                await c.conversations.append(
                    ctx.account_id,
                    user,
                    conv_id,
                    role="assistant",
                    content="".join(partial) + "\n\n_(stopped)_",
                    aura={"kind": "stopped"},
                )
            raise
        finally:
            await events.aclose()

    return StreamingResponse(
        _detach(conv_id, body()), media_type="text/event-stream", headers=_SSE_HEADERS
    )


async def _subquery_turn(
    c: RuntimeContainer,
    ctx: ExecutionContext,
    user: str,
    conv_id: str,
    content: str,
    subqueries: list[str],
    stored_user: MessageView,
    *,
    as_tasks: bool = False,
) -> AsyncIterator[str]:
    from aura_graph.service import GraphRequest

    yield _sse("user", _msg_json(stored_user))
    yield _sse("plan", {"subqueries": subqueries})
    try:
        result = await c.graphs.execute(
            GraphRequest(
                ctx.account_id,
                ctx.workspace_id,
                ctx.region,
                content,
                subqueries=tuple(subqueries),
                as_tasks=as_tasks,
            )
        )
    except AuraError as exc:
        yield _sse("error", exc.to_problem())
        return
    nodes = {n["id"]: n for n in result["trace"].get("nodes", [])}
    parts = [
        {
            "model": nodes.get(f"sq{i}", {}).get("model"),
            "cost_usd": nodes.get(f"sq{i}", {}).get("cost_usd", "0"),
            "status": nodes.get(f"sq{i}", {}).get("status"),
        }
        for i, q in enumerate(subqueries, 1)
    ]
    combine = nodes.get("answer", {})
    aura = {
        "kind": "subqueries",
        "execution_id": result["id"],
        "status": result["status"],
        "cost_usd": result["trace"].get("cost_usd", "0"),
        "subqueries": parts,
        "combine_model": combine.get("model"),
        "combine_cost_usd": combine.get("cost_usd", "0"),
        "models": sorted({p["model"] for p in parts if p["model"]}),
        "notes": [
            f"split into {len(subqueries)} sub-questions, answered in parallel",
            *(result["trace"].get("warnings") or []),
        ],
    }
    text = str(result["output"] or "")
    # The combined answer arrives whole; send it as a few chunks so the UI renders it
    # the same way as a streamed answer.
    for i in range(0, len(text), 400):
        yield _sse("delta", {"text": text[i : i + 400]})
    # Sub-question text is user content: it goes in the encrypted trace, not in aura.
    saved = await c.conversations.append(
        ctx.account_id,
        user,
        conv_id,
        role="assistant",
        content=text,
        aura=aura,
        trace={"subqueries": subqueries},
    )
    yield _sse("done", _msg_json(saved))


async def _agent_turn(
    c: RuntimeContainer,
    ctx: ExecutionContext,
    user: str,
    conv_id: str,
    prior: list[MessageView],
    content: str,
    mode: str,
    model: str | None,
    stored_user: MessageView,
) -> AsyncIterator[str]:
    """Drafter ↔ Reviewer → Finaliser for every message. Progress streams as ``agent``
    events; the reviewed tree is stored encrypted with the answer."""
    from aura_graph.service import GraphRequest

    yield _sse("user", _msg_json(stored_user))
    yield _sse("agent", {"type": "splitting"})
    resolved = await c.policies.resolve(ctx.account_id, workload=None, sticky_key=conv_id)
    policy = resolved.policy or ExecutionPolicy()
    try:
        subqueries = await c.graphs.decompose(
            ctx.account_id,
            content,
            policy=policy,
            always=True,
            as_tasks=c.settings.subquery_as_tasks,
        )
    except AuraError:
        subqueries = []  # the question itself becomes the one sub-query
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(
        c.graphs.run_agents(
            GraphRequest(ctx.account_id, ctx.workspace_id, ctx.region, content, policy=policy),
            subqueries=subqueries,
            mode=mode,
            drafter_model=model,
            on_event=queue.put_nowait,
            history=_history_text(prior),
            memory=await _personal_memory(c, ctx, content),
        )
    )
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield _sse("agent", getter.result())
                continue
            getter.cancel()
            while not queue.empty():
                yield _sse("agent", queue.get_nowait())
            break
        result = task.result()
    except AuraError as exc:
        yield _sse("error", exc.to_problem())
        return
    finally:
        task.cancel()  # Stop (or a failure) ends the whole tree
    for i in range(0, len(result.answer), 400):
        yield _sse("delta", {"text": result.answer[i : i + 400]})
    saved = await c.conversations.append(
        ctx.account_id,
        user,
        conv_id,
        role="assistant",
        content=result.answer,
        aura=result.summary(),
        trace={"version": 1, "tree": [n.to_trace() for n in result.root.children]},
    )
    yield _sse("done", _msg_json(saved))


def _history_text(prior: list[MessageView], budget: int = 6_000) -> str | None:
    """The latest turns as plain text for the agents' context (newest kept)."""
    lines: list[str] = []
    used = 0
    for m in reversed(prior):
        line = f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}"
        used += len(line)
        if used > budget and lines:
            break
        lines.append(line[:budget])
    return "\n\n".join(reversed(lines)) or None


async def _personal_memory(c: RuntimeContainer, ctx: ExecutionContext, query: str) -> list[str]:
    """The user's saved preferences (personal memory), when their plan includes it."""
    if ctx.user_id is None:
        return []
    ents = await c.entitlements.get(ctx.account_id)
    if not ents.allows("personal_memory"):
        return []
    try:
        hits = await c.long_memory.search(
            ctx.account_id, scopes=[("user", ctx.user_id)], query=query, k=5
        )
    except Exception:
        log.warning("personal memory lookup failed", exc_info=True)
        return []
    return [h.content for h in hits]
