"""Run-scoped working memory: intermediate results of one execution (graph) run."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from aura_shared.redis import KeyFamily, build_key


class WorkingMemory:
    def __init__(self, state: aioredis.Redis, *, ttl_s: int = 3600) -> None:
        self._r = state
        self._ttl = ttl_s

    def _key(self, account_id: str, run_id: str) -> str:
        return build_key(KeyFamily.WORKING_MEMORY, account_id, run_id)

    async def put(self, account_id: str, run_id: str, field: str, value: Any) -> None:
        key = self._key(account_id, run_id)
        async with self._r.pipeline(transaction=True) as p:
            p.hset(key, field, json.dumps(value, default=str))
            p.expire(key, self._ttl)
            await p.execute()

    async def get(self, account_id: str, run_id: str, field: str) -> Any:
        raw = await self._r.hget(self._key(account_id, run_id), field)
        return None if raw is None else json.loads(raw)

    async def all(self, account_id: str, run_id: str) -> dict[str, Any]:
        raw = await self._r.hgetall(self._key(account_id, run_id))
        return {(k.decode() if isinstance(k, bytes) else k): json.loads(v) for k, v in raw.items()}

    async def clear(self, account_id: str, run_id: str) -> None:
        await self._r.delete(self._key(account_id, run_id))
