"""Transactional outbox: enqueue in the same transaction as the state change."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aura_db.models import OutboxMessage

CONFIG_CHANGED = "aura.config.changed"


async def enqueue(
    session: AsyncSession, topic: str, payload: dict[str, Any], *, key: str | None = None
) -> None:
    session.add(OutboxMessage(topic=topic, key=key, payload=payload))
