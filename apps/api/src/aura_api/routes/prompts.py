from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from aura_api.deps import ContainerDep, UserDep, account_access
from aura_auth.application.dto import AccountAccess
from aura_auth.domain.rbac import Permission

router = APIRouter(prefix="/v1/accounts/{account_id}", tags=["prompts"])

Read = Annotated[AccountAccess, Depends(account_access(Permission.POLICIES_READ))]
Manage = Annotated[AccountAccess, Depends(account_access(Permission.POLICIES_MANAGE))]


class PromptCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=100_000)


class VersionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class EvalItem(BaseModel):
    input: str = Field(min_length=1, max_length=50_000)
    vars: dict[str, str] = Field(default_factory=dict)
    reference: str | None = Field(default=None, max_length=50_000)


class OptimizeIn(BaseModel):
    model: str
    items: list[EvalItem] = Field(min_length=1, max_length=100)
    budget_usd: Decimal = Field(default=Decimal("2"), gt=0, le=1000)


@router.get("/prompts")
async def list_prompts(access: Read, c: ContainerDep) -> list[dict[str, Any]]:
    return await c.prompts.list(access.account_id)


@router.post("/prompts", status_code=201)
async def create_prompt(
    body: PromptCreate, access: Manage, user: UserDep, c: ContainerDep
) -> dict[str, str]:
    return await c.prompts.create(
        access.account_id, name=body.name, content=body.content, actor_id=user.id
    )


@router.post("/prompts/{template_id}/versions", status_code=201)
async def add_version(
    template_id: str, body: VersionCreate, access: Manage, user: UserDep, c: ContainerDep
) -> dict[str, str]:
    vid = await c.prompts.add_version(
        access.account_id, template_id=template_id, content=body.content, actor_id=user.id
    )
    return {"version_id": vid}


@router.post("/prompts/{template_id}/versions/{version_id}/activate", status_code=204)
async def activate(
    template_id: str, version_id: str, access: Manage, user: UserDep, c: ContainerDep
) -> None:
    await c.prompts.activate(
        access.account_id, template_id=template_id, version_id=version_id, actor_id=user.id
    )


@router.post("/prompts/{template_id}/optimize", status_code=202)
async def optimize(
    template_id: str, body: OptimizeIn, access: Manage, user: UserDep, c: ContainerDep
) -> dict[str, str]:
    return await c.prompt_optimizer.start(
        access.account_id,
        template_id=template_id,
        model=body.model,
        items=[i.model_dump() for i in body.items],
        actor_id=user.id,
        budget_usd=body.budget_usd,
    )


@router.get("/prompt-optimizations/{run_id}")
async def get_optimization(run_id: str, access: Read, c: ContainerDep) -> dict[str, Any]:
    return await c.prompt_optimizer.get(access.account_id, run_id)
