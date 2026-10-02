"""Razorpay webhook verification and normalisation. Pure."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class GatewayEventKind(StrEnum):
    SUBSCRIPTION_ACTIVATED = "subscription_activated"
    SUBSCRIPTION_CHARGED = "subscription_charged"
    SUBSCRIPTION_PAST_DUE = "subscription_past_due"
    SUBSCRIPTION_PAUSED = "subscription_paused"
    SUBSCRIPTION_RESUMED = "subscription_resumed"
    SUBSCRIPTION_CANCELED = "subscription_canceled"
    INVOICE_PAID = "invoice_paid"
    ORDER_PAID = "order_paid"  # one-off purchases (credit packs)
    IGNORED = "ignored"


_MAP = {
    "subscription.activated": GatewayEventKind.SUBSCRIPTION_ACTIVATED,
    "subscription.charged": GatewayEventKind.SUBSCRIPTION_CHARGED,
    "subscription.pending": GatewayEventKind.SUBSCRIPTION_PAST_DUE,
    "subscription.halted": GatewayEventKind.SUBSCRIPTION_PAST_DUE,
    "subscription.paused": GatewayEventKind.SUBSCRIPTION_PAUSED,
    "subscription.resumed": GatewayEventKind.SUBSCRIPTION_RESUMED,
    "subscription.cancelled": GatewayEventKind.SUBSCRIPTION_CANCELED,
    "subscription.completed": GatewayEventKind.SUBSCRIPTION_CANCELED,
    "invoice.paid": GatewayEventKind.INVOICE_PAID,
    "order.paid": GatewayEventKind.ORDER_PAID,
}


@dataclass(frozen=True, slots=True)
class GatewayEvent:
    kind: GatewayEventKind
    raw_type: str
    subscription_id: str | None
    period_start: datetime | None = None
    period_end: datetime | None = None
    notes: dict[str, str] | None = None
    invoice_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    invoice_url: str | None = None
    issued_at: datetime | None = None
    order_id: str | None = None


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def _entity(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return (payload.get("payload", {}).get(name) or {}).get("entity") or {}


def normalize(payload: dict[str, Any]) -> GatewayEvent:
    raw_type = str(payload.get("event", ""))
    kind = _MAP.get(raw_type, GatewayEventKind.IGNORED)
    if kind is GatewayEventKind.INVOICE_PAID:
        inv = _entity(payload, "invoice")
        return GatewayEvent(
            kind,
            raw_type,
            inv.get("subscription_id"),
            invoice_id=inv.get("id"),
            amount_minor=inv.get("amount_paid", inv.get("amount")),
            currency=inv.get("currency"),
            invoice_url=inv.get("short_url"),
            issued_at=_ts(inv.get("issued_at") or inv.get("paid_at")),
        )
    if kind is GatewayEventKind.ORDER_PAID:
        order = _entity(payload, "order")
        return GatewayEvent(
            kind,
            raw_type,
            None,
            notes={str(k): str(v) for k, v in (order.get("notes") or {}).items()} or None,
            amount_minor=order.get("amount_paid"),
            currency=order.get("currency"),
            order_id=order.get("id"),
        )
    sub = _entity(payload, "subscription")
    return GatewayEvent(
        kind,
        raw_type,
        sub.get("id"),
        period_start=_ts(sub.get("current_start")),
        period_end=_ts(sub.get("current_end")),
        notes={str(k): str(v) for k, v in (sub.get("notes") or {}).items()} or None,
    )
