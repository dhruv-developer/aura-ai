"""Distributed tier backed by the ``redis-cache`` role.

Stored envelope: ``{"v": value, "d": compute_ms, "e": expires_at_ms}``. ``d`` and ``e``
drive probabilistic early refresh (XFetch, Vattani et al. 2015).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis

from aura_cache.codec import Codec, JsonCodec


@dataclass(frozen=True, slots=True)
class L2Entry:
    value: Any
    compute_ms: float
    expires_at_ms: float


class RedisL2:
    def __init__(self, client: aioredis.Redis, codec: Codec | None = None) -> None:
        self._r = client
        self._codec = codec or JsonCodec()

    async def get(self, key: str) -> L2Entry | None:
        raw = await self._r.get(key)
        if raw is None:
            return None
        env = self._codec.loads(raw if isinstance(raw, bytes) else raw.encode())
        return L2Entry(env["v"], float(env["d"]), float(env["e"]))

    async def set(
        self, key: str, value: Any, *, ttl_s: int, compute_ms: float, now_ms: float
    ) -> None:
        env = {"v": value, "d": compute_ms, "e": now_ms + ttl_s * 1000}
        await self._r.set(key, self._codec.dumps(env), ex=ttl_s)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._r.delete(*keys)
