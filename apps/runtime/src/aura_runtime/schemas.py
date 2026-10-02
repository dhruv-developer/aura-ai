"""Wire formats: OpenAI-compatible chat completions + native /v1/execute."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aura_policy import (
    CacheMode,
    ContextMode,
    ExecutionPolicy,
    FrontierUsage,
    PiiMode,
    QualityMode,
)
from aura_providers.domain.types import ChatRequest, Message
from aura_shared.errors import ValidationFailed

MODEL_ALIASES: dict[str, QualityMode | None] = {
    "aura/auto": None,
    "auto": None,
    "aura": None,
    "aura/fast": QualityMode.FAST,
    "aura/balanced": QualityMode.BALANCED,
    "aura/best": QualityMode.BEST,
}

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class AuraOptions(BaseModel):
    """AURA extension, accepted as the ``aura`` field of any request body."""

    model_config = ConfigDict(extra="forbid")

    quality: Literal["auto", "fast", "balanced", "best"] | None = None
    max_cost_usd: Decimal | None = Field(default=None, gt=0)
    max_latency_ms: int | None = Field(default=None, gt=0)
    effort: Effort | None = None
    allowed_models: list[str] | None = None
    frontier: Literal["never", "escalation_only", "allowed"] | None = None
    data_regions: list[str] | None = None
    session_id: str | None = Field(default=None, max_length=128)
    workload: str | None = Field(default=None, max_length=120)
    sticky_key: str | None = Field(default=None, max_length=200)
    cache: Literal["off", "exact", "semantic"] | None = None
    memory: list[str] | None = Field(
        default=None, max_length=10, description="scope:ref, e.g. kb:handbook"
    )
    prompt: str | None = Field(default=None, max_length=120, description="registry prompt name")
    prompt_vars: dict[str, str] = Field(default_factory=dict)
    cache_ttl_s: int | None = Field(default=None, gt=0, le=30 * 86_400)
    context_mode: Literal["strict", "auto", "budget"] | None = None
    context_token_budget: int | None = Field(default=None, ge=256, le=2_000_000)
    pii: Literal["allow", "redact", "block"] | None = None

    def to_policy(self) -> ExecutionPolicy:
        return ExecutionPolicy(
            context_mode=ContextMode[self.context_mode.upper()] if self.context_mode else None,
            context_token_budget=self.context_token_budget,
            cache_mode=CacheMode[self.cache.upper()] if self.cache else None,
            cache_ttl_s=self.cache_ttl_s,
            max_cost_usd=self.max_cost_usd,
            max_latency_ms=self.max_latency_ms,
            allowed_models=frozenset(self.allowed_models) if self.allowed_models else None,
            frontier_usage=FrontierUsage[self.frontier.upper()] if self.frontier else None,
            data_regions=frozenset(self.data_regions) if self.data_regions else None,
            quality=QualityMode(self.quality) if self.quality else None,
            effort=self.effort,
            pii=PiiMode[self.pii.upper()] if self.pii else None,
        )


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    text: str | None = None


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: str | list[ContentPart] | None = None


class ChatCompletionIn(BaseModel):
    # OpenAI clients send many optional fields; unknown ones are ignored, but fields that
    # would change semantics if silently dropped are rejected explicitly below.
    model_config = ConfigDict(extra="ignore")

    model: str = "aura/auto"
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=2_000)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stop: str | list[str] | None = None
    stream: bool = False
    n: int | None = None
    tools: list[Any] | None = None
    functions: list[Any] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    user: str | None = None
    aura: AuraOptions | None = None

    def validate_supported(self) -> None:
        if self.n not in (None, 1):
            raise ValidationFailed("n > 1 is not supported")
        if self.tools or self.functions or self.tool_choice not in (None, "none"):
            raise ValidationFailed("tool/function calling is not supported yet")
        if self.response_format and self.response_format.get("type") not in (None, "text"):
            raise ValidationFailed("response_format other than text is not supported yet")

    def to_chat_request(self) -> ChatRequest:
        self.validate_supported()
        messages: list[Message] = []
        for m in self.messages:
            if m.role in ("tool", "function"):
                raise ValidationFailed("tool messages are not supported yet")
            if isinstance(m.content, list):
                if any(p.type != "text" for p in m.content):
                    raise ValidationFailed("only text content parts are supported yet")
                text = "".join(p.text or "" for p in m.content)
            else:
                text = m.content or ""
            role = "system" if m.role == "developer" else m.role
            messages.append(Message(role, text))  # type: ignore[arg-type]
        stop = (self.stop,) if isinstance(self.stop, str) else tuple(self.stop or ())
        return ChatRequest(
            messages=tuple(messages),
            max_tokens=self.max_completion_tokens or self.max_tokens,
            temperature=self.temperature,
            stop=stop,
            effort=self.aura.effort if self.aura else None,
        )


class ExecuteConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Spec §141: a numeric quality target, or a named mode.
    quality: float | Literal["auto", "fast", "balanced", "best"] | None = None
    max_cost: Decimal | None = Field(default=None, gt=0, description="USD per request")
    max_latency_ms: int | None = Field(default=None, gt=0)
    effort: Effort | None = None
    allowed_models: list[str] | None = None
    data_region: str | list[str] | None = None
    context_mode: Literal["strict", "auto", "budget"] | None = None
    context_budget: int | None = Field(default=None, ge=256, le=2_000_000)

    def quality_mode(self) -> QualityMode | None:
        q = self.quality
        if q is None:
            return None
        if isinstance(q, str):
            return QualityMode(q)
        if not 0 < q <= 1:
            raise ValidationFailed("numeric quality must be in (0, 1]")
        return (
            QualityMode.BEST
            if q >= 0.95
            else QualityMode.BALANCED
            if q >= 0.85
            else QualityMode.FAST
        )

    def to_policy(self) -> ExecutionPolicy:
        regions = (
            frozenset([self.data_region] if isinstance(self.data_region, str) else self.data_region)
            if self.data_region
            else None
        )
        return ExecutionPolicy(
            max_cost_usd=self.max_cost,
            max_latency_ms=self.max_latency_ms,
            allowed_models=frozenset(self.allowed_models) if self.allowed_models else None,
            data_regions=regions,
            quality=self.quality_mode(),
            effort=self.effort,
            context_mode=ContextMode[self.context_mode.upper()] if self.context_mode else None,
            context_token_budget=self.context_budget,
        )


class ExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=400_000)
    context: str | None = Field(default=None, max_length=4_000_000)
    system: str | None = Field(default=None, max_length=200_000)
    constraints: ExecuteConstraints | None = None
    model: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    session_id: str | None = Field(default=None, max_length=128)
    workload: str | None = Field(default=None, max_length=120)
    sticky_key: str | None = Field(default=None, max_length=200)
    memory: list[str] | None = Field(default=None, max_length=10)
    mode: Literal["direct", "planned"] = "direct"
    require_approval: bool = Field(
        default=False, description="planned runs: hold the final output for a human reviewer"
    )

    def to_chat_request(self) -> ChatRequest:
        messages: list[Message] = []
        if self.system:
            messages.append(Message("system", self.system))
        # Context is attached by the engine, which compiles it per the context policy.
        messages.append(Message("user", self.goal))
        return ChatRequest(
            messages=tuple(messages),
            max_tokens=self.max_tokens,
            effort=self.constraints.effort if self.constraints else None,
        )


class OutcomeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=4, max_length=64)
    outcome: Literal["success", "failure"]


def parse_memory_scopes(values: list[str] | None) -> tuple[tuple[str, str], ...]:
    out = []
    for v in values or []:
        scope, sep, ref = v.partition(":")
        if not sep or not ref or scope not in ("user", "workspace", "workload", "kb"):
            raise ValidationFailed(
                f"memory scope must look like 'kb:handbook' or 'user:u1', got {v!r}"
            )
        out.append((scope, ref))
    return tuple(out)


class PersonalMemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=2_000)
    kind: Literal["preference", "semantic"] = "preference"


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["user", "workspace", "workload", "kb"]
    scope_ref: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=20_000)
    kind: Literal["semantic", "episodic", "preference", "document"] = "semantic"
    importance: float = Field(default=0.5, ge=0, le=1)


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2_000_000)
    source: str = Field(default="api", max_length=120)


class TuneQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    expected: str = Field(min_length=1, max_length=2_000)


class TuneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[TuneQuestion] = Field(min_length=1, max_length=500)
    recall_target: float = Field(default=0.9, ge=0.1, le=1.0)


class OpeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=r"^(learned|always:[\w./-]+)$")
    workload: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=5_000, ge=20, le=10_000)


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reason: str = Field(default="", max_length=500)
