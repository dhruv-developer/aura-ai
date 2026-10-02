"""Exact and semantic response caching (spec §54).

Scoping: every key and vector set is namespaced by account (Redis hash tag) *and* the
routed model, and semantic sets also by a digest of the system prompt. A cached answer
is therefore never served across tenants, across models, or under a different system
prompt.

Semantic matching applies only to single-turn requests (system + one user message).
Matching the last message of a multi-turn conversation would ignore the context that
gave it meaning.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from aura_providers.domain.catalog import ModelSpec
from aura_providers.domain.types import ChatRequest, ChatResult, FinishReason, Usage
from aura_providers.embeddings import Embedder
from aura_shared.redis import KeyFamily, build_key

log = logging.getLogger("aura.engine.cache")
_FAILURES = (RedisError, OSError, TimeoutError)
DEFAULT_TTL_S = 3600
DEFAULT_SIMILARITY = 0.92  # cosine


@dataclass(frozen=True, slots=True)
class CacheHit:
    kind: str  # exact | semantic
    result: ChatResult
    saved_cost: Decimal
    similarity: float | None = None


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]


def request_digest(req: ChatRequest, max_tokens: int) -> str:
    body = json.dumps(
        {
            "m": [[m.role, m.content] for m in req.messages],
            "t": req.temperature,
            "s": list(req.stop),
            "e": req.effort,
            "x": max_tokens,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return _digest(body)


def _single_turn(req: ChatRequest) -> tuple[str, str] | None:
    convo = [m for m in req.messages if m.role != "system"]
    if len(convo) != 1 or convo[0].role != "user":
        return None
    system = "\n".join(m.content for m in req.messages if m.role == "system")
    return system, convo[0].content


class ResponseCache:
    def __init__(
        self,
        cache: aioredis.Redis,
        vectors: aioredis.Redis,
        *,
        embedder: Embedder | None,
        max_vectors_per_set: int = 50_000,
    ) -> None:
        self._r = cache
        self._v = vectors
        self._embedder = embedder
        self._max = max_vectors_per_set

    async def _vcmd(self, *args: object) -> Any:
        """Vector-set commands (Redis 8) go through execute_command (untyped stubs)."""
        return await self._v.execute_command(*args)  # type: ignore[no-untyped-call]

    def _entry_key(self, account_id: str, spec: ModelSpec, digest: str) -> str:
        return build_key(KeyFamily.RESPONSE_EXACT, account_id, _digest(spec.id), digest)

    def _vset_key(self, account_id: str, spec: ModelSpec, system: str) -> str:
        return build_key(KeyFamily.RESPONSE_SEMANTIC, account_id, _digest(spec.id), _digest(system))

    @staticmethod
    def _decode(raw: bytes | str) -> tuple[ChatResult, Decimal]:
        d = json.loads(raw)
        u = d["usage"]
        return (
            ChatResult(d["text"], FinishReason(d["finish"]), Usage(*u), d["model"]),
            Decimal(d["cost"]),
        )

    async def lookup(
        self,
        account_id: str,
        spec: ModelSpec,
        req: ChatRequest,
        *,
        max_tokens: int,
        semantic: bool,
        similarity: float | None,
    ) -> CacheHit | None:
        digest = request_digest(req, max_tokens)
        try:
            raw = await self._r.get(self._entry_key(account_id, spec, digest))
            if raw is not None:
                result, cost = self._decode(raw)
                return CacheHit("exact", result, cost)
            if not semantic or self._embedder is None:
                return None
            turn = _single_turn(req)
            if turn is None:
                return None
            system, user = turn
            [vec] = await self._embedder.embed([user])
            vkey = self._vset_key(account_id, spec, system)
            found = await self._vcmd(
                "VSIM", vkey, "VALUES", len(vec), *vec, "WITHSCORES", "COUNT", 1
            )
            if not found:
                return None
            # redis-py returns {element: score} for WITHSCORES; raw RESP2 is a flat list.
            pairs = list(found.items()) if isinstance(found, dict) else [(found[0], found[1])]
            raw_element, score = pairs[0]
            element = raw_element.decode() if isinstance(raw_element, bytes) else str(raw_element)
            cosine = 2 * float(score) - 1  # vector sets report (1 + cos) / 2
            if cosine < (similarity or DEFAULT_SIMILARITY):
                return None
            raw = await self._r.get(self._entry_key(account_id, spec, element))
            if raw is None:  # entry expired: drop the orphaned vector lazily
                await self._vcmd("VREM", vkey, element)
                return None
            result, cost = self._decode(raw)
            return CacheHit("semantic", result, cost, round(cosine, 4))
        except _FAILURES:
            log.warning("response cache lookup failed; executing normally", exc_info=True)
            return None

    async def store(
        self,
        account_id: str,
        spec: ModelSpec,
        req: ChatRequest,
        result: ChatResult,
        cost: Decimal,
        *,
        max_tokens: int,
        ttl_s: int | None,
        semantic: bool,
    ) -> None:
        if result.finish_reason is not FinishReason.STOP or not result.text.strip():
            return  # never cache truncations, refusals or empty answers
        digest = request_digest(req, max_tokens)
        u = result.usage
        payload = json.dumps(
            {
                "text": result.text,
                "finish": result.finish_reason.value,
                "usage": [
                    u.input_tokens,
                    u.output_tokens,
                    u.cached_input_tokens,
                    u.cache_write_tokens,
                ],
                "model": result.model_served,
                "cost": str(cost),
            }
        )
        try:
            await self._r.set(
                self._entry_key(account_id, spec, digest), payload, ex=ttl_s or DEFAULT_TTL_S
            )
            if not semantic or self._embedder is None:
                return
            turn = _single_turn(req)
            if turn is None:
                return
            system, user = turn
            vkey = self._vset_key(account_id, spec, system)
            if int(await self._vcmd("VCARD", vkey) or 0) >= self._max:
                return  # bounded memory: stop growing a saturated set
            [vec] = await self._embedder.embed([user])
            await self._vcmd("VADD", vkey, "VALUES", len(vec), *vec, digest)
            await self._v.expire(vkey, max(ttl_s or DEFAULT_TTL_S, 86_400))
        except _FAILURES:
            log.warning("response cache store failed", exc_info=True)
