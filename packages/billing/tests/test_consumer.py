"""Consumer credit economics: gate, metering, grants, referrals, packs, margin."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from aura_billing.application.ports import OrderSession
from aura_billing.application.subscriptions import SubscriptionService
from aura_billing.domain.credits import credits_for_cost
from aura_billing.domain.ledger import TxnKind
from aura_db.models import User
from aura_shared.errors import Conflict, NotFound, ValidationFailed

WEBHOOK_SECRET = "whsec_test"


async def _personal(container, email: str) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    [access] = await container.identity.list_access(user.id)
    return user.id, str(access.account_id)


def test_credit_pricing() -> None:
    assert credits_for_cost(Decimal(0)) == 0
    assert credits_for_cost(Decimal("0.0000001")) == 1  # never free
    assert credits_for_cost(Decimal("0.01")) == 100
    assert credits_for_cost(Decimal("0.010001")) == 101  # rounds up


async def test_signup_grants_first_month_and_gate_tracks_balance(container) -> None:  # type: ignore[no-untyped-def]
    _, acct = await _personal(container, "a@x.io")
    assert await container.credits.balance(acct) == 1000  # free plan monthly_credits
    gate = container.credit_gate
    assert await gate.metered(acct) and not await gate.exhausted(acct)
    await container.credits.post(acct, TxnKind.CONSUME, 1000)
    await gate.invalidate(acct)
    assert await gate.exhausted(acct)

    owner = await container.identity.login_with_email("o@acme.io")
    org = await container.identity.create_organization(owner.id, "Acme")
    assert not await gate.metered(org.account_id)  # business accounts are invoiced
    assert not await gate.exhausted(org.account_id)


async def test_meter_charges_platform_usage_once(container) -> None:  # type: ignore[no-untyped-def]
    from aura_billing.application.consumer import CreditMeter

    _, acct = await _personal(container, "m@x.io")
    meter = CreditMeter(container.credits, container.credit_gate)
    kw = {"account_id": acct, "execution_id": "exe_1", "cost_usd": Decimal("0.0123")}
    assert await meter.charge(**kw, credential_source="byok") == 0
    assert await meter.charge(**kw, credential_source="cache") == 0
    assert await meter.charge(**kw, credential_source="platform") == 123
    assert await meter.charge(**kw, credential_source="platform") == 0  # redelivery
    assert await container.credits.balance(acct) == 1000 - 123
    big = {**kw, "execution_id": "exe_2", "cost_usd": Decimal("1")}
    await meter.charge(**big, credential_source="platform")  # overdraft is allowed
    assert await container.credits.balance(acct) < 0
    assert await container.credit_gate.exhausted(acct)


async def test_monthly_grant_expires_only_unused_grant(container) -> None:  # type: ignore[no-untyped-def]
    _, acct = await _personal(container, "g@x.io")
    grants = container.grants
    now = datetime.now(UTC)
    assert await grants.grant(acct, now=now) is None  # signup already granted this month
    await container.credits.post(acct, TxnKind.CONSUME, 300)
    await container.credits.post(acct, TxnKind.PURCHASE, 5000, reference="rzp_order:o1")
    later = (now.replace(day=1) + timedelta(days=32)).replace(day=2)
    line = await grants.grant(acct, now=later)
    # 700 unused grant credits expire; the 5000 purchased credits are untouched.
    assert line is not None and line.expired == 700 and line.granted == 1000
    assert await container.credits.balance(acct) == 5000 + 1000
    assert await grants.grant(acct, now=later) is None  # idempotent
    lines = await grants.run(now=later)
    assert acct not in {line.account_id for line in lines}


async def test_referrals(container) -> None:  # type: ignore[no-untyped-def]
    ref_user, ref_acct = await _personal(container, "ref@x.io")
    new_user, new_acct = await _personal(container, "new@x.io")
    svc = container.referrals
    code = await svc.code_for(ref_user)
    assert code == await svc.code_for(ref_user) and len(code) == 8
    with pytest.raises(ValidationFailed):
        await svc.redeem(user_id=ref_user, code=code)  # self-referral
    with pytest.raises(NotFound):
        await svc.redeem(user_id=new_user, code="NOPE1234")
    assert await svc.redeem(user_id=new_user, code=code.lower()) == 2000
    with pytest.raises(Conflict):
        await svc.redeem(user_id=new_user, code=code)  # once per user
    assert await container.credits.balance(new_acct) == 1000 + 2000
    assert await container.credits.balance(ref_acct) == 1000 + 2000
    assert await svc.stats(ref_user) == {"referred": 1, "credits_earned": 2000}

    late_user, _ = await _personal(container, "late@x.io")
    async with container.db.system() as s:
        await s.execute(
            update(User)
            .where(User.id == late_user)
            .values(created_at=datetime.now(UTC) - timedelta(days=30))
        )
    with pytest.raises(ValidationFailed, match="soon after signup"):
        await svc.redeem(user_id=late_user, code=code)


class FakeOrders:
    name = "razorpay"

    async def create_order(self, *, amount_minor, currency, receipt, notes):  # type: ignore[no-untyped-def]
        self.notes = notes
        return OrderSession("razorpay", "order_T1", amount_minor, currency, "rzp_test_key")


def _signed(payload: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    return raw, hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _order_paid(order_id: str, amount: int, notes: dict[str, str]) -> dict[str, object]:
    return {
        "event": "order.paid",
        "payload": {
            "order": {
                "entity": {
                    "id": order_id,
                    "amount_paid": amount,
                    "currency": "INR",
                    "notes": notes,
                }
            }
        },
    }


async def test_credit_pack_purchase_via_signed_webhook(container) -> None:  # type: ignore[no-untyped-def]
    subs = SubscriptionService(
        container.db,
        catalog=container.catalog,
        entitlements=container.entitlements,
        gateway=FakeOrders(),  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        credits=container.credits,
        on_credits_changed=container.credit_gate.invalidate,
    )
    user_id, acct = await _personal(container, "buyer@x.io")
    with pytest.raises(NotFound):
        await subs.begin_credit_purchase(account_id=acct, pack_code="huge", actor_user_id=user_id)
    order = await subs.begin_credit_purchase(
        account_id=acct, pack_code="small", actor_user_id=user_id
    )
    assert order.order_id == "order_T1" and order.amount_minor == 19900
    assert await container.credits.balance(acct) == 1000  # nothing until the webhook

    notes = {"account_id": acct, "credit_pack": "small"}
    body, sig = _signed(_order_paid("order_T0", 100, notes))  # tampered amount
    assert await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="o0") == (
        "rejected_order:amount_mismatch"
    )
    body, sig = _signed(_order_paid("order_T1", 19900, notes))
    assert await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="o1") == "applied"
    assert await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="o1") == (
        "duplicate"
    )
    # A different event id for the same order (provider retry) cannot double-credit.
    assert await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="o1b") == (
        "duplicate_order"
    )
    assert await container.credits.balance(acct) == 1000 + 10000

    owner = await container.identity.login_with_email("o@acme.io")
    org = await container.identity.create_organization(owner.id, "Acme")
    with pytest.raises(ValidationFailed):
        await subs.begin_credit_purchase(
            account_id=org.account_id, pack_code="small", actor_user_id=owner.id
        )

    now = datetime.now(UTC)
    report = await container.economics.consumer_margin(now - timedelta(days=1), now + timedelta(1))
    mine = next(a for a in report.accounts if a.account_id == acct)
    assert mine.revenue_inr == Decimal("199")
    await container.credits.post(acct, TxnKind.CONSUME, 10_000_000, allow_negative=True)
    report = await container.economics.consumer_margin(now - timedelta(days=1), now + timedelta(1))
    assert report.alert and report.to_dict()["negative_margin_accounts"] >= 1
