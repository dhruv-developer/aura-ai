"""Anthropic Messages API adapter (official ``anthropic`` SDK)."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import AsyncGenerator
from typing import Any

import anthropic

from aura_providers.domain.catalog import ModelSpec
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

# Server-side refusal fallback ("default" mode routes by refusal category).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "pause_turn": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "refusal": FinishReason.CONTENT_FILTER,
}


def map_error(exc: Exception) -> ProviderError:
    """Most-specific first: timeouts subclass connection errors."""
    k = ProviderErrorKind
    status = getattr(exc, "status_code", None)
    if isinstance(exc, anthropic.APITimeoutError):
        kind = k.TIMEOUT
    elif isinstance(exc, anthropic.APIConnectionError):
        kind = k.UNAVAILABLE
    elif isinstance(exc, anthropic.RateLimitError):
        kind = k.RATE_LIMITED
    elif isinstance(exc, anthropic.OverloadedError):
        kind = k.OVERLOADED
    elif isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        kind = k.AUTH
    elif isinstance(exc, anthropic.NotFoundError):
        kind = k.NOT_FOUND
    elif isinstance(exc, anthropic.APIStatusError):
        kind = k.UNAVAILABLE if (status or 0) >= 500 else k.BAD_REQUEST
    else:
        kind = k.UNAVAILABLE
    return ProviderError(kind, f"anthropic: {type(exc).__name__}", status=status)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        timeout_s: float = 600.0,
        max_retries: int = 1,
        max_clients: int = 256,
        http_client: Any = None,
    ):
        # The gateway fails over to other models itself, so SDK retries stay low.
        # ``http_client`` (an ``anthropic.DefaultAsyncHttpxClient``) lets every per-key
        # client share one connection pool, and lets tests inject a mock transport.
        self._http_client = http_client
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._max_clients = max_clients
        self._clients: OrderedDict[tuple[str, str | None], anthropic.AsyncAnthropic] = OrderedDict()

    def _client(self, creds: Credentials) -> anthropic.AsyncAnthropic:
        """One pooled client per credential. Bounded LRU, because BYOK means many keys."""
        key = (hashlib.sha256(creds.api_key.encode()).hexdigest(), creds.base_url)
        client = self._clients.get(key)
        if client is None:
            client = anthropic.AsyncAnthropic(
                api_key=creds.api_key,
                base_url=creds.base_url,
                timeout=self._timeout,
                max_retries=self._max_retries,
                http_client=self._http_client,
            )
            self._clients[key] = client
            while len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(key)
        return client

    @staticmethod
    def build_params(
        spec: ModelSpec, req: ChatRequest, max_tokens: int
    ) -> tuple[dict[str, Any], list[str]]:
        notes: list[str] = []
        system = "\n\n".join(m.content for m in req.messages if m.role == "system")
        messages = [
            {"role": m.role, "content": m.content} for m in req.messages if m.role != "system"
        ]
        if not messages or messages[0]["role"] != "user":
            raise ProviderError(
                ProviderErrorKind.BAD_REQUEST, "conversation must start with a user message"
            )
        params: dict[str, Any] = {
            "model": spec.upstream,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            params["system"] = system
        if req.temperature is not None:
            if spec.has("sampling"):
                params["temperature"] = req.temperature
            else:
                notes.append("temperature ignored: model does not accept sampling parameters")
        if req.stop and spec.has("stop_sequences"):
            params["stop_sequences"] = list(req.stop)
        if req.effort:
            if spec.has("effort"):
                params["output_config"] = {"effort": req.effort}
            else:
                notes.append("effort ignored: not supported by this model")
        if spec.has("refusal_fallback"):
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params, notes

    @staticmethod
    def to_result(message: Any, notes: list[str]) -> ChatResult:
        # Only text blocks are the answer. Thinking/fallback blocks are metadata.
        text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
        u = message.usage
        cached = getattr(u, "cache_read_input_tokens", None) or 0
        written = getattr(u, "cache_creation_input_tokens", None) or 0
        # Anthropic's input_tokens excludes cache reads/writes. Normalize to the total.
        usage = Usage(
            input_tokens=(u.input_tokens or 0) + cached + written,
            output_tokens=u.output_tokens or 0,
            cached_input_tokens=cached,
            cache_write_tokens=written,
        )
        if any(getattr(b, "type", None) == "fallback" for b in message.content):
            notes = [*notes, "served_by_refusal_fallback"]
        return ChatResult(
            text=text,
            finish_reason=_STOP.get(message.stop_reason or "", FinishReason.STOP),
            usage=usage,
            model_served=message.model,
            provider_request_id=getattr(message, "id", None),
            notes=tuple(notes),
        )

    async def complete(
        self, spec: ModelSpec, req: ChatRequest, creds: Credentials, *, max_tokens: int
    ) -> ChatResult:
        params, notes = self.build_params(spec, req, max_tokens)
        client = self._client(creds)
        try:
            if "betas" in params:
                message = await client.beta.messages.create(**params)
            else:
                message = await client.messages.create(**params)
        except anthropic.APIError as exc:
            raise map_error(exc) from exc
        return self.to_result(message, notes)

    async def stream(
        self, spec: ModelSpec, req: ChatRequest, creds: Credentials, *, max_tokens: int
    ) -> AsyncGenerator[StreamEvent, None]:
        params, notes = self.build_params(spec, req, max_tokens)
        client = self._client(creds)
        api: Any = client.beta.messages if "betas" in params else client.messages
        try:
            async with api.stream(**params) as s:
                async for text in s.text_stream:
                    yield TextDelta(text)
                final = await s.get_final_message()
        except anthropic.APIError as exc:
            raise map_error(exc) from exc
        yield StreamDone(self.to_result(final, notes))
