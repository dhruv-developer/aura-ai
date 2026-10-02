import hashlib
import hmac
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from aura_billing.domain import razorpay
from aura_billing.domain.catalog import CatalogError, load_catalog, parse_catalog
from aura_billing.domain.entitlements import Entitlement, EntitlementSet, Override, resolve
from aura_billing.domain.ledger import TxnKind, customer_delta, postings_for
from aura_billing.domain.periods import counter_expiry_ms, period_end, period_label
from aura_billing.domain.subscriptions import SubscriptionStatus as S
from aura_billing.domain.subscriptions import can_transition, transition
from aura_shared.errors import Conflict, ValidationFailed


def test_shipped_catalog_is_valid_and_coherent() -> None:
    cat = load_catalog()
    assert cat.default_plan_for("individual").code == "free"
    assert cat.default_plan_for("organization").code == "startup"
    assert {p.audience for p in cat.plans.values()} == {"b2c", "b2b"}
    # Higher B2C tiers never lose a capability the tier below has.
    tiers = [cat.plans[c] for c in ("free", "plus", "pro")]
    for lower, higher in itertools.pairwise(tiers):
        lower_on = {e.feature for e in lower.entitlements if e.enabled}
        higher_on = {e.feature for e in higher.entitlements if e.enabled}
        assert lower_on <= higher_on, (lower.code, higher.code)


def _base(**plan_overrides):  # type: ignore[no-untyped-def]
    plan = {
        "name": "P",
        "audience": "b2c",
        "priority_class": "free",
        "price_minor": 0,
        "entitlements": {"flag": True, "runs": {"limit": 10, "period": "month"}},
    }
    plan.update(plan_overrides)
    return {
        "features": {
            "flag": {"kind": "boolean"},
            "runs": {"kind": "metered"},
            "seats": {"kind": "gauge"},
        },
        "plans": {"p": plan},
        "defaults": {"individual": "p"},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["plans"]["p"]["entitlements"].update({"ghost": True}),
        lambda d: d["plans"]["p"]["entitlements"].update({"flag": {"limit": 1}}),
        lambda d: d["plans"]["p"]["entitlements"].update({"runs": {"limit": 5}}),  # no period
        lambda d: d["plans"]["p"]["entitlements"].update({"runs": {"limit": -1, "period": "day"}}),
        lambda d: d["plans"]["p"]["entitlements"].update({"seats": {"limit": 1, "period": "day"}}),
        lambda d: d["plans"]["p"].update({"audience": "b2x"}),
        lambda d: d.update({"defaults": {"individual": "nope"}}),
        lambda d: d.update({"defaults": {"organization": "p"}}),  # b2c plan for orgs
    ],
)
def test_catalog_validation_rejects_bad_input(mutate) -> None:  # type: ignore[no-untyped-def]
    data = _base()
    mutate(data)
    with pytest.raises(CatalogError):
        parse_catalog(data)


def test_content_hash_tracks_customer_affecting_changes_only() -> None:
    h = parse_catalog(_base()).plans["p"].content_hash()
    assert parse_catalog(_base(name="Renamed")).plans["p"].content_hash() == h
    assert parse_catalog(_base(price_minor=100)).plans["p"].content_hash() != h
    changed = _base(entitlements={"flag": True, "runs": {"limit": 11, "period": "month"}})
    assert parse_catalog(changed).plans["p"].content_hash() != h


def test_overrides_win_until_they_expire() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    plan = [Entitlement("runs", True, 10, "month"), Entitlement("flag", False, None, None)]
    overrides = [
        Override(Entitlement("runs", True, 1000, "month"), now + timedelta(days=1)),
        Override(Entitlement("flag", True, None, None), now - timedelta(seconds=1)),  # expired
    ]
    ents = resolve("p", plan, overrides, now=now)
    assert ents.limit("runs") == 1000 and not ents.allows("flag") and not ents.allows("ghost")
    assert EntitlementSet.from_dict(ents.to_dict()).to_dict() == ents.to_dict()


def test_credit_packs_validated() -> None:
    assert load_catalog().credit_packs["small"].credits == 10_000
    bad = _base()
    bad["credit_packs"] = {"x": {"credits": 0, "price_minor": 100}}
    with pytest.raises(CatalogError):
        parse_catalog(bad)


def test_periods() -> None:
    now = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
    assert period_label("month", now) == "2026-12" and period_label("day", now) == "2026-12-31"
    assert period_label(None, now) == "all"
    assert period_end("month", now) == datetime(2027, 1, 1, tzinfo=UTC)
    assert period_end("day", now) == datetime(2027, 1, 1, tzinfo=UTC)
    assert counter_expiry_ms(None, now) == 0
    assert counter_expiry_ms("day", now) == (30 * 60 + 35 * 86400) * 1000


def test_subscription_state_machine() -> None:
    assert can_transition(S.INCOMPLETE, S.ACTIVE)
    assert can_transition(S.ACTIVE, S.ACTIVE)
    assert not can_transition(S.CANCELED, S.ACTIVE)  # no resurrection
    assert not can_transition(S.INCOMPLETE, S.PAST_DUE)
    with pytest.raises(Conflict):
        transition(S.CANCELED, S.ACTIVE)


def test_ledger_postings_always_balance() -> None:
    for kind in TxnKind:
        amounts = (5, -5) if kind is TxnKind.ADJUST else (5,)
        for amount in amounts:
            a, b = postings_for(kind, amount)
            assert a.book == "customer" and a.amount + b.amount == 0
    assert customer_delta(TxnKind.CONSUME, 7) == -7
    assert customer_delta(TxnKind.GRANT, 7) == 7
    with pytest.raises(ValidationFailed):
        postings_for(TxnKind.CONSUME, -1)
    with pytest.raises(ValidationFailed):
        postings_for(TxnKind.ADJUST, 0)


def test_razorpay_signature_and_normalisation() -> None:
    body = b'{"event":"subscription.charged"}'
    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert razorpay.verify_signature(body, sig, "secret")
    assert not razorpay.verify_signature(body, sig, "other")
    assert not razorpay.verify_signature(body + b" ", sig, "secret")
    assert not razorpay.verify_signature(body, "", "secret")

    evt = razorpay.normalize(
        {
            "event": "subscription.charged",
            "payload": {
                "subscription": {
                    "entity": {
                        "id": "sub_X",
                        "current_start": 1790000000,
                        "current_end": 1792600000,
                        "notes": {"account_id": "acct_1"},
                    }
                }
            },
        }
    )
    assert evt.kind is razorpay.GatewayEventKind.SUBSCRIPTION_CHARGED
    assert evt.subscription_id == "sub_X" and evt.notes == {"account_id": "acct_1"}
    assert evt.period_end == datetime.fromtimestamp(1792600000, UTC)
    assert razorpay.normalize({"event": "payment.failed"}).kind is razorpay.GatewayEventKind.IGNORED
    order = razorpay.normalize(
        {
            "event": "order.paid",
            "payload": {"order": {"entity": {"id": "order_1", "amount_paid": 19900}}},
        }
    )
    assert order.kind is razorpay.GatewayEventKind.ORDER_PAID
    assert (order.order_id, order.amount_minor) == ("order_1", 19900)
