"""Organization members and custom roles."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from aura_api.deps import ContainerDep, UserDep, account_access
from aura_api.schemas import CustomRoleIn, MemberIn, MemberRoleIn
from aura_auth.application.dto import AccountAccess
from aura_auth.domain.rbac import Permission

router = APIRouter(prefix="/v1/accounts/{account_id}", tags=["members"])

MembersRead = Annotated[AccountAccess, Depends(account_access(Permission.MEMBERS_READ))]
MembersManage = Annotated[AccountAccess, Depends(account_access(Permission.MEMBERS_MANAGE))]


@router.get("/members")
async def list_members(access: MembersRead, c: ContainerDep) -> list[dict[str, Any]]:
    return [dataclasses.asdict(m) for m in await c.members.list_members(access.account_id)]


@router.post("/members", status_code=201)
async def add_member(
    body: MemberIn, access: MembersManage, user: UserDep, c: ContainerDep
) -> dict[str, Any]:
    if body.custom_role_id:
        await c.entitlements.require(access.account_id, "advanced_rbac")
    m = await c.members.add_member(
        access,
        user.id,
        access.account_id,
        email=body.email,
        role=body.role,
        custom_role_id=body.custom_role_id,
    )
    return dataclasses.asdict(m)


@router.patch("/members/{user_id}")
async def change_role(
    user_id: str, body: MemberRoleIn, access: MembersManage, user: UserDep, c: ContainerDep
) -> dict[str, Any]:
    if body.custom_role_id:
        await c.entitlements.require(access.account_id, "advanced_rbac")
    m = await c.members.change_role(
        access,
        user.id,
        access.account_id,
        user_id,
        role=body.role,
        custom_role_id=body.custom_role_id,
    )
    return dataclasses.asdict(m)


@router.delete("/members/{user_id}", status_code=204)
async def remove_member(
    user_id: str, access: MembersManage, user: UserDep, c: ContainerDep
) -> None:
    await c.members.remove_member(access, user.id, access.account_id, user_id)


@router.get("/roles")
async def list_roles(access: MembersRead, c: ContainerDep) -> list[dict[str, Any]]:
    return [dataclasses.asdict(r) for r in await c.members.list_roles(access.account_id)]


@router.post("/roles", status_code=201)
async def create_role(
    body: CustomRoleIn, access: MembersManage, user: UserDep, c: ContainerDep
) -> dict[str, Any]:
    await c.entitlements.require(access.account_id, "advanced_rbac")
    role = await c.members.create_role(
        access,
        user.id,
        name=body.name,
        permissions=set(body.permissions),
        description=body.description,
    )
    return dataclasses.asdict(role)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: str, access: MembersManage, user: UserDep, c: ContainerDep) -> None:
    await c.members.delete_role(access.account_id, role_id, user.id)
