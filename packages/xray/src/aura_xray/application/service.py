"""Run and persist X-Ray reports. Only aggregates are stored; prompt text never is."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from aura_db import Database, audit
from aura_db.models import Lead, XRayReport
from aura_providers.domain.catalog import ModelCatalog
from aura_shared.errors import NotFound, ValidationFailed
from aura_shared.ids import new_id
from aura_xray.domain.parsers import parse
from aura_xray.domain.records import PriceBook, TrafficRecord
from aura_xray.domain.report import build_report

MAX_UPLOAD_BYTES = 50_000_000


@dataclass(frozen=True, slots=True)
class XRayView:
    id: str
    name: str
    source: str
    status: str
    record_count: int
    skipped_count: int
    created_at: datetime | None
    report: dict[str, Any]


def _view(r: XRayReport) -> XRayView:
    return XRayView(
        r.id, r.name, r.source, r.status, r.record_count, r.skipped_count, r.created_at, r.report
    )


def _prices(
    catalog: ModelCatalog, overrides: dict[str, tuple[Decimal, Decimal]] | None
) -> PriceBook:
    return PriceBook(catalog, overrides or {})


class XRayService:
    def __init__(self, db: Database, *, catalog: ModelCatalog) -> None:
        self._db = db
        self._catalog = catalog

    def analyze(
        self,
        records: list[TrafficRecord],
        *,
        price_overrides: dict[str, tuple[Decimal, Decimal]] | None = None,
    ) -> dict[str, Any]:
        return build_report(records, _prices(self._catalog, price_overrides))

    async def run_upload(
        self,
        *,
        account_id: str,
        data: bytes,
        fmt: str,
        name: str,
        actor_user_id: str,
        price_overrides: dict[str, tuple[Decimal, Decimal]] | None = None,
    ) -> XRayView:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValidationFailed(f"upload exceeds {MAX_UPLOAD_BYTES // 1_000_000} MB")
        parsed = parse(data, fmt)
        if not parsed.records:
            raise ValidationFailed("no usable records found", errors=parsed.errors)
        report = self.analyze(parsed.records, price_overrides=price_overrides)
        report["ingestion"] = {"skipped": parsed.skipped, "errors": parsed.errors}
        return await self._store(
            account_id,
            f"upload:{fmt}",
            name,
            len(parsed.records),
            parsed.skipped,
            report,
            actor_user_id,
        )

    async def run_records(
        self,
        *,
        account_id: str,
        records: list[TrafficRecord],
        source: str,
        name: str,
        actor_user_id: str | None,
    ) -> XRayView:
        if not records:
            raise ValidationFailed("no traffic in the selected window")
        return await self._store(
            account_id, source, name, len(records), 0, self.analyze(records), actor_user_id
        )

    async def _store(
        self,
        account_id: str,
        source: str,
        name: str,
        count: int,
        skipped: int,
        report: dict[str, Any],
        actor: str | None,
    ) -> XRayView:
        async with self._db.tenant(account_id) as s:
            row = XRayReport(
                id=new_id("xr"),
                account_id=account_id,
                source=source,
                name=name.strip()[:200] or "X-Ray",
                status="ready",
                record_count=count,
                skipped_count=skipped,
                report=report,
                created_by=actor,
            )
            s.add(row)
            await s.flush()
            await s.refresh(row)
            await audit.record(
                s,
                actor_type="user" if actor else "system",
                actor_id=actor,
                account_id=account_id,
                action="xray.run",
                target_type="xray_report",
                target_id=row.id,
                after={"records": count, "source": source},
            )
            return _view(row)

    async def list(self, account_id: str) -> list[XRayView]:
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(select(XRayReport).order_by(XRayReport.created_at.desc()))
            return [_view(r) for r in rows]

    async def get(self, account_id: str, report_id: str) -> XRayView:
        async with self._db.tenant(account_id) as s:
            row = await s.get(XRayReport, report_id)
            if row is None:
                raise NotFound("report not found")
            return _view(row)

    async def public_sample(
        self, *, email: str, company: str | None, data: bytes, fmt: str
    ) -> dict[str, Any]:
        """Public funnel: analyze a sample, record a lead with headline numbers only."""
        if len(data) > 10_000_000:
            raise ValidationFailed("public samples are limited to 10 MB")
        parsed = parse(data, fmt)
        if not parsed.records:
            raise ValidationFailed("no usable records found", errors=parsed.errors)
        report = self.analyze(parsed.records)
        s = report["summary"]
        async with self._db.system() as session:  # platform-level CRM data, not tenant data
            session.add(
                Lead(
                    id=new_id("lead"),
                    email=email,
                    company=(company or "").strip()[:200] or None,
                    source="xray_public",
                    summary={
                        "requests": s["requests"],
                        "monthly_spend_usd": s["monthly_spend_usd"],
                        "est_monthly_savings_usd": s["est_monthly_savings_usd"],
                        "models": [m["model"] for m in report["models"][:5]],
                    },
                )
            )
        return report
