"""Engine integration tests: real Redis + Postgres, stub models."""

from decimal import Decimal

import pytest

from aura_engine import EngineDelta, EngineDone, EngineError, ExecutionContext, ExecutionOptions
from aura_policy import ExecutionPolicy, QualityMode
from aura_providers.domain.types import ChatRequest, FinishReason, Message, ProviderErrorKind
from aura_shared.errors import DependencyUnavailable, QuotaExceeded, ValidationFailed
from aura_shared.events import ExecutionStatus


def _req(text: str = "hi there") -> ChatRequest:
    return ChatRequest((Message("user", text),))


async def _org_ctx(container) -> ExecutionContext:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("eng@acme.io")
    org = await container.identity.create_organization(user.id, "Acme")
    return ExecutionContext(org.account_id, org.workspace_id, "in", "b2b", "startup")


async def _personal_ctx(container) -> ExecutionContext:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("me@x.io")
    [acc] = await container.identity.list_access(user.id)
    ws = await container.identity.default_workspace_id(acc.account_id)
    return ExecutionContext(acc.account_id, ws, "in", "free", "free")


async def test_happy_path_routes_prices_meters_and_emits(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    run = await runtime.engine.prepare(ctx, _req(), ExecutionOptions(), stream=False)
    e = await runtime.engine.execute(run)
    assert e.spec.id == "stub/small" and e.result.text == "[small] hi there"
    assert e.cost_usd > 0 and e.aura_block()["routing"]["router_version"] == "rules-v1"
    assert await runtime.entitlements.usage(ctx.account_id, "executions") == (1, 2_000_000)
    [evt] = list(runtime.publisher.events)
    assert evt.status is ExecutionStatus.SUCCEEDED and evt.total_cost == e.cost_usd
    assert evt.routing.chosen_model == "stub/small" and evt.calls[0].model == "stub/small"


async def test_retryable_failure_falls_back(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    runtime.stub.fail_next("small", ProviderErrorKind.OVERLOADED)
    e = await runtime.engine.execute(
        await runtime.engine.prepare(ctx, _req(), ExecutionOptions(), stream=False)
    )
    assert e.spec.id != "stub/small"
    assert [a.error for a in e.attempts] == ["overloaded", None]
    [evt] = list(runtime.publisher.events)
    assert [c.error for c in evt.calls] == ["overloaded", None]


async def test_non_retryable_failure_releases_quota(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    runtime.stub.fail_next("small", ProviderErrorKind.BAD_REQUEST)
    with pytest.raises(ValidationFailed):
        await runtime.engine.execute(
            await runtime.engine.prepare(ctx, _req(), ExecutionOptions(), stream=False)
        )
    assert runtime.stub.calls == ["small"]  # no fallback for a bad request
    assert (await runtime.entitlements.usage(ctx.account_id, "executions"))[0] == 0
    [evt] = list(runtime.publisher.events)
    assert evt.status is ExecutionStatus.FAILED and evt.error_code == "execution_failed"


async def test_all_models_failing_is_dependency_unavailable(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    for m in ("small", "medium", "frontier"):
        runtime.stub.fail_next(m, ProviderErrorKind.UNAVAILABLE)
    with pytest.raises(DependencyUnavailable):
        await runtime.engine.execute(
            await runtime.engine.prepare(ctx, _req(), ExecutionOptions(), stream=False)
        )


async def test_circuit_opens_and_router_avoids_model(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    for _ in range(5):
        runtime.stub.fail_next("small", ProviderErrorKind.OVERLOADED)
        await runtime.engine.execute(
            await runtime.engine.prepare(ctx, _req(), ExecutionOptions(), stream=False)
        )
    run = await runtime.engine.prepare(ctx, _req(), ExecutionOptions(), stream=False)
    assert "stub/small" not in run.route.decision.candidates
    e = await runtime.engine.execute(run)
    assert e.attempts[0].error is None  # straight to a healthy model


async def test_quota_exhaustion_rejects_before_any_model_call(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _personal_ctx(container)
    await runtime.entitlements.reserve(ctx.account_id, "executions", amount=300, reservation_id="x")
    with pytest.raises(QuotaExceeded):
        await runtime.engine.prepare(ctx, _req(), ExecutionOptions(), stream=False)
    assert runtime.stub.calls == []
    [evt] = list(runtime.publisher.events)
    assert evt.status is ExecutionStatus.REJECTED and evt.error_code == "quota_exceeded"


async def test_premium_quota_exhausted_downgrades_with_note(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _personal_ctx(container)
    await runtime.entitlements.reserve(
        ctx.account_id, "premium_model_calls", amount=20, reservation_id="burn"
    )
    opts = ExecutionOptions(policy=ExecutionPolicy(quality=QualityMode.BEST))
    e = await runtime.engine.execute(await runtime.engine.prepare(ctx, _req(), opts, stream=False))
    assert e.spec.id != "stub/frontier"
    assert any("premium model quota exhausted" in n for n in e.notes)


async def test_premium_quota_counts_frontier_calls(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _personal_ctx(container)
    opts = ExecutionOptions(policy=ExecutionPolicy(quality=QualityMode.BEST))
    e = await runtime.engine.execute(await runtime.engine.prepare(ctx, _req(), opts, stream=False))
    assert e.spec.id == "stub/frontier"
    assert await runtime.entitlements.usage(ctx.account_id, "premium_model_calls") == (1, 20)


async def test_refusal_is_a_successful_execution_with_content_filter(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    e = await runtime.engine.execute(
        await runtime.engine.prepare(ctx, _req("[refuse] this"), ExecutionOptions(), stream=False)
    )
    assert e.result.finish_reason is FinishReason.CONTENT_FILTER


async def test_budget_policy_clamps_max_tokens(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    opts = ExecutionOptions(
        policy=ExecutionPolicy(max_cost_usd=Decimal("0.0001")), max_tokens=10_000
    )
    run = await runtime.engine.prepare(ctx, _req(), opts, stream=False)
    assert run.route.max_tokens[run.route.primary.id] < 10_000


# ---- streaming -------------------------------------------------------------------------


async def _collect(runtime, run):  # type: ignore[no-untyped-def]
    return [e async for e in runtime.engine.stream(run)]


async def test_stream_happy_path(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    events = await _collect(
        runtime, await runtime.engine.prepare(ctx, _req("one two"), ExecutionOptions(), stream=True)
    )
    text = "".join(e.text for e in events if isinstance(e, EngineDelta))
    assert text == "[small] one two" and isinstance(events[-1], EngineDone)
    assert (await runtime.entitlements.usage(ctx.account_id, "executions"))[0] == 1


async def test_stream_falls_back_before_first_token(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    runtime.stub.fail_next("small", ProviderErrorKind.RATE_LIMITED)
    events = await _collect(
        runtime, await runtime.engine.prepare(ctx, _req(), ExecutionOptions(), stream=True)
    )
    done = events[-1]
    assert isinstance(done, EngineDone) and done.execution.spec.id != "stub/small"


async def test_stream_failure_after_first_token_ends_with_error(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    runtime.stub.fail_mid_stream("small")
    events = await _collect(
        runtime, await runtime.engine.prepare(ctx, _req("a b c"), ExecutionOptions(), stream=True)
    )
    assert isinstance(events[0], EngineDelta) and isinstance(events[-1], EngineError)
    assert runtime.stub.calls == ["small"]  # no fallback once tokens were sent
    assert (await runtime.entitlements.usage(ctx.account_id, "executions"))[0] == 0


async def test_stream_abandoned_by_client_releases_quota(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    gen = runtime.engine.stream(
        await runtime.engine.prepare(ctx, _req("a b c d"), ExecutionOptions(), stream=True)
    )
    assert isinstance(await anext(gen), EngineDelta)
    await gen.aclose()  # what Starlette does when the client disconnects
    assert (await runtime.entitlements.usage(ctx.account_id, "executions"))[0] == 0
    assert list(runtime.publisher.events)[-1].error_code == "stream_closed_early"


# ---- session memory ----------------------------------------------------------------------


async def test_session_memory_carries_context_across_requests(container, runtime) -> None:  # type: ignore[no-untyped-def]
    ctx = await _org_ctx(container)
    opts = ExecutionOptions(session_id="chat-1")
    first = await runtime.engine.execute(
        await runtime.engine.prepare(ctx, _req("my name is Ada"), opts, stream=False)
    )
    assert not any("session memory" in n for n in first.notes)
    run = await runtime.engine.prepare(ctx, _req("what is my name?"), opts, stream=False)
    assert [m.content for m in run.req.messages] == [
        "my name is Ada",
        "[small] my name is Ada",
        "what is my name?",
    ]
    second = await runtime.engine.execute(run)
    assert any("session memory: 2/2 turns" in n for n in second.notes)

    # A failed execution does not pollute the session.
    runtime.stub.fail_next("small", ProviderErrorKind.BAD_REQUEST)
    with pytest.raises(ValidationFailed):
        await runtime.engine.execute(
            await runtime.engine.prepare(ctx, _req("bad turn"), opts, stream=False)
        )
    run = await runtime.engine.prepare(ctx, _req("again"), opts, stream=False)
    assert "bad turn" not in [m.content for m in run.req.messages]

    # Sessions are per tenant.
    other = await _personal_ctx(container)
    run = await runtime.engine.prepare(other, _req("hi"), opts, stream=False)
    assert [m.content for m in run.req.messages] == ["hi"]


async def test_quota_store_outage_fails_open_for_b2b_closed_for_consumers(
    container, runtime, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise RedisConnectionError("redis down")

    org, personal = await _org_ctx(container), await _personal_ctx(container)
    monkeypatch.setattr(runtime.entitlements, "reserve", boom)
    e = await runtime.engine.execute(
        await runtime.engine.prepare(org, _req(), ExecutionOptions(), stream=False)
    )
    assert "quota check skipped: quota store unavailable" in e.notes
    with pytest.raises(DependencyUnavailable):
        await runtime.engine.prepare(personal, _req(), ExecutionOptions(), stream=False)
