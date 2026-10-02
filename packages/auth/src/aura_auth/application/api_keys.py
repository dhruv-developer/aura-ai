"""Issue, list, revoke and authenticate API keys."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select

from aura_auth.application.dto import ApiKeyPrincipal, ApiKeyView
from aura_auth.domain import api_keys as keys
from aura_auth.domain.rbac import Permission, validate_api_key_permissions
from aura_cache import TieredCache
from aura_db import Database, audit
from aura_db.models import Account, ApiKey, Environment, Workspace
from aura_shared.errors import Forbidden, NotFound, Unauthenticated, ValidationFailed
from aura_shared.ids import new_id
from aura_shared.redis import KeyFamily, build_key

GLOBAL = "_global"
LAST_USED_THROTTLE_S = 300


def _view(k: ApiKey) -> ApiKeyView:
    return ApiKeyView(
        k.id,
        k.account_id,
        k.environment_id,
        k.name,
        k.prefix,
        list(k.permissions),
        k.created_at,
        k.expires_at,
        k.revoked_at,
        k.last_used_at,
    )


class ApiKeyService:
    def __init__(
        self, db: Database, *, cache: TieredCache, state: aioredis.Redis, pepper: bytes
    ) -> None:
        if cache.family is not KeyFamily.API_KEY:
            raise ValueError("ApiKeyService needs a TieredCache for KeyFamily.API_KEY")
        self._db = db
        self._cache = cache
        self._r = state
        self._pepper = pepper

    async def issue(
        self,
        *,
        account_id: str,
        actor_user_id: str,
        actor_permissions: frozenset[Permission],
        name: str,
        permissions: set[str],
        environment_id: str | None,
        expires_at: datetime | None = None,
    ) -> tuple[ApiKeyView, str]:
        """Returns (metadata, plaintext). The plaintext is shown once and never stored."""
        name = name.strip()
        if not 1 <= len(name) <= 120:
            raise ValidationFailed("key name must be 1-120 characters")
        perms = validate_api_key_permissions(permissions, actor_permissions)
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise ValidationFailed("expires_at must be in the future")
        async with self._db.tenant(account_id) as s:
            env_kind: str | None = None
            if environment_id is not None:
                env = await s.get(Environment, environment_id)  # RLS: only this tenant's
                if env is None:
                    raise NotFound("environment not found")
                env_kind = env.kind
            generated = keys.generate(env_kind, self._pepper)
            row = ApiKey(
                id=new_id("key"),
                account_id=account_id,
                environment_id=environment_id,
                name=name,
                prefix=generated.prefix,
                secret_hash=generated.secret_hash,
                permissions=sorted(p.value for p in perms),
                expires_at=expires_at,
                created_by_user_id=actor_user_id,
            )
            s.add(row)
            await s.flush()
            await s.refresh(row)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_user_id,
                account_id=account_id,
                action="api_key.create",
                target_type="api_key",
                target_id=row.id,
                after={"name": name, "prefix": row.prefix, "permissions": row.permissions},
            )
            return _view(row), generated.plaintext

    async def list(self, account_id: str) -> list[ApiKeyView]:
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))
            return [_view(k) for k in rows]

    async def revoke(self, *, account_id: str, key_id: str, actor_user_id: str) -> None:
        async with self._db.tenant(account_id) as s:
            row = await s.get(ApiKey, key_id)
            if row is None:
                raise NotFound("API key not found")
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                await audit.record(
                    s,
                    actor_type="user",
                    actor_id=actor_user_id,
                    account_id=account_id,
                    action="api_key.revoke",
                    target_type="api_key",
                    target_id=row.id,
                )
            prefix = row.prefix
        await self._cache.invalidate(GLOBAL, prefix)

    async def invalidate_account(self, account_id: str) -> int:
        """Drop cached key records for an account (e.g. after suspension)."""
        async with self._db.tenant(account_id) as s:
            prefixes = list(await s.scalars(select(ApiKey.prefix)))
        for prefix in prefixes:
            await self._cache.invalidate(GLOBAL, prefix)
        return len(prefixes)

    async def authenticate(self, plaintext: str) -> ApiKeyPrincipal:
        parsed = keys.parse(plaintext)

        async def load() -> dict[str, Any] | None:
            # Cross-tenant by necessity: the tenant is not known until the key is found.
            async with self._db.system() as s:
                row = await s.scalar(select(ApiKey).where(ApiKey.prefix == parsed.prefix))
                if row is None:
                    return None
                workspace_id = await s.scalar(
                    select(Workspace.id).where(
                        Workspace.account_id == row.account_id, Workspace.is_default.is_(True)
                    )
                )
                account = await s.get(Account, row.account_id)
                region = account.region if account else None
                return {
                    "account_status": account.status if account else "missing",
                    "workspace_id": workspace_id or "",
                    "region": region or "in",
                    "id": row.id,
                    "account_id": row.account_id,
                    "environment_id": row.environment_id,
                    "hash": row.secret_hash,
                    "permissions": list(row.permissions),
                    "expires_at": row.expires_at.timestamp() if row.expires_at else None,
                    "revoked": row.revoked_at is not None,
                }

        rec = await self._cache.get_or_load(GLOBAL, parsed.prefix, loader=load)
        if rec is None or not keys.verify(parsed, rec["hash"], self._pepper):
            raise Unauthenticated("invalid API key")
        if rec["revoked"]:
            raise Unauthenticated("API key revoked")
        if rec.get("account_status") != "active":
            raise Forbidden("account suspended")
        if rec["expires_at"] is not None and rec["expires_at"] <= datetime.now(UTC).timestamp():
            raise Unauthenticated("API key expired")
        await self._touch(rec["id"])
        return ApiKeyPrincipal(
            rec["id"],
            rec["account_id"],
            rec["environment_id"],
            frozenset(rec["permissions"]),
            workspace_id=rec["workspace_id"],
            region=rec["region"],
        )

    async def _touch(self, key_id: str) -> None:
        """Update last_used_at at most once per 5 minutes per key."""
        throttle = build_key(KeyFamily.THROTTLE, GLOBAL, "apikey_used", key_id)
        if await self._r.set(throttle, b"1", nx=True, ex=LAST_USED_THROTTLE_S):
            async with self._db.system() as s:
                row = await s.get(ApiKey, key_id)
                if row is not None:
                    row.last_used_at = datetime.now(UTC)
