"""Opaque, revocable server-side sessions in the Redis *state* role.

Cookie value: ``<user_id>.<secret>``. Redis stores only sha256(secret). The user id
gives the key its hash tag, which enables per-user "log out everywhere" through an
index set. Opaque sessions are used instead of JWTs so revocation (logout, suspension)
takes effect immediately.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass

import redis.asyncio as aioredis

from aura_shared.ids import id_prefix
from aura_shared.redis import KeyFamily, build_key


@dataclass(frozen=True, slots=True)
class SessionData:
    user_id: str
    created_at: int
    ip: str | None
    user_agent: str | None


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:40]


class SessionStore:
    def __init__(self, state: aioredis.Redis, *, ttl_s: int) -> None:
        self._r = state
        self._ttl = ttl_s

    def _key(self, user_id: str, digest: str) -> str:
        return build_key(KeyFamily.SESSION, user_id, digest)

    def _index(self, user_id: str) -> str:
        return build_key(KeyFamily.SESSION_INDEX, user_id)

    async def create(
        self, user_id: str, *, ip: str | None = None, user_agent: str | None = None
    ) -> str:
        secret = secrets.token_urlsafe(32)
        digest = _digest(secret)
        data = {"u": user_id, "c": int(time.time()), "ip": ip, "ua": (user_agent or "")[:200]}
        async with self._r.pipeline(transaction=True) as p:
            p.set(self._key(user_id, digest), json.dumps(data), ex=self._ttl)
            p.sadd(self._index(user_id), digest)
            p.expire(self._index(user_id), self._ttl)
            await p.execute()
        return f"{user_id}.{secret}"

    @staticmethod
    def _parse(cookie: str) -> tuple[str, str] | None:
        user_id, sep, secret = cookie.partition(".")
        if not sep or not secret:
            return None
        try:
            if id_prefix(user_id) != "usr":
                return None
        except ValueError:
            return None
        return user_id, secret

    async def get(self, cookie: str) -> SessionData | None:
        parsed = self._parse(cookie)
        if parsed is None:
            return None
        user_id, secret = parsed
        key = self._key(user_id, _digest(secret))
        raw = await self._r.get(key)
        if raw is None:
            return None
        # Sliding expiry: extend once the session is past half its lifetime.
        if await self._r.ttl(key) < self._ttl // 2:
            await self._r.expire(key, self._ttl)
            await self._r.expire(self._index(user_id), self._ttl)
        d = json.loads(raw)
        return SessionData(d["u"], d["c"], d.get("ip"), d.get("ua"))

    async def revoke(self, cookie: str) -> None:
        parsed = self._parse(cookie)
        if parsed is None:
            return
        user_id, secret = parsed
        digest = _digest(secret)
        async with self._r.pipeline(transaction=True) as p:
            p.delete(self._key(user_id, digest))
            p.srem(self._index(user_id), digest)
            await p.execute()

    async def revoke_all(self, user_id: str) -> int:
        digests = await self._r.smembers(self._index(user_id))
        keys = [self._key(user_id, d.decode() if isinstance(d, bytes) else d) for d in digests]
        if keys:
            await self._r.delete(*keys)
        await self._r.delete(self._index(user_id))
        return len(keys)
