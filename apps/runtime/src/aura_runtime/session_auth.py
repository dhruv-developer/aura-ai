"""Cookie-session access to the runtime for the AURA web app (the consumer /app and
the dashboard playground). API keys stay the only way in for machines.

The hot path must not hit Postgres per request, so the user's access to an account is
cached briefly (bounded staleness: a removed membership stops working within the TTL;
logout and session revocation take effect immediately because the session itself is
checked in Redis on every request).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from aura_auth.application.identity import IdentityService
from aura_auth.domain.rbac import Permission
from aura_cache import TieredCache
from aura_shared.errors import Forbidden, NotFound


@dataclass(frozen=True, slots=True)
class SessionAccess:
    user_id: str
    account_id: str
    kind: str
    region: str
    workspace_id: str
    can_execute: bool


class SessionAccessResolver:
    def __init__(self, identity: IdentityService, cache: TieredCache) -> None:
        self._identity = identity
        self._cache = cache

    async def resolve(self, user_id: str, account_id: str | None) -> SessionAccess:
        async def load() -> dict[str, Any] | None:
            user = await self._identity.get_user(user_id)
            if user.status != "active":
                return None
            access = await self._identity.list_access(user_id)
            chosen = next(
                (
                    a
                    for a in access
                    if (a.account_id == account_id if account_id else a.kind == "individual")
                ),
                None,
            )
            if chosen is None or chosen.status != "active":
                return None
            return asdict(
                SessionAccess(
                    user_id=user_id,
                    account_id=chosen.account_id,
                    kind=chosen.kind,
                    region=chosen.region,
                    workspace_id=await self._identity.default_workspace_id(chosen.account_id),
                    can_execute=Permission.EXECUTE in chosen.permissions,
                )
            )

        data = await self._cache.get_or_load(
            user_id, "rt_access", account_id or "personal", loader=load
        )
        if data is None:
            raise NotFound("account not found")
        access = SessionAccess(**data)
        if not access.can_execute:
            raise Forbidden("your role cannot run executions in this account")
        return access
