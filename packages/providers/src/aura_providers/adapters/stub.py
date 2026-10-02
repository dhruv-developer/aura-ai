"""Deterministic in-process provider for tests and key-less local development."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncGenerator

from aura_providers.domain.catalog import ModelSpec
from aura_providers.domain.tokens import estimate_input_tokens, estimate_text_tokens
from aura_providers.domain.types import (
    ChatRequest,
    ChatResult,
    Credentials,
    FinishReason,
    ProviderError,
    ProviderErrorKind,
    StreamDone,
    StreamEvent,
    TextDelta,
    Usage,
)

REFUSE_MARKER = "[refuse]"


class StubProvider:
    """Echoes the last user message. Supports failure injection per model:
    ``stub.fail_next("frontier", ProviderErrorKind.OVERLOADED, times=2)``."""

    name = "stub"

    def __init__(self, *, latency_s: float = 0.0) -> None:
        self._latency = latency_s
        self._failures: dict[str, deque[ProviderErrorKind]] = defaultdict(deque)
        self._mid_stream_failures: dict[str, int] = defaultdict(int)
        self.calls: list[str] = []

    def fail_next(self, upstream: str, kind: ProviderErrorKind, *, times: int = 1) -> None:
        self._failures[upstream].extend([kind] * times)

    def fail_mid_stream(self, upstream: str, *, times: int = 1) -> None:
        """The next stream(s) on ``upstream`` emit one token, then fail."""
        self._mid_stream_failures[upstream] += times

    def _answer(self, spec: ModelSpec, req: ChatRequest, max_tokens: int) -> ChatResult:
        self.calls.append(spec.upstream)
        if self._failures[spec.upstream]:
            kind = self._failures[spec.upstream].popleft()
            raise ProviderError(kind, f"stub: injected {kind.value}")
        last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        if REFUSE_MARKER in last_user:
            text, finish = "", FinishReason.CONTENT_FILTER
        else:
            words = f"[{spec.upstream}] {last_user}".split()
            # Treat each word as ~1 token so max_tokens truncation is observable in tests.
            finish = FinishReason.LENGTH if len(words) > max_tokens else FinishReason.STOP
            text = " ".join(words[:max_tokens])
        usage = Usage(estimate_input_tokens(req), estimate_text_tokens(text))
        return ChatResult(text, finish, usage, spec.upstream, f"stub-{len(self.calls)}")

    async def complete(
        self, spec: ModelSpec, req: ChatRequest, creds: Credentials, *, max_tokens: int
    ) -> ChatResult:
        if self._latency:
            await asyncio.sleep(self._latency)
        return self._answer(spec, req, max_tokens)

    async def stream(
        self, spec: ModelSpec, req: ChatRequest, creds: Credentials, *, max_tokens: int
    ) -> AsyncGenerator[StreamEvent, None]:
        result = self._answer(spec, req, max_tokens)
        if self._mid_stream_failures[spec.upstream] > 0:
            self._mid_stream_failures[spec.upstream] -= 1
            yield TextDelta(result.text.split(" ")[0])
            raise ProviderError(ProviderErrorKind.UNAVAILABLE, "stub: injected mid-stream drop")
        for i, word in enumerate(result.text.split(" ")):
            if self._latency:
                await asyncio.sleep(self._latency)
            yield TextDelta(word if i == 0 else " " + word)
        yield StreamDone(result)


__all__ = ["ProviderErrorKind", "StubProvider"]
