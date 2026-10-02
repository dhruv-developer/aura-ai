from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile

from aura_api.deps import ContainerDep, UserDep, account_access
from aura_auth.application.dto import AccountAccess
from aura_auth.domain.rbac import Permission
from aura_shared.errors import ValidationFailed

router = APIRouter(prefix="/v1", tags=["shadow"])

WorkloadsRead = Annotated[AccountAccess, Depends(account_access(Permission.WORKLOADS_READ))]
WorkloadsManage = Annotated[AccountAccess, Depends(account_access(Permission.WORKLOADS_MANAGE))]


def _out(v: Any) -> dict[str, Any]:
    return {
        "id": v.id,
        "name": v.name,
        "status": v.status,
        "config": v.config,
        "results": v.results,
        "spent_usd": str(v.spent_usd),
        "error": v.error,
        "created_at": v.created_at,
    }


@router.post("/accounts/{account_id}/shadow-runs", status_code=202)
async def create_shadow_run(
    access: WorkloadsManage,
    user: UserDep,
    c: ContainerDep,
    file: Annotated[UploadFile, File()],
    candidates: Annotated[str, Form(description="comma-separated model ids")],
    format: Annotated[str, Form()] = "jsonl",
    workload_key: Annotated[str | None, Form()] = None,
    sample_size: Annotated[int, Form()] = 100,
    budget_usd: Annotated[str, Form()] = "5",
    judge_model: Annotated[str | None, Form()] = None,
    quality_target: Annotated[float, Form(ge=0.5, le=1.0)] = 0.9,
    name: Annotated[str, Form()] = "",
) -> dict[str, Any]:
    try:
        budget = Decimal(budget_usd)
    except InvalidOperation as exc:
        raise ValidationFailed("budget_usd must be a number") from exc
    view = await c.shadow.create(
        account_id=access.account_id,
        data=await file.read(),
        fmt=format,
        candidates=[x.strip() for x in candidates.split(",") if x.strip()],
        actor_user_id=user.id,
        workload_key=workload_key or None,
        sample_size=sample_size,
        budget_usd=budget,
        judge_model=judge_model or None,
        quality_target=quality_target,
        name=name,
    )
    return _out(view)


@router.get("/accounts/{account_id}/shadow-runs")
async def list_shadow_runs(access: WorkloadsRead, c: ContainerDep) -> list[dict[str, Any]]:
    return [_out(v) for v in await c.shadow.list(access.account_id)]


@router.get("/accounts/{account_id}/shadow-runs/{run_id}")
async def get_shadow_run(run_id: str, access: WorkloadsRead, c: ContainerDep) -> dict[str, Any]:
    return _out(await c.shadow.get(access.account_id, run_id))
