"""Cross-pod L1 invalidation over Redis pub/sub.

Two paths deliver an invalidation:
1. Immediate: ``TieredCache.invalidate()`` calls ``bus.publish(key)`` right away.
2. Guaranteed: control-plane changes enqueue an outbox row in the same transaction,
   and the outbox relay publishes it after commit, even if path 1 was lost or the
   writer crashed.

Pub/sub is fire-and-forget. A pod that misses a message still converges within its
L1 TTL (seconds), which is the safety net.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable

import redis.asyncio as aioredis

from aura_cache.tiered import TieredCache
from aura_shared.settings import get_settings

log = logging.getLogger("aura.cache.invalidation")


def channel_name(env: str | None = None) -> str:
    return f"aura:{env or get_settings().env.value}:invalidate"


class InvalidationBus:
    def __init__(self, client: aioredis.Redis, *, env: str | None = None) -> None:
        self._r = client
        self._channel = channel_name(env)
        self._caches: list[TieredCache] = []
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self.received = 0

    def register(self, caches: Iterable[TieredCache]) -> None:
        self._caches.extend(caches)

    async def publish(self, key: str) -> None:
        try:
            await self._r.publish(self._channel, key)
        except Exception:
            log.warning("invalidation publish failed for %s", key, exc_info=True)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._listen())
            await asyncio.wait_for(self._ready.wait(), timeout=5)

    async def _listen(self) -> None:
        backoff = 0.2
        while True:
            pubsub = self._r.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(self._channel)
                self._ready.set()
                backoff = 0.2
                async for message in pubsub.listen():
                    data = message.get("data")
                    key = data.decode() if isinstance(data, bytes) else str(data)
                    self.received += 1
                    for cache in self._caches:
                        cache.invalidate_local(key)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("invalidation listener error; reconnecting", exc_info=True)
                self._ready.set()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]  # redis stubs

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
