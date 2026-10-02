"""/v1/admin: superadmin only, behind a TOTP-backed admin session."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from aura_api.admin_service import AdminService
from aura_api.deps import ContainerDep, UserDep
from aura_auth.application.dto import UserView
from aura_shared.errors import Forbidden, Unauthenticated

router = APIRouter(prefix="/v1/admin", tags=["admin"])
ADMIN_COOKIE = "aura_admin"


async def require_superadmin(user: UserDep) -> UserView:
    if not user.is_superadmin:
        raise Forbidden("superadmin access required")
    return user


SuperadminDep = Annotated[UserView, Depends(require_superadmin)]


async def require_admin_session(request: Request, user: SuperadminDep, c: ContainerDep) -> UserView:
    cookie = request.cookies.get(ADMIN_COOKIE)
    if not cookie or not await c.admin_mfa.check_session(cookie, user_id=user.id):
        raise Unauthenticated("admin session required: verify your TOTP code")
    return user


AdminDep = Annotated[UserView, Depends(require_admin_session)]


def _svc(c: ContainerDep) -> AdminService:
    return AdminService(c)


SvcDep = Annotated[AdminService, Depends(_svc)]


# ---- admin auth -------------------------------------------------------------------------


class TotpCode(BaseModel):
    code: str = Field(min_length=6, max_length=8)


@router.post("/auth/totp/enroll")
async def enroll(user: SuperadminDep, c: ContainerDep) -> dict[str, str]:
    secret, uri = await c.admin_mfa.enroll(user.id)
    return {"secret": secret, "otpauth_uri": uri}


@router.post("/auth/totp/verify", status_code=204)
async def verify(
    body: TotpCode, user: SuperadminDep, c: ContainerDep, response: Response
) -> Response:
    token = await c.admin_mfa.verify(user.id, body.code)
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=8 * 3600,
        httponly=True,
        secure=c.settings.cookie_secure,
        samesite="strict",
        path="/v1/admin",
    )
    response.status_code = 204
    return response


@router.post("/auth/logout", status_code=204)
async def admin_logout(request: Request, c: ContainerDep, response: Response) -> Response:
    if cookie := request.cookies.get(ADMIN_COOKIE):
        await c.admin_mfa.end_session(cookie)
    response.delete_cookie(ADMIN_COOKIE, path="/v1/admin")
    response.status_code = 204
    return response


# ---- read models ------------------------------------------------------------------------


@router.get("/metrics")
async def metrics(_: AdminDep, svc: SvcDep) -> dict[str, Any]:
    return await svc.metrics()


@router.get("/optimization-health")
async def optimization_health(_: AdminDep, svc: SvcDep) -> dict[str, Any]:
    return await svc.optimization_health()


@router.get("/economics")
async def economics(_: AdminDep, c: ContainerDep, month: str | None = None) -> dict[str, Any]:
    """Consumer unit economics for a month (YYYY-MM, default: the current month)."""
    from datetime import UTC, datetime

    from aura_billing.domain.periods import period_end
    from aura_shared.errors import ValidationFailed

    try:
        start = (
            datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
            if month
            else datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )
    except ValueError as exc:
        raise ValidationFailed("month must look like 2026-10") from exc
    end = period_end("month", start)
    assert end is not None
    return (await c.economics.consumer_margin(start, end)).to_dict()


@router.get("/accounts")
async def accounts(
    _: AdminDep,
    svc: SvcDep,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return await svc.list_accounts(kind=kind, q=q, limit=limit, offset=offset)


@router.get("/accounts/{account_id}")
async def account(account_id: str, _: AdminDep, svc: SvcDep) -> dict[str, Any]:
    return await svc.account_detail(account_id)


@router.get("/leads")
async def leads(
    _: AdminDep, svc: SvcDep, stage: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    return await svc.leads(stage=stage, limit=limit)


@router.get("/audit")
async def audit_log(
    _: AdminDep, svc: SvcDep, account_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    return await svc.audit_log(account_id=account_id, limit=limit)


@router.get("/models")
async def models(_: AdminDep, c: ContainerDep) -> list[dict[str, Any]]:
    from aura_providers.domain.catalog import load_catalog

    return [
        {
            "id": m.id,
            "provider": m.provider,
            "tier": m.tier.name.lower(),
            "context_window": m.context_window,
            "prices": {
                "input": str(m.prices.input),
                "output": str(m.prices.output),
                "cached_input": str(m.prices.cached_input),
            },
        }
        for m in load_catalog().models.values()
    ]


# ---- actions ----------------------------------------------------------------------------


class StatusChange(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PlanChange(BaseModel):
    plan: str
    reason: str = Field(min_length=3, max_length=500)


class CreditChange(BaseModel):
    amount: int
    kind: str = "grant"
    reason: str = Field(min_length=3, max_length=500)
    reference: str | None = Field(default=None, max_length=128)


class OverrideIn(BaseModel):
    feature: str
    enabled: bool = True
    limit: int | None = Field(default=None, ge=0)
    period: str | None = None
    expires_at: datetime | None = None
    reason: str = Field(min_length=3, max_length=500)


class LeadStage(BaseModel):
    stage: str


@router.post("/accounts/{account_id}/suspend", status_code=204)
async def suspend(account_id: str, body: StatusChange, admin: AdminDep, svc: SvcDep) -> Response:
    await svc.set_account_status(
        account_id, status="suspended", reason=body.reason, actor_id=admin.id
    )
    return Response(status_code=204)


@router.post("/accounts/{account_id}/restore", status_code=204)
async def restore(account_id: str, body: StatusChange, admin: AdminDep, svc: SvcDep) -> Response:
    await svc.set_account_status(account_id, status="active", reason=body.reason, actor_id=admin.id)
    return Response(status_code=204)


@router.post("/accounts/{account_id}/plan")
async def change_plan(
    account_id: str, body: PlanChange, admin: AdminDep, c: ContainerDep
) -> dict[str, Any]:
    view = await c.subscriptions.change_plan(
        account_id=account_id,
        plan_code=body.plan,
        actor_type="superadmin",
        actor_id=admin.id,
        reason=body.reason,
    )
    return {"plan": view.plan_code, "status": view.status, "version": view.plan_version}


@router.post("/accounts/{account_id}/credits")
async def credits(
    account_id: str, body: CreditChange, admin: AdminDep, svc: SvcDep
) -> dict[str, Any]:
    return await svc.grant_credits(
        account_id,
        amount=body.amount,
        kind=body.kind,
        reason=body.reason,
        actor_id=admin.id,
        reference=body.reference,
    )


@router.post("/accounts/{account_id}/overrides", status_code=201)
async def override(
    account_id: str, body: OverrideIn, admin: AdminDep, svc: SvcDep
) -> dict[str, str]:
    oid = await svc.add_override(
        account_id,
        feature=body.feature,
        enabled=body.enabled,
        limit=body.limit,
        period=body.period,
        expires_at=body.expires_at,
        reason=body.reason,
        actor_id=admin.id,
    )
    return {"id": oid}


@router.post("/users/{user_id}/suspend", status_code=204)
async def suspend_user(user_id: str, body: StatusChange, admin: AdminDep, svc: SvcDep) -> Response:
    if user_id == admin.id:
        raise Forbidden("you cannot suspend yourself")
    await svc.set_user_status(user_id, status="suspended", reason=body.reason, actor_id=admin.id)
    return Response(status_code=204)


@router.patch("/leads/{lead_id}", status_code=204)
async def lead_stage(lead_id: str, body: LeadStage, admin: AdminDep, svc: SvcDep) -> Response:
    await svc.update_lead(lead_id, stage=body.stage, actor_id=admin.id)
    return Response(status_code=204)
