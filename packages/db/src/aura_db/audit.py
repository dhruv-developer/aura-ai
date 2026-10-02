"""Append-only audit log writer. Call inside the same transaction as the change."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aura_db.models import AuditLog
from aura_shared.ids import new_id


async def record(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    account_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    session.add(
        AuditLog(
            id=new_id("aud"),
            actor_type=actor_type,
            actor_id=actor_id,
            account_id=account_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=before,
            after=after,
            ip=ip,
        )
    )
