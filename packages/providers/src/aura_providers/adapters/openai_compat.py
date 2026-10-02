"""OpenAI-compatible Chat Completions adapter (OpenAI, OpenRouter, Google's
OpenAI-compatible endpoint, vLLM, Ollama, Indian model hosts)."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

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

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
}
_FINISH = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def _status_kind(status: int) -> ProviderErrorKind:
    k = ProviderErrorKind
    if status == 429:
        return k.RATE_LIMITED
    if status in (401, 403):
        return k.AUTH
    if status == 404:
        return k.NOT_FOUND
    if status == 529:
        return k.OVERLOADED
    if status >= 500:
        return k.UNAVAILABLE
    return k.BAD_REQUEST


class OpenAICompatibleProvider:
    def __init__(self, name: str, http: httpx.AsyncClient, *, timeout_s: float = 600.0) -> None:
        self.name = name
        self._http = http
        self._timeout = timeout_s

    def _url(self, creds: Credentials) -> str:
        base = creds.base_url or DEFAULT_BASE_URLS.get(self.name)
        if not base:
            raise ProviderError(ProviderErrorKind.BAD_REQUEST, f"{self.name}: base_url required")
        return base.rstrip("/") + "/chat/completions"

    @staticmethod
    def build_body(
        spec: ModelSpec, req: ChatRequest, max_tokens: int, *, stream: bool
    ) -> tuple[dict[str, Any], list[str]]:
        notes: list[str] = []
        body: dict[str, Any] = {
            "model": spec.upstream,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "max_tokens": max_tokens,
        }
        if req.temperature is not None:
            if spec.has("sampling"):
                body["temperature"] = req.temperature
            else:
                notes.append("temperature ignored: model does not accept sampling parameters")
        if req.stop and spec.has("stop_sequences"):
            body["stop"] = list(req.stop)
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body, notes

    @staticmethod
    def _usage(raw: dict[str, Any] | None) -> Usage | None:
        if not raw:
            return None
        details = raw.get("prompt_tokens_details") or {}
        return Usage(
            input_tokens=int(raw.get("prompt_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or 0),
            cached_input_tokens=int(details.get("cached_tokens") or 0),
        )

    async def complete(
        self, spec: ModelSpec, req: ChatRequest, creds: Credentials, *, max_tokens: int
    ) -> ChatResult:
        body, notes = self.build_body(spec, req, max_tokens, stream=False)
        try:
            resp = await self._http.post(
                self._url(creds),
                json=body,
                headers={"Authorization": f"Bearer {creds.api_key}"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(ProviderErrorKind.TIMEOUT, f"{self.name}: timeout") from exc
        except httpx.TransportError as exc:
            raise ProviderError(ProviderErrorKind.UNAVAILABLE, f"{self.name}: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                _status_kind(resp.status_code),
                f"{self.name}: HTTP {resp.status_code}",
                status=resp.status_code,
            )
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = self._usage(data.get("usage"))
        if usage is None:
            notes.append("usage estimated: provider did not report token usage")
            usage = Usage(estimate_input_tokens(req), estimate_text_tokens(text))
        return ChatResult(
            text=text,
            finish_reason=_FINISH.get(choice.get("finish_reason") or "stop", FinishReason.STOP),
            usage=usage,
            model_served=str(data.get("model") or spec.upstream),
            provider_request_id=data.get("id"),
            notes=tuple(notes),
        )

    async def stream(
        self, spec: ModelSpec, req: ChatRequest, creds: Credentials, *, max_tokens: int
    ) -> AsyncGenerator[StreamEvent, None]:
        body, notes = self.build_body(spec, req, max_tokens, stream=True)
        parts: list[str] = []
        usage: Usage | None = None
        finish = FinishReason.STOP
        model_served, request_id = spec.upstream, None
        try:
            async with self._http.stream(
                "POST",
                self._url(creds),
                json=body,
                headers={"Authorization": f"Bearer {creds.api_key}"},
                timeout=self._timeout,
            ) as resp:
                if resp.status_code >= 400:
                    raise ProviderError(
                        _status_kind(resp.status_code),
                        f"{self.name}: HTTP {resp.status_code}",
                        status=resp.status_code,
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    model_served = chunk.get("model") or model_served
                    request_id = chunk.get("id") or request_id
                    usage = self._usage(chunk.get("usage")) or usage
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            parts.append(delta)
                            yield TextDelta(delta)
                        if fr := choice.get("finish_reason"):
                            finish = _FINISH.get(fr, FinishReason.STOP)
        except httpx.TimeoutException as exc:
            raise ProviderError(ProviderErrorKind.TIMEOUT, f"{self.name}: timeout") from exc
        except httpx.TransportError as exc:
            raise ProviderError(ProviderErrorKind.UNAVAILABLE, f"{self.name}: {exc}") from exc
        text = "".join(parts)
        if usage is None:
            notes.append("usage estimated: provider did not report token usage")
            usage = Usage(estimate_input_tokens(req), estimate_text_tokens(text))
        yield StreamDone(ChatResult(text, finish, usage, model_served, request_id, tuple(notes)))
