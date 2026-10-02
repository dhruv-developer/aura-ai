"""Provider-neutral chat types. Pure."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

Role = Literal["system", "user", "assistant"]


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"  # provider safety refusal
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: tuple[Message, ...]
    max_tokens: int | None = None
    temperature: float | None = None
    stop: tuple[str, ...] = ()
    effort: str | None = None  # low | medium | high | xhigh | max (where supported)

    def text(self) -> str:
        return "\n".join(m.content for m in self.messages)


@dataclass(frozen=True, slots=True)
class Usage:
    """``input_tokens`` counts all prompt tokens. ``cached_input_tokens`` and
    ``cache_write_tokens`` are subsets of it that are priced differently."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ChatResult:
    text: str
    finish_reason: FinishReason
    usage: Usage
    model_served: str  # upstream model that actually produced the answer
    provider_request_id: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class StreamDone:
    result: ChatResult


StreamEvent = TextDelta | StreamDone


class ProviderErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    BAD_REQUEST = "bad_request"
    AUTH = "auth"
    NOT_FOUND = "not_found"


RETRYABLE = frozenset(
    {
        ProviderErrorKind.RATE_LIMITED,
        ProviderErrorKind.OVERLOADED,
        ProviderErrorKind.UNAVAILABLE,
        ProviderErrorKind.TIMEOUT,
    }
)


class ProviderError(Exception):
    """Normalized upstream failure. ``retryable`` tells the runtime whether trying the
    next model in the fallback chain makes sense."""

    def __init__(self, kind: ProviderErrorKind, message: str, *, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def counts_against_health(self) -> bool:
        """Only failures that indicate the model/provider is unhealthy trip the circuit.
        Bad requests or our own auth problems do not."""
        return self.kind in RETRYABLE


@dataclass(frozen=True, slots=True)
class Credentials:
    api_key: str
    base_url: str | None = None
    source: Literal["platform", "byok"] = "platform"
