from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from aura_api.deps import ContainerDep, UserDep, account_access
from aura_api.schemas import (
    AccountAccessOut,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    MeOut,
    OrganizationCreate,
    OrganizationOut,
    ProviderCredentialCreate,
    ProviderCredentialOut,
    UserOut,
)
from aura_auth.application.dto import AccountAccess, OrganizationView
from aura_auth.domain.rbac import DEFAULT_API_KEY_PERMISSIONS, Permission

router = APIRouter(prefix="/v1", tags=["accounts"])


def _access_out(a: AccountAccess) -> AccountAccessOut:
    return AccountAccessOut(
        account_id=a.account_id,
        kind=a.kind,
        tier=a.tier,
        name=a.name,
        role=a.role,
        organization_id=a.organization_id,
        slug=a.slug,
        permissions=sorted(p.value for p in a.permissions),
    )


@router.get("/me", response_model=MeOut)
async def me(user: UserDep, c: ContainerDep) -> MeOut:
    accounts = await c.identity.list_access(user.id)
    return MeOut(user=UserOut.model_validate(user), accounts=[_access_out(a) for a in accounts])


@router.post("/organizations", status_code=201, response_model=OrganizationOut)
async def create_organization(
    body: OrganizationCreate, user: UserDep, c: ContainerDep
) -> OrganizationView:
    return await c.identity.create_organization(user.id, body.name)


KeysRead = Annotated[AccountAccess, Depends(account_access(Permission.API_KEYS_READ))]
KeysManage = Annotated[AccountAccess, Depends(account_access(Permission.API_KEYS_MANAGE))]


@router.get("/accounts/{account_id}/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(access: KeysRead, c: ContainerDep) -> list[ApiKeyOut]:
    return [ApiKeyOut.model_validate(k) for k in await c.api_keys.list(access.account_id)]


@router.post("/accounts/{account_id}/api-keys", status_code=201, response_model=ApiKeyCreated)
async def create_api_key(
    body: ApiKeyCreate, access: KeysManage, user: UserDep, c: ContainerDep
) -> ApiKeyCreated:
    view, plaintext = await c.api_keys.issue(
        account_id=access.account_id,
        actor_user_id=user.id,
        actor_permissions=access.permissions,
        name=body.name,
        permissions=set(body.permissions or {p.value for p in DEFAULT_API_KEY_PERMISSIONS}),
        environment_id=body.environment_id,
        expires_at=body.expires_at,
    )
    return ApiKeyCreated(**ApiKeyOut.model_validate(view).model_dump(), key=plaintext)


@router.delete("/accounts/{account_id}/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str, access: KeysManage, user: UserDep, c: ContainerDep
) -> Response:
    await c.api_keys.revoke(account_id=access.account_id, key_id=key_id, actor_user_id=user.id)
    return Response(status_code=204)


# ---- BYOK provider credentials ----------------------------------------------------------


@router.get(
    "/accounts/{account_id}/provider-credentials", response_model=list[ProviderCredentialOut]
)
async def list_provider_credentials(
    access: KeysRead, c: ContainerDep
) -> list[ProviderCredentialOut]:
    return [
        ProviderCredentialOut.model_validate(v)
        for v in await c.provider_credentials.list(access.account_id)
    ]


@router.post(
    "/accounts/{account_id}/provider-credentials",
    status_code=201,
    response_model=ProviderCredentialOut,
)
async def add_provider_credential(
    body: ProviderCredentialCreate, access: KeysManage, user: UserDep, c: ContainerDep
) -> ProviderCredentialOut:
    await c.entitlements.require(access.account_id, "byok")
    view = await c.provider_credentials.add(
        account_id=access.account_id,
        provider=body.provider,
        api_key=body.api_key,
        label=body.label,
        base_url=body.base_url,
        actor_user_id=user.id,
    )
    return ProviderCredentialOut.model_validate(view)


@router.delete("/accounts/{account_id}/provider-credentials/{credential_id}", status_code=204)
async def revoke_provider_credential(
    credential_id: str, access: KeysManage, user: UserDep, c: ContainerDep
) -> Response:
    await c.provider_credentials.revoke(
        account_id=access.account_id, credential_id=credential_id, actor_user_id=user.id
    )
    return Response(status_code=204)


# ---- execution graph runs (metadata + trace only; content stays sealed) ---------------


@router.get("/accounts/{account_id}/runs")
async def list_runs(access: KeysRead, c: ContainerDep, limit: int = 50) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from aura_db.models import GraphRun

    async with c.db.tenant(access.account_id) as s:
        rows = await s.scalars(
            select(GraphRun).order_by(GraphRun.created_at.desc()).limit(min(limit, 200))
        )
        return [
            {
                "id": r.id,
                "status": r.status,
                "template": r.template,
                "trace": r.trace,
                "cost_usd": str(r.cost_usd),
                "error": r.error,
                "created_at": r.created_at,
            }
            for r in rows
        ]


class AccountSettings(BaseModel):
    autopilot: bool | None = None
    learning_consent: bool | None = None


@router.patch("/accounts/{account_id}/settings")
async def update_settings(
    body: AccountSettings,
    access: Annotated[AccountAccess, Depends(account_access(Permission.ACCOUNT_MANAGE))],
    user: UserDep,
    c: ContainerDep,
) -> dict[str, bool]:
    from aura_db import audit
    from aura_db.models import Account

    if body.autopilot:
        await c.entitlements.require(access.account_id, "policy_deployment")
    async with c.db.system() as s:
        account = await s.get(Account, access.account_id)
        assert account is not None
        before = {"autopilot": account.autopilot, "learning_consent": account.learning_consent}
        if body.autopilot is not None:
            account.autopilot = body.autopilot
        if body.learning_consent is not None:
            account.learning_consent = body.learning_consent
        after = {"autopilot": account.autopilot, "learning_consent": account.learning_consent}
        await audit.record(
            s,
            actor_type="user",
            actor_id=user.id,
            account_id=access.account_id,
            action="account.settings",
            before=before,
            after=after,
        )
    return after
