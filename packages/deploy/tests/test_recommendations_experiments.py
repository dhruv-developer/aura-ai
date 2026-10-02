from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aura_deploy import CanaryConfig, CanaryMonitor, VariantStats, recommend
from aura_providers.domain.catalog import load_catalog
from aura_shared.errors import Conflict, ValidationFailed
from aura_xray.domain.parsers import parse
from aura_xray.domain.records import PriceBook
from aura_xray.domain.report import build_report

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "xray_sample.jsonl"


@pytest.fixture(scope="module")
def report() -> dict:  # type: ignore[type-arg]
    return build_report(parse(FIXTURE.read_bytes(), "jsonl").records, PriceBook(load_catalog()))


def test_recommendations_map_findings_to_actions(report) -> None:  # type: ignore[no-untyped-def]
    recs = recommend(report, [])
    by_kind = {r["kind"]: r for r in recs}
    assert by_kind["model_downgrade"]["type"] == "run_shadow"  # no evidence yet
    ctx = by_kind["context_trimming"]
    assert ctx["type"] == "policy" and ctx["spec"]["context_mode"] == "auto"
    assert ctx["spec"]["context_token_budget"] >= 1024
    assert by_kind["prompt_caching"]["type"] == "advice"

    support_key = by_kind["model_downgrade"]["workload_key"]
    shadow = [
        {
            "id": "shd_1",
            "status": "completed",
            "config": {"workload_key": support_key},
            "results": {
                "candidates": [
                    {
                        "model": "anthropic/claude-haiku-4-5",
                        "deploy_recommended": True,
                        "projection": {"monthly_savings_usd": "7.20"},
                    }
                ]
            },
        }
    ]
    proven = {r["kind"]: r for r in recommend(report, shadow)}["model_downgrade"]
    assert proven["type"] == "policy"
    assert proven["spec"] == {"allowed_models": ["anthropic/claude-haiku-4-5"]}
    assert proven["evidence"]["shadow_run_id"] == "shd_1"


async def _growth_org(container):  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("ops@acme.io")
    org = await container.identity.create_organization(user.id, "Acme")
    await container.subscriptions.change_plan(
        account_id=org.account_id, plan_code="growth", actor_type="system", actor_id="t", reason="t"
    )
    return user, org


async def test_apply_recommendation_creates_draft(container) -> None:  # type: ignore[no-untyped-def]
    user, org = await _growth_org(container)
    view = await container.xray.run_upload(
        account_id=org.account_id,
        data=FIXTURE.read_bytes(),
        fmt="jsonl",
        name="x",
        actor_user_id=user.id,
    )
    recs = await container.recommendations.list(org.account_id, view.id)
    idx = next(i for i, r in enumerate(recs) if r["kind"] == "context_trimming")
    applied = await container.recommendations.apply(
        org.account_id, xray_report_id=view.id, index=idx, actor_id=user.id
    )
    [pol] = await container.policies.list(org.account_id)
    assert pol["scope_ref"] == applied["workload"]
    assert (
        pol["versions"][0]["status"] == "draft"
        and pol["versions"][0]["spec"]["context_mode"] == "auto"
    )
    again = await container.recommendations.apply(
        org.account_id, xray_report_id=view.id, index=idx, actor_id=user.id
    )
    assert again["policy_id"] == applied["policy_id"]  # adds a version, not a duplicate policy
    advice_idx = next(i for i, r in enumerate(recs) if r["type"] == "advice")
    with pytest.raises(ValidationFailed):
        await container.recommendations.apply(
            org.account_id, xray_report_id=view.id, index=advice_idx, actor_id=user.id
        )


async def test_experiment_holds_split_until_concluded(container, redis_client) -> None:  # type: ignore[no-untyped-def]
    user, org = await _growth_org(container)
    acct = org.account_id
    created = await container.policies.create_policy(
        account_id=acct,
        name="exp",
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
        force_reason="A",
    )
    b = await container.policies.add_version(
        account_id=acct,
        policy_id=created["policy_id"],
        spec={"allowed_models": ["stub/medium"]},
        note="B",
        actor_id=user.id,
    )
    with pytest.raises(ValidationFailed):
        await container.policies.deploy(
            account_id=acct,
            policy_id=created["policy_id"],
            version_id=b,
            strategy="experiment",
            actor_id=user.id,
            force_reason="x",
            percent=33,
        )
    exp = await container.policies.deploy(
        account_id=acct,
        policy_id=created["policy_id"],
        version_id=b,
        strategy="experiment",
        actor_id=user.id,
        force_reason="A/B test",
        percent=20,
    )
    assert exp["mode"] == "experiment" and exp["percent"] == 20

    stats = VariantStats(redis_client)
    for i in range(200):
        await stats.record_execution(
            account_id=acct,
            execution_id=f"e{i}",
            deployment_id=exp["deployment_id"],
            variant="canary" if i % 5 == 0 else "stable",
            policy_version_id=None,
            ok=True,
            refused=False,
            cost=Decimal("0.001"),
            latency_ms=50,
        )
    monitor = CanaryMonitor(
        container.db,
        policies=container.policies,
        stats=stats,
        config=CanaryConfig(min_samples=10, min_dwell=timedelta(0)),
    )
    assert await monitor.run_once() == []  # experiments are never auto-advanced
    [pol] = await container.policies.list(acct)
    assert pol["deployments"][0]["percent"] == 20

    result = await container.policies.conclude_experiment(
        account_id=acct, deployment_id=exp["deployment_id"], promote=True, actor_id=user.id
    )
    assert result == {"status": "promoted"}
    [pol] = await container.policies.list(acct)
    assert pol["active_version_id"] == b
    with pytest.raises(Conflict):
        await container.policies.conclude_experiment(
            account_id=acct, deployment_id=exp["deployment_id"], promote=False, actor_id=user.id
        )
