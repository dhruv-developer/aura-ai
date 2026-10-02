"""Long-term memory with hybrid retrieval (ADR-0004, ADR-0009).

Postgres is the source of truth: content, provenance, validity windows, and a generated
tsvector for full-text search. Redis 8 vector sets hold the ANN index, one set per
(account, scope). It can be rebuilt from Postgres at any time (``reindex``).

Search: full-text ranks + vector ranks, fused with reciprocal rank fusion, then weighted
by recency decay, importance and confidence.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from sqlalchemy import delete, func, or_, select, update

from aura_db import Database
from aura_db.models import MemoryItem
from aura_providers.embeddings import Embedder
from aura_shared.errors import ValidationFailed
from aura_shared.ids import new_id
from aura_shared.redis import KeyFamily, build_key

log = logging.getLogger("aura.memory")
SCOPES = frozenset({"user", "workspace", "workload", "kb"})
KINDS = frozenset({"semantic", "episodic", "preference", "document"})
DUPLICATE_COSINE = 0.95
RECENCY_HALF_LIFE_DAYS = 30.0
RRF_K = 60
MIN_VECTOR_COSINE = 0.15  # below this a "nearest" vector is noise, not relevance


@dataclass(frozen=True, slots=True)
class MemoryHit:
    id: str
    scope: str
    scope_ref: str
    kind: str
    content: str
    score: float


_TERM = re.compile(r"\w+", re.UNICODE)


def _or_query(text: str) -> str | None:
    """Retrieval wants OR semantics ranked by coverage (plainto_tsquery ANDs every word,
    so natural questions match nothing). Terms are restricted to word characters, so
    user input can never inject tsquery syntax."""
    terms = sorted({t for t in _TERM.findall(text.lower()) if len(t) > 1})[:32]
    return " | ".join(terms) if terms else None


def _validate_scope(scope: str, scope_ref: str) -> None:
    if scope not in SCOPES:
        raise ValidationFailed(f"scope must be one of {sorted(SCOPES)}")
    if not 1 <= len(scope_ref) <= 120:
        raise ValidationFailed("scope_ref must be 1-120 characters")


class LongTermMemory:
    def __init__(self, db: Database, *, vectors: aioredis.Redis, embedder: Embedder) -> None:
        self._db = db
        self._v = vectors
        self._embedder = embedder

    def _vkey(self, account_id: str, scope: str, scope_ref: str) -> str:
        ref = hashlib.sha256(scope_ref.encode()).hexdigest()[:24]
        return build_key(KeyFamily.MEMORY_VECTORS, account_id, scope, ref)

    async def _vcmd(self, *args: object) -> Any:
        return await self._v.execute_command(*args)  # type: ignore[no-untyped-call]

    async def _nearest(self, key: str, vec: list[float], k: int) -> list[tuple[str, float]]:
        try:
            found = await self._vcmd(
                "VSIM", key, "VALUES", len(vec), *vec, "WITHSCORES", "COUNT", k
            )
        except (RedisError, OSError, TimeoutError):
            log.warning("vector search unavailable; full-text only", exc_info=True)
            return []
        if not found:
            return []
        pairs = (
            list(found.items())
            if isinstance(found, dict)
            else list(zip(found[::2], found[1::2], strict=False))
        )
        return [
            ((e.decode() if isinstance(e, bytes) else str(e)), 2 * float(s) - 1) for e, s in pairs
        ]

    # ---- write -----------------------------------------------------------------------
    async def add(
        self,
        account_id: str,
        *,
        scope: str,
        scope_ref: str,
        content: str,
        kind: str = "semantic",
        importance: float = 0.5,
        confidence: float = 0.8,
        source: str = "api",
    ) -> tuple[str, bool]:
        """Returns (id, created). A near-identical existing memory is reinforced with the
        newest wording instead of duplicated."""
        _validate_scope(scope, scope_ref)
        content = content.strip()
        if not content or len(content) > 20_000:
            raise ValidationFailed("content must be 1-20000 characters")
        if kind not in KINDS:
            raise ValidationFailed(f"kind must be one of {sorted(KINDS)}")
        if not (0 <= importance <= 1 and 0 <= confidence <= 1):
            raise ValidationFailed("importance and confidence must be within 0..1")
        [vec] = await self._embedder.embed([content])
        key = self._vkey(account_id, scope, scope_ref)
        near = await self._nearest(key, vec, 1)
        now = datetime.now(UTC)
        async with self._db.tenant(account_id) as s:
            if near and near[0][1] >= DUPLICATE_COSINE:
                existing = await s.get(MemoryItem, near[0][0])
                if existing is not None and existing.valid_to is None:
                    existing.content = content  # newest wording wins
                    existing.confidence = max(existing.confidence, confidence)
                    existing.importance = max(existing.importance, importance)
                    existing.valid_from = now
                    existing.embedding = vec
                    return existing.id, False
            item = MemoryItem(
                id=new_id("mem"),
                account_id=account_id,
                scope=scope,
                scope_ref=scope_ref,
                kind=kind,
                content=content,
                embedding=vec,
                importance=importance,
                confidence=confidence,
                source=source[:120],
                valid_from=now,
            )
            s.add(item)
        try:
            await self._vcmd("VADD", key, "VALUES", len(vec), *vec, item.id)
        except (RedisError, OSError, TimeoutError):
            log.warning("vector index write failed; run reindex to repair", exc_info=True)
        return item.id, True

    # ---- read ------------------------------------------------------------------------
    async def search(
        self,
        account_id: str,
        *,
        scopes: list[tuple[str, str]],
        query: str,
        k: int = 8,
        now: datetime | None = None,
    ) -> list[MemoryHit]:
        if not scopes or not query.strip():
            return []
        now = now or datetime.now(UTC)
        [vec] = await self._embedder.embed([query])
        vector_rank: list[str] = []
        for scope, ref in scopes:
            vector_rank += [
                mid
                for mid, cos in await self._nearest(self._vkey(account_id, scope, ref), vec, 50)
                if cos >= MIN_VECTOR_COSINE
            ]
        async with self._db.tenant(account_id) as s:
            scope_filter = [
                (MemoryItem.scope == sc) & (MemoryItem.scope_ref == ref) for sc, ref in scopes
            ]
            fts_rows: list[str] = []
            or_query = _or_query(query)
            if or_query is not None:
                tsq = func.to_tsquery("english", or_query).op("||")(
                    func.to_tsquery("simple", or_query)
                )
                fts_rows = list(
                    (
                        await s.execute(
                            select(MemoryItem.id)
                            .where(
                                or_(*scope_filter),
                                MemoryItem.valid_to.is_(None),
                                MemoryItem.tsv.op("@@")(tsq),
                            )
                            .order_by(func.ts_rank_cd(MemoryItem.tsv, tsq).desc())
                            .limit(50)
                        )
                    )
                    .scalars()
                    .all()
                )
            fused: dict[str, float] = {}
            for ranking in (fts_rows, vector_rank):
                for rank, mid in enumerate(ranking):
                    fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + rank + 1)
            if not fused:
                return []
            items = {
                m.id: m
                for m in await s.scalars(
                    select(MemoryItem).where(
                        MemoryItem.id.in_(list(fused)),
                        MemoryItem.valid_to.is_(None),
                        or_(*scope_filter),
                    )
                )
            }
            scored = []
            for mid, base in fused.items():
                m = items.get(mid)
                if m is None:
                    continue  # stale vector for an archived/other-scope item
                age_days = (now - (m.last_accessed_at or m.valid_from)).total_seconds() / 86400
                recency = math.pow(0.5, max(age_days, 0) / RECENCY_HALF_LIFE_DAYS)
                score = base * (0.5 + 0.5 * recency) * (0.5 + m.importance) * m.confidence
                scored.append((score, m))
            scored.sort(key=lambda x: -x[0])
            top = scored[:k]
            if top:
                await s.execute(
                    update(MemoryItem)
                    .where(MemoryItem.id.in_([m.id for _, m in top]))
                    .values(access_count=MemoryItem.access_count + 1, last_accessed_at=now)
                )
            return [
                MemoryHit(m.id, m.scope, m.scope_ref, m.kind, m.content, round(sc * 1000, 4))
                for sc, m in top
            ]

    async def list_items(
        self, account_id: str, *, scope: str, scope_ref: str, limit: int = 200
    ) -> list[MemoryHit]:
        """Current items of one scope, newest first (the user-visible memory view)."""
        _validate_scope(scope, scope_ref)
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(
                select(MemoryItem)
                .where(
                    MemoryItem.scope == scope,
                    MemoryItem.scope_ref == scope_ref,
                    MemoryItem.valid_to.is_(None),
                )
                .order_by(MemoryItem.valid_from.desc(), MemoryItem.id)
                .limit(min(max(limit, 1), 500))
            )
            return [
                MemoryHit(m.id, m.scope, m.scope_ref, m.kind, m.content, m.importance) for m in rows
            ]

    # ---- lifecycle -------------------------------------------------------------------
    async def delete_item(
        self, account_id: str, memory_id: str, *, scope: str, scope_ref: str
    ) -> bool:
        """Hard delete one item, only within the caller's scope (a user can never delete
        another user's memory in a shared account)."""
        _validate_scope(scope, scope_ref)
        async with self._db.tenant(account_id) as s:
            item = await s.get(MemoryItem, memory_id)
            if item is None or (item.scope, item.scope_ref) != (scope, scope_ref):
                return False
            await s.delete(item)
        try:
            await self._vcmd("VREM", self._vkey(account_id, scope, scope_ref), memory_id)
        except (RedisError, OSError, TimeoutError):
            log.warning("vector index delete failed; run reindex to repair", exc_info=True)
        return True

    async def forget(self, account_id: str, *, scope: str, scope_ref: str) -> int:
        """Hard delete (privacy requests): rows and the vector index."""
        _validate_scope(scope, scope_ref)
        async with self._db.tenant(account_id) as s:
            result = await s.execute(
                delete(MemoryItem).where(
                    MemoryItem.scope == scope, MemoryItem.scope_ref == scope_ref
                )
            )
        try:
            await self._v.delete(self._vkey(account_id, scope, scope_ref))
        except (RedisError, OSError, TimeoutError):
            log.error("vector index delete failed after forget", exc_info=True)
            raise
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def decay(
        self, account_id: str, *, older_than_days: int = 180, now: datetime | None = None
    ) -> int:
        """Archive never-used, low-importance memories (valid_to = now), keeping them
        for audit but out of retrieval."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=older_than_days)
        async with self._db.tenant(account_id) as s:
            stale = list(
                await s.scalars(
                    select(MemoryItem).where(
                        MemoryItem.valid_to.is_(None),
                        MemoryItem.access_count == 0,
                        MemoryItem.importance < 0.3,
                        MemoryItem.valid_from < cutoff,
                        MemoryItem.kind != "document",
                    )
                )
            )
            for m in stale:
                m.valid_to = now
        for m in stale:
            try:
                await self._vcmd("VREM", self._vkey(account_id, m.scope, m.scope_ref), m.id)
            except (RedisError, OSError, TimeoutError):
                log.warning("vector index cleanup failed", exc_info=True)
        return len(stale)

    async def reindex(self, account_id: str) -> int:
        """Rebuild the vector index from Postgres (after Redis data loss)."""
        n = 0
        async with self._db.tenant(account_id) as s:
            rows = list(
                await s.scalars(
                    select(MemoryItem).where(
                        MemoryItem.valid_to.is_(None), MemoryItem.embedding.is_not(None)
                    )
                )
            )
        for m in rows:
            assert m.embedding is not None
            await self._vcmd(
                "VADD",
                self._vkey(account_id, m.scope, m.scope_ref),
                "VALUES",
                len(m.embedding),
                *m.embedding,
                m.id,
            )
            n += 1
        return n
