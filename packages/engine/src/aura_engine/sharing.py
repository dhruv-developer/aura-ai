"""Shareable "Optimized by AURA" cards (spec §15).

A card is built from a content-free execution summary that the runtime records when an
execution finishes: model, mode, tokens, cost, the frontier baseline and the kinds of
optimizations applied. Prompts, outputs, context, memory and credential details are
never part of a summary, so sharing cannot leak them.
"""

from __future__ import annotations

import json
import re
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select

from aura_db import Database
from aura_db.models import SharedCard
from aura_engine.engine import Execution, PreparedExecution
from aura_shared.errors import NotFound
from aura_shared.ids import new_id
from aura_shared.redis import KeyFamily, build_key, ttl_for

# Notes are free text; only their category (the words before the first ":" or "(")
# reaches a card, and only from this allow-list.
_NOTE_KINDS = {
    "session memory": "session memory",
    "memory": "personal memory",
    "context": "context compiled",
    "served from exact cache": "exact cache hit",
    "served from semantic cache": "semantic cache hit",
    "learned routing": "learned routing",
    "prompt": "prompt optimized",
    "credits exhausted": "your own key",
    "pii": "personal data redacted",
}


def _kinds(notes: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for note in notes:
        head = re.split(r"[:(]", note, maxsplit=1)[0].strip().lower()
        for prefix, label in _NOTE_KINDS.items():
            if head.startswith(prefix) and label not in out:
                out.append(label)
    return out


def summarize(run: PreparedExecution, execution: Execution) -> dict[str, Any]:
    baseline = execution.baseline_cost_usd
    saved_pct = float((execution.saved_usd / baseline) * 100) if baseline and baseline > 0 else 0.0
    return {
        "execution_id": execution.execution_id,
        "model": execution.spec.id,
        "tier": execution.spec.tier.name.lower(),
        "mode": run.policy.effective_quality.value,
        "latency_ms": execution.latency_ms,
        "input_tokens": execution.result.usage.input_tokens,
        "output_tokens": execution.result.usage.output_tokens,
        "cost_usd": str(execution.cost_usd),
        "baseline_cost_usd": str(baseline) if baseline is not None else None,
        "saved_usd": str(execution.saved_usd),
        "saved_pct": round(saved_pct, 1),
        "cached": execution.credential_source == "cache",
        "optimizations": _kinds(execution.notes),
    }


class ExecutionSummaries:
    def __init__(self, state: aioredis.Redis) -> None:
        self._r = state

    async def record(self, account_id: str, summary: dict[str, Any]) -> None:
        key = build_key(KeyFamily.EXEC_SUMMARY, account_id, summary["execution_id"])
        await self._r.set(key, json.dumps(summary), ex=ttl_for(KeyFamily.EXEC_SUMMARY))

    async def get(self, account_id: str, execution_id: str) -> dict[str, Any] | None:
        raw = await self._r.get(build_key(KeyFamily.EXEC_SUMMARY, account_id, execution_id))
        return json.loads(raw) if raw else None


class SummaryObserver:
    def __init__(self, summaries: ExecutionSummaries) -> None:
        self._summaries = summaries

    async def on_finished(
        self, run: PreparedExecution, execution: Execution | None, *, ok: bool
    ) -> None:
        if ok and execution is not None:
            await self._summaries.record(run.ctx.account_id, summarize(run, execution))


class ShareService:
    def __init__(self, db: Database, summaries: ExecutionSummaries) -> None:
        self._db = db
        self._summaries = summaries

    async def create(self, account_id: str, execution_id: str) -> dict[str, Any]:
        """Idempotent per execution: sharing twice returns the same card."""
        async with self._db.tenant(account_id) as s:
            existing = await s.scalar(
                select(SharedCard).where(SharedCard.execution_id == execution_id)
            )
            if existing is not None:
                return self._view(existing)
        summary = await self._summaries.get(account_id, execution_id)
        if summary is None:
            raise NotFound("execution not found or too old to share")
        card = SharedCard(
            id=new_id("shr"), account_id=account_id, execution_id=execution_id, summary=summary
        )
        async with self._db.system() as s:
            s.add(card)
        return self._view(card)

    async def get_public(self, card_id: str) -> dict[str, Any]:
        async with self._db.system() as s:
            card = await s.get(SharedCard, card_id)
        if card is None:
            raise NotFound("card not found")
        view = self._view(card)
        view.pop("execution_id")  # the public view does not expose internal ids
        return view

    async def delete(self, account_id: str, card_id: str) -> None:
        async with self._db.system() as s:
            card = await s.get(SharedCard, card_id)
            if card is None or card.account_id != account_id:
                raise NotFound("card not found")
            await s.delete(card)

    @staticmethod
    def _view(card: SharedCard) -> dict[str, Any]:
        return {"id": card.id, "execution_id": card.execution_id, **card.summary}
