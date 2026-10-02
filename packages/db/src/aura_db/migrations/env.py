"""Alembic environment (async). URL precedence: -x url=... > AURA_DATABASE_URL."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from aura_db import models  # noqa: F401  (registers tables)
from aura_db.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = (
        context.get_x_argument(as_dictionary=True).get("url")
        or config.get_main_option("sqlalchemy.url")
        or os.environ.get("AURA_DATABASE_URL")
    )
    if not url:
        raise RuntimeError("set AURA_DATABASE_URL or pass -x url=...")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
