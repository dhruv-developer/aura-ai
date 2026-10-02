"""Execution telemetry events emitted by the runtime."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from aura_shared.events.base import Event


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"  # quota / budget / policy
    CACHED = "cached"


class RoutingDecision(BaseModel):
    """Logged for every routing choice so later learning has off-policy data (V4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    router_version: str
    chosen_model: str
    candidates: list[str]
    features: dict[str, float | int | str | bool]
    propensity: float = Field(ge=0.0, le=1.0, default=1.0)
    reason: str = ""


class ModelCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0, default=0)
    latency_ms: int = Field(ge=0)
    cost: Decimal = Field(ge=0)
    currency: str = "USD"
    error: str | None = None
    credential_source: str = "platform"  # platform | byok (who pays the provider)


class ExecutionCompleted(Event):
    event_type: ClassVar[str] = "execution.completed"
    schema_version: ClassVar[int] = 1

    trace_id: str
    execution_id: str
    workspace_id: str
    environment_id: str | None = None
    policy_id: str | None = None
    policy_version: int | None = None
    workload_id: str | None = None
    session_id: str | None = None
    mode: str = "optimize"  # passthrough | optimize
    status: ExecutionStatus
    total_latency_ms: int = Field(ge=0)
    total_cost: Decimal = Field(ge=0)
    currency: str = "USD"
    calls: list[ModelCallRecord] = Field(default_factory=list)
    routing: RoutingDecision | None = None
    cache_hit: str | None = None  # exact | semantic | None
    payload_ref: str | None = None  # s3://... when the request/response is large
    error_code: str | None = None  # why a rejected/failed execution did not succeed
    policy_version_id: str | None = None
    deployment_id: str | None = None
    variant: str | None = None  # canary | stable during a deployment
    prompt_version_id: str | None = None
    cost_saved: Decimal | None = Field(default=None, ge=0)  # cache hits: compute not spent
    notes: list[str] = Field(default_factory=list)
