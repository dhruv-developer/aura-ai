from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from aura_api.deps import ContainerDep, UserDep, account_access
from aura_api.schemas import (
    CheckoutOut,
    CheckoutRequest,
    CreditsOut,
    EntitlementOut,
    LedgerLineOut,
    PlanOut,
    SubscriptionOut,
)
from aura_auth.application.dto import AccountAccess
from aura_auth.domain.rbac import Permission
from aura_shared.errors import NotFound

router = APIRouter(prefix="/v1", tags=["billing"])

BillingRead = Annotated[AccountAccess, Depends(account_access(Permission.BILLING_READ))]
BillingManage = Annotated[AccountAccess, Depends(account_access(Permission.BILLING_MANAGE))]
AccountRead = Annotated[AccountAccess, Depends(account_access(Permission.ACCOUNT_READ))]


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(c: ContainerDep, audience: str | None = None) -> list[PlanOut]:
    plans = sorted(c.catalog.plans.values(), key=lambda p: p.sort_order)
    return [
        PlanOut(
            code=p.code,
            name=p.name,
            audience=p.audience,
            price_minor=p.price_minor,
            currency=p.currency,
            interval=p.interval,
            trial_days=p.trial_days,
            entitlements=[
                EntitlementOut(feature=e.feature, enabled=e.enabled, limit=e.limit, period=e.period)
                for e in p.entitlements
            ],
        )
        for p in plans
        if p.is_public and (audience is None or p.audience == audience)
    ]


@router.get("/accounts/{account_id}/subscription", response_model=SubscriptionOut)
async def get_subscription(access: BillingRead, c: ContainerDep) -> SubscriptionOut:
    sub = await c.subscriptions.current(access.account_id)
    if sub is None:
        raise NotFound("no active subscription")
    return SubscriptionOut.model_validate(sub)


@router.post("/accounts/{account_id}/subscription/checkout", response_model=CheckoutOut)
async def checkout(
    body: CheckoutRequest, access: BillingManage, user: UserDep, c: ContainerDep
) -> CheckoutOut:
    url = await c.subscriptions.begin_checkout(
        account_id=access.account_id, plan_code=body.plan, actor_user_id=user.id
    )
    return CheckoutOut(checkout_url=url)


@router.get("/accounts/{account_id}/entitlements", response_model=list[EntitlementOut])
async def entitlements(access: AccountRead, c: ContainerDep) -> list[EntitlementOut]:
    ents = await c.entitlements.get(access.account_id)
    out = []
    for key, e in sorted(ents.features().items()):
        spec = c.catalog.features.get(key)
        used = None
        if spec is not None and spec.kind != "boolean" and e.enabled:
            used, _ = await c.entitlements.usage(access.account_id, key)
        out.append(
            EntitlementOut(
                feature=key, enabled=e.enabled, limit=e.limit, period=e.period, used=used
            )
        )
    return out


@router.get("/accounts/{account_id}/credits", response_model=CreditsOut)
async def credits(access: BillingRead, c: ContainerDep, limit: int = 50) -> CreditsOut:
    return CreditsOut(
        balance=await c.credits.balance(access.account_id),
        history=[
            LedgerLineOut.model_validate(line)
            for line in await c.credits.history(access.account_id, limit=limit)
        ],
    )


@router.post("/webhooks/razorpay", include_in_schema=False)
async def razorpay_webhook(
    request: Request,
    c: ContainerDep,
    x_razorpay_signature: Annotated[str, Header()] = "",
    x_razorpay_event_id: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    outcome = await c.subscriptions.handle_razorpay_webhook(
        body=await request.body(), signature=x_razorpay_signature, event_id=x_razorpay_event_id
    )
    return {"status": outcome}
