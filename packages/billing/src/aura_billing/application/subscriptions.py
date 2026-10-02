"""Subscriptions: defaults at signup, checkout, admin plan changes, gateway webhooks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aura_billing.application.credits import CreditService
from aura_billing.application.entitlements import EntitlementService
from aura_billing.application.ports import OrderSession, PaymentGateway
from aura_billing.domain import razorpay
from aura_billing.domain.catalog import Catalog
from aura_billing.domain.ledger import TxnKind
from aura_billing.domain.razorpay import GatewayEventKind as K
from aura_billing.domain.subscriptions import LIVE, SubscriptionStatus, can_transition
from aura_db import Database, audit
from aura_db.models import Account, Invoice, PaymentEvent, Plan, PlanVersion, Subscription
from aura_shared.errors import (
    DependencyUnavailable,
    NotFound,
    Unauthenticated,
    ValidationFailed,
)
from aura_shared.ids import new_id

AUDIENCE_FOR_KIND = {"individual": "b2c", "organization": "b2b"}
LIVE_VALUES = [s.value for s in LIVE]


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    id: str
    account_id: str
    plan_code: str
    plan_name: str
    plan_version: int
    status: str
    price_minor: int | None
    currency: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_ends_at: datetime | None
    cancel_at_period_end: bool


class SubscriptionService:
    def __init__(
        self,
        db: Database,
        *,
        catalog: Catalog,
        entitlements: EntitlementService,
        gateway: PaymentGateway | None = None,
        webhook_secret: str | None = None,
        credits: CreditService | None = None,
        on_credits_changed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._credits = credits or CreditService(db)
        self._on_credits_changed = on_credits_changed
        self._catalog = catalog
        self._ent = entitlements
        self._gateway = gateway
        self._webhook_secret = webhook_secret

    # ---- signup hook ---------------------------------------------------------------
    async def on_account_created(self, s: AsyncSession, account_id: str, kind: str) -> None:
        """Registered as an ``AccountCreatedHook`` and runs inside the signup transaction."""
        plan = self._catalog.default_plan_for(kind)
        pv = await self._current_version(s, plan.code)
        now = datetime.now(UTC)
        trial_end = now + timedelta(days=pv.trial_days) if pv.trial_days else None
        s.add(
            Subscription(
                id=new_id("sub"),
                account_id=account_id,
                plan_version_id=pv.id,
                status=(
                    SubscriptionStatus.TRIALING if trial_end else SubscriptionStatus.ACTIVE
                ).value,
                current_period_start=now,
                current_period_end=trial_end,
                trial_ends_at=trial_end,
            )
        )
        await s.flush()

    @staticmethod
    async def _current_version(s: AsyncSession, plan_code: str) -> PlanVersion:
        pv = await s.scalar(
            select(PlanVersion)
            .join(Plan, Plan.current_version_id == PlanVersion.id)
            .where(Plan.code == plan_code)
        )
        if pv is None:
            raise NotFound(f"plan {plan_code!r} not found (is the catalog synced?)")
        return pv

    # ---- reads -----------------------------------------------------------------------
    async def current(self, account_id: str) -> SubscriptionView | None:
        async with self._db.tenant(account_id) as s:
            row = (
                await s.execute(
                    select(Subscription, PlanVersion, Plan)
                    .join(PlanVersion, PlanVersion.id == Subscription.plan_version_id)
                    .join(Plan, Plan.code == PlanVersion.plan_code)
                    .where(Subscription.status.in_(LIVE_VALUES))
                )
            ).first()
        if row is None:
            return None
        sub, pv, plan = row
        return SubscriptionView(
            sub.id,
            sub.account_id,
            plan.code,
            plan.name,
            pv.version,
            sub.status,
            pv.price_minor,
            pv.currency,
            sub.current_period_start,
            sub.current_period_end,
            sub.trial_ends_at,
            sub.cancel_at_period_end,
        )

    # ---- admin plan change (superadmin, sales-led deals) -----------------------------
    async def change_plan(
        self, *, account_id: str, plan_code: str, actor_type: str, actor_id: str, reason: str
    ) -> SubscriptionView:
        if not reason.strip():
            raise ValidationFailed("a reason is required for plan changes")
        async with self._db.system() as s:
            account = await s.get(Account, account_id)
            if account is None:
                raise NotFound("account not found")
            plan = await s.get(Plan, plan_code)
            if plan is None:
                raise NotFound("plan not found")
            if plan.audience != AUDIENCE_FOR_KIND[account.kind]:
                raise ValidationFailed(f"plan {plan_code!r} is not available for this account")
            pv = await self._current_version(s, plan_code)
            before = await self._cancel_live(s, account_id)
            now = datetime.now(UTC)
            s.add(
                Subscription(
                    id=new_id("sub"),
                    account_id=account_id,
                    plan_version_id=pv.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    current_period_start=now,
                )
            )
            await audit.record(
                s,
                actor_type=actor_type,
                actor_id=actor_id,
                account_id=account_id,
                action="subscription.change_plan",
                target_type="account",
                target_id=account_id,
                before={"plan_version_id": before} if before else None,
                after={"plan": plan_code, "plan_version_id": pv.id, "reason": reason},
            )
            await self._ent.enqueue_invalidation(s, account_id)
        await self._ent.invalidate_cache(account_id)
        view = await self.current(account_id)
        assert view is not None
        return view

    @staticmethod
    async def _cancel_live(s: AsyncSession, account_id: str) -> int | None:
        live = await s.scalar(
            select(Subscription)
            .where(Subscription.account_id == account_id, Subscription.status.in_(LIVE_VALUES))
            .with_for_update()
        )
        if live is None:
            return None
        live.status = SubscriptionStatus.CANCELED.value
        live.canceled_at = datetime.now(UTC)
        await s.flush()  # frees the one-live-subscription unique index before the insert
        return live.plan_version_id

    # ---- self-serve checkout -------------------------------------------------------
    async def begin_checkout(self, *, account_id: str, plan_code: str, actor_user_id: str) -> str:
        if self._gateway is None:
            raise DependencyUnavailable("payments are not configured")
        async with self._db.system() as s:
            account = await s.get(Account, account_id)
            plan = await s.get(Plan, plan_code)
            if account is None or plan is None or not plan.is_public:
                raise NotFound("plan not found")
            if plan.audience != AUDIENCE_FOR_KIND[account.kind]:
                raise ValidationFailed(f"plan {plan_code!r} is not available for this account")
            pv = await self._current_version(s, plan_code)
            if not pv.price_minor:
                raise ValidationFailed("this plan cannot be purchased online")
            provider_plan = pv.provider_refs.get(self._gateway.name)
            if not provider_plan:
                raise ValidationFailed("this plan is not purchasable yet")
        checkout = await self._gateway.create_subscription(
            provider_plan_id=provider_plan,
            notes={"account_id": account_id, "plan_version_id": str(pv.id)},
            total_count=120,
        )
        async with self._db.system() as s:
            s.add(
                Subscription(
                    id=new_id("sub"),
                    account_id=account_id,
                    plan_version_id=pv.id,
                    status=SubscriptionStatus.INCOMPLETE.value,
                    provider=checkout.provider,
                    provider_subscription_id=checkout.provider_subscription_id,
                )
            )
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_user_id,
                account_id=account_id,
                action="subscription.checkout_started",
                after={"plan": plan_code, "provider_id": checkout.provider_subscription_id},
            )
        return checkout.checkout_url

    # ---- credit packs (one-off orders) ------------------------------------------------
    async def begin_credit_purchase(
        self, *, account_id: str, pack_code: str, actor_user_id: str
    ) -> OrderSession:
        """Credits are granted only by the signed ``order.paid`` webhook, never by the
        client's success callback (which a user could forge)."""
        if self._gateway is None:
            raise DependencyUnavailable("payments are not configured")
        pack = self._catalog.credit_packs.get(pack_code)
        if pack is None:
            raise NotFound("credit pack not found")
        async with self._db.system() as s:
            account = await s.get(Account, account_id)
            if account is None:
                raise NotFound("account not found")
            if account.kind != "individual":
                raise ValidationFailed("credit packs are for personal accounts")
        order = await self._gateway.create_order(
            amount_minor=pack.price_minor,
            currency=pack.currency,
            receipt=f"{account_id}:{pack.code}",
            notes={"account_id": account_id, "credit_pack": pack.code},
        )
        async with self._db.system() as s:
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_user_id,
                account_id=account_id,
                action="credits.purchase_started",
                after={"pack": pack.code, "order_id": order.order_id},
            )
        return order

    async def _apply_order(
        self, s: AsyncSession, e: razorpay.GatewayEvent
    ) -> tuple[str, str | None]:
        notes = e.notes or {}
        account_id, pack = (
            notes.get("account_id"),
            self._catalog.credit_packs.get(notes.get("credit_pack", "")),
        )
        if not account_id or pack is None or not e.order_id:
            return "ignored_order:not_a_credit_pack", None
        if e.amount_minor != pack.price_minor or (e.currency or "").upper() != pack.currency:
            # Never trust notes alone: the amount actually paid must match the pack.
            return "rejected_order:amount_mismatch", None
        if await s.get(Account, account_id) is None:
            return "rejected_order:unknown_account", None
        posted = await self._credits.post_in(
            s,
            account_id,
            TxnKind.PURCHASE,
            pack.credits,
            reference=f"rzp_order:{e.order_id}",
            memo=f"credit pack {pack.code}",
            actor=("system", "razorpay"),
        )
        # A retried delivery under a new event id: already credited (and not revenue twice).
        return ("duplicate_order" if posted.duplicate else "applied"), None

    # ---- webhooks --------------------------------------------------------------------
    async def handle_razorpay_webhook(
        self, *, body: bytes, signature: str, event_id: str | None
    ) -> str:
        """At-least-once safe: the event row is unique on the provider event id, so a
        replay is a no-op. If processing fails, the whole transaction (including the event
        row) rolls back and the provider retries."""
        if not self._webhook_secret:
            raise DependencyUnavailable("webhooks are not configured")
        if not razorpay.verify_signature(body, signature, self._webhook_secret):
            raise Unauthenticated("invalid webhook signature")
        payload: dict[str, Any] = json.loads(body)
        provider_event_id = event_id or hashlib.sha256(body).hexdigest()
        event = razorpay.normalize(payload)
        touched: str | None = None
        async with self._db.system() as s:
            inserted = await s.scalar(
                insert(PaymentEvent)
                .values(
                    id=new_id("pev"),
                    provider="razorpay",
                    provider_event_id=provider_event_id,
                    type=event.raw_type,
                    payload=payload,
                )
                .on_conflict_do_nothing(constraint="uq_payment_event_provider_id")
                .returning(PaymentEvent.id)
            )
            if inserted is None:
                return "duplicate"
            credited: str | None = None
            if event.kind is K.ORDER_PAID:
                outcome, touched = await self._apply_order(s, event)
                if outcome == "applied":
                    credited = (event.notes or {}).get("account_id")
            else:
                outcome, touched = await self._apply(s, event)
            row = await s.get(PaymentEvent, inserted)
            assert row is not None
            row.processed_at = datetime.now(UTC)
            if outcome != "applied":
                row.error = outcome
            if touched:
                await self._ent.enqueue_invalidation(s, touched)
        if touched:
            await self._ent.invalidate_cache(touched)
        if credited and self._on_credits_changed is not None:
            await self._on_credits_changed(credited)
        return outcome

    async def _apply(self, s: AsyncSession, e: razorpay.GatewayEvent) -> tuple[str, str | None]:
        if e.kind is K.IGNORED:
            return "ignored", None
        if e.subscription_id is None:
            return "missing_subscription_id", None
        sub = await s.scalar(
            select(Subscription)
            .where(Subscription.provider_subscription_id == e.subscription_id)
            .with_for_update()
        )
        if sub is None:
            return "unknown_subscription", None

        if e.kind is K.INVOICE_PAID:
            if e.invoice_id:
                await s.execute(
                    insert(Invoice)
                    .values(
                        id=new_id("inv"),
                        account_id=sub.account_id,
                        subscription_id=sub.id,
                        provider="razorpay",
                        provider_invoice_id=e.invoice_id,
                        amount_minor=e.amount_minor or 0,
                        currency=(e.currency or "INR").upper(),
                        status="paid",
                        issued_at=e.issued_at,
                        pdf_url=e.invoice_url,
                    )
                    .on_conflict_do_nothing(index_elements=[Invoice.provider_invoice_id])
                )
            return "applied", None

        target = {
            K.SUBSCRIPTION_ACTIVATED: SubscriptionStatus.ACTIVE,
            K.SUBSCRIPTION_CHARGED: SubscriptionStatus.ACTIVE,
            K.SUBSCRIPTION_RESUMED: SubscriptionStatus.ACTIVE,
            K.SUBSCRIPTION_PAST_DUE: SubscriptionStatus.PAST_DUE,
            K.SUBSCRIPTION_PAUSED: SubscriptionStatus.PAUSED,
            K.SUBSCRIPTION_CANCELED: SubscriptionStatus.CANCELED,
        }[e.kind]
        if not can_transition(sub.status, target):
            # Late or out-of-order events (e.g. charged after cancel) must not resurrect.
            return f"ignored_transition:{sub.status}->{target.value}", None
        if (
            sub.status == SubscriptionStatus.INCOMPLETE.value
            and target is SubscriptionStatus.ACTIVE
        ):
            await self._cancel_live(s, sub.account_id)  # upgrade replaces the old plan
        sub.status = target.value
        if target is SubscriptionStatus.CANCELED:
            sub.canceled_at = datetime.now(UTC)
        if e.period_start:
            sub.current_period_start = e.period_start
        if e.period_end:
            sub.current_period_end = e.period_end
        return "applied", sub.account_id
