"""RFC 7807 problem+json error handling."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from aura_shared.errors import AuraError, RateLimited

log = logging.getLogger("aura.http")
PROBLEM = "application/problem+json"


def problem_body(exc: AuraError, instance: str | None = None) -> dict[str, object]:
    return exc.to_problem(instance=instance)


def install(app: FastAPI) -> None:
    @app.exception_handler(AuraError)
    async def aura_error(request: Request, exc: AuraError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimited) and "retry_after_ms" in exc.extra:
            headers["Retry-After"] = str(max(1, int(exc.extra["retry_after_ms"]) // 1000))
        return JSONResponse(
            exc.to_problem(instance=request.url.path),
            status_code=exc.status,
            media_type=PROBLEM,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "type": "https://docs.aura.ai/errors/validation_failed",
                "title": "Validation failed",
                "status": 422,
                "code": "validation_failed",
                "instance": request.url.path,
                "errors": [
                    {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ],
            },
            status_code=422,
            media_type=PROBLEM,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            {
                "type": "https://docs.aura.ai/errors/internal_error",
                "title": "Internal error",
                "status": 500,
                "code": "internal_error",
            },
            status_code=500,
            media_type=PROBLEM,
        )
