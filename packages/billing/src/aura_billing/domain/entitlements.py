"""Entitlement resolution. Pure: no I/O."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Entitlement:
    feature: str
    enabled: bool
    limit: int | None  # None = unlimited (for metered/gauge); unused for boolean
    period: str | None  # day | month | None


@dataclass(frozen=True, slots=True)
class Override:
    entitlement: Entitlement
    expires_at: datetime | None


class EntitlementSet:
    """Effective entitlements for one account. Missing features are disabled."""

    def __init__(self, plan_code: str | None, items: dict[str, Entitlement]) -> None:
        self.plan_code = plan_code
        self._items = items

    def get(self, feature: str) -> Entitlement | None:
        return self._items.get(feature)

    def allows(self, feature: str) -> bool:
        e = self._items.get(feature)
        return bool(e and e.enabled)

    def limit(self, feature: str) -> int | None:
        """``None`` means unlimited; callers must check ``allows`` first."""
        e = self._items.get(feature)
        return e.limit if e else 0

    def features(self) -> dict[str, Entitlement]:
        return dict(self._items)

    # Serialisation for the L2 cache.
    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan_code,
            "items": {k: [e.enabled, e.limit, e.period] for k, e in sorted(self._items.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntitlementSet:
        return cls(
            data["plan"],
            {k: Entitlement(k, bool(v[0]), v[1], v[2]) for k, v in data["items"].items()},
        )


def resolve(
    plan_code: str | None,
    plan_entitlements: Iterable[Entitlement],
    overrides: Iterable[Override],
    *,
    now: datetime,
) -> EntitlementSet:
    """Plan entitlements, then unexpired account overrides on top (override wins)."""
    items = {e.feature: e for e in plan_entitlements}
    for o in overrides:
        if o.expires_at is None or o.expires_at > now:
            items[o.entitlement.feature] = o.entitlement
    return EntitlementSet(plan_code, items)
