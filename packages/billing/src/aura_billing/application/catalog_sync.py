"""Idempotent catalog sync: YAML → features, plans, immutable plan versions."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from aura_billing.domain.catalog import Catalog
from aura_db import Database
from aura_db.models import Feature, Plan, PlanEntitlement, PlanVersion


@dataclass(frozen=True, slots=True)
class SyncResult:
    plan_code: str
    version: int
    created: bool


async def sync_catalog(db: Database, catalog: Catalog) -> list[SyncResult]:
    results: list[SyncResult] = []
    async with db.system() as s:
        # Serialise concurrent syncs (several pods starting at once).
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtext('aura.catalog_sync'))"))
        for f in catalog.features.values():
            stmt = insert(Feature).values(
                key=f.key, kind=f.kind, unit=f.unit, description=f.description
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Feature.key],
                    set_={"kind": f.kind, "unit": f.unit, "description": f.description},
                )
            )
        for p in catalog.plans.values():
            values = {
                "name": p.name,
                "audience": p.audience,
                "priority_class": p.priority_class,
                "is_public": p.is_public,
                "sort_order": p.sort_order,
            }
            await s.execute(
                insert(Plan)
                .values(code=p.code, **values)
                .on_conflict_do_update(index_elements=[Plan.code], set_=values)
            )
            latest = await s.scalar(
                select(PlanVersion)
                .where(PlanVersion.plan_code == p.code)
                .order_by(PlanVersion.version.desc())
                .limit(1)
            )
            digest = p.content_hash()
            if latest is not None and latest.content_hash == digest:
                results.append(SyncResult(p.code, latest.version, False))
                continue
            next_version = (
                await s.scalar(
                    select(func.coalesce(func.max(PlanVersion.version), 0)).where(
                        PlanVersion.plan_code == p.code
                    )
                )
                or 0
            ) + 1
            pv = PlanVersion(
                plan_code=p.code,
                version=next_version,
                price_minor=p.price_minor,
                currency=p.currency,
                interval=p.interval,
                trial_days=p.trial_days,
                content_hash=digest,
                # Carry provider plan ids forward. If the price changed, an operator
                # must set a new provider plan before checkout is possible again.
                provider_refs=(
                    dict(latest.provider_refs)
                    if latest is not None and latest.price_minor == p.price_minor
                    else {}
                ),
            )
            s.add(pv)
            await s.flush()
            for e in p.entitlements:
                s.add(
                    PlanEntitlement(
                        plan_version_id=pv.id,
                        feature_key=e.feature,
                        enabled=e.enabled,
                        limit=e.limit,
                        period=e.period,
                    )
                )
            plan_row = await s.get(Plan, p.code)
            assert plan_row is not None
            plan_row.current_version_id = pv.id
            results.append(SyncResult(p.code, next_version, True))
    return results
