"""Connection pools, one per Redis cluster role, per process."""

from __future__ import annotations

from enum import StrEnum

import redis.asyncio as aioredis

from aura_shared.settings import Settings, get_settings


class RedisRole(StrEnum):
    STATE = "state"  # noeviction + AOF: sessions, quotas, rate limits, locks
    CACHE = "cache"  # allkeys-lfu, no persistence
    VECTOR = "vector"  # semantic cache / memory vectors


class RedisPools:
    """Owns the async clients for each role. Create once per process at startup."""

    def __init__(self, settings: Settings | None = None) -> None:
        s = settings or get_settings()
        urls = {
            RedisRole.STATE: s.redis_state_url,
            RedisRole.CACHE: s.redis_cache_url,
            RedisRole.VECTOR: s.redis_vector_url,
        }
        self._clients: dict[RedisRole, aioredis.Redis] = {
            role: aioredis.Redis.from_url(
                url,
                max_connections=s.redis_max_connections,
                socket_timeout=s.redis_socket_timeout_s,
                socket_connect_timeout=s.redis_socket_timeout_s,
                health_check_interval=30,
                decode_responses=False,
            )
            for role, url in urls.items()
        }

    def get(self, role: RedisRole) -> aioredis.Redis:
        return self._clients[role]

    async def ping(self) -> dict[RedisRole, bool]:
        out: dict[RedisRole, bool] = {}
        for role, client in self._clients.items():
            try:
                out[role] = bool(await client.ping())
            except Exception:  # noqa: BLE001 - health probe must never raise
                out[role] = False
        return out

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
