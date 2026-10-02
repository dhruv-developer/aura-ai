"""Read-through tiered cache with single-flight and early refresh."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError

from aura_cache.l1 import L1Cache
from aura_cache.l2 import RedisL2
from aura_shared.redis import KeyFamily, RedisScripts, build_key, ttl_for

log = logging.getLogger("aura.cache")
# Redis is an accelerator, never a dependency: these errors degrade to a cache miss.
_REDIS_FAILURES = (RedisError, OSError, TimeoutError)

Loader = Callable[[], Awaitable[Any]]
Publisher = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class CacheStats:
    l1_hits: int = 0
    l2_hits: int = 0
    loads: int = 0
    early_refreshes: int = 0
    lease_waits: int = 0
    degraded: int = 0  # L2/lease operations that failed and fell back to the source


def _now_ms() -> float:
    return time.time() * 1000


class TieredCache:
    """One instance per key family per process.

    ``l1_ttl_s`` is deliberately short: L1 is a per-pod accelerator, and staleness is
    bounded by it even if an invalidation message is lost.
    """

    def __init__(
        self,
        family: KeyFamily,
        *,
        l1: L1Cache,
        l2: RedisL2,
        scripts: RedisScripts,
        l1_ttl_s: float = 30.0,
        ttl_s: int | None = None,
        xfetch_beta: float = 1.0,
        lease_ms: int = 5_000,
        lease_wait_ms: int = 2_000,
        publisher: Publisher | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.family = family
        self._l1 = l1
        self._l2 = l2
        self._scripts = scripts
        self._l1_ttl_s = l1_ttl_s
        self._ttl_s = ttl_s
        self._beta = xfetch_beta
        self._lease_ms = lease_ms
        self._lease_wait_ms = lease_wait_ms
        self._publisher = publisher
        self._rng = rng or random.Random()  # noqa: S311 - not crypto
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self.stats = CacheStats()

    def set_publisher(self, publisher: Publisher | None) -> None:
        self._publisher = publisher

    def key(self, tenant: str, *parts: str) -> str:
        return build_key(self.family, tenant, *parts)

    def _ttl(self) -> int:
        if self._ttl_s is not None:
            return self._ttl_s
        ttl = ttl_for(self.family, rng=self._rng)
        if ttl is None:
            raise ValueError(f"{self.family.name} has no TTL; it is not a cache family")
        return ttl

    def _should_refresh_early(self, compute_ms: float, expires_at_ms: float) -> bool:
        # XFetch: now - delta * beta * ln(rand) >= expiry. ln(rand) < 0, so this is a
        # probabilistic lead proportional to how expensive the value is to recompute.
        r = self._rng.random() or 1e-12
        return _now_ms() - compute_ms * self._beta * math.log(r) >= expires_at_ms

    async def get_or_load(self, tenant: str, *parts: str, loader: Loader) -> Any:
        key = self.key(tenant, *parts)

        value = self._l1.get(key, L1Cache.MISSING)
        if value is not L1Cache.MISSING:
            self.stats.l1_hits += 1
            return value

        # In-process single-flight over the whole miss path (L2 read + load): concurrent
        # L1 misses in this pod cost one Redis round trip, not one per caller.
        fut = self._inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await self._fill(key, loader)
            fut.set_result(result)
            return result
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so an unobserved failure does not warn
            raise
        finally:
            self._inflight.pop(key, None)

    def _degraded(self, op: str, key: str) -> None:
        self.stats.degraded += 1
        log.warning("cache %s failed for %s; serving from source", op, key, exc_info=True)

    async def _fill(self, key: str, loader: Loader) -> Any:
        try:
            entry = await self._l2.get(key)
        except _REDIS_FAILURES:
            self._degraded("read", key)
            return await self._load_and_store(key, loader)
        if entry is not None:
            if self._should_refresh_early(entry.compute_ms, entry.expires_at_ms):
                self.stats.early_refreshes += 1
            else:
                self.stats.l2_hits += 1
                self._l1.set(key, entry.value, self._l1_ttl_s)
                return entry.value
        return await self._load_across_pods(key, loader, stale=entry)

    async def _load_across_pods(self, key: str, loader: Loader, stale: Any) -> Any:
        lease_key = build_key(KeyFamily.LOCK, "_global", "cache", self.family.domain, _digest(key))
        token = secrets.token_hex(8)
        try:
            acquired = await self._scripts.lease_acquire(lease_key, token, self._lease_ms)
        except _REDIS_FAILURES:
            self._degraded("lease", key)
            return await self._load_and_store(key, loader)
        if acquired:
            try:
                return await self._load_and_store(key, loader)
            finally:
                try:
                    await self._scripts.lease_release(lease_key, token)
                except _REDIS_FAILURES:
                    self._degraded("lease release", key)  # lease expires on its own

        # Another pod is loading. Serve stale during an early refresh; otherwise wait.
        if stale is not None:
            self._l1.set(key, stale.value, self._l1_ttl_s)
            return stale.value
        self.stats.lease_waits += 1
        deadline = _now_ms() + self._lease_wait_ms
        delay = 0.005
        while _now_ms() < deadline:
            await asyncio.sleep(delay)
            try:
                entry = await self._l2.get(key)
            except _REDIS_FAILURES:
                self._degraded("read", key)
                break
            if entry is not None:
                self._l1.set(key, entry.value, self._l1_ttl_s)
                return entry.value
            delay = min(delay * 2, 0.1)
        # Holder is slow or died: load ourselves rather than fail the request.
        return await self._load_and_store(key, loader)

    async def _load_and_store(self, key: str, loader: Loader) -> Any:
        started = _now_ms()
        value = await loader()
        now = _now_ms()
        self.stats.loads += 1
        try:
            await self._l2.set(key, value, ttl_s=self._ttl(), compute_ms=now - started, now_ms=now)
        except _REDIS_FAILURES:
            self._degraded("write", key)
        self._l1.set(key, value, self._l1_ttl_s)
        return value

    async def invalidate(self, tenant: str, *parts: str) -> None:
        key = self.key(tenant, *parts)
        self._l1.delete(key)
        try:
            await self._l2.delete(key)
        except _REDIS_FAILURES:
            # Other pods converge via the outbox relay and the L2/L1 TTLs.
            self._degraded("invalidate", key)
        if self._publisher is not None:
            await self._publisher(key)

    def invalidate_local(self, key: str) -> None:
        """Called by the config-change subscriber on every pod."""
        self._l1.delete(key)


def _digest(key: str) -> str:
    return hashlib.blake2b(key.encode(), digest_size=12).hexdigest()
