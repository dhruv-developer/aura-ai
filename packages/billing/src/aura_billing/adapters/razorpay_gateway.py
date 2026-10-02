"""Razorpay Subscriptions API adapter."""

from __future__ import annotations

import httpx

from aura_billing.application.ports import CheckoutSession, OrderSession
from aura_shared.errors import DependencyUnavailable

API = "https://api.razorpay.com/v1"


class RazorpayGateway:
    name = "razorpay"

    def __init__(self, key_id: str, key_secret: str, http: httpx.AsyncClient) -> None:
        self._key_id = key_id
        self._auth = httpx.BasicAuth(key_id, key_secret)
        self._http = http

    async def create_subscription(
        self, *, provider_plan_id: str, notes: dict[str, str], total_count: int
    ) -> CheckoutSession:
        resp = await self._http.post(
            f"{API}/subscriptions",
            auth=self._auth,
            json={
                "plan_id": provider_plan_id,
                "total_count": total_count,
                "customer_notify": 1,
                "notes": notes,
            },
            timeout=15,
        )
        if resp.status_code >= 300:
            raise DependencyUnavailable("payment provider rejected the checkout request")
        data = resp.json()
        return CheckoutSession(self.name, data["id"], data["short_url"])

    async def cancel_subscription(
        self, provider_subscription_id: str, *, at_period_end: bool
    ) -> None:
        resp = await self._http.post(
            f"{API}/subscriptions/{provider_subscription_id}/cancel",
            auth=self._auth,
            json={"cancel_at_cycle_end": 1 if at_period_end else 0},
            timeout=15,
        )
        if resp.status_code >= 300:
            raise DependencyUnavailable("payment provider rejected the cancel request")

    async def create_order(
        self, *, amount_minor: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> OrderSession:
        resp = await self._http.post(
            f"{API}/orders",
            auth=self._auth,
            json={
                "amount": amount_minor,
                "currency": currency,
                "receipt": receipt[:40],
                "notes": notes,
            },
            timeout=15,
        )
        if resp.status_code >= 300:
            raise DependencyUnavailable("payment provider rejected the order request")
        data = resp.json()
        return OrderSession(self.name, data["id"], amount_minor, currency, self._key_id)
