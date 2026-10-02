"""Short-term session memory (Redis state role)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import redis.asyncio as aioredis

from aura_providers.domain.tokens import PER_MESSAGE_OVERHEAD, estimate_text_tokens
from aura_providers.domain.types import Message
from aura_shared.errors import ValidationFailed
from aura_shared.prompts import render
from aura_shared.redis import KeyFamily, build_key

_SESSION_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


@dataclass(frozen=True, slots=True)
class SessionWindow:
    messages: tuple[Message, ...]  # summary (as a system message, if any) + recent turns
    turns_total: int
    turns_included: int
    est_tokens: int


def validate_session_id(session_id: str) -> str:
    if not 1 <= len(session_id) <= 128 or not set(session_id) <= _SESSION_ID_CHARS:
        raise ValidationFailed("session_id must be 1-128 chars of [A-Za-z0-9-_.]")
    return session_id


class SessionMemory:
    def __init__(
        self, state: aioredis.Redis, *, max_turns: int = 100, ttl_s: int = 24 * 3600
    ) -> None:
        self._r = state
        self._max_turns = max_turns
        self._ttl = ttl_s

    def _turns_key(self, account_id: str, session_id: str) -> str:
        return build_key(
            KeyFamily.SESSION_MEMORY, account_id, validate_session_id(session_id), "turns"
        )

    def _summary_key(self, account_id: str, session_id: str) -> str:
        return build_key(
            KeyFamily.SESSION_MEMORY, account_id, validate_session_id(session_id), "summary"
        )

    async def append(self, account_id: str, session_id: str, messages: list[Message]) -> None:
        """Append turns atomically, trim to ``max_turns`` and slide the TTL."""
        items = [
            json.dumps({"r": m.role, "c": m.content, "t": int(time.time())})
            for m in messages
            if m.role in ("user", "assistant") and m.content
        ]
        if not items:
            return
        key = self._turns_key(account_id, session_id)
        async with self._r.pipeline(transaction=True) as p:
            p.rpush(key, *items)
            p.ltrim(key, -self._max_turns, -1)
            p.expire(key, self._ttl)
            p.expire(self._summary_key(account_id, session_id), self._ttl)
            await p.execute()

    async def set_summary(self, account_id: str, session_id: str, summary: str) -> None:
        await self._r.set(self._summary_key(account_id, session_id), summary, ex=self._ttl)

    async def window(self, account_id: str, session_id: str, *, token_budget: int) -> SessionWindow:
        """Newest turns that fit ``token_budget``, oldest first, with the summary first.
        The window never starts on an assistant turn (provider APIs need a user first)."""
        key = self._turns_key(account_id, session_id)
        raw_turns, summary = (
            await self._r.lrange(key, 0, -1),
            await self._r.get(self._summary_key(account_id, session_id)),
        )
        turns = [json.loads(t) for t in raw_turns]
        budget = token_budget
        picked: list[Message] = []
        summary_msg: Message | None = None
        if summary:
            text = summary.decode() if isinstance(summary, bytes) else str(summary)
            summary_msg = Message("system", render("memory/session_summary.j2", summary=text))
            budget -= estimate_text_tokens(summary_msg.content) + PER_MESSAGE_OVERHEAD
        for t in reversed(turns):
            cost = estimate_text_tokens(t["c"]) + PER_MESSAGE_OVERHEAD
            if cost > budget:
                break
            picked.append(Message(t["r"], t["c"]))
            budget -= cost
        picked.reverse()
        while picked and picked[0].role != "user":
            picked.pop(0)
        out = ((summary_msg,) if summary_msg else ()) + tuple(picked)
        return SessionWindow(out, len(turns), len(picked), token_budget - budget)

    async def forget(self, account_id: str, session_id: str) -> None:
        await self._r.delete(
            self._turns_key(account_id, session_id), self._summary_key(account_id, session_id)
        )
