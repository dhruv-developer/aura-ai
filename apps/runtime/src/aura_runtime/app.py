"""Runtime application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aura_http import BodySizeLimitMiddleware
from aura_http import install as install_error_handlers
from aura_runtime.chat import router as chat_router
from aura_runtime.container import RuntimeContainer
from aura_runtime.routes import router
from aura_shared.settings import Settings, get_settings


def create_app(
    container: RuntimeContainer | None = None, *, settings: Settings | None = None
) -> FastAPI:
    s = container.settings if container else (settings or get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = container or RuntimeContainer.build(s)
        app.state.container = c
        await c.invalidation.start()
        try:
            yield
        finally:
            if container is None:
                await c.close()

    app = FastAPI(title="AURA Runtime", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=s.runtime_max_request_bytes)
    app.include_router(router)
    app.include_router(chat_router)
    return app
