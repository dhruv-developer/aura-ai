"""Transactional outbox relay: publish committed outbox rows, mark them published.

Concurrency-safe across replicas (``FOR UPDATE SKIP LOCKED``). Delivery is at-least-once:
if the commit fails after publishing, rows are re-published, and consumers are
idempotent (cache invalidation trivially so).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from aura_db import Database
from aura_db.models import OutboxMessage
from aura_db.outbox import CONFIG_CHANGED

log = logging.getLogger("aura.workers.outbox")

Handler = Callable[[str, str | None, dict[str, Any]], Awaitable[None]]


class OutboxRelay:
    def __init__(self, db: Database, handler: Handler, *, batch: int = 500) -> None:
        self._db = db
        self._handler = handler
        self._batch = batch

    async def relay_once(self) -> int:
        async with self._db.system() as s:
            rows = list(
                await s.scalars(
                    select(OutboxMessage)
                    .where(OutboxMessage.published_at.is_(None))
                    .order_by(OutboxMessage.id)
                    .limit(self._batch)
                    .with_for_update(skip_locked=True)
                )
            )
            now = datetime.now(UTC)
            for row in rows:
                await self._handler(row.topic, row.key, row.payload)
                row.published_at = now
            return len(rows)

    async def purge(self, *, older_than: timedelta = timedelta(days=7)) -> int:
        async with self._db.system() as s:
            result = await s.execute(
                delete(OutboxMessage).where(
                    OutboxMessage.published_at.is_not(None),
                    OutboxMessage.published_at < datetime.now(UTC) - older_than,
                )
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def run(self, *, idle_sleep_s: float = 0.5) -> None:
        while True:
            try:
                n = await self.relay_once()
            except Exception:
                log.exception("outbox relay batch failed")
                n = 0
            if n == 0:
                await asyncio.sleep(idle_sleep_s)


def invalidation_handler(publish: Callable[[str], Awaitable[None]]) -> Handler:
    """Route config-change rows to the cache invalidation bus."""

    async def handle(topic: str, key: str | None, payload: dict[str, Any]) -> None:
        if topic == CONFIG_CHANGED and key:
            await publish(key)

    return handle
