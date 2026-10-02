"""V4: learned routing inside hard constraints, OPE logs, consent-gated priors."""

import random

from aura_engine import ExecutionContext, ExecutionOptions
from aura_engine.learning import LearnedSelector, aggregate_prior
from aura_providers.domain.types import ChatRequest, Message, ProviderErrorKind


async def _growth_ctx(container, email="ml@acme.io") -> ExecutionContext:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    org = await container.identity.create_organization(user.id, "Acme")
    await container.subscriptions.change_plan(
        account_id=org.account_id, plan_code="growth", actor_type="system", actor_id="t", reason="t"
    )
    return ExecutionContext(org.account_id, org.workspace_id, "in", "b2b", "growth")


async def test_bandit_learns_to_avoid_a_failing_model(container, runtime) -> None:  # type: ignore[no-untyped-def]
    from aura_runtime.observers import LearningObserver

    learner = LearnedSelector(runtime.scripts._r, epsilon=0.2, rng=random.Random(3))
    runtime.engine._learner = learner
    runtime.engine._observers = [LearningObserver(learner)]
    ctx = await _growth_ctx(container)
    runtime.stub.fail_next("small", ProviderErrorKind.OVERLOADED, times=10_000)  # small is down
    req = ChatRequest((Message("user", "hello there"),))
    chosen = []
    for _ in range(80):
        run = await runtime.engine.prepare(
            ctx, req, ExecutionOptions(workload="chat"), stream=False
        )
        chosen.append(run.route.primary.id)
        await runtime.engine.execute(run)
    assert chosen[0] == "stub/small"  # untrained: ties keep the rules router's (cheapest) order
    # Learned: the small model fails, so it is chosen only by occasional exploration.
    assert chosen[-20:].count("stub/small") <= 5
    evt = list(runtime.publisher.events)[-1]
    assert evt.routing.router_version == "linucb-v1" and 0 < evt.routing.propensity <= 1
    logs = await learner.decisions(ctx.account_id, "chat")
    assert len(logs) == 80 and {d.action for d in logs} >= {"stub/small"}


async def test_explicit_models_and_non_entitled_plans_bypass_learning(container, runtime) -> None:  # type: ignore[no-untyped-def]
    runtime.engine._learner = LearnedSelector(runtime.scripts._r)
    ctx = await _growth_ctx(container)
    req = ChatRequest((Message("user", "hi"),))
    run = await runtime.engine.prepare(
        ctx, req, ExecutionOptions(model="stub/medium"), stream=False
    )
    assert run.learning is None and run.route.primary.id == "stub/medium"
    user = await container.identity.login_with_email("s@startup.io")
    org = await container.identity.create_organization(user.id, "Startup")  # no advanced_routing
    sctx = ExecutionContext(org.account_id, org.workspace_id, "in", "b2b", "startup")
    run = await runtime.engine.prepare(sctx, req, ExecutionOptions(), stream=False)
    assert run.learning is None and run.route.decision.router_version == "rules-v1"


async def test_priors_only_from_consenting_tenants(redis_client) -> None:  # type: ignore[no-untyped-def]
    learner = LearnedSelector(redis_client)
    from aura_engine.learning import LearnedChoice

    await learner.update("acct_consent", LearnedChoice("w", "stub/small", [1.0] * 8, 0.5), 0.9)
    await learner.update("acct_private", LearnedChoice("w", "stub/small", [1.0] * 8, 0.5), -5.0)
    prior = await aggregate_prior(redis_client, ["acct_consent"], env="test")
    assert prior == {"stub/small": {"mean": 0.9, "n": 1}}  # the private tenant contributed nothing
    await learner.publish_prior(prior)
    fresh = await learner.bandit("acct_new", "w")
    assert fresh.theta("stub/small")[0] > 0  # warm-started from the aggregate


async def test_ope_endpoint(container, runtime) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from aura_runtime.app import create_app

    ctx = await _growth_ctx(container, "ope@acme.io")
    learner = runtime.learner
    from aura_engine.learning import LearnedChoice

    rng = random.Random(5)
    for _ in range(60):
        arm = rng.choice(["stub/small", "stub/medium"])
        await learner.update(
            ctx.account_id,
            LearnedChoice("chat", arm, [1.0] + [0.0] * 7, 0.5),
            0.9 if arm == "stub/small" else 0.4,
        )
    user = await container.identity.login_with_email("ope@acme.io")
    access = await container.identity.resolve_access(user.id, ctx.account_id)
    _, key = await container.api_keys.issue(
        account_id=ctx.account_id,
        actor_user_id=user.id,
        actor_permissions=access.permissions,
        name="k",
        permissions={"runtime:execute"},
        environment_id=None,
    )
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rt") as c,
    ):
        r = await c.post(
            "/v1/learning/evaluate",
            headers={"authorization": f"Bearer {key}"},
            json={"target": "always:stub/small", "workload": "chat"},
        )
        assert r.status_code == 200, r.text
        est = r.json()["estimates"]
        assert abs(est["snips"] - 0.9) < 0.05  # always-small would have scored ~0.9
        assert r.json()["logging_policy_value"] < est["snips"]
        bad = await c.post(
            "/v1/learning/evaluate",
            headers={"authorization": f"Bearer {key}"},
            json={"target": "rm -rf"},
        )
        assert bad.status_code == 422
