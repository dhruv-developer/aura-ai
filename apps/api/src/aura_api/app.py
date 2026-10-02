"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aura_api.container import Container
from aura_api.routes import (
    accounts,
    admin,
    auth,
    billing,
    health,
    members,
    personal,
    policies,
    prompts,
    shadow,
    xray,
)
from aura_billing.application.catalog_sync import sync_catalog
from aura_http import BodySizeLimitMiddleware
from aura_http import install as install_error_handlers
from aura_shared.settings import Settings, get_settings


def create_app(
    container: Container | None = None,
    *,
    settings: Settings | None = None,
    sync_catalog_on_startup: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = container or Container.build(settings or get_settings())
        app.state.container = c
        await c.invalidation.start()
        if sync_catalog_on_startup:
            await sync_catalog(c.db, c.catalog)  # idempotent + advisory-locked
        try:
            yield
        finally:
            if container is None:  # we own it
                await c.close()

    app = FastAPI(title="AURA API", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=1_000_000,
        path_limits=[
            (r"/v1/accounts/[^/]+/xray", 55_000_000),  # authenticated traffic uploads
            (r"/v1/xray/public", 11_000_000),  # public sample funnel
            (r"/v1/accounts/[^/]+/shadow-runs", 55_000_000),  # replay datasets
        ],
    )
    for module in (
        health,
        auth,
        accounts,
        members,
        billing,
        personal,
        xray,
        shadow,
        policies,
        prompts,
        admin,
    ):
        app.include_router(module.router)
    return app
