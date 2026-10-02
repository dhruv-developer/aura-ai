from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from aura_api.deps import ContainerDep, UserDep, account_access
from aura_auth.application.dto import AccountAccess
from aura_auth.domain.rbac import Permission

router = APIRouter(prefix="/v1/accounts/{account_id}", tags=["policies"])

PoliciesRead = Annotated[AccountAccess, Depends(account_access(Permission.POLICIES_READ))]
PoliciesManage = Annotated[AccountAccess, Depends(account_access(Permission.POLICIES_MANAGE))]
DeploymentsManage = Annotated[AccountAccess, Depends(account_access(Permission.DEPLOYMENTS_MANAGE))]


class PolicyCreate(BaseModel):
    name: str = Field(default="", max_length=120)
    scope: str = "workload"
    scope_ref: str = Field(default="", max_length=120)
    spec: dict[str, Any] = Field(default_factory=dict)


class VersionCreate(BaseModel):
    spec: dict[str, Any]
    note: str = Field(default="", max_length=500)


class DeployIn(BaseModel):
    strategy: str = "canary"
    percent: int | None = None
    shadow_run_id: str | None = None
    force_reason: str | None = Field(default=None, max_length=500)


class RollbackIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.get("/policies")
async def list_policies(access: PoliciesRead, c: ContainerDep) -> list[dict[str, Any]]:
    return await c.policies.list(access.account_id)


@router.post("/policies", status_code=201)
async def create_policy(
    body: PolicyCreate, access: PoliciesManage, user: UserDep, c: ContainerDep
) -> dict[str, Any]:
    return await c.policies.create_policy(
        account_id=access.account_id,
        name=body.name,
        scope=body.scope,
        scope_ref=body.scope_ref,
        spec=body.spec,
        actor_id=user.id,
    )


@router.post("/policies/{policy_id}/versions", status_code=201)
async def add_version(
    policy_id: str, body: VersionCreate, access: PoliciesManage, user: UserDep, c: ContainerDep
) -> dict[str, str]:
    vid = await c.policies.add_version(
        account_id=access.account_id,
        policy_id=policy_id,
        spec=body.spec,
        note=body.note,
        actor_id=user.id,
    )
    return {"version_id": vid}


@router.post("/policies/{policy_id}/versions/{version_id}/deploy")
async def deploy(
    policy_id: str,
    version_id: str,
    body: DeployIn,
    access: DeploymentsManage,
    user: UserDep,
    c: ContainerDep,
) -> dict[str, Any]:
    return await c.policies.deploy(
        account_id=access.account_id,
        policy_id=policy_id,
        version_id=version_id,
        strategy=body.strategy,
        actor_id=user.id,
        shadow_run_id=body.shadow_run_id,
        force_reason=body.force_reason,
        percent=body.percent,
    )


@router.post("/deployments/{deployment_id}/rollback", status_code=204)
async def rollback(
    deployment_id: str, body: RollbackIn, access: DeploymentsManage, user: UserDep, c: ContainerDep
) -> None:
    await c.policies.rollback(
        account_id=access.account_id,
        deployment_id=deployment_id,
        reason=body.reason,
        actor=("user", user.id),
    )


class ExperimentConclude(BaseModel):
    promote: bool


class ApplyRecommendation(BaseModel):
    xray_report_id: str
    index: int = Field(ge=0)


@router.get("/deployments/{deployment_id}/stats")
async def deployment_stats(
    deployment_id: str, access: PoliciesRead, c: ContainerDep
) -> dict[str, Any]:
    out = {}
    for variant in ("canary", "stable"):
        snap = await c.variant_stats.snapshot(access.account_id, deployment_id, variant)
        out[variant] = {
            "executions": snap.n,
            "error_rate": round(snap.error_rate, 4),
            "avg_cost_usd": round(snap.avg_cost, 8),
            "avg_latency_ms": round(snap.avg_latency_ms, 1),
            "outcomes": snap.outcomes,
            "success_rate": round(snap.success_rate, 4),
            "cost_per_outcome_usd": snap.cost_per_outcome,
        }
    return out


@router.post("/deployments/{deployment_id}/conclude")
async def conclude(
    deployment_id: str,
    body: ExperimentConclude,
    access: DeploymentsManage,
    user: UserDep,
    c: ContainerDep,
) -> dict[str, Any]:
    return await c.policies.conclude_experiment(
        account_id=access.account_id,
        deployment_id=deployment_id,
        promote=body.promote,
        actor_id=user.id,
    )


@router.get("/recommendations")
async def recommendations(
    xray_report_id: str, access: PoliciesRead, c: ContainerDep
) -> list[dict[str, Any]]:
    return await c.recommendations.list(access.account_id, xray_report_id)


@router.post("/recommendations/apply", status_code=201)
async def apply_recommendation(
    body: ApplyRecommendation, access: PoliciesManage, user: UserDep, c: ContainerDep
) -> dict[str, Any]:
    return await c.recommendations.apply(
        access.account_id, xray_report_id=body.xray_report_id, index=body.index, actor_id=user.id
    )
