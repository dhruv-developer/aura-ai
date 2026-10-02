"""Reconcile hot Redis usage counters with the durable Postgres snapshot.

Counters only ever increase within a period, so the higher value is correct in both
directions:
- Redis > Postgres: normal operation. Snapshot the Redis value into Postgres.
- Postgres > Redis: Redis lost data (failover or restart). Raise Redis back up so limits
  keep being enforced.

The snapshot includes in-flight reservations, so it can briefly over-count by at most
the reserved amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from aura_billing.application.entitlements import EntitlementService
from aura_billing.domain.catalog import Catalog
from aura_billing.domain.periods import counter_expiry_ms, period_label
from aura_db import Database
from aura_db.models import UsageRecord
from aura_shared.redis import RedisScripts
from aura_shared.redis.scripts import QuotaKeys


@dataclass(frozen=True, slots=True)
class ReconcileLine:
    feature: str
    period: str
    redis: int
    postgres: int
    action: str  # snapshot | restored | in_sync


class UsageReconciler:
    def __init__(
        self,
        db: Database,
        *,
        scripts: RedisScripts,
        entitlements: EntitlementService,
        catalog: Catalog,
    ) -> None:
        self._db = db
        self._scripts = scripts
        self._ent = entitlements
        self._catalog = catalog

    async def flush(self, account_id: str, *, now: datetime | None = None) -> list[ReconcileLine]:
        now = now or datetime.now(UTC)
        ents = await self._ent.get(account_id)
        lines: list[ReconcileLine] = []
        async with self._db.tenant(account_id) as s:
            for feature, e in sorted(ents.features().items()):
                spec = self._catalog.features.get(feature)
                if spec is None or spec.kind == "boolean" or not e.enabled:
                    continue
                label = period_label(e.period, now)
                keys = QuotaKeys.for_(account_id, feature, label)
                hot = await self._scripts.quota_used(keys)
                durable = (
                    await s.scalar(
                        select(UsageRecord.quantity).where(
                            UsageRecord.metric == feature, UsageRecord.period == label
                        )
                    )
                    or 0
                )
                if hot > durable:
                    await s.execute(
                        insert(UsageRecord)
                        .values(account_id=account_id, metric=feature, period=label, quantity=hot)
                        .on_conflict_do_update(
                            index_elements=[
                                UsageRecord.account_id,
                                UsageRecord.metric,
                                UsageRecord.period,
                            ],
                            set_={
                                "quantity": func.greatest(UsageRecord.quantity, hot),
                                "updated_at": func.now(),
                            },
                        )
                    )
                    action = "snapshot"
                elif durable > hot:
                    await self._scripts.counter_raise(
                        keys.used, durable, expire_ms=counter_expiry_ms(e.period, now)
                    )
                    action = "restored"
                else:
                    action = "in_sync"
                if action != "in_sync" or hot:
                    lines.append(ReconcileLine(feature, label, hot, durable, action))
        return lines
