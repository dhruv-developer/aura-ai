"""Integration tests: real Postgres (RLS, triggers) + real Redis."""

import asyncio
import hashlib
import hmac
import json

import pytest
import yaml
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from aura_billing.application.catalog_sync import sync_catalog
from aura_billing.application.ports import CheckoutSession
from aura_billing.application.subscriptions import SubscriptionService
from aura_billing.domain.catalog import parse_catalog
from aura_billing.domain.ledger import TxnKind
from aura_db.models import (
    AuditLog,
    EntitlementOverride,
    Invoice,
    LedgerEntry,
    Plan,
    PlanVersion,
    Subscription,
    UsageRecord,
)
from aura_shared.errors import (
    Forbidden,
    QuotaExceeded,
    Unauthenticated,
    ValidationFailed,
)
from aura_shared.ids import new_id

WEBHOOK_SECRET = "whsec_test"


async def _personal(container, email: str = "u@x.io") -> str:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    [access] = await container.identity.list_access(user.id)
    return str(access.account_id)


# ---- entitlements & quotas -------------------------------------------------------------


async def test_default_plans_gate_features(container) -> None:  # type: ignore[no-untyped-def]
    acct = await _personal(container)
    owner = await container.identity.login_with_email("o@acme.io")
    org = await container.identity.create_organization(owner.id, "Acme")
    with pytest.raises(Forbidden):
        await container.entitlements.require(acct, "shadow_optimization")
    ents = await container.entitlements.require(org.account_id, "shadow_optimization")
    assert ents.plan_code == "startup" and ents.limit("workloads") == 10
    assert ents.limit("seats") is None  # unlimited


async def test_quota_reserve_commit_release(container) -> None:  # type: ignore[no-untyped-def]
    acct = await _personal(container)
    ent = container.entitlements
    r = await ent.reserve(acct, "executions", amount=300, reservation_id="r1")
    with pytest.raises(QuotaExceeded) as exc:
        await ent.reserve(acct, "executions", amount=1, reservation_id="r2")
    assert exc.value.extra["limit"] == 300
    assert await ent.commit(r, actual=250) == 250  # actual usage lower than estimate
    await ent.reserve(acct, "executions", amount=50, reservation_id="r3")
    assert await ent.usage(acct, "executions") == (300, 300)
    with pytest.raises(ValidationFailed):
        await ent.reserve(acct, "basic_routing", amount=1, reservation_id="r4")
    with pytest.raises(Forbidden):
        await ent.reserve(acct, "shadow_runs", amount=1, reservation_id="r5")


async def test_override_takes_effect_after_invalidation(container) -> None:  # type: ignore[no-untyped-def]
    acct = await _personal(container)
    assert (await container.entitlements.get(acct)).limit("executions") == 300
    async with container.db.tenant(acct) as s:
        s.add(
            EntitlementOverride(
                id=new_id("ovr"),
                account_id=acct,
                feature_key="executions",
                enabled=True,
                limit=5000,
                period="month",
                reason="pilot",
                created_by="test",
            )
        )
    assert (await container.entitlements.get(acct)).limit("executions") == 300  # cached
    await container.entitlements.invalidate_cache(acct)
    assert (await container.entitlements.get(acct)).limit("executions") == 5000


# ---- admin plan change & catalog versioning -------------------------------------------


async def test_admin_plan_change_is_audited_and_immediate(container) -> None:  # type: ignore[no-untyped-def]
    acct = await _personal(container)
    assert not (await container.entitlements.get(acct)).allows("research_mode")
    view = await container.subscriptions.change_plan(
        account_id=acct,
        plan_code="pro",
        actor_type="superadmin",
        actor_id="usr_admin",
        reason="creator program",
    )
    assert view.plan_code == "pro" and view.status == "active"
    assert (await container.entitlements.get(acct)).allows("research_mode")
    with pytest.raises(ValidationFailed):  # B2B plan on a personal account
        await container.subscriptions.change_plan(
            account_id=acct,
            plan_code="growth",
            actor_type="superadmin",
            actor_id="a",
            reason="x",
        )
    async with container.db.system() as s:
        subs = list(
            await s.scalars(select(Subscription.status).where(Subscription.account_id == acct))
        )
        audits = list(await s.scalars(select(AuditLog.action).where(AuditLog.account_id == acct)))
        outbox = (await s.execute(text("SELECT count(*) FROM outbox"))).scalar()
    assert sorted(subs) == ["active", "canceled"]
    assert "subscription.change_plan" in audits and outbox >= 1


async def test_catalog_sync_versions_plans_and_grandfathers(container) -> None:  # type: ignore[no-untyped-def]
    base = yaml.safe_load(
        (
            __import__("importlib.resources").resources.files("aura_billing") / "catalog.yaml"
        ).read_text("utf-8")
    )
    base["plans"]["legacy_test"] = {
        "name": "Legacy",
        "audience": "b2c",
        "priority_class": "paid",
        "price_minor": 100,
        "is_public": False,
        "entitlements": {"executions": {"limit": 10, "period": "month"}},
    }
    first = {r.plan_code: r for r in await sync_catalog(container.db, parse_catalog(base))}
    assert first["legacy_test"].created and not first["free"].created  # unchanged = no-op
    again = await sync_catalog(container.db, parse_catalog(base))
    assert not any(r.created for r in again)

    acct = await _personal(container)
    await container.subscriptions.change_plan(
        account_id=acct, plan_code="legacy_test", actor_type="system", actor_id="t", reason="t"
    )
    base["plans"]["legacy_test"]["entitlements"]["executions"]["limit"] = 99
    bumped = {r.plan_code: r for r in await sync_catalog(container.db, parse_catalog(base))}
    assert bumped["legacy_test"].created
    assert bumped["legacy_test"].version == first["legacy_test"].version + 1

    await container.entitlements.invalidate_cache(acct)
    assert (await container.entitlements.get(acct)).limit("executions") == 10  # grandfathered
    fresh = await _personal(container, "new@x.io")
    await container.subscriptions.change_plan(
        account_id=fresh, plan_code="legacy_test", actor_type="system", actor_id="t", reason="t"
    )
    assert (await container.entitlements.get(fresh)).limit("executions") == 99


# ---- credit ledger ---------------------------------------------------------------------


async def test_credit_ledger_is_idempotent_and_never_negative(container) -> None:  # type: ignore[no-untyped-def]
    owner = await container.identity.login_with_email("o@acme.io")
    acct = (await container.identity.create_organization(owner.id, "Acme")).account_id
    credits = container.credits
    grant = await credits.post(acct, TxnKind.GRANT, 100, reference="signup-bonus")
    dup = await credits.post(acct, TxnKind.GRANT, 100, reference="signup-bonus")
    assert grant.balance == 100 and dup.duplicate and dup.transaction_id == grant.transaction_id

    results = await asyncio.gather(
        *[credits.post(acct, TxnKind.CONSUME, 10, reference=f"exe-{i}") for i in range(20)],
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 10
    assert all(isinstance(r, QuotaExceeded) for r in results if isinstance(r, Exception))
    assert await credits.balance(acct) == 0

    await credits.post(acct, TxnKind.REFUND, 10, reference="exe-0-refund", memo="failed run")
    history = await credits.history(acct)
    assert history[0].kind == "refund" and history[0].amount == 10
    assert await credits.balance(acct) == 10


async def test_ledger_rejects_unbalanced_and_mutation(container) -> None:  # type: ignore[no-untyped-def]
    acct = await _personal(container)
    posted = await container.credits.post(acct, TxnKind.GRANT, 5)
    with pytest.raises(DBAPIError, match="unbalanced"):
        async with container.db.system() as s:
            s.add(
                LedgerEntry(
                    id=new_id("led"),
                    transaction_id=posted.transaction_id,
                    account_id=acct,
                    book="customer",
                    amount=1,
                )
            )
    with pytest.raises(DBAPIError, match="append-only"):
        async with container.db.system() as s:
            await s.execute(update(LedgerEntry).values(amount=500))


# ---- checkout & webhooks ---------------------------------------------------------------


class FakeGateway:
    name = "razorpay"

    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    async def create_subscription(self, *, provider_plan_id, notes, total_count):  # type: ignore[no-untyped-def]
        self.created.append({"plan": provider_plan_id, "notes": notes})
        return CheckoutSession("razorpay", "sub_RP1", "https://rzp.io/i/test")

    async def cancel_subscription(self, provider_subscription_id, *, at_period_end):  # type: ignore[no-untyped-def]
        return None


def _signed(event: str, entity: str, body: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps({"event": event, "payload": {entity: {"entity": body}}}).encode()
    return raw, hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


async def test_checkout_and_webhook_lifecycle(container) -> None:  # type: ignore[no-untyped-def]
    gateway = FakeGateway()
    subs = SubscriptionService(
        container.db,
        catalog=container.catalog,
        entitlements=container.entitlements,
        gateway=gateway,
        webhook_secret=WEBHOOK_SECRET,
    )
    acct = await _personal(container)
    with pytest.raises(ValidationFailed, match="not purchasable"):
        await subs.begin_checkout(account_id=acct, plan_code="plus", actor_user_id="usr_x")
    async with container.db.system() as s:  # an operator maps the plan to Razorpay
        pv_id = await s.scalar(select(Plan.current_version_id).where(Plan.code == "plus"))
        await s.execute(
            update(PlanVersion)
            .where(PlanVersion.id == pv_id)
            .values(provider_refs={"razorpay": "plan_TEST"})
        )
    try:
        url = await subs.begin_checkout(account_id=acct, plan_code="plus", actor_user_id="usr_x")
        assert url == "https://rzp.io/i/test"
        assert gateway.created[0]["notes"] == {"account_id": acct, "plan_version_id": str(pv_id)}
        assert (await subs.current(acct)).plan_code == "free"  # unpaid: nothing changes yet

        body, sig = _signed(
            "subscription.activated",
            "subscription",
            {"id": "sub_RP1", "current_start": 1790000000, "current_end": 1792600000},
        )
        assert (
            await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="e1") == "applied"
        )
        assert (
            await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="e1")
            == "duplicate"
        )
        current = await subs.current(acct)
        assert current.plan_code == "plus" and current.status == "active"
        assert (await container.entitlements.get(acct)).allows("personal_memory")

        body, sig = _signed(
            "invoice.paid",
            "invoice",
            {
                "id": "inv_1",
                "subscription_id": "sub_RP1",
                "amount_paid": 49900,
                "currency": "INR",
                "paid_at": 1790000100,
            },
        )
        await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="e2")
        await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="e2b")  # same inv

        body, sig = _signed("subscription.cancelled", "subscription", {"id": "sub_RP1"})
        await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="e3")
        assert await subs.current(acct) is None
        assert (await container.entitlements.get(acct)).plan_code == "free"  # fallback

        body, sig = _signed("subscription.charged", "subscription", {"id": "sub_RP1"})
        outcome = await subs.handle_razorpay_webhook(body=body, signature=sig, event_id="e4")
        assert outcome.startswith("ignored_transition:canceled")  # no resurrection

        with pytest.raises(Unauthenticated):
            await subs.handle_razorpay_webhook(body=body, signature="bad", event_id="e5")

        async with container.db.system() as s:
            invoices = list(await s.scalars(select(Invoice.amount_minor)))
        assert invoices == [49900]
    finally:
        async with container.db.system() as s:
            await s.execute(
                update(PlanVersion).where(PlanVersion.id == pv_id).values(provider_refs={})
            )


# ---- usage reconciliation --------------------------------------------------------------


async def test_usage_reconciler_snapshots_and_restores(container, redis_client) -> None:  # type: ignore[no-untyped-def]
    acct = await _personal(container)
    r = await container.entitlements.reserve(acct, "executions", amount=40, reservation_id="a")
    await container.entitlements.commit(r, actual=40)

    lines = {line.feature: line for line in await container.usage.flush(acct)}
    assert lines["executions"].action == "snapshot" and lines["executions"].redis == 40

    # Simulate Redis data loss: the counter disappears.
    await redis_client.delete(r.keys.used)
    lines = {line.feature: line for line in await container.usage.flush(acct)}
    assert lines["executions"].action == "restored"
    assert await container.entitlements.usage(acct, "executions") == (40, 300)
    async with container.db.tenant(acct) as s:
        assert await s.scalar(select(UsageRecord.quantity)) == 40
