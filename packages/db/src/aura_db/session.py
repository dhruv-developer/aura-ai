"""Session factory with tenant isolation.

Two Postgres group roles are created by the initial migration:

- ``aura_app``: subject to Row-Level Security on every tenant table. Its policy only
  exposes rows where ``account_id = current_setting('aura.account_id')``.
- ``aura_system``: a permissive policy for cross-tenant control-plane work.

The login user must be a member of both roles. Each transaction switches with
``SET LOCAL ROLE``, so a pooled connection never leaks a role or tenant into the next
transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

APP_ROLE = "aura_app"
SYSTEM_ROLE = "aura_system"


class Database:
    def __init__(
        self, url: str, *, pool_size: int = 10, max_overflow: int = 10, echo: bool = False
    ) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            echo=echo,
        )
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def tenant(self, account_id: str) -> AsyncIterator[AsyncSession]:
        """A transaction that can only see ``account_id``'s tenant rows."""
        async with self._sessions() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
            await session.execute(
                text("SELECT set_config('aura.account_id', :a, true)"), {"a": account_id}
            )
            yield session

    @asynccontextmanager
    async def system(self) -> AsyncIterator[AsyncSession]:
        """A cross-tenant transaction. Use only for control-plane operations that must
        span tenants (signup, webhooks, API-key lookup, superadmin)."""
        async with self._sessions() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {SYSTEM_ROLE}"))
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
