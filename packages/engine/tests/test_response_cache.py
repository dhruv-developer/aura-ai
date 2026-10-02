"""Exact + semantic response caching against real Redis 8 vector sets."""

import pytest

from aura_engine import EngineDelta, EngineDone, ExecutionContext, ExecutionOptions
from aura_policy import CacheMode, ExecutionPolicy
from aura_providers.domain.types import ChatRequest, Message
from aura_providers.embeddings import HashingEmbedder
from aura_shared.events import ExecutionStatus


def _req(*msgs: tuple[str, str]) -> ChatRequest:
    return ChatRequest(tuple(Message(r, c) for r, c in msgs))  # type: ignore[arg-type]


async def _ctx(container, email="c@acme.io", org=True) -> ExecutionContext:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    if org:
        o = await container.identity.create_organization(user.id, "Acme")
        return ExecutionContext(o.account_id, o.workspace_id, "in", "b2b", "startup")
    [acc] = await container.identity.list_access(user.id)
    ws = await container.identity.default_workspace_id(acc.account_id)
    return ExecutionContext(acc.account_id, ws, "in", "free", "free")


async def _run(runtime, ctx, req, mode=CacheMode.EXACT, **kw):  # type: ignore[no-untyped-def]
    opts = ExecutionOptions(policy=ExecutionPolicy(cache_mode=mode, **kw))
    return await runtime.engine.execute(await runtime.engine.prepare(ctx, req, opts, stream=False))


async def test_off_by_default(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _ctx(container)
    req = _req(("user", "hello"))
    for _ in range(2):
        await runtime.engine.execute(
            await runtime.engine.prepare(ctx, req, ExecutionOptions(), stream=False)
        )
    assert runtime.stub.calls == ["small", "small"]


async def test_exact_hit_costs_nothing_counts_as_execution(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _ctx(container)
    req = _req(("system", "Be brief."), ("user", "What is AURA?"))
    first = await _run(runtime, ctx, req)
    second = await _run(runtime, ctx, req)
    assert runtime.stub.calls == ["small"]  # the model ran once
    assert second.result.text == first.result.text and second.cost_usd == 0
    assert any("served from exact cache" in n for n in second.notes)
    evt = list(runtime.publisher.events)[-1]
    assert evt.status is ExecutionStatus.CACHED and evt.cache_hit == "exact"
    assert evt.cost_saved == first.cost_usd
    assert (await runtime.entitlements.usage(ctx.account_id, "executions"))[0] == 2
    different = await _run(runtime, ctx, _req(("system", "Be brief."), ("user", "What is AURA??")))
    assert different.cost_usd > 0  # exact means exact


async def test_semantic_hit_for_same_question_different_formatting(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _ctx(container)
    await _run(runtime, ctx, _req(("user", "How do I reset my password?")), CacheMode.SEMANTIC)
    hit = await _run(runtime, ctx, _req(("user", "how do i RESET my password")), CacheMode.SEMANTIC)
    assert hit.cost_usd == 0 and any("semantic cache" in n for n in hit.notes)
    miss = await _run(
        runtime, ctx, _req(("user", "What are your opening hours?")), CacheMode.SEMANTIC
    )
    assert miss.cost_usd > 0
    # A different system prompt is a different question even with the same words.
    other_system = await _run(
        runtime,
        ctx,
        _req(("system", "You are a pirate."), ("user", "How do I reset my password?")),
        CacheMode.SEMANTIC,
    )
    assert other_system.cost_usd > 0


async def test_semantic_never_matches_multi_turn_or_other_tenants(container, runtime) -> None:  # type: ignore[no-untyped-def]
    a = await _ctx(container, "a@acme.io")
    b = await _ctx(container, "b@other.io")
    await _run(runtime, a, _req(("user", "reset my password")), CacheMode.SEMANTIC)
    other_tenant = await _run(runtime, b, _req(("user", "reset my password")), CacheMode.SEMANTIC)
    assert other_tenant.cost_usd > 0
    convo = _req(("user", "hi"), ("assistant", "hello"), ("user", "reset my password"))
    assert (await _run(runtime, a, convo, CacheMode.SEMANTIC)).cost_usd > 0


async def test_semantic_requires_entitlement_and_refusals_are_not_cached(
    container, runtime
) -> None:  # type: ignore[no-untyped-def]
    free = await _ctx(container, "f@x.io", org=False)
    e = await _run(runtime, free, _req(("user", "q")), CacheMode.SEMANTIC)
    assert "semantic cache not in plan: using exact cache only" in e.notes
    org = await _ctx(container, "r@acme.io")
    req = _req(("user", "[refuse] please"))
    await _run(runtime, org, req)
    await _run(runtime, org, req)
    assert runtime.stub.calls.count("small") == 3  # free q + two uncached refusals


async def test_streaming_serves_cache_hits(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _ctx(container)
    req = _req(("user", "stream me"))
    await _run(runtime, ctx, req)
    opts = ExecutionOptions(policy=ExecutionPolicy(cache_mode=CacheMode.EXACT))
    events = [
        e
        async for e in runtime.engine.stream(
            await runtime.engine.prepare(ctx, req, opts, stream=True)
        )
    ]
    assert isinstance(events[0], EngineDelta) and isinstance(events[-1], EngineDone)
    assert events[-1].execution.cost_usd == 0 and runtime.stub.calls == ["small"]


async def test_cache_backend_failure_degrades_to_execution(container, runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from redis.exceptions import ConnectionError as RedisConnectionError

    ctx = await _ctx(container)

    async def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise RedisConnectionError("down")

    monkeypatch.setattr(runtime.engine._cache._r, "get", boom)
    monkeypatch.setattr(runtime.engine._cache._r, "set", boom)
    e = await _run(runtime, ctx, _req(("user", "x")))
    assert e.cost_usd > 0  # served by the model, no error


async def test_hashing_embedder_properties() -> None:
    emb = HashingEmbedder(dim=128)
    a, b, c = await emb.embed(["reset my password", "RESET my password!", "opening hours"])
    dot = lambda x, y: sum(p * q for p, q in zip(x, y, strict=True))  # noqa: E731
    assert dot(a, b) == pytest.approx(1.0) and dot(a, c) < 0.5
    assert dot(a, a) == pytest.approx(1.0)
