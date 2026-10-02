"""Ports for external billing services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    provider: str
    provider_subscription_id: str
    checkout_url: str


@dataclass(frozen=True, slots=True)
class OrderSession:
    """A one-off payment the client completes in the provider's checkout widget."""

    provider: str
    order_id: str
    amount_minor: int
    currency: str
    public_key: str


class PaymentGateway(Protocol):
    name: str

    async def create_subscription(
        self, *, provider_plan_id: str, notes: dict[str, str], total_count: int
    ) -> CheckoutSession: ...

    async def cancel_subscription(
        self, provider_subscription_id: str, *, at_period_end: bool
    ) -> None: ...

    async def create_order(
        self, *, amount_minor: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> OrderSession: ...
