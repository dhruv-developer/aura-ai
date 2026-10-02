from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from aura_api.deps import ContainerDep, UserDep, account_access
from aura_auth.application.dto import AccountAccess
from aura_auth.domain.emails import normalize_email
from aura_auth.domain.rbac import Permission
from aura_shared.errors import RateLimited, ValidationFailed
from aura_shared.redis import KeyFamily, build_key
from aura_xray.application.html import render_html

router = APIRouter(prefix="/v1", tags=["xray"])

WorkloadsRead = Annotated[AccountAccess, Depends(account_access(Permission.WORKLOADS_READ))]
WorkloadsManage = Annotated[AccountAccess, Depends(account_access(Permission.WORKLOADS_MANAGE))]


def _summary(v: Any) -> dict[str, Any]:
    return {
        "id": v.id,
        "name": v.name,
        "source": v.source,
        "status": v.status,
        "record_count": v.record_count,
        "skipped_count": v.skipped_count,
        "created_at": v.created_at,
        "summary": v.report.get("summary", {}),
    }


def _price_overrides(raw: str | None) -> dict[str, tuple[Decimal, Decimal]]:
    """``model=input/output`` pairs (USD per MTok), comma separated."""
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        try:
            model, prices = part.split("=", 1)
            inp, outp = prices.split("/", 1)
            out[model.strip()] = (Decimal(inp), Decimal(outp))
        except (ValueError, InvalidOperation) as exc:
            raise ValidationFailed("prices must look like 'gpt-x=2.5/10,other=1/4'") from exc
    return out


@router.post("/accounts/{account_id}/xray", status_code=201)
async def upload_xray(
    access: WorkloadsManage,
    user: UserDep,
    c: ContainerDep,
    file: Annotated[UploadFile, File()],
    format: Annotated[str, Form()] = "jsonl",
    name: Annotated[str, Form()] = "",
    prices: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    await c.entitlements.require(access.account_id, "workload_analytics")
    data = await file.read()
    view = await c.xray.run_upload(
        account_id=access.account_id,
        data=data,
        fmt=format,
        name=name or file.filename or "Uploaded traffic",
        actor_user_id=user.id,
        price_overrides=_price_overrides(prices),
    )
    return _summary(view) | {"report": view.report}


class GatewayXRayIn(BaseModel):
    days: int = Field(default=7, ge=1, le=90)


@router.post("/accounts/{account_id}/xray/gateway", status_code=201)
async def gateway_xray(
    body: GatewayXRayIn, access: WorkloadsManage, user: UserDep, c: ContainerDep
) -> dict[str, Any]:
    await c.entitlements.require(access.account_id, "workload_analytics")
    end = datetime.now(UTC)
    records = await c.traffic.fetch(access.account_id, end - timedelta(days=body.days), end)
    view = await c.xray.run_records(
        account_id=access.account_id,
        records=records,
        source="gateway",
        name=f"Gateway traffic, last {body.days} days",
        actor_user_id=user.id,
    )
    return _summary(view) | {"report": view.report}


@router.get("/accounts/{account_id}/xray")
async def list_xray(access: WorkloadsRead, c: ContainerDep) -> list[dict[str, Any]]:
    return [_summary(v) for v in await c.xray.list(access.account_id)]


@router.get("/accounts/{account_id}/xray/{report_id}")
async def get_xray(report_id: str, access: WorkloadsRead, c: ContainerDep) -> dict[str, Any]:
    v = await c.xray.get(access.account_id, report_id)
    return _summary(v) | {"report": v.report}


@router.get("/accounts/{account_id}/xray/{report_id}/report.html", response_class=HTMLResponse)
async def xray_html(report_id: str, access: WorkloadsRead, c: ContainerDep) -> HTMLResponse:
    v = await c.xray.get(access.account_id, report_id)
    return HTMLResponse(render_html(v.name, v.report))


@router.post("/xray/public")
async def public_xray(
    request: Request,
    c: ContainerDep,
    email: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    format: Annotated[str, Form()] = "jsonl",
    company: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Unauthenticated "Get your AI Workload Report" funnel (spec §25)."""
    ip = request.client.host if request.client else "unknown"
    digest = hashlib.sha256(ip.encode()).hexdigest()[:24]
    rl = await c.scripts.rate_limit(
        build_key(KeyFamily.RATE_LIMIT, "_global", "xray_public", digest),
        rate_per_s=5 / 3600,
        burst=5,
    )
    if not rl.allowed:
        raise RateLimited("too many reports requested", retry_after_ms=rl.retry_after_ms)
    report = await c.xray.public_sample(
        email=normalize_email(email), company=company, data=await file.read(), fmt=format
    )
    return {"report": report}
