"""AURA Personal (B2C) control-plane endpoints: credit packs, referrals, share cards."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from aura_api.deps import ContainerDep, UserDep, account_access
from aura_api.schemas import (
    CreditOrderOut,
    CreditPackOut,
    CreditPurchaseIn,
    ReferralOut,
    ReferralRedeemIn,
    ShareIn,
)
from aura_auth.application.dto import AccountAccess
from aura_auth.domain.rbac import Permission

router = APIRouter(prefix="/v1", tags=["personal"])

BillingManage = Annotated[AccountAccess, Depends(account_access(Permission.BILLING_MANAGE))]
Execute = Annotated[AccountAccess, Depends(account_access(Permission.EXECUTE))]


@router.get("/credit-packs", response_model=list[CreditPackOut])
async def credit_packs(c: ContainerDep) -> list[CreditPackOut]:
    return [
        CreditPackOut(
            code=p.code,
            name=p.name,
            credits=p.credits,
            price_minor=p.price_minor,
            currency=p.currency,
        )
        for p in sorted(c.catalog.credit_packs.values(), key=lambda p: p.price_minor)
    ]


@router.post("/accounts/{account_id}/credits/purchase", response_model=CreditOrderOut)
async def purchase_credits(
    body: CreditPurchaseIn, access: BillingManage, user: UserDep, c: ContainerDep
) -> CreditOrderOut:
    order = await c.subscriptions.begin_credit_purchase(
        account_id=access.account_id, pack_code=body.pack, actor_user_id=user.id
    )
    return CreditOrderOut(
        provider=order.provider,
        order_id=order.order_id,
        amount_minor=order.amount_minor,
        currency=order.currency,
        key_id=order.public_key,
    )


@router.get("/me/referral", response_model=ReferralOut)
async def my_referral(user: UserDep, c: ContainerDep) -> ReferralOut:
    code = await c.referrals.code_for(user.id)
    stats = await c.referrals.stats(user.id)
    return ReferralOut(code=code, **stats)


@router.post("/me/referral/redeem")
async def redeem_referral(body: ReferralRedeemIn, user: UserDep, c: ContainerDep) -> dict[str, int]:
    return {"credits": await c.referrals.redeem(user_id=user.id, code=body.code)}


@router.post("/accounts/{account_id}/shares", status_code=201)
async def create_share(body: ShareIn, access: Execute, c: ContainerDep) -> dict[str, Any]:
    return await c.shares.create(access.account_id, body.execution_id)


@router.delete("/accounts/{account_id}/shares/{card_id}", status_code=204)
async def delete_share(card_id: str, access: Execute, c: ContainerDep) -> None:
    await c.shares.delete(access.account_id, card_id)


@router.get("/shares/{card_id}")
async def public_share(card_id: str, c: ContainerDep) -> dict[str, Any]:
    """Public, unauthenticated: the card only holds content-free aggregates."""
    return await c.shares.get_public(card_id)
