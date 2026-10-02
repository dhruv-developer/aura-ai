from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from aura_api.deps import ContainerDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/readyz")
async def readyz(c: ContainerDep) -> JSONResponse:
    checks: dict[str, Any] = {}
    try:
        async with c.db.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:  # noqa: BLE001 - readiness must report, not raise
        checks["postgres"] = False
    checks.update({f"redis_{role.value}": ok for role, ok in (await c.redis.ping()).items()})
    ok = all(checks.values())
    return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)
