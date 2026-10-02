"""Atomic Redis operations implemented as Lua scripts.

``redis-py`` ``register_script`` runs ``EVALSHA`` and transparently falls back to
``EVAL`` (re-loading the script) on ``NOSCRIPT`` — e.g. after a failover or restart.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from importlib import resources

import redis.asyncio as aioredis
from redis.commands.core import AsyncScript

from aura_shared.redis.keys import KeyFamily, build_key


def _load(name: str) -> str:
    return (resources.files("aura_shared.redis") / "lua" / f"{name}.lua").read_text("utf-8")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    retry_after_ms: int
    remaining: int


@dataclass(frozen=True, slots=True)
class QuotaResult:
    ok: bool
    used: int


@dataclass(frozen=True, slots=True)
class QuotaKeys:
    used: str
    reservations: str
    deadlines: str

    @classmethod
    def for_(cls, tenant: str, metric: str, period: str) -> QuotaKeys:
        return cls(
            used=build_key(KeyFamily.QUOTA_USED, tenant, metric, period),
            reservations=build_key(KeyFamily.QUOTA_RESERVATIONS, tenant, metric, period),
            deadlines=build_key(KeyFamily.QUOTA_DEADLINES, tenant, metric, period),
        )

    def as_list(self) -> list[str]:
        return [self.used, self.reservations, self.deadlines]


class RedisScripts:
    """Typed wrappers around the Lua scripts. Bind to the *state* Redis role."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._r = client
        self._gcra: AsyncScript = client.register_script(_load("gcra_rate_limit"))
        self._reserve: AsyncScript = client.register_script(_load("quota_reserve"))
        self._commit: AsyncScript = client.register_script(_load("quota_commit"))
        self._release: AsyncScript = client.register_script(_load("quota_release"))
        self._lease_release: AsyncScript = client.register_script(_load("lease_release"))
        self._counter_raise: AsyncScript = client.register_script(_load("counter_raise"))

    # ---- rate limiting -------------------------------------------------------------
    async def rate_limit(
        self,
        key: str,
        *,
        rate_per_s: float,
        burst: int,
        cost: int = 1,
        now_ms: int | None = None,
    ) -> RateLimitResult:
        if rate_per_s <= 0 or burst <= 0:
            raise ValueError("rate_per_s and burst must be positive")
        emission_ms = 1000.0 / rate_per_s
        res = await self._gcra(
            keys=[key],
            args=[emission_ms, burst, cost, "" if now_ms is None else now_ms],
        )
        return RateLimitResult(bool(int(res[0])), int(float(res[1])), int(float(res[2])))

    # ---- quotas: reserve -> commit | release ----------------------------------------
    async def quota_reserve(
        self,
        keys: QuotaKeys,
        *,
        limit: int | None,
        amount: int,
        reservation_id: str,
        lease_ms: int = 120_000,
        period_ms: int = 0,
        now_ms: int | None = None,
    ) -> QuotaResult:
        res = await self._reserve(
            keys=keys.as_list(),
            args=[
                -1 if limit is None else limit,
                amount,
                reservation_id,
                lease_ms,
                period_ms,
                _now_ms() if now_ms is None else now_ms,
            ],
        )
        return QuotaResult(bool(int(res[0])), int(res[1]))

    async def quota_commit(
        self, keys: QuotaKeys, *, reservation_id: str, actual: int
    ) -> QuotaResult:
        res = await self._commit(keys=keys.as_list(), args=[reservation_id, actual])
        return QuotaResult(bool(int(res[0])), int(res[1]))

    async def quota_release(self, keys: QuotaKeys, *, reservation_id: str) -> QuotaResult:
        res = await self._release(keys=keys.as_list(), args=[reservation_id])
        return QuotaResult(bool(int(res[0])), int(res[1]))

    async def quota_used(self, keys: QuotaKeys) -> int:
        raw = await self._r.get(keys.used)
        return int(raw) if raw is not None else 0

    async def counter_raise(self, key: str, floor: int, *, expire_ms: int = 0) -> int:
        return int(await self._counter_raise(keys=[key], args=[floor, expire_ms]))

    # ---- leases (distributed single-flight / leader tasks) -------------------------
    async def lease_acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        return bool(await self._r.set(key, token, nx=True, px=ttl_ms))

    async def lease_release(self, key: str, token: str) -> bool:
        return bool(int(await self._lease_release(keys=[key], args=[token])))
