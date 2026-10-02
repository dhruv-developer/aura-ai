"""ExecutionPolicy and stricter-wins merging. Pure."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum, StrEnum


class QualityMode(StrEnum):
    AUTO = "auto"  # AURA decides from the task
    FAST = "fast"  # cheapest adequate (small tier)
    BALANCED = "balanced"  # medium tier
    BEST = "best"  # frontier tier


class FrontierUsage(IntEnum):
    """Ordered by permissiveness, so the stricter value is ``min``."""

    NEVER = 0
    ESCALATION_ONLY = 1  # frontier only as a fallback, never the first choice
    ALLOWED = 2


class CacheMode(IntEnum):
    """Ordered so the stricter value is ``min``: an org that forbids caching (e.g. for
    compliance) cannot be overridden by a request asking for semantic reuse."""

    OFF = 0
    EXACT = 1
    SEMANTIC = 2


class ContextMode(IntEnum):
    """Ordered so ``min`` is the most faithful: STRICT (never alter customer context)
    beats AUTO (compile when over budget) beats BUDGET (always enforce the budget)."""

    STRICT = 0
    AUTO = 1
    BUDGET = 2


class PiiMode(IntEnum):
    """Ordered so the stricter value is ``max``: an org that blocks personal data cannot
    be loosened by a request."""

    ALLOW = 0
    REDACT = 1  # replace identifiers with placeholders before any provider sees them
    BLOCK = 2  # refuse requests that contain them


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    # --- hard limits (stricter wins) ---
    max_cost_usd: Decimal | None = None  # per request, worst case
    max_latency_ms: int | None = None
    max_output_tokens: int | None = None
    allowed_models: frozenset[str] | None = None  # None = any
    disallowed_models: frozenset[str] = field(default_factory=frozenset)
    frontier_usage: FrontierUsage | None = None  # None = unset (allowed)
    data_regions: frozenset[str] | None = None  # None = any
    cache_mode: CacheMode | None = None  # None = unset (off)
    cache_ttl_s: int | None = None
    cache_similarity: float | None = None  # cosine threshold for semantic hits
    context_mode: ContextMode | None = None  # None = strict
    context_token_budget: int | None = None
    pii: PiiMode | None = None  # None = unset (allow)
    # --- preferences (most specific wins) ---
    quality: QualityMode | None = None
    effort: str | None = None

    @property
    def effective_frontier(self) -> FrontierUsage:
        return FrontierUsage.ALLOWED if self.frontier_usage is None else self.frontier_usage

    @property
    def effective_cache(self) -> CacheMode:
        return CacheMode.OFF if self.cache_mode is None else self.cache_mode

    @property
    def effective_context(self) -> ContextMode:
        return ContextMode.STRICT if self.context_mode is None else self.context_mode

    @property
    def effective_pii(self) -> PiiMode:
        return PiiMode.ALLOW if self.pii is None else self.pii

    @property
    def effective_quality(self) -> QualityMode:
        return self.quality or QualityMode.AUTO

    def permits_model(self, model_id: str) -> bool:
        if model_id in self.disallowed_models:
            return False
        return self.allowed_models is None or model_id in self.allowed_models


def _min[T: (int, Decimal)](a: T | None, b: T | None) -> T | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _intersect(a: frozenset[str] | None, b: frozenset[str] | None) -> frozenset[str] | None:
    if a is None:
        return b
    if b is None:
        return a
    return a & b


def merge(*layers: ExecutionPolicy | None) -> ExecutionPolicy:
    """Merge from least to most specific. Later layers can only tighten limits."""
    out = ExecutionPolicy()
    for p in layers:
        if p is None:
            continue
        frontier = (
            p.frontier_usage
            if out.frontier_usage is None
            else out.frontier_usage
            if p.frontier_usage is None
            else min(out.frontier_usage, p.frontier_usage)
        )
        out = ExecutionPolicy(
            max_cost_usd=_min(out.max_cost_usd, p.max_cost_usd),
            max_latency_ms=_min(out.max_latency_ms, p.max_latency_ms),
            max_output_tokens=_min(out.max_output_tokens, p.max_output_tokens),
            allowed_models=_intersect(out.allowed_models, p.allowed_models),
            disallowed_models=out.disallowed_models | p.disallowed_models,
            frontier_usage=frontier,
            data_regions=_intersect(out.data_regions, p.data_regions),
            cache_mode=(
                p.cache_mode
                if out.cache_mode is None
                else out.cache_mode
                if p.cache_mode is None
                else min(out.cache_mode, p.cache_mode)
            ),
            cache_ttl_s=_min(out.cache_ttl_s, p.cache_ttl_s),
            # Stricter = a *higher* similarity requirement.
            cache_similarity=(
                p.cache_similarity
                if out.cache_similarity is None
                else out.cache_similarity
                if p.cache_similarity is None
                else max(out.cache_similarity, p.cache_similarity)
            ),
            context_mode=(
                p.context_mode
                if out.context_mode is None
                else out.context_mode
                if p.context_mode is None
                else min(out.context_mode, p.context_mode)
            ),
            context_token_budget=_min(out.context_token_budget, p.context_token_budget),
            pii=(p.pii if out.pii is None else out.pii if p.pii is None else max(out.pii, p.pii)),
            quality=p.quality or out.quality,
            effort=p.effort or out.effort,
        )
    return out
