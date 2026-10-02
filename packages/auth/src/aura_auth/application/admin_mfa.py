"""Superadmin second factor and short-lived admin sessions."""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from aura_auth.domain import totp
from aura_db import Database, audit
from aura_db.models import AdminMfa, User
from aura_shared.crypto import Envelope, Sealed
from aura_shared.errors import Conflict, Forbidden, Unauthenticated
from aura_shared.redis import KeyFamily, build_key, ttl_for

ADMIN_SESSION_TTL = ttl_for(KeyFamily.ADMIN_SESSION, jitter=False) or 8 * 3600


def _aad(user_id: str) -> str:
    return f"{user_id}|admin_totp"


class AdminMfaService:
    def __init__(self, db: Database, *, envelope: Envelope, state: aioredis.Redis) -> None:
        self._db = db
        self._envelope = envelope
        self._r = state

    async def _superadmin(self, s: AsyncSession, user_id: str) -> User:
        user = await s.get(User, user_id)
        if user is None or not user.is_superadmin or user.status != "active":
            raise Forbidden("superadmin access required")
        return user

    async def enroll(self, user_id: str) -> tuple[str, str]:
        """Start (or restart, while unconfirmed) enrollment. Returns (secret, otpauth URI)."""
        secret = totp.generate_secret()
        sealed = self._envelope.seal(secret, aad=_aad(user_id))
        async with self._db.system() as s:
            user = await self._superadmin(s, user_id)
            existing = await s.get(AdminMfa, user_id)
            if existing is not None and existing.confirmed_at is not None:
                raise Conflict("TOTP already enrolled; ask another superadmin to reset it")
            if existing is not None:
                await s.delete(existing)
                await s.flush()
            s.add(
                AdminMfa(
                    user_id=user_id,
                    key_id=sealed.key_id,
                    wrapped_dek=sealed.wrapped_dek,
                    nonce=sealed.nonce,
                    ciphertext=sealed.ciphertext,
                )
            )
            await audit.record(
                s, actor_type="superadmin", actor_id=user_id, action="admin.totp_enroll"
            )
            return secret, totp.provisioning_uri(secret, user.email)

    async def verify(self, user_id: str, code: str, *, now: float | None = None) -> str:
        """Verify a code; first success confirms enrollment. Returns an admin session token."""
        now = now if now is not None else time.time()
        async with self._db.system() as s:
            await self._superadmin(s, user_id)
            mfa = await s.get(AdminMfa, user_id, with_for_update=True)
            if mfa is None:
                raise Unauthenticated("TOTP not enrolled")
            secret = self._envelope.open(
                Sealed(mfa.key_id, mfa.wrapped_dek, mfa.nonce, mfa.ciphertext), aad=_aad(user_id)
            )
            step = totp.verify(secret, code, now=now)
            if step is None or (mfa.last_used_step is not None and step <= mfa.last_used_step):
                await audit.record(
                    s, actor_type="superadmin", actor_id=user_id, action="admin.totp_failed"
                )
                raise Unauthenticated("invalid or already used code")
            mfa.last_used_step = step
            mfa.confirmed_at = mfa.confirmed_at or datetime.now(UTC)
            await audit.record(s, actor_type="superadmin", actor_id=user_id, action="admin.login")
        token = secrets.token_urlsafe(32)
        await self._r.set(self._key(user_id, token), b"1", ex=ADMIN_SESSION_TTL)
        return f"{user_id}.{token}"

    def _key(self, user_id: str, token: str) -> str:
        return build_key(
            KeyFamily.ADMIN_SESSION, user_id, hashlib.sha256(token.encode()).hexdigest()[:40]
        )

    async def check_session(self, cookie: str, *, user_id: str) -> bool:
        uid, sep, token = cookie.partition(".")
        if not sep or uid != user_id:
            return False
        return bool(await self._r.exists(self._key(uid, token)))

    async def end_session(self, cookie: str) -> None:
        uid, sep, token = cookie.partition(".")
        if sep:
            await self._r.delete(self._key(uid, token))
