"""The single gate for features and metered limits (spec §37: never `if plan == ...`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aura_billing.domain.catalog import Catalog
from aura_billing.domain.entitlements import Entitlement, EntitlementSet, Override, resolve
from aura_billing.domain.periods import counter_expiry_ms, period_label
from aura_billing.domain.subscriptions import LIVE
from aura_cache import TieredCache
from aura_db import Database, outbox
from aura_db.models import (
    Account,
    EntitlementOverride,
    Plan,
    PlanEntitlement,
    PlanVersion,
    Subscription,
)
from aura_shared.errors import Forbidden, QuotaExceeded, ValidationFailed
from aura_shared.redis import KeyFamily, RedisScripts
from aura_shared.redis.scripts import QuotaKeys


@dataclass(frozen=True, slots=True)
class Reservation:
    account_id: str
    feature: str
    reservation_id: str
    keys: QuotaKeys


class EntitlementService:
    def __init__(
        self, db: Database, *, cache: TieredCache, scripts: RedisScripts, catalog: Catalog
    ) -> None:
        if cache.family is not KeyFamily.ENTITLEMENTS:
            raise ValueError("EntitlementService needs a TieredCache for KeyFamily.ENTITLEMENTS")
        self._db = db
        self._cache = cache
        self._scripts = scripts
        self._catalog = catalog

    # ---- reads -----------------------------------------------------------------------
    async def get(self, account_id: str) -> EntitlementSet:
        async def load() -> dict[str, Any]:
            return (await self._load(account_id)).to_dict()

        return EntitlementSet.from_dict(
            await self._cache.get_or_load(account_id, "set", loader=load)
        )

    async def _load(self, account_id: str) -> EntitlementSet:
        async with self._db.tenant(account_id) as s:
            row = (
                await s.execute(
                    select(Subscription.plan_version_id, PlanVersion.plan_code)
                    .join(PlanVersion, PlanVersion.id == Subscription.plan_version_id)
                    .where(Subscription.status.in_([st.value for st in LIVE]))
                )
            ).first()
            pv_id: int | None
            plan_code: str | None
            if row is not None:
                pv_id, plan_code = row
            else:
                pv_id, plan_code = await self._fallback_plan(s, account_id)
            plan_ents: list[Entitlement] = []
            if pv_id is not None:
                for pe in await s.scalars(
                    select(PlanEntitlement).where(PlanEntitlement.plan_version_id == pv_id)
                ):
                    plan_ents.append(Entitlement(pe.feature_key, pe.enabled, pe.limit, pe.period))
            overrides = [
                Override(Entitlement(o.feature_key, o.enabled, o.limit, o.period), o.expires_at)
                for o in await s.scalars(select(EntitlementOverride))
            ]
        return resolve(plan_code, plan_ents, overrides, now=datetime.now(UTC))

    async def _fallback_plan(
        self, s: AsyncSession, account_id: str
    ) -> tuple[int | None, str | None]:
        """No live subscription: individuals fall back to the default free plan;
        organizations get nothing until they subscribe."""
        kind = await s.scalar(select(Account.kind).where(Account.id == account_id))
        if kind != "individual":
            return None, None
        code = self._catalog.default_plan_for("individual").code
        pv_id = await s.scalar(select(Plan.current_version_id).where(Plan.code == code))
        return pv_id, code

    async def require(self, account_id: str, feature: str) -> EntitlementSet:
        ents = await self.get(account_id)
        if not ents.allows(feature):
            raise Forbidden(f"'{feature}' is not included in your plan", feature=feature)
        return ents

    # ---- metered limits: reserve -> commit | release --------------------------------
    def _keys(self, account_id: str, e: Entitlement, now: datetime) -> QuotaKeys:
        return QuotaKeys.for_(account_id, e.feature, period_label(e.period, now))

    async def reserve(
        self,
        account_id: str,
        feature: str,
        *,
        amount: int,
        reservation_id: str,
        now: datetime | None = None,
    ) -> Reservation:
        if amount <= 0:
            raise ValidationFailed("amount must be positive")
        now = now or datetime.now(UTC)
        ents = await self.require(account_id, feature)
        e = ents.get(feature)
        assert e is not None
        spec = self._catalog.features.get(feature)
        if spec is None or spec.kind == "boolean":
            raise ValidationFailed(f"'{feature}' is not a metered feature")
        keys = self._keys(account_id, e, now)
        res = await self._scripts.quota_reserve(
            keys,
            limit=e.limit,
            amount=amount,
            reservation_id=reservation_id,
            period_ms=counter_expiry_ms(e.period, now),
        )
        if not res.ok:
            raise QuotaExceeded(
                f"'{feature}' limit reached for this period",
                feature=feature,
                limit=e.limit,
                used=res.used,
            )
        return Reservation(account_id, feature, reservation_id, keys)

    async def commit(self, r: Reservation, *, actual: int) -> int:
        return (
            await self._scripts.quota_commit(r.keys, reservation_id=r.reservation_id, actual=actual)
        ).used

    async def release(self, r: Reservation) -> int:
        return (await self._scripts.quota_release(r.keys, reservation_id=r.reservation_id)).used

    async def usage(
        self, account_id: str, feature: str, *, now: datetime | None = None
    ) -> tuple[int, int | None]:
        """(used, limit) for the current period. ``limit`` None = unlimited."""
        now = now or datetime.now(UTC)
        ents = await self.get(account_id)
        e = ents.get(feature)
        if e is None or not e.enabled:
            return 0, 0
        return await self._scripts.quota_used(self._keys(account_id, e, now)), e.limit

    # ---- invalidation ----------------------------------------------------------------
    async def enqueue_invalidation(self, session: AsyncSession, account_id: str) -> None:
        """Call inside the transaction that changed entitlements. Other pods drop their
        L1 copy when the outbox relay broadcasts this."""
        await outbox.enqueue(
            session,
            outbox.CONFIG_CHANGED,
            {"kind": "entitlements", "account_id": account_id},
            key=self._cache.key(account_id, "set"),
        )

    async def invalidate_cache(self, account_id: str) -> None:
        """Call after the transaction commits."""
        await self._cache.invalidate(account_id, "set")
