"""Bring-your-own-key provider credentials (spec §10).

- Plaintext keys exist only in request memory: during ``add`` and just before a model
  call.
- The cache (Redis and in-process L1) holds the *sealed* record only. Each resolve
  decrypts in-process, which costs microseconds with AES-GCM.
- Ciphertext is bound to ``account_id|provider`` through AEAD associated data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aura_cache import TieredCache
from aura_db import Database, audit
from aura_db.models import ProviderCredential
from aura_providers.domain.types import Credentials
from aura_shared.crypto import Envelope, Sealed
from aura_shared.errors import Conflict, NotFound, ValidationFailed
from aura_shared.ids import new_id
from aura_shared.redis import KeyFamily

BYOK_PROVIDERS = frozenset({"anthropic", "openai", "openrouter", "groq"})


@dataclass(frozen=True, slots=True)
class ProviderCredentialView:
    id: str
    provider: str
    label: str
    base_url: str | None
    last4: str
    created_at: datetime | None
    revoked_at: datetime | None


def _aad(account_id: str, provider: str) -> str:
    return f"{account_id}|{provider}"


def _validate_base_url(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        # https only: keys must never travel in clear text. Private hosts need the
        # self-hosted deployment (V6), where egress policy is customer-controlled.
        raise ValidationFailed("base_url must be an https URL")
    return url.rstrip("/")


class ProviderCredentialService:
    def __init__(self, db: Database, *, envelope: Envelope, cache: TieredCache) -> None:
        if cache.family is not KeyFamily.PROVIDER_CREDENTIAL:
            raise ValueError("needs a TieredCache for KeyFamily.PROVIDER_CREDENTIAL")
        self._db = db
        self._envelope = envelope
        self._cache = cache

    async def add(
        self,
        *,
        account_id: str,
        provider: str,
        api_key: str,
        label: str,
        base_url: str | None,
        actor_user_id: str,
    ) -> ProviderCredentialView:
        if provider not in BYOK_PROVIDERS:
            raise ValidationFailed(f"unsupported provider {provider!r}")
        api_key = api_key.strip()
        if not 16 <= len(api_key) <= 512:
            raise ValidationFailed("api_key looks malformed")
        sealed = self._envelope.seal(api_key, aad=_aad(account_id, provider))
        row = ProviderCredential(
            id=new_id("pcr"),
            account_id=account_id,
            provider=provider,
            label=label.strip()[:120] or provider,
            base_url=_validate_base_url(base_url),
            key_id=sealed.key_id,
            wrapped_dek=sealed.wrapped_dek,
            nonce=sealed.nonce,
            ciphertext=sealed.ciphertext,
            last4=api_key[-4:],
            created_by=actor_user_id,
        )
        try:
            async with self._db.tenant(account_id) as s:
                s.add(row)
                await s.flush()
                await s.refresh(row)
                await audit.record(
                    s,
                    actor_type="user",
                    actor_id=actor_user_id,
                    account_id=account_id,
                    action="provider_credential.add",
                    target_type="provider_credential",
                    target_id=row.id,
                    after={"provider": provider, "last4": row.last4},  # never the key
                )
        except IntegrityError as exc:
            raise Conflict(f"an active {provider} key already exists; revoke it first") from exc
        await self._cache.invalidate(account_id, provider)
        return self._view(row)

    async def list(self, account_id: str) -> list[ProviderCredentialView]:
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(
                select(ProviderCredential).order_by(ProviderCredential.created_at.desc())
            )
            return [self._view(r) for r in rows]

    async def revoke(self, *, account_id: str, credential_id: str, actor_user_id: str) -> None:
        async with self._db.tenant(account_id) as s:
            row = await s.get(ProviderCredential, credential_id)
            if row is None:
                raise NotFound("credential not found")
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                await audit.record(
                    s,
                    actor_type="user",
                    actor_id=actor_user_id,
                    account_id=account_id,
                    action="provider_credential.revoke",
                    target_type="provider_credential",
                    target_id=row.id,
                )
            provider = row.provider
        await self._cache.invalidate(account_id, provider)

    async def resolve(self, account_id: str, provider: str) -> Credentials | None:
        """``CredentialResolver`` implementation used by the runtime."""

        async def load() -> dict[str, Any] | None:
            async with self._db.tenant(account_id) as s:
                row = await s.scalar(
                    select(ProviderCredential).where(
                        ProviderCredential.provider == provider,
                        ProviderCredential.revoked_at.is_(None),
                    )
                )
                if row is None:
                    return None
                sealed = Sealed(row.key_id, row.wrapped_dek, row.nonce, row.ciphertext)
                return {"sealed": sealed.to_dict(), "base_url": row.base_url}

        record = await self._cache.get_or_load(account_id, provider, loader=load)
        if record is None:
            return None
        api_key = self._envelope.open(
            Sealed.from_dict(record["sealed"]), aad=_aad(account_id, provider)
        )
        return Credentials(api_key=api_key, base_url=record["base_url"], source="byok")

    @staticmethod
    def _view(r: ProviderCredential) -> ProviderCredentialView:
        return ProviderCredentialView(
            r.id, r.provider, r.label, r.base_url, r.last4, r.created_at, r.revoked_at
        )
