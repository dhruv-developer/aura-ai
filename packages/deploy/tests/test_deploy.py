from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aura_deploy import CanaryConfig, CanaryMonitor, VariantStats
from aura_deploy.assignment import variant
from aura_deploy.canary import assess
from aura_deploy.service import check_evidence
from aura_deploy.stats import VariantSnapshot
from aura_shared.errors import Conflict, Forbidden, NotFound, ValidationFailed

# ---- pure -------------------------------------------------------------------------------


def test_assignment_is_sticky_and_monotonic() -> None:
    keys = [f"user-{i}" for i in range(5000)]
    at = {p: {k for k in keys if variant("dep_1", k, p) == "canary"} for p in (1, 5, 25, 50, 100)}
    assert at[1] <= at[5] <= at[25] <= at[50] <= at[100] == set(keys)  # only ever grows
    assert 0.03 < len(at[5]) / len(keys) < 0.07  # roughly the requested share
    assert variant("dep_1", "user-1", 25) == variant("dep_1", "user-1", 25)  # sticky
    assert {variant(f"dep_{i}", "u", 50) for i in range(40)} == {
        "canary",
        "stable",
    }  # per-deployment


def _snap(n=100, errors=0, cost=100_000, lat=100_000, ok=0, fail=0) -> VariantSnapshot:  # type: ignore[no-untyped-def]
    return VariantSnapshot(n, errors, 0, cost, lat, ok, fail)


def test_assess_rules() -> None:
    cfg = CanaryConfig(min_samples=50)
    assert assess(_snap(n=10), _snap(), cfg)[0] == "wait"
    assert assess(_snap(), _snap(), cfg)[0] == "advance"
    assert assess(_snap(errors=10), _snap(errors=1), cfg)[0] == "rollback"
    assert assess(_snap(lat=200_000), _snap(lat=100_000), cfg)[0] == "rollback"
    assert assess(_snap(cost=150_000), _snap(cost=100_000), cfg)[0] == "rollback"
    worse_outcomes = assess(_snap(ok=60, fail=40), _snap(ok=90, fail=10), cfg)
    assert worse_outcomes[0] == "rollback" and "outcome" in worse_outcomes[1][0]
    assert _snap(n=4, cost=2_000_000, ok=2).cost_per_outcome == 1.0


def test_evidence_requires_policy_pinned_to_proven_models() -> None:
    run = SimpleNamespace(
        id="shd_1",
        status="completed",
        results={
            "candidates": [
                {"model": "stub/small", "deploy_recommended": True},
                {"model": "stub/medium", "deploy_recommended": False},
            ]
        },
    )
    assert check_evidence({"allowed_models": ["stub/small"]}, run).ok
    assert not check_evidence({"allowed_models": ["stub/small", "stub/medium"]}, run).ok
    assert not check_evidence({}, run).ok  # an unpinned policy proves nothing
    assert not check_evidence({"allowed_models": ["stub/small"]}, None).ok
    pending = SimpleNamespace(id="x", status="running", results={})
    assert not check_evidence({"allowed_models": ["stub/small"]}, pending).ok


# ---- integration --------------------------------------------------------------------------


async def _org(container, plan: str = "growth"):  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("ops@acme.io")
    org = await container.identity.create_organization(user.id, "Acme")
    await container.subscriptions.change_plan(
        account_id=org.account_id, plan_code=plan, actor_type="system", actor_id="t", reason="test"
    )
    return user, org


async def test_gate_blocks_unproven_and_forced_activation_is_enforced(container, runtime) -> None:  # type: ignore[no-untyped-def]
    from aura_engine import ExecutionContext, ExecutionOptions
    from aura_providers.domain.types import ChatRequest, Message

    user, org = await _org(container, "startup")
    created = await container.policies.create_policy(
        account_id=org.account_id,
        name="support",
        scope="workload",
        scope_ref="support",
        spec={"allowed_models": ["stub/small"], "max_latency_ms": 5000},
        actor_id=user.id,
    )
    pid, vid = created["policy_id"], created["version_id"]
    with pytest.raises(Forbidden, match="safety gate"):
        await container.policies.deploy(
            account_id=org.account_id,
            policy_id=pid,
            version_id=vid,
            strategy="immediate",
            actor_id=user.id,
        )
    with pytest.raises(Forbidden):  # startup plan has no canary_rollout
        await container.policies.deploy(
            account_id=org.account_id,
            policy_id=pid,
            version_id=vid,
            strategy="canary",
            actor_id=user.id,
            force_reason="x",
        )
    await container.policies.deploy(
        account_id=org.account_id,
        policy_id=pid,
        version_id=vid,
        strategy="immediate",
        actor_id=user.id,
        force_reason="customer-authored constraint",
    )
    with pytest.raises(Conflict):  # versions are immutable once active
        await container.policies.deploy(
            account_id=org.account_id,
            policy_id=pid,
            version_id=vid,
            strategy="immediate",
            actor_id=user.id,
            force_reason="x",
        )

    r = await runtime.policies.resolve(org.account_id, workload="support", sticky_key="u1")
    assert r.policy is not None and r.policy.allowed_models == {"stub/small"}
    assert r.policy_version_id == vid and r.deployment_id is None
    assert (
        await runtime.policies.resolve(org.account_id, workload="other", sticky_key="u")
    ).policy is None

    ctx = ExecutionContext(org.account_id, org.workspace_id, "in", "b2b", "startup")
    from aura_policy import ExecutionPolicy, QualityMode

    opts = ExecutionOptions(
        policy=ExecutionPolicy(quality=QualityMode.BEST),
        account_policy=r.policy,
        policy_id=r.policy_id,
        policy_version_id=r.policy_version_id,
    )
    e = await runtime.engine.execute(
        await runtime.engine.prepare(ctx, ChatRequest((Message("user", "hi"),)), opts, stream=False)
    )
    assert e.spec.id == "stub/small"  # the workload policy wins over the request's "best"
    assert list(runtime.publisher.events)[-1].policy_version_id == vid

    with pytest.raises(ValidationFailed):
        await container.policies.create_policy(
            account_id=org.account_id,
            name="x",
            scope="workload",
            scope_ref="",
            spec={},
            actor_id=user.id,
        )
    with pytest.raises(ValidationFailed):
        await container.policies.add_version(
            account_id=org.account_id,
            policy_id=pid,
            spec={"max_cost": 1},
            note="",
            actor_id=user.id,
        )


async def test_canary_advances_then_rolls_back_on_regression(
    container, runtime, redis_client
) -> None:  # type: ignore[no-untyped-def]
    user, org = await _org(container)
    acct = org.account_id
    created = await container.policies.create_policy(
        account_id=acct,
        name="support",
        scope="workload",
        scope_ref="support",
        spec={"allowed_models": ["stub/small"]},
        actor_id=user.id,
    )
    await container.policies.deploy(
        account_id=acct,
        policy_id=created["policy_id"],
        version_id=created["version_id"],
        strategy="immediate",
        actor_id=user.id,
        force_reason="baseline",
    )
    v2 = await container.policies.add_version(
        account_id=acct,
        policy_id=created["policy_id"],
        spec={"allowed_models": ["stub/medium"]},
        note="try",
        actor_id=user.id,
    )
    dep = await container.policies.deploy(
        account_id=acct,
        policy_id=created["policy_id"],
        version_id=v2,
        strategy="canary",
        actor_id=user.id,
        force_reason="test canary",
    )
    assert dep["status"] == "canary" and dep["percent"] == 1
    did = dep["deployment_id"]

    # Resolution splits traffic: find a canary and a stable user at 1%.
    seen = {}
    for i in range(3000):
        r = await runtime.policies.resolve(acct, workload="support", sticky_key=f"user-{i}")
        seen.setdefault(r.variant, r)
        if len(seen) == 2:
            break
    assert seen["canary"].policy.allowed_models == {"stub/medium"}
    assert seen["stable"].policy.allowed_models == {"stub/small"}

    stats = VariantStats(redis_client)
    monitor = CanaryMonitor(
        container.db,
        policies=container.policies,
        stats=stats,
        config=CanaryConfig(min_samples=20, min_dwell=timedelta(0)),
    )

    async def traffic(variant_name: str, n: int, errors: int = 0) -> None:
        for i in range(n):
            await stats.record_execution(
                account_id=acct,
                execution_id=f"exe_{variant_name}_{i}_{n}_{errors}",
                deployment_id=did,
                variant=variant_name,
                policy_version_id=None,
                ok=i >= errors,
                refused=False,
                cost=Decimal("0.001"),
                latency_ms=100,
            )

    await traffic("stable", 100)
    await traffic("canary", 5)
    assert (await monitor.run_once())[0]["decision"] == "wait"
    await traffic("canary", 30)
    action = (await monitor.run_once())[0]
    assert action["decision"] == "advance"
    listed = await container.policies.list(acct)
    assert listed[0]["deployments"][0]["percent"] == 5

    await traffic("canary", 40, errors=20)  # the canary starts failing
    action = (await monitor.run_once())[0]
    assert action["decision"] == "rollback" and "error rate" in action["reasons"][0]
    listed = await container.policies.list(acct)
    assert listed[0]["deployments"][0]["status"] == "rolled_back"
    assert {v["status"] for v in listed[0]["versions"]} == {"active", "rolled_back"}

    # After invalidation every user is back on the stable version.
    await runtime.policies._cache.invalidate(acct, "snapshot")
    for i in range(50):
        r = await runtime.policies.resolve(acct, workload="support", sticky_key=f"user-{i}")
        assert r.variant is None and r.policy.allowed_models == {"stub/small"}


async def test_canary_completes_and_outcomes_are_recorded(container, runtime, redis_client) -> None:  # type: ignore[no-untyped-def]
    user, org = await _org(container)
    acct = org.account_id
    created = await container.policies.create_policy(
        account_id=acct,
        name="all",
        scope="account",
        scope_ref="",
        spec={"max_latency_ms": 10_000},
        actor_id=user.id,
    )
    dep = await container.policies.deploy(
        account_id=acct,
        policy_id=created["policy_id"],
        version_id=created["version_id"],
        strategy="canary",
        actor_id=user.id,
        force_reason="first policy",
    )
    for _ in range(6):  # 1 → 5 → 10 → 25 → 50 → 100 → completed
        result = await container.policies.advance(
            account_id=acct, deployment_id=dep["deployment_id"], actor=("user", user.id)
        )
    assert result["status"] == "completed"
    listed = await container.policies.list(acct)
    assert listed[0]["active_version_id"] == created["version_id"]

    stats = VariantStats(redis_client)
    await stats.record_execution(
        account_id=acct,
        execution_id="exe_1",
        deployment_id="dep_x",
        variant="canary",
        policy_version_id="polv_1",
        ok=True,
        refused=False,
        cost=Decimal("0.01"),
        latency_ms=10,
    )
    ref = await stats.record_outcome(acct, "exe_1", success=True)
    assert ref == {"deployment_id": "dep_x", "variant": "canary", "policy_version_id": "polv_1"}
    assert (await stats.snapshot(acct, "dep_x", "canary")).outcomes_ok == 1
    with pytest.raises(NotFound):  # another tenant cannot report on this execution
        await stats.record_outcome("acct_other", "exe_1", success=True)
