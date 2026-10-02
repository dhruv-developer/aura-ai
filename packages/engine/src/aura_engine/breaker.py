"""Per-model circuit breaker shared by all runtime pods (Redis *state* role).

- Each failure increments a windowed counter.
- At ``threshold`` failures within ``window_s`` the circuit opens for ``cooldown_s``.
- When the open key expires, traffic flows again (half-open). A new failure burst
  re-opens the circuit quickly.
- Successes write nothing, which keeps the hot path free of Redis writes.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from aura_shared.redis import KeyFamily, build_key
from aura_shared.redis.keys import key_part

log = logging.getLogger("aura.engine.breaker")
GLOBAL = "_global"


class CircuitBreaker:
    def __init__(
        self,
        state: aioredis.Redis,
        *,
        threshold: int = 5,
        window_s: int = 30,
        cooldown_s: int = 30,
    ) -> None:
        self._r = state
        self._threshold = threshold
        self._window = window_s
        self._cooldown = cooldown_s

    def _keys(self, model_id: str) -> tuple[str, str]:
        return (
            build_key(KeyFamily.CIRCUIT, GLOBAL, "fail", key_part(model_id)),
            build_key(KeyFamily.CIRCUIT, GLOBAL, "open", key_part(model_id)),
        )

    async def open_circuits(self, model_ids: list[str]) -> frozenset[str]:
        if not model_ids:
            return frozenset()
        try:
            values = await self._r.mget([self._keys(m)[1] for m in model_ids])
        except Exception:
            log.warning("circuit breaker read failed; assuming all closed", exc_info=True)
            return frozenset()
        return frozenset(m for m, v in zip(model_ids, values, strict=True) if v is not None)

    async def record_failure(self, model_id: str) -> bool:
        """Returns True if this failure opened the circuit."""
        fail_key, open_key = self._keys(model_id)
        try:
            async with self._r.pipeline(transaction=True) as p:
                p.incr(fail_key)
                p.expire(fail_key, self._window, nx=True)
                count, _ = await p.execute()
            if int(count) >= self._threshold:
                opened = await self._r.set(open_key, b"1", ex=self._cooldown, nx=True)
                if opened:
                    await self._r.delete(fail_key)
                    log.warning("circuit opened for %s", model_id)
                return bool(opened)
        except Exception:
            log.warning("circuit breaker write failed", exc_info=True)
        return False
