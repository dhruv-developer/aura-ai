"""The provider port every adapter implements."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol

from aura_providers.domain.catalog import ModelSpec
from aura_providers.domain.types import ChatRequest, ChatResult, Credentials, StreamEvent


class ModelProvider(Protocol):
    name: str

    async def complete(
        self, spec: ModelSpec, req: ChatRequest, creds: Credentials, *, max_tokens: int
    ) -> ChatResult: ...

    def stream(
        self, spec: ModelSpec, req: ChatRequest, creds: Credentials, *, max_tokens: int
    ) -> AsyncGenerator[StreamEvent, None]:
        """Must be an async generator: the engine calls ``aclose()`` to cancel the
        upstream request when the client disconnects."""
        ...
