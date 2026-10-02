"""Credit pricing for consumer (B2C) usage on AURA's platform keys. Pure.

One credit is ``1 / CREDITS_PER_USD`` dollars of provider cost. Calls made with the
user's own key (BYOK) or served from cache cost no credits: the user already pays
the provider, or nobody pays at all.
"""

from __future__ import annotations

import math
from decimal import Decimal

CREDITS_PER_USD = 10_000  # 1 credit = $0.0001 of inference


def credits_for_cost(cost_usd: Decimal, *, rate: int = CREDITS_PER_USD) -> int:
    """Round up so many tiny calls cannot each round to zero, and charge at least one
    credit for any paid call."""
    if cost_usd <= 0:
        return 0
    return max(1, math.ceil(cost_usd * rate))


def billable(credential_source: str) -> bool:
    return credential_source == "platform"
